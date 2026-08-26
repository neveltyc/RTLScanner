//! `info` end to end: the binary, a database, the envelope a caller parses.
//!
//! Two kinds of database appear here. Most tests build one from the kit's own
//! DDL, which covers what `info` says about a seal without needing an exporter
//! at all. The ones that must see what the current producer actually writes run
//! the pinned `rtl-designdb`; where it is absent they say so and skip, and
//! `RTLSCANNER_REQUIRE_EXPORTER=1` turns that skip into a failure so a run
//! meant to cover them cannot pass by not running them.

use std::path::PathBuf;
use std::process::Command;

use designdb::{SCHEMA_VERSION, open::fixture};
use serde_json::Value;

const RTL: &str = "module info_cli(input logic clk, input logic d, output logic q);\n\
                   \x20 always_ff @(posedge clk) q <= d;\n\
                   endmodule\n";

/// The exporter, from the environment or the submodule build.
fn exporter(test: &str) -> Option<PathBuf> {
    let from_env = std::env::var("RTL_DESIGNDB").ok().map(PathBuf::from);
    let built = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../extern/RTLDebugDBKit/build/rtl-designdb");
    let found = from_env.into_iter().chain([built]).find(|p| p.exists());

    if found.is_none() {
        let demand = std::env::var("RTLSCANNER_REQUIRE_EXPORTER").is_ok_and(|v| v != "0");
        assert!(!demand, "{test} needs rtl-designdb and RTLSCANNER_REQUIRE_EXPORTER is set");
        eprintln!("SKIP {test}: no rtl-designdb (run `make designdb`, or set RTL_DESIGNDB)");
    }
    found
}

fn scanner() -> PathBuf {
    // The integration test binary sits beside the one under test.
    let mut p = std::env::current_exe().unwrap();
    p.pop();
    if p.ends_with("deps") {
        p.pop();
    }
    p.join("rtlscanner")
}

fn run(args: &[&str]) -> (String, String, i32) {
    let out = Command::new(scanner()).args(args).output().expect("running rtlscanner");
    (
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
        out.status.code().unwrap_or(-1),
    )
}

/// The JSON envelope on stdout, which every command produces under `--json`.
fn info_json(db: &PathBuf) -> (Value, i32) {
    let (stdout, stderr, code) = run(&["info", "--json", db.to_str().unwrap()]);
    assert!(stderr.is_empty(), "JSON keeps everything on stdout, got: {stderr}");
    (serde_json::from_str(&stdout).expect("stdout is one JSON envelope"), code)
}

/// Export a database from the RTL above, or `None` where no exporter is around.
fn exported(tag: &str, test: &str) -> Option<(PathBuf, PathBuf)> {
    let exporter = exporter(test)?;
    let dir = fixture::tmp(tag);
    let rtl = dir.join("info_cli.sv");
    std::fs::write(&rtl, RTL).unwrap();
    let db = dir.join("design.db");

    let out = Command::new(&exporter)
        .arg(&rtl)
        .args(["--top", "info_cli", "-o"])
        .arg(&db)
        .arg("-q")
        .output()
        .expect("running rtl-designdb");
    assert!(out.status.success(), "export failed: {}", String::from_utf8_lossy(&out.stderr));

    Some((db, rtl))
}

#[test]
fn info_reports_the_seal_of_a_freshly_exported_database() {
    let Some((db, _rtl)) = exported("info-seal", "info_reports_the_seal") else { return };
    let (v, code) = info_json(&db);

    assert_eq!(code, 0);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["data"]["schema_version"], SCHEMA_VERSION);
    assert_eq!(v["data"]["top"], "info_cli");
    assert_eq!(v["data"]["analysis"]["status"], "complete");
    assert_eq!(v["data"]["producer"]["tool"], "rtl-designdb");

    // The RTL is untouched since the export, so its recorded digest still holds
    // and a location in it means the line it names.
    assert_eq!(v["data"]["sources"][0]["state"], "current");
    assert_eq!(v["summary"]["sources_stale"], 0);
    assert_eq!(v["diagnostics"].as_array().unwrap().len(), 0);
}

#[test]
fn editing_the_rtl_makes_the_export_stale_without_making_it_an_error() {
    let Some((db, rtl)) = exported("info-stale", "editing_the_rtl_makes_it_stale") else { return };
    std::fs::write(&rtl, format!("{RTL}// a line the export never saw\n")).unwrap();

    let (v, code) = info_json(&db);

    // Drift is reported, not refused: the rows are still the design's, and
    // which of the two to rebuild is the caller's to decide.
    assert_eq!(code, 0);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["data"]["sources"][0]["state"], "stale");
    assert_eq!(v["summary"]["sources_stale"], 1);
    assert!(v["diagnostics"][0]["message"].as_str().unwrap().contains("changed"));
}

#[test]
fn a_partial_export_names_what_it_fell_short_of() {
    let dir = fixture::tmp("info-partial");
    let db = dir.join("design.db");
    fixture::write_db(
        &db,
        SCHEMA_VERSION,
        &["INSERT OR REPLACE INTO meta VALUES ('analysis_status', 'partial'), \
           ('error_count', '4'), ('empty_procedure_count', '2')"],
    );
    let (v, code) = info_json(&db);

    assert_eq!(code, 0);
    assert_eq!(v["data"]["analysis"]["status"], "partial");
    let note = v["diagnostics"][0]["message"].as_str().unwrap();
    assert!(note.contains("4 errors") && note.contains("2 empty procedures"), "{note}");

    // A skipped procedure contributes no drivers at all, which reads as an
    // undriven signal rather than as an error, so it gets its own note.
    let notes = v["diagnostics"].as_array().unwrap();
    assert!(notes.iter().any(|n| n["message"].as_str().unwrap().contains("procedure(s) were skipped")));
}

#[test]
fn a_truncated_recursion_is_told_apart_from_a_construct_the_export_declined() {
    let dir = fixture::tmp("info-recursion");
    let db = dir.join("design.db");
    // Neither count makes an export partial, but they mean different things: a
    // black box is named and a truncated tree is simply absent below the cut.
    fixture::write_db(
        &db,
        SCHEMA_VERSION,
        &["INSERT OR REPLACE INTO meta VALUES ('recursion_count', '7'), \
           ('unresolved_count', '3')"],
    );
    let (v, code) = info_json(&db);

    assert_eq!(code, 0);
    assert_eq!(v["data"]["analysis"]["status"], "complete");
    let notes: Vec<&str> =
        v["diagnostics"].as_array().unwrap().iter().map(|n| n["message"].as_str().unwrap()).collect();
    assert!(
        notes.iter().any(|n| n.contains("hierarchy stops at 7")),
        "a truncated tree must be reported: {notes:?}"
    );
}

#[test]
fn a_seal_that_contradicts_itself_is_reported_as_such() {
    let dir = fixture::tmp("info-malformed");
    let db = dir.join("design.db");
    // The contract: `partial` is exactly the five counts, so `partial` with
    // none of them is a file whose seal cannot be taken at its word.
    fixture::write_db(
        &db,
        SCHEMA_VERSION,
        &["INSERT OR REPLACE INTO meta VALUES ('analysis_status', 'partial')"],
    );
    let (v, _) = info_json(&db);

    let note = v["diagnostics"][0]["message"].as_str().unwrap();
    assert!(note.contains("contradicts itself"), "{note}");
}

#[test]
fn a_file_that_is_not_a_design_database_is_an_error_envelope_on_stdout() {
    let dir = fixture::tmp("info-junk");
    let junk = dir.join("junk.db");
    std::fs::write(&junk, b"not a database").unwrap();

    let (v, code) = info_json(&junk);
    assert_eq!(code, 1);
    assert_eq!(v["status"], "error");
    assert_eq!(v["errors"][0]["code"], "DB_UNREADABLE");
    assert!(v["data"].is_null());
}

#[test]
fn a_missing_file_names_itself_and_is_told_apart_from_an_unreadable_one() {
    let (v, code) = info_json(&PathBuf::from("/nonexistent/design.db"));

    assert_eq!(code, 1);
    assert_eq!(v["errors"][0]["code"], "INPUT_NOT_FOUND");
    assert_eq!(v["errors"][0]["details"]["path"], "/nonexistent/design.db");
}

#[test]
fn a_closed_pipe_ends_the_process_without_a_backtrace() {
    let dir = fixture::tmp("info-pipe");
    let db = dir.join("design.db");
    // Enough output to exceed the pipe buffer, so the write fails rather than
    // fitting in it: `| head -1` is an everyday way to read one line.
    let rows: Vec<String> = (0..5000)
        .map(|i| {
            format!("INSERT INTO src_file(id, path, digest) VALUES ({i}, '/p/f{i}.sv', 'd{i}')")
        })
        .collect();
    let seed: Vec<&str> = rows.iter().map(String::as_str).collect();
    fixture::write_db(&db, SCHEMA_VERSION, &seed);

    let piped = Command::new("sh")
        .arg("-c")
        .arg(format!("{} info --json {} | head -1", scanner().display(), db.display()))
        .output()
        .expect("running the pipeline");

    let stderr = String::from_utf8_lossy(&piped.stderr);
    assert!(!stderr.contains("panicked"), "a closed pipe produced a backtrace: {stderr}");
}

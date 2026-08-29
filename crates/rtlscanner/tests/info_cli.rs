//! `info` end to end: the binary, a database, the envelope a caller parses.
//!
//! Most tests build their database from the kit's own DDL, which covers what
//! `info` says about a seal without needing an exporter at all. The ones that
//! must see what the current producer writes run the pinned `rtl-designdb`.

mod common;

use common::{Exported, exported, json_of, scanner, tmp};

use std::path::{Path, PathBuf};
use std::process::Command;

use designdb::{SCHEMA_VERSION, open::fixture};
use serde_json::Value;

const RTL: &str = "module info_cli(input logic clk, input logic d, output logic q);\n\
                   \x20 always_ff @(posedge clk) q <= d;\n\
                   endmodule\n";

/// The JSON envelope of `info --json <db>`.
fn info_json(db: &Path) -> (Value, i32) {
    json_of(&["info", "--json", db.to_str().unwrap()])
}

#[test]
fn info_reports_the_seal_of_a_freshly_exported_database() {
    let Some(Exported { db, .. }) = exported("info_cli", RTL, "info_reports_the_seal") else {
        return;
    };
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
    let Some(Exported { db, rtl, .. }) =
        exported("info_cli", RTL, "editing_the_rtl_makes_it_stale")
    else {
        return;
    };
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
    let dir = tmp("info-partial");
    let db = dir.join("design.db");
    fixture::write_db(
        &db,
        SCHEMA_VERSION,
        &["UPDATE db_info SET analysis_status = 'partial', error_count = 4, \
           empty_procedure_count = 2"],
    );
    let (v, code) = info_json(&db);

    assert_eq!(code, 0);
    assert_eq!(v["data"]["analysis"]["status"], "partial");
    let note = v["diagnostics"][0]["message"].as_str().unwrap();
    assert!(note.contains("4 errors") && note.contains("2 empty procedures"), "{note}");

    // A skipped procedure contributes no drivers at all, which reads as an
    // undriven signal rather than as an error, so it gets its own note.
    let notes = v["diagnostics"].as_array().unwrap();
    assert!(
        notes.iter().any(|n| n["message"].as_str().unwrap().contains("procedure(s) were skipped"))
    );
}

#[test]
fn a_truncated_recursion_is_told_apart_from_a_construct_the_export_declined() {
    let dir = tmp("info-recursion");
    let db = dir.join("design.db");
    // Neither count makes an export partial, but they mean different things: a
    // black box is named and a truncated tree is simply absent below the cut.
    fixture::write_db(
        &db,
        SCHEMA_VERSION,
        &["UPDATE db_info SET recursion_count = 7, unresolved_count = 3"],
    );
    let (v, code) = info_json(&db);

    assert_eq!(code, 0);
    assert_eq!(v["data"]["analysis"]["status"], "complete");
    let notes: Vec<&str> = v["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n["message"].as_str().unwrap())
        .collect();
    assert!(
        notes.iter().any(|n| n.contains("hierarchy stops at 7")),
        "a truncated tree must be reported: {notes:?}"
    );
}

#[test]
fn a_seal_that_contradicts_itself_is_refused_by_v20() {
    let dir = tmp("info-malformed");
    let db = dir.join("design.db");
    // v20 enforces the seal through db_info CHECK; a contradictory
    // analysis_status vs the counts beside it can no longer be written.
    fixture::write_db(&db, SCHEMA_VERSION, &[]);
    let c = designdb::Connection::open(&db).unwrap();
    let err = c
        .execute_batch("UPDATE db_info SET analysis_status = 'partial'")
        .unwrap_err();
    assert!(err.to_string().contains("CHECK"), "expected CHECK violation: {err}");
}

#[test]
fn a_file_that_is_not_a_design_database_is_an_error_envelope_on_stdout() {
    let dir = tmp("info-junk");
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
    let dir = tmp("info-pipe");
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

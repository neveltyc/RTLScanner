//! `info` end to end: the binary, a real database, the envelope a caller parses.
//!
//! The database is built by the pinned `rtl-designdb` from RTL written here, so
//! what is asserted is what the current producer actually writes. Where that
//! binary is absent the test skips rather than fails: building it compiles
//! slang, which is not a precondition for running the unit tests.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

const RTL: &str = "module info_cli(input logic clk, input logic d, output logic q);\n\
                   \x20 always_ff @(posedge clk) q <= d;\n\
                   endmodule\n";

/// The exporter, from the environment or the submodule build. `None` skips.
fn exporter() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("RTL_DESIGNDB") {
        let p = PathBuf::from(p);
        return p.exists().then_some(p);
    }
    let built = repo_root().join("extern/RTLDebugDBKit/build/rtl-designdb");
    built.exists().then_some(built)
}

fn repo_root() -> PathBuf {
    // CARGO_MANIFEST_DIR is crates/rtlscanner.
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_path_buf()
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

struct Fixture {
    _dir: PathBuf,
    db: PathBuf,
    rtl: PathBuf,
}

/// Export a database from the RTL above, or `None` where no exporter is around.
fn fixture(tag: &str) -> Option<Fixture> {
    let exporter = exporter()?;
    let dir = std::env::temp_dir().join(format!("rtlscanner-info-cli-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();

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

    Some(Fixture { _dir: dir, db, rtl })
}

fn run(args: &[&str]) -> (String, String, i32) {
    let out = Command::new(scanner()).args(args).output().expect("running rtlscanner");
    (
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
        out.status.code().unwrap_or(-1),
    )
}

#[test]
fn info_reports_the_seal_of_a_freshly_exported_database() {
    let Some(fx) = fixture("seal") else { return };
    let (stdout, _, code) = run(&["info", "--json", fx.db.to_str().unwrap()]);
    let v: Value = serde_json::from_str(&stdout).expect("stdout is one JSON envelope");

    assert_eq!(code, 0);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["data"]["schema_version"], designdb::SCHEMA_VERSION);
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
    let Some(fx) = fixture("stale") else { return };
    std::fs::write(&fx.rtl, format!("{RTL}// a line the export never saw\n")).unwrap();

    let (stdout, _, code) = run(&["info", "--json", fx.db.to_str().unwrap()]);
    let v: Value = serde_json::from_str(&stdout).unwrap();

    // Drift is reported, not refused: the rows are still the design's, and
    // which of the two to fix is the caller's to decide.
    assert_eq!(code, 0);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["data"]["sources"][0]["state"], "stale");
    assert_eq!(v["summary"]["sources_stale"], 1);
    let note = v["diagnostics"][0]["message"].as_str().unwrap();
    assert!(note.contains("changed"), "{note}");
}

#[test]
fn a_file_that_is_not_a_design_database_is_an_error_envelope_on_stdout() {
    let dir = std::env::temp_dir().join(format!("rtlscanner-info-cli-junk-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let junk = dir.join("junk.db");
    std::fs::write(&junk, b"not a database").unwrap();

    let (stdout, stderr, code) = run(&["info", "--json", junk.to_str().unwrap()]);
    let v: Value = serde_json::from_str(&stdout).expect("a failure is still an envelope");

    assert_eq!(code, 1);
    assert_eq!(v["status"], "error");
    assert_eq!(v["errors"][0]["code"], "DB_UNREADABLE");
    assert!(v["data"].is_null());
    assert!(stderr.is_empty(), "JSON keeps the failure on stdout");
}

#[test]
fn a_missing_file_names_itself_and_is_told_apart_from_an_unreadable_one() {
    let (stdout, _, code) = run(&["info", "--json", "/nonexistent/design.db"]);
    let v: Value = serde_json::from_str(&stdout).unwrap();

    assert_eq!(code, 1);
    assert_eq!(v["errors"][0]["code"], "INPUT_NOT_FOUND");
    assert_eq!(v["errors"][0]["details"]["path"], "/nonexistent/design.db");
}

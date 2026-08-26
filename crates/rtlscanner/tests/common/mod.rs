//! Running the binary against a database built from RTL written by the test.
//!
//! Exporting needs `rtl-designdb`; where it is absent a test says so and skips,
//! and `RTLSCANNER_REQUIRE_EXPORTER=1` turns that skip into a failure so a run
//! meant to cover these cannot pass by not running them.

// Every integration test binary compiles this module separately, so helpers
// only some of them call are dead code in the rest.
#![allow(dead_code)]

use std::path::PathBuf;
use std::process::Command;

use serde_json::Value;

/// The exporter, from the environment or the submodule build.
pub fn exporter(test: &str) -> Option<PathBuf> {
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

/// The binary under test, which sits beside the integration test binary.
pub fn scanner() -> PathBuf {
    let mut p = std::env::current_exe().unwrap();
    p.pop();
    if p.ends_with("deps") {
        p.pop();
    }
    p.join("rtlscanner")
}

pub struct Exported {
    pub dir: PathBuf,
    pub db: PathBuf,
    pub rtl: PathBuf,
}

/// A fresh directory for one test, named so concurrent tests do not share one.
pub fn tmp(tag: &str) -> PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!("rtlscanner-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&p);
    std::fs::create_dir_all(&p).unwrap();
    p
}

/// Write `rtl` and export it, or `None` where no exporter is around.
///
/// The directory is the test's own: tests run concurrently, and two sharing one
/// would race to write the database the other is reading.
pub fn exported(top: &str, rtl: &str, test: &str) -> Option<Exported> {
    let exporter = exporter(test)?;
    let dir = tmp(test);
    let sv = dir.join(format!("{top}.sv"));
    std::fs::write(&sv, rtl).unwrap();
    let db = dir.join("design.db");

    let out = Command::new(&exporter)
        .arg(&sv)
        .args(["--top", top, "-o"])
        .arg(&db)
        .arg("-q")
        .output()
        .expect("running rtl-designdb");
    assert!(out.status.success(), "export failed: {}", String::from_utf8_lossy(&out.stderr));

    Some(Exported { dir, db, rtl: sv })
}

pub fn run(args: &[&str]) -> (String, String, i32) {
    let out = Command::new(scanner()).args(args).output().expect("running rtlscanner");
    (
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
        out.status.code().unwrap_or(-1),
    )
}

/// The JSON envelope of one command, with the invariant every command holds:
/// under `--json`, everything is on stdout.
pub fn json_of(args: &[&str]) -> (Value, i32) {
    let (stdout, stderr, code) = run(args);
    assert!(stderr.is_empty(), "JSON keeps everything on stdout, got: {stderr}");
    (serde_json::from_str(&stdout).expect("stdout is one JSON envelope"), code)
}

/// `trace --json <db> <args...>`.
pub fn json_trace(fx: &Exported, args: &[&str]) -> (Value, i32) {
    let mut argv = vec!["trace", "--json", fx.db.to_str().unwrap()];
    argv.extend(args);
    json_of(&argv)
}

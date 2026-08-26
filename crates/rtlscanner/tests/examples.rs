//! The worked answers in `examples/`, and the check that they still are.
//!
//! An agent reading this repository should be able to see the shape of an
//! answer without running anything, and a shape kept by hand drifts: the
//! predecessor's examples were three versions behind and nothing said so. So
//! they are generated from the binary in this tree, and this test regenerates
//! them and compares — a change to an answer's shape either lands here as a
//! diff or fails.
//!
//! `make examples` runs this with `RTLSCANNER_WRITE_EXAMPLES=1` to rewrite them.

mod common;

use std::path::PathBuf;

use common::{exporter, run, tmp};

/// One question, its file, and how it is asked.
const QUESTIONS: [(&str, &[&str]); 8] = [
    ("info", &["info"]),
    ("tree", &["tree", "--depth", "0"]),
    ("find", &["find", "*ed"]),
    ("trace-driver", &["trace", "top.muxed"]),
    ("trace-load", &["trace", "top.u_core.sum", "--load"]),
    ("trace-slices", &["trace", "top.packed_up"]),
    ("fanin-comb", &["fanin", "top.out", "--comb"]),
    ("path", &["path", "top.a", "top.out"]),
];

fn examples_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples")
}

/// Every worked answer is at least a well-formed envelope.
///
/// Checked without an exporter, so a run that cannot regenerate them still says
/// something about them: the drift check below is the only thing standing
/// between these files and silent staleness, and it skips where no exporter is
/// around. A test that does nothing and reports ok is what let the
/// predecessor's examples fall three versions behind.
#[test]
fn the_worked_answers_are_envelopes() {
    const KEYS: [&str; 8] =
        ["command", "data", "diagnostics", "errors", "status", "summary", "tool", "version"];
    for (name, _) in QUESTIONS {
        let path = examples_dir().join(format!("{name}.json"));
        let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{name}: {e}"));
        let body = text.split_once("\n\n").expect("a command line, then its answer").1;
        let v: serde_json::Value =
            serde_json::from_str(body).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(v.as_object().unwrap().keys().collect::<Vec<_>>(), KEYS, "{name}");
        assert_eq!(v["status"], "ok", "{name}");
    }
}

#[test]
fn the_worked_answers_are_what_the_tool_answers() {
    let Some(exporter) = exporter("the_worked_answers") else { return };
    let dir = examples_dir().canonicalize().expect("examples/ is in the repository");

    // Exported beside a copy of the source, so the recorded paths are stable
    // and the quoted lines are the ones in the repository.
    let work = tmp("examples");
    let db = work.join("design.db");
    // Run from the design's own directory: the export records the spelling it
    // was given, relative to where it ran, and a path that depends on the
    // caller's working directory is not one a worked answer can show.
    let out = std::process::Command::new(&exporter)
        .current_dir(&dir)
        .args(["design.sv", "--top", "top", "-o"])
        .arg(&db)
        .arg("-q")
        .output()
        .expect("running rtl-designdb");
    assert!(out.status.success(), "export failed: {}", String::from_utf8_lossy(&out.stderr));

    // Spelled as the other switches are: set to anything but `0`.
    let writing = std::env::var("RTLSCANNER_WRITE_EXAMPLES").is_ok_and(|v| v != "0");
    let mut stale = Vec::new();

    for (name, argv) in QUESTIONS {
        let mut args: Vec<&str> = vec![argv[0], "--json", db.to_str().unwrap()];
        args.extend(&argv[1..]);
        let (stdout, stderr, code) = run(&args);
        assert!(stderr.is_empty(), "{name}: {stderr}");
        assert_eq!(code, 0, "{name} failed: {stdout}");

        // Where this ran is not where a reader will run it: the paths are the
        // one part of an answer that cannot be stable, and are the only part
        // rewritten.
        let answer = stdout
            .replace(db.to_str().unwrap(), "design.db")
            .replace(&format!("{}/", dir.display()), "");
        let shown = format!("$ rtlscanner {} --json design.db\n\n{answer}", argv.join(" "));

        let path = dir.join(format!("{name}.json"));
        match (writing, std::fs::read_to_string(&path)) {
            (true, _) => {
                std::fs::write(&path, &shown).unwrap();
                println!("wrote {}", path.display());
            }
            (false, Ok(kept)) if kept == shown => {}
            (false, _) => stale.push(name),
        }
    }

    assert!(
        stale.is_empty(),
        "these worked answers no longer match what the tool answers: {stale:?}\n\
         If the change was meant, run `make examples`. If RTL_DESIGNDB points at \
         a different build of the exporter, the difference may be its identity \
         rather than this tool's behaviour."
    );
}

//! The invariants, on designs nobody wrote for this tool.
//!
//! Nothing here is an expected value. Every assertion is the walk checked
//! against itself — fan-in against fan-out, a cone against a deeper one, a
//! window against the whole object — so it holds of any design at all, and a
//! failure is a defect rather than a design this test had not met.
//!
//! A property comparing a walk to another walk is blind to a defect both
//! walks share, so the load-bearing checks are the ones that compare
//! different commands: `first_hop_is_a_trace`, where a trace assembles its
//! answer from the rows and a cone from the walk, and `containment`'s
//! full-width window. The fixtures cover what neither reaches, on designs
//! small enough to state the expected answer.
//!
//! The RTL is not vendored: `RTLSCANNER_CORES` names a directory holding the
//! checkouts, one subdirectory per core, at these commits — pinned, because a
//! corpus that moves turns an invariant failure into a question about which
//! commit it was:
//!
//! * `picorv32`  — https://github.com/YosysHQ/picorv32
//!   at 87c89acc18994c8cf9a2311e871818e87d304568
//! * `tinyriscv` — https://github.com/liangkangnan/tinyriscv
//!   at 7cb8c8aa0676a27bacb574a80ac20a3c4508c939
//!
//! Without the directory these say so and skip; `RTLSCANNER_REQUIRE_CORES=1`
//! turns that skip into a failure, which is what `make test-cores` sets.

mod common;

use std::path::{Path, PathBuf};

use serde_json::Value;

use common::{exporter, json_of, tmp};

/// A core, and what of it is the design.
struct Core {
    /// Its directory under `RTLSCANNER_CORES`, and the name failures carry.
    dir: &'static str,
    top: &'static str,
    /// Sources relative to that directory; a directory contributes every `.v`
    /// and `.sv` below it. Named here rather than taken from a filelist the
    /// checkout carries, because what these two ship is a simulation setup —
    /// testbenches, and in one case no filelist at all.
    sources: &'static [&'static str],
}

const PICORV32: Core = Core { dir: "picorv32", top: "picorv32", sources: &["picorv32.v"] };

const TINYRISCV: Core = Core { dir: "tinyriscv", top: "tinyriscv_soc_top", sources: &["rtl"] };

/// Every `.v` and `.sv` at or below `path`, in a stable order.
fn sources_under(path: &std::path::Path, into: &mut Vec<PathBuf>) {
    if path.is_file() {
        into.push(path.to_path_buf());
        return;
    }
    let mut here: Vec<PathBuf> = std::fs::read_dir(path)
        .expect("a source path that is there")
        .map(|e| e.unwrap().path())
        .collect();
    here.sort();
    for entry in here {
        match entry.extension().and_then(|e| e.to_str()) {
            _ if entry.is_dir() => sources_under(&entry, into),
            Some("v" | "sv") => into.push(entry),
            _ => {}
        }
    }
}

/// How many nets each sweep asks about, spread evenly over the design's own
/// order. Every question is a process, so this is the cost knob; a stride over
/// the whole list beats a prefix, which would be one module's worth.
const SAMPLE: usize = 14;

/// Multi-bit nets are sampled apart from the rest: the window check needs
/// them and they are a minority, so one stride over every net would leave
/// most of them unasked.
const WIDE_SAMPLE: usize = 20;

/// Export one core, or `None` where the corpus or the exporter is absent.
fn exported(core: &Core, test: &str) -> Option<PathBuf> {
    let exporter = exporter(test)?;
    let root = match std::env::var("RTLSCANNER_CORES") {
        Ok(dir) => PathBuf::from(dir),
        Err(_) => {
            let demand = std::env::var("RTLSCANNER_REQUIRE_CORES").is_ok_and(|v| v != "0");
            assert!(!demand, "{test} needs RTLSCANNER_CORES and RTLSCANNER_REQUIRE_CORES is set");
            eprintln!("SKIP {test}: no RTLSCANNER_CORES (a directory holding the core checkouts)");
            return None;
        }
    };

    let dir = root.join(core.dir);
    assert!(dir.is_dir(), "RTLSCANNER_CORES has no {}", core.dir);
    let mut sources = Vec::new();
    for name in core.sources {
        sources_under(&dir.join(name), &mut sources);
    }
    assert!(!sources.is_empty(), "{} has no sources under {:?}", core.dir, core.sources);

    let db = tmp(test).join("design.db");
    let out = std::process::Command::new(&exporter)
        .current_dir(&dir)
        .args(&sources)
        .args(["--top", core.top, "-o"])
        .arg(&db)
        .arg("-q")
        .output()
        .expect("running rtl-designdb");
    assert!(
        out.status.success(),
        "exporting {}: {}",
        core.dir,
        String::from_utf8_lossy(&out.stderr)
    );
    Some(db)
}

fn ask(db: &Path, cmd: &str, args: &[&str]) -> Value {
    let mut argv = vec![cmd, "--json", db.to_str().unwrap()];
    argv.extend(args);
    let (v, code) = json_of(&argv);
    assert_eq!(code, 0, "{cmd} {args:?}: {v}");
    v
}

/// A cone as its edge set. An edge is its two ends, what kind of arc it is and
/// which bits of each end it touches — the duality check compares all of it,
/// since two walks agreeing on the pair of names while disagreeing about the
/// arc between them is a disagreement.
type Arc = (String, String, String, String, String);

fn arcs(v: &Value) -> Vec<Arc> {
    let text = |e: &Value, k: &str| e[k].as_str().unwrap_or("").to_string();
    v["data"]["edges"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| {
            (
                text(e, "source"),
                text(e, "target"),
                text(e, "kind"),
                text(e, "source_bits"),
                text(e, "target_bits"),
            )
        })
        .collect()
}

/// The whole cone of `signal`, in one direction.
fn cone(db: &Path, dir: &str, signal: &str) -> Value {
    ask(db, dir, &[signal, "--depth", "0", "--limit", "0"])
}

/// Every net of the design, with its width, in the design's own order.
///
/// A net whose type has no bits at all — an event, a string — carries no width
/// and counts as one, which only decides whether the window check asks about
/// it.
fn nets(db: &Path) -> Vec<(String, u64)> {
    let found = ask(db, "find", &["*", "--limit", "0"]);
    // `find` stops after its own cap and says so. Which nets went unasked is
    // worth knowing when reading a green run.
    if found["summary"]["capped"] == true {
        eprintln!("NOTE: find capped at {} of this design's nets", found["summary"]["hits"]);
    }
    found["data"]["hits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| {
            let width = h["detail"]
                .as_str()
                .and_then(|d| d.split_once('['))
                .and_then(|(_, r)| r.split_once(' '))
                .and_then(|(n, _)| n.parse().ok())
                .unwrap_or(1);
            (h["path"].as_str().unwrap().to_string(), width)
        })
        .collect()
}

/// `want` of `from`, spread evenly rather than taken off the front — a prefix
/// would be one module's worth.
fn spread(from: Vec<(String, u64)>, want: usize) -> Vec<(String, u64)> {
    if from.len() <= want {
        return from;
    }
    // Ceiling division, so the stride reaches the end of the list instead of
    // running out partway and taking a prefix of it.
    let stride = from.len().div_ceil(want);
    from.into_iter().step_by(stride).collect()
}

/// Fan-in and fan-out read one relation. Asking both and comparing is the only
/// check here that involves two answers neither of which is derived from the
/// other, so a disagreement convicts one of them without saying which.
fn duality(db: &Path, signal: &str) {
    // The first hop only: a source's own fan-out is a separate walk, and the
    // point is made by the arcs that touch the net asked about.
    for arc in arcs(&ask(db, "fanin", &[signal, "--depth", "1", "--limit", "0"])) {
        let out = arcs(&cone(db, "fanout", &arc.0));
        assert!(
            out.contains(&arc),
            "fan-in of {signal} has {arc:?}; fan-out of {} does not",
            arc.0
        );
    }
}

/// A deeper walk keeps what a shallower one found, and a combinational walk is
/// part of the unbounded one.
fn containment(db: &Path, signal: &str) {
    let shallow = arcs(&ask(db, "fanin", &[signal, "--depth", "2", "--limit", "0"]));
    let whole = arcs(&cone(db, "fanin", signal));
    for edge in &shallow {
        assert!(whole.contains(edge), "the unbounded cone of {signal} lost {edge:?}");
    }

    let comb = arcs(&ask(db, "fanin", &[signal, "--comb", "--limit", "0"]));
    for edge in &comb {
        // A cone that stops at state elements reaches no further than one that
        // does not, so every edge of it is an edge of the other.
        assert!(whole.contains(edge), "the unbounded cone of {signal} lacks {edge:?} from --comb");
    }
}

/// A window covering every bit asks about the whole object.
///
/// The cheapest invariant there is, and the one that catches a walk that
/// emits an arc without following it.
fn every_bit_is_the_whole(db: &Path, signal: &str, width: u64) {
    let whole = arcs(&cone(db, "fanin", signal));
    let spelled = format!("{signal}[{}:0]", width - 1);
    let (v, code) = json_of(&[
        "fanin",
        "--json",
        db.to_str().unwrap(),
        &spelled,
        "--depth",
        "0",
        "--limit",
        "0",
    ]);
    if code != 0 {
        // BAD_SELECT is the answer to two spellings this cannot make: an
        // aggregate, which has no one declared range to measure against, and a
        // net whose range does not start at zero, which `[w-1:0]` misspells.
        // Both leave this net unchecked, so both are counted rather than
        // passed over — a check that quietly does not run is one that reports
        // ok for the wrong reason.
        assert_eq!(v["errors"][0]["code"], "BAD_SELECT", "{spelled}: {v}");
        eprintln!("NOTE: {signal} has no zero-based declared range; window unchecked");
        return;
    }
    let every_bit = arcs(&v);
    for edge in &whole {
        assert!(every_bit.contains(edge), "{spelled} lost {edge:?}");
    }
    for edge in &every_bit {
        assert!(whole.contains(edge), "{spelled} invented {edge:?}");
    }
}

/// One hop of a cone and a trace are the same question asked twice.
///
/// The one check here that is not a walk compared to itself: a trace assembles
/// its answer from the rows a signal names, a cone assembles it from the walk,
/// and a defect in the walk shows up as a disagreement rather than as a
/// smaller answer that is still consistent with itself.
///
/// Asked twice over, because the two commands differ on what a condition is: a
/// trace files the conditions gating a statement under that statement's
/// `gates` unless `--control` is given, while a cone makes them arcs of their
/// own and has them on by default. `--no-control` compares the data half;
/// `--control` on both sides compares the whole, which is what notices a walk
/// that stopped following conditions at all.
fn first_hop_is_a_trace(db: &Path, signal: &str) {
    for (cone_args, trace_args) in [
        (["--depth", "1", "--limit", "0", "--no-control"].as_slice(), [].as_slice()),
        (["--depth", "1", "--limit", "0"].as_slice(), ["--control"].as_slice()),
    ] {
        let mut argv = vec![signal];
        argv.extend(cone_args);
        let mut from_cone: Vec<String> =
            arcs(&ask(db, "fanin", &argv)).into_iter().map(|a| a.0).collect();
        from_cone.sort();
        from_cone.dedup();

        let mut argv = vec![signal];
        argv.extend(trace_args);
        let mut from_trace: Vec<String> = ask(db, "trace", &argv)["data"]["hops"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|h| h["signals"].as_array().unwrap())
            .map(|s| s.as_str().unwrap().to_string())
            .collect();
        from_trace.sort();
        from_trace.dedup();

        assert_eq!(
            from_cone, from_trace,
            "one hop of {signal}'s cone is its trace, asked with {trace_args:?}"
        );
    }
}

/// A clipped answer counts the whole cone and stays a graph.
fn clipping_is_honest(db: &Path, signal: &str) {
    let whole = cone(db, "fanin", signal);
    let total = whole["summary"]["edges"].as_u64().unwrap();
    if total < 2 {
        return;
    }
    let clipped = ask(db, "fanin", &[signal, "--depth", "0", "--limit", "1"]);
    assert_eq!(clipped["summary"]["edges"], total, "{signal}: the count is of the cone");
    assert_eq!(clipped["summary"]["shown_edges"], 1, "{signal}");

    for answer in [&whole, &clipped] {
        let shown: Vec<&str> = answer["data"]["nodes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n["path"].as_str().unwrap())
            .collect();
        for (src, tgt, ..) in arcs(answer) {
            assert!(shown.contains(&src.as_str()), "{signal}: {src} is an endpoint and not a node");
            assert!(shown.contains(&tgt.as_str()), "{signal}: {tgt} is an endpoint and not a node");
        }
    }
}

fn sweep(core: &Core, test: &str) {
    let Some(db) = exported(core, test) else { return };

    // The export is only as trustworthy as it says it is, and every answer
    // below rests on it. tinyriscv elaborates with errors; what must hold is
    // that the panel says so rather than that it is clean.
    let info = ask(&db, "info", &[]);
    assert_eq!(info["data"]["schema_version"], 19);
    assert_eq!(info["data"]["top"], core.top);

    let all = nets(&db);
    assert!(all.len() > SAMPLE, "a design with fewer nets than the sample is not a core");
    for (signal, _) in spread(all.clone(), SAMPLE) {
        duality(&db, &signal);
        containment(&db, &signal);
        first_hop_is_a_trace(&db, &signal);
        clipping_is_honest(&db, &signal);
    }
    let wide: Vec<(String, u64)> = all.into_iter().filter(|(_, w)| *w > 1).collect();
    assert!(!wide.is_empty(), "a core has multi-bit nets");
    for (signal, width) in spread(wide, WIDE_SAMPLE) {
        every_bit_is_the_whole(&db, &signal, width);
    }
}

#[test]
fn the_walk_holds_together_on_picorv32() {
    sweep(&PICORV32, "the_walk_holds_together_on_picorv32");
}

#[test]
fn the_walk_holds_together_on_tinyriscv() {
    sweep(&TINYRISCV, "the_walk_holds_together_on_tinyriscv");
}

//! The invariants, on designs nobody wrote for this tool.
//!
//! The fixtures elsewhere are small enough to read whole, which is also what
//! makes them agreeable: they hold the shapes I thought to write. A processor
//! someone else wrote holds the ones I did not, and the worst defects so far
//! were found by running these same properties over one — a bit-select cone
//! that quietly lost a fifth of its edges on tinyriscv, and a state-element
//! test that stopped one hop late across a port.
//!
//! Nothing here is an expected value. Every assertion is the walk checked
//! against itself — fan-in against fan-out, a cone against a deeper one, a
//! window against the whole object — so it holds of any design at all, and a
//! failure is a defect rather than a design this test had not met.
//!
//! The RTL is not vendored: `RTLSCANNER_CORES` names a directory holding the
//! checkouts, one subdirectory per core. Without it these say so and skip;
//! `RTLSCANNER_REQUIRE_CORES=1` turns that skip into a failure, which is what
//! `make test-cores` and CI set. The upstreams and the commits they are pinned
//! to are in `.github/workflows/ci.yml`, which is what fetches them — naming
//! them here as well would be two places to keep agreeing.

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

/// A cone as its edge set, which is what the invariants compare.
fn arcs(v: &Value) -> Vec<(String, String)> {
    v["data"]["edges"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| {
            (e["source"].as_str().unwrap().to_string(), e["target"].as_str().unwrap().to_string())
        })
        .collect()
}

/// The whole cone of `signal`, in one direction.
fn cone(db: &Path, dir: &str, signal: &str) -> Value {
    ask(db, dir, &[signal, "--depth", "0", "--limit", "0"])
}

/// Every net of the design, with its width, in the design's own order.
fn nets(db: &Path) -> Vec<(String, u64)> {
    ask(db, "find", &["*", "--limit", "0"])["data"]["hits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| {
            let detail = h["detail"].as_str().unwrap();
            let width = detail
                .split_once('[')
                .and_then(|(_, r)| r.split_once(' '))
                .and_then(|(n, _)| n.parse().ok())
                .unwrap_or(1);
            (h["path"].as_str().unwrap().to_string(), width)
        })
        .collect()
}

/// `SAMPLE` nets spread over the design.
fn sampled(db: &Path) -> Vec<(String, u64)> {
    let all = nets(db);
    assert!(all.len() > SAMPLE, "a core has more nets than the sample");
    let stride = all.len() / SAMPLE;
    all.into_iter().step_by(stride).take(SAMPLE).collect()
}

/// Fan-in and fan-out read one relation. Asking both and comparing is the only
/// check here that involves two answers neither of which is derived from the
/// other, so a disagreement convicts one of them without saying which.
fn duality(db: &Path, signal: &str) {
    // The first hop only: a source's own fan-out is a separate walk, and the
    // point is made by the arcs that touch the net asked about.
    for (src, tgt) in arcs(&ask(db, "fanin", &[signal, "--depth", "1", "--limit", "0"])) {
        let out = arcs(&cone(db, "fanout", &src));
        assert!(
            out.contains(&(src.clone(), tgt.clone())),
            "fan-in of {signal} has {src} -> {tgt}; fan-out of {src} does not"
        );
    }
}

/// A deeper walk keeps what a shallower one found, a combinational walk is
/// part of the unbounded one, and a window is part of the whole object.
fn containment(db: &Path, signal: &str, width: u64) {
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

    if width < 2 {
        return;
    }
    // A window covering every bit asks about the whole object. The cheapest
    // invariant there is, and the one that caught a walk emitting an arc it
    // never followed.
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
        // An aggregate has no one declared range to measure a select against,
        // and says so. Any other refusal is not a thing this asked for.
        assert_eq!(v["errors"][0]["code"], "BAD_SELECT", "{spelled}: {v}");
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
/// `--no-control` because the two commands differ on what a condition is:
/// a trace carries the conditions gating each statement in that statement's
/// `gates`, so its `signals` are the values alone, while a cone makes them
/// arcs of their own and has them on by default. The data half is the half
/// both spell the same way.
fn first_hop_is_a_trace(db: &Path, signal: &str) {
    let mut from_cone: Vec<String> =
        arcs(&ask(db, "fanin", &[signal, "--depth", "1", "--limit", "0", "--no-control"]))
            .into_iter()
            .map(|(s, _)| s)
            .collect();
    from_cone.sort();
    from_cone.dedup();

    let mut from_trace: Vec<String> = ask(db, "trace", &[signal])["data"]["hops"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|h| h["signals"].as_array().unwrap())
        .map(|s| s.as_str().unwrap().to_string())
        .collect();
    from_trace.sort();
    from_trace.dedup();

    assert_eq!(from_cone, from_trace, "one hop of {signal}'s cone is its trace");
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
        for (src, tgt) in arcs(answer) {
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

    for (signal, width) in sampled(&db) {
        duality(&db, &signal);
        containment(&db, &signal, width);
        first_hop_is_a_trace(&db, &signal);
        clipping_is_honest(&db, &signal);
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

//! `fanin`, `fanout` and `path`, and the invariants that hold between them.
//!
//! Most of this file checks the walk against itself rather than against an
//! expected answer. There is no reference implementation to diff against — the
//! database's own truthfulness is the producer's to guarantee, and a commercial
//! tool's output is neither reachable nor comparable — so what stands in for
//! one is a set of properties that must hold on any design at all: fan-in and
//! fan-out must agree, a cone must grow with its depth, counts must not lie
//! about what was clipped, and a walk over bidirectional structure must end.
//!
//! A property that holds on a fixture and on a real core is worth more than an
//! expected value that holds on a fixture.

mod common;

use common::{Exported, exported, json_of};
use serde_json::Value;

const PIPE: &str = r#"
module leaf(input logic clk, en, input logic [7:0] d, output logic [7:0] q);
  always_ff @(posedge clk) if (en) q <= d;
endmodule

module pipe(input logic clk, en, input logic [7:0] a, b, output logic [7:0] y);
  logic [7:0] s, q, m;
  assign s = a ^ b;                       // combinational
  leaf u_reg (.clk(clk), .en(en), .d(s), .q(q));
  assign m = q | b;                       // combinational, past the flop
  assign y = m;
endmodule
"#;

fn pipe(test: &str) -> Option<Exported> {
    exported("pipe", PIPE, test)
}

fn cone(fx: &Exported, cmd: &str, args: &[&str]) -> Value {
    let mut argv = vec![cmd, "--json", fx.db.to_str().unwrap()];
    argv.extend(args);
    let (v, code) = json_of(&argv);
    assert_eq!(code, 0, "{cmd} failed: {v}");
    v
}

/// Every edge, as the pair of paths it joins.
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

fn nodes(v: &Value) -> Vec<String> {
    v["data"]["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n["path"].as_str().unwrap().to_string())
        .collect()
}

#[test]
fn a_cone_reaches_across_hierarchy_and_through_the_flop() {
    let Some(fx) = pipe("a_cone_reaches_across_hierarchy") else { return };
    let v = cone(&fx, "fanin", &["pipe.y", "--depth", "0", "--limit", "0"]);

    let reached = nodes(&v);
    for expected in ["pipe.m", "pipe.q", "pipe.u_reg.q", "pipe.u_reg.d", "pipe.s", "pipe.a"] {
        assert!(reached.iter().any(|n| n == expected), "{expected} missing from {reached:?}");
    }
    // The flop is inside a child module, so reaching `a` means the walk went
    // down through a port, across the flop, and back up.
    let edges = arcs(&v);
    assert!(edges.contains(&("pipe.u_reg.q".into(), "pipe.q".into())));
    assert!(edges.contains(&("pipe.s".into(), "pipe.u_reg.d".into())));
}

#[test]
fn fan_in_and_fan_out_are_the_same_relation_read_the_other_way() {
    let Some(fx) = pipe("fan_in_and_fan_out_are_the_same_relation") else { return };

    // Duality: if X's fan-in holds the arc Y->X, then Y's fan-out holds it too.
    // The two directions read one relation, and a disagreement is a defect in
    // whichever of them is wrong — which is the point of asking both.
    let into_y = cone(&fx, "fanin", &["pipe.y", "--depth", "0", "--limit", "0"]);
    for (src, tgt) in arcs(&into_y) {
        let out_of_src =
            cone(&fx, "fanout", &[&src, "--depth", "0", "--limit", "0"]);
        assert!(
            arcs(&out_of_src).contains(&(src.clone(), tgt.clone())),
            "fan-in has {src} -> {tgt}; fan-out of {src} does not"
        );
    }
}

#[test]
fn the_first_hop_of_a_cone_is_what_trace_answers() {
    let Some(fx) = pipe("the_first_hop_of_a_cone_is_what_trace_answers") else { return };

    // A cone one hop deep and a trace are the same question. They render
    // differently — a trace groups by statement, a cone by arc — so what must
    // agree is which nets are named.
    let hop = cone(&fx, "fanin", &["pipe.m", "--depth", "1", "--limit", "0"]);
    let mut from_cone: Vec<String> = arcs(&hop).into_iter().map(|(s, _)| s).collect();
    from_cone.sort();
    from_cone.dedup();

    let (traced, _) = json_of(&["trace", "--json", fx.db.to_str().unwrap(), "pipe.m"]);
    let mut from_trace: Vec<String> = traced["data"]["hops"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|h| h["signals"].as_array().unwrap())
        .map(|s| s.as_str().unwrap().to_string())
        .collect();
    from_trace.sort();
    from_trace.dedup();

    assert_eq!(from_cone, from_trace, "one hop of a cone is a trace");
}

#[test]
fn a_deeper_cone_contains_a_shallower_one() {
    let Some(fx) = pipe("a_deeper_cone_contains_a_shallower_one") else { return };

    // Monotonicity. A walk that dropped something on its way further out would
    // be reporting a route it had already found not to exist.
    let mut previous: Vec<(String, String)> = Vec::new();
    for depth in ["1", "2", "3", "4", "0"] {
        let v = cone(&fx, "fanin", &["pipe.y", "--depth", depth, "--limit", "0"]);
        let here = arcs(&v);
        for edge in &previous {
            assert!(here.contains(edge), "depth {depth} lost {edge:?}");
        }
        previous = here;
    }
}

#[test]
fn a_combinational_cone_is_part_of_the_whole_one_and_stops_at_the_flop() {
    let Some(fx) = pipe("a_combinational_cone_is_part_of_the_whole_one") else { return };

    let all = cone(&fx, "fanin", &["pipe.y", "--depth", "0", "--limit", "0"]);
    let comb = cone(&fx, "fanin", &["pipe.y", "--comb", "--limit", "0"]);

    for edge in arcs(&comb) {
        assert!(arcs(&all).contains(&edge), "the combinational cone invented {edge:?}");
    }
    // The flop ends this cycle. Where it stopped is part of the answer — a
    // cone that fell silent could not be told from one that found nothing — so
    // the boundary is named and marked, and nothing beyond it is reached.
    let reached = nodes(&comb);
    assert!(reached.iter().any(|n| n == "pipe.m"), "{reached:?}");
    assert!(reached.iter().any(|n| n == "pipe.q"), "the boundary is named: {reached:?}");
    for past in ["pipe.u_reg.d", "pipe.s", "pipe.a"] {
        assert!(!reached.iter().any(|n| n == past), "{past} is past the flop: {reached:?}");
    }
    let stops: Vec<&serde_json::Value> = comb["data"]["edges"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["ends_at_state"] == true)
        .collect();
    assert_eq!(stops.len(), 1, "one boundary, said once");
    // Walking backwards, the state element is the far end of the arc — its
    // source. The mark is on the arc that reaches it either way.
    assert_eq!(stops[0]["source"], "pipe.q");
    assert_eq!(comb["summary"]["stopped_at_state"], 1);
    // The walk itself crossed nothing clocked: the boundary arc is a wire.
    assert!(comb["data"]["edges"].as_array().unwrap().iter().all(|e| e["clocked"] == false));
}

#[test]
fn a_cone_starting_at_a_flop_walks_its_own_input_side() {
    let Some(fx) = pipe("a_cone_starting_at_a_flop_walks_its_own_input") else { return };
    let v = cone(&fx, "fanin", &["pipe.u_reg.q", "--comb", "--limit", "0"]);

    // The start is never past its own boundary: asking what feeds a flop is
    // the ordinary reason to ask, and answering "nothing, it is a flop" would
    // make the combinational cone useless at exactly the place it is wanted.
    let reached = nodes(&v);
    assert!(reached.iter().any(|n| n == "pipe.u_reg.d"), "{reached:?}");
    assert!(reached.iter().any(|n| n == "pipe.s"), "the walk continues past the port: {reached:?}");
}

#[test]
fn clipping_a_cone_keeps_the_counts_true_and_the_graph_whole() {
    let Some(fx) = pipe("clipping_a_cone_keeps_the_counts_true") else { return };

    let whole = cone(&fx, "fanin", &["pipe.y", "--depth", "0", "--limit", "0"]);
    let total = whole["summary"]["edges"].as_u64().unwrap();
    assert!(total > 2, "the fixture must have more edges than the clip");

    let clipped = cone(&fx, "fanin", &["pipe.y", "--depth", "0", "--limit", "2"]);
    assert_eq!(clipped["summary"]["edges"], total, "the count is of the cone, not of the window");
    assert_eq!(clipped["summary"]["shown_edges"], 2);
    assert_eq!(clipped["summary"]["truncated"], true);

    // Every edge shown names nodes that are shown: a caller reading `nodes` as
    // the things `edges` refers to would otherwise be reading a lie. It holds
    // of the whole answer as much as of a clipped one — a cone that stops at a
    // state element still names where it stopped.
    for answer in [&whole, &clipped] {
        let shown = nodes(answer);
        for (src, tgt) in arcs(answer) {
            assert!(shown.contains(&src), "{src} is an edge endpoint and not a node");
            assert!(shown.contains(&tgt), "{tgt} is an edge endpoint and not a node");
        }
    }
}

#[test]
fn a_walk_over_bidirectional_structure_ends() {
    const LOOPS: &str = r#"
module loops(input logic en, inout wire p, q, output logic o);
  logic a, b;
  assign a = b & en;        // a combinational loop
  assign b = a | en;
  wire t0, t1, t2;
  alias t0 = t1 = t2;       // every pair bound to every other
  tranif1 sw (p, q, en);    // conducts both ways
  assign o = a ^ t0 ^ p;
endmodule
"#;
    let Some(fx) = exported("loops", LOOPS, "a_walk_over_bidirectional_structure_ends") else {
        return;
    };

    // None of these can be walked by following arcs until they run out: each
    // has a route back to where it started. Termination comes from the visited
    // set, not from the direction of travel.
    for signal in ["loops.a", "loops.t0", "loops.p", "loops.o"] {
        for dir in ["fanin", "fanout"] {
            // Completing is the assertion. `o` is a top-level output that
            // nothing inside reads, so its fan-out is legitimately empty —
            // what would not be legitimate is not coming back.
            cone(&fx, dir, &[signal, "--depth", "0", "--limit", "0"]);
        }
    }

    // The loop is reported as one: the arc back to the start is an answer, not
    // a reason to keep going.
    let v = cone(&fx, "fanin", &["loops.a", "--depth", "0", "--limit", "0"]);
    let edges = arcs(&v);
    assert!(edges.contains(&("loops.b".into(), "loops.a".into())));
    assert!(edges.contains(&("loops.a".into(), "loops.b".into())), "the way back is in it too");
}

#[test]
fn a_route_is_a_walk_read_backwards_and_not_finding_one_is_an_answer() {
    let Some(fx) = pipe("a_route_is_a_walk_read_backwards") else { return };

    let (v, code) = json_of(&["path", "--json", fx.db.to_str().unwrap(), "pipe.a", "pipe.y"]);
    assert_eq!(code, 0);
    assert_eq!(v["data"]["found"], true);

    // `edges[i]` joins `nodes[i]` to `nodes[i+1]`: the two lists are read
    // together or neither is worth reading.
    let route_nodes = v["data"]["nodes"].as_array().unwrap();
    let route_edges = v["data"]["edges"].as_array().unwrap();
    assert_eq!(route_nodes.len(), route_edges.len() + 1);
    for (i, edge) in route_edges.iter().enumerate() {
        assert_eq!(edge["source"], route_nodes[i]);
        assert_eq!(edge["target"], route_nodes[i + 1]);
    }
    assert_eq!(route_nodes[0], "pipe.a");
    assert_eq!(route_nodes[route_nodes.len() - 1], "pipe.y");
    assert_eq!(v["summary"]["clocked_edges"], 1, "the route crosses the flop once");

    // The same question with the flop excluded has no answer, and that is an
    // ordinary result rather than a failure: two nets with no combinational
    // route between them is a fact about the design.
    let (blocked, code) =
        json_of(&["path", "--json", fx.db.to_str().unwrap(), "pipe.a", "pipe.y", "--comb"]);
    assert_eq!(code, 0, "not finding a route is not an error");
    assert_eq!(blocked["status"], "ok");
    assert_eq!(blocked["data"]["found"], false);
    assert_eq!(blocked["data"]["nodes"].as_array().unwrap().len(), 0);
}

#[test]
fn conditions_are_dependencies_and_are_told_apart_from_values() {
    let Some(fx) = pipe("conditions_are_dependencies") else { return };

    let all = cone(&fx, "fanin", &["pipe.u_reg.q", "--depth", "1", "--limit", "0"]);
    let without = cone(&fx, "fanin", &["pipe.u_reg.q", "--depth", "1", "--no-control", "--limit", "0"]);

    // `en` gates the assignment. It is a real dependency — the value would not
    // be there without it — and a different kind from `d`, which supplies it.
    let sources: Vec<String> = arcs(&all).into_iter().map(|(s, _)| s).collect();
    assert!(sources.iter().any(|s| s == "pipe.u_reg.en"), "{sources:?}");
    assert!(sources.iter().any(|s| s == "pipe.u_reg.d"), "{sources:?}");

    let plain: Vec<String> = arcs(&without).into_iter().map(|(s, _)| s).collect();
    assert!(!plain.iter().any(|s| s == "pipe.u_reg.en"), "{plain:?}");
    assert!(plain.iter().any(|s| s == "pipe.u_reg.d"), "{plain:?}");

    // How many of them there are is worth saying: a cone is mostly conditions
    // on real RTL, and a caller deciding whether to ask for them needs to know.
    assert!(all["summary"]["control_edges"].as_u64().unwrap() > 0);
    assert_eq!(without["summary"]["control_edges"], 0);
}

#[test]
fn a_bit_select_narrows_a_cone_to_what_feeds_those_bits() {
    const SLICES: &str = r#"
module slices(input logic [7:0] hi, lo, output logic [7:0] y);
  assign y[7:4] = hi[3:0];
  assign y[3:0] = lo[7:4];
endmodule
"#;
    let Some(fx) = exported("slices", SLICES, "a_bit_select_narrows_a_cone") else { return };

    let upper = cone(&fx, "fanin", &["slices.y[7:4]", "--depth", "0", "--limit", "0"]);
    let reached = nodes(&upper);
    assert!(reached.iter().any(|n| n == "slices.hi"), "{reached:?}");
    assert!(!reached.iter().any(|n| n == "slices.lo"), "the other half feeds other bits: {reached:?}");

    let whole = cone(&fx, "fanin", &["slices.y", "--depth", "0", "--limit", "0"]);
    // A window can only narrow: the bits asked about are some of the bits.
    for edge in arcs(&upper) {
        assert!(arcs(&whole).contains(&edge), "the narrowed cone invented {edge:?}");
    }
    assert!(nodes(&whole).iter().any(|n| n == "slices.lo"));
}

#[test]
fn a_latch_ends_a_combinational_cone_unless_crossing_it_is_the_question() {
    const LATCH: &str = r#"
module lat(input logic en, input logic [7:0] a, output logic [7:0] o);
  logic [7:0] held;
  always_latch if (en) held = a;
  assign o = held ^ 8'h0F;
endmodule
"#;
    let Some(fx) = exported("lat", LATCH, "a_latch_ends_a_combinational_cone") else { return };

    // A latch holds state, so by default a combinational walk stops there
    // rather than crossing silently into another timing context. It says so:
    // the latch is named as the boundary, and what feeds it is not reached.
    let stopped = cone(&fx, "fanin", &["lat.o", "--comb", "--limit", "0"]);
    let reached = nodes(&stopped);
    assert!(reached.iter().any(|n| n == "lat.held"), "the boundary is named: {reached:?}");
    assert!(!reached.iter().any(|n| n == "lat.a"), "past the latch: {reached:?}");
    assert_eq!(stopped["summary"]["stopped_at_state"], 1);

    // It is also transparent while its enable holds, which is the whole
    // subject of a glitch, a loop closing through one, or a pulse-latch
    // borrow. Asking for that view is a different question, and has a flag.
    let crossed = cone(&fx, "fanin", &["lat.o", "--comb", "--through-latch", "--limit", "0"]);
    let reached = nodes(&crossed);
    assert!(reached.iter().any(|n| n == "lat.a"), "the far side of the latch: {reached:?}");
    assert_eq!(crossed["summary"]["stopped_at_state"], 0, "nothing stopped it");
}

#[test]
fn asking_about_every_bit_is_asking_about_the_whole_object() {
    const WIDE: &str = r#"
module wide(input logic [3:0] p, q, output logic [7:0] y);
  logic [7:0] s, t;
  assign s[7:4] = p;
  assign s[3:0] = q;
  assign t = s;
  assign y[7:4] = t[7:4];
  assign y[3:0] = t[3:0];
endmodule
"#;
    let Some(fx) = exported("wide", WIDE, "asking_about_every_bit") else { return };

    // A window covering the whole object asks the same question as no window
    // at all, and so does one spanning the halves two statements write. The
    // cheapest invariant there is, and the one that catches a walk which
    // reports an arc without following it.
    let whole = arcs(&cone(&fx, "fanin", &["wide.y", "--depth", "0", "--limit", "0"]));
    for spelled in ["wide.y[7:0]", "wide.y[5:2]"] {
        let narrowed = arcs(&cone(&fx, "fanin", &[spelled, "--depth", "0", "--limit", "0"]));
        for edge in &whole {
            assert!(narrowed.contains(edge), "{spelled} lost {edge:?}");
        }
    }
}

#[test]
fn one_call_of_a_subroutine_is_not_another() {
    const CALLS: &str = r#"
module calls(input logic clk, input logic [7:0] d1, d2, output logic [7:0] q);
  function automatic [7:0] inc(input [7:0] v); return v + 8'd1; endfunction
  task automatic put(input [7:0] dd); q <= inc(dd); endtask
  always_ff @(posedge clk) begin
    put(d1);
    put(d2);
  end
endmodule
"#;
    let Some(fx) = exported("calls", CALLS, "one_call_of_a_subroutine_is_not_another") else {
        return;
    };

    // A body is walked once per call and its formals are shared, so following
    // every row that touches one builds a path out of one call's argument and
    // another's — a combination no execution makes.
    let from_d1 = cone(&fx, "fanout", &["calls.d1", "--depth", "0", "--limit", "0"]);
    let reached: Vec<String> = arcs(&from_d1).into_iter().map(|(_, t)| t).collect();
    assert!(!reached.iter().any(|n| n.contains("d2")), "{reached:?}");

    // The nested call is reachable: `put` calls `inc`, and a walk that only
    // admitted the same expansion would stop at the outer body's formal.
    assert!(reached.iter().any(|n| n.ends_with("inc.v")), "the nested call: {reached:?}");
    assert!(reached.iter().any(|n| n.ends_with("inc.inc")), "past it: {reached:?}");

    // Read the other way, each chain of expansions ends at its own argument.
    let into_result = cone(&fx, "fanin", &["calls.inc.inc", "--depth", "0", "--limit", "0"]);
    let sites: Vec<(Option<u64>, String)> = into_result["data"]["edges"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| (e["call_site"].as_u64(), e["source"].as_str().unwrap().to_string()))
        .collect();
    for arg in ["calls.d1", "calls.d2"] {
        assert!(sites.iter().any(|(_, s)| s == arg), "{arg} is reachable: {sites:?}");
    }
    // Each expansion's rows carry its own tag, which is what kept them apart.
    let tags: Vec<Option<u64>> = sites.iter().map(|(cs, _)| *cs).collect();
    assert!(tags.iter().filter(|t| t.is_some()).count() >= 2, "{tags:?}");
}

#[test]
fn a_temporary_inside_a_clocked_procedure_is_not_storage() {
    const TEMP: &str = r#"
module temp(input logic clk, input logic [7:0] a, b, output logic [7:0] q);
  logic [7:0] t;
  always_ff @(posedge clk) begin
    t = a + b;          // blocking: computed within this evaluation
    q <= t ^ 8'h0F;     // non-blocking: what the flop stores
  end
endmodule
"#;
    let Some(fx) = exported("temp", TEMP, "a_temporary_inside_a_clocked_procedure") else {
        return;
    };

    // Only what a clocked procedure assigns non-blockingly is storage. Reading
    // the temporary as a flop output would end the combinational cone at a
    // value that never held one — and `--comb` would answer nothing at all.
    let comb = cone(&fx, "fanin", &["temp.q", "--comb", "--limit", "0"]);
    let reached = nodes(&comb);
    for expected in ["temp.t", "temp.a", "temp.b"] {
        assert!(reached.iter().any(|n| n == expected), "{expected} missing: {reached:?}");
    }
}

#[test]
fn a_port_wired_to_part_of_a_net_says_nothing_about_the_rest() {
    const PART: &str = r#"
module flop(input logic clk, output logic [3:0] o);
  always_ff @(posedge clk) o <= 4'h5;
endmodule

module part(input logic clk, input logic [3:0] a, output logic [7:0] y);
  logic [7:0] bus;
  flop u (.clk(clk), .o(bus[3:0]));   // a flop drives the low half
  assign bus[7:4] = a;                // the high half is combinational
  assign y = bus;
endmodule
"#;
    let Some(fx) = exported("part", PART, "a_port_wired_to_part_of_a_net") else { return };

    // A crossing carries "is a state element" only where it carries the whole
    // object. Propagating it across a four-bit tie would call `bus` a flop
    // output and end every combinational cone through it — including the half
    // that is plain logic.
    let comb = cone(&fx, "fanin", &["part.y", "--comb", "--limit", "0"]);
    let reached = nodes(&comb);
    assert!(reached.iter().any(|n| n == "part.a"), "the combinational half: {reached:?}");
    assert_eq!(comb["summary"]["stopped_at_state"], 1, "and only the other half stops");
}

#[test]
fn a_variable_a_subroutine_writes_is_still_the_module_s() {
    const SHARED: &str = r#"
module shared(input logic [7:0] a, output logic [7:0] o);
  logic [7:0] m;
  task automatic w(input logic [7:0] x); m = x; endtask
  task automatic r(output logic [7:0] y); y = m; endtask
  always_comb begin w(a); r(o); end
endmodule
"#;
    let Some(fx) = exported("shared", SHARED, "a_variable_a_subroutine_writes") else { return };

    // `m` is the module's, written by one call and read by another. Treating
    // it as a formal — which every row touching it carrying a call tag makes
    // it look like — would leave each call refusing the other's rows, and the
    // route through it reported as absent.
    let (v, code) = json_of(&["path", "--json", fx.db.to_str().unwrap(), "shared.a", "shared.o"]);
    assert_eq!(code, 0);
    assert_eq!(v["data"]["found"], true, "there is plainly a route: {v}");
    let route: Vec<&str> =
        v["data"]["nodes"].as_array().unwrap().iter().map(|n| n.as_str().unwrap()).collect();
    assert!(route.iter().any(|n| *n == "shared.m"), "through the shared variable: {route:?}");
}

#[test]
fn an_edge_says_which_bits_of_each_end_it_touches() {
    const SLICE: &str = r#"
module slice(input logic [7:0] wide, output logic [3:0] narrow);
  assign narrow = wide[7:4];
endmodule
"#;
    let Some(fx) = exported("slice", SLICE, "an_edge_says_which_bits_of_each_end") else { return };
    let v = cone(&fx, "fanin", &["slice.narrow", "--depth", "1", "--limit", "0"]);
    let edge = &v["data"]["edges"][0];

    // Each end is spelled against its own declared range: the two are
    // different objects and rarely declared alike. The database has both
    // windows, and an answer that dropped them would be hiding what it knows.
    assert_eq!(edge["source_bits"], "[7:4]", "{edge}");
    assert_eq!(edge["target_bits"], serde_json::Value::Null, "the whole of a four-bit net");
}

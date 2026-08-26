//! `tree` and `find` — the two commands a caller reaches for when they do not
//! yet have a name, and the statement ordering that says which write held.

mod common;

use common::{Exported, exported, json_of};
use serde_json::Value;

const DESIGN: &str = r#"
module alu(input logic [7:0] a, b, output logic [7:0] r);
  assign r = a + b;
endmodule

module core(input logic clk, input logic [7:0] a, b, output logic [7:0] out);
  logic [7:0] partial;
  alu u_alu (.a(a), .b(b), .r(partial));
  always_ff @(posedge clk) out <= partial;
endmodule

module chip(input logic clk, input logic [1:0] sel, input logic [7:0] a, b,
              output logic [7:0] out, output logic [7:0] muxed);
  core u_core (.clk(clk), .a(a), .b(b), .out(out));
  for (genvar i = 0; i < 2; i++) begin : lane
    logic [7:0] copy;
    assign copy = a;
  end
  always_comb begin
    muxed = 8'h00;              // a default the arms below overwrite
    case (sel)
      2'b01: muxed = a;
      2'b10: muxed = b;
    endcase
  end
endmodule
"#;

fn chip(test: &str) -> Option<Exported> {
    exported("chip", DESIGN, test)
}

fn run(fx: &Exported, cmd: &str, args: &[&str]) -> Value {
    let mut argv = vec![cmd, "--json", fx.db.to_str().unwrap()];
    argv.extend(args);
    let (v, code) = json_of(&argv);
    assert_eq!(code, 0, "{cmd} failed: {v}");
    v
}

#[test]
fn a_tree_says_what_the_design_is_made_of() {
    let Some(fx) = chip("a_tree_says_what_the_design_is_made_of") else { return };
    let v = run(&fx, "tree", &["--depth", "0", "--limit", "0"]);

    let levels: Vec<(&str, &str, u64)> = v["data"]["levels"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| {
            (l["path"].as_str().unwrap(), l["kind"].as_str().unwrap(), l["depth"].as_u64().unwrap())
        })
        .collect();

    // A child comes after its parent and deeper than it: the answer is read as
    // a tree, not as a list that happens to contain one.
    let at = |p: &str| levels.iter().position(|(path, ..)| *path == p).expect(p);
    assert!(at("chip.u_core") < at("chip.u_core.u_alu"));
    assert!(levels[at("chip.u_core.u_alu")].2 > levels[at("chip.u_core")].2);

    // A generate block is a level of the path and not an instance, and the
    // kinds say which is which.
    assert_eq!(levels[at("chip.lane[0]")].1, "generate");
    assert_eq!(levels[at("chip.u_core")].1, "instance");

    // A definition is named where there is one, and a net count says whether a
    // level is worth descending into.
    let alu = &v["data"]["levels"][at("chip.u_core.u_alu")];
    assert_eq!(alu["module"], "alu");
    assert!(alu["nets"].as_u64().unwrap() >= 3);
}

#[test]
fn a_tree_can_start_below_the_top_and_stop_before_the_bottom() {
    let Some(fx) = chip("a_tree_can_start_below_the_top") else { return };

    let under = run(&fx, "tree", &["chip.u_core", "--depth", "0", "--limit", "0"]);
    let paths: Vec<&str> = under["data"]["levels"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l["path"].as_str().unwrap())
        .collect();
    assert_eq!(paths[0], "chip.u_core");
    assert!(paths.contains(&"chip.u_core.u_alu"));
    assert!(!paths.iter().any(|p| p.starts_with("chip.lane")), "{paths:?}");

    // One level down is the top and its children, and nothing below them.
    let shallow = run(&fx, "tree", &["--depth", "1", "--limit", "0"]);
    let deepest = shallow["data"]["levels"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l["depth"].as_u64().unwrap())
        .max()
        .unwrap();
    assert_eq!(deepest, 1);
}

#[test]
fn a_scope_that_is_not_there_comes_back_with_the_level_that_is() {
    let Some(fx) = chip("a_scope_that_is_not_there") else { return };
    let (v, code) = json_of(&["tree", "--json", fx.db.to_str().unwrap(), "chip.u_corex"]);

    assert_eq!(code, 1);
    assert_eq!(v["errors"][0]["code"], "SCOPE_NOT_FOUND");
    let details = &v["errors"][0]["details"];
    assert_eq!(details["failing_segment"], "u_corex");
    assert_eq!(details["valid_prefix"], "chip");
    assert_eq!(details["close_matches"][0], "u_core");
}

#[test]
fn find_turns_a_name_into_the_paths_that_carry_it() {
    let Some(fx) = chip("find_turns_a_name_into_the_paths") else { return };

    // The glob matches the name relative to an instance, which is what a
    // caller knows when all they have is a signal name from a waveform.
    let v = run(&fx, "find", &["partial", "--limit", "0"]);
    let hits: Vec<&str> =
        v["data"]["hits"].as_array().unwrap().iter().map(|h| h["path"].as_str().unwrap()).collect();
    assert_eq!(hits, ["chip.u_core.partial"]);
    assert!(v["data"]["hits"][0]["detail"].as_str().unwrap().contains("8 bit"));

    // A wildcard finds the family, and the answer is a path every other
    // command accepts.
    let v = run(&fx, "find", &["mux*", "--limit", "0"]);
    let hits: Vec<&str> =
        v["data"]["hits"].as_array().unwrap().iter().map(|h| h["path"].as_str().unwrap()).collect();
    assert_eq!(hits, ["chip.muxed"]);
    let (traced, code) = json_of(&["trace", "--json", fx.db.to_str().unwrap(), hits[0]]);
    assert_eq!(code, 0, "what find returns, trace accepts: {traced}");
}

#[test]
fn find_can_look_for_instances_and_definitions_instead() {
    let Some(fx) = chip("find_can_look_for_instances_and_definitions") else { return };

    let v = run(&fx, "find", &["u_*", "--instances", "--limit", "0"]);
    let hits: Vec<(&str, &str)> = v["data"]["hits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| (h["path"].as_str().unwrap(), h["what"].as_str().unwrap()))
        .collect();
    assert!(hits.contains(&("chip.u_core", "instance")));
    assert!(hits.contains(&("chip.u_core.u_alu", "instance")));

    // A definition is not an occurrence: `alu` is one module elaborated once,
    // and saying how often is what makes the two tellable apart.
    let v = run(&fx, "find", &["alu", "--modules", "--limit", "0"]);
    assert_eq!(v["data"]["hits"][0]["path"], "alu");
    assert_eq!(v["data"]["hits"][0]["what"], "module");
    assert!(v["data"]["hits"][0]["detail"].as_str().unwrap().contains("1 occurrence"));

    // Asking for both at once asks for two different things.
    let (v, code) =
        json_of(&["find", "--json", fx.db.to_str().unwrap(), "*", "--instances", "--modules"]);
    assert_eq!(code, 1);
    assert_eq!(v["errors"][0]["code"], "BAD_SELECT");
}

#[test]
fn a_procedure_that_writes_a_signal_more_than_once_says_in_what_order() {
    let Some(fx) = chip("a_procedure_that_writes_a_signal_more_than_once") else { return };
    let (v, _) = json_of(&["trace", "--json", fx.db.to_str().unwrap(), "chip.muxed"]);

    let hops = v["data"]["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 3, "a default and two arms");

    // Which assignment held is a fact about the procedure, not about the one
    // statement being looked at, so it is stated once. Repeating it on every
    // hop would square a wide case: three hundred arms, three hundred lists.
    let procedures = v["data"]["procedures"].as_array().unwrap();
    assert_eq!(procedures.len(), 1);
    assert_eq!(procedures[0]["proc_kind"], "always_comb");
    let writes = procedures[0]["writes"].as_array().unwrap();
    assert_eq!(writes.len(), 3, "{writes:?}");

    // In execution order.
    let order: Vec<i64> = writes.iter().map(|w| w["sequence"].as_i64().unwrap()).collect();
    assert!(order.windows(2).all(|w| w[0] < w[1]), "{order:?}");

    // The default is the one nothing gates. That it comes first and is ungated
    // is what says the arms overwrite it — the reader draws that conclusion;
    // the answer supplies the order, the conditions and the bits.
    assert_eq!(writes[0]["unconditional"], true);
    assert_eq!(writes[1]["unconditional"], false);
    assert_eq!(writes[2]["unconditional"], false);

    // A hop names its procedure, and its own place in it by `sequence`.
    for hop in hops {
        assert_eq!(hop["procedure"], 0);
        let mine = hop["sequence"].as_i64().unwrap();
        assert!(order.contains(&mine), "{mine} is one of {order:?}");
    }
}

#[test]
fn a_single_write_needs_no_ordering() {
    let Some(fx) = chip("a_single_write_needs_no_ordering") else { return };
    let (v, _) = json_of(&["trace", "--json", fx.db.to_str().unwrap(), "chip.u_core.out"]);

    // One assignment is the statement already reported; listing it again as
    // "the order it ran in" would say nothing.
    assert_eq!(v["data"]["procedures"].as_array().unwrap().len(), 0);
    assert_eq!(v["data"]["hops"][0]["kind"], "procedural");
    assert!(v["data"]["hops"][0]["procedure"].is_null());
}

#[test]
fn a_cold_start_reaches_a_driver_without_knowing_a_name_first() {
    let Some(fx) = chip("a_cold_start_reaches_a_driver") else { return };
    let db = fx.db.to_str().unwrap();

    // The path a caller with nothing takes: what is this design, what is that
    // signal called, what drives it. Each answer is the next question's input,
    // which is the property worth holding onto.
    let tree = run(&fx, "tree", &["--depth", "0", "--limit", "0"]);
    // A level with nets of its own is one worth looking inside, which is what
    // the count is for.
    let scope = tree["data"]["levels"]
        .as_array()
        .unwrap()
        .iter()
        .find(|l| l["module"] == "core")
        .and_then(|l| l["path"].as_str())
        .expect("the tree names the levels and what each one is")
        .to_string();

    let found = run(&fx, "find", &["partial", "--limit", "0"]);
    let signal = found["data"]["hits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| h["path"].as_str().unwrap())
        .find(|p| p.starts_with(&scope))
        .expect("a signal inside the scope the tree named");

    let (traced, code) = json_of(&["trace", "--json", db, signal]);
    assert_eq!(code, 0, "{traced}");
    // The value arrives through a port, so the answer is where to look next
    // rather than a statement — and it names it, which is what makes the
    // following step a step rather than a search.
    assert_eq!(traced["data"]["status"], "boundary_only");
    let feeds = traced["data"]["hops"][0]["signals"][0].as_str().unwrap();

    let (across, code) = json_of(&["trace", "--json", db, feeds]);
    assert_eq!(code, 0, "the name a boundary gives is one trace accepts: {across}");
    assert_eq!(across["data"]["status"], "resolved");
    assert!(across["data"]["hops"][0]["statement"].as_str().unwrap().contains("a + b"));

    let (cone, code) = json_of(&["fanin", "--json", db, feeds, "--depth", "0", "--limit", "0"]);
    assert_eq!(code, 0, "what a trace names, a cone accepts: {cone}");
    assert!(cone["summary"]["edges"].as_u64().unwrap() > 0);
}

#[test]
fn two_writes_to_disjoint_windows_do_not_overwrite_one_another() {
    const SLICED: &str = r#"
module sliced(input logic [7:0] a, b, output logic [7:0] y);
  always_comb begin
    y[7:4] = a[3:0];
    y[3:0] = b[7:4];
  end
endmodule
"#;
    let Some(fx) = exported("sliced", SLICED, "two_writes_to_disjoint_windows") else { return };
    let (v, _) = json_of(&["trace", "--json", fx.db.to_str().unwrap(), "sliced.y"]);

    let writes = v["data"]["procedures"][0]["writes"].as_array().unwrap();
    assert_eq!(writes.len(), 2);

    // Both are ungated, and a reader told only that would conclude the second
    // overwrites the first. The bits are what says otherwise: they never meet.
    assert!(writes.iter().all(|w| w["unconditional"] == true));
    let windows: Vec<&str> = writes.iter().map(|w| w["bits"].as_str().unwrap()).collect();
    assert_eq!(windows, ["[7:4]", "[3:0]"]);
}

#[test]
fn a_write_that_leaves_the_instance_is_still_one_of_the_procedure_s() {
    const OUTWARD: &str = r#"
module sink(input logic clk, output logic n);
  always_ff @(posedge clk) n <= 1'b0;
endmodule

module outward(input logic clk, s, a, b, output logic o);
  sink u_sink (.clk(clk), .n());
  always_comb begin
    u_sink.n = a;             // written by name, from outside the instance
    if (s) u_sink.n = b;
  end
  assign o = u_sink.n;
endmodule
"#;
    let Some(fx) = exported("outward", OUTWARD, "a_write_that_leaves_the_instance") else {
        return;
    };
    let (v, _) = json_of(&["trace", "--json", fx.db.to_str().unwrap(), "outward.u_sink.n"]);

    // Such a write is a `hier_ref`, not a statement target, and asking only for
    // targets reported a procedure that writes the signal twice as writing it
    // never — while the hops beside it said otherwise.
    let procedures = v["data"]["procedures"].as_array().unwrap();
    assert_eq!(procedures.len(), 1, "the always_comb writes it twice: {procedures:?}");
    assert_eq!(procedures[0]["writes"].as_array().unwrap().len(), 2);
}

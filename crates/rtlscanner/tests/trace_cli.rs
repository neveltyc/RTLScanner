//! `trace` against RTL exported by the pinned producer.
//!
//! The fixtures are SystemVerilog rather than seeded rows: what is asserted is
//! what the current export makes of real constructs, so a producer that changes
//! its mind about one shows up here rather than in a hand-written row that
//! agrees with an older opinion.

mod common;

use common::{Exported, exported, json_trace};

/// Shapes one trace has to tell apart.
const BASIC: &str = r#"
module leaf(input logic clk, input logic en, input logic [7:0] d, output logic [7:0] q);
  always_ff @(posedge clk) begin
    if (en) q <= d;
    else    q <= 8'h00;
  end
endmodule

module basic(input logic clk, input logic [7:0] a, b, output logic [7:0] y);
  logic [7:0] mid;
  logic       enable;
  assign enable = |a;
  leaf u_leaf (.clk(clk), .en(enable), .d(a), .q(mid));
  assign y[7:4] = mid[3:0];
  assign y[3:0] = b[7:4];
endmodule
"#;

fn basic(test: &str) -> Option<Exported> {
    exported("basic", BASIC, test)
}

#[test]
fn a_signal_assembled_from_slices_names_each_driver_with_its_bits() {
    let Some(fx) = basic("a_signal_assembled_from_slices_names_each_driver_with_its_bits") else { return };
    let (v, code) = json_trace(&fx, &["basic.y"]);

    assert_eq!(code, 0);
    assert_eq!(v["data"]["status"], "resolved");
    assert_eq!(v["data"]["width"], 8);

    let hops = v["data"]["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 2);
    for hop in hops {
        assert_eq!(hop["kind"], "continuous_assign");
        assert_eq!(hop["assign_kind"], "continuous");
    }
    // `bits` is a list: a statement may write more than one window, and one
    // range spanning them would claim the bits in between.
    let bits: Vec<&str> =
        hops.iter().map(|h| h["bits"][0].as_str().unwrap()).collect();
    assert!(bits.contains(&"[7:4]") && bits.contains(&"[3:0]"), "{bits:?}");

    // Two drivers writing disjoint halves is a signal assembled from parts,
    // not two sources contending for one.
    assert_eq!(v["summary"]["drivers"], 2);
    assert_eq!(v["summary"]["multiple_drivers"], false);
}

#[test]
fn a_bit_select_narrows_to_the_driver_that_writes_those_bits() {
    let Some(fx) = basic("a_bit_select_narrows_to_the_driver_that_writes_those_bits") else { return };

    let (upper, _) = json_trace(&fx, &["basic.y[7:4]"]);
    let hops = upper["data"]["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 1, "only the assignment covering [7:4]");
    assert_eq!(upper["data"]["bits"], "[7:4]");
    assert_eq!(hops[0]["bits"][0], "[7:4]");
    assert!(hops[0]["signals"][0].as_str().unwrap().ends_with(".mid"));

    // The other half answers with the other driver, and a single index is a
    // select like any range.
    let (lower, _) = json_trace(&fx, &["basic.y[3]"]);
    let hops = lower["data"]["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 1);
    assert!(hops[0]["signals"][0].as_str().unwrap().ends_with(".b"));
}

#[test]
fn a_select_outside_the_declared_range_is_an_error_with_its_own_code() {
    let Some(fx) = basic("a_select_outside_the_declared_range_is_an_error_with_its_own_code") else { return };
    let (v, code) = json_trace(&fx, &["basic.y[99]"]);

    assert_eq!(code, 1);
    assert_eq!(v["errors"][0]["code"], "BAD_SELECT");
    assert!(v["errors"][0]["message"].as_str().unwrap().contains("outside the declared range"));
}

#[test]
fn the_two_arms_of_one_if_are_two_statements_and_one_driver() {
    let Some(fx) = basic("the_two_arms_of_one_if_are_two_statements_and_one_driver") else { return };
    let (v, _) = json_trace(&fx, &["basic.u_leaf.q"]);

    let hops = v["data"]["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 2, "each arm is its own statement");
    // A procedure drives as a whole: its statements run in order and cannot
    // contend, so this is one source and no conflict.
    assert_eq!(v["summary"]["drivers"], 1);
    assert_eq!(v["summary"]["multiple_drivers"], false);

    // The arms are distinguishable, which is what v19's gating tree added: in
    // v18 both carried the same condition and nothing said which side.
    let senses: Vec<&str> =
        hops.iter().map(|h| h["gates"][0]["sense"].as_str().unwrap()).collect();
    assert!(senses.contains(&"then") && senses.contains(&"else"), "{senses:?}");
    for hop in hops {
        assert_eq!(hop["gates"][0]["kind"], "if");
        assert_eq!(hop["gates"][0]["reads"][0], "en");
        assert_eq!(hop["timing"]["proc_kind"], "always_ff");
        assert_eq!(hop["timing"]["events"][0]["edge"], "posedge");
        assert_eq!(hop["timing"]["events"][0]["signal"], "clk");
    }

    // The else arm assigns a literal: there is no net driving it, and saying
    // so is different from saying nothing drives the signal.
    let kinds: Vec<&str> = hops.iter().map(|h| h["kind"].as_str().unwrap()).collect();
    assert!(kinds.contains(&"procedural") && kinds.contains(&"constant"), "{kinds:?}");
}

#[test]
fn a_statement_is_quoted_from_the_file_the_export_read() {
    let Some(fx) = basic("a_statement_is_quoted_from_the_file_the_export_read") else { return };
    let (v, _) = json_trace(&fx, &["basic.u_leaf.q"]);
    let hop = &v["data"]["hops"][0];

    assert_eq!(hop["source"], "read");
    assert!(hop["statement"].as_str().unwrap().contains("q <= "), "{hop}");
    assert!(hop["file"].as_str().unwrap().ends_with(".sv"));
    assert!(hop["line"].as_u64().unwrap() > 0);
}

#[test]
fn a_port_crossing_is_a_boundary_and_says_where_it_leads() {
    let Some(fx) = basic("a_port_crossing_is_a_boundary_and_says_where_it_leads") else { return };
    let (v, _) = json_trace(&fx, &["basic.mid"]);

    // Nothing in this module drives `mid`; it is driven through the port. That
    // is an answer about where to look next, not a failure to find one.
    assert_eq!(v["data"]["status"], "boundary_only");
    let hop = &v["data"]["hops"][0];
    assert_eq!(hop["kind"], "port");
    assert_eq!(hop["boundary"], true);
    assert_eq!(hop["signals"][0], "basic.u_leaf.q");
}

#[test]
fn loads_answer_the_other_direction_with_the_same_shape() {
    let Some(fx) = basic("loads_answer_the_other_direction_with_the_same_shape") else { return };
    let (v, code) = json_trace(&fx, &["basic.mid", "--load"]);

    assert_eq!(code, 0);
    assert_eq!(v["data"]["direction"], "load");
    let hops = v["data"]["hops"].as_array().unwrap();
    assert!(!hops.is_empty());
    assert!(
        hops.iter().any(|h| h["signals"][0].as_str().is_some_and(|s| s.ends_with(".y"))),
        "the assignment reading mid is a load of it: {hops:?}"
    );
    // Counting drivers of a load query would be answering a question nobody
    // asked; the summary says so by reporting none.
    assert_eq!(v["summary"]["drivers"], 0);
}

#[test]
fn a_name_that_does_not_resolve_comes_back_with_what_does() {
    let Some(fx) = basic("a_name_that_does_not_resolve_comes_back_with_what_does") else { return };
    let (v, code) = json_trace(&fx, &["basic.u_leaf.qq"]);

    assert_eq!(code, 1);
    assert_eq!(v["errors"][0]["code"], "SIGNAL_NOT_FOUND");
    let details = &v["errors"][0]["details"];
    assert_eq!(details["failing_segment"], "qq");
    // The correction is in the answer: the next call is a fix, not a search.
    assert_eq!(details["close_matches"][0], "q");
    assert!(details["available"].as_array().unwrap().iter().any(|n| n == "q"));
}

const GATING: &str = r#"
module gating(input logic clk, input logic [1:0] sel, input logic [7:0] a, b,
              output logic [7:0] y, output logic [7:0] q);
  localparam bit ENABLE = 1'b0;
  logic [7:0] rev;

  always_comb begin
    casez (sel)
      2'b1?:   y = a;
      2'b01:   y = b;
      default: y = 8'hFF;
    endcase
  end

  always_ff @(posedge clk) begin
    if (ENABLE) q <= a;
    else        q <= b;
  end

  always_comb for (int j = 0; j < 8; j++) rev[j] = a[7-j];
endmodule
"#;

fn gating(test: &str) -> Option<Exported> {
    exported("gating", GATING, test)
}

#[test]
fn each_case_arm_carries_its_own_labels_and_its_priority() {
    let Some(fx) = gating("each_case_arm_carries_its_own_labels_and_its_priority") else { return };
    let (v, _) = json_trace(&fx, &["gating.y"]);
    let hops = v["data"]["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 3, "three arms, three statements");

    for hop in hops {
        let gates = hop["gates"].as_array().unwrap();
        // The point carries the selector's read; the arm carries its own
        // labels. In v18 every arm saw every arm's labels and the labels
        // themselves, being constants, were filtered out entirely.
        assert_eq!(gates[0]["kind"], "case");
        assert_eq!(gates[0]["case_kind"], "casez");
        assert_eq!(gates[0]["reads"][0], "sel");
        assert!(matches!(gates[1]["kind"].as_str(), Some("case_item" | "case_default")));
    }

    let arms: Vec<(Option<&str>, Option<u64>)> = hops
        .iter()
        .map(|h| (h["gates"][1]["labels"].as_str(), h["gates"][1]["ordinal"].as_u64()))
        .collect();
    // A wildcard label is the value it elaborated to, and the ordinal is the
    // priority a plain case has — which source line cannot carry, since a
    // whole case may be written on one.
    assert!(arms.iter().any(|(l, o)| *l == Some("2'b1z") && *o == Some(0)), "{arms:?}");
    assert!(arms.iter().any(|(l, o)| *l == Some("2'b1") && *o == Some(1)), "{arms:?}");
    assert!(arms.iter().any(|(l, o)| l.is_none() && *o == Some(2)), "the default arm: {arms:?}");
}

#[test]
fn an_arm_a_constant_condition_rules_out_is_reported_and_not_counted() {
    let Some(fx) = gating("an_arm_a_constant_condition_rules_out_is_reported_and_not_counted") else { return };
    let (v, _) = json_trace(&fx, &["gating.q"]);
    let hops = v["data"]["hops"].as_array().unwrap();

    // Both arms are in the elaborated design and both are reported: a source
    // view shows the statement, and a database that had pruned it would leave
    // the reader wondering where it went. What the answer adds is the verdict.
    assert_eq!(hops.len(), 2);
    let dead: Vec<&serde_json::Value> =
        hops.iter().filter(|h| h["unreachable"] == true).collect();
    assert_eq!(dead.len(), 1, "ENABLE is 0, so the then arm cannot run");
    assert_eq!(dead[0]["gates"][0]["sense"], "then");
    assert_eq!(dead[0]["gates"][0]["static_taken"], 0);

    // It is not a driver of this parameterisation, so it is not counted as one.
    assert_eq!(v["summary"]["drivers"], 1);
}

#[test]
fn a_loop_publishes_the_iteration_space_behind_an_inexact_range() {
    let Some(fx) = gating("a_loop_publishes_the_iteration_space_behind_an_inexact_range") else { return };
    let (v, _) = json_trace(&fx, &["gating.rev"]);
    let hop = &v["data"]["hops"][0];

    // One statement covers eight assignments, so the range is an upper bound.
    // The iteration space is what separates that from a smear: it says what to
    // substitute back.
    assert_eq!(hop["bits_exact"], false);
    let loop_gate = hop["gates"]
        .as_array()
        .unwrap()
        .iter()
        .find(|g| g["kind"] == "loop")
        .expect("the loop body is a gating level");
    let space = loop_gate["iteration"].as_str().unwrap();
    assert!(space.contains("j = 0"), "{space}");
    assert!(space.contains("8 iteration"), "{space}");
}

#[test]
fn a_waveform_path_resolves_once_its_testbench_prefix_is_named() {
    let Some(fx) = basic("a_waveform_path_resolves_once_its_testbench_prefix_is_named") else { return };
    // A path from a waveform tool is anchored at a testbench the design has
    // never heard of. The design cannot recognise that scope, so the caller
    // states it.
    let (v, code) = json_trace(&fx, &["tb.u_dut.basic.y", "--strip-prefix", "tb.u_dut"]);
    assert_eq!(code, 0, "{v}");
    assert_eq!(v["data"]["hops"].as_array().unwrap().len(), 2);

    // A scope whose name merely begins with the prefix is a different scope.
    let (v, code) = json_trace(&fx, &["tb.u_dutch.y", "--strip-prefix", "tb.u_dut"]);
    assert_eq!(code, 1);
    assert!(v["errors"][0]["message"].as_str().unwrap().contains("not under"));
}

/// Constructs the first round of tests did not reach, each of which turned out
/// to hide a defect: an aggregate has no one declared range, a sensitivity row
/// names no statement, and a condition reaching outside its instance is filed
/// under where its source is rather than under what it does.
const SHAPES: &str = r#"
module inner(input logic [7:0] a, output logic [7:0] y);
  always_comb begin
    if (shapes.dbg_en) y = a;      // a condition that leaves this instance
    else               y = 8'h00;
  end
endmodule

module shapes(input logic clk, dbg_en, input logic [7:0] a, b, c, d,
              output logic [7:0] y, ext, output logic [7:0] r1, r2);
  logic [7:0] mem [0:3];           // unpacked: no one declared range
  logic [3:0][7:0] packed_arr;     // packed array: likewise

  always_ff @(posedge clk) r1 <= d;
  always_ff @(posedge clk) r2 <= d;

  assign mem[0] = a;
  assign packed_arr[0] = a;

  // Two windows in one statement, and two more from elsewhere. None overlap.
  assign {y[7:6], y[3:2]} = {a[1:0], b[1:0]};
  assign y[5:4] = c[1:0];
  assign y[1:0] = c[3:2];

  inner u_in (.a(a), .y(ext));
endmodule
"#;

fn shapes(test: &str) -> Option<Exported> {
    exported("shapes", SHAPES, test)
}

#[test]
fn an_aggregate_has_no_declared_range_and_says_so_instead_of_using_a_part() {
    let Some(fx) = shapes("an_aggregate_has_no_declared_range") else { return };

    // The first `[a:b]` of `logic [7:0] mem [0:3]` spans one element, not the
    // object. Measuring a select against it would name bits of `mem[0]` while
    // answering about `mem[2]` — an answer indistinguishable from a right one.
    for signal in ["shapes.mem[2]", "shapes.packed_arr[2]"] {
        let (v, code) = json_trace(&fx, &[signal]);
        assert_eq!(code, 1, "{signal} was answered: {v}");
        assert_eq!(v["errors"][0]["code"], "BAD_SELECT");
        let said = v["errors"][0]["message"].as_str().unwrap();
        assert!(said.contains("no single declared bit range"), "{said}");
    }

    // The whole object still answers.
    let (v, code) = json_trace(&fx, &["shapes.mem"]);
    assert_eq!(code, 0, "{v}");
    assert!(!v["data"]["hops"].as_array().unwrap().is_empty());
}

#[test]
fn every_procedure_triggering_on_a_clock_is_one_of_its_loads() {
    let Some(fx) = shapes("every_procedure_triggering_on_a_clock") else { return };
    let (v, _) = json_trace(&fx, &["shapes.clk", "--load"]);

    // A sensitivity row belongs to a procedure's header and names no
    // statement, so identifying rows by their statement alone collapses every
    // flop clocked by one net into a single answer.
    let hops = v["data"]["hops"].as_array().unwrap();
    let sens: Vec<&serde_json::Value> =
        hops.iter().filter(|h| h["kind"] == "sensitivity").collect();
    assert_eq!(sens.len(), 2, "two flops, two clock pins: {hops:?}");

    // Membership follows the netlist model: a flop's clock pin is a load of
    // its clock, so a clock whose only answers are sensitivities has loads.
    assert_eq!(v["data"]["status"], "resolved");
}

#[test]
fn a_condition_reaching_outside_the_instance_still_gates_rather_than_drives() {
    let Some(fx) = shapes("a_condition_reaching_outside_the_instance") else { return };
    let (v, _) = json_trace(&fx, &["shapes.u_in.y"]);

    let hops = v["data"]["hops"].as_array().unwrap();
    for hop in hops {
        // The export files this dependency under where its source is
        // (`external`), which says nothing about it being a condition. Listed
        // among the reads it would look like a value the statement uses.
        let reads: Vec<&str> =
            hop["signals"].as_array().unwrap().iter().map(|s| s.as_str().unwrap()).collect();
        assert!(!reads.iter().any(|s| s.contains("dbg_en")), "{reads:?}");

        // It is on the gating level it came from, named as it was written.
        let gates = hop["gates"].as_array().unwrap();
        assert_eq!(gates[0]["kind"], "if");
        let gate_reads: Vec<&str> =
            gates[0]["reads"].as_array().unwrap().iter().map(|s| s.as_str().unwrap()).collect();
        assert!(
            gate_reads.iter().any(|s| s.contains("dbg_en")),
            "a condition reaching outside is still the condition: {gate_reads:?}"
        );
    }
}

#[test]
fn disjoint_windows_of_one_statement_are_not_a_contest() {
    let Some(fx) = shapes("disjoint_windows_of_one_statement") else { return };
    let (v, _) = json_trace(&fx, &["shapes.y"]);

    let hops = v["data"]["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 3);
    let multi = hops
        .iter()
        .find(|h| h["bits"].as_array().is_some_and(|b| b.len() == 2))
        .expect("the statement writing two windows keeps both");
    let windows: Vec<&str> =
        multi["bits"].as_array().unwrap().iter().map(|b| b.as_str().unwrap()).collect();
    assert!(windows.contains(&"[7:6]") && windows.contains(&"[3:2]"), "{windows:?}");

    // Four windows, none overlapping: a signal assembled from parts. Reporting
    // one range spanning two of them would claim the bits between and make
    // this read as a conflict.
    assert_eq!(v["summary"]["multiple_drivers"], false);
}

#[test]
fn a_name_inside_a_generate_block_is_correctable() {
    const GEN: &str = r#"
module gen(input logic clk, input logic a, output logic o);
  for (genvar i = 0; i < 2; i++) begin : blk
    logic inner;
    assign inner = a;
  end
  assign o = blk[0].inner;
endmodule
"#;
    let Some(fx) = exported("gen", GEN, "a_name_inside_a_generate_block") else { return };
    let (v, code) = json_trace(&fx, &["gen.blk[0].innerX"]);

    assert_eq!(code, 1);
    // A generate level declares no instance of its own, so listing only its
    // child nodes offers nothing — and this is the level where names are most
    // often mistyped.
    let details = &v["errors"][0]["details"];
    let available: Vec<&str> =
        details["available"].as_array().unwrap().iter().map(|s| s.as_str().unwrap()).collect();
    assert!(available.contains(&"inner"), "{available:?}");
    assert_eq!(details["close_matches"][0], "inner");
}

#!/usr/bin/env python3
"""Tests for ``trace --logic``: the driver value-logic payload.

``--logic`` attaches a ``logic`` object ({timing, assignments}) to each driver
in the trace result. Without the flag the output is unchanged (no ``logic`` key).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import jsonschema
    _HAVE_JSONSCHEMA = True
except ImportError:
    _HAVE_JSONSCHEMA = False

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

# Dual-clock flop with async reset (EventListControl sensitivity list).
SEQ_DESIGN = """
module dut(input clk, input rst, input en, input [7:0] d, output reg [7:0] q);
  always @(posedge clk or posedge rst) begin
    if (rst) q <= 8'd0;
    else if (en) q <= d + 8'd1;
  end
endmodule
"""

COMB_DESIGN = """
module top(input a, input b, input sel, output o);
  assign o = sel ? a : b;
endmodule
"""

# Single-clock case block (SignalEventControl sensitivity list).
CASE_DESIGN = """
module fsm(input clk, input [1:0] s, output reg [1:0] y);
  always @(posedge clk) begin
    case (s)
      2'd0: y <= 2'd1;
      2'd1: y <= 2'd2;
      default: y <= 2'd0;
    endcase
  end
endmodule
"""

# Canonical single-clock flop; the most common sequential form. Must not be
# misread as combinational (a single-event sensitivity list is a
# SignalEventControl, not an EventListControl).
SYNC_DESIGN = """
module ff(input clk, input rst, input [7:0] d, output reg [7:0] q);
  always_ff @(posedge clk) begin
    if (rst) q <= 8'd0;
    else q <= d;
  end
endmodule
"""

# One net driven by two disjoint per-slice continuous assigns.
SPLIT_DESIGN = """
module split(input [3:0] lo, input [3:0] hi, output [7:0] bus);
  assign bus[3:0] = lo;
  assign bus[7:4] = hi;
endmodule
"""


def _run(design, args, logic=True):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "dut.sv").write_text(design)
        cmd = RTLSCANNER + ["trace", "dut.sv", *args, "--json"]
        if logic:
            cmd.append("--logic")
        proc = subprocess.run(
            cmd, cwd=d, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert proc.stdout, proc.stderr
        return json.loads(proc.stdout)


def _driver(env):
    return env["data"]["results"][0]["driver"]


class TraceLogic(unittest.TestCase):
    def test_sequential_driver_timing_and_branches(self):
        env = _run(SEQ_DESIGN, ["-s", "q", "--scope", "dut"])
        self.assertEqual(env["status"], "ok")
        logic = _driver(env)["logic"]

        timing = logic["timing"]
        self.assertEqual(timing["kind"], "sequential")
        self.assertEqual(timing["clock"], "clk")
        self.assertEqual(timing["clock_edge"], "posedge")
        self.assertEqual(timing["reset"], "rst")
        self.assertTrue(timing["reset_async"])

        assigns = logic["assignments"]
        rhs = {a["rhs_text"] for a in assigns}
        self.assertIn("8'd0", rhs)
        self.assertIn("d + 8'd1", rhs)

        # the d+1 branch has operand d and requires !rst && en
        dplus = next(a for a in assigns if "d +" in a["rhs_text"])
        self.assertEqual([o["path"] for o in dplus["rhs_operands"]], ["dut.d"])
        polarities = [(g["kind"], g["polarity"]) for g in dplus["guards"]]
        self.assertIn(("if", False), polarities)  # else of rst
        self.assertIn(("if", True), polarities)   # en true

    def test_continuous_driver_operands(self):
        env = _run(COMB_DESIGN, ["-s", "o", "--scope", "top"])
        logic = _driver(env)["logic"]
        self.assertEqual(logic["timing"]["kind"], "combinational")
        ops = {o["name"] for o in logic["assignments"][0]["rhs_operands"]}
        self.assertEqual(ops, {"sel", "a", "b"})

    def test_case_statement_guards(self):
        env = _run(CASE_DESIGN, ["-s", "y", "--scope", "fsm"])
        logic = _driver(env)["logic"]
        # single-clock `always @(posedge clk)` is still sequential
        self.assertEqual(logic["timing"]["kind"], "sequential")
        self.assertEqual(logic["timing"]["clock"], "clk")
        assigns = logic["assignments"]
        # three branches: y<=1, y<=2, default y<=0
        self.assertEqual(len(assigns), 3)
        for a in assigns:
            self.assertTrue(a["guards"])
            self.assertEqual(a["guards"][0]["kind"], "case")

    def test_single_clock_flop_is_sequential(self):
        # `always_ff @(posedge clk)` is a single-event sensitivity list;
        # its clock must still be recovered (regression guard).
        env = _run(SYNC_DESIGN, ["-s", "q", "--scope", "ff"])
        timing = _driver(env)["logic"]["timing"]
        self.assertEqual(timing["kind"], "sequential")
        self.assertEqual(timing["clock"], "clk")
        self.assertEqual(timing["clock_edge"], "posedge")
        # no reset in the sensitivity list -> synchronous reset, not reported here
        self.assertNotIn("reset", timing)

    def test_bit_select_narrows_driver_origin(self):
        # bus[1] is written only by the `bus[3:0] = lo` slice.
        env = _run(SPLIT_DESIGN, ["-s", "bus[1]", "--scope", "split"])
        rd = env["data"]["results"][0]
        self.assertEqual(rd.get("extra_drivers", []), [])
        self.assertEqual(rd["driver"]["logic"]["assignments"][0]["rhs_text"], "lo")
        # bus[5] is written only by the `bus[7:4] = hi` slice.
        env = _run(SPLIT_DESIGN, ["-s", "bus[5]", "--scope", "split"])
        rd = env["data"]["results"][0]
        self.assertEqual(rd["driver"]["logic"]["assignments"][0]["rhs_text"], "hi")

    def test_bit_select_out_of_range_errors(self):
        env = _run(SPLIT_DESIGN, ["-s", "bus[99]", "--scope", "split"])
        self.assertEqual(env["status"], "error")

    def test_without_flag_no_logic_key(self):
        # Back-compat: plain `trace` output carries no `logic` field.
        env = _run(SEQ_DESIGN, ["-s", "q", "--scope", "dut"], logic=False)
        self.assertNotIn("logic", _driver(env))

    @unittest.skipUnless(_HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_logic_output_validates_against_schema(self):
        schema = json.loads(subprocess.run(
            RTLSCANNER + ["trace", "--schema"],
            text=True, stdout=subprocess.PIPE,
        ).stdout)
        env = _run(SEQ_DESIGN, ["-s", "q", "--scope", "dut"])
        jsonschema.validate(env, schema)


if __name__ == "__main__":
    unittest.main()

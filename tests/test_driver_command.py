#!/usr/bin/env python3
"""Tests for the ``driver`` command: structured branch/operand/timing payload."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

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

# Single-clock flop (single-event sensitivity list -> SignalEventControl).
# The most common sequential form; must not be misread as combinational.
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


def _run(design, args):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "dut.sv").write_text(design)
        proc = subprocess.run(
            RTLSCANNER + ["driver", "dut.sv", *args, "--json"],
            cwd=d, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert proc.stdout, proc.stderr
        return json.loads(proc.stdout)


class DriverCommand(unittest.TestCase):
    def test_sequential_driver_timing_and_branches(self):
        env = _run(SEQ_DESIGN, ["-s", "q", "--scope", "dut"])
        self.assertEqual(env["status"], "ok")
        drivers = env["data"]["drivers"]
        self.assertEqual(len(drivers), 1)
        d = drivers[0]

        timing = d["timing"]
        self.assertEqual(timing["kind"], "sequential")
        self.assertEqual(timing["clock"], "clk")
        self.assertEqual(timing["clock_edge"], "posedge")
        self.assertEqual(timing["reset"], "rst")
        self.assertTrue(timing["reset_async"])

        assigns = d["assignments"]
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
        d = env["data"]["drivers"][0]
        self.assertEqual(d["timing"]["kind"], "combinational")
        ops = {o["name"] for o in d["assignments"][0]["rhs_operands"]}
        self.assertEqual(ops, {"sel", "a", "b"})

    def test_case_statement_guards(self):
        env = _run(CASE_DESIGN, ["-s", "y", "--scope", "fsm"])
        d = env["data"]["drivers"][0]
        # single-clock `always @(posedge clk)` is still sequential
        self.assertEqual(d["timing"]["kind"], "sequential")
        self.assertEqual(d["timing"]["clock"], "clk")
        assigns = d["assignments"]
        # three branches: y<=1, y<=2, default y<=0
        self.assertEqual(len(assigns), 3)
        for a in assigns:
            self.assertTrue(a["guards"])
            self.assertEqual(a["guards"][0]["kind"], "case")

    def test_single_clock_flop_is_sequential(self):
        # `always_ff @(posedge clk)` is a single-event sensitivity list;
        # its clock must still be recovered (regression guard).
        env = _run(SYNC_DESIGN, ["-s", "q", "--scope", "ff"])
        timing = env["data"]["drivers"][0]["timing"]
        self.assertEqual(timing["kind"], "sequential")
        self.assertEqual(timing["clock"], "clk")
        self.assertEqual(timing["clock_edge"], "posedge")
        # no reset in the sensitivity list -> synchronous reset, not reported here
        self.assertNotIn("reset", timing)

    def test_bit_select_narrows_driver_origin(self):
        # bus[1] is written only by the `bus[3:0] = lo` slice.
        env = _run(SPLIT_DESIGN, ["-s", "bus[1]", "--scope", "split"])
        drivers = env["data"]["drivers"]
        self.assertEqual(len(drivers), 1)
        self.assertEqual(drivers[0]["assignments"][0]["rhs_text"], "lo")
        # bus[5] is written only by the `bus[7:4] = hi` slice.
        env = _run(SPLIT_DESIGN, ["-s", "bus[5]", "--scope", "split"])
        drivers = env["data"]["drivers"]
        self.assertEqual(len(drivers), 1)
        self.assertEqual(drivers[0]["assignments"][0]["rhs_text"], "hi")

    def test_bit_select_out_of_range_errors(self):
        env = _run(SPLIT_DESIGN, ["-s", "bus[99]", "--scope", "split"])
        self.assertEqual(env["status"], "error")

    def test_summary_reports_driver_count(self):
        env = _run(SEQ_DESIGN, ["-s", "q", "--scope", "dut"])
        self.assertEqual(env["summary"]["drivers"], 1)
        self.assertEqual(env["summary"]["signal"], "q")


if __name__ == "__main__":
    unittest.main()

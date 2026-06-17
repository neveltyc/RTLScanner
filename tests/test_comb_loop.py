"""Combinational-loop detection (Tarjan SCC over the non-sequential edges).

A combinational loop is a feedback path with no register in it.  The check runs
cycle detection over the flow graph restricted to its *non-clocked* edges, so a
flip-flop in the path breaks the cycle.  These tests pin: an assign/always_comb
cycle is flagged; the same shape with a register in the loop is not; a structural
self-assign is flagged; and a loop that closes through child ports is caught.
"""

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

try:
    import pyslang.ast as ast  # noqa: F401  (availability guard)
    from rtl_common import build_compilation
    from rtl_lint import CombLoopAnalyzer
    from signal_trace import SignalTracer
    HAVE_PYSLANG = True
except Exception:  # pragma: no cover
    HAVE_PYSLANG = False


def _loops(text, unroll=False):
    p = Path(tempfile.mkdtemp()) / "loop.sv"
    p.write_text(textwrap.dedent(text))
    comp = build_compilation([str(p)])[0]
    tracer = SignalTracer(comp, unroll=unroll)
    return CombLoopAnalyzer(comp, tracer=tracer).loops()


def _leaf_sets(loops):
    """List of leaf-name sets, one per reported loop."""
    return [{n.rsplit('.', 1)[-1] for n in lp.nodes} for lp in loops]


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class LoopDetection(unittest.TestCase):
    def test_assign_cycle(self):
        loops = _loops("""
            module m(input logic en, output logic y);
              logic a, b;
              assign a = b & en;
              assign b = a | en;
              assign y = a;
            endmodule
            """)
        self.assertEqual(len(loops), 1)
        self.assertLessEqual({"a", "b"}, _leaf_sets(loops)[0])

    def test_always_comb_cycle(self):
        loops = _loops("""
            module m(input logic en, output logic y);
              logic a, b;
              always_comb a = b & en;
              always_comb b = a | en;
              assign y = a;
            endmodule
            """)
        self.assertEqual(len(loops), 1)
        self.assertLessEqual({"a", "b"}, _leaf_sets(loops)[0])

    def test_registered_feedback_is_not_a_loop(self):
        # A flip-flop in the path breaks the combinational cycle.
        loops = _loops("""
            module m(input logic clk, input logic d, output logic o);
              logic a, b;
              always_ff @(posedge clk) a <= b;
              assign b = a ^ d;
              assign o = a;
            endmodule
            """)
        self.assertEqual(loops, [])

    def test_self_assign_loop(self):
        loops = _loops("""
            module m(output logic o);
              logic a;
              assign a = a;
              assign o = a;
            endmodule
            """)
        self.assertEqual(len(loops), 1)
        self.assertEqual(_leaf_sets(loops)[0], {"a"})

    def test_self_loop_on_min_node_does_not_hide_multinode_cycle(self):
        # Regression: a multi-node combinational SCC whose lexicographically
        # smallest node ('a') ALSO has a structural self-loop must still be
        # reported.  Cycle reconstruction used to close on 'a's trivial self-edge
        # and return the length-1 path [a], which the caller then dropped --
        # silently losing the real a<->b loop (and the self-loop with it).
        loops = _loops("""
            module m(input logic en, output logic y);
              logic a, b;
              assign a = a & b & en;   // 'a' self-loop + a<-b
              assign b = a | en;       // b<-a closes the a<->b loop
              assign y = a;
            endmodule
            """)
        self.assertEqual(len(loops), 1)
        self.assertLessEqual({"a", "b"}, _leaf_sets(loops)[0])

    def test_clean_pipeline_has_no_loop(self):
        loops = _loops("""
            module m(input logic clk, input logic d, output logic o);
              logic a, b;
              always_ff @(posedge clk) a <= d;
              always_ff @(posedge clk) b <= a;
              assign o = b;
            endmodule
            """)
        self.assertEqual(loops, [])

    def test_constant_dead_branch_is_not_a_loop(self):
        # Regression: the only edge that would close a y -> z -> y cycle (`z = y`)
        # lives in a branch guarded by a constant-0 condition, so it is dead in
        # elaborated hardware.  With constant-condition pruning on (the same pass
        # fanin/fanout/trace use) the dead edge drops out and there is no loop;
        # without it a phantom y -> z -> y loop appears.  This pins that comb-loop
        # runs on the pruned graph.
        design = """
            module top(input logic a, output logic y, output logic z);
              localparam bit C = 1'b0;
              assign y = z & a;
              always_comb begin
                if (C) z = y;        // dead branch: phantom z <- y edge
                else   z = a;
              end
            endmodule
            """
        self.assertEqual(_loops(design, unroll=True), [])      # pruned: no loop
        # The unpruned graph still over-approximates the dead edge into a loop,
        # so this proves pruning is what removes the false positive.
        self.assertEqual(len(_loops(design, unroll=False)), 1)

    def test_cross_hierarchy_loop_through_ports(self):
        loops = _loops("""
            module inv(input logic i, output logic o);
              assign o = ~i;
            endmodule
            module m(output logic y);
              logic a, b;
              inv u1(.i(a), .o(b));
              inv u2(.i(b), .o(a));
              assign y = a;
            endmodule
            """)
        self.assertEqual(len(loops), 1)
        # the loop closes through both child instances
        leaves = _leaf_sets(loops)[0]
        self.assertLessEqual({"a", "b"}, leaves)


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class LintRuleWiring(unittest.TestCase):
    """`comb-loop` is an opt-in lint family, parallel to `cdc`."""

    DEMO = ["-d", "examples/lint"]

    def _run(self, *args):
        proc = subprocess.run(
            RTLSCANNER + ["lint"] + list(args) + ["--json"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        env = json.loads(proc.stdout)
        self.assertEqual(env["status"], "ok", env.get("errors"))
        return env

    @staticmethod
    def _rules(env):
        return {f["rule"] for f in env["data"]["findings"]}

    def test_opt_in_reports_comb_loop(self):
        env = self._run("examples/lint/comb_loop_demo.sv", "--rules", "comb-loop")
        self.assertIn("comb-loop", self._rules(env))
        # the registered_feedback module is legal sequential feedback
        msgs = " ".join(f["message"] for f in env["data"]["findings"])
        self.assertNotIn("registered_feedback", msgs)

    def test_constant_dead_branch_not_flagged(self):
        # Regression: the lint comb-loop check runs on the pruned dataflow graph
        # (constant if/case dead branches dropped), the same graph fanin/fanout/
        # CDC use.  The const_dead_branch module has only a phantom y -> z -> y
        # loop that exists solely through a constant-0 dead branch, so it must
        # NOT be reported.
        env = self._run("examples/lint/comb_loop_demo.sv", "--rules", "comb-loop")
        msgs = " ".join(f["message"] for f in env["data"]["findings"])
        self.assertNotIn("const_dead_branch", msgs)
        # the real loops in the demo are still caught
        self.assertIn("comb_loop", msgs)

    def test_comb_loop_findings_carry_module(self):
        # comb-loop findings are attributed to their enclosing module.
        env = self._run("examples/lint/comb_loop_demo.sv", "--rules", "comb-loop")
        mods = {f.get("module") for f in env["data"]["findings"]}
        self.assertIn("comb_loop", mods)
        self.assertNotIn(None, mods)

    def test_default_includes_comb_loop(self):
        # comb-loop is one of the five categories run by default (no flag).
        env = self._run("examples/lint/comb_loop_demo.sv")
        self.assertIn("comb-loop", self._rules(env))

    def test_check_category_is_comb_loop(self):
        env = self._run("examples/lint/comb_loop_demo.sv", "--rules", "comb-loop")
        checks = {f["check"] for f in env["data"]["findings"]}
        self.assertEqual(checks, {"comb-loop"})

    def test_help_lists_comb_loop_category(self):
        text = subprocess.run(RTLSCANNER + ["lint", "--help"], cwd=ROOT,
                              text=True, stdout=subprocess.PIPE).stdout
        self.assertIn("comb-loop", text)


if __name__ == "__main__":
    unittest.main()

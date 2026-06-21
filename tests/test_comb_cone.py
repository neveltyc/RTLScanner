"""Combinational-cone mode for ``fanin`` / ``fanout`` (``--comb``).

A combinational cone stops at register nodes, exactly like slang-netlist's
``getCombFanIn`` / ``getCombFanOut`` (DFS predicate ``source/target != State``).
These tests pin that boundary in RTLScanner's edge model, where "register node"
means "target of a clocked edge":

  * fan-out from combinational logic stops before a downstream flop;
  * fan-in to a combinational node excludes the upstream boundary flops;
  * fan-in to a *register output* still walks its own combinational D-cone
    (the start is the seed), stopping at the next register up;
  * the cone defaults to unbounded depth (registers bound it), and a comb loop
    still terminates;
  * ``--comb`` changes nothing about the default (full) traversal.
"""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import pyslang.ast as ast  # noqa: F401  (availability guard)
    from rtl_common import build_compilation
    from rtl_dataflow import SignalTracer
    HAVE_PYSLANG = True
except Exception:  # pragma: no cover
    HAVE_PYSLANG = False


# A pipeline with one register in the middle:
#   a,b -> s (comb) -> q (flop) -> m (comb) -> y (comb)
PIPE = textwrap.dedent("""
    module dut(input logic clk, input logic [7:0] a, b,
               output logic [7:0] y);
      logic [7:0] s, q, m;
      assign s = a & b;                  // comb: a,b -> s
      always_ff @(posedge clk) q <= s;   // flop: s -> q  (clocked boundary)
      assign m = q | b;                  // comb: q,b -> m
      assign y = m;                      // comb: m -> y
    endmodule
    """)


def _tracer(text):
    p = Path(tempfile.mkdtemp()) / "comb.sv"
    p.write_text(text)
    return SignalTracer(build_compilation([str(p)])[0])


def _pairs(result):
    return {(e.source.rsplit(".", 1)[-1], e.target.rsplit(".", 1)[-1])
            for e, _d in result.edges}


def _nodes(result):
    return {n.rsplit(".", 1)[-1] for n in result.nodes}


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class CombBoundary(unittest.TestCase):
    def setUp(self):
        self.tr = _tracer(PIPE)

    def test_fanout_stops_before_downstream_flop(self):
        # Comb fan-out of input `a` reaches `s`, then stops — the register `q`
        # is a sequential boundary and is excluded entirely.
        comb = self.tr.flow("a", "dut", "fanout", None, comb=True)
        self.assertEqual(_pairs(comb), {("a", "s")})
        self.assertNotIn("q", _nodes(comb))
        self.assertFalse(any(e.clocked for e, _ in comb.edges))

    def test_full_fanout_crosses_the_flop(self):
        # Without --comb the same query crosses the register: the clocked edge
        # s -> q is present and the cone continues to m and y.
        full = self.tr.flow("a", "dut", "fanout", 8)
        self.assertIn(("s", "q"), _pairs(full))
        self.assertTrue(any(e.clocked and e.source.endswith(".s")
                            for e, _ in full.edges))
        self.assertIn("m", _nodes(full))

    def test_fanin_excludes_upstream_boundary_flop(self):
        # Comb fan-in of output `y`: y <- m <- {q, b}.  `q` is a register, so it
        # and the edge q -> m drop out; only the combinational `b` survives.
        comb = self.tr.flow("y", "dut", "fanin", None, comb=True)
        self.assertEqual(_pairs(comb), {("m", "y"), ("b", "m")})
        self.assertNotIn("q", _nodes(comb))
        self.assertFalse(any(e.clocked for e, _ in comb.edges))

    def test_fanin_of_register_output_walks_its_d_cone(self):
        # The start IS the register: its own D-cone is still reported (s, then
        # a,b), and the clocked d->q edge is the boundary edge shown.  It stops
        # there — no register beyond `q` exists, so a,b are the cone leaves.
        comb = self.tr.flow("q", "dut", "fanin", None, comb=True)
        self.assertEqual(_pairs(comb), {("s", "q"), ("a", "s"), ("b", "s")})
        # the s -> q boundary edge is the (one) clocked edge in the cone
        clocked = [(e.source.rsplit(".", 1)[-1], e.target.rsplit(".", 1)[-1])
                   for e, _ in comb.edges if e.clocked]
        self.assertEqual(clocked, [("s", "q")])


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class CombDepthAndTermination(unittest.TestCase):
    CHAIN = textwrap.dedent("""
        module chain(input logic [7:0] i, output logic [7:0] o);
          logic [7:0] a, b, c, d, e, f;
          assign a = i;  assign b = a;  assign c = b;  assign d = c;
          assign e = d;  assign f = e;  assign o = f;
        endmodule
        """)

    def test_comb_default_is_unbounded(self):
        # A 7-hop combinational chain: the default depth-4 fan-in stops short of
        # the primary input `i`, but the (unbounded) comb cone reaches it.
        tr = _tracer(self.CHAIN)
        bounded = tr.flow("o", "chain", "fanin", 4)
        self.assertNotIn("i", _nodes(bounded))
        comb = tr.flow("o", "chain", "fanin", None, comb=True)
        self.assertIn("i", _nodes(comb))
        self.assertGreater(comb.max_depth, 4)

    def test_explicit_depth_still_caps_a_comb_cone(self):
        tr = _tracer(self.CHAIN)
        capped = tr.flow("o", "chain", "fanin", 2, comb=True)
        self.assertEqual(capped.max_depth, 2)
        self.assertNotIn("i", _nodes(capped))

    def test_comb_loop_terminates(self):
        # a -> b -> a with no register: the cone must not loop forever.
        tr = _tracer(textwrap.dedent("""
            module loop(input logic en, output logic y);
              logic a, b;
              assign a = b & en;
              assign b = a | en;
              assign y = a;
            endmodule
            """))
        comb = tr.flow("a", "loop", "fanout", None, comb=True)
        self.assertIn(("a", "b"), _pairs(comb))
        self.assertIn(("b", "a"), _pairs(comb))


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class CombFlagSurfaced(unittest.TestCase):
    """The JSON / human output advertises that the cone is combinational."""

    def test_flow_result_carries_comb_flag(self):
        tr = _tracer(PIPE)
        self.assertTrue(tr.flow("y", "dut", "fanin", None, comb=True).comb)
        self.assertFalse(tr.flow("y", "dut", "fanin", 4).comb)


if __name__ == "__main__":
    unittest.main()

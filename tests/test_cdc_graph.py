"""Graph-based, cross-hierarchy CDC detection.

The CDC check runs on the dataflow flow graph instead of a single-module
``always_ff`` scan, and resolves each flop's clock to its **source net** before
comparing domains.  These tests pin the properties that buys:

  * a launch flop feeding (combinationally) a capture flop in a *different*
    clock domain is flagged, even across module boundaries;
  * two flops on the *same physical clock* are one domain even when their local
    clock ports are named differently or live in different instances — the
    false positive/negative the old name-compare produced;
  * asynchronous resets are not mistaken for a clock domain;
  * a gated clock is a distinct domain.
"""

import fnmatch
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
    from rtl_lint import _DEFAULT_RESET_GLOBS
    from signal_trace import SignalTracer
    HAVE_PYSLANG = True
except Exception:  # pragma: no cover
    HAVE_PYSLANG = False


def _is_reset(name):
    n = (name or "").lower()
    return any(fnmatch.fnmatch(n, g.lower()) for g in _DEFAULT_RESET_GLOBS)


def _tracer(text):
    p = Path(tempfile.mkdtemp()) / "cdc.sv"
    p.write_text(textwrap.dedent(text))
    return SignalTracer(build_compilation([str(p)])[0])


def _crossings(text):
    return _tracer(text).cdc_crossings(_is_reset)


def _pairs(crossings):
    """{(launch_leaf, capture_leaf)} for terse assertions."""
    return {(c.launch_name, c.capture_name) for c in crossings}


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class TwoClockCrossing(unittest.TestCase):
    SRC = """
        module top(input logic clka, input logic clkb,
                   input logic d, output logic q);
          logic a;
          always_ff @(posedge clka) a <= d;     // launch: clka
          always_ff @(posedge clkb) q <= a;     // capture: clkb  -> CDC
        endmodule
        """

    def test_crossing_is_flagged_with_domains(self):
        cx = _crossings(self.SRC)
        self.assertIn(("a", "q"), _pairs(cx))
        c = next(c for c in cx if (c.launch_name, c.capture_name) == ("a", "q"))
        self.assertEqual(c.from_domains, ["clka"])
        self.assertEqual(c.to_domains, ["clkb"])

    def test_same_clock_pipeline_is_clean(self):
        # Both flops on clk: a -> q is a legal same-domain path, not a CDC.
        cx = _crossings("""
            module top(input logic clk, input logic d, output logic q);
              logic a;
              always_ff @(posedge clk) a <= d;
              always_ff @(posedge clk) q <= a;
            endmodule
            """)
        self.assertEqual(cx, [])


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class SourceNetResolution(unittest.TestCase):
    """The headline win: domains are compared by *source net*, not local name."""

    def test_aliased_clock_across_instances_is_one_domain(self):
        # u_a and u_b both clock off the single net `sysclk`, but each child's
        # local clock port is named `clk`.  The old name-compare would treat
        # any two `clk`-named flops as one domain by luck here, but it could
        # never even relate flops across module boundaries; the graph + source
        # resolution gets it right: u_a -> u_b is NOT a crossing, while the
        # genuine sysclk -> clkb capture IS.
        cx = _crossings("""
            module ff(input logic clk, input logic d, output logic q);
              always_ff @(posedge clk) q <= d;
            endmodule
            module top(input logic sysclk, input logic clkb,
                       input logic din, output logic dout);
              logic a, b;
              ff u_a(.clk(sysclk), .d(din), .q(a));
              ff u_b(.clk(sysclk), .d(a),   .q(b));  // same source net as u_a
              always_ff @(posedge clkb) dout <= b;   // real crossing
            endmodule
            """)
        pairs = _pairs(cx)
        self.assertNotIn(("q", "q"), pairs)          # u_a.q -> u_b.q not flagged
        self.assertIn(("q", "dout"), pairs)          # u_b.q (sysclk) -> dout (clkb)
        # every reported crossing is genuinely cross-domain
        for c in cx:
            self.assertNotEqual(set(c.from_domains), set(c.to_domains))

    def test_cross_hierarchy_crossing(self):
        # Launch in one submodule, capture in another, wired through the top.
        cx = _crossings("""
            module launch_m(input logic clka, input logic d, output logic q);
              always_ff @(posedge clka) q <= d;
            endmodule
            module capture_m(input logic clkb, input logic d, output logic q);
              always_ff @(posedge clkb) q <= d;
            endmodule
            module top(input logic clka, input logic clkb,
                       input logic din, output logic dout);
              logic mid;
              launch_m u_l(.clka(clka), .d(din), .q(mid));
              capture_m u_c(.clkb(clkb), .d(mid), .q(dout));
            endmodule
            """)
        full = {(c.launch, c.capture) for c in cx}
        self.assertIn(("top.u_l.q", "top.u_c.q"), full)


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class ResetAndGating(unittest.TestCase):
    def test_async_reset_is_not_a_domain(self):
        # Single clock with an async reset: the `negedge rst_n` event and the
        # `if(!rst_n)` control read must not look like a second domain.
        cx = _crossings("""
            module top(input logic clk, input logic rst_n,
                       input logic d, output logic q);
              logic a;
              always_ff @(posedge clk or negedge rst_n)
                if(!rst_n) a <= 1'b0; else a <= d;
              always_ff @(posedge clk or negedge rst_n)
                if(!rst_n) q <= 1'b0; else q <= a;
            endmodule
            """)
        self.assertEqual(cx, [])

    def test_gated_clock_is_a_distinct_domain(self):
        cx = _crossings("""
            module top(input logic clk, input logic en,
                       input logic d, output logic q1, output logic q2);
              logic gclk;
              assign gclk = clk & en;
              always_ff @(posedge clk)  q1 <= d;
              always_ff @(posedge gclk) q2 <= q1;   // gated clock -> distinct
            endmodule
            """)
        self.assertIn(("q1", "q2"), _pairs(cx))


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class EdgeClassification(unittest.TestCase):
    """`FlowEdge.clocked` underpins both CDC and loop detection: it is set for
    edges driven by a sequential block and clear for combinational ones."""

    def test_clocked_flag_on_edges(self):
        tr = _tracer("""
            module m(input logic clk, input logic d, output logic o);
              logic q;
              always_ff @(posedge clk) q <= d;   // clocked edge: d -> q
              assign o = q;                       // combinational: q -> o
            endmodule
            """)
        by_pair = {(e.source.rsplit('.', 1)[-1], e.target.rsplit('.', 1)[-1]): e
                   for e in tr._build_flow_edges()}
        self.assertTrue(by_pair[("d", "q")].clocked)
        self.assertFalse(by_pair[("q", "o")].clocked)
        # the registered edge is exposed in the dataflow JSON, too
        self.assertTrue(by_pair[("d", "q")].to_dict().get("clocked"))
        self.assertNotIn("clocked", by_pair[("q", "o")].to_dict())


if __name__ == "__main__":
    unittest.main()

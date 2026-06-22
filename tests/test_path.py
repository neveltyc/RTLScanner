"""Point-to-point path finding (``rtlscanner path``).

``path`` finds a directional dataflow path between two nodes by depth-first
search over the same graph ``fanin``/``fanout`` traverse (one forward DFS builds
a parent map, the path to the end is read back and reversed; ``find`` vs
``findComb`` differ only by an edge predicate).  These tests pin:

  * a path is found across continuous assigns, procedural blocks, and port
    boundaries (cross-hierarchy), and reports the node/edge walk in order;
  * no path is a *normal* empty result (``found == False``), not an error;
  * the path is directional (``from`` must drive ``to``);
  * ``from == to`` is a single-node, zero-hop path;
  * ``--comb`` never crosses a register, so a path through a flop disappears
    while the register's own comb fan-out path stays;
  * the agent envelope (shape, summary counts, ``--comb`` flag, errors).
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
    from rtl_dataflow import PathFinder, SignalTracer
    HAVE_PYSLANG = True
except Exception:  # pragma: no cover
    HAVE_PYSLANG = False


# Two disjoint combinational copies (the classic "no path" fixture): a -> c
# and b -> d, with no path from a to d.
SPLIT = textwrap.dedent("""
    module m(input logic a, input logic b,
             output logic c, output logic d);
      assign c = a;
      assign d = b;
    endmodule
    """)

# A pipeline with one register in the middle:
#   a,b -> s (comb) -> q (flop) -> m (comb), and b -> m (comb) -> y (comb)
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
    p = Path(tempfile.mkdtemp()) / "path.sv"
    p.write_text(text)
    return SignalTracer(build_compilation([str(p)])[0])


def _leaves(nodes):
    """The path's node sequence as leaf names (order preserved)."""
    return [n.rsplit(".", 1)[-1] for n in nodes]


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class FindAnyPath(unittest.TestCase):
    def test_simple_assign_path(self):
        tr = _tracer(SPLIT)
        r = tr.find_path("a", "m", "c", "m")
        self.assertTrue(r.found)
        self.assertEqual(_leaves(r.nodes), ["a", "c"])
        self.assertEqual(r.length, 1)
        # the single edge is the continuous assign a -> c
        self.assertEqual(len(r.edges), 1)
        self.assertEqual(r.edges[0].kind, "continuous_assign")

    def test_no_path_is_empty_not_error(self):
        # a connects to c, b to d, but there is no path a -> d.
        tr = _tracer(SPLIT)
        r = tr.find_path("a", "m", "d", "m")
        self.assertFalse(r.found)
        self.assertEqual(r.nodes, [])
        self.assertEqual(r.edges, [])
        self.assertEqual(r.length, 0)

    def test_from_equals_to_is_single_node(self):
        tr = _tracer(SPLIT)
        r = tr.find_path("a", "m", "a", "m")
        self.assertTrue(r.found)
        self.assertEqual(_leaves(r.nodes), ["a"])
        self.assertEqual(r.edges, [])
        self.assertEqual(r.length, 0)

    def test_path_is_directional(self):
        # c is downstream of a; there is no forward path c -> a.
        tr = _tracer(SPLIT)
        self.assertFalse(tr.find_path("c", "m", "a", "m").found)

    def test_path_crosses_a_register(self):
        # a -> s -> q (flop) -> m -> y: the full path crosses the register and
        # the s -> q edge is the (clocked) boundary.
        tr = _tracer(PIPE)
        r = tr.find_path("a", "dut", "y", "dut")
        self.assertTrue(r.found)
        self.assertEqual(_leaves(r.nodes), ["a", "s", "q", "m", "y"])
        clocked = [(e.source.rsplit(".", 1)[-1], e.target.rsplit(".", 1)[-1])
                   for e in r.edges if e.clocked]
        self.assertEqual(clocked, [("s", "q")])

    def test_edges_chain_the_nodes(self):
        # edges[i] connects nodes[i] -> nodes[i+1] for the whole walk.
        tr = _tracer(PIPE)
        r = tr.find_path("a", "dut", "y", "dut")
        for i, e in enumerate(r.edges):
            self.assertEqual(e.source, r.nodes[i])
            self.assertEqual(e.target, r.nodes[i + 1])


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class FindCombPath(unittest.TestCase):
    def test_comb_path_blocked_by_register(self):
        # a reaches y only through the flop q, so there is NO combinational path.
        tr = _tracer(PIPE)
        self.assertFalse(tr.find_path("a", "dut", "y", "dut", comb=True).found)
        # the non-comb find still crosses it.
        self.assertTrue(tr.find_path("a", "dut", "y", "dut").found)

    def test_comb_path_around_the_register(self):
        # b feeds m directly (m = q | b), so b -> m -> y is purely combinational.
        tr = _tracer(PIPE)
        r = tr.find_path("b", "dut", "y", "dut", comb=True)
        self.assertTrue(r.found)
        self.assertEqual(_leaves(r.nodes), ["b", "m", "y"])
        self.assertFalse(any(e.clocked for e in r.edges))

    def test_comb_path_from_register_output(self):
        # The start IS the register q (always expanded): its own comb fan-out
        # path q -> m -> y is found, with no clocked edge.
        tr = _tracer(PIPE)
        r = tr.find_path("q", "dut", "y", "dut", comb=True)
        self.assertTrue(r.found)
        self.assertEqual(_leaves(r.nodes), ["q", "m", "y"])
        self.assertFalse(any(e.clocked for e in r.edges))

    def test_pathfinder_find_and_findcomb_directly(self):
        # The PathFinder class exposes find / findComb on raw node paths.
        tr = _tracer(PIPE)
        finder = PathFinder(tr)
        nodes, _edges = finder.find("dut.a", "dut.y")
        self.assertEqual(_leaves(nodes), ["a", "s", "q", "m", "y"])
        comb_nodes, _ = finder.findComb("dut.a", "dut.y")
        self.assertEqual(comb_nodes, [])


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class CrossHierarchy(unittest.TestCase):
    """A path threads through child-instance port connections (the basic
    example: top.a -> u_dp0 register -> u_alu adder -> top.y0)."""

    def _run(self, *args):
        return subprocess.run(RTLSCANNER + ["path", "-d", "examples/basic",
                                            "--scope", "top", *args],
                              cwd=ROOT, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def test_cross_module_path_found(self):
        p = self._run("--from", "a", "--to", "y0", "--json")
        d = json.loads(p.stdout)["data"]
        self.assertTrue(d["found"])
        # passes through both module boundaries and the register
        self.assertIn("top.u_dp0.u_reg.q", d["nodes"])
        self.assertIn("top.u_dp0.u_alu.u_add.y", d["nodes"])
        self.assertEqual(d["nodes"][0], "top.a")
        self.assertEqual(d["nodes"][-1], "top.y0")

    def test_comb_blocked_across_hierarchy(self):
        # a -> y0 crosses u_dp0's register, so there is no combinational path.
        d = json.loads(self._run("--from", "a", "--to", "y0", "--comb",
                                  "--json").stdout)["data"]
        self.assertFalse(d["found"])
        self.assertTrue(d["comb"])


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class CliEnvelope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = Path(tempfile.mkdtemp()) / "pipe.sv"
        cls.src.write_text(PIPE)

    def _run(self, *args):
        return subprocess.run(RTLSCANNER + ["path", str(self.src),
                                            "--scope", "dut", *args],
                              cwd=ROOT, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def test_json_found_shape(self):
        p = self._run("--from", "a", "--to", "y", "--json")
        env = json.loads(p.stdout)
        self.assertEqual(env["status"], "ok")
        d, s = env["data"], env["summary"]
        self.assertTrue(d["found"])
        self.assertEqual(d["from"], "a")
        self.assertEqual(d["to"], "y")
        self.assertEqual(len(d["edges"]), len(d["nodes"]) - 1)
        self.assertEqual(s["mode"], "path")
        self.assertEqual(s["found"], True)
        self.assertEqual(s["length"], d["length"])
        self.assertIn("truncated", s)
        self.assertIn("limit", s)

    def test_json_not_found_is_ok(self):
        # No combinational path a -> y (register in between).
        p = self._run("--from", "a", "--to", "y", "--comb", "--json")
        env = json.loads(p.stdout)
        self.assertEqual(env["status"], "ok")    # not-found is a normal result
        self.assertEqual(p.returncode, 0)
        self.assertFalse(env["data"]["found"])
        self.assertEqual(env["data"]["nodes"], [])
        self.assertTrue(env["data"]["comb"])

    def test_human_output_walk(self):
        p = self._run("--from", "a", "--to", "y", "--no-color")
        self.assertIn("Path:", p.stdout)
        self.assertIn("found", p.stdout)
        self.assertIn("→", p.stdout)        # the path arrow header
        self.assertIn("always_ff", p.stdout)  # a procedural edge on the walk

    def test_human_comb_marks_the_path(self):
        p = self._run("--from", "b", "--to", "y", "--comb", "--no-color")
        self.assertIn("combinational", p.stdout)
        self.assertIn("found", p.stdout)

    def test_missing_endpoint_errors(self):
        env = json.loads(self._run("--from", "a", "--json").stdout)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["errors"][0]["code"], "INPUT_NOT_FOUND")

    def test_signal_not_found_errors(self):
        env = json.loads(self._run("--from", "nope", "--to", "y",
                                    "--json").stdout)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["errors"][0]["code"], "SIGNAL_NOT_FOUND")
        self.assertIn("close_matches", env["errors"][0]["details"])


if __name__ == "__main__":
    unittest.main()

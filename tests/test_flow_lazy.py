"""Demand-driven fanin/fanout.

`SignalTracer.flow()` expands a node's incident edges only when the BFS reaches
it, instead of building the whole-design flow graph up front.  These tests pin
the two properties that change buys us:

  * **Exactness** — the lazy traversal returns the same edges, in the same
    order, as a BFS over the fully-materialized graph (`_build_flow_edges`),
    including the cross-module *downward* hierarchical-reference case.
  * **Locality** — a shallow query on a wide design materializes only a small,
    design-size-independent neighborhood, never every instance.
"""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    # `import pyslang.ast` doubles as the availability guard: when pyslang is
    # absent it raises ImportError here, before the signal_trace import (whose
    # module-level dependency check would otherwise abort the interpreter).
    import pyslang.ast as ast
    from rtl_common import build_compilation, safe_str
    from rtl_slang import iter_instances, symbol_key
    from signal_trace import SignalTracer
    HAVE_PYSLANG = True
except Exception:  # pragma: no cover - exercised only without pyslang
    HAVE_PYSLANG = False


def _sv_files(*relparts):
    d = ROOT.joinpath(*relparts)
    return sorted(str(p) for p in d.glob("*.sv")) + \
        sorted(str(p) for p in d.glob("*.v"))


def _whole_graph_bfs(tr, start, mode, max_depth):
    """Oracle: the pre-refactor BFS over the fully-materialized flow graph."""
    edges = tr._build_flow_edges()
    by_source, by_target = {}, {}
    for e in edges:
        by_source.setdefault(e.source, []).append(e)
        by_target.setdefault(e.target, []).append(e)
    edge_map = by_target if mode == "fanin" else by_source
    traversed, seen_e, seen_n, frontier, depth = [], set(), {start}, [start], 0
    max_depth = max(0, int(max_depth))
    while frontier and depth < max_depth:
        depth += 1
        nxt_frontier = []
        for node in frontier:
            for e in edge_map.get(node, []):
                k = e.key()
                if k not in seen_e:
                    seen_e.add(k)
                    traversed.append((e, depth))
                nxt = e.source if mode == "fanin" else e.target
                if nxt not in seen_n:
                    seen_n.add(nxt)
                    nxt_frontier.append(nxt)
        frontier = nxt_frontier
    return traversed


def _signature(traversed):
    return [(e.source, e.target, e.kind, e.file, e.line, d)
            for e, d in traversed]


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class LazyFlowParity(unittest.TestCase):
    """Lazy flow() reproduces the whole-graph BFS edge-for-edge and in order."""

    def _all_signals(self, tr):
        out = []
        for inst in iter_instances(tr._root):
            for m in inst.body:
                if getattr(m, "kind", None) in (ast.SymbolKind.Net,
                                                ast.SymbolKind.Variable):
                    out.append((symbol_key(inst), safe_str(m.name, "")))
        return out

    def _assert_parity(self, files):
        comp = build_compilation(files)[0]
        tr = SignalTracer(comp)
        signals = self._all_signals(tr)
        self.assertTrue(signals, msg=f"no signals discovered in {files}")
        for scope, sig in signals:
            try:
                _inst, sym = tr._lookup(sig, scope)
            except Exception:
                continue
            start = tr._sym_path(sym)
            for mode in ("fanin", "fanout"):
                for depth in (1, 2, 4, 16):
                    lazy = _signature(tr.flow(sig, scope, mode, depth).edges)
                    oracle = _signature(
                        _whole_graph_bfs(tr, start, mode, depth))
                    self.assertEqual(
                        lazy, oracle,
                        msg=f"{files}: {scope}.{sig} {mode} depth={depth}")

    def test_trace_example(self):
        self._assert_parity(_sv_files("examples", "trace"))

    def test_generate_example(self):
        self._assert_parity(_sv_files("examples", "generate"))

    def test_basic_example(self):
        self._assert_parity(_sv_files("examples", "basic"))

    def test_ports_example(self):
        self._assert_parity(_sv_files("examples", "ports"))


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class DownwardHierRefParity(unittest.TestCase):
    """A parent procedure that hierarchically reads a child's internal signal
    anchors the dataflow edge at the parent, not at the child.  The lazy path
    must still surface it from the child-internal side, which it does by also
    consulting each ancestor's procedural edges."""

    @classmethod
    def setUpClass(cls):
        cls.src = Path(tempfile.mkdtemp()) / "hier.sv"
        cls.src.write_text(textwrap.dedent(
            """
            module sub(input logic clk, input logic [7:0] d,
                       output logic [7:0] q);
              logic [7:0] inner_q;
              always_ff @(posedge clk) inner_q <= d;
              assign q = inner_q;
            endmodule
            module top(input logic clk, input logic [7:0] din,
                       output logic [7:0] dout);
              logic [7:0] mon;
              sub u_sub(.clk(clk), .d(din), .q(dout));
              // procedural hierarchical reference down into the child:
              always_ff @(posedge clk) mon <= u_sub.inner_q + din;
            endmodule
            """
        ))

    def test_fanout_from_child_internal_reaches_parent_monitor(self):
        comp = build_compilation([str(self.src)])[0]
        tr = SignalTracer(comp)
        r = tr.flow("inner_q", "top.u_sub", "fanout", 2)
        keys = {(e.source, e.target, e.kind) for e, _ in r.edges}
        self.assertIn(
            ("top.u_sub.inner_q", "top.mon", "procedural"), keys)

    def test_parity_with_whole_graph(self):
        comp = build_compilation([str(self.src)])[0]
        tr = SignalTracer(comp)
        for inst in iter_instances(tr._root):
            for m in inst.body:
                if getattr(m, "kind", None) not in (ast.SymbolKind.Net,
                                                    ast.SymbolKind.Variable):
                    continue
                scope, sig = symbol_key(inst), safe_str(m.name, "")
                start = tr._sym_path(m)
                for mode in ("fanin", "fanout"):
                    lazy = _signature(tr.flow(sig, scope, mode, 8).edges)
                    oracle = _signature(_whole_graph_bfs(tr, start, mode, 8))
                    self.assertEqual(lazy, oracle,
                                     msg=f"{scope}.{sig} {mode}")


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class LazyLocality(unittest.TestCase):
    """A shallow query materializes only the touched neighborhood, so its cost
    does not grow with the size of the rest of the design."""

    @staticmethod
    def _wide_design(n):
        leaf = (
            "module leaf(input logic clk, input logic [7:0] a, b,\n"
            "            output logic [7:0] y);\n"
            "  logic [7:0] t;\n"
            "  always_ff @(posedge clk) t <= a + b;\n"
            "  assign y = t ^ b;\n"
            "endmodule\n"
        )
        lines = [leaf,
                 "module big(input logic clk, input logic [7:0] din,\n"
                 "           output logic [7:0] dout);",
                 f"  logic [7:0] w [0:{n}];",
                 "  assign w[0] = din;"]
        for i in range(n):
            lines.append(
                f"  leaf u{i}(.clk(clk), .a(w[{i}]), .b(din), .y(w[{i + 1}]));")
        lines.append(f"  assign dout = w[{n}];")
        lines.append("endmodule")
        p = Path(tempfile.mkdtemp()) / "big.sv"
        p.write_text("\n".join(lines))
        return p

    def _materialized(self, n):
        comp = build_compilation([str(self._wide_design(n))])[0]
        tr = SignalTracer(comp)
        total = len(list(iter_instances(tr._root)))
        # A leaf-internal fanin, depth 1 — touches the leaf and its parent only.
        tr.flow("t", "big.u0", "fanin", 1)
        return total, len(tr._inst_proc_cache)

    def test_depth1_cost_is_design_size_independent(self):
        total_small, touched_small = self._materialized(20)
        total_large, touched_large = self._materialized(200)

        # The large design really is much bigger...
        self.assertGreater(total_large, total_small * 5)
        # ...yet the same shallow query materializes the same tiny instance set,
        # a small fraction of the whole design.
        self.assertEqual(touched_small, touched_large)
        self.assertLess(touched_large, total_large // 10)


if __name__ == "__main__":
    unittest.main()

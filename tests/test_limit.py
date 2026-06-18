"""Output capping via ``--limit`` (default 200, ``--limit 0`` = unlimited).

A query against a large design must stay agent-friendly: every emitted list is
clipped to the effective limit while the count fields keep reporting the *true*
totals, and a truncation note (human) / ``truncated`` flag (JSON) tells the
caller that more exists.  Modeled on RWaveAnalyzer's ``--limit`` handling.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent_json  # noqa: E402


RTLSCANNER = [sys.executable, "-m", "rtlscanner"]


def _make_wide_design(dirpath, n):
    """Write top.sv: a top that instantiates a trivial leaf *n* times.

    Hierarchy has ``n + 1`` instances (the top plus ``n`` leaves), the top has
    two ports (``a`` and ``w``), and there are ``2 * n`` port connections.
    """
    leaf = "module leaf(input wire a, output wire y);\n  assign y = a;\nendmodule\n"
    insts = "\n".join(f"  leaf u{i} (.a(a), .y(w[{i}]));" for i in range(n))
    top = (f"module top(input wire a, output wire [{n - 1}:0] w);\n"
           f"{insts}\nendmodule\n")
    p = Path(dirpath) / "top.sv"
    p.write_text(leaf + top)
    return p


def _run(args, cwd):
    return subprocess.run(RTLSCANNER + args, text=True, cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class LimitHelpers(unittest.TestCase):
    """Pure-function behavior of the shared helpers."""

    def test_default_is_200(self):
        self.assertEqual(agent_json.DEFAULT_LIMIT, 200)

    def test_resolve_default(self):
        self.assertEqual(agent_json.resolve_limit(None), 200)

    def test_resolve_explicit_wins(self):
        self.assertEqual(agent_json.resolve_limit(5), 5)

    def test_resolve_zero_is_unlimited(self):
        self.assertEqual(agent_json.resolve_limit(0), 0)

    def test_resolve_negative_is_unlimited(self):
        self.assertEqual(agent_json.resolve_limit(-3), 0)

    def test_resolve_verbose_only_without_explicit_limit(self):
        # --verbose disables truncation, but an explicit --limit still wins.
        self.assertEqual(agent_json.resolve_limit(None, verbose=True), 0)
        self.assertEqual(agent_json.resolve_limit(7, verbose=True), 7)

    def test_clip_under_limit(self):
        self.assertEqual(agent_json.clip([1, 2, 3], 5), ([1, 2, 3], 3, False))

    def test_clip_over_limit(self):
        self.assertEqual(agent_json.clip([1, 2, 3, 4, 5], 2), ([1, 2], 5, True))

    def test_clip_unlimited(self):
        shown, total, trunc = agent_json.clip(range(10), 0)
        self.assertEqual((len(shown), total, trunc), (10, 10, False))

    def test_truncation_note_mentions_escape_hatch(self):
        note = agent_json.truncation_note(3, 11, "instances")
        self.assertIn("3/11 instances", note)
        self.assertIn("--limit 0", note)


class TreeFlatLimit(unittest.TestCase):
    def test_flat_caps_and_notes_true_total(self):
        with tempfile.TemporaryDirectory() as d:
            _make_wide_design(d, 10)  # 11 instances
            proc = _run(["tree", "top.sv", "--flat", "--limit", "3", "--no-color"], d)
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            body = [l for l in lines if "truncated" not in l]
            self.assertEqual(len(body), 3)
            self.assertTrue(any("truncated: 3/11 instances" in l for l in lines))

    def test_flat_unlimited_emits_everything(self):
        with tempfile.TemporaryDirectory() as d:
            _make_wide_design(d, 10)
            proc = _run(["tree", "top.sv", "--flat", "--limit", "0", "--no-color"], d)
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            self.assertEqual(len(lines), 11)
            self.assertFalse(any("truncated" in l for l in lines))

    def test_json_summary_total_truthful(self):
        with tempfile.TemporaryDirectory() as d:
            _make_wide_design(d, 10)
            proc = _run(["tree", "top.sv", "--limit", "3", "--json"], d)
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "ok")
            self.assertEqual(env["summary"]["instances"], 11)   # true total
            self.assertTrue(env["summary"]["truncated"])
            self.assertEqual(env["summary"]["limit"], 3)


class ScopeLimit(unittest.TestCase):
    def test_connections_clip_but_count_truthful(self):
        with tempfile.TemporaryDirectory() as d:
            _make_wide_design(d, 10)   # 20 port connections
            proc = _run(["scope", "top.sv", "--connections", "--limit", "4",
                         "--json"], d)
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "ok")
            self.assertEqual(len(env["data"]["connections"]), 4)
            self.assertEqual(env["summary"]["connections"], 20)  # true total
            self.assertTrue(env["summary"]["truncated"])

    def test_unlimited_returns_all_connections(self):
        with tempfile.TemporaryDirectory() as d:
            _make_wide_design(d, 10)
            proc = _run(["scope", "top.sv", "--connections", "--limit", "0",
                         "--json"], d)
            env = json.loads(proc.stdout)
            self.assertEqual(len(env["data"]["connections"]), 20)
            self.assertFalse(env["summary"]["truncated"])


class FlowLimitGraphConsistency(unittest.TestCase):
    """fanin/fanout keep `nodes` and `edges` mutually consistent under --limit.

    Clipping the two lists independently could emit an edge whose endpoint was
    dropped from `nodes`, leaving the JSON graph internally inconsistent.
    """

    def _fanout(self, d, limit):
        proc = _run(["fanout", "top.sv", "-s", "a", "--scope", "top",
                     "--limit", str(limit), "--json"], d)
        return json.loads(proc.stdout)

    def test_edges_only_reference_present_nodes(self):
        with tempfile.TemporaryDirectory() as d:
            _make_wide_design(d, 8)   # 'a' fans out to every leaf input
            env = self._fanout(d, 2)
            data, summary = env["data"], env["summary"]
            self.assertEqual(env["status"], "ok")
            self.assertTrue(summary["truncated"])
            node_set = set(data["nodes"])
            dangling = [p for e in data["edges"]
                        for p in (e["source"], e["target"]) if p not in node_set]
            self.assertEqual(dangling, [],
                             f"edge endpoints missing from nodes[]: {dangling}")
            # counts still report the true totals, not the clipped view
            self.assertGreater(summary["edges"], len(data["edges"]))
            self.assertGreater(summary["nodes"], len(data["nodes"]))

    def test_unlimited_graph_is_complete_and_consistent(self):
        with tempfile.TemporaryDirectory() as d:
            _make_wide_design(d, 8)
            env = self._fanout(d, 0)
            data, summary = env["data"], env["summary"]
            self.assertFalse(summary["truncated"])
            node_set = set(data["nodes"])
            for e in data["edges"]:
                self.assertIn(e["source"], node_set)
                self.assertIn(e["target"], node_set)


class LintFindingsLimit(unittest.TestCase):
    def test_findings_list_capped_but_total_truthful(self):
        with tempfile.TemporaryDirectory() as d:
            mods = "\n".join(
                f"module m{i}(input wire a, output wire y);\n"
                f"  wire unused_{i};\n  assign y = a;\nendmodule" for i in range(6))
            (Path(d) / "many.sv").write_text(mods + "\n")
            proc = _run(["lint", "many.sv", "--rules", "unused",
                         "--limit", "2", "--json"], d)
            env = json.loads(proc.stdout)
            self.assertEqual(env["status"], "ok")
            self.assertEqual(len(env["data"]["findings"]), 2)   # clipped
            self.assertEqual(env["summary"]["total"], 6)        # true total
            self.assertTrue(env["summary"]["truncated"])


if __name__ == "__main__":
    unittest.main()

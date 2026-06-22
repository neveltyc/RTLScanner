"""Design-wide node lookup (``rtlscanner find``).

``find`` walks the whole elaborated design and reports every signal / instance
node whose hierarchical path matches a glob (default) or regex (``--regex``).
These tests pin the matching surface (glob vs regex, ``--kind`` / ``--scope``
filters, sibling-instance coverage) and the agent envelope (shape, counts,
structured errors).
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

try:
    import pyslang.ast as ast  # noqa: F401  (availability guard)
    from rtl_common import build_compilation, collect_filelist
    from rtl_find import FindAnalyzer
    HAVE_PYSLANG = True
except Exception:  # pragma: no cover
    HAVE_PYSLANG = False


def _compile(*relparts):
    fl = collect_filelist([str(ROOT.joinpath(*relparts))])
    comp, _ = build_compilation(fl.sources, fl.include_dirs, fl.defines)
    return comp


def _run_json(*args):
    proc = subprocess.run(RTLSCANNER + list(args) + ["--json"], cwd=ROOT,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, json.loads(proc.stdout)


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class FindAnalyzerMatching(unittest.TestCase):
    def setUp(self):
        self.fa = FindAnalyzer(_compile("examples", "basic"))

    def _paths(self, pattern, **kw):
        kw.setdefault("regex", False)
        kw.setdefault("kind", "all")
        return [n.hierarchical_path for n in self.fa.find(pattern, **kw)]

    def test_glob_segment_scoped_star(self):
        # `top.u_*` is single-segment: the three direct children, none deeper.
        self.assertEqual(
            self._paths("top.u_*", kind="instance"),
            ["top.u_dp0", "top.u_dp1", "top.u_extra_reg"])

    def test_recursive_glob_matches_all_q_signals(self):
        # Every `q` across the design, including both siblings' copies — the
        # design-wide reach a scope-local query can't give.
        got = self._paths("**.q", kind="signal")
        self.assertIn("top.u_dp0.q", got)
        self.assertIn("top.u_dp1.q", got)          # sibling instance, own path
        self.assertIn("top.u_dp0.u_reg.q", got)
        self.assertIn("top.u_extra_reg.q", got)

    def test_kind_filter_separates_signals_and_instances(self):
        sigs = self.fa.find("top.u_dp0.**", regex=False, kind="signal")
        insts = self.fa.find("top.u_dp0.**", regex=False, kind="instance")
        self.assertTrue(all(n.category == "signal" for n in sigs))
        self.assertTrue(all(n.category == "instance" for n in insts))
        # The two partitions of `--kind all`.
        alln = self.fa.find("top.u_dp0.**", regex=False, kind="all")
        self.assertEqual(len(alln), len(sigs) + len(insts))

    def test_regex_whole_path(self):
        got = [n.hierarchical_path
               for n in self.fa.find(r"top\.y[01]", regex=True, kind="signal")]
        self.assertEqual(sorted(got), ["top.y0", "top.y1"])

    def test_scope_restricts_subtree(self):
        got = self._paths("**", scope_path="top.u_dp0.u_reg")
        self.assertTrue(got)
        self.assertTrue(all(p == "top.u_dp0.u_reg" or
                            p.startswith("top.u_dp0.u_reg.") for p in got))
        self.assertNotIn("top.u_dp1.u_reg.q", got)

    def test_node_record_has_location_and_type(self):
        node = next(n for n in self.fa.find("top.u_dp0.q", regex=False, kind="all"))
        d = node.to_dict()
        self.assertEqual(d["category"], "signal")
        self.assertEqual(d["kind"], "Variable")
        self.assertTrue(d["type"].startswith("logic"))
        self.assertTrue(d["file"].endswith("top.sv"))
        self.assertEqual(d["location"]["line"], d["line"])

    def test_instance_record_reports_module(self):
        node = next(n for n in self.fa.find("top.u_dp0", regex=False,
                                            kind="instance"))
        d = node.to_dict()
        self.assertEqual(d["category"], "instance")
        self.assertEqual(d["module"], "datapath")


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class FindGenerateArray(unittest.TestCase):
    """Matching is over *elaborated* paths, so every generate-array element is
    its own node — the design-wide reach a scope-local or source query lacks."""

    def test_recursive_glob_finds_each_genblock_instance(self):
        fa = FindAnalyzer(_compile("examples", "generate"))
        got = [n.hierarchical_path
               for n in fa.find("**.u_gen_leaf", regex=False, kind="instance")]
        self.assertEqual(got, [
            "gen_top.u_mid.gen_arr[0].u_gen_leaf",
            "gen_top.u_mid.gen_arr[1].u_gen_leaf",
            "gen_top.u_mid.gen_arr[2].u_gen_leaf",
        ])


@unittest.skipUnless(HAVE_PYSLANG, "pyslang not installed")
class FindEnvelope(unittest.TestCase):
    def test_json_envelope_and_summary(self):
        rc, env = _run_json("find", "-d", "examples/basic", "-p", "**.q",
                            "--kind", "signal")
        self.assertEqual(rc, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["data"]["mode"], "find")
        self.assertFalse(env["data"]["regex"])
        self.assertEqual(env["summary"]["matches"], env["data"]["match_count"])
        self.assertEqual(env["summary"]["instances"], 0)
        self.assertGreaterEqual(env["summary"]["signals"], 4)

    def test_empty_match_is_ok_not_error(self):
        rc, env = _run_json("find", "-d", "examples/basic", "-p", "nope.nope")
        self.assertEqual(rc, 0)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["data"]["matches"], [])
        self.assertEqual(env["summary"]["matches"], 0)

    def test_missing_pattern_is_structured_error(self):
        rc, env = _run_json("find", "-d", "examples/basic")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["errors"][0]["code"], "INPUT_NOT_FOUND")
        self.assertNotEqual(rc, 0)

    def test_bad_regex_is_structured_error(self):
        rc, env = _run_json("find", "-d", "examples/basic", "--regex", "-p", "[")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["errors"][0]["code"], "INPUT_NOT_FOUND")
        self.assertIn("regex", env["errors"][0]["message"])

    def test_unknown_scope_is_scope_not_found(self):
        rc, env = _run_json("find", "-d", "examples/basic", "-p", "**",
                            "--scope", "top.nope")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["errors"][0]["code"], "SCOPE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()

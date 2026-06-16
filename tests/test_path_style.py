"""Unified path-style vocabulary across `tree --path-style` and xref.

Both accept the long and short spellings (``relative``<->``rel``,
``absolute``<->``abs``) and normalize to the long canonical form, so the same
words work for either command. The third option stays command-specific
(``tree``: ``prefix``, ``xref``: ``name``) because those are different *modes*,
not spellings.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]


class CanonPathStyle(unittest.TestCase):
    def test_short_normalizes_to_long(self):
        self.assertEqual(agent_json.canon_path_style("rel"), "relative")
        self.assertEqual(agent_json.canon_path_style("abs"), "absolute")

    def test_long_passes_through(self):
        self.assertEqual(agent_json.canon_path_style("relative"), "relative")
        self.assertEqual(agent_json.canon_path_style("absolute"), "absolute")

    def test_command_specific_modes_pass_through(self):
        self.assertEqual(agent_json.canon_path_style("prefix"), "prefix")
        self.assertEqual(agent_json.canon_path_style("name"), "name")

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(agent_json.canon_path_style("garbage"), "relative")
        self.assertEqual(
            agent_json.canon_path_style("garbage", default="absolute"),
            "absolute")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(agent_json.canon_path_style("  REL "), "relative")


def _make_design(dirpath):
    (Path(dirpath) / "m.sv").write_text(
        "module m(input wire a, output wire y);\n  assign y = a;\nendmodule\n")


def _export(dirpath, style):
    proc = subprocess.run(
        RTLSCANNER + ["tree", "-d", dirpath, "--export", "-",
                      "--path-style", style],
        text=True, cwd=dirpath,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout


class TreeExportSpellings(unittest.TestCase):
    def test_rel_equals_relative(self):
        with tempfile.TemporaryDirectory() as d:
            _make_design(d)
            self.assertEqual(_export(d, "rel"), _export(d, "relative"))

    def test_abs_equals_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            _make_design(d)
            self.assertEqual(_export(d, "abs"), _export(d, "absolute"))

    def test_prefix_mode_uses_projpath(self):
        with tempfile.TemporaryDirectory() as d:
            _make_design(d)
            out = _export(d, "prefix")
            self.assertTrue(any("${PROJPATH}/" in ln for ln in out.splitlines()))

    def test_relative_and_absolute_differ(self):
        with tempfile.TemporaryDirectory() as d:
            _make_design(d)
            self.assertNotEqual(_export(d, "relative"), _export(d, "absolute"))


class XrefSpellings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from rtl_common import build_compilation, collect_filelist
        fl = collect_filelist([str(ROOT / "examples/trace")])
        cls.comp, _ = build_compilation(fl.sources, fl.include_dirs, fl.defines)

    def _file_for(self, style):
        from rtl_xref import XrefAnalyzer
        xa = XrefAnalyzer(self.comp, root=ROOT, path_style=style)
        return xa.xref_module("mux2").definitions[0].file

    def test_rel_equals_relative(self):
        self.assertEqual(self._file_for("rel"), self._file_for("relative"))

    def test_abs_equals_absolute(self):
        self.assertEqual(self._file_for("abs"), self._file_for("absolute"))
        self.assertTrue(self._file_for("abs").startswith("/"))

    def test_rel_differs_from_abs(self):
        self.assertNotEqual(self._file_for("rel"), self._file_for("abs"))


if __name__ == "__main__":
    unittest.main()

"""`--waive` target prefixes: module: / file: / scope: (+ bare back-compat).

A bare glob keeps the backward-compatible "module OR file basename" union.
A ``module:`` prefix matches only the design unit (so it never catches a
sibling module in the same file); a ``file:`` prefix matches the source file
(and, unlike a module glob, reaches findings that have no module attribution);
``scope:`` is reserved and currently waives nothing, with a note.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

# A file NAMED after one of its modules: alpha.sv holds alpha AND beta.
ALPHA_AND_BETA = (
    "module alpha (input wire a, output wire y);\n"
    "  wire unused_a;\n  assign y = a;\nendmodule\n"
    "module beta (input wire b, output wire z);\n"
    "  wire unused_b;\n  assign z = b;\nendmodule\n"
)

# A $unit-scope error outside any module -> the finding has module == "".
SCOPELESS = (
    "logic [`W-1:0] g_bus;\n"
    "module top (input wire a, output wire y);\n  assign y = a;\nendmodule\n"
)


def _lint(d, fname, *args, rules="unused"):
    proc = subprocess.run(
        RTLSCANNER + ["lint", fname, "--rules", rules, "--json", *args],
        cwd=d, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(proc.stdout)


def _kept_modules(env):
    return sorted(f.get("module") for f in env["data"]["findings"])


def _notes(env):
    return " ".join(x["message"] for x in env["diagnostics"])


class ModulePrefix(unittest.TestCase):
    def test_module_prefix_waives_only_that_module(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alpha.sv").write_text(ALPHA_AND_BETA)
            env = _lint(d, "alpha.sv", "--waive", "module:alpha")
            self.assertEqual(_kept_modules(env), ["beta"])

    def test_module_prefix_cannot_reach_a_finding_with_no_module(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "scopeless.sv").write_text(SCOPELESS)
            base = _lint(d, "scopeless.sv", rules="semantic")
            self.assertTrue(any(f.get("module", "") == ""
                                for f in base["data"]["findings"]))
            env = _lint(d, "scopeless.sv", "--waive", "module:scopeless",
                        rules="semantic")
            self.assertEqual(env["summary"]["total"], base["summary"]["total"])


class FilePrefix(unittest.TestCase):
    def test_file_prefix_waives_whole_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alpha.sv").write_text(ALPHA_AND_BETA)
            self.assertEqual(
                _lint(d, "alpha.sv", "--waive", "file:*.sv")["summary"]["total"], 0)
            self.assertEqual(
                _lint(d, "alpha.sv", "--waive", "file:alpha")["summary"]["total"], 0)

    def test_file_prefix_reaches_findings_with_no_module(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "scopeless.sv").write_text(SCOPELESS)
            env = _lint(d, "scopeless.sv", "--waive", "file:scopeless.sv",
                        rules="semantic")
            self.assertEqual(env["summary"]["total"], 0)


class BareGlobBackCompat(unittest.TestCase):
    def test_bare_glob_still_matches_module_or_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alpha.sv").write_text(ALPHA_AND_BETA)
            # bare module name -> matches that module
            self.assertEqual(_kept_modules(
                _lint(d, "alpha.sv", "--waive", "beta")), ["alpha"])
            # bare file stem -> matches every finding in the file (union)
            self.assertEqual(
                _lint(d, "alpha.sv", "--waive", "alpha")["summary"]["total"], 0)


class ReservedAndUnknownPrefixes(unittest.TestCase):
    def test_scope_prefix_is_reserved_and_notes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alpha.sv").write_text(ALPHA_AND_BETA)
            env = _lint(d, "alpha.sv", "--waive", "scope:top.u_x")
            self.assertEqual(env["summary"]["total"], 2)   # nothing waived
            self.assertIn("scope-level waivers are not yet supported", _notes(env))

    def test_unknown_kind_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alpha.sv").write_text(ALPHA_AND_BETA)
            env = _lint(d, "alpha.sv", "--waive", "modual:alpha")
            self.assertIn("not a known target kind", _notes(env))

    def test_waived_reason_names_the_token(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alpha.sv").write_text(ALPHA_AND_BETA)
            env = _lint(d, "alpha.sv", "--waive", "module:alpha")
            reasons = [f["waived_reason"] for f in env["data"]["waived"]]
            self.assertIn("waived ('module:alpha')", reasons)


if __name__ == "__main__":
    unittest.main()

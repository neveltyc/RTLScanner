"""Per-module finding attribution and module-targeted waivers.

A file may declare several modules.  Each finding is attributed to the design
unit whose source range contains it, and ``--waive`` matches that unit (with the
file basename kept as a backward-compatible fallback) — so a glob can waive one
module without taking out the whole file.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

# alpha (lines 1-4) and beta (lines 6-9) each have one unused signal.
MULTI = (
    "module alpha (input wire a, output wire y);\n"
    "  wire unused_in_alpha;\n"
    "  assign y = a;\n"
    "endmodule\n"
    "\n"
    "module beta (input wire b, output wire z);\n"
    "  wire unused_in_beta;\n"
    "  assign z = b;\n"
    "endmodule\n"
)


def _lint(srcdir, fname, *args):
    proc = subprocess.run(
        RTLSCANNER + ["lint", fname, "--rules", "unused", "--json", *args],
        cwd=srcdir, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(proc.stdout)


class ModuleAttribution(unittest.TestCase):
    def test_findings_carry_their_module(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "multi.sv").write_text(MULTI)
            env = _lint(d, "multi.sv")
            findings = env["data"]["findings"]
            self.assertEqual(len(findings), 2)
            self.assertEqual(sorted(f["module"] for f in findings),
                             ["alpha", "beta"])
            # attribution is by source range: the alpha finding sits in alpha,
            # the beta finding in beta (not both lumped under the file name).
            by_line = {f["line"]: f["module"] for f in findings}
            self.assertEqual(by_line[2], "alpha")
            self.assertEqual(by_line[7], "beta")


class ModuleTargetedWaiver(unittest.TestCase):
    def _kept_modules(self, d, *args):
        env = _lint(d, "multi.sv", *args)
        return sorted(f.get("module") for f in env["data"]["findings"])

    def test_waive_one_module_keeps_the_other(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "multi.sv").write_text(MULTI)
            self.assertEqual(self._kept_modules(d), ["alpha", "beta"])
            self.assertEqual(self._kept_modules(d, "--waive", "alpha"), ["beta"])
            self.assertEqual(self._kept_modules(d, "--waive", "beta"), ["alpha"])

    def test_file_basename_still_waives_whole_file(self):
        # Backward compatibility: a glob matching the file basename waives every
        # finding in the file, regardless of module.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "multi.sv").write_text(MULTI)
            self.assertEqual(self._kept_modules(d, "--waive", "multi"), [])

    def test_one_module_per_file_glob(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alpha.sv").write_text(
                "module alpha (input wire a, output wire y);\n"
                "  wire unused;\n  assign y = a;\nendmodule\n")
            env = _lint(d, "alpha.sv", "--waive", "alpha")
            self.assertEqual(env["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()

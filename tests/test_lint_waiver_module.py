"""Per-module finding attribution.

A file may declare several modules.  Each finding is attributed to the design
unit whose source range contains it (not lumped under the file name), so an
agent can filter the JSON by the ``module`` field.
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

    def test_single_module_file_attributed_by_module_not_filename(self):
        # A one-module-per-file unit parses with that unit AS the syntax-tree
        # root; attribution must still find it even when the file name differs.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "widget_impl.sv").write_text(
                "module widget (input wire a, output wire y);\n"
                "  wire unused_w;\n  assign y = a;\nendmodule\n")
            env = _lint(d, "widget_impl.sv")
            self.assertEqual(env["data"]["findings"][0]["module"], "widget")


if __name__ == "__main__":
    unittest.main()

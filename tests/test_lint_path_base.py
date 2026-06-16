"""lint and xref report a file with the same path.

lint used to relativize against the process CWD while xref relativized against
the configured ``[inputs].root``; running from a directory other than the root
made the two commands disagree on the very same file.  Both now key off the
resolved input root.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

FOO = (
    "module foo (input wire a, output wire y);\n"
    "  wire unused_sig;\n"
    "  assign y = a;\n"
    "endmodule\n"
)


class LintXrefPathBase(unittest.TestCase):
    def test_lint_and_xref_agree_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            proj = tmp / "proj"
            rtl = proj / "rtl"
            rtl.mkdir(parents=True)
            (rtl / "foo.sv").write_text(FOO)
            # Absolute root in config resolves to <proj>, independent of CWD.
            cfg = tmp / "cfg.toml"
            cfg.write_text(f'[inputs]\nroot = "{proj.as_posix()}"\n')
            src = str(rtl / "foo.sv")
            # Run from <tmp>, which is NOT the root: pre-fix, lint would report
            # "proj/rtl/foo.sv" (relative to CWD) while xref reports
            # "rtl/foo.sv" (relative to root).
            lint = subprocess.run(
                RTLSCANNER + ["lint", src, "--config", str(cfg),
                              "--rules", "unused", "--json"],
                cwd=str(tmp), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            xref = subprocess.run(
                RTLSCANNER + ["xref", src, "--module", "foo",
                              "--config", str(cfg), "--json"],
                cwd=str(tmp), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            le = json.loads(lint.stdout)
            xe = json.loads(xref.stdout)
            lint_file = le["data"]["findings"][0]["file"]
            xref_file = xe["data"]["definitions"][0]["file"]
            self.assertEqual(lint_file, "./rtl/foo.sv")
            self.assertEqual(lint_file, xref_file)


if __name__ == "__main__":
    unittest.main()

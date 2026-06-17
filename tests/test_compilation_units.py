"""Per-file compilation-unit isolation.

Each source file in a file list is compiled as its own compilation unit, the
way slang's own driver (and VCS/Verilator) treat a file list.  A `define in one
file must NOT leak into the next; command-line +define+ macros, by contrast,
are global predefines visible to every file.

Regression guard for the bug where every file was concatenated into a single
synthetic `include buffer: a `define in file A leaked into file B and could
mask a real "unknown macro" error, so a design with a genuine missing-define
bug linted clean.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
import tempfile


RTLSCANNER = [sys.executable, "-m", "rtlscanner"]


def run_lint(cwd, *args, expect_status="ok"):
    """Run `lint ... --rules semantic --json` in *cwd*; return the envelope."""
    proc = subprocess.run(
        RTLSCANNER + ["lint", *args, "--rules", "semantic", "--json"],
        cwd=str(cwd), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    env = json.loads(proc.stdout)
    assert env["status"] == expect_status, (
        f"expected status={expect_status}, got {env['status']}: "
        f"{env.get('errors')}")
    return env


def _errors_in(env, basename):
    """Error-severity findings whose file is *basename*."""
    return [f for f in env["data"]["findings"]
            if f["severity"] == "error" and Path(f["file"]).name == basename]


class CompilationUnitIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "rtl_cu_test"
        self.tmp.mkdir(parents=True)
        # a.sv defines SECRET_WIDTH and uses it; b.sv uses it but never
        # defines it.  Under separate compilation units, b.sv must error.
        (self.tmp / "a.sv").write_text(
            "`define SECRET_WIDTH 8\n"
            "module a (input  logic [`SECRET_WIDTH-1:0] x,\n"
            "          output logic [`SECRET_WIDTH-1:0] y);\n"
            "  assign y = x;\n"
            "endmodule\n"
        )
        (self.tmp / "b.sv").write_text(
            "module b (input  logic [`SECRET_WIDTH-1:0] p,\n"
            "          output logic [`SECRET_WIDTH-1:0] q);\n"
            "  assign q = p;\n"
            "endmodule\n"
        )
        # Both files, a.sv first so its `define would leak forward if the
        # compilation merged units.
        (self.tmp / "files.f").write_text("a.sv\nb.sv\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_macro_does_not_leak_across_files(self):
        """A `define in a.sv must not satisfy `SECRET_WIDTH in b.sv."""
        env = run_lint(self.tmp, "-f", "files.f")
        b_errs = _errors_in(env, "b.sv")
        self.assertTrue(
            b_errs,
            "expected an 'unknown macro' error in b.sv (macro leaked across "
            "compilation units)")
        self.assertTrue(
            any("SECRET_WIDTH" in f["message"] for f in b_errs),
            f"expected the b.sv error to name `SECRET_WIDTH; got {b_errs}")
        # a.sv, which legitimately defines and uses the macro, stays clean.
        self.assertEqual(_errors_in(env, "a.sv"), [])

    def test_macro_visible_within_its_own_file(self):
        """The fix must not over-correct: a.sv alone still resolves its macro."""
        (self.tmp / "only_a.f").write_text("a.sv\n")
        env = run_lint(self.tmp, "-f", "only_a.f")
        self.assertEqual(_errors_in(env, "a.sv"), [])

    def test_command_line_define_reaches_every_file(self):
        """+define+ is a global predefine: it satisfies `SECRET_WIDTH in b.sv."""
        # b.sv on its own, with the macro supplied only on the command line.
        (self.tmp / "only_b.f").write_text("+define+SECRET_WIDTH=4\nb.sv\n")
        env = run_lint(self.tmp, "-f", "only_b.f")
        self.assertEqual(
            _errors_in(env, "b.sv"), [],
            "a +define+ on the command line should make `SECRET_WIDTH visible")

    def test_cross_file_package_and_instance_still_resolve(self):
        """Separate units must not break global package/module visibility."""
        (self.tmp / "p_pkg.sv").write_text(
            "package p_pkg;\n"
            "  localparam int W = 8;\n"
            "endpackage\n"
        )
        (self.tmp / "leaf.sv").write_text(
            "module leaf (input logic [7:0] d, output logic [7:0] z);\n"
            "  assign z = d;\n"
            "endmodule\n"
        )
        (self.tmp / "top.sv").write_text(
            "module top (input logic [7:0] a, output logic [7:0] b);\n"
            "  import p_pkg::*;\n"
            "  logic [W-1:0] mid;\n"
            "  leaf u_leaf (.d(a), .z(mid));\n"
            "  assign b = mid;\n"
            "endmodule\n"
        )
        (self.tmp / "design.f").write_text("p_pkg.sv\nleaf.sv\ntop.sv\n")
        env = run_lint(self.tmp, "-f", "design.f")
        errs = [f for f in env["data"]["findings"] if f["severity"] == "error"]
        self.assertEqual(
            errs, [],
            f"cross-file package import / instantiation should resolve; got {errs}")

    def test_single_unit_lets_macro_leak(self):
        """--single-unit restores legacy behavior: a.sv's `define reaches b.sv.

        The inverse of test_macro_does_not_leak_across_files: with the escape
        hatch the whole file list is one compilation unit again, so the macro
        defined in a.sv satisfies `SECRET_WIDTH in b.sv and the design lints
        clean -- proving the flag genuinely toggles the compilation-unit model.
        """
        env = run_lint(self.tmp, "-f", "files.f", "--single-unit")
        self.assertEqual(
            _errors_in(env, "b.sv"), [],
            "with --single-unit, a.sv's `define should leak into b.sv")
        self.assertEqual(_errors_in(env, "a.sv"), [])


if __name__ == "__main__":
    unittest.main()

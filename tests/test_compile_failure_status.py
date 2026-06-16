"""Structural commands must report COMPILE_FAILED on a design that won't compile.

v0.2.0's per-file compilation-unit change activated a latent bug: tree/scope/
xref (and trace/fanin/fanout) built their output from slang's error-recovery
AST and returned ``status="ok"`` with a phantom structure (and, for tree, a
``diagnostics`` entry that was a raw ``<...Diagnostic object at 0x...>`` repr
hardcoded to severity ``"warning"``).  These tests pin the corrected contract:

  * a design that fails to compile -> ``status="error"``, ``errors[0].code ==
    "COMPILE_FAILED"``, ``data == null``, and real error diagnostics with the
    right severity / message / location;
  * ``--single-unit`` restores the legacy single-compilation-unit behavior so a
    ``$unit``-scoped typedef shared across files compiles clean again;
  * a clean or warning-only design still succeeds (warnings never flip status).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

# (subcommand, extra args needed to reach the compile step).  xref needs a
# target; the flow commands need a signal -- but all of them fail at compile
# before the signal/scope is ever resolved.
STRUCTURAL = [
    ("tree",   []),
    ("scope",  []),
    ("xref",   ["-s", "byte_t"]),
    ("trace",  ["-s", "mid"]),
    ("fanin",  ["-s", "mid"]),
    ("fanout", ["-s", "mid"]),
]


def run(cwd, cmd, *args):
    """Run ``<cmd> ... --json`` in *cwd*; return (envelope, returncode)."""
    proc = subprocess.run(
        RTLSCANNER + [cmd, *args, "--json"],
        cwd=str(cwd), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return json.loads(proc.stdout), proc.returncode


class CompileFailureStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "rtl_cf"
        self.tmp.mkdir(parents=True)
        # 00_defs.sv declares a $unit-scoped typedef AND a real module (so the
        # classifier keeps it as a compiled source, not an include header).
        # Under per-file units, byte_t does NOT reach top.sv -> undeclared id.
        (self.tmp / "00_defs.sv").write_text(
            "typedef logic [7:0] byte_t;\n"
            "module leaf (input byte_t d, output byte_t q);\n"
            "  assign q = d;\n"
            "endmodule\n"
        )
        (self.tmp / "top.sv").write_text(
            "module top (input logic [7:0] a, output byte_t b);\n"
            "  byte_t mid;\n"
            "  leaf u_leaf (.d(a), .q(mid));\n"
            "  assign b = mid;\n"
            "endmodule\n"
        )
        (self.tmp / "files.f").write_text("00_defs.sv\ntop.sv\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_structural_commands_report_compile_failed(self):
        """Every structural command -> status=error + COMPILE_FAILED, no phantom."""
        for cmd, extra in STRUCTURAL:
            with self.subTest(cmd=cmd):
                env, rc = run(self.tmp, cmd, "-f", "files.f", *extra)
                self.assertEqual(env["status"], "error",
                                 f"{cmd}: expected status=error, got {env}")
                self.assertTrue(env["errors"], f"{cmd}: errors[] empty")
                self.assertEqual(env["errors"][0]["code"], "COMPILE_FAILED",
                                 f"{cmd}: {env['errors']}")
                self.assertIsNone(env["data"],
                                  f"{cmd}: phantom data should be null")
                self.assertNotEqual(rc, 0, f"{cmd}: exit code should be nonzero")

    def test_diagnostics_are_formatted_not_object_reprs(self):
        """Diagnostics carry real severity/message/location, not str(Diagnostic)."""
        for cmd, extra in STRUCTURAL:
            with self.subTest(cmd=cmd):
                env, _ = run(self.tmp, cmd, "-f", "files.f", *extra)
                diags = env["diagnostics"]
                self.assertTrue(diags, f"{cmd}: diagnostics[] empty")
                for d in diags:
                    # regression: never a raw "<pyslang...object at 0x...>" repr
                    self.assertNotRegex(
                        d["message"], r"<.*object at 0x",
                        f"{cmd}: diagnostic is a raw object repr: {d}")
                errs = [d for d in diags if d["severity"] == "error"]
                # regression: severity must not be hardcoded to "warning"
                self.assertTrue(errs, f"{cmd}: no error-severity diagnostic: {diags}")
                self.assertTrue(
                    any("byte_t" in d["message"] for d in errs),
                    f"{cmd}: expected a 'byte_t' error; got {errs}")
                # regression: location must be preserved (was file='' line=0 col=0)
                self.assertTrue(
                    any(d["line"] > 0 and d["file"] for d in errs),
                    f"{cmd}: error diagnostics lost their location: {errs}")

    def test_single_unit_restores_cross_file_unit_scope(self):
        """--single-unit: byte_t declared in 00_defs.sv reaches top.sv again."""
        env, rc = run(self.tmp, "tree", "-f", "files.f", "--single-unit")
        self.assertEqual(
            env["status"], "ok",
            f"--single-unit should compile clean; got {env['errors']}")
        self.assertEqual(env["errors"], [])
        self.assertEqual(rc, 0)
        modules = {n["module"] for n in env["data"]["hierarchy"]}
        self.assertIn("top", modules)

    def test_clean_design_succeeds(self):
        """No false COMPILE_FAILED on a design that compiles cleanly."""
        (self.tmp / "clean.sv").write_text(
            "module clean (input logic x, output logic y);\n"
            "  assign y = x;\n"
            "endmodule\n"
        )
        env, rc = run(self.tmp, "tree", "clean.sv")
        self.assertEqual(env["status"], "ok", env.get("errors"))
        self.assertEqual(env["diagnostics"], [])
        self.assertEqual(rc, 0)

    def test_warnings_do_not_flip_status(self):
        """A design with only warnings stays status=ok, warning surfaced."""
        # 4-bit -> 8-bit assign emits a default-on slang width warning.
        (self.tmp / "warn.sv").write_text(
            "module warn (input logic [3:0] a, output logic [7:0] y);\n"
            "  assign y = a;\n"
            "endmodule\n"
        )
        env, rc = run(self.tmp, "tree", "warn.sv")
        self.assertEqual(env["status"], "ok", env.get("errors"))
        self.assertEqual(rc, 0)
        self.assertTrue(
            any(d["severity"] == "warning" for d in env["diagnostics"]),
            f"expected a warning diagnostic; got {env['diagnostics']}")
        self.assertFalse(
            any(d["severity"] == "error" for d in env["diagnostics"]),
            "a warning must not be reported as an error")


if __name__ == "__main__":
    unittest.main()

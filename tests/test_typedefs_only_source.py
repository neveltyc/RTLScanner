"""A user-listed source carrying only ``$unit``-scoped typedefs must compile.

The file classifier used to decide a ``.v``/``.sv`` was a compilation *source*
(vs. an include header) by sniffing it for a top-level declaration --
``module`` / ``interface`` / ``package`` / ``program`` / ``primitive``.  A file
holding nothing but a ``$unit`` ``typedef`` matched none of those, so it was
demoted to an include directory and **silently dropped from the source list**:
every later file that used the type then failed with "undeclared identifier",
and -- worst of all -- ``--single-unit`` was defeated for exactly the files it
was added to serve (a leading typedefs-only file sharing its ``$unit`` scope).

The fix: a file the user *names directly* -- listed in a filelist or passed as a
path argument -- is always a source, whatever its contents.  The
top-level-declaration heuristic survives only for directory auto-discovery,
where it still skips include-style ``.sv`` snippets.  These tests pin both
halves: the explicit-source promotion AND the preserved discovery heuristic, plus
the end-to-end ``--single-unit`` behavior the bug actually broke.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_common import classify_hdl_file, collect_filelist, parse_filelist

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

# A file with no module/interface/package/program/primitive -- only a
# $unit-scoped typedef.  The pre-fix classifier called this an include header.
TYPEDEFS_ONLY = "typedef logic [7:0] byte_t;\n"
# A consumer that needs byte_t from the $unit scope of an earlier file.
TOP = (
    "module top (input logic [7:0] a, output byte_t b);\n"
    "  byte_t mid;\n"
    "  assign mid = a;\n"
    "  assign b = mid;\n"
    "endmodule\n"
)


def run(cwd, cmd, *args):
    """Run ``<cmd> ... --json`` in *cwd*; return (envelope, returncode)."""
    proc = subprocess.run(
        RTLSCANNER + [cmd, *args, "--json"],
        cwd=str(cwd), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return json.loads(proc.stdout), proc.returncode


class ClassificationTests(unittest.TestCase):
    """Unit-level: a typedefs-only file is a source iff the user named it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "rtl_types"
        self.tmp.mkdir(parents=True)
        self.types = self.tmp / "00_types.sv"
        self.types.write_text(TYPEDEFS_ONLY)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explicit_typedefs_only_is_source(self):
        """Explicitly named -> source, despite no top-level declaration."""
        self.assertEqual(classify_hdl_file(str(self.types), explicit=True), "source")

    def test_discovered_typedefs_only_stays_include(self):
        """The auto-discovery heuristic is preserved: non-explicit -> include."""
        self.assertFalse(classify_hdl_file(str(self.types)).startswith("source"))
        self.assertEqual(classify_hdl_file(str(self.types)), "include")

    def test_explicit_does_not_promote_include_suffix(self):
        """An explicit .svh is still an include -- suffix wins over the flag."""
        svh = self.tmp / "defs.svh"
        svh.write_text(TYPEDEFS_ONLY)
        self.assertEqual(classify_hdl_file(str(svh), explicit=True), "include")

    def test_parse_filelist_keeps_typedefs_only_in_sources(self):
        """A filelist entry is user-explicit -> the typedefs file is a source."""
        (self.tmp / "top.sv").write_text(TOP)
        (self.tmp / "files.f").write_text("00_types.sv\ntop.sv\n")
        fl = parse_filelist("files.f", self.tmp)
        names = [Path(s).name for s in fl.sources]
        self.assertIn("00_types.sv", names,
                      f"typedefs-only file dropped from sources: {names}")
        self.assertEqual(names, ["00_types.sv", "top.sv"])

    def test_collect_filelist_path_arg_is_source_but_dir_scan_is_not(self):
        """A path argument is explicit (source); a directory scan applies the heuristic."""
        as_path = collect_filelist([str(self.types)])
        self.assertEqual([Path(s).name for s in as_path.sources], ["00_types.sv"])

        as_dir = collect_filelist([str(self.tmp)])
        self.assertEqual(as_dir.sources, [],
                         "directory auto-discovery should still demote a "
                         "typedefs-only .sv to an include")
        self.assertIn(str(self.tmp.resolve()), as_dir.include_dirs)


class SingleUnitEndToEndTests(unittest.TestCase):
    """The regression the bug report filed: --single-unit on a bare typedef file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "rtl_types_e2e"
        self.tmp.mkdir(parents=True)
        # A bare typedef file FIRST, then a consumer -- the exact shape that
        # --single-unit exists to handle, and the exact shape the classifier
        # used to silently drop.
        (self.tmp / "00_types.sv").write_text(TYPEDEFS_ONLY)
        (self.tmp / "top.sv").write_text(TOP)
        (self.tmp / "files.f").write_text("00_types.sv\ntop.sv\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_unit_shares_unit_typedef_from_bare_file(self):
        """--single-unit: byte_t from 00_types.sv reaches top.sv -> clean compile."""
        env, rc = run(self.tmp, "tree", "-f", "files.f", "--single-unit")
        self.assertEqual(env["status"], "ok",
                         f"--single-unit should compile clean; got {env.get('errors')}")
        self.assertEqual(env["errors"], [])
        self.assertEqual(rc, 0)
        self.assertIn("top", {n["module"] for n in env["data"]["hierarchy"]})

    def test_default_still_isolates_units(self):
        """Per-file default is unchanged: byte_t does NOT leak into top.sv.

        The fix only stops the file from being *dropped*; it must not turn the
        per-file default into single-unit.  top.sv still cannot see byte_t.
        """
        env, rc = run(self.tmp, "tree", "-f", "files.f")
        self.assertEqual(env["status"], "error")
        self.assertTrue(env["errors"])
        self.assertEqual(env["errors"][0]["code"], "COMPILE_FAILED")
        self.assertNotEqual(rc, 0)
        self.assertTrue(
            any("byte_t" in d["message"] for d in env["diagnostics"]
                if d["severity"] == "error"),
            f"expected an undeclared 'byte_t' error; got {env['diagnostics']}")


if __name__ == "__main__":
    unittest.main()

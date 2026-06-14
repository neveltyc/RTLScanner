"""`lint --rules bugs` preset and the unknown-rule diagnostic.

`bugs` is a curated, high-precision view: it keeps the rules that flag real
functional defects (inferred latches, undriven/unassigned values, port width
problems, implicit truncation) plus every hard compile error, and drops style
noise (unused-*, case-default, empty-output-connection, the port-unconnected
note).  cdc-crossing is excluded by default and composes back via `bugs,cdc`.

The unknown-rule diagnostic catches a typo'd `--rules`/`--skip` token (e.g.
`bugz`) that would otherwise select zero findings silently — without flagging a
real rule that simply has no findings this run.
"""

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]


def run_lint(*args, expect_status="ok"):
    """Run `lint ... --json`; return (returncode, envelope).  lint keeps a
    non-zero exit on real findings, so the caller checks returncode, not us."""
    proc = subprocess.run(
        RTLSCANNER + ["lint"] + list(args) + ["--json"],
        cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    env = json.loads(proc.stdout)
    assert env["status"] == expect_status, (
        f"expected status={expect_status}, got {env['status']}: {env.get('errors')}")
    return proc.returncode, env


def kept_rules(env):
    return {f["rule"] for f in env["data"]["findings"]}


class BugsPresetTests(unittest.TestCase):
    def test_help_documents_bugs(self):
        text = subprocess.run(
            RTLSCANNER + ["lint", "--help"], cwd=ROOT, text=True,
            stdout=subprocess.PIPE).stdout
        self.assertIn("bugs", text)

    def test_bugs_keeps_real_bugs_drops_style(self):
        _, env = run_lint("-d", "examples/lint", "--rules", "bugs")
        rules = kept_rules(env)
        # real bugs kept
        self.assertIn("inferred-latch", rules)
        self.assertIn("unassigned-variable", rules)
        self.assertIn("width-trunc", rules)
        # style noise dropped
        self.assertNotIn("unused-port", rules)
        self.assertNotIn("case-default", rules)
        self.assertNotIn("empty-output-connection", rules)

    def test_bugs_runs_unused_pass(self):
        # inferred-latch / unassigned-variable come from the CheckUnused pass
        # but lack the `unused-` prefix; this guards the _RULE_RUN_FAMILY wiring
        # that makes the pass run for them.
        _, env = run_lint("-d", "examples/lint", "--rules", "bugs")
        self.assertTrue({"inferred-latch", "unassigned-variable"} <= kept_rules(env))

    def test_bugs_keeps_port_defects(self):
        _, env = run_lint("-d", "examples/ports", "--rules", "bugs")
        rules = kept_rules(env)
        self.assertIn("port-width-mismatch", rules)
        self.assertIn("undriven-port", rules)
        self.assertNotIn("unused-port", rules)
        self.assertNotIn("port-unconnected", rules)  # note-level, not a bug

    def test_bugs_keeps_compile_errors(self):
        src = Path(tempfile.mkdtemp()) / "bad.sv"
        src.write_text("module m(output logic y); assign y = nonexistent; endmodule\n")
        rc, env = run_lint(str(src), "--rules", "bugs")
        rules = {(f["severity"], f["rule"]) for f in env["data"]["findings"]}
        self.assertIn(("error", "UndeclaredIdentifier"), rules)
        self.assertTrue(env["summary"]["has_error"])
        self.assertEqual(rc, 1)            # real error -> non-zero exit

    def test_bugs_excludes_cdc_but_composes(self):
        _, only_bugs = run_lint("-d", "examples/lint", "--rules", "bugs")
        self.assertNotIn("cdc-crossing", kept_rules(only_bugs))
        _, with_cdc = run_lint("-d", "examples/lint", "--rules", "bugs,cdc")
        self.assertIn("cdc-crossing", kept_rules(with_cdc))


class UnknownRuleNoteTests(unittest.TestCase):
    def test_typo_rule_emits_note(self):
        _, env = run_lint("-d", "examples/lint", "--rules", "bugz")
        msgs = [d["message"] for d in env["diagnostics"]]
        self.assertTrue(any("bugz" in m and "did you mean" in m and "bugs" in m
                            for m in msgs),
                        f"expected did-you-mean note, got {msgs}")

    def test_typo_skip_emits_note(self):
        _, env = run_lint("-d", "examples/lint", "--skip", "widht-trunc")
        msgs = [d["message"] for d in env["diagnostics"]]
        self.assertTrue(any("widht-trunc" in m for m in msgs),
                        f"expected --skip typo note, got {msgs}")

    def test_real_rule_zero_findings_no_note(self):
        # A valid rule that simply has no findings must NOT be flagged.
        _, env = run_lint("-d", "examples/basic", "--rules", "inferred-latch")
        msgs = [d["message"] for d in env["diagnostics"]]
        self.assertFalse(any("did you mean" in m for m in msgs),
                         f"valid rule should not be flagged, got {msgs}")
        self.assertEqual(len(env["data"]["findings"]), 0)

    def test_family_and_meta_not_flagged(self):
        _, env = run_lint("-d", "examples/lint", "--rules", "semantic")
        msgs = [d["message"] for d in env["diagnostics"]]
        self.assertFalse(any("not a known rule" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()

"""Tests for the three-tier input resolution (CLI > env > config) and
the filelist-overrides-dir rule.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]


def run(*args, cwd=None, env=None, check=True):
    e = dict(os.environ)
    if env:
        e.update({k: ("" if v is None else str(v)) for k, v in env.items()})
    proc = subprocess.run(
        RTLSCANNER + list(args),
        cwd=str(cwd or ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=e,
        check=False,
    )
    if check and proc.returncode not in (0, 1):
        raise AssertionError(
            f"rtlscanner exited {proc.returncode}\nstderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        )
    return proc


def run_json(*args, **kw):
    proc = run(*args, "--json", **kw)
    return json.loads(proc.stdout), proc.stderr, proc.returncode


class InputsResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/rtl_test_inputs")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir()
        shutil.copytree(ROOT / "examples", self.tmp / "examples")

    def tearDown(self):
        if self.tmp.exists():
            shutil.rmtree(self.tmp)

    def _clean_env(self):
        return {
            "RTLSCANNER_FILELIST": "",
            "RTLSCANNER_DIR": "",
            "RTLSCANNER_EXCLUDE": "",
            "RTLSCANNER_ROOT": "",
            "RTLSCANNER_PREFIX": "",
        }

    def test_config_inputs_drive_tree(self):
        (self.tmp / ".rtlscanner.toml").write_text(
            '[inputs]\ndir = ["examples/basic"]\n'
        )
        env, _, rc = run_json("tree", cwd=self.tmp, env=self._clean_env())
        self.assertEqual(rc, 0)
        self.assertEqual(env["status"], "ok")
        self.assertTrue(env["data"]["hierarchy"])

    def test_env_overrides_config(self):
        (self.tmp / ".rtlscanner.toml").write_text(
            '[inputs]\ndir = ["examples/basic"]\n'
        )
        env, _, rc = run_json(
            "signals", "--scope", "lint_demo",
            cwd=self.tmp,
            env={**self._clean_env(), "RTLSCANNER_DIR": "examples/lint"},
        )
        self.assertEqual(rc, 0)
        # signals fall under lint_demo module ⇒ env-dir worked
        self.assertGreater(len(env["data"]["signals"]), 0)

    def test_cli_overrides_env(self):
        env, _, rc = run_json(
            "tree", "-d", "examples/basic",
            cwd=self.tmp,
            env={**self._clean_env(), "RTLSCANNER_DIR": "examples/lint"},
        )
        self.assertEqual(rc, 0)
        # examples/basic has 'top' top module
        modules = {n["module"] for n in env["data"]["hierarchy"]}
        self.assertIn("top", modules)

    def test_filelist_wins_over_dir(self):
        (self.tmp / "basic.f").write_text("examples/basic/top.sv\n")
        proc = run(
            "tree", "-f", "basic.f", "-d", "examples/lint",
            cwd=self.tmp, env=self._clean_env(),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("using filelist", proc.stderr)
        self.assertIn("top", proc.stdout)
        # 'lint_demo' from examples/lint must NOT be in hierarchy
        self.assertNotIn("lint_demo", proc.stdout)

    def test_legacy_rtllint_toml_detected(self):
        (self.tmp / ".rtllint.toml").write_text("# legacy\n")
        proc = run("lint", "-d", "examples/lint",
                   cwd=self.tmp, env=self._clean_env())
        # Returns 0 or 1 (findings ok), but must emit migration note
        self.assertIn("found .rtllint.toml", proc.stderr)

    def test_no_config_no_env_no_args_errors(self):
        proc = run("tree", cwd=self.tmp, env=self._clean_env(), check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no .v/.sv source files found", proc.stderr + proc.stdout)


class CommaListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/rtl_test_commalist")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir()
        shutil.copytree(ROOT / "examples", self.tmp / "examples")

    def tearDown(self):
        if self.tmp.exists():
            shutil.rmtree(self.tmp)

    def _no_env(self):
        return {k: "" for k in (
            "RTLSCANNER_FILELIST", "RTLSCANNER_DIR", "RTLSCANNER_EXCLUDE",
            "RTLSCANNER_ROOT", "RTLSCANNER_PREFIX",
        )}

    def test_dir_comma_equals_repeats(self):
        env_a, _, _ = run_json("tree", "-d", "examples/basic,examples/lint",
                               cwd=self.tmp, env=self._no_env())
        env_b, _, _ = run_json("tree", "-d", "examples/basic",
                               "-d", "examples/lint",
                               cwd=self.tmp, env=self._no_env())
        self.assertEqual(env_a["command"]["dir"], env_b["command"]["dir"])

    def test_rules_bracket_syntax(self):
        env_a, _, _ = run_json("lint", "-d", "examples/lint",
                               "--rules", "width-trunc,case-default",
                               cwd=self.tmp, env=self._no_env())
        env_b, _, _ = run_json("lint", "-d", "examples/lint",
                               "--rules", "[width-trunc,case-default]",
                               cwd=self.tmp, env=self._no_env())
        env_c, _, _ = run_json("lint", "-d", "examples/lint",
                               "--rules", "{width-trunc,case-default}",
                               cwd=self.tmp, env=self._no_env())
        rules_a = sorted(f["rule"] for f in env_a["data"]["findings"])
        rules_b = sorted(f["rule"] for f in env_b["data"]["findings"])
        rules_c = sorted(f["rule"] for f in env_c["data"]["findings"])
        self.assertEqual(rules_a, rules_b)
        self.assertEqual(rules_a, rules_c)


class LintRuleModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/rtl_test_lint")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir()
        shutil.copytree(ROOT / "examples", self.tmp / "examples")

    def tearDown(self):
        if self.tmp.exists():
            shutil.rmtree(self.tmp)

    def _no_env(self):
        return {k: "" for k in (
            "RTLSCANNER_FILELIST", "RTLSCANNER_DIR", "RTLSCANNER_EXCLUDE",
            "RTLSCANNER_ROOT", "RTLSCANNER_PREFIX",
        )}

    def test_rules_none_suppresses_all(self):
        env, _, _ = run_json("lint", "-d", "examples/lint", "--rules", "none",
                             cwd=self.tmp, env=self._no_env())
        self.assertEqual(env["data"]["findings"], [])

    def test_explicit_rule_keeps_only_that_rule(self):
        env, _, _ = run_json("lint", "-d", "examples/lint",
                             "--rules", "width-trunc",
                             cwd=self.tmp, env=self._no_env())
        rules = {f["rule"] for f in env["data"]["findings"]}
        self.assertEqual(rules, {"width-trunc"})

    def test_skip_subtracts(self):
        env, _, _ = run_json("lint", "-d", "examples/lint",
                             "--rules", "default",
                             "--skip", "case-default",
                             cwd=self.tmp, env=self._no_env())
        rules = {f["rule"] for f in env["data"]["findings"]}
        self.assertNotIn("case-default", rules)

    def test_waive_module_glob(self):
        env, _, _ = run_json("lint", "-d", "examples/lint",
                             "--rules", "default",
                             "--waive", "lint_demo",
                             cwd=self.tmp, env=self._no_env())
        # Every finding should be in the waived bucket (their file is lint_demo.sv)
        self.assertEqual(env["data"]["findings"], [])
        self.assertGreater(len(env["data"]["waived"]), 0)

    def test_strict_promotes_warnings_and_exits_nonzero(self):
        proc = run("lint", "-d", "examples/lint", "--rules", "default", "--strict",
                   cwd=self.tmp, env=self._no_env(), check=False)
        self.assertEqual(proc.returncode, 1)

    def test_rules_everything_keeps_semantic_findings(self):
        env, _, _ = run_json("lint", "-d", "examples/lint",
                             "--rules", "everything",
                             cwd=self.tmp, env=self._no_env())
        checks = {f["check"] for f in env["data"]["findings"]}
        self.assertEqual(checks, {"semantic"})
        self.assertGreater(len(env["data"]["findings"]), 0)

    def test_rules_all_keeps_every_real_family_without_everything_family(self):
        env, _, _ = run_json("lint", "-d", "examples/lint",
                             "--rules", "all",
                             cwd=self.tmp, env=self._no_env())
        checks = {f["check"] for f in env["data"]["findings"]}
        self.assertIn("semantic", checks)
        self.assertIn("unused", checks)
        self.assertIn("cdc", checks)
        self.assertNotIn("everything", checks)

    def test_semantic_includes_frontend_diagnostics(self):
        bad = self.tmp / "bad_include.v"
        bad.write_text('`include "missing_defs.vh"\nmodule bad_include; endmodule\n')
        env, _, rc = run_json("lint", str(bad), "--rules", "semantic",
                             cwd=self.tmp, env=self._no_env())
        self.assertEqual(rc, 1)
        rules = {f["rule"] for f in env["data"]["findings"]}
        self.assertIn("CouldNotOpenIncludeFile", rules)


if __name__ == "__main__":
    unittest.main()

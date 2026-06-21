"""End-to-end tests for the `batch` subcommand.

Mirrors the subprocess pattern of the other CLI tests (run `python -m rtlscanner`
against the `examples/basic` fixture), piping query lines on stdin.  Covers the
RWaveAnalyzer-derived contract: a batch `result` is identical to the equivalent
single command, failures are isolated (exit 0), and only an unloadable design is
fatal (exit non-zero).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]
BASIC = ["-d", "examples/basic"]


def run_batch(stdin, *args):
    """Run `rtlscanner batch <args>` with *stdin* piped; return CompletedProcess."""
    return subprocess.run(
        RTLSCANNER + ["batch", *args],
        cwd=ROOT, text=True, input=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def run_single(*args):
    """Run a single subcommand with --json and return its parsed envelope."""
    p = subprocess.run(
        RTLSCANNER + list(args) + ["--json"],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(p.stdout)


def frames(stdout):
    """Parse JSONL batch output into a list of frame dicts."""
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


class BatchTests(unittest.TestCase):

    # ── Fidelity: a batch result == the equivalent single command ──
    def test_result_matches_single_command(self):
        queries = ("scope --scope top.u_dp0\n"
                   "trace -s q --scope top.u_dp0\n")
        out = run_batch(queries, *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertEqual(len(got), 2)

        self.assertTrue(got[0]["ok"])
        self.assertEqual(
            got[0]["result"],
            run_single("scope", *BASIC, "--scope", "top.u_dp0"))
        self.assertTrue(got[1]["ok"])
        self.assertEqual(
            got[1]["result"],
            run_single("trace", *BASIC, "-s", "q", "--scope", "top.u_dp0"))

    def test_multiple_mixed_queries_in_order(self):
        queries = ("tree\n"
                   "scope --scope top.u_dp0\n"
                   "fanin -s q --scope top.u_dp0\n"
                   "xref -s q --scope top.u_dp0\n"
                   "lint\n")
        out = run_batch(queries, *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertEqual([f["id"] for f in got], ["1", "2", "3", "4", "5"])
        self.assertTrue(all(f["ok"] for f in got))
        self.assertEqual([f["result"]["tool"] for f in got],
                         ["tree", "scope", "fanin", "xref", "lint"])

    # ── A failing query is isolated; the batch still exits 0 ──
    def test_error_isolation_exits_zero(self):
        queries = ("scope --scope top.u_dp0\n"
                   "trace -s does_not_exist --scope top.u_dp0\n"
                   "tree\n")
        out = run_batch(queries, *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertEqual(len(got), 3)
        self.assertTrue(got[0]["ok"])
        self.assertFalse(got[1]["ok"])
        self.assertIn("error", got[1])
        self.assertIn("not found", got[1]["error"])
        self.assertTrue(got[2]["ok"])           # batch kept going after the failure

    def test_unknown_command_isolated(self):
        out = run_batch("bogus --foo\ntree\n", *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertFalse(got[0]["ok"])
        self.assertIn("unknown command", got[0]["error"])
        self.assertTrue(got[1]["ok"])

    def test_malformed_line_isolated(self):
        # The middle line has an unterminated quote.
        queries = ('scope --scope top.u_dp0\n'
                   'trace --filter "oops\n'
                   'tree\n')
        out = run_batch(queries, *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertEqual([f["ok"] for f in got], [True, False, True])
        self.assertIn("parse error", got[1]["error"])

    # ── Labels override the id; blanks/comments are skipped (no seq bump) ──
    def test_labels_comments_and_blanks(self):
        queries = ("tree\n"
                   "\n"
                   "# a full-line comment\n"
                   "scope --scope top.u_dp0  # mylabel\n"
                   "tree\n")
        out = run_batch(queries, *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        # blank + comment skipped without consuming a sequence number, so the
        # third emitted query is "3" (not "5"); the label overrides id #2.
        self.assertEqual([f["id"] for f in got], ["1", "mylabel", "3"])

    # ── Per-line --limit overrides the batch-level default ──
    def test_per_line_limit_overrides_default(self):
        queries = ("trace -s q --scope top.u_dp0\n"
                   "trace -s q --scope top.u_dp0 --limit 1\n")
        out = run_batch(queries, *BASIC, "--json", "--limit", "7")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertEqual(got[0]["result"]["summary"]["limit"], 7)   # inherited
        self.assertEqual(got[1]["result"]["summary"]["limit"], 1)   # overridden

    # ── Text mode: a "# <id>" header precedes each command's normal output ──
    def test_text_mode_headers(self):
        out = run_batch("scope --scope top.u_dp0  # qscope\ntree\n", *BASIC)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("# qscope", out.stdout)
        self.assertIn("# 2", out.stdout)
        self.assertIn("PORTS", out.stdout)      # scope's human output is present

    # ── --commands FILE reads queries from a file instead of stdin ──
    def test_commands_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("tree\nscope --scope top.u_dp0\n")
            path = fh.name
        try:
            out = run_batch("", *BASIC, "--json", "--commands", path)
            self.assertEqual(out.returncode, 0, out.stderr)
            got = frames(out.stdout)
            self.assertEqual([f["result"]["tool"] for f in got], ["tree", "scope"])
        finally:
            Path(path).unlink()

    # ── An unloadable design is fatal: non-zero exit, no per-line frames ──
    def test_fatal_no_sources_exits_nonzero(self):
        out = run_batch("tree\n", "-d", "definitely_not_a_dir", "--json")
        self.assertNotEqual(out.returncode, 0)
        envelope = json.loads(out.stdout)        # a single error envelope, not JSONL
        self.assertEqual(envelope["status"], "error")
        self.assertNotIn("id", envelope)         # not a batch frame

    # ── `batch --schema` prints valid JSON describing the frame ──
    def test_schema(self):
        p = subprocess.run(
            RTLSCANNER + ["batch", "--schema"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr)
        schema = json.loads(p.stdout)
        self.assertEqual(set(schema["required"]), {"id", "ok"})
        self.assertIn("result", schema["properties"])


if __name__ == "__main__":
    unittest.main()

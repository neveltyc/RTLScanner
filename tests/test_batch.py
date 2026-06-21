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

# Import the tokenizer directly for unit tests (the package requires pyslang,
# the same dependency every subprocess test below already needs).
sys.path.insert(0, str(ROOT / "src"))
import rtl_batch  # noqa: E402


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

    # ── Fidelity across all seven query subcommands (not just scope/trace) ──
    def test_fidelity_across_subcommands(self):
        queries = [
            ["tree"],
            ["scope", "--scope", "top.u_dp0"],
            ["trace", "-s", "q", "--scope", "top.u_dp0"],
            ["fanin", "-s", "q", "--scope", "top.u_dp0"],
            ["fanout", "-s", "q", "--scope", "top.u_dp0"],
            ["xref", "--module", "register"],
            ["lint"],
        ]
        stdin = "".join(" ".join(q) + "\n" for q in queries)
        out = run_batch(stdin, *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertEqual(len(got), len(queries))
        for frame, q in zip(got, queries):
            self.assertTrue(frame["ok"], f"{q[0]}: {frame.get('error')}")
            self.assertEqual(frame["result"], run_single(q[0], *BASIC, *q[1:]),
                             f"{q[0]} batch result != single command")

    # ── Load-once reuse is safe: a repeated query does not drift, and matches
    #    the standalone command, on a fixture that actually has findings ──
    def test_repeated_query_is_identical_and_matches_single(self):
        LINT = ["-d", "examples/lint"]
        out = run_batch("lint\nlint\n", *LINT, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)
        self.assertEqual(len(got), 2)
        # No state accumulates across queries on the shared compilation.
        self.assertEqual(got[0]["result"], got[1]["result"])
        self.assertEqual(got[0]["result"], run_single("lint", *LINT))
        self.assertGreater(got[0]["result"]["summary"]["total"], 0)  # real findings

    # ── lint's exit-1-on-error-finding is NOT propagated: frame stays ok=true,
    #    the finding shows up in summary.has_error, and the batch exits 0 ──
    def test_lint_error_finding_frame_stays_ok(self):
        design = ("module m(input wire a, output wire y);\n"
                  "  assign y = a & missing_sig;\n"   # undeclared -> error severity
                  "endmodule\n")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "m.sv").write_text(design)
            out = run_batch("lint --rules semantic\n", "-d", d, "--json")
            self.assertEqual(out.returncode, 0, out.stderr)   # batch still exits 0
            got = frames(out.stdout)
            self.assertEqual(len(got), 1)
            self.assertTrue(got[0]["ok"])                     # ok means "ran"
            self.assertTrue(got[0]["result"]["summary"]["has_error"])

    # ── tree --export is rejected per-line (no stdout corruption, no fs write) ──
    def test_export_dash_rejected_keeps_jsonl_clean(self):
        out = run_batch("tree --export -\nscope --scope top.u_dp0\n", *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = frames(out.stdout)            # every line still parses as JSON
        self.assertEqual(len(got), 2)
        self.assertFalse(got[0]["ok"])
        self.assertIn("export", got[0]["error"])
        self.assertTrue(got[1]["ok"])       # batch continued

    def test_export_file_rejected_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "should_not_exist.f"
            out = run_batch(f"tree --export {target}\n", *BASIC, "--json")
            self.assertEqual(out.returncode, 0, out.stderr)
            got = frames(out.stdout)
            self.assertFalse(got[0]["ok"])
            self.assertFalse(target.exists())   # no filesystem side effect

    # ── A non-UTF8 byte in a --commands file is isolated, not fatal, and the
    #    emitted JSONL stays valid UTF-8 (the bad byte degrades to U+FFFD) ──
    def test_commands_file_non_utf8_does_not_abort(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            fh.write(b"tree\nscope --scope top.u_dp0  # caf\xe9\ntree\n")  # 0xe9 byte
            path = fh.name
        try:
            out = run_batch("", *BASIC, "--json", "--commands", path)
            self.assertEqual(out.returncode, 0, out.stderr)   # not aborted
            got = frames(out.stdout)                          # output still valid UTF-8 JSON
            self.assertEqual(len(got), 3)                     # all queries ran
            self.assertTrue(all(f["ok"] for f in got))
        finally:
            Path(path).unlink()

    # ── --commands - reads stdin (same as omitting --commands) ──
    def test_commands_dash_reads_stdin(self):
        stdin = "tree\nscope --scope top.u_dp0\n"
        a = run_batch(stdin, *BASIC, "--json")
        b = run_batch(stdin, *BASIC, "--json", "--commands", "-")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertEqual(frames(a.stdout), frames(b.stdout))

    # ── A failed query frame carries `error` and no `result` key ──
    def test_failure_frame_has_no_result_key(self):
        out = run_batch("trace -s nope --scope top.u_dp0\n", *BASIC, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        frame = frames(out.stdout)[0]
        self.assertFalse(frame["ok"])
        self.assertIn("error", frame)
        self.assertNotIn("result", frame)

    # ── Text mode: a failing query is isolated under its header; batch continues ──
    def test_text_mode_error_isolation(self):
        out = run_batch("trace -s nope --scope top.u_dp0\ntree\n", *BASIC)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("# 1", out.stdout)
        self.assertIn("Error:", out.stdout)     # the failure is shown inline
        self.assertIn("# 2", out.stdout)
        self.assertIn("instances", out.stdout)  # the second query still ran (tree)


class SplitLineTests(unittest.TestCase):
    """Unit tests for the hand-written shell-style tokenizer (no subprocess)."""

    def test_basic(self):
        self.assertEqual(rtl_batch.split_line("scope --scope top.u_dp0\n"),
                         (["scope", "--scope", "top.u_dp0"], None))

    def test_word_boundary_label(self):
        self.assertEqual(rtl_batch.split_line("scope --scope x  # my label\n"),
                         (["scope", "--scope", "x"], "my label"))

    def test_empty_comment_is_no_label(self):
        self.assertEqual(rtl_batch.split_line("tree #\n"), (["tree"], None))

    def test_full_line_comment_skipped(self):
        self.assertEqual(rtl_batch.split_line("# just a comment\n"), ([], "just a comment"))

    def test_blank_line(self):
        self.assertEqual(rtl_batch.split_line("\n"), ([], None))
        self.assertEqual(rtl_batch.split_line("   \n"), ([], None))

    def test_glued_hash_is_literal(self):
        # '#' only starts a comment at a word boundary, so a#b stays one token.
        self.assertEqual(rtl_batch.split_line("trace -s a#b\n"),
                         (["trace", "-s", "a#b"], None))

    def test_quoted_hash_is_literal(self):
        self.assertEqual(rtl_batch.split_line('trace --filter "a#b"\n'),
                         (["trace", "--filter", "a#b"], None))

    def test_escaped_hash_is_literal(self):
        self.assertEqual(rtl_batch.split_line("trace -s a\\#b\n"),
                         (["trace", "-s", "a#b"], None))

    def test_escaped_trailing_space_is_preserved(self):
        # Regression: a backslash-escaped trailing space must survive into the
        # token, not become a spurious "No escaped character" parse error.
        self.assertEqual(rtl_batch.split_line("scope --scope a\\ \n"),
                         (["scope", "--scope", "a "], None))

    def test_unterminated_quote_raises(self):
        with self.assertRaises(ValueError):
            rtl_batch.split_line('trace --filter "oops\n')


if __name__ == "__main__":
    unittest.main()

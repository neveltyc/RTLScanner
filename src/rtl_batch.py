#!/usr/bin/env python3
"""
rtl_batch — the ``batch`` command: many queries against one loaded design.

Ports RWaveAnalyzer's ``--batch`` mode to RTLScanner.  The expensive work — parse
+ elaborate the whole SystemVerilog design (``rtl_common.build_compilation`` ->
``pyslang`` parse + ``getRoot()`` elaboration) — is done **once**; then each line
read from stdin is dispatched to the *same* per-command code path the single-shot
CLI uses (``tree`` / ``trace`` / ``scope`` / ``fanin`` / ``fanout`` / ``lint`` /
``xref``), reusing the one loaded design.

    rtlscanner batch -d ./rtl --json < queries.txt

Each non-blank line is ``<subcmd> [flags...]  # optional-label`` (shell-tokenized;
a trailing ``#`` at a word boundary starts a label/comment).  In ``--json`` mode
every query emits one compact JSONL frame, flushed as it finishes::

    {"id":"1","ok":true,"result":<the command's normal --json envelope>}
    {"id":"crit","ok":false,"error":"signal 'nope' not found ..."}

In text mode a ``# <id>`` header precedes each command's normal output.  A failing
query never stops the batch and the run still exits 0; a non-zero exit means the
design could not be loaded at all (fatal, surfaced before any line is read).

The two reuse seams live elsewhere: ``rtl_cli.prepare_compilation`` short-circuits
on the injected ``args._prepared`` (load once), and ``agent_json.capture_emit``
intercepts each command's envelope so it can be re-framed (stream JSONL).
"""

from __future__ import annotations

import argparse
import copy
import json
import shlex
import sys
from typing import Dict, List, Optional, Tuple

import agent_json
import rtl_cli


# ── Per-line tokenization ────────────────────────────────────────────
def _split_comment(line: str) -> Tuple[str, Optional[str]]:
    """Split a trailing ``#`` comment off a line, honoring quotes/escapes.

    A ``#`` starts the comment only at a word boundary (start of line or after
    whitespace) and only when unquoted — matching shell behavior, so a ``#``
    inside ``"..."`` / ``'...'`` or glued to a token (``a#b``) stays literal.
    Returns ``(code, label)`` where ``label`` is the trimmed comment text (or
    None).
    """
    quote: Optional[str] = None
    prev_ws = True
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote is not None:
            if c == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:        # backslash escapes the next char
            prev_ws = False
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            prev_ws = False
            i += 1
            continue
        if c == "#" and prev_ws:
            label = line[i + 1:].strip()
            return line[:i], (label or None)
        prev_ws = c.isspace()
        i += 1
    return line, None


def split_line(line: str) -> Tuple[List[str], Optional[str]]:
    """Tokenize one batch line into ``(tokens, label)``.

    Blank lines and full-line comments yield an empty token list (the caller
    skips them).  Raises ``ValueError`` on a malformed line (e.g. an unterminated
    quote), which the caller isolates as one error frame.
    """
    code, label = _split_comment(line)
    # Tokenize the raw remainder — do NOT strip it first.  Stripping would orphan
    # a backslash that escapes a trailing space/tab (`-s a\ `), turning a valid
    # token into a spurious "No escaped character" parse error.  shlex treats the
    # trailing newline as whitespace, and a blank / comment-only line tokenizes
    # to [] (the caller skips it).
    return shlex.split(code), label


# ── Per-line argument parsing ────────────────────────────────────────
class _LineParseError(Exception):
    """A batch line failed to parse; isolated instead of exiting the process."""


class _RaisingArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that raises instead of ``sys.exit`` on error.

    The single-shot CLI wants argparse's default ``error -> print + exit(2)``; a
    batch line must instead surface the failure as one result frame and keep
    going, so both ``error`` and ``exit`` raise.
    """

    def error(self, message: str):                       # noqa: D401
        raise _LineParseError(message)

    def exit(self, status: int = 0, message: Optional[str] = None):
        raise _LineParseError(message or f"exit {status}")


def _build_line_parsers() -> Dict[str, argparse.ArgumentParser]:
    """One parser per query subcommand, carrying only its own flags.

    Reuses each subcommand's ``add_arguments`` so per-line parsing matches the
    single-shot CLI exactly.  Input flags and ``--json`` are fixed by the batch
    line; only the per-query item cap (``--limit``) is allowed per line.
    """
    import rtlscanner  # lazy: rtlscanner imports this module (avoid a cycle)

    parsers: Dict[str, argparse.ArgumentParser] = {}
    for name, (add_fn, _run, _desc) in rtlscanner.SUBCOMMANDS.items():
        if name == "batch":
            continue
        lp = _RaisingArgumentParser(prog=name, add_help=False)
        add_fn(lp)
        lp.add_argument("--limit", type=int, default=None, metavar="N")
        parsers[name] = lp
    return parsers


# ── Output framing ───────────────────────────────────────────────────
def _print_frame(obj: Dict) -> None:
    """Print one compact JSONL frame and flush (so a long batch streams)."""
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), flush=True)


def _emit_failure(json_mode: bool, ident: str, message: str) -> None:
    if json_mode:
        _print_frame({"id": ident, "ok": False, "error": message})
    else:
        print(f"# {ident}")
        print(f"Error: {message}")


def _run_query(json_mode: bool, ident: str, subcmd: str, qargs,
               run_fn) -> None:
    """Run one already-parsed query and emit its frame / text block."""
    if not json_mode:
        print(f"# {ident}")
        try:
            run_fn(qargs, None)
        except agent_json.AgentError as e:
            print(f"Error: {e.message}")
        except Exception as e:                           # noqa: BLE001
            print(f"Error: internal error: {e}")
        return

    q_env = agent_json.Envelope(subcmd, agent_json.filter_command(qargs))
    try:
        with agent_json.capture_emit() as sink:
            run_fn(qargs, q_env)
        if not sink:
            _emit_failure(True, ident, "no result produced")
            return
        envelope = sink[-1]
        _print_frame({"id": ident,
                      "ok": envelope.get("status") == "ok",
                      "result": envelope})
    except agent_json.AgentError as e:
        _emit_failure(True, ident, e.message)
    except Exception as e:                               # noqa: BLE001
        _emit_failure(True, ident, f"internal error: {e}")


# ── Command source ───────────────────────────────────────────────────
def _open_commands(args):
    """Return the stream of query lines: ``--commands FILE`` or stdin."""
    path = getattr(args, "_commands", None)
    if path and path != "-":
        try:
            # errors="replace" so a stray non-UTF-8 byte degrades to U+FFFD in
            # one query instead of aborting the whole batch with a misleading
            # "design could not be loaded" non-zero exit — and, unlike
            # surrogateescape, keeps the emitted JSONL stream valid UTF-8.
            return open(path, "r", encoding="utf-8", errors="replace")
        except OSError as e:
            raise rtl_cli.CliError(
                agent_json.ERR_INPUT_NOT_FOUND,
                f"cannot read commands file: {e}", 1)
    return sys.stdin


# ── Subcommand: batch ────────────────────────────────────────────────
def add_batch_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("batch")
    # dest is underscore-prefixed so it stays out of each query's `command`
    # echo (agent_json.filter_command drops private attrs), keeping a batch
    # `result` byte-identical to the equivalent single command's envelope.
    g.add_argument("--commands", dest="_commands", default=None, metavar="FILE",
                   help="Read query lines from FILE instead of stdin "
                        "('-' = stdin). One '<subcmd> [flags]  # label' per line.")


def run_batch(args, env) -> int:
    """Load the design once, then stream one result per stdin query line."""
    import rtlscanner  # lazy: avoid the import cycle

    json_mode = bool(args.json)

    # 1. Load the design ONCE.  A failure here is fatal — no query can run — so
    #    let the CliError propagate to main(), which emits a single error and a
    #    non-zero exit before any line is read (RWaveAnalyzer loads first).
    prepared = rtl_cli.prepare_compilation(
        args, require_sources=True, human_error_rc=1, collect_diagnostics=True)

    line_parsers = _build_line_parsers()
    run_fns = rtlscanner.SUBCOMMANDS

    # 2. Stream queries.  Each line is isolated: a parse/run failure becomes one
    #    error frame and the batch keeps going (exit stays 0).
    src = _open_commands(args)
    seq = 0
    try:
        for raw in src:
            try:
                tokens, label = split_line(raw)
            except ValueError as e:
                seq += 1                      # malformed but non-blank line
                _emit_failure(json_mode, str(seq), f"parse error: {e}")
                continue
            if not tokens:
                continue                      # blank / comment: no sequence bump

            seq += 1
            ident = label or str(seq)
            subcmd = tokens[0]
            if subcmd not in line_parsers:
                _emit_failure(json_mode, ident,
                              f"unknown command '{subcmd}'; choose from "
                              + ", ".join(sorted(line_parsers)))
                continue

            try:
                qargs = line_parsers[subcmd].parse_args(
                    tokens[1:], namespace=copy.copy(args))
            except _LineParseError as e:
                _emit_failure(json_mode, ident, f"{subcmd}: {e}")
                continue

            # A query line must stay read-only and route its output through the
            # frame stream.  `tree --export` does neither — it writes a filelist
            # to disk, or with `-` raw text straight to stdout (corrupting the
            # JSONL stream, since it bypasses the capture sink) — so reject it as
            # an isolated per-query error.
            if getattr(qargs, "export", None):
                _emit_failure(json_mode, ident,
                              f"{subcmd}: --export is not supported inside batch; "
                              "run it as a standalone command")
                continue

            qargs.cmd = subcmd
            qargs._prepared = prepared        # reuse the one loaded design
            _run_query(json_mode, ident, subcmd, qargs, run_fns[subcmd][1])
    finally:
        if src is not sys.stdin:
            src.close()

    return 0

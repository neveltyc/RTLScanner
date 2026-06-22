#!/usr/bin/env python3
"""
signal_cli — shared front-end for the dataflow commands.

The argv → engine-query adapter that ``trace`` / ``fanin`` / ``fanout`` share:
resolve inputs and build the compilation, construct the ``SignalTracer``,
auto-detect the scope, normalize a dotted ``-s`` form, and split off a trailing
bit-select.  Keeps the per-command presentation modules (``signal_trace``,
``signal_flow``) thin — they import :func:`prepare` / :func:`add_unroll_args`
from here and never touch input/compile plumbing directly.
"""

from __future__ import annotations

import re
import sys

import agent_json
import rtl_cli
from rtl_config import flow_config
from rtl_dataflow import SignalTracer, bit_label


# ── Bit-select on a queried signal (e.g. `status[3]`, `status[7:4]`) ──
_BITSEL_RE = re.compile(r'^(.+?)\[(\d+)(?::(\d+))?\]$')


def split_bit_select(name):
    """Split a trailing bit-select off a signal name.

    'status[3]'   -> ('status', (3, 3))
    'u_dp.q[7:4]' -> ('u_dp.q', (4, 7))
    'status'      -> ('status', None)
    A variable/dynamic index does not match and is left on the name.
    """
    if not name:
        return name, None
    m = _BITSEL_RE.match(name.strip())
    if not m:
        return name, None
    a = int(m.group(2))
    b = int(m.group(3)) if m.group(3) is not None else a
    return m.group(1), (min(a, b), max(a, b))


# ── Shared dataflow-precision flags ──────────────────────────────────
def add_unroll_args(p):
    """Constant-condition pruning / procedural loop unrolling (default on).
    Folds constant if/case branches and unrolls constant-bound for/repeat loops
    so dead-branch edges drop out and ``p[i]`` resolves to concrete bits."""
    g = p.add_argument_group('precision')
    g.add_argument('--unroll', dest='unroll', action='store_true', default=None,
                   help='Prune constant if/case branches and unroll constant-'
                        'bound for/repeat loops (default: on)')
    g.add_argument('--no-unroll', dest='unroll', action='store_false',
                   help='Disable pruning / loop unrolling (conservative, '
                        'symbol-level control over-approximation)')
    g.add_argument('--max-unroll', type=int, default=None, metavar='N',
                   help='Cap on total unrolled iterations per block (default: '
                        '2048); a loop exceeding it stays conservative')


# ── Shared input/dispatch helpers ────────────────────────────────────
def _emit_note(env, message):
    """Surface a reinterpretation note: a JSON diagnostic, else a stderr line."""
    if env is not None:
        env.add_diagnostic("note", message=message)
    else:
        print(f"note: {message}", file=sys.stderr)


def _build_tracer(args, prepared):
    """Construct a ``SignalTracer`` honoring the ``[flow]`` precision config and
    the ``--unroll`` / ``--max-unroll`` flags (CLI overrides config overrides the
    built-in defaults), shared by every dataflow command's setup."""
    fcfg = flow_config(prepared.config)
    unroll = getattr(args, 'unroll', None)
    if unroll is None:
        unroll = fcfg["unroll"] if fcfg["unroll"] is not None else True
    max_unroll = getattr(args, 'max_unroll', None)
    if max_unroll is None:
        max_unroll = fcfg["max_unroll"] if fcfg["max_unroll"] is not None else 2048
    return SignalTracer(prepared.comp, unroll=unroll, max_unroll=max_unroll)


def prepare(args, env, *, need_signal=False):
    """Common setup for trace/fanin/fanout: resolve inputs, build
    compilation, auto-detect scope, normalize dotted -s forms, and split off
    a trailing bit-select.  Returns (tracer, scope, signal, bit_range);
    raises CliError on any input/compile/scope failure."""
    prepared = rtl_cli.prepare_compilation_checked(args, env, human_error_rc=1)
    tracer = _build_tracer(args, prepared)
    scope = rtl_cli.resolve_scope(
        args.scope,
        tracer.get_top_paths(),
        human_error_rc=1,
    )

    signal = getattr(args, 'signal', None)
    if need_signal and not signal:
        raise rtl_cli.CliError(
            agent_json.ERR_INPUT_NOT_FOUND,
            'specify --signal/-s NAME',
            1,
        )

    bit_range = None
    if signal:
        signal, bit_range = split_bit_select(signal)
        scope, signal, note = tracer.normalize_signal(scope, signal)
        if note:
            _emit_note(env, note)

    return tracer, scope, signal, bit_range


def prepare_path(args, env):
    """Setup for the ``path`` command: build the compilation + tracer, resolve
    the anchor scope, and normalize the ``--from`` / ``--to`` endpoints.

    Each endpoint may be a bare signal in ``--scope``, a dotted relative path, or
    an absolute hierarchical path (resolved by ``normalize_signal``, the same way
    a dotted ``-s`` is).  A trailing bit-select is stripped — path-finding is at
    node (signal) granularity — with a note.  Returns ``(tracer, from_scope,
    from_signal, to_scope, to_signal)``; raises CliError on any
    input/compile/scope failure or a missing endpoint.
    """
    prepared = rtl_cli.prepare_compilation_checked(args, env, human_error_rc=1)
    tracer = _build_tracer(args, prepared)
    base_scope = rtl_cli.resolve_scope(
        args.scope, tracer.get_top_paths(), human_error_rc=1)

    raw_from = getattr(args, 'from_sig', None)
    raw_to = getattr(args, 'to_sig', None)
    if not raw_from or not raw_to:
        raise rtl_cli.CliError(
            agent_json.ERR_INPUT_NOT_FOUND,
            'specify both --from NAME and --to NAME',
            1,
        )

    def resolve_endpoint(raw, flag):
        name, bit_range = split_bit_select(raw)
        if bit_range is not None:
            _emit_note(env, f"--{flag} bit-select {bit_label(bit_range)} ignored; "
                            "path-finding is node-level (signal granularity)")
        scope, signal, note = tracer.normalize_signal(base_scope, name)
        if note:
            _emit_note(env, note)
        return scope, signal

    from_scope, from_signal = resolve_endpoint(raw_from, 'from')
    to_scope, to_signal = resolve_endpoint(raw_to, 'to')
    return tracer, from_scope, from_signal, to_scope, to_signal

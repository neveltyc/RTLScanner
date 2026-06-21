#!/usr/bin/env python3
"""
signal_trace — the ``trace`` command (presentation layer).

Renders the dataflow engine's typed ``TraceResult`` (the single RTL driver and
all loads of a signal, produced by ``rtl_dataflow.SignalTracer.trace``) into the
two ``trace`` shapes: the ``--json`` envelope and the human driver/load view.
All mechanism lives in ``rtl_dataflow``; the shared argv front-end lives in
``signal_cli``.

    rtlscanner trace -d ./rtl --signal q --scope top.u_dp0
    rtlscanner trace --filelist rtl.f --signal clk --scope top --filter 'u_dp*'
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Optional

import agent_json
from rtl_common import Color
from rtl_dataflow import TraceResult, bit_label
from signal_cli import add_unroll_args, prepare


# ── Display glyphs ───────────────────────────────────────────────────
# Defined as named constants so they can be referenced inside f-string
# expressions.  Python < 3.12 forbids backslash escapes (e.g. "◀")
# inside the "{...}" part of an f-string, which would be a SyntaxError.
GLYPH_DRIVER = "◀"
GLYPH_LOADS = "▶"
GLYPH_WARN = "⚠"
GLYPH_DASH = "—"
GLYPH_HR = "─"
GLYPH_ARROW_L = "←"


# ── Render: TraceResult → trace shapes ───────────────────────────────
def _driver_to_dict(d):
    out = dict(kind=d.kind, source=d.source, description=d.description,
               symbol=d.symbol_name, symbol_kind=d.symbol_kind,
               scope_path=d.scope_path)
    if d.bits:
        out['bits'] = d.bits
    if d.file:
        out['file'] = d.file
        out['line'] = d.line
    return out


def _load_to_dict(ld):
    out = dict(kind=ld.kind, description=ld.description,
               scope_path=ld.scope_path)
    if ld.instance_name:
        out['instance'] = ld.instance_name
    if ld.port_name:
        out['port'] = ld.port_name
        out['direction'] = ld.port_direction
    if ld.bits:
        out['bits'] = ld.bits
    if ld.file:
        out['file'] = ld.file
        out['line'] = ld.line
    return out


def _filtered_loads(r, pattern=None):
    if not pattern:
        return list(r.loads)
    return [ld for ld in r.loads
            if fnmatch.fnmatch(ld.instance_name, pattern)]


def _trace_to_dict(r, load_filter=None):
    d = dict(signal=r.signal_name, type=r.signal_type,
             kind=r.signal_kind, scope=r.scope_path,
             module=r.scope_module)
    if r.bit_range is not None:
        d['bit_select'] = bit_label(r.bit_range)
    d['driver'] = _driver_to_dict(r.driver) if r.driver else None
    if r.extra_drivers:
        d['extra_drivers'] = [_driver_to_dict(x) for x in r.extra_drivers]
        d['multi_driver_warning'] = r.multi_driver
    loads = _filtered_loads(r, load_filter)
    d['loads'] = [_load_to_dict(ld) for ld in loads]
    d['load_count'] = len(loads)
    return d


def _driver_line(r, d):
    C = Color
    bits = f" {C.yellow(r.signal_name + d.bits)}" if d.bits else ""
    where = ""
    if d.scope_path and d.scope_path != r.scope_path:
        where = f"  {C.dim('@ ' + d.scope_path)}"
    loc = f"  {C.dim(d.file + ':' + str(d.line))}" if d.file else ""
    return f"    {GLYPH_ARROW_L} {d.description}{bits}{where}{loc}"


def _trace_pretty(r, load_filter=None, limit=0):
    C = Color

    display_name = r.signal_name + (bit_label(r.bit_range) if r.bit_range else "")
    print(f"Signal: {C.bold(display_name)}  {C.dim(r.signal_type)}")
    print(f"Scope:  {C.cyan(r.scope_path)}  [{C.yellow(r.scope_module)}]")
    print("─" * 60)

    # ── Driver (singular in RTL) ──
    drivers = ([r.driver] if r.driver else []) + r.extra_drivers
    if not drivers:
        print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}  {C.dim('(none ' + GLYPH_DASH + ' undriven)')}")
    elif len(drivers) == 1:
        print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}")
        print(_driver_line(r, drivers[0]))
    elif r.multi_driver:
        print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}  {C.red(GLYPH_WARN + ' MULTI-DRIVER (' + str(len(drivers)) + ')')}")
        for d in drivers:
            print(_driver_line(r, d))
    else:
        print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVERS')} ({len(drivers)})  {C.dim('disjoint bit ranges')}")
        for d in drivers:
            print(_driver_line(r, d))

    # ── Loads (narrowed to the queried bits on a bit-select) ──
    loads = _filtered_loads(r, load_filter)
    shown, total, truncated = agent_json.clip(loads, limit)
    hdr = f"\n  {C.green(GLYPH_LOADS + ' LOADS')} ({total})"
    if load_filter:
        hdr += f"  {C.dim('filter: ' + load_filter)}"
    print(hdr)

    if not shown:
        print(f"    {C.dim('(none found)')}")
    else:
        by_kind = {}
        for ld in shown:
            by_kind.setdefault(ld.kind, []).append(ld)
        kind_labels = {
            "port_connection": "Instance port connections",
            "continuous_assign": "Continuous assignments",
            "procedural": "Procedural blocks",
        }
        for kind_key, kind_loads in by_kind.items():
            if len(by_kind) > 1:
                lbl = kind_labels.get(kind_key, kind_key)
                print(f"    {C.dim(GLYPH_HR + GLYPH_HR + ' ' + lbl + ' (' + str(len(kind_loads)) + ') ' + GLYPH_HR + GLYPH_HR)}")
            for ld in kind_loads:
                loc = f"  {C.dim(ld.file + ':' + str(ld.line))}" if ld.file else ""
                bits = f" {C.yellow(r.signal_name + ld.bits)}" if ld.bits else ""
                print(f"    → {ld.description}{bits}{loc}")
        if truncated:
            print(f"    {C.dim(agent_json.truncation_note(len(shown), total, 'loads'))}")

    print()


# ── Subcommand: trace ────────────────────────────────────────────────
def add_trace_args(p):
    g = p.add_argument_group('trace')
    g.add_argument('-s', '--signal', default=None, metavar='NAME',
                   help='Signal to trace; a bit-select narrows the driver '
                        'origin (e.g. status[3], status[7:4])')
    g.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope; auto-detect when single top')
    g.add_argument('--filter', default=None, metavar='GLOB',
                   help='Shell glob on instance names to narrow loads')
    add_unroll_args(p)


@dataclass
class TraceOutput(agent_json.CommandResult):
    """Typed result of ``trace``: one signal's driver/loads in a scope.

    The engine's ``TraceResult`` is pure data; this wraps the single result in
    the shared envelope shape (``mode``/``scope``/``results``) with the
    load-count summary, and renders it (JSON via ``_trace_to_dict``, human via
    ``_trace_pretty``) so the seam matches the other five commands.
    """
    result: TraceResult
    scope: str
    load_filter: Optional[str] = None

    def to_json(self, limit):
        rd = _trace_to_dict(self.result, self.load_filter)
        load_total = int(rd.get('load_count', 0))
        if 'loads' in rd:
            shown, _t, tr = agent_json.clip(rd['loads'], limit)
            rd['loads'] = shown
        else:
            tr = False
        data = {'mode': 'signal', 'scope': self.scope, 'results': [rd]}
        summary = {
            'mode': 'signal', 'results': 1,
            'drivers': 1 if rd.get('driver') else 0,
            'loads':   load_total,
            'truncated': tr,
            'limit': limit,
        }
        return data, summary

    def render_human(self, limit):
        _trace_pretty(self.result, self.load_filter, limit=limit)
        return 0


def run_trace(args, env):
    tracer, scope, signal, bit_range = prepare(args, env, need_signal=True)
    r = tracer.trace(signal, scope, bit_range)
    out = TraceOutput(r, scope, args.filter)
    return agent_json.render(env, out, agent_json.resolve_limit(args.limit))

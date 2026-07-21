#!/usr/bin/env python3
"""
signal_driver — the ``driver`` command (presentation layer).

Where ``trace`` locates a signal's single RTL driver and its loads, ``driver``
returns the *value logic* of each driver: the branch structure (guard chain),
each branch's RHS operands (with bit ranges), and — for sequential drivers — the
clock/reset timing. This is the elaborated structure a downstream value/root-cause
analysis joins with waveform values to explain "why is S this value at time T".

All mechanism lives in ``rtl_dataflow.SignalTracer.driver_payload``; the shared
argv front-end (input resolution, scope auto-detect, bit-select) lives in
``signal_cli``.

    rtlscanner driver -d ./rtl --signal q --scope top.u_dp
    rtlscanner driver --filelist rtl.f -s state --scope top --json
"""

from __future__ import annotations

from dataclasses import dataclass

import agent_json
from rtl_common import Color
from signal_cli import add_unroll_args, prepare


def add_driver_args(p):
    g = p.add_argument_group('driver')
    g.add_argument('-s', '--signal', default=None, metavar='NAME',
                   help='Signal whose driver value-logic to extract; a '
                        'bit-select narrows the driver origin (e.g. state[3])')
    g.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope; auto-detect when single top')
    add_unroll_args(p)


@dataclass
class DriverOutput(agent_json.CommandResult):
    """Typed result of ``driver``: one signal's structured drivers in a scope."""
    payload: dict

    def to_json(self, limit):
        data = self.payload
        summary = {
            'signal': data.get('signal'),
            'scope': data.get('scope'),
            'drivers': len(data.get('drivers', [])),
            'truncated': False,
            'limit': limit,
        }
        return data, summary

    def render_human(self, limit):
        _driver_pretty(self.payload)
        return 0


def _driver_pretty(p):
    C = Color
    print(f"Signal: {C.bold(p.get('signal', ''))}  "
          f"{C.dim('(' + str(p.get('width')) + ' bits)') if p.get('width') else ''}")
    print(f"Scope:  {C.cyan(p.get('scope', ''))}")
    print("─" * 60)
    drivers = p.get('drivers', [])
    if not drivers:
        print(f"  {C.dim('(no driver — undriven)')}")
    for i, d in enumerate(drivers):
        loc = f"  {C.dim(d.get('file', '') + ':' + str(d.get('line', '')))}" if d.get('file') else ""
        print(f"\n  {C.red('◀ DRIVER')} {C.yellow(d.get('source', ''))}{loc}")
        t = d.get('timing', {})
        if t.get('kind') == 'sequential':
            clk = f"{t.get('clock_edge', '')} {t.get('clock', '?')}"
            rst = f", reset {t.get('reset_edge', '')} {t['reset']}" if t.get('reset') else ""
            print(f"    {C.dim('timing:')} sequential ({clk}{rst})")
        else:
            print(f"    {C.dim('timing:')} {t.get('kind', 'unknown')}")
        for a in d.get('assignments', []):
            guards = _guard_summary(a.get('guards', []))
            when = f"  {C.dim('when ' + guards)}" if guards else ""
            ops = ", ".join(o.get('path', o.get('name', '')) for o in a.get('rhs_operands', []))
            print(f"    {a.get('lhs', '')} ← {C.green(a.get('rhs_text', ''))}{when}")
            if ops:
                print(f"        {C.dim('operands: ' + ops)}")


def _guard_summary(guards):
    parts = []
    for g in guards:
        if g.get('kind') == 'if':
            conds = " && ".join(c.get('text', '') for c in g.get('conditions', []))
            parts.append(conds if g.get('polarity') else f"!({conds})")
        elif g.get('kind') == 'case':
            parts.append(f"{g.get('case_text', '')} in {{{', '.join(g.get('match', []))}}}")
    return " && ".join(p for p in parts if p)


def run_driver(args, env):
    tracer, scope, signal, bit_range = prepare(args, env, need_signal=True)
    payload = tracer.driver_payload(signal, scope, bit_range)
    out = DriverOutput(payload)
    return agent_json.render(env, out, agent_json.resolve_limit(args.limit))

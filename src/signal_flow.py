#!/usr/bin/env python3
"""
signal_flow — the ``fanin`` / ``fanout`` commands (presentation layer).

Renders the dataflow engine's typed ``FlowResult`` (a bit-aware BFS cone,
produced by ``rtl_dataflow.SignalTracer.flow``) into the two ``flow`` shapes:
the full node/edge graph (``FlowGraphOutput``) and the ``--summary`` view
(``FlowSummaryOutput``).  All mechanism lives in ``rtl_dataflow``; the shared
argv front-end lives in ``signal_cli``.

    rtlscanner fanin  -d ./rtl --signal q  --scope top.u_dp0
    rtlscanner fanout -d ./rtl --signal q  --scope top.u_dp0 --summary
"""

from __future__ import annotations

from dataclasses import dataclass

import agent_json
from rtl_common import Color
from rtl_dataflow import FlowResult, bit_label
from signal_cli import add_unroll_args, prepare


# ── Render: FlowEdge / FlowResult → flow shapes ──────────────────────
def _edge_source_label(e):
    return e.source + (bit_label(e.source_bits) if e.source_bits else "")


def _edge_target_label(e):
    return e.target + (bit_label(e.target_bits) if e.target_bits else "")


def _edge_to_dict(e, depth=None):
    d = dict(source=e.source, target=e.target, kind=e.kind,
             description=e.description,
             source_type=e.source_type, target_type=e.target_type)
    if e.source_bits is not None:
        d['source_bits'] = bit_label(e.source_bits)
    if e.target_bits is not None:
        d['target_bits'] = bit_label(e.target_bits)
    if e.segments:
        d['segments'] = [
            {'source_bits': bit_label(sb), 'target_bits': bit_label(db)}
            for (sb, db, _off) in e.segments]
    if e.file:
        d['file'] = e.file
        d['line'] = e.line
    if e.clocked:
        d['clocked'] = True
    if depth is not None:
        d['depth'] = depth
    return d


def _flow_to_dict(r):
    d = dict(
        mode=r.mode, signal=r.signal_name, type=r.signal_type,
        kind=r.signal_kind, scope=r.scope_path,
        module=r.scope_module, start=r.start,
        max_depth=r.max_depth,
        nodes=r.nodes,
        edges=[_edge_to_dict(edge, depth) for edge, depth in r.edges],
        edge_count=len(r.edges),
    )
    if r.bit_range is not None:
        d['bit_select'] = bit_label(r.bit_range)
    if r.comb:
        d['comb'] = True
    return d


def _mode_line(title, max_depth, comb):
    C = Color
    tag = C.dim(' · combinational cone') if comb else ''
    return (f"Mode:   {C.green(title)}  "
            f"{C.dim('depth <= ' + str(max_depth))}{tag}")


def _flow_pretty(r, limit=0):
    C = Color
    title = "FANIN" if r.mode == "fanin" else "FANOUT"
    name = r.signal_name + (bit_label(r.bit_range) if r.bit_range else "")
    print(f"Signal: {C.bold(name)}  {C.dim(r.signal_type)}")
    print(f"Scope:  {C.cyan(r.scope_path)}  [{C.yellow(r.scope_module)}]")
    print(_mode_line(title, r.max_depth, r.comb))
    print("─" * 60)
    if not r.edges:
        print(f"\n  {C.dim('(no dataflow edges found)')}\n")
        return
    shown, total, truncated = agent_json.clip(r.edges, limit)
    cur_depth = None
    for edge, depth in shown:
        if depth != cur_depth:
            cur_depth = depth
            print(f"\n  {C.dim('depth ' + str(depth))}")
        loc = f"  {C.dim(edge.file + ':' + str(edge.line))}" if edge.file else ""
        print(f"    {C.cyan(_edge_source_label(edge))} → "
              f"{C.cyan(_edge_target_label(edge))}  "
              f"{C.yellow(edge.kind)} {C.dim(edge.description)}{loc}")
    if truncated:
        print(f"\n  {C.dim(agent_json.truncation_note(len(shown), total, 'edges'))}")
    print()


# ── Subcommands: fanin / fanout ──────────────────────────────────────
def add_flow_args(p):
    g = p.add_argument_group('flow')
    g.add_argument('-s', '--signal', default=None, metavar='NAME',
                   help='Starting signal (required)')
    g.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope; auto-detect when single top')
    g.add_argument('--depth', type=int, default=None, metavar='N',
                   help='Maximum BFS traversal depth (default: 4; with --comb, '
                        'unbounded — the cone is bounded by registers instead)')
    g.add_argument('--comb', action='store_true',
                   help='Combinational cone: stop at sequential (clocked) edges, '
                        'so the result is the pure combinational fan-in/out '
                        'bounded by flip-flops (slang-netlist getCombFan parity)')
    g.add_argument('--summary', action='store_true',
                   help='Counts + direct neighbors only; omit the full node/edge graph')
    add_unroll_args(p)


@dataclass
class FlowGraphOutput(agent_json.CommandResult):
    """Typed result of ``fanin``/``fanout`` (full cone): the node/edge graph."""
    result: FlowResult
    mode: str
    scope: str
    signal: str

    def to_json(self, limit):
        rd = _flow_to_dict(self.result)
        # Cap the edges, then keep exactly the nodes those surviving edges
        # reference (plus the depth-0 start).  Clipping `nodes` and `edges`
        # independently could emit an edge whose endpoint was dropped from
        # `nodes`, leaving the JSON graph internally inconsistent.
        edges_shown, edges_total, e_tr = agent_json.clip(rd['edges'], limit)
        kept = {rd['start']}
        for e in edges_shown:
            kept.add(e['source'])
            kept.add(e['target'])
        nodes_shown = [n for n in rd['nodes'] if n in kept]
        nodes_total = len(rd['nodes'])
        data = {
            'mode': self.mode, 'scope': self.scope, 'signal': self.signal,
            'start': rd['start'], 'nodes': nodes_shown, 'edges': edges_shown,
            'max_depth': rd['max_depth'],
        }
        if 'bit_select' in rd:
            data['bit_select'] = rd['bit_select']
        if rd.get('comb'):
            data['comb'] = True
        summary = {
            'mode': self.mode, 'results': 1,
            'nodes': nodes_total, 'edges': edges_total,
            'max_depth': rd['max_depth'],
            'truncated': e_tr or len(nodes_shown) < nodes_total, 'limit': limit,
        }
        return data, summary

    def render_human(self, limit):
        _flow_pretty(self.result, limit=limit)
        return 0


@dataclass
class FlowSummaryOutput(agent_json.CommandResult):
    """Typed result of ``fanin``/``fanout`` ``--summary``: counts, an
    edges-by-depth histogram, and the direct neighbors — instead of the full
    cone, which can be thousands of edges on a real design.

    The histogram and direct-neighbor set are derived once (``__post_init__``)
    and read by both renderers; this summary view intentionally omits the
    ``truncated``/``limit`` envelope fields (the full graph is not emitted).
    """
    result: FlowResult
    mode: str
    scope: str
    signal: str

    def __post_init__(self):
        rd = _flow_to_dict(self.result)
        edges = rd['edges']
        by_depth = {}
        for e in edges:
            d = int(e.get('depth', 0))
            by_depth[d] = by_depth.get(d, 0) + 1
        far = 'source' if self.mode == 'fanin' else 'target'
        self.by_depth = by_depth
        self.direct = sorted({e[far] for e in edges
                              if int(e.get('depth', 0)) == 1})
        self.node_count = len(rd['nodes'])
        self.edge_count = len(edges)
        self.max_depth = rd['max_depth']
        self.start = rd['start']
        self.comb = bool(self.result.comb)

    def to_json(self, limit):
        data = {
            'mode': self.mode, 'scope': self.scope, 'signal': self.signal,
            'start': self.start, 'summary_only': True,
            'node_count': self.node_count, 'edge_count': self.edge_count,
            'max_depth': self.max_depth,
            'edges_by_depth': {str(k): self.by_depth[k]
                               for k in sorted(self.by_depth)},
            'direct': self.direct,
        }
        if self.comb:
            data['comb'] = True
        summary = {'mode': self.mode, 'results': 1, 'nodes': self.node_count,
                   'edges': self.edge_count, 'max_depth': self.max_depth}
        return data, summary

    def render_human(self, limit):
        C = Color
        title = "FANIN" if self.mode == "fanin" else "FANOUT"
        print(f"Signal: {C.bold(self.signal)}")
        print(_mode_line(title + ' summary', self.max_depth, self.comb))
        print(f"  nodes {C.yellow(str(self.node_count))}   "
              f"edges {C.yellow(str(self.edge_count))}")
        if self.by_depth:
            print("  edges by depth: " +
                  ", ".join(f"{k}:{self.by_depth[k]}"
                            for k in sorted(self.by_depth)))
        label = "direct sources" if self.mode == "fanin" else "direct sinks"
        print(f"  {label} ({len(self.direct)}): "
              + (", ".join(self.direct) or "(none)"))
        return 0


def run_flow(args, env, *, mode):
    tracer, scope, signal, bit_range = prepare(args, env, need_signal=True)
    comb = bool(getattr(args, 'comb', False))
    # --depth defaults to 4, except a combinational cone defaults to unbounded
    # (None) since registers, not a hop count, bound it.  An explicit --depth
    # always wins for either mode.
    depth = args.depth
    if depth is None:
        depth = None if comb else 4
    r = tracer.flow(signal, scope, mode, depth, bit_range=bit_range, comb=comb)
    if getattr(args, 'summary', False):
        out = FlowSummaryOutput(r, mode, scope, signal)
    else:
        out = FlowGraphOutput(r, mode, scope, signal)
    return agent_json.render(env, out, agent_json.resolve_limit(args.limit))

#!/usr/bin/env python3
"""
signal_path — the ``path`` command (presentation layer).

Renders the dataflow engine's typed ``PathResult`` (a point-to-point path found
by ``rtl_dataflow.PathFinder`` via depth-first search) into the two ``path``
shapes: the ``--json`` envelope and the human node/edge walk.  All mechanism
lives in ``rtl_dataflow``; the shared argv front-end lives in ``signal_cli``.

    rtlscanner path -d ./rtl --from a --to y0 --scope top
    rtlscanner path -d ./rtl --from u_dp.q --to result --comb
"""

from __future__ import annotations

from dataclasses import dataclass

import agent_json
from rtl_common import Color
from rtl_dataflow import PathResult, bit_label
from signal_cli import add_unroll_args, prepare_path
# Reuse the fan-in/out edge serializer so a path edge and a flow edge can never
# drift in shape (source/target/kind/description/bits/segments/clocked/location).
from signal_flow import _edge_to_dict


# ── Display glyphs ───────────────────────────────────────────────────
GLYPH_ARROW = "→"
GLYPH_STEP = "↓"
GLYPH_HR = "─"


# ── Render: PathResult → path shapes ─────────────────────────────────
def _path_to_dict(r):
    edges = [_edge_to_dict(e) for e in r.edges]
    d = dict(
        mode="path",
        comb=r.comb,
        **{"from": r.from_signal, "to": r.to_signal},
        from_scope=r.from_scope, to_scope=r.to_scope,
        from_type=r.start_type, to_type=r.end_type,
        start=r.start, end=r.end,
        found=r.found,
        length=r.length,
        nodes=list(r.nodes),
        edges=edges,
    )
    return d


def _edge_bits_label(e):
    """A compact ``[hi:lo]→[hi:lo]`` bit annotation for an edge, or '' when the
    whole signal flows (additive — only shown for a proper sub-range)."""
    if e.source_bits is None and e.target_bits is None:
        return ""
    sb = bit_label(e.source_bits) if e.source_bits else ""
    db = bit_label(e.target_bits) if e.target_bits else ""
    return f"{sb}{GLYPH_ARROW}{db}"


def _path_pretty(r, limit=0):
    C = Color
    tag = C.dim(" · combinational") if r.comb else ""
    print(f"Path:   {C.bold(r.start)} {GLYPH_ARROW} {C.bold(r.end)}{tag}")
    if r.found:
        hops = f"{r.length} hop" + ("" if r.length == 1 else "s")
        print(f"Result: {C.green('found')}  {C.dim('(' + hops + ')')}")
    else:
        print(f"Result: {C.red('no path')}")
    print(GLYPH_HR * 60)

    if not r.found:
        kind = "combinational path" if r.comb else "path"
        print(f"\n  {C.dim('(no ' + kind + ' from ' + r.start + ' to ' + r.end + ')')}")
        print()
        return 0

    # Alternating node / edge walk.  Clip to the first N edges (a start-side
    # prefix) so a pathological path stays agent-friendly; nodes follow the
    # surviving edges (node[i] --edge[i]--> node[i+1]).
    edges_shown, edges_total, truncated = agent_json.clip(r.edges, limit)
    n_show = len(edges_shown) + 1
    types = r.node_types()
    for i, node in enumerate(r.nodes[:n_show]):
        ty = types[i] if i < len(types) else ""
        suffix = f"  {C.dim(ty)}" if ty else ""
        print(f"  {C.cyan(node)}{suffix}")
        if i < len(edges_shown):
            e = edges_shown[i]
            loc = f"  {C.dim(e.file + ':' + str(e.line))}" if e.file else ""
            clk = f" {C.red('clocked')}" if e.clocked else ""
            bits = _edge_bits_label(e)
            bits = f"  {C.yellow(bits)}" if bits else ""
            print(f"    {GLYPH_STEP} {C.yellow(e.kind)}{clk} "
                  f"{C.dim(e.description)}{bits}{loc}")
    if truncated:
        print(f"\n  {C.dim(agent_json.truncation_note(n_show, len(r.nodes), 'nodes'))}")
    print()
    return 0


# ── Subcommand: path ─────────────────────────────────────────────────
def add_path_args(p):
    g = p.add_argument_group("path")
    # `from`/`to` are Python keywords, so store under from_sig/to_sig; the CLI
    # spelling stays --from / --to.
    g.add_argument("--from", dest="from_sig", default=None, metavar="NAME",
                   help="Path start node (the driver end). A bare signal in "
                        "--scope, a dotted relative path, or an absolute path.")
    g.add_argument("--to", dest="to_sig", default=None, metavar="NAME",
                   help="Path end node (the loaded end), same forms as --from. "
                        "The path is directional: --from must drive --to.")
    g.add_argument("--scope", default=None, metavar="SCOPE",
                   help="Hierarchical scope anchoring bare --from/--to names; "
                        "auto-detect when there is a single top.")
    g.add_argument("--comb", action="store_true",
                   help="Combinational path only: never traverse into a register "
                        "(sequential) node, so the path is bounded by flip-flops "
                        "(the same boundary as --comb fanin/fanout).")
    add_unroll_args(p)


@dataclass
class PathOutput(agent_json.CommandResult):
    """Typed result of ``path``: the node/edge walk from --from to --to.

    A point-to-point path between two design nodes, found by a depth-first search
    over the dataflow graph (``rtl_dataflow.PathFinder``).  ``found == False`` (an
    empty path) is a normal, successful result — the two nodes simply are not
    connected in the requested direction (or, with ``--comb``, only through a
    register) — so the envelope still reports ``status:"ok"``.
    """
    result: PathResult

    def to_json(self, limit):
        rd = _path_to_dict(self.result)
        # Clip edges to the start-side prefix, then keep exactly the nodes those
        # surviving edges connect (node[i] --edge[i]--> node[i+1]) so the emitted
        # nodes/edges stay mutually consistent, like FlowGraphOutput.
        edges_shown, edges_total, e_tr = agent_json.clip(rd["edges"], limit)
        rd["edges"] = edges_shown
        rd["nodes"] = rd["nodes"][:len(edges_shown) + 1] if rd["nodes"] else []
        summary = {
            "mode": "path",
            "found": self.result.found,
            "length": self.result.length,
            "nodes": len(self.result.nodes),
            "edges": edges_total,
            "comb": self.result.comb,
            "truncated": e_tr,
            "limit": limit,
        }
        return rd, summary

    def render_human(self, limit):
        return _path_pretty(self.result, limit=limit)


def run_path(args, env):
    tracer, from_scope, from_signal, to_scope, to_signal = prepare_path(args, env)
    r = tracer.find_path(from_signal, from_scope, to_signal, to_scope,
                         comb=bool(getattr(args, "comb", False)))
    out = PathOutput(r)
    return agent_json.render(env, out, agent_json.resolve_limit(args.limit))

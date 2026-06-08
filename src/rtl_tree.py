#!/usr/bin/env python3
"""
rtl_tree — Verilog/SystemVerilog RTL Hierarchy Viewer

Like ``tree`` for your RTL design.  Uses pyslang for accurate parsing
with full SystemVerilog support (generate, parameters, interfaces, …).

Usage:
    rtl_tree file1.sv file2.v ...
    rtl_tree -d ./rtl_dir
    rtl_tree -d ./rtl_dir --top top_module
    rtl_tree -d ./rtl_dir --trace clk --scope top
    rtl_tree -d ./rtl_dir --write-filelist rtl.f

Install dependency:
    pip install pyslang
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import pyslang.ast as ast

from rtl_common import (
    Color,
    FileList,
    collect_filelist,
    parse_filelist,
    merge_filelists,
    filter_filelist,
    write_filelist_file,
    build_compilation,
)

import agent_json
from agent_json import AgentError, Envelope, filter_command, emit


# ── Data Structures ──────────────────────────────────────────────────
@dataclass
class InstanceNode:
    """Represents one instance in the elaborated hierarchy."""
    inst_name: str
    module_name: str
    hier_path: str
    params: dict = field(default_factory=dict)
    is_interface: bool = False
    generated_scope: str = ""
    children: list = field(default_factory=list)


# ── Hierarchy Building ───────────────────────────────────────────────
def build_hierarchy(
    files: list[str],
    top_module: Optional[str] = None,
    include_dirs: list[str] = None,
    defines: list[str] = None,
    return_compilation: bool = False,
):
    """
    Parse files with pyslang, elaborate, and return the instance tree.

    Returns ``(tops, diags)`` normally.  When *return_compilation* is
    True a third element — the raw ``ast.Compilation`` — is appended so
    that callers (e.g. signal tracing) can run further analysis.
    """
    comp, diag_messages = build_compilation(files, include_dirs, defines)
    root = comp.getRoot()

    all_instances = []
    def _collect(sym):
        all_instances.append(sym)
        return None
    root.visit(lookup_table={ast.SymbolKind.Instance: _collect})

    node_map = {}
    for inst in all_instances:
        try:
            path = inst.hierarchicalPath
            node_map[path] = InstanceNode(
                inst_name=inst.name,
                module_name=inst.body.name,
                hier_path=path,
                params={p.name: str(p.value) for p in (inst.body.parameters or [])},
                is_interface=getattr(inst, 'isInterface', False),
            )
        except Exception:
            continue  # skip phantom instances from `include compilation

    for path, node in node_map.items():
        parts = path.split('.')
        parent_path, parent_len = None, 0
        for i in range(len(parts) - 1, 0, -1):
            candidate = '.'.join(parts[:i])
            if candidate in node_map:
                parent_path, parent_len = candidate, i
                break
        if parent_path is not None:
            gs = '.'.join(parts[parent_len:-1])
            if gs:
                node.generated_scope = gs
            node_map[parent_path].children.append(node)

    tops = []
    for inst in root.topInstances:
        path = inst.hierarchicalPath
        if path in node_map:
            if top_module is None or inst.body.name == top_module or inst.name == top_module:
                tops.append(node_map[path])
    if not tops:
        for inst in root.topInstances:
            path = inst.hierarchicalPath
            if path in node_map:
                tops.append(node_map[path])

    if return_compilation:
        return tops, diag_messages, comp
    return tops, diag_messages


# ── Tree Display ─────────────────────────────────────────────────────
def _node_label(node, show_params=True):
    label = f"{Color.cyan(node.inst_name)} : {Color.yellow(node.module_name)}"
    if show_params and node.params:
        pstr = ', '.join(f"{k}={v}" for k, v in node.params.items())
        label += f" {Color.dim('#(' + pstr + ')')}"
    if node.is_interface:
        label += f" {Color.magenta('[interface]')}"
    if node.generated_scope:
        label += f" {Color.dim('← ' + node.generated_scope)}"
    return label


def print_tree(node, prefix="", is_last=True, is_root=False,
               max_depth=-1, cur=0, show_params=True, show_path=False):
    label = _node_label(node, show_params)
    if show_path:
        label += f"  {Color.dim(node.hier_path)}"
    if is_root:
        root_lbl = f"{Color.green(node.inst_name)} : {Color.yellow(node.module_name)}"
        if show_params and node.params:
            pstr = ', '.join(f"{k}={v}" for k, v in node.params.items())
            root_lbl += f" {Color.dim('#(' + pstr + ')')}"
        print(root_lbl)
    else:
        print(f"{prefix}{'└── ' if is_last else '├── '}{label}")
    if 0 <= max_depth <= cur:
        if node.children:
            cp = prefix + ("    " if is_last else "│   ") if not is_root else "    "
            n = len(node.children)
            noun = "child" if n == 1 else "children"
            print(f"{cp}{Color.dim(f'... ({n} {noun})')}")
        return
    cp = "" if is_root else prefix + ("    " if is_last else "│   ")
    for i, child in enumerate(node.children):
        print_tree(child, cp, i == len(node.children) - 1,
                   max_depth=max_depth, cur=cur + 1,
                   show_params=show_params, show_path=show_path)


def node_to_dict(node, max_depth=-1, depth=0):
    d = {'instance': node.inst_name, 'module': node.module_name, 'path': node.hier_path}
    if node.params:       d['parameters'] = node.params
    if node.is_interface: d['is_interface'] = True
    if node.generated_scope: d['generated_scope'] = node.generated_scope
    if max_depth < 0 or depth < max_depth:
        if node.children:
            d['children'] = [node_to_dict(c, max_depth, depth + 1) for c in node.children]
    return d


# ── Statistics ───────────────────────────────────────────────────────
def _collect_stats(node, stats):
    stats['total'] += 1
    stats['modules'].add(node.module_name)
    stats['counts'][node.module_name] = stats['counts'].get(node.module_name, 0) + 1
    if not node.children: stats['leaf'] += 1
    for c in node.children: _collect_stats(c, stats)

def _depth(node):
    return 0 if not node.children else 1 + max(_depth(c) for c in node.children)

def print_stats(tops):
    st = {'total': 0, 'modules': set(), 'counts': {}, 'leaf': 0}
    md = 0
    for t in tops:
        _collect_stats(t, st); md = max(md, _depth(t))
    print(f"\n{'─'*50}\n  {Color.green('Hierarchy Statistics')}\n{'─'*50}")
    print(f"  Top modules:      {', '.join(t.module_name for t in tops)}")
    print(f"  Total instances:  {st['total']}")
    print(f"  Unique modules:   {len(st['modules'])}")
    print(f"  Max depth:        {md}")
    print(f"  Leaf instances:   {st['leaf']}")
    print(f"\n  {Color.cyan('Module usage breakdown:')}")
    for mod, cnt in sorted(st['counts'].items(), key=lambda x: -x[1]):
        print(f"    {mod:20s} {cnt:4d}  {Color.dim('█' * min(cnt, 40))}")
    print(f"{'─'*50}")


def _walk(nodes):
    for n in nodes:
        yield n; yield from _walk(n.children)


# ── Agent-mode helpers ───────────────────────────────────────────────
def _filelist_to_dict(fl: FileList) -> dict:
    return {
        'sources':      list(fl.sources),
        'include_dirs': list(fl.include_dirs),
        'defines':      dict(getattr(fl, 'defines', {}) or {}),
    }


def _hierarchy_summary(tops, files_parsed: int) -> dict:
    st = {'total': 0, 'modules': set(), 'counts': {}, 'leaf': 0}
    md = 0
    for t in tops:
        _collect_stats(t, st); md = max(md, _depth(t))
    return {
        'instances':      st['total'],
        'unique_modules': len(st['modules']),
        'max_depth':      md,
        'files_parsed':   files_parsed,
        'module_counts':  dict(st['counts']),
    }


def _emit_trace_envelope(env: 'Envelope', tracer, scope,
                         trace_signal, trace_list, trace_all,
                         cross, load_filter) -> int:
    if trace_list:
        sigs = tracer.list_signals(scope)
        if not sigs:
            return emit(env.fail(agent_json.ERR_SCOPE_NOT_FOUND,
                                 f"scope '{scope}' not found or empty"))
        data = {'mode': 'list', 'scope': scope, 'signals': sigs}
        summary = {'mode': 'list', 'results': 0, 'signals': len(sigs)}
        return emit(env.ok(data, summary))

    if trace_all:
        results = tracer.trace_all(scope, cross)
        if not results:
            return emit(env.fail(agent_json.ERR_SCOPE_NOT_FOUND,
                                 f"no signals in scope '{scope}'"))
        rdicts = [r.to_dict(load_filter) for r in results]
        data = {'mode': 'all', 'scope': scope, 'results': rdicts}
        summary = {
            'mode':    'all',
            'results': len(rdicts),
            'drivers': sum(1 for d in rdicts if d.get('driver')),
            'loads':   sum(int(d.get('load_count', 0)) for d in rdicts),
        }
        return emit(env.ok(data, summary))

    # single signal
    r = tracer.trace(trace_signal, scope, cross)
    if r is None:
        return emit(env.fail(agent_json.ERR_SIGNAL_NOT_FOUND,
                             f"signal '{trace_signal}' not found in scope '{scope}'"))
    rd = r.to_dict(load_filter)
    data = {'mode': 'signal', 'scope': scope, 'results': [rd]}
    summary = {
        'mode':    'signal',
        'results': 1,
        'drivers': 1 if rd.get('driver') else 0,
        'loads':   int(rd.get('load_count', 0)),
    }
    return emit(env.ok(data, summary))


# ── CLI ──────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        prog='rtl-tree',
        description='rtl-tree — SV RTL Hierarchy Viewer (pyslang)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Hierarchy:
  rtl-tree top.sv sub.sv               Show hierarchy tree
  rtl-tree -d ./rtl --top cpu          Specify top module
  rtl-tree -d ./rtl --depth 2          Limit depth
  rtl-tree -d ./rtl --stats            Module usage stats
  rtl-tree --filelist rtl.f --top cpu  Use VCS-style filelist

Signal tracing:
  rtl-tree -d ./rtl --trace clk --scope top
  rtl-tree --filelist rtl.f --trace q --scope top.u_dp --cross
  rtl-tree --filelist rtl.f --trace-list --scope top.u_dp
  rtl-tree --filelist rtl.f --trace data --scope top --filter 'u_fifo*'
""")
    p.add_argument('files', nargs='*', help='Verilog/SV source files')
    p.add_argument('-d', '--dir', action='append', default=[], metavar='DIR',
                   help='Directory to scan recursively (repeatable)')

    fl = p.add_argument_group('filelist')
    fl.add_argument('--filelist', action='append', default=[], metavar='FILE',
                    help='VCS-style .f filelist (repeatable)')
    fl.add_argument('--write-filelist', default=None, metavar='FILE',
                    help="Write the resolved filelist to FILE ('-' for stdout)")
    fl.add_argument('--filelist-only', action='store_true',
                    help='Only write the filelist, then exit (no tree)')
    fl.add_argument('--filelist-root', '--projpath', dest='filelist_root',
                    default='.', metavar='DIR',
                    help='Base path for filelist relative paths (default: .)')
    fl.add_argument('--filelist-path', choices=('rel', 'abs', 'prefix'),
                    default='rel',
                    help='Path style in --write-filelist output (default: rel)')
    fl.add_argument('--filelist-prefix', default='${PROJPATH}', metavar='STR',
                    help="Prefix for 'prefix' path style (default: ${PROJPATH})")
    fl.add_argument('--exclude', action='append', default=[], metavar='GLOB',
                    help='Exclude paths matching glob (repeatable)')

    h = p.add_argument_group('hierarchy display')
    h.add_argument('--top', default=None, metavar='MODULE',
                   help='Treat MODULE as the top (default: auto-detect)')
    h.add_argument('--depth', type=int, default=-1, metavar='N',
                   help='Limit tree depth to N levels (default: unlimited)')
    h.add_argument('--no-params', action='store_true',
                   help='Hide module parameter values')
    h.add_argument('--path', action='store_true',
                   help='Show full hierarchical path next to each node')
    h.add_argument('--json', action='store_true',
                   help='Emit the hierarchy as JSON')
    h.add_argument('--schema', action='store_true',
                   help='Print the JSON Schema for --json output and exit')
    h.add_argument('--stats', action='store_true',
                   help='Print module usage statistics')
    h.add_argument('--flat', action='store_true',
                   help='List every instance path flat, one per line')

    t = p.add_argument_group('signal tracing')
    t.add_argument('--trace', metavar='SIGNAL', default=None,
                   help='Trace driver/loads of SIGNAL (needs --scope)')
    t.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope for tracing (e.g. top.u_dp)')
    t.add_argument('--trace-list', action='store_true',
                   help='List all signals in --scope')
    t.add_argument('--trace-all', action='store_true',
                   help='Trace every signal in --scope')
    t.add_argument('--cross', action='store_true',
                   help='Follow the signal through port boundaries')
    t.add_argument('--filter', default=None, metavar='GLOB',
                   help='Filter traced loads by instance-name glob')

    p.add_argument('--no-color', action='store_true',
                   help='Disable ANSI colors')
    p.add_argument('--diag', action='store_true',
                   help='Print parser/elaboration diagnostics to stderr')
    a = p.parse_args()

    if a.schema:
        sys.exit(agent_json.print_schema('rtl-tree'))

    if a.no_color or not sys.stdout.isatty() or a.json:
        Color.disable()

    env: Optional[Envelope] = Envelope('rtl-tree', filter_command(a)) if a.json else None

    def die(msg, code=agent_json.ERR_INTERNAL, exit_code=1):
        if a.json:
            sys.exit(emit(env.fail(code, msg)))
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(exit_code)

    # ── sources ──
    all_paths = list(a.files) + list(a.dir)
    if not all_paths and not a.filelist:
        if a.json:
            die('no input: pass files, --dir, or --filelist', agent_json.ERR_INPUT_NOT_FOUND, 1)
        p.print_help(); sys.exit(1)

    fl_root = Path(a.filelist_root).expanduser().resolve()
    parsed = []
    for f in a.filelist:
        try: parsed.append(parse_filelist(f, fl_root, prefix=a.filelist_prefix))
        except FileNotFoundError as e:
            die(str(e), agent_json.ERR_BAD_FILELIST)
    scanned = collect_filelist(all_paths, excludes=a.exclude, root=fl_root) if all_paths else FileList()
    filelist = filter_filelist(merge_filelists(*parsed, scanned), a.exclude, fl_root)

    if a.write_filelist:
        write_filelist_file(a.write_filelist, filelist, fl_root, a.filelist_path, a.filelist_prefix)
        if a.write_filelist != '-':
            print(f"Wrote filelist: {a.write_filelist}", file=sys.stderr)
    if a.filelist_only:
        if not a.write_filelist:
            die('--filelist-only requires --write-filelist', agent_json.ERR_INPUT_NOT_FOUND)
        if a.json:
            sys.exit(emit(env.ok({'filelist': _filelist_to_dict(filelist)},
                                 {'instances': 0, 'unique_modules': 0,
                                  'max_depth': 0, 'files_parsed': len(filelist.sources),
                                  'module_counts': {}})))
        return

    files = filelist.sources
    if not files:
        die('no .v/.sv source files found', agent_json.ERR_INPUT_NOT_FOUND)

    # ── elaborate ──
    is_trace = a.trace or a.trace_list or a.trace_all
    try:
        res = build_hierarchy(files, a.top, filelist.include_dirs, filelist.defines,
                              return_compilation=is_trace)
    except Exception as e:
        die(f'compilation failed: {e}', agent_json.ERR_COMPILE_FAILED)
    if is_trace:
        tops, diags, comp = res
    else:
        tops, diags = res

    if env is not None:
        for d in diags[:50]:
            env.add_diagnostic('warning', '', 0, 0, str(d))
    elif a.diag and diags:
        print(f"\n{Color.red('Parser diagnostics:')}", file=sys.stderr)
        for d in diags[:20]: print(f"  {d}", file=sys.stderr)
        if len(diags) > 20: print(f"  ... and {len(diags)-20} more", file=sys.stderr)

    if not tops:
        msg = "no top-level modules found"
        if a.top: msg += f" (--top {a.top})"
        die(msg, agent_json.ERR_NO_TOP)

    # ── signal tracing ──
    if is_trace:
        from signal_trace import SignalTracer
        tracer = SignalTracer(comp)
        scope = a.scope
        if scope is None:
            tp = tracer.get_top_paths()
            if len(tp) == 1: scope = tp[0]
            elif tp:
                if a.json:
                    die('multiple tops — specify --scope: ' + ', '.join(tp),
                        agent_json.ERR_SCOPE_NOT_FOUND)
                print("Multiple tops — specify --scope:", file=sys.stderr)
                for x in tp: print(f"  {x}", file=sys.stderr)
                sys.exit(1)
        if a.json:
            sys.exit(_emit_trace_envelope(env, tracer, scope,
                                          a.trace, a.trace_list, a.trace_all,
                                          a.cross, a.filter))
        if a.trace_list:   tracer.cmd_list(scope, a.json)
        elif a.trace_all:  tracer.cmd_trace_all(scope, a.cross, a.filter, a.json)
        elif a.trace:      tracer.cmd_trace(a.trace, scope, a.cross, a.filter, a.json)
        return

    # ── hierarchy output ──
    if a.json:
        hier = [node_to_dict(t, a.depth) for t in tops]
        sys.exit(emit(env.ok(
            {'hierarchy': hier, 'filelist': _filelist_to_dict(filelist)},
            _hierarchy_summary(tops, len(files)),
        )))
    elif a.flat:
        for t in _walk(tops): print(f"{t.hier_path}  ({t.module_name})")
    else:
        for i, t in enumerate(tops):
            if i: print()
            print_tree(t, is_root=True, max_depth=a.depth,
                       show_params=not a.no_params, show_path=a.path)
    if a.stats: print_stats(tops)
    if not a.flat:
        total = sum(1 for _ in _walk(tops))
        mods = len(set(n.module_name for n in _walk(tops)))
        print(f"\n{Color.dim(f'{total} instances, {mods} unique modules, {len(files)} files parsed')}")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
rtl_tree — Verilog/SystemVerilog RTL Hierarchy Viewer

Like ``tree`` for your RTL design.  Uses pyslang for accurate parsing
with full SystemVerilog support (generate, parameters, interfaces, …).

This module is invoked via the unified `rtlscanner tree` subcommand.

Install dependency:
    pip install pyslang
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import pyslang.ast as ast

from rtl_common import (
    Color,
    FileList,
    write_filelist_file,
    build_compilation,
)

import agent_json
from agent_json import Envelope, filter_command, emit
from rtl_config import build_filelist


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
    files: list,
    top_module: Optional[str] = None,
    include_dirs: list = None,
    defines: list = None,
    return_compilation: bool = False,
):
    """Parse files with pyslang, elaborate, and return the instance tree."""
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
            continue

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
    defines = {}
    for define in getattr(fl, 'defines', []) or []:
        if '=' in define:
            name, value = define.split('=', 1)
        else:
            name, value = define, ""
        if name:
            defines[name] = value
    return {
        'sources':      list(fl.sources),
        'include_dirs': list(fl.include_dirs),
        'defines':      defines,
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


# ── CLI plumbing ─────────────────────────────────────────────────────
def add_arguments(p: argparse.ArgumentParser) -> None:
    """Attach tree-specific flags to a subparser."""
    g = p.add_argument_group('hierarchy display')
    g.add_argument('--top', default=None, metavar='MODULE',
                   help='Treat MODULE as the top (default: auto-detect)')
    g.add_argument('--depth', type=int, default=-1, metavar='N',
                   help='Limit tree depth to N levels (default: unlimited)')
    g.add_argument('--no-params', action='store_true',
                   help='Hide module parameter values')
    g.add_argument('--path', action='store_true',
                   help='Show full hierarchical path next to each node')
    g.add_argument('--stats', action='store_true',
                   help='Print module usage statistics')
    g.add_argument('--flat', action='store_true',
                   help='List every instance path flat, one per line')

    exp = p.add_argument_group('filelist export')
    exp.add_argument('--export', default=None, metavar='FILE',
                     help="Write the resolved filelist to FILE ('-' for stdout) and exit")
    exp.add_argument('--path-style', choices=('rel', 'abs', 'prefix'),
                     default='rel',
                     help="Path style for --export (default: rel)")

    p.add_argument('--diag', action='store_true',
                   help='Print parser/elaboration diagnostics to stderr')


def run(args: argparse.Namespace, env: Optional[Envelope]) -> int:
    from rtl_config import resolve_inputs, load_config
    cfg, cfg_path = load_config()
    ri = resolve_inputs(
        cli_files=args.files,
        cli_dir=args.dir,
        cli_filelist=args.filelist,
        cli_exclude=args.exclude,
        config=cfg,
        config_path=cfg_path,
    )
    for note in ri.notes:
        print(f"note: {note}", file=sys.stderr)

    try:
        filelist = build_filelist(ri)
    except FileNotFoundError as e:
        return _die(env, str(e), agent_json.ERR_BAD_FILELIST)
    except ValueError as e:
        return _die(env, str(e), agent_json.ERR_INPUT_NOT_FOUND)

    # --export FILE: write filelist and exit
    if args.export:
        if not filelist.sources:
            return _die(env, 'no .v/.sv source files found',
                        agent_json.ERR_INPUT_NOT_FOUND)
        try:
            write_filelist_file(args.export, filelist, ri.root,
                                args.path_style, ri.prefix)
        except Exception as e:
            return _die(env, f"failed to write filelist: {e}",
                        agent_json.ERR_INTERNAL)
        if args.export != '-':
            print(f"Wrote filelist: {args.export}", file=sys.stderr)
        if env is not None:
            return emit(env.ok(
                {'hierarchy': [], 'filelist': _filelist_to_dict(filelist)},
                {'instances': 0, 'unique_modules': 0, 'max_depth': 0,
                 'files_parsed': len(filelist.sources), 'module_counts': {}},
            ))
        return 0

    if not filelist.sources:
        return _die(env, 'no .v/.sv source files found',
                    agent_json.ERR_INPUT_NOT_FOUND)

    try:
        tops, diags = build_hierarchy(
            filelist.sources, args.top,
            filelist.include_dirs, filelist.defines)
    except Exception as e:
        return _die(env, f'compilation failed: {e}',
                    agent_json.ERR_COMPILE_FAILED)

    if env is not None:
        for d in diags[:50]:
            env.add_diagnostic('warning', '', 0, 0, str(d))
    elif args.diag and diags:
        print(f"\n{Color.red('Parser diagnostics:')}", file=sys.stderr)
        for d in diags[:20]: print(f"  {d}", file=sys.stderr)
        if len(diags) > 20: print(f"  ... and {len(diags)-20} more", file=sys.stderr)

    if not tops:
        msg = "no top-level modules found"
        if args.top:
            msg += f" (--top {args.top})"
        return _die(env, msg, agent_json.ERR_NO_TOP)

    if env is not None:
        hier = [node_to_dict(t, args.depth) for t in tops]
        return emit(env.ok(
            {'hierarchy': hier, 'filelist': _filelist_to_dict(filelist)},
            _hierarchy_summary(tops, len(filelist.sources)),
        ))

    if args.flat:
        for t in _walk(tops):
            print(f"{t.hier_path}  ({t.module_name})")
    else:
        for i, t in enumerate(tops):
            if i: print()
            print_tree(t, is_root=True, max_depth=args.depth,
                       show_params=not args.no_params, show_path=args.path)
    if args.stats:
        print_stats(tops)
    if not args.flat:
        total = sum(1 for _ in _walk(tops))
        mods = len(set(n.module_name for n in _walk(tops)))
        print(f"\n{Color.dim(f'{total} instances, {mods} unique modules, {len(filelist.sources)} files parsed')}")
    return 0


def _die(env: Optional[Envelope], msg: str, code: str) -> int:
    if env is not None:
        return emit(env.fail(code, msg))
    print(f"Error: {msg}", file=sys.stderr)
    return 1

#!/usr/bin/env python3
"""
rtl_tree — Verilog/SystemVerilog RTL Hierarchy Viewer

Like `tree` for your RTL design. Uses pyslang for accurate parsing
with full SystemVerilog support (generate, parameters, interfaces, etc.)

Usage:
    rtl_tree file1.sv file2.v ...
    rtl_tree -d ./rtl_dir
    rtl_tree -d ./rtl_dir --top top_module
    rtl_tree -d ./rtl_dir --depth 3
    rtl_tree -d ./rtl_dir --json
    rtl_tree -d ./rtl_dir --stats

Install dependency:
    pip install pyslang
"""

import argparse
import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

try:
    import pyslang.ast as ast
    from pyslang.syntax import SyntaxTree
except ImportError:
    print("Error: pyslang is required. Install with:", file=sys.stderr)
    print("  pip install pyslang", file=sys.stderr)
    sys.exit(1)


# ── ANSI Colors ──────────────────────────────────────────────────────
class Color:
    """ANSI color codes, auto-disabled when not writing to a terminal."""
    _enabled = True

    @classmethod
    def disable(cls):
        cls._enabled = False

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls._enabled else text

    @classmethod
    def green(cls, t):   return cls._wrap("1;32", t)
    @classmethod
    def cyan(cls, t):    return cls._wrap("1;36", t)
    @classmethod
    def yellow(cls, t):  return cls._wrap("33", t)
    @classmethod
    def dim(cls, t):     return cls._wrap("2", t)
    @classmethod
    def red(cls, t):     return cls._wrap("1;31", t)
    @classmethod
    def magenta(cls, t): return cls._wrap("35", t)


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


# ── Core: Parse & Build Hierarchy ────────────────────────────────────
def collect_files(paths: list[str], recursive: bool = True) -> list[str]:
    """Gather all .v / .sv / .svh files from the given paths."""
    extensions = {'.v', '.sv', '.svh', '.vh'}
    result = []
    for p in paths:
        path = Path(p)
        if path.is_file() and path.suffix in extensions:
            result.append(str(path))
        elif path.is_dir():
            glob_fn = path.rglob if recursive else path.glob
            for ext in extensions:
                result.extend(str(f) for f in glob_fn(f'*{ext}'))
    result.sort()
    return result


def build_hierarchy(
    files: list[str],
    top_module: Optional[str] = None,
    include_dirs: list[str] = None,
    defines: list[str] = None,
) -> tuple[list[InstanceNode], list[str]]:
    """
    Parse the given files with pyslang, elaborate, and return the
    instance hierarchy tree(s) plus any diagnostic messages.
    """
    comp = ast.Compilation()

    for f in files:
        try:
            tree = SyntaxTree.fromFile(f)
            comp.addSyntaxTree(tree)
        except Exception as e:
            print(f"Warning: could not parse {f}: {e}", file=sys.stderr)

    # Collect diagnostics
    diag_messages = []
    try:
        diags = comp.getAllDiagnostics()
        for d in diags:
            diag_messages.append(str(d))
    except Exception:
        pass

    root = comp.getRoot()

    # ── Single-pass instance collection ──
    all_instances = []
    def collect(sym):
        all_instances.append(sym)
        return None  # continue traversal

    root.visit(lookup_table={ast.SymbolKind.Instance: collect})

    # Build path→instance and path→node maps first so parent lookup does not
    # depend on traversal order.
    path_to_inst = {}
    node_map = {}      # path → InstanceNode

    for inst in all_instances:
        path = inst.hierarchicalPath
        path_to_inst[path] = inst

        # Extract parameters
        params = {}
        try:
            for p in inst.body.parameters:
                params[p.name] = str(p.value)
        except Exception:
            pass

        node = InstanceNode(
            inst_name=inst.name,
            module_name=inst.body.name,
            hier_path=path,
            params=params,
            is_interface=inst.isInterface if hasattr(inst, 'isInterface') else False,
        )
        node_map[path] = node

    children_map = {}  # parent_path → [InstanceNode]
    for path, node in node_map.items():
        # Find the nearest ancestor that is also a collected instance
        parts = path.split('.')
        parent_path = None
        parent_len = 0
        for i in range(len(parts) - 1, 0, -1):
            candidate = '.'.join(parts[:i])
            if candidate in node_map:
                parent_path = candidate
                parent_len = i
                break

        if parent_path is not None:
            generated_scope = '.'.join(parts[parent_len:-1])
            if generated_scope:
                node.generated_scope = generated_scope
            children_map.setdefault(parent_path, []).append(node)

    # Wire up children
    for path, children in children_map.items():
        if path in node_map:
            node_map[path].children = children

    # Identify top-level instances
    tops = []
    for inst in root.topInstances:
        path = inst.hierarchicalPath
        if path in node_map:
            if top_module is None or inst.body.name == top_module:
                tops.append(node_map[path])

    if not tops and top_module:
        # Try matching by instance name
        for inst in root.topInstances:
            path = inst.hierarchicalPath
            if path in node_map and inst.name == top_module:
                tops.append(node_map[path])

    if not tops:
        for inst in root.topInstances:
            path = inst.hierarchicalPath
            if path in node_map:
                tops.append(node_map[path])

    return tops, diag_messages


# ── Output: Tree Format ──────────────────────────────────────────────
def format_node_label(node: InstanceNode, show_params: bool = True) -> str:
    """Format a single node's display label."""
    inst = Color.cyan(node.inst_name)
    mod = Color.yellow(node.module_name)
    label = f"{inst} : {mod}"

    if show_params and node.params:
        pstr = ', '.join(f"{k}={v}" for k, v in node.params.items())
        label += f" {Color.dim(f'#({pstr})')}"

    if node.is_interface:
        label += f" {Color.magenta('[interface]')}"

    if node.generated_scope:
        label += f" {Color.dim('← ' + node.generated_scope)}"

    return label


def print_tree(
    node: InstanceNode,
    prefix: str = "",
    is_last: bool = True,
    is_root: bool = False,
    max_depth: int = -1,
    current_depth: int = 0,
    show_params: bool = True,
    show_path: bool = False,
    counters: dict = None,
):
    """Recursively print one node and its children in tree format."""
    if counters is None:
        counters = {'instances': 0, 'modules': set()}

    counters['instances'] += 1
    counters['modules'].add(node.module_name)

    # ── Print this node ──
    label = format_node_label(node, show_params)
    if show_path:
        label += f"  {Color.dim(node.hier_path)}"

    if is_root:
        root_label = f"{Color.green(node.inst_name)} : {Color.yellow(node.module_name)}"
        if show_params and node.params:
            pstr = ', '.join(f"{k}={v}" for k, v in node.params.items())
            root_label += f" {Color.dim('#(' + pstr + ')')}"
        print(root_label)
    else:
        connector = "└── " if is_last else "├── "
        print(f"{prefix}{connector}{label}")

    # ── Depth limit ──
    if max_depth >= 0 and current_depth >= max_depth:
        if node.children:
            child_prefix = prefix + ("    " if is_last else "│   ")
            if not is_root:
                print(f"{child_prefix}{Color.dim(f'... ({len(node.children)} children)')}")
            else:
                print(f"    {Color.dim(f'... ({len(node.children)} children)')}")
        return

    # ── Children ──
    if is_root:
        child_prefix = ""
    else:
        child_prefix = prefix + ("    " if is_last else "│   ")

    for i, child in enumerate(node.children):
        print_tree(
            child,
            prefix=child_prefix,
            is_last=(i == len(node.children) - 1),
            is_root=False,
            max_depth=max_depth,
            current_depth=current_depth + 1,
            show_params=show_params,
            show_path=show_path,
            counters=counters,
        )

    return counters


# ── Output: JSON Format ─────────────────────────────────────────────
def node_to_dict(node: InstanceNode, max_depth: int = -1, depth: int = 0) -> dict:
    """Convert a hierarchy tree to a JSON-serializable dict."""
    d = {
        'instance': node.inst_name,
        'module': node.module_name,
        'path': node.hier_path,
    }
    if node.params:
        d['parameters'] = node.params
    if node.is_interface:
        d['is_interface'] = True
    if node.generated_scope:
        d['generated_scope'] = node.generated_scope

    if max_depth < 0 or depth < max_depth:
        if node.children:
            d['children'] = [node_to_dict(c, max_depth, depth + 1) for c in node.children]

    return d


# ── Output: Statistics ───────────────────────────────────────────────
def collect_stats(node: InstanceNode, stats: dict = None) -> dict:
    """Recursively collect hierarchy statistics."""
    if stats is None:
        stats = {
            'total_instances': 0,
            'unique_modules': set(),
            'module_counts': {},
            'max_depth': 0,
            'leaf_count': 0,
        }

    stats['total_instances'] += 1
    stats['unique_modules'].add(node.module_name)
    stats['module_counts'][node.module_name] = stats['module_counts'].get(node.module_name, 0) + 1

    if not node.children:
        stats['leaf_count'] += 1

    for child in node.children:
        collect_stats(child, stats)

    return stats


def compute_depth(node: InstanceNode) -> int:
    if not node.children:
        return 0
    return 1 + max(compute_depth(c) for c in node.children)


def print_stats(tops: list[InstanceNode]):
    """Print summary statistics for the hierarchy."""
    stats = {'total_instances': 0, 'unique_modules': set(), 'module_counts': {}, 'leaf_count': 0}
    max_depth = 0
    for top in tops:
        collect_stats(top, stats)
        d = compute_depth(top)
        max_depth = max(max_depth, d)

    print(f"\n{'─' * 50}")
    print(f"  {Color.green('Hierarchy Statistics')}")
    print(f"{'─' * 50}")
    print(f"  Top modules:      {', '.join(t.module_name for t in tops)}")
    print(f"  Total instances:  {stats['total_instances']}")
    print(f"  Unique modules:   {len(stats['unique_modules'])}")
    print(f"  Max depth:        {max_depth}")
    print(f"  Leaf instances:   {stats['leaf_count']}")

    print(f"\n  {Color.cyan('Module usage breakdown:')}")
    for mod, count in sorted(stats['module_counts'].items(), key=lambda x: -x[1]):
        bar = '█' * min(count, 40)
        print(f"    {mod:20s} {count:4d}  {Color.dim(bar)}")

    print(f"{'─' * 50}")


# ── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='rtl_tree — Verilog/SystemVerilog Hierarchy Viewer (powered by pyslang)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rtl_tree top.sv sub.sv          Parse files and show hierarchy
  rtl_tree -d ./rtl               Scan directory for .v/.sv files
  rtl_tree -d ./rtl --top cpu     Only show tree rooted at 'cpu'
  rtl_tree -d ./rtl --depth 2     Limit display depth
  rtl_tree -d ./rtl --stats       Show module usage statistics
  rtl_tree -d ./rtl --json        Output as JSON
  rtl_tree -d ./rtl --no-color    Disable colored output
  rtl_tree -d ./rtl --path        Show hierarchical paths
""")

    parser.add_argument('files', nargs='*', help='Verilog/SV source files')
    parser.add_argument('-d', '--dir', action='append', default=[],
                        help='Directory to scan for .v/.sv files (recursive)')
    parser.add_argument('--top', type=str, default=None,
                        help='Specify top module name (auto-detected if omitted)')
    parser.add_argument('--depth', type=int, default=-1,
                        help='Max display depth (-1 = unlimited)')
    parser.add_argument('--no-params', action='store_true',
                        help='Hide parameter values')
    parser.add_argument('--path', action='store_true',
                        help='Show hierarchical path for each instance')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON instead of tree')
    parser.add_argument('--stats', action='store_true',
                        help='Show hierarchy statistics')
    parser.add_argument('--no-color', action='store_true',
                        help='Disable colored output')
    parser.add_argument('--diag', action='store_true',
                        help='Show parser diagnostics/warnings')
    parser.add_argument('--flat', action='store_true',
                        help='Flat list of all instance paths')

    args = parser.parse_args()

    # ── Determine color mode ──
    if args.no_color or not sys.stdout.isatty() or args.json:
        Color.disable()

    # ── Collect files ──
    all_paths = list(args.files) + list(args.dir)
    if not all_paths:
        parser.print_help()
        sys.exit(1)

    files = collect_files(all_paths)
    if not files:
        print(f"Error: no .v/.sv/.svh files found in: {all_paths}", file=sys.stderr)
        sys.exit(1)

    # ── Parse & elaborate ──
    tops, diags = build_hierarchy(files, top_module=args.top)

    if args.diag and diags:
        print(f"\n{Color.red('Parser diagnostics:')}", file=sys.stderr)
        for d in diags[:20]:
            print(f"  {d}", file=sys.stderr)
        if len(diags) > 20:
            print(f"  ... and {len(diags)-20} more", file=sys.stderr)
        print(file=sys.stderr)

    if not tops:
        msg = "No top-level modules found."
        if args.top:
            msg += f" (filter: --top {args.top})"
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    # ── Output ──
    if args.json:
        data = [node_to_dict(t, args.depth) for t in tops]
        output = data[0] if len(data) == 1 else data
        print(json.dumps(output, indent=2, ensure_ascii=False))

    elif args.flat:
        # Flat list mode
        def flatten(node, depth=0):
            print(f"{'  ' * depth}{node.hier_path}  ({node.module_name})")
            for c in node.children:
                flatten(c, depth)  # no indentation in flat mode, just list
        for top in tops:
            flatten(top)

    else:
        # Tree mode (default)
        for i, top in enumerate(tops):
            if i > 0:
                print()
            print_tree(
                top,
                is_root=True,
                max_depth=args.depth,
                show_params=not args.no_params,
                show_path=args.path,
            )

    if args.stats:
        print_stats(tops)

    # Summary line
    if not args.json and not args.flat:
        total = sum(1 for _ in _walk(tops))
        mods = len(set(n.module_name for n in _walk(tops)))
        print(f"\n{Color.dim(f'{total} instances, {mods} unique modules, {len(files)} files parsed')}")


def _walk(nodes):
    """Yield all nodes in the tree(s)."""
    for n in nodes:
        yield n
        yield from _walk(n.children)


if __name__ == '__main__':
    main()

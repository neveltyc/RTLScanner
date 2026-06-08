#!/usr/bin/env python3
"""
rtl_ports — SystemVerilog Module Interface & Connectivity Report

Three modes:
  modules      — list each unique module with its port signature (default)
  instances    — list each instance and what each port is connected to
  check        — show only connectivity issues (unconnected ports,
                 width mismatches)

Output: pretty (default), --markdown, or --json.

Examples:
    rtl_ports -d ./rtl                          # all module interfaces
    rtl_ports -d ./rtl --module cpu_core        # filter by module name
    rtl_ports -d ./rtl --markdown > IFACE.md    # auto-generate docs
    rtl_ports -d ./rtl --instances              # connectivity per instance
    rtl_ports -d ./rtl --check                  # only issues

Install dependency:
    pip install pyslang
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import pyslang
    import pyslang.ast as ast
except ImportError:
    print("Error: pyslang is required.  pip install pyslang", file=sys.stderr)
    sys.exit(1)

from rtl_common import (
    Color,
    FileList,
    build_compilation,
    collect_filelist,
    parse_filelist,
    merge_filelists,
    filter_filelist,
    safe_str,
)


# ── Data Structures ──────────────────────────────────────────────────
@dataclass
class PortInfo:
    """One port on a module's interface."""
    name: str
    direction: str             # "input" | "output" | "inout" | "ref"
    type_str: str
    width: Optional[int]       # bits, or None for non-integral types
    is_interface: bool = False
    file: str = ""
    line: int = 0

    def to_dict(self):
        d = dict(name=self.name, direction=self.direction,
                 type=self.type_str, width=self.width)
        if self.is_interface:
            d['is_interface'] = True
        if self.file:
            d['file'] = self.file
            d['line'] = self.line
        return d


@dataclass
class ModuleInterface:
    """Port signature of one unique module."""
    name: str
    kind: str                  # "module" | "interface" | "program"
    ports: list = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    instance_count: int = 0
    file: str = ""
    line: int = 0

    def to_dict(self):
        return dict(module=self.name, kind=self.kind,
                    parameters=self.parameters,
                    instance_count=self.instance_count,
                    ports=[p.to_dict() for p in self.ports],
                    file=self.file, line=self.line)


@dataclass
class Connection:
    """One port connection at an instance site."""
    instance_path: str
    instance_name: str
    module_name: str
    port_name: str
    port_direction: str
    port_width: Optional[int]
    conn_text: str = ""        # source-rendered expression, "" if unconnected
    conn_width: Optional[int] = None
    is_unconnected: bool = False
    file: str = ""
    line: int = 0

    def to_dict(self):
        return dict(instance=self.instance_path, module=self.module_name,
                    port=self.port_name, direction=self.port_direction,
                    port_width=self.port_width,
                    connection=self.conn_text or None,
                    conn_width=self.conn_width,
                    unconnected=self.is_unconnected,
                    file=self.file, line=self.line)


@dataclass
class PortIssue:
    """A connectivity problem at an instance port."""
    kind: str                  # "unconnected" | "width_mismatch"
    severity: str              # "warning" | "note"
    instance_path: str
    port_name: str
    port_direction: str
    message: str
    file: str = ""
    line: int = 0

    def to_dict(self):
        return dict(kind=self.kind, severity=self.severity,
                    instance=self.instance_path, port=self.port_name,
                    direction=self.port_direction, message=self.message,
                    file=self.file, line=self.line)


# ── Direction / kind helpers ─────────────────────────────────────────
_DIR_NAMES = {
    ast.ArgumentDirection.In: "input",
    ast.ArgumentDirection.Out: "output",
    ast.ArgumentDirection.InOut: "inout",
    ast.ArgumentDirection.Ref: "ref",
}


def _dir_name(d):
    return _DIR_NAMES.get(d, str(d).split('.')[-1].lower())


def _def_kind(definition_kind):
    """DefinitionSymbol.definitionKind → short label."""
    name = getattr(definition_kind, 'name', str(definition_kind))
    return name.lower()


# ── Core: Port Analyzer ──────────────────────────────────────────────
class PortAnalyzer:
    """Pulls module interfaces and instance connections from a Compilation."""

    def __init__(self, compilation):
        self._comp = compilation
        self._sm = compilation.sourceManager
        self._root = compilation.getRoot()
        self._cwd = Path.cwd().resolve()
        self._instances = self._collect_instances()
        self._modules = self._collect_modules()

    # ── helpers ───────────────────────────────────────────────────────

    def _rel(self, name: str) -> str:
        """Path readable from cwd; fall back to absolute when ../ chain is long."""
        if not name:
            return name
        try:
            p = Path(name).resolve()
            import os
            rel = os.path.relpath(p, self._cwd)
            return rel if not rel.startswith(os.pardir + os.sep) else p.as_posix()
        except Exception:
            return name

    def _loc(self, sym_or_range):
        try:
            loc = getattr(sym_or_range, 'start', None) or sym_or_range.location
            fn = self._rel(safe_str(self._sm.getFileName(loc)))
            return fn, int(self._sm.getLineNumber(loc))
        except Exception:
            return "", 0

    def _port_width(self, type_obj):
        try:
            return int(type_obj.bitWidth) or None
        except Exception:
            return None

    def _collect_instances(self):
        out = []
        def coll(s):
            out.append(s)
        try:
            self._root.visit(lookup_table={ast.SymbolKind.Instance: coll})
        except Exception:
            pass
        return out

    def _make_port_info(self, port_sym):
        is_iface = getattr(port_sym, 'kind', None) == ast.SymbolKind.InterfacePort
        try:
            dirn = _dir_name(port_sym.direction)
        except Exception:
            dirn = "input"
        try:
            tstr = safe_str(port_sym.type, "")
        except Exception:
            tstr = ""
        width = self._port_width(getattr(port_sym, 'type', None))
        f, ln = self._loc(port_sym)
        return PortInfo(name=safe_str(port_sym.name, "?"),
                        direction=dirn, type_str=tstr, width=width,
                        is_interface=is_iface, file=f, line=ln)

    def _collect_modules(self):
        """Build one ModuleInterface per unique elaborated module body."""
        seen = {}
        for inst in self._instances:
            try:
                body = inst.body
                mname = safe_str(body.name, "?")
            except Exception:
                continue
            mi = seen.get(mname)
            if mi is None:
                kind = "interface" if getattr(inst, 'isInterface', False) else "module"
                # Use the InstanceBodySymbol's location for the module file/line
                f, ln = self._loc(body)
                mi = ModuleInterface(name=mname, kind=kind, file=f, line=ln)
                try:
                    for p in body.portList:
                        mi.ports.append(self._make_port_info(p))
                except Exception:
                    pass
                try:
                    mi.parameters = {p.name: safe_str(p.value, "?")
                                     for p in (body.parameters or [])}
                except Exception:
                    pass
                seen[mname] = mi
            mi.instance_count += 1

        # Definitions never instantiated: include with port list extracted
        # from syntax (best-effort — we report instance_count=0).
        try:
            inst_names = set(seen)
            for d in self._comp.getDefinitions():
                dname = safe_str(d.name, "")
                if not dname or dname in inst_names:
                    continue
                kind = _def_kind(getattr(d, 'definitionKind', 'module'))
                f, ln = self._loc(d)
                mi = ModuleInterface(name=dname, kind=kind, file=f, line=ln)
                # We can't easily extract ports without elaboration — leave empty
                # but still report the module exists. (instance_count stays 0.)
                seen[dname] = mi
        except Exception:
            pass
        return seen

    # ── expression rendering ─────────────────────────────────────────

    def _unwrap_for_user_expr(self, e):
        """Peel implicit conversions to reach the user-written expression."""
        for _ in range(8):  # bounded
            if e is None:
                return e
            op = getattr(e, 'operand', None)
            if op is not None and hasattr(e, 'conversionKind'):
                e = op
                continue
            break
        return e

    def _conn_target(self, expression, direction):
        """For an output port connection, the assignment LHS is the user
        net; for inputs/inouts, the expression itself is the target.
        """
        if expression is None:
            return None
        if direction == ast.ArgumentDirection.Out and hasattr(expression, 'left'):
            return expression.left
        return expression

    def _render_expr(self, expression):
        """Best-effort source-text rendering of a connection expression."""
        if expression is None:
            return ""
        # Try the simple .syntax — works for named values, concat, literals
        e = expression
        for _ in range(8):
            syn = getattr(e, 'syntax', None)
            if syn is not None:
                return safe_str(syn, "").strip()
            inner = getattr(e, 'operand', None) or getattr(e, 'left', None)
            if inner is None:
                break
            e = inner
        # Fallback: read raw source via SourceRange
        try:
            sr = expression.sourceRange
            buf = self._sm.getSourceText(sr.start.buffer)
            ln = self._sm.getLineNumber(sr.start)
            ca = self._sm.getColumnNumber(sr.start)
            cb = self._sm.getColumnNumber(sr.end)
            lines = buf.splitlines()
            if 0 < ln <= len(lines):
                return lines[ln - 1][ca - 1:cb - 1].strip()
        except Exception:
            pass
        return f"<{type(expression).__name__}>"

    # ── connections & issues ──────────────────────────────────────────

    def _make_connection(self, inst, pc):
        port = pc.port
        try:
            mname = safe_str(inst.body.name, "?")
            ipath = safe_str(inst.hierarchicalPath, inst.name)
        except Exception:
            mname, ipath = "?", safe_str(inst.name, "?")
        pwidth = self._port_width(getattr(port, 'type', None))
        f, ln = self._loc(inst)

        if pc.expression is None:
            return Connection(
                instance_path=ipath, instance_name=safe_str(inst.name, "?"),
                module_name=mname, port_name=safe_str(port.name, "?"),
                port_direction=_dir_name(port.direction),
                port_width=pwidth, is_unconnected=True,
                file=f, line=ln,
            )

        target = self._unwrap_for_user_expr(
            self._conn_target(pc.expression, port.direction))
        cwidth = self._port_width(getattr(target, 'type', None)) if target is not None else None
        ctext = self._render_expr(pc.expression)
        return Connection(
            instance_path=ipath, instance_name=safe_str(inst.name, "?"),
            module_name=mname, port_name=safe_str(port.name, "?"),
            port_direction=_dir_name(port.direction),
            port_width=pwidth, conn_text=ctext, conn_width=cwidth,
            file=f, line=ln,
        )

    def all_connections(self):
        out = []
        for inst in self._instances:
            try:
                pcs = inst.portConnections
            except Exception:
                continue
            for pc in pcs:
                try:
                    out.append(self._make_connection(inst, pc))
                except Exception:
                    continue
        return out

    def issues(self):
        """Connectivity problems: unconnected ports + width mismatches."""
        out = []
        for c in self.all_connections():
            if c.is_unconnected:
                # Flag inputs as warnings (genuinely undriven), outputs as notes
                # (intentionally-discarded outputs are common and benign).
                if c.port_direction == "input":
                    sev = "warning"
                    msg = f"input port '{c.port_name}' is unconnected"
                else:
                    sev = "note"
                    msg = f"{c.port_direction} port '{c.port_name}' is unconnected"
                out.append(PortIssue(
                    kind="unconnected", severity=sev,
                    instance_path=c.instance_path, port_name=c.port_name,
                    port_direction=c.port_direction, message=msg,
                    file=c.file, line=c.line,
                ))
            elif (c.port_width and c.conn_width
                  and c.port_width != c.conn_width):
                out.append(PortIssue(
                    kind="width_mismatch", severity="warning",
                    instance_path=c.instance_path, port_name=c.port_name,
                    port_direction=c.port_direction,
                    message=(f"width mismatch on .{c.port_name}: "
                             f"port is {c.port_width} bits, "
                             f"connection is {c.conn_width} bits"),
                    file=c.file, line=c.line,
                ))
        return out

    # ── public ────────────────────────────────────────────────────────

    def modules(self, name_glob=None):
        items = list(self._modules.values())
        if name_glob:
            items = [m for m in items if fnmatch.fnmatch(m.name, name_glob)]
        items.sort(key=lambda m: m.name)
        return items

    def connections(self, instance_glob=None, module_glob=None):
        # Preserve port declaration order within each instance — only sort
        # by instance path, stably.
        out = self.all_connections()
        if instance_glob:
            out = [c for c in out
                   if fnmatch.fnmatch(c.instance_path, instance_glob)
                   or fnmatch.fnmatch(c.instance_name, instance_glob)]
        if module_glob:
            out = [c for c in out if fnmatch.fnmatch(c.module_name, module_glob)]
        out.sort(key=lambda c: c.instance_path)
        return out

    def filter_issues(self, instance_glob=None, module_glob=None):
        items = self.issues()
        # Mirror connections() filtering, but issues only have instance_path/port.
        if instance_glob:
            items = [i for i in items
                     if fnmatch.fnmatch(i.instance_path, instance_glob)]
        if module_glob:
            # Look up the module via the matching connection
            mods = {c.instance_path: c.module_name for c in self.all_connections()}
            items = [i for i in items
                     if fnmatch.fnmatch(mods.get(i.instance_path, ""), module_glob)]
        items.sort(key=lambda i: i.instance_path)
        return items


# ── Output ───────────────────────────────────────────────────────────
_DIR_GLYPH = {"input": "→", "output": "←", "inout": "↔", "ref": "↔"}


def _fmt_width(p):
    return f"[{p.width-1}:0]" if p.width and p.width > 1 else ""


def print_modules_pretty(modules):
    if not modules:
        print(Color.dim("(no modules)"))
        return
    for mi in modules:
        head = f"{Color.bold(mi.name)}"
        if mi.kind != "module":
            head += f"  {Color.magenta('[' + mi.kind + ']')}"
        head += f"  {Color.dim('×' + str(mi.instance_count))}"
        if mi.file:
            head += f"  {Color.dim(mi.file + ':' + str(mi.line))}"
        print(f"\n{head}")
        if mi.parameters:
            ps = ", ".join(f"{k}={v}" for k, v in mi.parameters.items())
            print(f"  {Color.dim('#(' + ps + ')')}")
        if not mi.ports:
            print(f"  {Color.dim('(no ports — definition not elaborated)')}")
            continue
        widest_name = max(len(p.name) for p in mi.ports)
        widest_type = max(len(p.type_str + _fmt_width(p)) for p in mi.ports)
        for p in mi.ports:
            glyph = _DIR_GLYPH.get(p.direction, "·")
            dir_lbl = Color.yellow(f"{p.direction:6s}")
            tstr = p.type_str
            print(f"  {glyph} {dir_lbl} "
                  f"{Color.cyan(p.name.ljust(widest_name))}  "
                  f"{Color.dim(tstr.ljust(widest_type))}")


def print_connections_pretty(conns):
    if not conns:
        print(Color.dim("(no connections)"))
        return
    cur = None
    for c in conns:
        if c.instance_path != cur:
            cur = c.instance_path
            print(f"\n{Color.bold(c.instance_path)}  "
                  f": {Color.yellow(c.module_name)}")
        dir_lbl = Color.yellow(f"{c.port_direction:6s}")
        port = Color.cyan(c.port_name)
        if c.is_unconnected:
            body = f".{port}()  {Color.red('— unconnected')}"
        else:
            rhs = c.conn_text or Color.dim("?")
            body = f".{port}({rhs})"
        width_note = ""
        if (c.port_width and c.conn_width
                and c.port_width != c.conn_width):
            width_note = Color.red(f"  ⚠ {c.port_width}≠{c.conn_width}")
        print(f"  {dir_lbl} {body}{width_note}")


def print_issues_pretty(issues):
    if not issues:
        print(Color.green("✓ No connectivity issues."))
        return
    cur = None
    for i in issues:
        if i.instance_path != cur:
            cur = i.instance_path
            print(f"\n{Color.bold(i.instance_path)}")
        sev_fn = Color.red if i.severity == "warning" else Color.cyan
        loc = f"{i.file}:{i.line}" if i.file else ""
        print(f"  {sev_fn(i.severity):8s}  {i.message}  "
              f"{Color.dim('[' + i.kind + ']' + ('  ' + loc if loc else ''))}")


# ── Markdown ─────────────────────────────────────────────────────────
def render_modules_markdown(modules):
    lines = ["# Module Interfaces", ""]
    for mi in modules:
        lines.append(f"## `{mi.name}`")
        meta = [f"kind: `{mi.kind}`", f"instances: {mi.instance_count}"]
        if mi.file:
            meta.append(f"source: `{mi.file}:{mi.line}`")
        lines.append(" · ".join(meta))
        lines.append("")
        if mi.parameters:
            lines.append("**Parameters**: " +
                         ", ".join(f"`{k}={v}`" for k, v in mi.parameters.items()))
            lines.append("")
        if not mi.ports:
            lines.append("_(no ports — definition not elaborated)_")
            lines.append("")
            continue
        lines.append("| Direction | Name | Type | Width |")
        lines.append("|-----------|------|------|-------|")
        for p in mi.ports:
            w = str(p.width) if p.width else "—"
            lines.append(f"| {p.direction} | `{p.name}` | `{p.type_str}` | {w} |")
        lines.append("")
    return "\n".join(lines)


def render_connections_markdown(conns):
    lines = ["# Instance Connectivity", ""]
    cur = None
    for c in conns:
        if c.instance_path != cur:
            cur = c.instance_path
            lines.append(f"## `{c.instance_path}` — `{c.module_name}`")
            lines.append("")
            lines.append("| Direction | Port | Connection | Width |")
            lines.append("|-----------|------|------------|-------|")
        rhs = "_(unconnected)_" if c.is_unconnected else f"`{c.conn_text}`"
        w = ""
        if c.port_width and c.conn_width:
            if c.port_width != c.conn_width:
                w = f"⚠ {c.port_width} ≠ {c.conn_width}"
            else:
                w = str(c.port_width)
        elif c.port_width:
            w = str(c.port_width)
        lines.append(f"| {c.port_direction} | `{c.port_name}` | {rhs} | {w} |")
    if cur is None:
        lines.append("_(no connections)_")
    return "\n".join(lines) + "\n"


def render_issues_markdown(issues):
    lines = ["# Port Connectivity Issues", ""]
    if not issues:
        lines.append("_None found._")
        return "\n".join(lines) + "\n"
    lines.append("| Severity | Instance | Port | Direction | Issue |")
    lines.append("|----------|----------|------|-----------|-------|")
    for i in issues:
        lines.append(f"| {i.severity} | `{i.instance_path}` | "
                     f"`{i.port_name}` | {i.port_direction} | {i.message} |")
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        prog='rtl-ports',
        description='rtl-ports — SV Module Interface & Connectivity Report',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  rtl-ports -d ./rtl                  Module interface signatures (default)
  rtl-ports -d ./rtl --instances      Connectivity per instance
  rtl-ports -d ./rtl --check          Only connectivity issues

Filtering:
  rtl-ports -d ./rtl --module cpu_*               # filter by module glob
  rtl-ports -d ./rtl --instances --instance 'top.u_cpu*'

Output:
  rtl-ports -d ./rtl --markdown > docs/IFACE.md   # auto-generate docs
  rtl-ports -d ./rtl --json
""")
    p.add_argument('files', nargs='*', help='Verilog/SV source files')
    p.add_argument('-d', '--dir', action='append', default=[], metavar='DIR',
                   help='Directory to scan recursively (repeatable)')

    fl = p.add_argument_group('filelist')
    fl.add_argument('--filelist', '-f', action='append', default=[], metavar='FILE',
                    help='VCS-style .f filelist (repeatable)')
    fl.add_argument('--filelist-root', '--projpath', dest='filelist_root',
                    default='.', metavar='DIR',
                    help='Base path for filelist relative paths (default: .)')
    fl.add_argument('--filelist-prefix', default='${PROJPATH}', metavar='STR',
                    help='Prefix substituted for filelist path variables '
                         '(default: ${PROJPATH})')
    fl.add_argument('--exclude', action='append', default=[], metavar='GLOB',
                    help='Exclude paths matching glob (repeatable)')

    md = p.add_argument_group('mode')
    g = md.add_mutually_exclusive_group()
    g.add_argument('--modules', action='store_true',
                   help='List module interfaces (default)')
    g.add_argument('--instances', action='store_true',
                   help='List instance connectivity')
    g.add_argument('--check', action='store_true',
                   help='Show only connectivity issues')

    ft = p.add_argument_group('filters')
    ft.add_argument('--module', default=None, metavar='GLOB',
                    help='Filter modules by name')
    ft.add_argument('--instance', default=None, metavar='GLOB',
                    help='Filter instances by hierarchical path')

    out = p.add_argument_group('output')
    out.add_argument('--markdown', action='store_true',
                     help='Render as Markdown tables')
    out.add_argument('--json', action='store_true', help='JSON output')
    out.add_argument('--no-color', action='store_true', help='Disable ANSI colors')
    out.add_argument('--werror', action='store_true',
                     help='Exit 1 when --check reports any warning')

    a = p.parse_args()

    if a.no_color or not sys.stdout.isatty() or a.json or a.markdown:
        Color.disable()

    # Resolve sources (same pattern as the other tools)
    all_paths = list(a.files) + list(a.dir)
    if not all_paths and not a.filelist:
        p.print_help()
        sys.exit(2)

    fl_root = Path(a.filelist_root).expanduser().resolve()
    parsed = []
    for f in a.filelist:
        try:
            parsed.append(parse_filelist(f, fl_root, prefix=a.filelist_prefix))
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)
    scanned = collect_filelist(all_paths, excludes=a.exclude, root=fl_root) if all_paths else FileList()
    filelist = filter_filelist(merge_filelists(*parsed, scanned), a.exclude, fl_root)
    if not filelist.sources:
        print("Error: no .v/.sv source files found", file=sys.stderr)
        sys.exit(2)

    comp, _ = build_compilation(filelist.sources, filelist.include_dirs, filelist.defines)
    pa = PortAnalyzer(comp)

    # Dispatch
    if a.instances:
        items = pa.connections(instance_glob=a.instance, module_glob=a.module)
        if a.json:
            print(json.dumps({'mode': 'instances',
                              'connections': [c.to_dict() for c in items]},
                             indent=2, ensure_ascii=False))
        elif a.markdown:
            print(render_connections_markdown(items))
        else:
            print_connections_pretty(items)
        sys.exit(0)

    if a.check:
        items = pa.filter_issues(instance_glob=a.instance, module_glob=a.module)
        if a.json:
            print(json.dumps({'mode': 'check',
                              'issues': [i.to_dict() for i in items]},
                             indent=2, ensure_ascii=False))
        elif a.markdown:
            print(render_issues_markdown(items))
        else:
            print_issues_pretty(items)
        has_warn = any(i.severity == "warning" for i in items)
        sys.exit(1 if (a.werror and has_warn) else 0)

    # Default: modules mode
    items = pa.modules(name_glob=a.module)
    if a.json:
        print(json.dumps({'mode': 'modules',
                          'modules': [m.to_dict() for m in items]},
                         indent=2, ensure_ascii=False))
    elif a.markdown:
        print(render_modules_markdown(items))
    else:
        print_modules_pretty(items)
    sys.exit(0)


if __name__ == '__main__':
    main()

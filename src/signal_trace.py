#!/usr/bin/env python3
"""
signal_trace — Verilog/SystemVerilog Signal Driver & Load Analyzer

Designed for the debug/simulation workflow where a VCS-style filelist
already exists.  Traces the single driver and all loads of a signal.

Primary usage (with filelist):
    signal_trace --filelist rtl.f --signal q --scope top.u_dp0
    signal_trace --filelist rtl.f --signal clk --scope top --filter 'u_dp*'
    signal_trace --filelist rtl.f --scope top.u_dp0 --list
    signal_trace --filelist rtl.f --scope top.u_dp0 --all

Also works with directory scan:
    signal_trace -d ./rtl --signal q --scope top.u_dp0

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
    import pyslang.ast as ast
    import pyslang.analysis as analysis
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

import agent_json
from agent_json import AgentError, Envelope, filter_command, emit


# ── Display glyphs ───────────────────────────────────────────────────
# Defined as named constants so they can be referenced inside f-string
# expressions.  Python < 3.12 forbids backslash escapes (e.g. "◀")
# inside the "{...}" part of an f-string, which would be a SyntaxError.
GLYPH_DRIVER = "◀"
GLYPH_LOADS = "▶"
GLYPH_CROSS = "⇅"
GLYPH_PORT = "↕"
GLYPH_WARN = "⚠"
GLYPH_DASH = "—"
GLYPH_HR = "─"
GLYPH_ARROW_L = "←"
GLYPH_CORNER = "└"


# ── Data Structures ──────────────────────────────────────────────────
@dataclass
class DriverInfo:
    """One driver of a signal (RTL convention: exactly one per net)."""
    kind: str               # "continuous" | "procedural"
    source: str             # "always_ff" | "assign" | "input_port" | "output_port" …
    description: str
    symbol_name: str
    symbol_kind: str
    scope_path: str = ""
    file: str = ""
    line: int = 0

    def to_dict(self):
        d = dict(kind=self.kind, source=self.source, description=self.description,
                 symbol=self.symbol_name, symbol_kind=self.symbol_kind,
                 scope_path=self.scope_path)
        if self.file:
            d['file'] = self.file
            d['line'] = self.line
        return d


@dataclass
class LoadInfo:
    """One load (reader) of a signal."""
    kind: str               # "port_connection" | "continuous_assign" | "procedural"
    description: str
    instance_name: str = ""
    port_name: str = ""
    port_direction: str = ""
    scope_path: str = ""
    file: str = ""
    line: int = 0

    def to_dict(self):
        d = dict(kind=self.kind, description=self.description,
                 scope_path=self.scope_path)
        if self.instance_name:
            d['instance'] = self.instance_name
        if self.port_name:
            d['port'] = self.port_name
            d['direction'] = self.port_direction
        if self.file:
            d['file'] = self.file
            d['line'] = self.line
        return d


@dataclass
class TraceResult:
    """Complete trace result for one signal."""
    signal_name: str
    signal_type: str
    signal_kind: str
    scope_path: str
    scope_module: str
    driver: Optional[DriverInfo] = None       # RTL: single driver
    extra_drivers: list = field(default_factory=list)  # multi-driver → warning
    loads: list = field(default_factory=list)
    cross_hier: list = field(default_factory=list)

    @property
    def all_drivers(self):
        d = ([self.driver] if self.driver else []) + self.extra_drivers
        return d

    def filtered_loads(self, pattern=None):
        if not pattern:
            return list(self.loads)
        return [ld for ld in self.loads
                if fnmatch.fnmatch(ld.instance_name, pattern)]

    def to_dict(self, load_filter=None):
        loads = self.filtered_loads(load_filter)
        d = dict(signal=self.signal_name, type=self.signal_type,
                 kind=self.signal_kind, scope=self.scope_path,
                 module=self.scope_module)
        d['driver'] = self.driver.to_dict() if self.driver else None
        if self.extra_drivers:
            d['extra_drivers'] = [x.to_dict() for x in self.extra_drivers]
            d['multi_driver_warning'] = True
        d['loads'] = [ld.to_dict() for ld in loads]
        d['load_count'] = len(loads)
        if self.cross_hier:
            d['cross_hierarchy'] = self.cross_hier
        return d

    def pretty_print(self, load_filter=None):
        C = Color
        loads = self.filtered_loads(load_filter)

        print(f"Signal: {C.bold(self.signal_name)}  {C.dim(self.signal_type)}")
        print(f"Scope:  {C.cyan(self.scope_path)}  [{C.yellow(self.scope_module)}]")
        print("\u2500" * 60)

        # ── Driver (singular in RTL) ──
        drivers = self.all_drivers
        if not drivers:
            print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}  {C.dim('(none ' + GLYPH_DASH + ' undriven)')}")
        elif len(drivers) == 1:
            d = drivers[0]
            loc = f"  {C.dim(d.file + ':' + str(d.line))}" if d.file else ""
            print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}")
            print(f"    {GLYPH_ARROW_L} {d.description}{loc}")
        else:
            print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}  {C.red(GLYPH_WARN + ' MULTI-DRIVER (' + str(len(drivers)) + ')')}")
            for d in drivers:
                loc = f"  {C.dim(d.file + ':' + str(d.line))}" if d.file else ""
                print(f"    \u2190 {d.description}{loc}")

        # ── Loads (many) ──
        hdr = f"\n  {C.green(GLYPH_LOADS + ' LOADS')} ({len(loads)})"
        if load_filter:
            hdr += f"  {C.dim('filter: ' + load_filter)}"
        print(hdr)

        if not loads:
            print(f"    {C.dim('(none found)')}")
        else:
            by_kind = {}
            for ld in loads:
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
                    print(f"    \u2192 {ld.description}{loc}")

        if self.cross_hier:
            print(f"\n  {C.blue(GLYPH_CROSS + ' CROSS-HIERARCHY')} ({len(self.cross_hier)})")
            for ch in self.cross_hier:
                print(f"    {ch}")
        print()


# ── Core: Signal Tracer ─────────────────────────────────────────────
class SignalTracer:
    """Analyzes signal drivers and loads in an elaborated SV design."""

    def __init__(self, compilation):
        self._comp = compilation
        self._root = compilation.getRoot()
        self._sm = compilation.sourceManager
        _ = compilation.getAllDiagnostics()
        self._mgr = analysis.AnalysisManager()
        self._mgr.analyze(compilation)

    # ── helpers ───────────────────────────────────────────────────────

    def _resolve_scope(self, scope_path):
        parts = scope_path.split('.')
        current = None
        for top in self._root.topInstances:
            try:
                if top.name == parts[0] or top.body.name == parts[0]:
                    current = top
                    break
            except Exception:
                continue
        if current is None:
            return None
        for part in parts[1:]:
            found = current.body.find(part)
            if found is None or found.kind != ast.SymbolKind.Instance:
                return None
            current = found
        return current

    def _find_signal(self, name, body):
        sym = body.find(name)
        if sym and sym.kind in (ast.SymbolKind.Net, ast.SymbolKind.Variable):
            return sym
        return None

    def _loc_range(self, sr):
        try:
            return str(self._sm.getFileName(sr.start)), int(self._sm.getLineNumber(sr.start))
        except Exception:
            return "", 0

    def _loc_sym(self, sym):
        try:
            loc = sym.location
            return str(self._sm.getFileName(loc)), int(self._sm.getLineNumber(loc))
        except Exception:
            return "", 0

    def _refs(self, expr, target):
        """Does *expr* reference *target*?"""
        hit = []
        def _w(e):
            if hasattr(e, 'symbol') and self._same_symbol(e.symbol, target):
                hit.append(True)
        try:
            expr.visit(f=_w)
        except Exception:
            pass
        return bool(hit)

    def _scope_visit(self, body, kinds):
        """Visit body collecting per kinds dict, skipping child instances."""
        table = dict(kinds)
        table.setdefault(ast.SymbolKind.Instance, lambda _: ast.VisitAction.Skip)
        body.visit(lookup_table=table)

    def _symbol_key(self, sym):
        try:
            hp = safe_str(sym.hierarchicalPath, "")
            if hp:
                return hp
        except Exception:
            pass
        kind = safe_str(getattr(sym, 'kind', ''), '')
        name = safe_str(getattr(sym, 'name', ''), '')
        return f"{kind}:{name}"

    def _same_symbol(self, a, b):
        return self._symbol_key(a) == self._symbol_key(b)

    def _proc_label(self, proc):
        try:
            pk = safe_str(proc.analyzedSymbol.procedureKind, "")
        except Exception:
            return "procedural block"
        labels = {
            "AlwaysFF": "always_ff",
            "AlwaysComb": "always_comb",
            "AlwaysLatch": "always_latch",
            "Always": "always",
            "Initial": "initial",
            "Final": "final",
        }
        for key, label in labels.items():
            if key in pk:
                return label
        return "procedural block"

    def _proc_reads_symbol(self, proc, symbol):
        try:
            if any(self._same_symbol(rr.symbol, symbol)
                   for rr in (proc.readSet or [])):
                return True
        except Exception:
            pass

        # Clocks are used in timing controls rather than expression read sets.
        for tc in getattr(proc, 'timingControls', []) or []:
            timing = getattr(tc, 'timing', None)
            if timing is not None and self._refs(timing, symbol):
                return True
        return False

    # ── driver analysis ──────────────────────────────────────────────

    def _analyze_drivers(self, symbol, scope_inst):
        C = Color
        infos = []
        for d in self._mgr.getDrivers(symbol):
            cs = d.containingSymbol
            cs_name = cs.name or "(anonymous)"
            f, ln = self._loc_range(d.sourceRange)
            kind = "continuous" if d.kind == analysis.DriverKind.Continuous else "procedural"

            _SM = {
                analysis.DriverSource.AlwaysFF: "always_ff",
                analysis.DriverSource.AlwaysComb: "always_comb",
                analysis.DriverSource.AlwaysLatch: "always_latch",
                analysis.DriverSource.Initial: "initial",
                analysis.DriverSource.Final: "final",
                analysis.DriverSource.Always: "always",
                analysis.DriverSource.Subroutine: "subroutine",
                analysis.DriverSource.Other: "other",
            }
            source = _SM.get(d.source, str(d.source))

            if d.flags & analysis.DriverFlags.InputPort:
                desc = f"{C.magenta('input port')} \u2014 driven from parent scope"
                source = "input_port"
            elif d.flags & analysis.DriverFlags.OutputPort:
                desc = f"{C.yellow('output port')} of instance {C.cyan(cs_name)}"
                source = "output_port"
            elif kind == "procedural":
                desc = f"{C.yellow(source)} block"
                if cs_name:
                    desc += f" in {C.cyan(cs_name)}"
            else:
                desc = f"{C.yellow('assign')} (continuous)"
                source = "assign"

            sp = ""
            try:
                sp = cs.hierarchicalPath
            except Exception:
                pass
            infos.append(DriverInfo(kind=kind, source=source, description=desc,
                                    symbol_name=cs_name, symbol_kind=cs.kind.name,
                                    scope_path=sp, file=f, line=ln))
        return infos

    # ── load analysis ────────────────────────────────────────────────

    def _analyze_loads(self, symbol, body, scope_inst):
        C = Color
        loads = []

        # 1. Instance port connections
        insts = []
        def _ci(s):
            insts.append(s); return ast.VisitAction.Skip
        self._scope_visit(body, {ast.SymbolKind.Instance: _ci})

        for inst in insts:
            try:
                pcs = inst.portConnections
                inst_name = inst.name
                inst_path = inst.hierarchicalPath
            except Exception:
                continue
            for pc in pcs:
                try:
                    expr = pc.expression
                    if expr is None or not self._refs(expr, symbol):
                        continue
                    port = pc.port
                    if port.direction not in (ast.ArgumentDirection.In, ast.ArgumentDirection.InOut):
                        continue
                    dl = "input" if port.direction == ast.ArgumentDirection.In else "inout"
                    f, ln = self._loc_sym(inst)
                    desc = f"{C.cyan(inst_name)}.{C.yellow(port.name)} ({C.dim(dl)})"
                    loads.append(LoadInfo(
                        kind="port_connection", description=desc,
                        instance_name=inst_name, port_name=port.name,
                        port_direction=port.direction.name,
                        scope_path=inst_path, file=f, line=ln))
                except Exception:
                    continue

        # 2. Continuous assignments (signal on RHS)
        cas = []
        def _cca(s):
            cas.append(s); return ast.VisitAction.Skip
        self._scope_visit(body, {ast.SymbolKind.ContinuousAssign: _cca})

        for ca in cas:
            try:
                asgn = ca.assignment
            except Exception:
                continue
            rhs = getattr(asgn, 'right', None)
            lhs = getattr(asgn, 'left', None)
            if rhs and self._refs(rhs, symbol):
                lhs_n = lhs.symbol.name if lhs and hasattr(lhs, 'symbol') else "\u2026"
                f, ln = self._loc_sym(ca)
                desc = f"{C.yellow('assign')} \u2192 {C.cyan(lhs_n)}"
                loads.append(LoadInfo(
                    kind="continuous_assign", description=desc,
                    scope_path=scope_inst.hierarchicalPath if scope_inst else "",
                    file=f, line=ln))

        # 3. Procedural blocks (readSet excludes assignment LHS symbols)
        try:
            analyzed_scope = self._mgr.getAnalyzedScope(body)
            procedures = analyzed_scope.procedures if analyzed_scope is not None else []
        except Exception:
            procedures = []

        for proc in procedures:
            try:
                if proc.analyzedSymbol.kind != ast.SymbolKind.ProceduralBlock:
                    continue
                if not self._proc_reads_symbol(proc, symbol):
                    continue
                f, ln = self._loc_sym(proc.analyzedSymbol)
                desc = f"{C.yellow(self._proc_label(proc))}"
                loads.append(LoadInfo(
                    kind="procedural", description=desc,
                    scope_path=scope_inst.hierarchicalPath if scope_inst else "",
                    file=f, line=ln))
            except Exception:
                continue
        return loads

    # ── cross-hierarchy ──────────────────────────────────────────────

    def _trace_cross(self, symbol, scope_inst):
        C = Color
        conns = []
        for port in scope_inst.body.portList:
            isym = port.internalSymbol
            if isym is None or isym.name != symbol.name:
                continue
            dw = "input" if port.direction == ast.ArgumentDirection.In else "output"
            conns.append(f"{C.magenta(GLYPH_PORT + ' ' + dw + ' port')} .{C.yellow(port.name)} {GLYPH_DASH} crosses boundary")
            try:
                pc = scope_inst.getPortConnection(port.name)
                if pc and pc.expression and hasattr(pc.expression, 'symbol'):
                    conns.append(f"  {C.dim(GLYPH_CORNER + GLYPH_HR)} connected to {C.cyan(pc.expression.symbol.name)} in parent")
            except Exception:
                pass
        return conns

    # ── public API ───────────────────────────────────────────────────

    def get_top_paths(self):
        paths = []
        for t in self._root.topInstances:
            try:
                paths.append(t.hierarchicalPath)
            except Exception:
                continue
        return paths

    def list_signals(self, scope_path):
        inst = self._resolve_scope(scope_path)
        if inst is None:
            return []
        sigs = []
        def _c(sym):
            sigs.append({'name': sym.name, 'kind': sym.kind.name, 'type': str(sym.type)})
            return ast.VisitAction.Skip
        self._scope_visit(inst.body, {ast.SymbolKind.Net: _c, ast.SymbolKind.Variable: _c})
        return sigs

    def trace(self, signal_name, scope_path, cross=False):
        inst = self._resolve_scope(scope_path)
        if inst is None:
            return None
        sym = self._find_signal(signal_name, inst.body)
        if sym is None:
            return None

        drivers = self._analyze_drivers(sym, inst)
        loads = self._analyze_loads(sym, inst.body, inst)
        xh = self._trace_cross(sym, inst) if cross else []

        r = TraceResult(
            signal_name=signal_name, signal_type=str(sym.type),
            signal_kind=sym.kind.name, scope_path=scope_path,
            scope_module=inst.body.name, cross_hier=xh,
        )
        if len(drivers) >= 1:
            r.driver = drivers[0]
        if len(drivers) > 1:
            r.extra_drivers = drivers[1:]
        r.loads = loads
        return r

    def trace_all(self, scope_path, cross=False):
        sigs = self.list_signals(scope_path)
        results, seen = [], set()
        for s in sigs:
            if s['name'] in seen:
                continue
            seen.add(s['name'])
            r = self.trace(s['name'], scope_path, cross)
            if r:
                results.append(r)
        return results

    # ── CLI helpers ──────────────────────────────────────────────────

    def cmd_list(self, scope, as_json=False):
        sigs = self.list_signals(scope)
        if not sigs:
            print(f"Error: scope '{scope}' not found or empty", file=sys.stderr)
            sys.exit(1)
        if as_json:
            print(json.dumps(sigs, indent=2))
        else:
            print(f"Signals in {Color.cyan(scope)}:\n")
            for s in sigs:
                print(f"  {Color.bold(s['name']):24s}  {Color.dim(s['type']):20s}  ({s['kind']})")
            print(f"\n{Color.dim(str(len(sigs)) + ' signals')}")

    def cmd_trace(self, signal, scope, cross=False, load_filter=None, as_json=False):
        r = self.trace(signal, scope, cross)
        if r is None:
            print(f"Error: signal '{signal}' not found in scope '{scope}'", file=sys.stderr)
            sys.exit(1)
        if as_json:
            print(json.dumps(r.to_dict(load_filter), indent=2))
        else:
            r.pretty_print(load_filter)

    def cmd_trace_all(self, scope, cross=False, load_filter=None, as_json=False):
        results = self.trace_all(scope, cross)
        if not results:
            print(f"Error: no signals in scope '{scope}'", file=sys.stderr)
            sys.exit(1)
        if as_json:
            print(json.dumps([r.to_dict(load_filter) for r in results], indent=2))
        else:
            for r in results:
                r.pretty_print(load_filter)


# ── Agent-mode helpers ───────────────────────────────────────────────
def _emit_list(env, tracer, scope) -> int:
    sigs = tracer.list_signals(scope)
    if not sigs:
        return emit(env.fail(agent_json.ERR_SCOPE_NOT_FOUND,
                             f"scope '{scope}' not found or empty"))
    data = {'mode': 'list', 'scope': scope, 'signals': sigs}
    summary = {'mode': 'list', 'results': 0, 'signals': len(sigs)}
    return emit(env.ok(data, summary))


def _emit_all(env, tracer, scope, cross, load_filter) -> int:
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


def _emit_signal(env, tracer, signal, scope, cross, load_filter) -> int:
    r = tracer.trace(signal, scope, cross)
    if r is None:
        return emit(env.fail(agent_json.ERR_SIGNAL_NOT_FOUND,
                             f"signal '{signal}' not found in scope '{scope}'"))
    rd = r.to_dict(load_filter)
    data = {'mode': 'signal', 'scope': scope, 'results': [rd]}
    summary = {
        'mode':    'signal',
        'results': 1,
        'drivers': 1 if rd.get('driver') else 0,
        'loads':   int(rd.get('load_count', 0)),
    }
    return emit(env.ok(data, summary))


# ── Standalone CLI ───────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        prog='signal-trace',
        description='signal-trace \u2014 SV Signal Driver & Load Analyzer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
With filelist (typical debug workflow):
  signal-trace --filelist rtl.f --signal q --scope top.u_dp
  signal-trace --filelist rtl.f --signal clk --scope top --filter 'u_dp*'
  signal-trace --filelist rtl.f --scope top.u_dp --list
  signal-trace --filelist rtl.f --scope top.u_dp --all

With directory scan:
  signal-trace -d ./rtl --signal q --scope top.u_dp
  signal-trace -d ./rtl --signal a --scope top --cross
""")
    # Source inputs — filelist is first-class
    p.add_argument('files', nargs='*', help='Verilog/SV source files')
    p.add_argument('-d', '--dir', action='append', default=[], metavar='DIR',
                   help='Directory to scan recursively (repeatable)')
    p.add_argument('--filelist', '-f', action='append', default=[], metavar='FILE',
                   help='VCS-style .f filelist (repeatable)')
    p.add_argument('--filelist-root', '--projpath', dest='filelist_root',
                   default='.', metavar='DIR',
                   help='Base path for filelist relative paths (default: .)')
    p.add_argument('--filelist-prefix', default='${PROJPATH}', metavar='STR',
                   help='Prefix substituted for filelist path variables '
                        '(default: ${PROJPATH})')
    p.add_argument('--exclude', action='append', default=[], metavar='GLOB',
                   help='Exclude paths matching glob (repeatable)')

    # Signal tracing
    p.add_argument('--signal', '-s', default=None, metavar='NAME',
                   help='Signal name to trace')
    p.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope (e.g. top.u_dp)')
    p.add_argument('--cross', action='store_true', help='Trace through port boundaries')
    p.add_argument('--filter', default=None, metavar='GLOB',
                   help='Filter loads by instance name glob (e.g. u_fifo*)')
    p.add_argument('--list', action='store_true', help='List all signals in scope')
    p.add_argument('--all', action='store_true', help='Trace every signal in scope')
    p.add_argument('--json', action='store_true',
                   help='Emit trace results as an agent-friendly JSON envelope (see --schema)')
    p.add_argument('--schema', action='store_true',
                   help='Print the JSON Schema for --json output and exit')
    p.add_argument('--no-color', action='store_true', help='Disable ANSI colors')

    a = p.parse_args()

    if a.schema:
        sys.exit(agent_json.print_schema('signal-trace'))

    if a.no_color or not sys.stdout.isatty() or a.json:
        Color.disable()

    env: Optional[Envelope] = Envelope('signal-trace', filter_command(a)) if a.json else None

    def die(msg, code=agent_json.ERR_INTERNAL):
        if a.json:
            sys.exit(emit(env.fail(code, msg)))
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    # ── Resolve sources from all input methods ──
    all_paths = list(a.files) + list(a.dir)
    if not all_paths and not a.filelist:
        if a.json:
            die('no input: pass files, --dir, or --filelist',
                agent_json.ERR_INPUT_NOT_FOUND)
        p.print_help()
        sys.exit(1)

    fl_root = Path(a.filelist_root).expanduser().resolve()

    parsed = []
    for fl in a.filelist:
        try:
            parsed.append(parse_filelist(fl, fl_root, prefix=a.filelist_prefix))
        except FileNotFoundError as e:
            die(str(e), agent_json.ERR_BAD_FILELIST)

    scanned = collect_filelist(all_paths, excludes=a.exclude, root=fl_root) if all_paths else FileList()
    filelist = filter_filelist(merge_filelists(*parsed, scanned), a.exclude, fl_root)

    if not filelist.sources:
        die('no source files found.', agent_json.ERR_INPUT_NOT_FOUND)

    # ── Build compilation ──
    try:
        comp, _ = build_compilation(filelist.sources, filelist.include_dirs, filelist.defines)
    except Exception as e:
        die(f'compilation failed: {e}', agent_json.ERR_COMPILE_FAILED)
    tracer = SignalTracer(comp)

    # ── Auto-detect scope ──
    scope = a.scope
    if scope is None:
        tp = tracer.get_top_paths()
        if len(tp) == 1:
            scope = tp[0]
        elif tp:
            if a.json:
                die('multiple tops, specify --scope: ' + ', '.join(tp),
                    agent_json.ERR_SCOPE_NOT_FOUND)
            print("Multiple tops \u2014 specify --scope:", file=sys.stderr)
            for t in tp:
                print(f"  {t}", file=sys.stderr)
            sys.exit(1)
        else:
            die('no top modules found.', agent_json.ERR_NO_TOP)

    # ── Dispatch ──
    if a.json:
        if a.list:
            sys.exit(_emit_list(env, tracer, scope))
        if a.all:
            sys.exit(_emit_all(env, tracer, scope, a.cross, a.filter))
        if a.signal:
            sys.exit(_emit_signal(env, tracer, a.signal, scope, a.cross, a.filter))
        die('specify --signal <name>, --list, or --all',
            agent_json.ERR_INPUT_NOT_FOUND)

    if a.list:
        tracer.cmd_list(scope, a.json)
    elif a.all:
        tracer.cmd_trace_all(scope, a.cross, a.filter, a.json)
    elif a.signal:
        tracer.cmd_trace(a.signal, scope, a.cross, a.filter, a.json)
    else:
        print("Specify --signal <name>, --list, or --all", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

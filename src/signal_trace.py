#!/usr/bin/env python3
"""
signal_trace — Verilog/SystemVerilog Signal Driver & Load Analyzer

Designed for the debug/simulation workflow where a VCS-style filelist
already exists.  Traces the single driver and all loads of a signal.

Primary usage (with filelist):
    rtlscanner trace --filelist rtl.f --signal q --scope top.u_dp0
    rtlscanner trace --filelist rtl.f --signal clk --scope top --filter 'u_dp*'
    rtlscanner fanin --filelist rtl.f --signal q --scope top.u_dp0

Also works with directory scan:
    rtlscanner trace -d ./rtl --signal q --scope top.u_dp0

Install dependency:
    pip install pyslang
"""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import pyslang.ast as ast
    import pyslang.analysis as analysis
except ImportError:
    print("Error: pyslang is required.  pip install pyslang", file=sys.stderr)
    sys.exit(1)

from rtl_common import (
    Color,
)
from rtl_slang import (
    analyzed_procedures,
    expr_symbols,
    expr_refs_symbol,
    find_signal,
    iter_instances,
    procedure_label,
    procedure_reads_symbol,
    resolve_scope,
    scope_visit,
    symbol_key,
)

import agent_json
import rtl_cli
from agent_json import emit


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


@dataclass
class FlowEdge:
    """One dataflow edge between elaborated symbols."""
    source: str
    target: str
    kind: str
    description: str
    source_type: str = ""
    target_type: str = ""
    file: str = ""
    line: int = 0

    def key(self):
        return (self.source, self.target, self.kind, self.file, self.line)

    def to_dict(self, depth=None):
        d = dict(source=self.source, target=self.target, kind=self.kind,
                 description=self.description,
                 source_type=self.source_type, target_type=self.target_type)
        if self.file:
            d['file'] = self.file
            d['line'] = self.line
        if depth is not None:
            d['depth'] = depth
        return d


@dataclass
class FlowResult:
    """Fanin / fanout result for one starting signal."""
    mode: str
    signal_name: str
    signal_type: str
    signal_kind: str
    scope_path: str
    scope_module: str
    start: str
    edges: list = field(default_factory=list)
    max_depth: int = 0

    @property
    def nodes(self):
        out = [self.start]
        seen = {self.start}
        for edge, _depth in self.edges:
            for node in (edge.source, edge.target):
                if node not in seen:
                    seen.add(node)
                    out.append(node)
        return out

    def to_dict(self):
        return dict(
            mode=self.mode, signal=self.signal_name, type=self.signal_type,
            kind=self.signal_kind, scope=self.scope_path,
            module=self.scope_module, start=self.start,
            max_depth=self.max_depth,
            nodes=self.nodes,
            edges=[edge.to_dict(depth) for edge, depth in self.edges],
            edge_count=len(self.edges),
        )

    def pretty_print(self):
        C = Color
        title = "FANIN" if self.mode == "fanin" else "FANOUT"
        print(f"Signal: {C.bold(self.signal_name)}  {C.dim(self.signal_type)}")
        print(f"Scope:  {C.cyan(self.scope_path)}  [{C.yellow(self.scope_module)}]")
        print(f"Mode:   {C.green(title)}  {C.dim('depth <= ' + str(self.max_depth))}")
        print("\u2500" * 60)
        if not self.edges:
            print(f"\n  {C.dim('(no dataflow edges found)')}\n")
            return
        cur_depth = None
        for edge, depth in self.edges:
            if depth != cur_depth:
                cur_depth = depth
                print(f"\n  {C.dim('depth ' + str(depth))}")
            loc = f"  {C.dim(edge.file + ':' + str(edge.line))}" if edge.file else ""
            print(f"    {C.cyan(edge.source)} → "
                  f"{C.cyan(edge.target)}  "
                  f"{C.yellow(edge.kind)} {C.dim(edge.description)}{loc}")
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
        return resolve_scope(self._root, scope_path)

    def _find_signal(self, name, body):
        return find_signal(body, name)

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
        return expr_refs_symbol(expr, target)

    def _scope_visit(self, body, kinds):
        return scope_visit(body, kinds)

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
        for proc in analyzed_procedures(self._mgr, body):
            try:
                if proc.analyzedSymbol.kind != ast.SymbolKind.ProceduralBlock:
                    continue
                if not procedure_reads_symbol(proc, symbol):
                    continue
                f, ln = self._loc_sym(proc.analyzedSymbol)
                desc = f"{C.yellow(procedure_label(proc))}"
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

    # ── dataflow graph ───────────────────────────────────────────────

    def _sym_path(self, sym):
        return symbol_key(sym)

    def _sym_type(self, sym):
        try:
            return str(sym.type)
        except Exception:
            return ""

    def _make_flow_edge(self, source, target, kind, description,
                        loc_sym=None):
        if source is None or target is None:
            return None
        f, ln = self._loc_sym(loc_sym) if loc_sym is not None else ("", 0)
        return FlowEdge(
            source=self._sym_path(source),
            target=self._sym_path(target),
            source_type=self._sym_type(source),
            target_type=self._sym_type(target),
            kind=kind,
            description=description,
            file=f,
            line=ln,
        )

    def _port_symbol(self, port):
        return getattr(port, 'internalSymbol', None) or port

    def _assignment_left_symbols(self, expr):
        left = getattr(expr, 'left', None)
        return expr_symbols(left) if left is not None else expr_symbols(expr)

    def _build_flow_edges(self):
        if hasattr(self, '_flow_edges'):
            return self._flow_edges

        edges = []
        seen = set()

        def add(edge):
            if edge is None:
                return
            key = edge.key()
            if key in seen:
                return
            seen.add(key)
            edges.append(edge)

        for inst in iter_instances(self._root):
            body = getattr(inst, 'body', None)
            if body is None:
                continue

            for proc in analyzed_procedures(self._mgr, body):
                try:
                    drivers = [d.symbol for d in (proc.drivers or [])]
                    reads = [r.symbol for r in (proc.readSet or [])]
                    if not drivers or not reads:
                        continue
                    if proc.analyzedSymbol.kind == ast.SymbolKind.ContinuousAssign:
                        kind = "continuous_assign"
                        desc = "assign"
                    elif proc.analyzedSymbol.kind == ast.SymbolKind.ProceduralBlock:
                        kind = "procedural"
                        desc = procedure_label(proc)
                    else:
                        kind = "procedure"
                        desc = str(proc.analyzedSymbol.kind)
                    for src in reads:
                        for dst in drivers:
                            add(self._make_flow_edge(
                                src, dst, kind, desc, proc.analyzedSymbol))
                except Exception:
                    continue

            try:
                port_connections = inst.portConnections
            except Exception:
                port_connections = []
            for pc in port_connections:
                try:
                    port = pc.port
                    port_sym = self._port_symbol(port)
                    expr = pc.expression
                    if expr is None or port_sym is None:
                        continue
                    direction = port.direction
                    if direction in (ast.ArgumentDirection.In,
                                     ast.ArgumentDirection.InOut):
                        for src in expr_symbols(expr):
                            add(self._make_flow_edge(
                                src, port_sym, "port_connection",
                                f"{inst.name}.{port.name} input", inst))
                    if direction in (ast.ArgumentDirection.Out,
                                     ast.ArgumentDirection.InOut):
                        for dst in self._assignment_left_symbols(expr):
                            add(self._make_flow_edge(
                                port_sym, dst, "port_connection",
                                f"{inst.name}.{port.name} output", inst))
                except Exception:
                    continue

        edges.sort(key=lambda e: (e.source, e.target, e.kind, e.file, e.line))
        self._flow_edges = edges
        return edges

    def flow(self, signal_name, scope_path, mode, max_depth=4):
        inst = self._resolve_scope(scope_path)
        if inst is None:
            return None
        sym = self._find_signal(signal_name, inst.body)
        if sym is None:
            return None

        start = self._sym_path(sym)
        edges = self._build_flow_edges()
        by_source, by_target = {}, {}
        for edge in edges:
            by_source.setdefault(edge.source, []).append(edge)
            by_target.setdefault(edge.target, []).append(edge)

        edge_map = by_target if mode == "fanin" else by_source
        traversed = []
        seen_edges = set()
        seen_nodes = {start}
        frontier = [start]
        depth = 0
        max_depth = max(0, int(max_depth))
        while frontier and depth < max_depth:
            depth += 1
            next_frontier = []
            for node in frontier:
                for edge in edge_map.get(node, []):
                    ekey = edge.key()
                    if ekey not in seen_edges:
                        seen_edges.add(ekey)
                        traversed.append((edge, depth))
                    nxt = edge.source if mode == "fanin" else edge.target
                    if nxt not in seen_nodes:
                        seen_nodes.add(nxt)
                        next_frontier.append(nxt)
            frontier = next_frontier

        return FlowResult(
            mode=mode, signal_name=signal_name, signal_type=str(sym.type),
            signal_kind=sym.kind.name, scope_path=scope_path,
            scope_module=inst.body.name, start=start,
            edges=traversed, max_depth=max_depth,
        )

    # ── public API ───────────────────────────────────────────────────

    def get_top_paths(self):
        paths = []
        for t in self._root.topInstances:
            try:
                paths.append(t.hierarchicalPath)
            except Exception:
                continue
        return paths

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

# ── Shared input/dispatch helpers ────────────────────────────────────
def _prepare(args, *, need_signal=False):
    """Common setup for trace/fanin/fanout: resolve inputs, build
    compilation, auto-detect scope.  Returns (tracer, scope); raises CliError
    on any input/compile/scope failure."""
    prepared = rtl_cli.prepare_compilation(args, human_error_rc=1)
    tracer = SignalTracer(prepared.comp)
    scope = rtl_cli.resolve_scope(
        args.scope,
        tracer.get_top_paths(),
        human_error_rc=1,
    )

    if need_signal and not getattr(args, 'signal', None):
        raise rtl_cli.CliError(
            agent_json.ERR_INPUT_NOT_FOUND,
            'specify --signal/-s NAME',
            1,
        )

    return tracer, scope


# ── Subcommand: trace ────────────────────────────────────────────────
def add_trace_args(p):
    g = p.add_argument_group('trace')
    g.add_argument('-s', '--signal', default=None, metavar='NAME',
                   help='Signal name to trace (required)')
    g.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope; auto-detect when single top')
    g.add_argument('--cross', action='store_true',
                   help='Trace through port boundaries')
    g.add_argument('--filter', default=None, metavar='GLOB',
                   help='Shell glob on instance names to narrow loads')


def run_trace(args, env):
    tracer, scope = _prepare(args, need_signal=True)
    r = tracer.trace(args.signal, scope, args.cross)
    if r is None:
        raise rtl_cli.CliError(
            agent_json.ERR_SIGNAL_NOT_FOUND,
            f"signal '{args.signal}' not found in scope '{scope}'",
            1,
        )
    if env is not None:
        rd = r.to_dict(args.filter)
        data = {'mode': 'signal', 'scope': scope, 'results': [rd]}
        summary = {
            'mode': 'signal', 'results': 1,
            'drivers': 1 if rd.get('driver') else 0,
            'loads':   int(rd.get('load_count', 0)),
        }
        return emit(env.ok(data, summary))
    r.pretty_print(args.filter)
    return 0

# ── Subcommands: fanin / fanout ──────────────────────────────────────
def add_flow_args(p):
    g = p.add_argument_group('flow')
    g.add_argument('-s', '--signal', default=None, metavar='NAME',
                   help='Starting signal (required)')
    g.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope; auto-detect when single top')
    g.add_argument('--depth', type=int, default=4, metavar='N',
                   help='Maximum BFS traversal depth (default: 4)')


def run_flow(args, env, *, mode):
    tracer, scope = _prepare(args, need_signal=True)
    r = tracer.flow(args.signal, scope, mode, args.depth)
    if r is None:
        raise rtl_cli.CliError(
            agent_json.ERR_SIGNAL_NOT_FOUND,
            f"signal '{args.signal}' not found in scope '{scope}'",
            1,
        )
    if env is not None:
        rd = r.to_dict()
        data = {
            'mode': mode, 'scope': scope, 'signal': args.signal,
            'start': rd['start'], 'nodes': rd['nodes'], 'edges': rd['edges'],
            'max_depth': rd['max_depth'],
        }
        summary = {
            'mode': mode, 'results': 1,
            'nodes': len(rd['nodes']), 'edges': len(rd['edges']),
            'max_depth': rd['max_depth'],
        }
        return emit(env.ok(data, summary))
    r.pretty_print()
    return 0

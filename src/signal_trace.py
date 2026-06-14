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
import re
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
    safe_str,
)
from rtl_slang import (
    analyzed_procedures,
    canonical_twin,
    canonical_view,
    expr_symbols,
    expr_refs_symbol,
    find_signal,
    is_data_symbol,
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
    bits: str = ""          # "[3]" / "[7:4]" when the driver covers a sub-range
    bounds: Optional[tuple] = None  # normalized (lo, hi) bit offsets

    def key(self):
        return (self.kind, self.source, self.scope_path,
                self.file, self.line, self.bounds)

    def to_dict(self):
        d = dict(kind=self.kind, source=self.source, description=self.description,
                 symbol=self.symbol_name, symbol_kind=self.symbol_kind,
                 scope_path=self.scope_path)
        if self.bits:
            d['bits'] = self.bits
        if self.file:
            d['file'] = self.file
            d['line'] = self.line
        return d


def drivers_overlap(infos) -> bool:
    """True when any two drivers cover overlapping bit ranges.

    Multiple drivers over disjoint ranges (per-bit generate outputs, packed
    struct field assigns) are legal single-driver RTL and must not be flagged.
    Unknown bounds fall back to the conservative answer.
    """
    if len(infos) < 2:
        return False
    spans = [i.bounds for i in infos]
    if any(s is None for s in spans):
        return True
    spans = sorted(spans)
    return any(spans[i][1] >= spans[i + 1][0] for i in range(len(spans) - 1))


# ── Bit-select on a traced signal (e.g. `status[3]`, `status[7:4]`) ──
_BITSEL_RE = re.compile(r'^(.+?)\[(\d+)(?::(\d+))?\]$')


def split_bit_select(name):
    """Split a trailing bit-select off a signal name.

    'status[3]'   -> ('status', (3, 3))
    'u_dp.q[7:4]' -> ('u_dp.q', (4, 7))
    'status'      -> ('status', None)
    A variable/dynamic index does not match and is left on the name.
    """
    if not name:
        return name, None
    m = _BITSEL_RE.match(name.strip())
    if not m:
        return name, None
    a = int(m.group(2))
    b = int(m.group(3)) if m.group(3) is not None else a
    return m.group(1), (min(a, b), max(a, b))


def bit_label(rng):
    lo, hi = rng
    return f"[{hi}]" if lo == hi else f"[{hi}:{lo}]"


def bits_overlap(bounds, rng):
    """True when driver bounds overlap the requested (lo, hi) range.
    Unknown bounds are kept (conservative)."""
    if bounds is None:
        return True
    return bounds[0] <= rng[1] and rng[0] <= bounds[1]


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
    extra_drivers: list = field(default_factory=list)  # additional drivers
    multi_driver: bool = False                # True only on overlapping ranges
    loads: list = field(default_factory=list)
    cross_hier: list = field(default_factory=list)
    bit_range: Optional[tuple] = None   # (lo, hi) when a bit-select was queried

    @property
    def display_name(self):
        return self.signal_name + (bit_label(self.bit_range) if self.bit_range else "")

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
        d = dict(signal=self.signal_name, type=self.signal_type,
                 kind=self.signal_kind, scope=self.scope_path,
                 module=self.scope_module)
        if self.bit_range is not None:
            d['bit_select'] = bit_label(self.bit_range)
        d['driver'] = self.driver.to_dict() if self.driver else None
        if self.extra_drivers:
            d['extra_drivers'] = [x.to_dict() for x in self.extra_drivers]
            d['multi_driver_warning'] = self.multi_driver
        # A bit-select is a driver-origin query; loads-by-bit is phase 2.
        if self.bit_range is None:
            loads = self.filtered_loads(load_filter)
            d['loads'] = [ld.to_dict() for ld in loads]
            d['load_count'] = len(loads)
        if self.cross_hier:
            d['cross_hierarchy'] = self.cross_hier
        return d

    def _driver_line(self, d):
        C = Color
        bits = f" {C.yellow(self.signal_name + d.bits)}" if d.bits else ""
        where = ""
        if d.scope_path and d.scope_path != self.scope_path:
            where = f"  {C.dim('@ ' + d.scope_path)}"
        loc = f"  {C.dim(d.file + ':' + str(d.line))}" if d.file else ""
        return f"    {GLYPH_ARROW_L} {d.description}{bits}{where}{loc}"

    def pretty_print(self, load_filter=None):
        C = Color

        print(f"Signal: {C.bold(self.display_name)}  {C.dim(self.signal_type)}")
        print(f"Scope:  {C.cyan(self.scope_path)}  [{C.yellow(self.scope_module)}]")
        print("\u2500" * 60)

        # ── Driver (singular in RTL) ──
        drivers = self.all_drivers
        if not drivers:
            print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}  {C.dim('(none ' + GLYPH_DASH + ' undriven)')}")
        elif len(drivers) == 1:
            print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}")
            print(self._driver_line(drivers[0]))
        elif self.multi_driver:
            print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVER')}  {C.red(GLYPH_WARN + ' MULTI-DRIVER (' + str(len(drivers)) + ')')}")
            for d in drivers:
                print(self._driver_line(d))
        else:
            print(f"\n  {C.red(GLYPH_DRIVER + ' DRIVERS')} ({len(drivers)})  {C.dim('disjoint bit ranges')}")
            for d in drivers:
                print(self._driver_line(d))

        # ── Loads (skipped for a bit-select: it is a driver-origin query) ──
        if self.bit_range is not None:
            print()
            return
        loads = self.filtered_loads(load_filter)
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
        # AnalysisManager.analyze() requires a fully-elaborated compilation;
        # getSemanticDiagnostics() forces elaboration (sets isElaborated).
        _ = compilation.getSemanticDiagnostics()
        self._mgr = analysis.AnalysisManager()
        self._mgr.analyze(compilation)
        self._proc_edge_cache = {}

    # ── helpers ───────────────────────────────────────────────────────

    def _resolve_scope(self, scope_path):
        return resolve_scope(self._root, scope_path)

    def _find_signal(self, name, body):
        return find_signal(body, name)

    def _lookup(self, signal_name, scope_path):
        """Resolve (inst, sym), raising a precise CliError on failure."""
        inst = self._resolve_scope(scope_path)
        if inst is None:
            raise rtl_cli.scope_not_found_error(
                self._root, scope_path, human_error_rc=1)
        sym = self._find_signal(signal_name, inst.body)
        if sym is None:
            raise rtl_cli.signal_not_found_error(
                inst.body, signal_name, scope_path, human_error_rc=1)
        return inst, sym

    def normalize_signal(self, scope, signal):
        """Support dotted -s forms.

        '<child.path>.<sig>' is reinterpreted relative to scope, then as an
        absolute hierarchical path, whichever resolves first.  Returns
        (scope, signal, note) where note describes a reinterpretation.
        """
        if not signal or '.' not in signal:
            return scope, signal, None
        inst = self._resolve_scope(scope) if scope else None
        if inst is not None and self._find_signal(signal, inst.body) is not None:
            return scope, signal, None
        prefix, leaf = signal.rsplit('.', 1)
        candidates = ([f"{scope}.{prefix}"] if scope else []) + [prefix]
        for cand in candidates:
            cinst = self._resolve_scope(cand)
            if cinst is not None and self._find_signal(leaf, cinst.body) is not None:
                note = (f"interpreted signal '{signal}' as '{leaf}' "
                        f"in scope '{cand}'")
                return cand, leaf, note
        return scope, signal, None

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

    def _signal_full_bounds(self, symbol):
        try:
            return (0, int(symbol.type.bitWidth) - 1)
        except Exception:
            return None

    def _driver_info(self, d, symbol, remap=None):
        C = Color
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
        if remap is not None and sp:
            sp = remap(sp)

        bounds = None
        try:
            b = d.bounds
            bounds = (int(b[0]), int(b[1]))
        except Exception:
            pass
        bits = ""
        if bounds is not None and bounds != self._signal_full_bounds(symbol):
            lo, hi = bounds
            bits = f"[{lo}]" if lo == hi else f"[{hi}:{lo}]"

        return DriverInfo(kind=kind, source=source, description=desc,
                          symbol_name=cs_name, symbol_kind=cs.kind.name,
                          scope_path=sp, file=f, line=ln,
                          bits=bits, bounds=bounds)

    def _analyze_drivers(self, symbol, scope_inst):
        infos = []
        seen = set()

        def add(d, remap=None):
            info = self._driver_info(d, symbol, remap)
            if info.key() not in seen:
                seen.add(info.key())
                infos.append(info)

        for d in self._mgr.getDrivers(symbol):
            add(d)

        # slang's AnalysisManager records drivers only against the canonical
        # body of deduplicated instances; query the canonical twin and remap
        # paths back into this instance.  Drivers whose containing symbol
        # lies outside the canonical subtree are hierarchical references that
        # target the canonical instance specifically, not this copy.
        view = canonical_view(self._root, scope_inst)
        if view.deduped:
            twin = canonical_twin(view, symbol)
            if twin is not None and twin is not symbol:
                for d in self._mgr.getDrivers(twin):
                    try:
                        cs_path = safe_str(d.containingSymbol.hierarchicalPath, "")
                    except Exception:
                        cs_path = ""
                    if cs_path and not view.contains(cs_path):
                        continue
                    add(d, view.remap)
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

        # 3. Procedural blocks (readSet excludes assignment LHS symbols).
        # Analyzed procedures live on the canonical body; read-set membership
        # must be checked against the canonical twin of the queried symbol.
        view = canonical_view(self._root, scope_inst) if scope_inst is not None else None
        proc_body, proc_symbol = body, symbol
        if view is not None and view.deduped:
            proc_body = view.body
            proc_symbol = canonical_twin(view, symbol)
        for proc in analyzed_procedures(self._mgr, proc_body):
            try:
                if proc.analyzedSymbol.kind != ast.SymbolKind.ProceduralBlock:
                    continue
                if not procedure_reads_symbol(proc, proc_symbol):
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

    def _proc_edge_templates(self, view):
        """Proc-derived edge tuples for one analyzed body, cached per body.

        Endpoint paths are in the canonical namespace; callers stamp them
        into a specific instance with view.remap.  Caching per canonical
        body means each unique module is walked once no matter how many
        times it is instantiated.

        Procedural blocks use per-statement dependencies (each assignment's
        RHS feeds only its own LHS; control-condition reads go to every driver
        in the block) instead of a readSet x drivers cross-product, which
        otherwise links every co-read signal to every driven signal.
        """
        key = safe_str(getattr(view.body, 'hierarchicalPath', ''), '') \
            or str(id(view.body))
        cached = self._proc_edge_cache.get(key)
        if cached is not None:
            return cached

        templates = []
        for proc in analyzed_procedures(self._mgr, view.body):
            try:
                drivers = [d.symbol for d in (proc.drivers or [])
                           if is_data_symbol(d.symbol)]
                reads = [r.symbol for r in (proc.readSet or [])
                         if is_data_symbol(r.symbol)]
                if not drivers or not reads:
                    continue
                pkind = proc.analyzedSymbol.kind
                if pkind == ast.SymbolKind.ContinuousAssign:
                    kind, desc = "continuous_assign", "assign"
                elif pkind == ast.SymbolKind.ProceduralBlock:
                    kind, desc = "procedural", procedure_label(proc)
                else:
                    kind, desc = "procedure", str(pkind)
                f, ln = self._loc_sym(proc.analyzedSymbol)

                if pkind == ast.SymbolKind.ProceduralBlock:
                    pairs = self._proc_statement_deps(proc, drivers)
                else:
                    # A single continuous assign: every read feeds the LHS.
                    pairs = [(src, dst) for src in reads for dst in drivers]

                for src, dst in pairs:
                    templates.append((
                        self._sym_path(src), self._sym_path(dst),
                        self._sym_type(src), self._sym_type(dst),
                        kind, desc, f, ln))
            except Exception:
                continue
        self._proc_edge_cache[key] = templates
        return templates

    def _proc_statement_deps(self, proc, drivers):
        """Per-statement LHS<-RHS data deps + conservative control reads for
        one procedural block.  Returns unique (src_sym, dst_sym) pairs.

        Data is precise (an assignment's RHS feeds only its own LHS).  Control
        conditions (if / case / loop) are attributed to every driver in the
        block — a small, never-under-reporting over-approximation that avoids
        a per-branch scoping walk.
        """
        data_pairs = []   # [(lhs_syms, rhs_syms)]
        control = []      # control-condition reads

        def collect(node):
            if type(node).__name__ == "AssignmentExpression":
                try:
                    data_pairs.append((expr_symbols(node.left),
                                       expr_symbols(node.right)))
                except Exception:
                    pass
            else:
                control.extend(self._statement_control_reads(node))

        try:
            proc.analyzedSymbol.body.visit(f=collect)
        except Exception:
            data_pairs = []

        if not data_pairs:
            # Degenerate walk; fall back to the coarse reads x drivers so we
            # never silently under-report.
            reads = [r.symbol for r in (proc.readSet or [])
                     if is_data_symbol(r.symbol)]
            return [(s, d) for s in reads for d in drivers]

        control = [s for s in control if is_data_symbol(s)]
        pairs = {}        # (src_path, dst_path) -> (src_sym, dst_sym)

        def add(src, dst):
            if is_data_symbol(src) and is_data_symbol(dst):
                pairs.setdefault((self._sym_path(src), self._sym_path(dst)),
                                 (src, dst))

        for lhs_syms, rhs_syms in data_pairs:
            for lhs in lhs_syms:
                for rhs in rhs_syms:
                    add(rhs, lhs)
        for dst in drivers:
            for cr in control:
                add(cr, dst)
        return list(pairs.values())

    @staticmethod
    def _statement_control_reads(node):
        """Symbols read in a statement's controlling condition (if/case/loop)."""
        tn = type(node).__name__
        if tn == "ConditionalStatement":
            out = []
            for cond in (getattr(node, "conditions", None) or []):
                out.extend(expr_symbols(getattr(cond, "expr", cond)))
            return out
        if tn == "CaseStatement":
            try:
                return expr_symbols(node.expr)
            except Exception:
                return []
        if "Loop" in tn:
            out = []
            for attr in ("cond", "stopExpr", "stopCondition", "count"):
                e = getattr(node, attr, None)
                if e is not None:
                    out.extend(expr_symbols(e))
            return out
        return []


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

            # Analyzed procedures live on canonical bodies only; stamp each
            # body's edges into this instance's namespace so deduplicated
            # instances (generate arrays, repeated modules) keep their edges.
            view = canonical_view(self._root, inst)
            for (src, dst, st, dt, kind, desc, f, ln) in \
                    self._proc_edge_templates(view):
                add(FlowEdge(source=view.remap(src), target=view.remap(dst),
                             source_type=st, target_type=dt, kind=kind,
                             description=desc, file=f, line=ln))

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
                            if not is_data_symbol(src):
                                continue
                            add(self._make_flow_edge(
                                src, port_sym, "port_connection",
                                f"{inst.name}.{port.name} input", inst))
                    if direction in (ast.ArgumentDirection.Out,
                                     ast.ArgumentDirection.InOut):
                        for dst in self._assignment_left_symbols(expr):
                            if not is_data_symbol(dst):
                                continue
                            add(self._make_flow_edge(
                                port_sym, dst, "port_connection",
                                f"{inst.name}.{port.name} output", inst))
                except Exception:
                    continue

        edges.sort(key=lambda e: (e.source, e.target, e.kind, e.file, e.line))
        self._flow_edges = edges
        return edges

    def flow(self, signal_name, scope_path, mode, max_depth=4):
        inst, sym = self._lookup(signal_name, scope_path)

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

    def trace(self, signal_name, scope_path, cross=False, bit_range=None):
        inst, sym = self._lookup(signal_name, scope_path)

        if bit_range is not None:
            try:
                width = int(sym.type.bitWidth)
            except Exception:
                width = None
            if width and bit_range[1] >= width:
                raise rtl_cli.CliError(
                    agent_json.ERR_SIGNAL_NOT_FOUND,
                    f"bit {bit_label(bit_range)} out of range for "
                    f"'{signal_name}' ({sym.type}, {width} bits)", 1)

        drivers = self._analyze_drivers(sym, inst)
        if bit_range is not None:
            drivers = [d for d in drivers if bits_overlap(d.bounds, bit_range)]
        # A bit-select is a driver-origin query: loads/cross are phase 2.
        loads = [] if bit_range is not None else self._analyze_loads(sym, inst.body, inst)
        xh = self._trace_cross(sym, inst) if (cross and bit_range is None) else []

        r = TraceResult(
            signal_name=signal_name, signal_type=str(sym.type),
            signal_kind=sym.kind.name, scope_path=scope_path,
            scope_module=inst.body.name, cross_hier=xh,
            multi_driver=drivers_overlap(drivers),
            bit_range=bit_range,
        )
        if len(drivers) >= 1:
            r.driver = drivers[0]
        if len(drivers) > 1:
            r.extra_drivers = drivers[1:]
        r.loads = loads
        return r

# ── Shared input/dispatch helpers ────────────────────────────────────
def _prepare(args, env, *, need_signal=False):
    """Common setup for trace/fanin/fanout: resolve inputs, build
    compilation, auto-detect scope, normalize dotted -s forms, and split off
    a trailing bit-select.  Returns (tracer, scope, signal, bit_range);
    raises CliError on any input/compile/scope failure."""
    prepared = rtl_cli.prepare_compilation(args, human_error_rc=1)
    tracer = SignalTracer(prepared.comp)
    scope = rtl_cli.resolve_scope(
        args.scope,
        tracer.get_top_paths(),
        human_error_rc=1,
    )

    signal = getattr(args, 'signal', None)
    if need_signal and not signal:
        raise rtl_cli.CliError(
            agent_json.ERR_INPUT_NOT_FOUND,
            'specify --signal/-s NAME',
            1,
        )

    bit_range = None
    if signal:
        signal, bit_range = split_bit_select(signal)
        scope, signal, note = tracer.normalize_signal(scope, signal)
        if note:
            if env is not None:
                env.add_diagnostic("note", message=note)
            else:
                print(f"note: {note}", file=sys.stderr)

    return tracer, scope, signal, bit_range


# ── Subcommand: trace ────────────────────────────────────────────────
def add_trace_args(p):
    g = p.add_argument_group('trace')
    g.add_argument('-s', '--signal', default=None, metavar='NAME',
                   help='Signal to trace; a bit-select narrows the driver '
                        'origin (e.g. status[3], status[7:4])')
    g.add_argument('--scope', default=None, metavar='SCOPE',
                   help='Hierarchical scope; auto-detect when single top')
    g.add_argument('--cross', action='store_true',
                   help='Trace through port boundaries')
    g.add_argument('--filter', default=None, metavar='GLOB',
                   help='Shell glob on instance names to narrow loads')


def run_trace(args, env):
    tracer, scope, signal, bit_range = _prepare(args, env, need_signal=True)
    r = tracer.trace(signal, scope, args.cross, bit_range)
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
    g.add_argument('--summary', action='store_true',
                   help='Counts + direct neighbors only; omit the full node/edge graph')


def _emit_flow_summary(env, r, mode, scope, signal):
    """Counts, an edges-by-depth histogram, and the direct neighbors — instead
    of the full cone, which can be thousands of edges on a real design."""
    rd = r.to_dict()
    edges = rd['edges']
    by_depth = {}
    for e in edges:
        d = int(e.get('depth', 0))
        by_depth[d] = by_depth.get(d, 0) + 1
    far = 'source' if mode == 'fanin' else 'target'
    direct = sorted({e[far] for e in edges if int(e.get('depth', 0)) == 1})
    node_count = len(rd['nodes'])
    edge_count = len(edges)
    max_depth = rd['max_depth']

    if env is not None:
        data = {
            'mode': mode, 'scope': scope, 'signal': signal,
            'start': rd['start'], 'summary_only': True,
            'node_count': node_count, 'edge_count': edge_count,
            'max_depth': max_depth,
            'edges_by_depth': {str(k): by_depth[k] for k in sorted(by_depth)},
            'direct': direct,
        }
        summary = {'mode': mode, 'results': 1, 'nodes': node_count,
                   'edges': edge_count, 'max_depth': max_depth}
        return emit(env.ok(data, summary))

    C = Color
    title = "FANIN" if mode == "fanin" else "FANOUT"
    print(f"Signal: {C.bold(signal)}")
    print(f"Mode:   {C.green(title + ' summary')}  {C.dim('depth <= ' + str(max_depth))}")
    print(f"  nodes {C.yellow(str(node_count))}   edges {C.yellow(str(edge_count))}")
    if by_depth:
        print("  edges by depth: " +
              ", ".join(f"{k}:{by_depth[k]}" for k in sorted(by_depth)))
    label = "direct sources" if mode == "fanin" else "direct sinks"
    print(f"  {label} ({len(direct)}): " + (", ".join(direct) or "(none)"))
    return 0


def run_flow(args, env, *, mode):
    tracer, scope, signal, bit_range = _prepare(args, env, need_signal=True)
    if bit_range is not None:
        note = f"bit-select ignored for {mode}; using whole signal '{signal}'"
        if env is not None:
            env.add_diagnostic("note", message=note)
        else:
            print(f"note: {note}", file=sys.stderr)
    r = tracer.flow(signal, scope, mode, args.depth)
    if getattr(args, 'summary', False):
        return _emit_flow_summary(env, r, mode, scope, signal)
    if env is not None:
        rd = r.to_dict()
        data = {
            'mode': mode, 'scope': scope, 'signal': signal,
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

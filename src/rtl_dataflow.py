#!/usr/bin/env python3
"""
rtl_dataflow — Verilog/SystemVerilog dataflow engine.

The mechanism layer behind ``trace`` / ``fanin`` / ``fanout`` (and the lint
CDC / combinational-loop checks).  It builds the design's driver/load and
dataflow-graph model and answers structural queries over it; it owns no output
framing.  The thin command modules (``signal_trace``, ``signal_flow``) render
the typed model (``TraceResult`` / ``FlowResult`` / ``FlowEdge`` …) into their
own shapes, and ``signal_cli`` adapts argv into an engine query.

``SignalTracer`` is the entry point:
    trace(signal, scope)       -> TraceResult        (single driver + all loads)
    flow(signal, scope, mode)  -> FlowResult         (bit-aware fanin/fanout BFS)
    flow_edges()               -> [FlowEdge]          (whole-design dataflow graph)
    clock_domain_map()         -> {node: ClockDomain}

Install dependency:
    pip install pyslang
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
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
    expr_reads_with_bounds,
    expr_symbols,
    expr_refs_symbol,
    find_signal,
    full_bounds,
    is_data_symbol,
    iter_instances,
    lsp_bounds,
    make_eval_context,
    procedure_label,
    procedure_reads_symbol,
    resolve_scope,
    scope_visit,
    symbol_key,
    try_eval_bool,
    try_eval_int,
)

import agent_json
import rtl_cli


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
    bits: str = ""          # "[3]" / "[7:4]" when the load reads a sub-range
    bounds: Optional[tuple] = None  # normalized (lo, hi) bit offsets read


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
    bit_range: Optional[tuple] = None   # (lo, hi) when a bit-select was queried


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
    # True when the edge is *registered* — its target is driven by an
    # edge-triggered/level-held sequential procedure (always_ff / always_latch
    # / an edge-sensitive `always`).  A clocked edge breaks combinational
    # feedback (loop detection ignores it) and marks a flip-flop boundary
    # (CDC keys launch/capture domains off it).  Continuous assigns and port
    # connections are never clocked.  Deliberately NOT part of ``key()`` so the
    # demand-driven / whole-graph parity (test_flow_lazy) is unaffected.
    clocked: bool = False
    # Bit-level dataflow.  source_bits / target_bits are the (lo, hi) sub-ranges
    # this edge reads / drives, or None for the whole signal — so whole-signal
    # edges serialize exactly as before (additive).
    # bit_offset is the affine map of a copy-like edge (target_bit = source_bit
    # + bit_offset), which lets fanin/fanout answer "dout[5] ← which bit"; it is
    # None when the relationship is many-to-many (arithmetic / reduction).
    source_bits: Optional[tuple] = None
    target_bits: Optional[tuple] = None
    bit_offset: Optional[int] = None
    # A bit *permutation* between the same (source, target) pair — a tuple of
    # (source_bits, target_bits, offset) sub-copies — for maps a single affine
    # offset cannot express (a bit reversal ``rev[i]=din[7-i]``, a half swap
    # ``o={a[3:0],a[7:4]}``).  Each segment is a precise positional copy; source/
    # target_bits hold the spanning union for coarse overlap/display, bit_offset
    # is None.  Lets fanin/fanout still answer "rev[0] ← din[7]" across the map.
    # None for a single-segment edge.
    segments: Optional[tuple] = None

    def _bit_key(self):
        """The bit-mapping component of ``key()``, as sort-safe strings (never
        ``None``, which is unorderable against a tuple in ``sort``)."""
        sb = bit_label(self.source_bits) if self.source_bits else ""
        db = bit_label(self.target_bits) if self.target_bits else ""
        seg = ";".join(f"{bit_label(s)}>{bit_label(d)}"
                       for (s, d, _o) in self.segments) if self.segments else ""
        return (sb, db, seg)

    def key(self):
        # The bit mapping is part of the identity: two assigns on the SAME source
        # line that drive the same (source,target) pair over *different* bits — a
        # generate-loop bit reversal `for i: dout[i]=din[7-i]`, or several bit
        # assigns sharing a line — are distinct edges, not duplicates to dedup
        # away.  Whole-signal edges carry an empty bit key, so they (and the
        # demand-driven / whole-graph parity) are unaffected.
        return ((self.source, self.target, self.kind, self.file, self.line)
                + self._bit_key())

    def trimmed_to(self, near_rngs, mode):
        """A display copy whose permutation ``segments`` are narrowed to those
        overlapping ANY of the near-side ranges in ``near_rngs`` (target bits for
        fanin, source bits for fanout), so a bit-select shows exactly the
        segments it asked for (``fanin rev[0]`` -> just ``din[7] -> rev[0]``).

        ``near_rngs`` is every range that reached this edge during the walk: a
        single edge can be discovered from several frontier bits (``y[0]<-m[0]``
        and ``y[1]<-m[1]`` both reach the ``din->m`` reversal), and *all* of
        their segments must survive — trimming to only the first would drop the
        rest.  Returns ``self`` when there is nothing to trim: no segments, a
        whole-signal range present (``None``), or every segment already kept."""
        if not self.segments or near_rngs is None or any(r is None
                                                         for r in near_rngs):
            return self
        kept = []
        for (sb, db, off) in self.segments:
            near = db if mode == "fanin" else sb
            if near is not None and any(not (r[1] < near[0] or r[0] > near[1])
                                        for r in near_rngs):
                kept.append((sb, db, off))
        if not kept or len(kept) == len(self.segments):
            return self
        return replace(self, segments=tuple(kept))


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
    bit_range: Optional[tuple] = None   # (lo, hi) when a bit-select was queried
    # Combinational-cone mode: the BFS stopped at sequential (clocked) edges, so
    # the cone is the pure combinational cloud bounded by register edges.
    comb: bool = False

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


@dataclass
class PathResult:
    """A point-to-point path between two nodes.

    ``nodes`` is the ordered node sequence from start to end, and ``edges`` the
    dataflow edges between them — ``edges[i]`` links ``nodes[i]`` -> ``nodes[i+1]``,
    so the pair reads as the alternating node/edge walk the path *is*.  An empty
    ``nodes`` means no path exists for the requested direction/predicate — a
    normal result, not an error.

    The path is *directional* (``from`` drives ``to`` — a forward / fan-out walk)
    and, with ``comb=True``, *combinational* (it never enters a register node, so
    it is bounded by flip-flops, the same boundary the ``--comb`` cone uses).
    """
    from_signal: str
    to_signal: str
    from_scope: str
    to_scope: str
    start: str               # resolved elaborated path of the start node
    end: str                 # resolved elaborated path of the end node
    start_type: str = ""
    end_type: str = ""
    nodes: list = field(default_factory=list)   # [node_path] ordered start..end
    edges: list = field(default_factory=list)   # [FlowEdge], len == len(nodes)-1
    comb: bool = False

    @property
    def found(self) -> bool:
        return bool(self.nodes)

    @property
    def length(self) -> int:
        """Hop count (number of edges); 0 for a not-found or single-node path."""
        return max(0, len(self.nodes) - 1)

    def node_types(self) -> list:
        """The data type of each node in ``nodes``, in order.  The start type is
        carried directly; every later node's type is the ``target_type`` of the
        edge that reaches it, so the list lines up one-for-one with ``nodes``."""
        if not self.nodes:
            return []
        types = [self.start_type]
        for e in self.edges:
            types.append(e.target_type or "")
        return types


# ── Graph-analysis result records (CDC / combinational loops) ────────
@dataclass
class ClockDomain:
    """The clock domain(s) a registered node belongs to."""
    domains: set = field(default_factory=set)   # resolved clock *source* paths
    names: set = field(default_factory=set)     # leaf names of those sources


# The CDC / combinational-loop *result* records (CDCCrossing, CombLoop) and the
# Tarjan SCC helpers live with their analyses in rtl_lint; this module keeps only
# the shared engine primitives (``flow_edges``, ``clock_domain_map`` + its
# ``ClockDomain`` return type) those analyses consume.


# ── Core: Signal Tracer ─────────────────────────────────────────────
class SignalTracer:
    """Analyzes signal drivers and loads in an elaborated SV design."""

    def __init__(self, compilation, *, unroll=False, max_unroll=2048):
        self._comp = compilation
        self._root = compilation.getRoot()
        self._sm = compilation.sourceManager
        # Constant-condition pruning / procedural loop unrolling.  When on, the
        # per-block walk evaluates constant if/case conditions to drop dead
        # branches and unrolls constant-bound for/repeat loops (folding p[i] to
        # concrete bits).  Off => the flat walk, byte-for-byte the prior output.
        # max_unroll bounds the *total* unrolled iterations per block.
        self._unroll = bool(unroll)
        self._max_unroll = max(0, int(max_unroll))
        # AnalysisManager.analyze() requires a fully-elaborated compilation;
        # getSemanticDiagnostics() forces elaboration (sets isElaborated).
        _ = compilation.getSemanticDiagnostics()
        self._mgr = analysis.AnalysisManager()
        self._mgr.analyze(compilation)
        self._proc_edge_cache = {}
        # Clock-domain caches (see clock_domain_map).
        self._clock_tmpl_cache = {}    # body key -> {driver path -> [clk path]}
        self._clock_domain_cache = {}    # reset-glob key -> {reg path -> ClockDomain}
        # Demand-driven flow-graph caches (see _build_flow_edges / flow).
        self._inst_proc_cache = {}     # inst path -> [FlowEdge] (proc/assign)
        self._inst_port_cache = {}     # inst path -> [FlowEdge] (own ports)
        self._child_cache = {}         # inst path -> [child InstanceSymbol]
        self._ancestor_cache = {}      # inst path -> [ancestor InstanceSymbol]
        self._owner_cache = {}         # node path -> owning InstanceSymbol
        self._flow_index_cache = {}    # inst path -> (by_source, by_target)
        self._proc_index_cache = {}    # inst path -> proc-only (by_src, by_tgt)
        self._registered_cache = {}    # node path -> bool (driven by a flop)
        # Upward / lateral hierarchical procedural references anchor at the
        # *referencing* instance, so they are invisible from the referenced
        # side's owner/ancestor lookup.  Recovered via a one-time index that is
        # built only when the design actually contains such a reference (a
        # port-wired design builds nothing and stays fully demand-driven).
        self._ext_ref_bodies = None    # {canonical body key} with such a ref
        self._ext_proc_index = None    # {node path -> [FlowEdge]} or None
        self._ext_proc_index_built = False

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
        # An always/initial block is unnamed; fall back to the instance it
        # lives in (its hierarchical path) so the line reads, e.g.,
        # "always_ff block in trace_top.u_dp.u_pipe" instead of "(anonymous)".
        cs_name = safe_str(getattr(cs, "name", ""), "")
        if not cs_name:
            cs_name = safe_str(getattr(cs, "hierarchicalPath", ""), "")
        if not cs_name:
            cs_name = "(anonymous)"
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

    def _read_bits_of(self, expr, symbol):
        """The bit range of `symbol` read within `expr`, or None (whole)."""
        if expr is None:
            return None
        key = symbol_key(symbol)
        for sym, bounds in expr_reads_with_bounds(expr):
            if symbol_key(sym) == key:
                return bounds
        return None

    @staticmethod
    def _proc_read_bits(proc, proc_symbol):
        """Span of `proc_symbol`'s read ranges in a procedure, or None."""
        key = symbol_key(proc_symbol)
        spans = []
        for r in (getattr(proc, "readSet", None) or []):
            try:
                if symbol_key(r.symbol) == key:
                    b = r.bitRange
                    spans.append((int(b[0]), int(b[1])))
            except Exception:
                pass
        if not spans:
            return None
        return (min(s[0] for s in spans), max(s[1] for s in spans))

    def _load_bits(self, symbol, bounds):
        """(display_label, bounds) for a load reading `symbol` over `bounds`.
        A non-simple type is kept conservatively (no range), so a bit-select
        never drops a reader whose coordinates we can't trust."""
        if bounds is None or not self._is_simple_vector(symbol):
            return "", None
        norm = self._norm_bits(symbol, bounds)
        return (bit_label(norm) if norm else ""), bounds

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
                    bits, bounds = self._load_bits(
                        symbol, self._read_bits_of(expr, symbol))
                    loads.append(LoadInfo(
                        kind="port_connection", description=desc,
                        instance_name=inst_name, port_name=port.name,
                        port_direction=port.direction.name,
                        scope_path=inst_path, file=f, line=ln,
                        bits=bits, bounds=bounds))
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
                bits, bounds = self._load_bits(
                    symbol, self._read_bits_of(rhs, symbol))
                loads.append(LoadInfo(
                    kind="continuous_assign", description=desc,
                    scope_path=scope_inst.hierarchicalPath if scope_inst else "",
                    file=f, line=ln, bits=bits, bounds=bounds))

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
                bits, bounds = self._load_bits(
                    symbol, self._proc_read_bits(proc, proc_symbol))
                loads.append(LoadInfo(
                    kind="procedural", description=desc,
                    scope_path=scope_inst.hierarchicalPath if scope_inst else "",
                    file=f, line=ln, bits=bits, bounds=bounds))
            except Exception:
                continue
        return loads

    # ── dataflow graph ───────────────────────────────────────────────

    def _sym_path(self, sym):
        return symbol_key(sym)

    def _sym_type(self, sym):
        try:
            return str(sym.type)
        except Exception:
            return ""

    @staticmethod
    def _is_simple_vector(sym):
        """True for a scalar or a little-endian ``[N:0]`` packed bit vector — the
        types where a declared bit index equals slang's internal LSB0 offset, so
        our bit arithmetic and slang's bounds agree.  Big-endian ``[0:N]`` vectors
        and multi-bit-element packed arrays (``[3:0][7:0]``) are excluded: their
        bits are reported conservatively (whole signal) rather than risk a
        declared-vs-internal coordinate mismatch."""
        try:
            t = sym.type
            if t.isScalar:
                return True
            # Descending AND zero-based: a declared index equals slang's internal
            # LSB0 offset only when the low bound is 0.  A non-zero LSB (`[8:1]`,
            # `[15:8]`) is descending too but its declared bits are offset from
            # the internal ones, so it must fall back to whole-signal rather than
            # emit mixed declared/internal bit labels.
            return bool(t.isPackedArray and t.elementType.bitWidth == 1
                        and t.range.isDescending and t.range.right == 0)
        except Exception:
            return False

    def _to_internal(self, sym, rng):
        """Map a user-written declared bit range to slang's internal LSB0
        coordinates (identity for little-endian) so a `-s sig[bits]` query lines
        up with the driver/read bounds the analysis reports.  Best-effort: a
        non-vector or unfoldable type is returned unchanged."""
        if rng is None:
            return None
        try:
            t = sym.type
            if not (t.isPackedArray and t.elementType.bitWidth == 1):
                return rng
            r = t.range
            a, b = r.translateIndex(int(rng[0])), r.translateIndex(int(rng[1]))
            return (min(a, b), max(a, b))
        except Exception:
            return rng

    def _norm_bits(self, sym, bounds):
        """Normalize an edge bit range for display/propagation: None for a range
        that spans the whole signal (so whole-signal edges stay byte-for-byte
        identical — additive output), and None for a type whose declared and
        internal bit numbering may differ (big-endian / packed array), which is
        then handled conservatively at signal granularity."""
        if bounds is None or not self._is_simple_vector(sym):
            return None
        full = self._signal_full_bounds(sym)
        b = (int(bounds[0]), int(bounds[1]))
        return None if full is not None and b == full else b

    def _edge_bits(self, src, dst, sb, db, off, segments=None):
        """Normalize an edge's (source_bits, target_bits, bit_offset, segments).

        Bits and offset are trusted only when BOTH endpoints are simple vectors
        (declared == internal LSB0).  If either side is a big-endian vector or a
        multi-bit-element packed array, the whole edge degrades to whole→whole
        (None, None, None, None): a wrong source width would otherwise mis-clamp
        even a simple target's bits.  Conservative and never mis-numbered.  A
        permutation map is carried through only when every segment is precise;
        otherwise it drops (the coarse source/target bits still apply)."""
        if not (self._is_simple_vector(src) and self._is_simple_vector(dst)):
            return (None, None, None, None)
        seg = None
        if segments:
            if all(s is not None and d is not None and o is not None
                   for (s, d, o) in segments):
                seg = tuple(segments)
        return (self._norm_bits(src, sb), self._norm_bits(dst, db), off, seg)

    def _copy_bits(self, src_sym, src_bits, dst_sym, dst_bits):
        """Bit correspondence for a positional copy (assign / select / trunc /
        zero-extend): LSB-aligned overlap + offset.  Returns
        (source_bits, target_bits, bit_offset)."""
        s = src_bits if src_bits is not None else self._signal_full_bounds(src_sym)
        d = dst_bits if dst_bits is not None else self._signal_full_bounds(dst_sym)
        if s is None or d is None:
            return (src_bits, dst_bits, None)
        n = min(s[1] - s[0] + 1, d[1] - d[0] + 1)   # overlap width
        sb = (s[0], s[0] + n - 1)
        db = (d[0], d[0] + n - 1)
        return (sb, db, db[0] - sb[0])               # target_bit = source_bit + off

    def _make_flow_edge(self, source, target, kind, description,
                        loc_sym=None, source_bits=None, target_bits=None,
                        bit_offset=None):
        if source is None or target is None:
            return None
        f, ln = self._loc_sym(loc_sym) if loc_sym is not None else ("", 0)
        sb, db, off, seg = self._edge_bits(source, target, source_bits,
                                           target_bits, bit_offset)
        return FlowEdge(
            source=self._sym_path(source),
            target=self._sym_path(target),
            source_type=self._sym_type(source),
            target_type=self._sym_type(target),
            kind=kind,
            description=description,
            file=f,
            line=ln,
            source_bits=sb,
            target_bits=db,
            bit_offset=off,
            segments=seg,
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
                clocked = False
                if pkind == ast.SymbolKind.ContinuousAssign:
                    kind, desc = "continuous_assign", "assign"
                elif pkind == ast.SymbolKind.ProceduralBlock:
                    kind, desc = "procedural", procedure_label(proc)
                    # A registered edge: its target is held by a flip-flop /
                    # latch, so it breaks combinational feedback and marks a
                    # clock-domain boundary for CDC.
                    clocked = self._proc_is_clocked(proc)
                else:
                    kind, desc = "procedure", str(pkind)
                f, ln = self._loc_sym(proc.analyzedSymbol)

                if pkind == ast.SymbolKind.ProceduralBlock:
                    pairs = self._proc_statement_deps(proc, drivers)
                else:
                    # A single continuous assign: every read feeds the LHS.
                    pairs = self._continuous_assign_pairs(proc)

                for src, dst, sbits, dbits, off, segs in pairs:
                    sb, db, o, sg = self._edge_bits(src, dst, sbits, dbits,
                                                    off, segs)
                    templates.append((
                        self._sym_path(src), self._sym_path(dst),
                        self._sym_type(src), self._sym_type(dst),
                        kind, desc, f, ln, clocked, sb, db, o, sg))
            except Exception:
                continue
        self._proc_edge_cache[key] = templates
        return templates

    @staticmethod
    def _driver_bounds(d):
        try:
            b = d.bounds
            return (int(b[0]), int(b[1]))
        except Exception:
            return None

    @staticmethod
    def _read_bounds(r):
        try:
            b = r.bitRange
            return (int(b[0]), int(b[1]))
        except Exception:
            return None

    # Binary operators that preserve bit position (result bit i depends only on
    # bit i of each operand) — so a source bit maps straight through.  Arithmetic
    # (carry mixes bits), shifts by a dynamic amount, comparisons, and reductions
    # are *not* here and fall back to the conservative whole-signal contribution.
    _BITWISE_BIN = frozenset({"BinaryAnd", "BinaryOr", "BinaryXor", "BinaryXnor"})

    @staticmethod
    def _expr_width(expr):
        try:
            return int(expr.type.bitWidth)
        except Exception:
            return 0

    def _seg(self, src_sym, src_bits, dst_base):
        """One positional-copy segment: src bits (LSB-aligned, clamped) drive
        the dst bits ``dst_base``.  Returns (src_sym, src_bits, dst_bits, off)."""
        sb, db, off = self._copy_bits(src_sym, src_bits, None, dst_base)
        return (src_sym, sb, db, off)

    @staticmethod
    def _live(syms, skip):
        """Symbols minus the bound loop variables (treated as constants)."""
        return [s for s in syms if symbol_key(s) not in skip] if skip else syms

    def _mix(self, dst_base, expr, eval_ctx=None, skip=frozenset()):
        """Conservative contribution: every read of `expr` feeds the whole
        ``dst_base`` with no positional map (offset None) — for arithmetic,
        reductions, dynamic indices, and anything not structurally understood."""
        return [(s, sb, dst_base, None)
                for (s, sb) in expr_reads_with_bounds(expr, eval_ctx)
                if is_data_symbol(s) and symbol_key(s) not in skip]

    def _map_concat(self, dst_base, operands, depth, eval_ctx=None,
                    skip=frozenset()):
        """Map a concatenation onto ``dst_base``.  The first operand is the MSB;
        assignment aligns by LSB (SV truncation drops the high bits), so place
        each operand by its bit position within the concat, lowest first, and
        drop any that fall above ``dst_base`` (truncated away)."""
        dlo, dhi = dst_base
        widths = [self._expr_width(op) for op in operands]
        out = []
        rpos = sum(widths)          # exclusive top of the next operand (RHS coords)
        for op, w in zip(operands, widths):
            if w <= 0:
                return out          # can't place precisely; safety net covers it
            rlo, rhi = rpos - w, rpos - 1
            rpos = rlo
            d_lo, d_hi = dlo + rlo, dlo + rhi
            if d_lo > dhi or d_hi < dlo:
                continue            # operand truncated away
            out += self._map_rhs((max(d_lo, dlo), min(d_hi, dhi)), op, depth + 1,
                                  eval_ctx, skip)
        return out

    def _map_rhs(self, dst_base, rhs, depth=0, eval_ctx=None, skip=frozenset()):
        """Structural bit-flow: how ``rhs`` drives the dst bits ``dst_base``.

        Returns [(src_sym, src_bits, dst_bits, offset)] — offset set for a
        positional copy, None for a many-to-many (whole-signal) contribution.
        Precise for value accesses, concatenation, mux (``?:``), and bitwise
        and/or/xor/not; conservative (whole source) for arithmetic, reductions,
        shifts, and dynamic indices.  ``dst_base`` is a concrete (lo, hi).  An
        ``eval_ctx`` folds loop-variable indices to concrete bits (unrolling),
        and ``skip`` are the bound loop variables to read as constants — so
        ``p[i]`` with ``i`` bound counts as a lone value access, not ``a[b]``."""
        if rhs is None or dst_base is None or depth > 64:
            return []
        tn = type(rhs).__name__
        if tn == "ConversionExpression":
            return self._map_rhs(dst_base, getattr(rhs, "operand", None), depth + 1,
                                 eval_ctx, skip)
        # A single value access (named / static select) is a positional copy.
        sym, sbits = lsp_bounds(rhs, eval_ctx)
        if sym is not None and is_data_symbol(sym) \
                and len(self._live(expr_symbols(rhs), skip)) == 1:
            return [self._seg(sym, sbits, dst_base)]
        if tn == "ConcatenationExpression":
            return self._map_concat(dst_base, list(rhs.operands), depth, eval_ctx,
                                    skip)
        if tn == "ConditionalExpression":
            out = []
            for arm in (getattr(rhs, "left", None), getattr(rhs, "right", None)):
                out += self._map_rhs(dst_base, arm, depth + 1, eval_ctx, skip)
            for cond in (getattr(rhs, "conditions", None) or []):
                for s in self._live(expr_symbols(getattr(cond, "expr", cond)), skip):
                    if is_data_symbol(s):
                        out.append((s, None, dst_base, None))   # predicate: whole
            return out
        if tn == "BinaryExpression":
            if getattr(getattr(rhs, "op", None), "name", "") in self._BITWISE_BIN:
                return (self._map_rhs(dst_base, getattr(rhs, "left", None),
                                      depth + 1, eval_ctx, skip)
                        + self._map_rhs(dst_base, getattr(rhs, "right", None),
                                        depth + 1, eval_ctx, skip))
            return self._mix(dst_base, rhs, eval_ctx, skip)
        if tn == "UnaryExpression":
            if getattr(getattr(rhs, "op", None), "name", "") == "BitwiseNot":
                return self._map_rhs(dst_base, getattr(rhs, "operand", None),
                                     depth + 1, eval_ctx, skip)
            return self._mix(dst_base, rhs, eval_ctx, skip)
        return self._mix(dst_base, rhs, eval_ctx, skip)

    def _segments_for(self, dst_base, rhs, allowed_syms, eval_ctx=None,
                      skip=frozenset()):
        """Per-source segments of ``rhs`` driving ``dst_base``, restricted to the
        symbols in ``allowed_syms``.  Any allowed symbol not placed precisely is
        added as a whole-signal contributor, so the emitted source set is exactly
        ``allowed_syms`` — preserving the symbol-level connectivity (parity)."""
        allowed = {symbol_key(s): s for s in allowed_syms}
        segs, covered = [], set()
        for (sym, sb, db, off) in self._map_rhs(dst_base, rhs, 0, eval_ctx, skip):
            k = symbol_key(sym)
            if k in allowed:
                segs.append((sym, sb, db, off))
                covered.add(k)
        for k, sym in allowed.items():
            if k not in covered:
                segs.append((sym, None, dst_base, None))
        return segs

    def _assignment_bit_pairs(self, node, eval_ctx=None, skip=frozenset()):
        """Bit-aware (src, dst, src_bits, dst_bits, offset) for one assignment.

        Targets are ``expr_symbols(left)`` and sources are exactly
        ``expr_symbols(right)`` — identical connectivity to the old symbol-only
        walk (parity) — with bit ranges and a copy offset attached via the
        structural analyzer.  A single target gets full structural precision
        (concat / mux / bitwise / select); a multi-target LHS concat is kept
        conservative per source read.  An ``eval_ctx`` (loop variable bound)
        folds ``p[i]``-style indices to concrete bits during unrolling, and
        ``skip`` drops those bound loop variables from the source/target symbol
        sets so an unrolled ``p[i+1] = p[i]`` reads as the single value ``p``.
        """
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        tgts = self._live(expr_symbols(left), skip)
        if not tgts:
            return []
        reads = self._live(expr_symbols(right), skip)
        if len(tgts) == 1:
            dst = tgts[0]
            dst_base = lsp_bounds(left, eval_ctx)[1] or self._signal_full_bounds(dst)
            if dst_base is None:
                return [(s, dst, sb, None, None, None)
                        for (s, sb) in expr_reads_with_bounds(right, eval_ctx)
                        if symbol_key(s) not in skip]
            return [(s, dst, sb, db, off, None)
                    for (s, sb, db, off)
                    in self._segments_for(dst_base, right, reads, eval_ctx, skip)]
        out = []
        for src, sb in expr_reads_with_bounds(right, eval_ctx):
            if symbol_key(src) in skip:
                continue
            for dst in tgts:
                out.append((src, dst, sb, None, None, None))
        return out

    @staticmethod
    def _fuse_segments(segs):
        """Fuse the bit segments of one (src, dst) symbol pair into a single
        edge's ``(source_bits, target_bits, bit_offset, segments)``:

          * one segment            -> that segment, no permutation map
          * all share one offset   -> a single spanning affine copy
          * several precise copies  -> spanning bits + the permutation map
                                      (``segments``), bit_offset None
          * any whole-signal / non-copy contribution -> whole signal

        A precise copy is one with concrete ``(src_bits, dst_bits, offset)``;
        the permutation map is what lets a bit reversal keep ``din[7]->rev[0]``
        instead of collapsing to a single affine offset it cannot express."""
        uniq, seen = [], set()
        for s in segs:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        if not uniq:
            return (None, None, None, None)
        if len(uniq) == 1:
            sb, db, off = uniq[0]
            return (sb, db, off, None)
        if any(sb is None or db is None or off is None for (sb, db, off) in uniq):
            return (None, None, None, None)          # an imprecise contribution
        slo = min(sb[0] for (sb, _d, _o) in uniq)
        shi = max(sb[1] for (sb, _d, _o) in uniq)
        dlo = min(db[0] for (_s, db, _o) in uniq)
        dhi = max(db[1] for (_s, db, _o) in uniq)
        if len({o for (_s, _d, o) in uniq}) == 1:
            return ((slo, shi), (dlo, dhi), uniq[0][2], None)   # one affine copy
        segments = tuple(sorted(uniq, key=lambda t: (t[1], t[0])))
        return ((slo, shi), (dlo, dhi), None, segments)

    def _fuse_pairs(self, pairs):
        """Group raw per-segment ``(src, dst, src_bits, dst_bits, offset, ...)``
        tuples by their (src, dst) symbol pair and fuse each group into one
        ``(src, dst, src_bits, dst_bits, offset, segments)`` edge tuple via
        :meth:`_fuse_segments`.  First-seen order of each pair is preserved.

        This mirrors the edge-build's key dedup (one edge per
        ``(src,dst,kind,file,line)``): a pair carrying several precise copies
        keeps the per-bit permutation (``o = {a[3:0], a[7:4]}`` stays mapped),
        and anything not expressible as positional copies downgrades to the
        whole signal — conservative, reachable from every bit."""
        groups, order = {}, []
        for tup in pairs:
            src, dst, sb, db, off = tup[0], tup[1], tup[2], tup[3], tup[4]
            k = (self._sym_path(src), self._sym_path(dst))
            if k not in groups:
                groups[k] = (src, dst, [])
                order.append(k)
            groups[k][2].append((sb, db, off))
        out = []
        for k in order:
            src, dst, segs = groups[k]
            sb, db, off, segments = self._fuse_segments(segs)
            out.append((src, dst, sb, db, off, segments))
        return out

    def _collapse_pairs(self, pairs):
        """Fuse the per-source segments of one continuous assign — see
        :meth:`_fuse_pairs`."""
        return self._fuse_pairs(pairs)

    def _continuous_assign_pairs(self, proc):
        """Bit-aware (src, dst, src_bits, dst_bits, offset, segments) for one
        continuous assign.  A single-driver assign gets full structural precision
        from its expression (over the same readSet symbols, so parity holds); a
        multi-driver LHS concat keeps the conservative reads x drivers map."""
        drv = [(d.symbol, self._driver_bounds(d)) for d in (proc.drivers or [])
               if is_data_symbol(d.symbol)]
        rds = [(r.symbol, self._read_bounds(r)) for r in (proc.readSet or [])
               if is_data_symbol(r.symbol)]
        asgn = getattr(proc.analyzedSymbol, "assignment", None)
        if len(drv) == 1 and asgn is not None:
            dsym, dbnd = drv[0]
            dst_base = dbnd or self._signal_full_bounds(dsym)
            if dst_base is not None:
                allowed = [s for (s, _b) in rds]
                segs = self._segments_for(dst_base, getattr(asgn, "right", None),
                                          allowed)
                return self._collapse_pairs(
                    [(s, dsym, sb, db, off) for (s, sb, db, off) in segs])
        out = []
        for dsym, dbnd in drv:
            for rsym, rbnd in rds:
                out.append((rsym, dsym, rbnd, dbnd, None, None))
        return out

    def _proc_statement_deps(self, proc, drivers):
        """Per-statement LHS<-RHS bit-aware deps + conservative control reads
        for one procedural block.  Returns unique
        (src_sym, dst_sym, src_bits, dst_bits, bit_offset, segments) tuples.

        Data is precise (an assignment's RHS feeds only its own LHS).  With
        unrolling enabled, constant if/case branches are pruned and constant-
        bound for/repeat loops are unrolled (so per-iteration ``p[i]`` indices
        fold to concrete bits); otherwise the flat walk attributes every control
        condition to every driver.  A symbol pair fed by a single coherent set of
        positional copies keeps its bit map (an affine offset, or a permutation
        via ``segments``); a pair fed by more than one statement/condition loses
        it (whole signal), which never under-reports.
        """
        bit_pairs = None
        control = []
        if self._unroll:
            wp, wc, ok = self._walk_proc_pairs(proc)
            if ok and (wp or wc):
                bit_pairs, control = wp, wc
        if bit_pairs is None:
            bit_pairs, control = self._flat_proc_pairs(proc)

        if bit_pairs is None:
            # Degenerate walk; fall back to the coarse reads x drivers so we
            # never silently under-report.
            reads = [r.symbol for r in (proc.readSet or [])
                     if is_data_symbol(r.symbol)]
            return [(s, d, None, None, None, None) for s in reads for d in drivers]

        control = [s for s in control if is_data_symbol(s)]
        pairs = {}        # (src_path, dst_path) -> 6-tuple

        def add(src, dst, sb=None, db=None, off=None, segs=None):
            if not (is_data_symbol(src) and is_data_symbol(dst)):
                return
            k = (self._sym_path(src), self._sym_path(dst))
            if k in pairs:
                s, d, *_ = pairs[k]
                pairs[k] = (s, d, None, None, None, None)   # conflict -> whole
            else:
                pairs[k] = (src, dst, sb, db, off, segs)

        for (src, dst, sb, db, off, segs) in bit_pairs:
            add(src, dst, sb, db, off, segs)
        for dst in drivers:
            for cr in control:
                add(cr, dst)
        return list(pairs.values())

    def _flat_proc_pairs(self, proc):
        """The flat body walk — used when unrolling is off or unproductive.
        Collects every AssignmentExpression's bit pairs (whole-signal indices)
        plus all control-condition reads.  Returns (bit_pairs | None, control);
        a None bit_pairs signals a degenerate walk (caller uses readSet x
        drivers).  This is exactly the pre-unrolling behaviour."""
        assigns = []
        control = []

        def collect(node):
            if type(node).__name__ == "AssignmentExpression":
                assigns.append(node)
            else:
                control.extend(self._statement_control_reads(node))

        try:
            proc.analyzedSymbol.body.visit(f=collect)
        except Exception:
            return (None, control)
        if not assigns:
            return (None, control)
        bit_pairs = []
        for node in assigns:
            bit_pairs.extend(self._assignment_bit_pairs(node))
        return (bit_pairs, control)

    def _merge_segments(self, pairs):
        """Fuse the per-iteration segments of an unrolled loop that share a
        (src, dst) symbol pair: one consistent copy offset merges into a single
        spanning-range affine edge (so ``y[i] = a[i+k]`` keeps its bit map); a
        true permutation (``rev[i] = din[7-i]``, offsets differ per bit) keeps
        each segment as a permutation map; any whole-signal contribution
        downgrades the pair to the whole signal.  A sparse same-offset index set
        is over-approximated to its [min, max] span (never under-reports)."""
        return self._fuse_pairs(pairs)

    def _walk_proc_pairs(self, proc):
        """Structured prune + unroll walk of a procedural block.

        Descends the statement tree: constant if/case conditions skip the dead
        branch; constant-bound for/repeat loops are unrolled with the loop
        variable bound in an EvalContext, so per-iteration ``p[i]`` indices fold
        to concrete bits.  Returns (bit_pairs, control, ok); ok=False asks the
        caller to fall back to the flat walk.  Never raises.  Anything not
        handled — while/do-while/foreach,
        non-constant bounds, an over-budget loop, an unknown statement kind —
        degrades to the conservative flat handling for that subtree, so the
        result never under-reports edges.

        Pairs assigned outside any unrolled loop are produced exactly as the flat
        walk would (eval context off, nothing skipped), so a block with no
        prunable/unrollable construct yields byte-for-byte the flat result; only
        loop-generated segments are offset-merged.  ``bound`` holds the symbol
        keys of the currently-bound loop variables (read as constants, not data).
        """
        try:
            sym = proc.analyzedSymbol
            ctx = make_eval_context(sym)
        except Exception:
            return ([], [], False)
        if ctx is None:
            return ([], [], False)

        loop_pairs = []      # segments produced inside an unrolled loop
        top_pairs = []       # segments produced at block top level (flat parity)
        control = []
        bound = set()        # symbol_key of currently-bound loop variables
        budget = [self._max_unroll]      # global iteration budget across loops

        def emit_assign(node):
            in_loop = bool(bound)
            try:
                segs = self._assignment_bit_pairs(
                    node, ctx if in_loop else None, frozenset(bound))
            except Exception:
                return
            (loop_pairs if in_loop else top_pairs).extend(segs)

        def emit_expr(expr):
            if expr is None:
                return

            def cb(n):
                if type(n).__name__ == "AssignmentExpression":
                    emit_assign(n)
            try:
                expr.visit(f=cb)
            except Exception:
                pass

        def flat_subtree(node):
            # Safety net for an unrecognized statement kind: exactly the flat
            # walk (assignments + control reads) over just this subtree.
            def cb(n):
                if type(n).__name__ == "AssignmentExpression":
                    emit_assign(n)
                else:
                    control.extend(self._statement_control_reads(n))
            try:
                node.visit(f=cb)
            except Exception:
                pass

        def conservative_stmt(node):
            # Loop/branch kept whole: its bounds/conditions become control reads
            # (every driver) and its body is walked once — never under-reports.
            try:
                control.extend(self._statement_control_reads(node))
            except Exception:
                pass
            walk(getattr(node, "body", None))

        def handle_cond(node):
            conds = list(getattr(node, "conditions", None) or [])
            if_true = getattr(node, "ifTrue", None)
            if_false = getattr(node, "ifFalse", None)
            has_pattern = any(getattr(c, "pattern", None) is not None for c in conds)
            verdict = None
            if conds and not has_pattern:
                results = [try_eval_bool(getattr(c, "expr", None), ctx)
                           for c in conds]
                if any(r is False for r in results):
                    verdict = False
                elif all(r is True for r in results):
                    verdict = True
            if verdict is True:
                walk(if_true)
                return
            if verdict is False:
                walk(if_false)
                return
            for c in conds:                  # non-constant: predicate is control
                control.extend(expr_symbols(getattr(c, "expr", c)))
            walk(if_true)
            walk(if_false)

        def handle_case(node):
            expr = getattr(node, "expr", None)
            items = list(getattr(node, "items", None) or [])
            default = getattr(node, "defaultCase", None)
            sel = try_eval_int(expr, ctx)
            if sel is not None:
                matched, bail = None, False
                for item in items:
                    vals = [try_eval_int(lab, ctx)
                            for lab in (getattr(item, "expressions", None) or [])]
                    if any(v == sel for v in vals if v is not None):
                        matched = item        # definitely taken (first match wins)
                        break
                    if any(v is None for v in vals):
                        bail = True           # a non-constant label might match
                        break
                if not bail:
                    if matched is not None:
                        walk(getattr(matched, "stmt", None))
                    elif default is not None:
                        walk(default)
                    return
            control.extend(expr_symbols(expr))   # non-constant / bailed
            for item in items:
                walk(getattr(item, "stmt", None))
            walk(default)

        def bind_initial(loop_vars):
            for v in loop_vars:
                init = getattr(v, "initializer", None)
                if init is None:
                    return False
                try:
                    iv = init.eval(ctx)
                except Exception:
                    return False
                if not bool(iv) or iv.hasUnknown():
                    return False
                ctx.createLocal(v, iv)
            return True

        def try_unroll_for(node):
            loop_vars = list(getattr(node, "loopVars", None) or [])
            stop = getattr(node, "stopExpr", None)
            steps = list(getattr(node, "steps", None) or [])
            body = getattr(node, "body", None)
            # Only the `for (int i = ...; ...; ...)` form (declared loop vars with
            # foldable initializers) is unrolled; other shapes go conservative.
            if not loop_vars or stop is None or body is None:
                return False
            keys = [symbol_key(v) for v in loop_vars]
            try:
                if not bind_initial(loop_vars):
                    return False
                # Pre-flight: count iterations WITHOUT walking the body, so an
                # over-budget loop bails wholesale (no partial unroll => the
                # never-under-report invariant holds).
                n = 0
                while True:
                    cv = stop.eval(ctx)
                    if not bool(cv) or cv.hasUnknown():
                        return False
                    if not cv.isTrue():
                        break
                    n += 1
                    if n > budget[0]:
                        return False
                    for st in steps:
                        st.eval(ctx)
                # Real pass: rebind to the initial value and walk the body n times.
                if not bind_initial(loop_vars):
                    return False
                bound.update(keys)
                try:
                    for _ in range(n):
                        walk(body)
                        for st in steps:
                            st.eval(ctx)
                finally:
                    bound.difference_update(keys)
                budget[0] -= n
                return True
            except Exception:
                return False
            finally:
                for v in reversed(loop_vars):
                    try:
                        ctx.deleteLocal(v)
                    except Exception:
                        pass

        def handle_repeat(node):
            count = try_eval_int(getattr(node, "count", None), ctx)
            body = getattr(node, "body", None)
            if count is None or body is None or count < 0 or count > budget[0]:
                return conservative_stmt(node)
            for _ in range(count):
                walk(body)
            budget[0] -= count

        def walk(node):
            if node is None:
                return
            tn = type(node).__name__
            if tn == "StatementList":
                for s in (getattr(node, "list", None) or []):
                    walk(s)
            elif tn == "BlockStatement":
                walk(getattr(node, "body", None))
            elif tn == "ExpressionStatement":
                emit_expr(getattr(node, "expr", None))
            elif tn == "ConditionalStatement":
                handle_cond(node)
            elif tn == "CaseStatement":
                handle_case(node)
            elif tn == "ForLoopStatement":
                if not try_unroll_for(node):
                    conservative_stmt(node)
            elif tn == "RepeatLoopStatement":
                handle_repeat(node)
            elif tn in ("WhileLoopStatement", "DoWhileLoopStatement",
                        "ForeachLoopStatement"):
                conservative_stmt(node)
            elif tn == "TimedStatement":
                walk(getattr(node, "stmt", None))
            elif tn in ("VariableDeclStatement", "EmptyStatement",
                        "ReturnStatement", "BreakStatement", "ContinueStatement",
                        "DisableStatement"):
                return
            else:
                flat_subtree(node)

        try:
            walk(getattr(sym, "body", None))
        except Exception:
            return ([], [], False)
        return (self._merge_segments(loop_pairs) + top_pairs, control, True)

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


    # ── per-instance edge producers (shared by lazy + whole-graph) ──────
    #
    # Every dataflow edge is *anchored* at exactly one instance: the instance
    # whose procedure / continuous-assign drives it, or whose port connection
    # carries it.  Producing edges one instance at a time lets the demand-
    # driven `flow()` materialize only the instances it actually visits, while
    # `_build_flow_edges()` reuses the same producers to build the whole graph.

    def _instance_proc_edges(self, inst):
        """Procedural / continuous-assign edges anchored at `inst`.

        Stamped from the canonical body's cached templates into this
        instance's namespace, so deduplicated instances (generate arrays,
        repeated modules) keep their edges and each unique module body is
        walked only once.  Memoized per instance.
        """
        key = self._sym_path(inst)
        cached = self._inst_proc_cache.get(key)
        if cached is not None:
            return cached
        view = canonical_view(self._root, inst)
        edges = [
            FlowEdge(source=view.remap(src), target=view.remap(dst),
                     source_type=st, target_type=dt, kind=kind,
                     description=desc, file=f, line=ln, clocked=clk,
                     source_bits=sb, target_bits=tb, bit_offset=off, segments=sg)
            for (src, dst, st, dt, kind, desc, f, ln, clk, sb, tb, off, sg)
            in self._proc_edge_templates(view)
        ]
        self._inst_proc_cache[key] = edges
        return edges

    def _instance_port_edges(self, inst):
        """Port-connection edges contributed by `inst`'s own connections.

        Each links one of `inst`'s port internals to the net in the parent
        scope it connects to.  Memoized per instance.
        """
        key = self._sym_path(inst)
        cached = self._inst_port_cache.get(key)
        if cached is not None:
            return cached
        edges = []
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
                    # The connection expression (parent scope) feeds the port
                    # internal.  A single (possibly sliced) net is a positional
                    # copy into the port; otherwise each read feeds it whole.
                    rsym, rbits = lsp_bounds(expr)
                    if (rsym is not None and is_data_symbol(rsym)
                            and len(expr_symbols(expr)) == 1):
                        sb, db, off = self._copy_bits(rsym, rbits, port_sym, None)
                        e = self._make_flow_edge(
                            rsym, port_sym, "port_connection",
                            f"{inst.name}.{port.name} input", inst,
                            source_bits=sb, target_bits=db, bit_offset=off)
                        if e is not None:
                            edges.append(e)
                    else:
                        for src, sb in expr_reads_with_bounds(expr):
                            if not is_data_symbol(src):
                                continue
                            e = self._make_flow_edge(
                                src, port_sym, "port_connection",
                                f"{inst.name}.{port.name} input", inst,
                                source_bits=sb)
                            if e is not None:
                                edges.append(e)
                if direction in (ast.ArgumentDirection.Out,
                                 ast.ArgumentDirection.InOut):
                    lval = getattr(expr, 'left', None) or expr
                    dsts = expr_symbols(lval)
                    single = (len(dsts) == 1 and is_data_symbol(dsts[0]))
                    dbits = lsp_bounds(lval)[1] if single else None
                    for dst in dsts:
                        if not is_data_symbol(dst):
                            continue
                        if single:
                            sb, db, off = self._copy_bits(
                                port_sym, None, dst, dbits)
                        else:
                            sb = db = off = None
                        e = self._make_flow_edge(
                            port_sym, dst, "port_connection",
                            f"{inst.name}.{port.name} output", inst,
                            source_bits=sb, target_bits=db, bit_offset=off)
                        if e is not None:
                            edges.append(e)
            except Exception:
                continue
        self._inst_port_cache[key] = edges
        return edges

    def flow_edges(self):
        """The whole-design dataflow edge list — the shared engine primitive the
        lint CDC / combinational-loop analyses consume (they live in rtl_lint;
        this stays a query-side primitive alongside ``clock_domain_map``)."""
        return self._build_flow_edges()

    def _build_flow_edges(self):
        """Whole-design edge list — every instance's proc + port edges,
        deduplicated and sorted.

        The demand-driven `flow()` no longer needs this (it expands only the
        touched neighborhood), but it stays as the exact whole-graph build:
        the oracle the lazy traversal is verified against, and a ready hook
        for any future whole-design consumer.
        """
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
            if getattr(inst, 'body', None) is None:
                continue
            for edge in self._instance_proc_edges(inst):
                add(edge)
            for edge in self._instance_port_edges(inst):
                add(edge)

        edges.sort(key=FlowEdge.key)
        self._flow_edges = edges
        return edges

    # ── demand-driven (lazy) neighborhood expansion ─────────────────────

    def _child_instances(self, inst):
        """Direct child instances declared in `inst`'s body.  Memoized."""
        key = self._sym_path(inst)
        cached = self._child_cache.get(key)
        if cached is not None:
            return cached
        children = []

        def collect(sym):
            children.append(sym)
            return ast.VisitAction.Skip

        body = getattr(inst, 'body', None)
        if body is not None:
            try:
                self._scope_visit(body, {ast.SymbolKind.Instance: collect})
            except Exception:
                pass
        self._child_cache[key] = children
        return children

    def _parent_instance(self, inst):
        """The instance that instantiates `inst`, or None for a top."""
        try:
            scope = getattr(inst, 'parentScope', None)
            body = getattr(scope, 'containingInstance', None) \
                if scope is not None else None
            return getattr(body, 'parentInstance', None) \
                if body is not None else None
        except Exception:
            return None

    def _ancestor_instances(self, inst):
        """`inst`'s instantiation chain, nearest parent first.  Memoized."""
        key = self._sym_path(inst)
        cached = self._ancestor_cache.get(key)
        if cached is not None:
            return cached
        chain = []
        cur = self._parent_instance(inst)
        guard = 0
        while cur is not None and guard < 256:  # guard pathological cycles
            chain.append(cur)
            cur = self._parent_instance(cur)
            guard += 1
        self._ancestor_cache[key] = chain
        return chain

    def _owner_instance(self, node):
        """The instance whose body declares the signal at hierarchical path
        `node`, resolved by name lookup.  None when it cannot be resolved
        (the traversal then simply stops at that node).  Memoized per path.
        """
        if node in self._owner_cache:
            return self._owner_cache[node]
        inst = None
        try:
            sym = self._root.lookupName(node)
            scope = getattr(sym, 'parentScope', None) if sym is not None else None
            body = getattr(scope, 'containingInstance', None) \
                if scope is not None else None
            inst = getattr(body, 'parentInstance', None) \
                if body is not None else None
        except Exception:
            inst = None
        self._owner_cache[node] = inst
        return inst

    def _instance_flow_index(self, inst):
        """Adjacency `(by_source, by_target)` over every edge whose anchor is
        local to `inst`: `inst`'s own proc + port edges, plus the port edges
        of `inst`'s direct children (whose parent-side nets live in `inst`).

        For a signal declared in `inst`, this captures every incident edge
        except those anchored at another instance via a hierarchical
        reference — `_incident_edges` folds those in separately.  Memoized.
        """
        key = self._sym_path(inst)
        cached = self._flow_index_cache.get(key)
        if cached is not None:
            return cached
        by_source, by_target = {}, {}
        seen = set()

        def add(edge):
            ekey = edge.key()
            if ekey in seen:
                return
            seen.add(ekey)
            by_source.setdefault(edge.source, []).append(edge)
            by_target.setdefault(edge.target, []).append(edge)

        for edge in self._instance_proc_edges(inst):
            add(edge)
        for edge in self._instance_port_edges(inst):
            add(edge)
        for child in self._child_instances(inst):
            for edge in self._instance_port_edges(child):
                add(edge)

        index = (by_source, by_target)
        self._flow_index_cache[key] = index
        return index

    def _instance_proc_index(self, inst):
        """`(by_source, by_target)` over only `inst`'s own procedural edges —
        no port edges, no child fan-out.  Used to look up an ancestor's
        hierarchical-reference edges in O(1) without materializing its
        (possibly wide) child-port adjacency.  Memoized."""
        key = self._sym_path(inst)
        cached = self._proc_index_cache.get(key)
        if cached is not None:
            return cached
        by_source, by_target = {}, {}
        for edge in self._instance_proc_edges(inst):
            by_source.setdefault(edge.source, []).append(edge)
            by_target.setdefault(edge.target, []).append(edge)
        index = (by_source, by_target)
        self._proc_index_cache[key] = index
        return index

    @staticmethod
    def _under(path, base):
        """True when hierarchical `path` is `base` or lives inside it."""
        return bool(base) and (path == base or path.startswith(base + "."))

    @staticmethod
    def _body_key(body):
        return safe_str(getattr(body, "hierarchicalPath", ""), "") or str(id(body))

    def _external_ref_bodies(self):
        """Canonical body keys whose procedures reference a signal *outside*
        the body (an upward or lateral hierarchical reference).  Computed once
        from the per-body edge templates — `view.contains` already flags an
        endpoint that escapes the canonical subtree — without materializing any
        per-instance adjacency, so a design with no such reference is detected
        cheaply and the demand-driven path is left untouched."""
        if self._ext_ref_bodies is not None:
            return self._ext_ref_bodies
        bodies, seen = set(), set()
        for inst in iter_instances(self._root):
            if getattr(inst, "body", None) is None:
                continue
            try:
                view = canonical_view(self._root, inst)
            except Exception:
                continue
            bkey = self._body_key(view.body)
            if bkey in seen:
                continue
            seen.add(bkey)
            for tmpl in self._proc_edge_templates(view):
                if not view.contains(tmpl[0]) or not view.contains(tmpl[1]):
                    bodies.add(bkey)
                    break
        self._ext_ref_bodies = bodies
        return bodies

    def _external_proc_index(self):
        """`{node -> [FlowEdge]}` for procedural edges that escape their anchor
        instance, keyed by the escaping (out-of-subtree) endpoint(s).  Returns
        ``None`` when the design has no such reference.  Built once and cached;
        only instances of a body that actually carries an external reference are
        materialized."""
        if self._ext_proc_index_built:
            return self._ext_proc_index
        ext_bodies = self._external_ref_bodies()
        index = None
        if ext_bodies:
            index = {}
            for inst in iter_instances(self._root):
                body = getattr(inst, "body", None)
                if body is None:
                    continue
                try:
                    view = canonical_view(self._root, inst)
                except Exception:
                    continue
                if self._body_key(view.body) not in ext_bodies:
                    continue
                base = safe_str(getattr(body, "hierarchicalPath", ""), "")
                for edge in self._instance_proc_edges(inst):
                    if not self._under(edge.source, base):
                        index.setdefault(edge.source, []).append(edge)
                    if not self._under(edge.target, base):
                        index.setdefault(edge.target, []).append(edge)
        self._ext_proc_index = index
        self._ext_proc_index_built = True
        return index

    def _incident_edges(self, node, mode):
        """Every edge incident to `node` for one BFS step, identical to the
        whole-graph build's per-node adjacency (same edges, same order)."""
        owner = self._owner_instance(node)
        edges = []
        if owner is not None:
            by_source, by_target = self._instance_flow_index(owner)
            edges = list((by_target if mode == "fanin" else by_source).get(node, []))

            # A *downward* procedural hierarchical reference (an ancestor's
            # procedure reading/driving `node` by hierarchical name) anchors at
            # the ancestor, not at `node`'s owner, so it is absent from the
            # owner's index.  Fold in each ancestor's procedural edges that
            # touch `node` via its proc-only index — an O(1) lookup that never
            # pulls in the ancestor's (potentially wide) child-port fan-out.
            for ancestor in self._ancestor_instances(owner):
                psrc, ptgt = self._instance_proc_index(ancestor)
                edges.extend((ptgt if mode == "fanin" else psrc).get(node, []))

        # An *upward* or *lateral* procedural hierarchical reference (a
        # descendant or cousin procedure reading/driving `node` by hierarchical
        # name) anchors at that referencing instance — neither `node`'s owner
        # nor an ancestor — so the lookups above never see it.  Fold in any such
        # edge from the external-reference index (empty/None unless the design
        # actually contains one).
        ext = self._external_proc_index()
        if ext is not None:
            want_source = mode != "fanin"
            for edge in ext.get(node, []):
                if (edge.source if want_source else edge.target) == node:
                    edges.append(edge)

        if not edges:
            return []

        # Match the whole-graph build's per-node ordering: a global sort by
        # (source, target, kind, file, line) leaves each node's sublist in that
        # order.  Dedup defensively (a node reached two ways keeps one edge).
        seen = set()
        ordered = []
        for edge in sorted(edges, key=FlowEdge.key):
            ekey = edge.key()
            if ekey not in seen:
                seen.add(ekey)
                ordered.append(edge)
        return ordered

    # Sentinel: a concrete query range that an edge does not touch at all.
    _NO_OVERLAP = object()

    def _map_range(self, edge, rng, mode):
        """Carry an interesting bit range across one edge to the far node.

        ``rng is None`` means "the whole signal" — a non-bit-select query.  It
        stays None (never narrows), so a whole-signal traversal visits exactly
        the same edges, in the same order, as the symbol-level graph: the
        demand-driven / whole-graph parity (test_flow_lazy) is preserved.

        A concrete (lo, hi) range is intersected with the bits this edge touches
        on the near side and mapped to the far side: precisely via bit_offset
        for a copy-like edge, else falling back to the bits the edge reads /
        drives on the far side (None = whole, the conservative answer for
        arithmetic).  A permutation edge (``segments``) maps the range through
        each matching segment and returns the spanning union of the far bits, so
        a bit-select still converges across a reversal/swap.  Returns _NO_OVERLAP
        when a concrete range misses the edge.
        """
        if rng is None:
            return None
        if edge.segments:
            spans = []
            for (sb, db, off) in edge.segments:
                near = db if mode == "fanin" else sb
                lo, hi = max(rng[0], near[0]), min(rng[1], near[1])
                if lo > hi:
                    continue
                shift = -off if mode == "fanin" else off
                spans.append((lo + shift, hi + shift))
            if not spans:
                return self._NO_OVERLAP
            return (min(lo for lo, _ in spans), max(hi for _, hi in spans))
        near = edge.target_bits if mode == "fanin" else edge.source_bits
        if near is not None:
            lo, hi = max(rng[0], near[0]), min(rng[1], near[1])
            if lo > hi:
                return self._NO_OVERLAP
            portion = (lo, hi)
        else:
            portion = rng
        if edge.bit_offset is not None:
            off = -edge.bit_offset if mode == "fanin" else edge.bit_offset
            return (portion[0] + off, portion[1] + off)
        # Non-copy (arithmetic / reduction): fall to the far-side bits.
        return edge.source_bits if mode == "fanin" else edge.target_bits

    def _is_registered(self, node):
        """True when ``node`` is a register — driven by a sequential (clocked)
        procedure, i.e. the target of a clocked edge.  This is the boundary a
        combinational cone stops at.  Resolved lazily from the node's own
        incident edges (the same ``clocked`` flag the whole engine keys off) and
        memoized, so combinational queries stay demand-driven instead of
        materializing the whole flow graph.

        The test is at *signal* granularity: a node with **any** clocked driver
        is a boundary, even if some of its bits are driven combinationally (a
        signal that is part-latched, part-`assign`ed).  Such a node is then
        excluded from a combinational cone whole, dropping the genuinely
        combinational sub-range — rare in practice, and the conservative call
        keeps the boundary simple and never *crosses* a sequential element."""
        cached = self._registered_cache.get(node)
        if cached is not None:
            return cached
        val = any(e.clocked and e.target == node
                  for e in self._incident_edges(node, "fanin"))
        self._registered_cache[node] = val
        return val

    def flow(self, signal_name, scope_path, mode, max_depth=4, bit_range=None,
             comb=False):
        inst, sym = self._lookup(signal_name, scope_path)
        self._check_bit_range(sym, signal_name, bit_range)

        start = self._sym_path(sym)
        # Match the query to slang's internal bit numbering (identity for
        # little-endian); the declared bit_range is kept on FlowResult for display.
        sel = self._to_internal(sym, bit_range)

        # ``max_depth is None`` means *unbounded*: walk until the frontier dries
        # up.  A combinational cone (``comb``) defaults to this — the cone is
        # bounded by registers, not by a hop count — and still terminates because
        # the design is finite and ``seen_nodes`` caps any feedback through
        # combinational loops.
        unbounded = max_depth is None
        if not unbounded:
            max_depth = max(0, int(max_depth))

        # Demand-driven, bit-aware BFS: the frontier carries (node, range) where
        # range is the bits still of interest (None = whole signal).  An edge is
        # followed only when its bits overlap range, and range is mapped across
        # the edge to the next node — so `-s dout[5]` converges to the exact
        # driving bit.  A whole-signal query keeps range None throughout and
        # reproduces the symbol-level traversal edge-for-edge.
        traversed = []           # (edge, depth) in discovery order
        edge_pos = {}            # ekey -> index into `traversed`
        edge_rngs = {}           # ekey -> [near-range that reached this edge]
        seen_nodes = {(start, sel)}
        frontier = [(start, sel)]
        depth = 0
        while frontier and (unbounded or depth < max_depth):
            depth += 1
            next_frontier = []
            for node, rng in frontier:
                for edge in self._incident_edges(node, mode):
                    far = edge.source if mode == "fanin" else edge.target
                    # Combinational cone: a register node is a sequential
                    # boundary, so don't cross into it.  The start is the BFS
                    # seed (always expanded), so a register *start* still yields
                    # its own combinational D-cone / fan-out; only register
                    # nodes reached as neighbors terminate the cone (and are
                    # themselves excluded).
                    if comb and self._is_registered(far):
                        continue
                    nxt_rng = self._map_range(edge, rng, mode)
                    if nxt_rng is self._NO_OVERLAP:
                        continue
                    ekey = edge.key()
                    # `rng` is the bits of interest on the near node (the target
                    # for fanin, the source for fanout).  An edge can be reached
                    # from several frontier bits; collect every such range so a
                    # permutation edge's segments are trimmed to their union, not
                    # to whichever range happened to arrive first.
                    if ekey not in edge_pos:
                        edge_pos[ekey] = len(traversed)
                        traversed.append((edge, depth))
                        edge_rngs[ekey] = [rng]
                    else:
                        edge_rngs[ekey].append(rng)
                    nxt = edge.source if mode == "fanin" else edge.target
                    if (nxt, nxt_rng) not in seen_nodes:
                        seen_nodes.add((nxt, nxt_rng))
                        next_frontier.append((nxt, nxt_rng))
            frontier = next_frontier

        traversed = [(edge.trimmed_to(edge_rngs[edge.key()], mode), depth)
                     for (edge, depth) in traversed]

        # Report the depth bound that was in force: the requested cap, or — when
        # unbounded — the deepest hop the cone actually reached.
        result_max_depth = (max((d for _e, d in traversed), default=0)
                            if unbounded else max_depth)

        return FlowResult(
            mode=mode, signal_name=signal_name, signal_type=str(sym.type),
            signal_kind=sym.kind.name, scope_path=scope_path,
            scope_module=inst.body.name, start=start,
            edges=traversed, max_depth=result_max_depth, bit_range=bit_range,
            comb=comb,
        )

    def find_path(self, from_signal, from_scope, to_signal, to_scope,
                  comb=False):
        """Find a directional dataflow path from one signal to another.

        Resolves both endpoints to graph nodes, runs the DFS :class:`PathFinder`
        (``comb`` selects the combinational predicate, which never enters a
        register), and packages the node/edge walk into a :class:`PathResult`.  A
        missing endpoint raises a precise SCOPE/SIGNAL error via ``_lookup``; a
        nonexistent path is a normal empty result (``found == False``), not an
        error.
        """
        _from_inst, from_sym = self._lookup(from_signal, from_scope)
        _to_inst, to_sym = self._lookup(to_signal, to_scope)
        start = self._sym_path(from_sym)
        end = self._sym_path(to_sym)
        finder = PathFinder(self)
        nodes, edges = (finder.findComb(start, end) if comb
                        else finder.find(start, end))
        return PathResult(
            from_signal=from_signal, to_signal=to_signal,
            from_scope=from_scope, to_scope=to_scope,
            start=start, end=end,
            start_type=str(from_sym.type), end_type=str(to_sym.type),
            nodes=nodes, edges=edges, comb=comb,
        )

    # ── clock / sequential classification ─────────────────────────────

    @staticmethod
    def _event_is_edge(ev):
        """True when a timing event is edge-sensitive (posedge/negedge/both)."""
        try:
            e = getattr(ev, 'edge', None)
            return e is not None and e in (ast.EdgeKind.PosEdge,
                                           ast.EdgeKind.NegEdge,
                                           ast.EdgeKind.BothEdges)
        except Exception:
            return False

    @staticmethod
    def _proc_timing_events(proc):
        """The leading timing control's events (or [] when level/none)."""
        try:
            tcs = proc.timingControls
        except Exception:
            tcs = None
        if not tcs:
            return []
        try:
            tc = tcs[0].timing
            return list(tc.events) if hasattr(tc, 'events') else [tc]
        except Exception:
            return []

    def _proc_is_clocked(self, proc):
        """True when a procedural block is sequential (registered).

        ``always_ff`` / ``always_latch`` are sequential by construction; a bare
        ``always`` is sequential only when its timing control is edge-sensitive
        (``always @(posedge clk)``) and combinational otherwise (``always @*``).
        Classify by ``procedureKind`` first (proven, cheap), falling back to the
        edge probe only for the ambiguous generic ``always``.
        """
        try:
            pk = safe_str(proc.analyzedSymbol.procedureKind, "")
        except Exception:
            pk = ""
        if "AlwaysFF" in pk or "AlwaysLatch" in pk:
            return True
        if "AlwaysComb" in pk:
            return False
        return any(self._event_is_edge(ev)
                   for ev in self._proc_timing_events(proc))

    def _clock_templates(self, view):
        """Per canonical body: ``{driver_path -> [clock_event_path, …]}``.

        For every sequential procedure, map each driven (non-input-port) data
        signal to the symbols named in the block's *edge* events — clocks plus
        asynchronous resets.  Reset filtering is deferred to
        :meth:`clock_domain_map`, so this stays reset-policy-agnostic and is
        cached once per unique module body (then stamped per instance with
        ``view.remap``, exactly like :meth:`_proc_edge_templates`).
        """
        key = self._body_key(view.body)
        cached = self._clock_tmpl_cache.get(key)
        if cached is not None:
            return cached
        out = {}
        for proc in analyzed_procedures(self._mgr, view.body):
            try:
                if proc.analyzedSymbol.kind != ast.SymbolKind.ProceduralBlock:
                    continue
                if not self._proc_is_clocked(proc):
                    continue
                clk_paths = []
                for ev in self._proc_timing_events(proc):
                    if not self._event_is_edge(ev):
                        continue
                    try:
                        cp = self._sym_path(ev.expr.symbol)
                    except Exception:
                        continue
                    if cp and cp not in clk_paths:
                        clk_paths.append(cp)
                if not clk_paths:
                    continue
                for d in (proc.drivers or []):
                    try:
                        if d.flags & analysis.DriverFlags.InputPort:
                            continue
                    except Exception:
                        pass
                    if not is_data_symbol(d.symbol):
                        continue
                    bucket = out.setdefault(self._sym_path(d.symbol), [])
                    for cp in clk_paths:
                        if cp not in bucket:
                            bucket.append(cp)
            except Exception:
                continue
        self._clock_tmpl_cache[key] = out
        return out

    def _resolve_clock_source(self, node):
        """Resolve a clock node to its *source net* by walking connectivity
        fanin (port connections + continuous assigns) up the hierarchy.

        Two flops driven by the same physical clock then share a domain key
        even when the local clock ports are named differently or live in
        different instances.  Stops at a primary net (no connectivity driver)
        or at a gate/mux (>1 connectivity driver), so a gated/divided clock is
        its own domain.  A visited-set bounds a pathological clock-net cycle.
        """
        cur = node
        seen = {cur}
        for _ in range(256):
            preds = []
            for e in self._incident_edges(cur, "fanin"):
                if e.clocked:
                    continue
                if e.kind not in ("port_connection", "continuous_assign"):
                    continue
                if e.source not in preds:
                    preds.append(e.source)
            if len(preds) != 1 or preds[0] in seen:
                break
            cur = preds[0]
            seen.add(cur)
        return cur

    def clock_domain_map(self, is_reset, reset_key=None):
        """``{registered_node_path -> ClockDomain}`` over the whole design.

        A registered node is any signal driven by a sequential procedure; its
        domain is the *source net* its clock resolves to (``is_reset`` drops
        async-reset events from the clock set).

        The result depends on ``is_reset``, so the cache is keyed by
        ``reset_key`` -- the reset-glob set the predicate was built from.  A
        tracer reused across calls with *different* reset configurations no
        longer hands back the first call's stale map.  When ``reset_key`` is
        None (an ad-hoc caller passing a bare predicate) the map is recomputed
        every call rather than risk serving a result for a different predicate.
        """
        if reset_key is not None and reset_key in self._clock_domain_cache:
            return self._clock_domain_cache[reset_key]
        domains = {}
        for inst in iter_instances(self._root):
            if getattr(inst, 'body', None) is None:
                continue
            try:
                view = canonical_view(self._root, inst)
            except Exception:
                continue
            tmpl = self._clock_templates(view)
            if not tmpl:
                continue
            for drv_canon, clk_canons in tmpl.items():
                node = view.remap(drv_canon)
                doms, names = set(), set()
                for clk_canon in clk_canons:
                    clk_node = view.remap(clk_canon)
                    if is_reset(clk_node.rsplit('.', 1)[-1]):
                        continue
                    src = self._resolve_clock_source(clk_node)
                    doms.add(src)
                    names.add(src.rsplit('.', 1)[-1])
                if not doms:
                    continue
                rec = domains.get(node)
                if rec is None:
                    domains[node] = ClockDomain(domains=doms, names=names)
                else:
                    rec.domains |= doms
                    rec.names |= names
        if reset_key is not None:
            self._clock_domain_cache[reset_key] = domains
        return domains

    # ── whole-graph analyses (CDC / combinational loops) ───────────────

    # ── public API ───────────────────────────────────────────────────

    def get_top_paths(self):
        paths = []
        for t in self._root.topInstances:
            try:
                paths.append(t.hierarchicalPath)
            except Exception:
                continue
        return paths

    def _check_bit_range(self, sym, signal_name, bit_range):
        """Reject a bit-select outside the signal's width (shared by trace and
        fanin/fanout).  Declared indices run 0..width-1 for either endianness."""
        if bit_range is None:
            return
        try:
            width = int(sym.type.bitWidth)
        except Exception:
            width = None
        if width and bit_range[1] >= width:
            raise rtl_cli.CliError(
                agent_json.ERR_SIGNAL_NOT_FOUND,
                f"bit {bit_label(bit_range)} out of range for "
                f"'{signal_name}' ({sym.type}, {width} bits)", 1)

    def trace(self, signal_name, scope_path, bit_range=None):
        inst, sym = self._lookup(signal_name, scope_path)
        self._check_bit_range(sym, signal_name, bit_range)

        drivers = self._analyze_drivers(sym, inst)
        loads = self._analyze_loads(sym, inst.body, inst)
        if bit_range is not None:
            # Bit-select: narrow both the driver origin and the loads to the
            # readers/writers that actually touch those bits.  Driver/read bounds
            # are slang-internal, so match against the internal-coord query.
            sel = self._to_internal(sym, bit_range)
            drivers = [d for d in drivers if bits_overlap(d.bounds, sel)]
            loads = [ld for ld in loads if bits_overlap(ld.bounds, sel)]

        r = TraceResult(
            signal_name=signal_name, signal_type=str(sym.type),
            signal_kind=sym.kind.name, scope_path=scope_path,
            scope_module=inst.body.name,
            multi_driver=drivers_overlap(drivers),
            bit_range=bit_range,
        )
        if len(drivers) >= 1:
            r.driver = drivers[0]
        if len(drivers) > 1:
            r.extra_drivers = drivers[1:]
        r.loads = loads
        return r


# ── Path finding ─────────────────────────────────────────────────────
class PathFinder:
    """Find a path between two nodes by depth-first search.

    One forward DFS from the start node builds a *parent map* — for each node,
    the edge by which it was first reached — and the path to the end node is read
    back along that map and reversed (``_build``).  ``find`` and ``findComb``
    differ only by the edge predicate:

      * ``find``     — follow every edge.
      * ``findComb`` — additionally refuse to enter a register (sequential) node,
                       so the path is purely combinational, bounded by
                       flip-flops.  The boundary is the same one ``--comb``
                       fan-in/out uses (``SignalTracer._is_registered``).

    The DFS walks the same demand-driven dataflow graph ``flow()`` traverses (via
    ``SignalTracer._incident_edges`` in the forward / fan-out direction), so a
    path crosses port boundaries and hierarchical references exactly as the
    fan-in/out cones do.  The search stops as soon as the end node is reached:
    a node's parent is fixed on first visit (it never gets a second parent), so
    early exit yields the same path a full traversal would — and avoids walking
    the rest of the start node's fan-out cone once the target is found.
    """

    def __init__(self, tracer: "SignalTracer"):
        self._tracer = tracer

    def find(self, start: str, end: str):
        """Any path ``start`` -> ``end``: (nodes, edges), or ([], []) if none."""
        return self._search(start, end, comb=False)

    def findComb(self, start: str, end: str):
        """A purely combinational path ``start`` -> ``end`` (never via a
        register): (nodes, edges), or ([], []) if none exists."""
        return self._search(start, end, comb=True)

    def _search(self, start, end, *, comb):
        """Iterative DFS with an explicit stack.  ``parent[node] = (source,
        edge)`` records the edge that first reached ``node``."""
        tracer = self._tracer
        parent = {}                      # node -> (source_node, FlowEdge)
        visited = {start}
        stack = [(start, iter(tracer._incident_edges(start, "fanout")))]
        while stack and end not in visited:
            node, edge_it = stack[-1]
            pushed = False
            for edge in edge_it:
                nxt = edge.target
                # Combinational predicate: never enter a register node.
                if comb and tracer._is_registered(nxt):
                    continue
                if nxt in visited:
                    continue
                visited.add(nxt)
                parent[nxt] = (node, edge)
                stack.append(
                    (nxt, iter(tracer._incident_edges(nxt, "fanout"))))
                pushed = True
                break
            if not pushed:
                stack.pop()
        return self._build(parent, start, end)

    @staticmethod
    def _build(parent, start, end):
        """Reconstruct the ordered (nodes, edges) for ``start`` -> ``end`` from
        the parent map, or ([], []) when ``end`` was never reached."""
        if start == end:
            return [start], []
        if end not in parent:
            return [], []
        nodes, edges = [end], []
        cur = end
        while cur != start:
            src, edge = parent[cur]
            nodes.append(src)
            edges.append(edge)
            cur = src
        nodes.reverse()
        edges.reverse()
        return nodes, edges

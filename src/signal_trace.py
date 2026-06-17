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
    expr_reads_with_bounds,
    expr_symbols,
    expr_refs_symbol,
    find_signal,
    full_bounds,
    is_data_symbol,
    iter_instances,
    lsp_bounds,
    procedure_label,
    procedure_reads_symbol,
    resolve_scope,
    same_symbol,
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
GLYPH_WARN = "⚠"
GLYPH_DASH = "—"
GLYPH_HR = "─"
GLYPH_ARROW_L = "←"


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
    bits: str = ""          # "[3]" / "[7:4]" when the load reads a sub-range
    bounds: Optional[tuple] = None  # normalized (lo, hi) bit offsets read

    def to_dict(self):
        d = dict(kind=self.kind, description=self.description,
                 scope_path=self.scope_path)
        if self.instance_name:
            d['instance'] = self.instance_name
        if self.port_name:
            d['port'] = self.port_name
            d['direction'] = self.port_direction
        if self.bits:
            d['bits'] = self.bits
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
        loads = self.filtered_loads(load_filter)
        d['loads'] = [ld.to_dict() for ld in loads]
        d['load_count'] = len(loads)
        return d

    def _driver_line(self, d):
        C = Color
        bits = f" {C.yellow(self.signal_name + d.bits)}" if d.bits else ""
        where = ""
        if d.scope_path and d.scope_path != self.scope_path:
            where = f"  {C.dim('@ ' + d.scope_path)}"
        loc = f"  {C.dim(d.file + ':' + str(d.line))}" if d.file else ""
        return f"    {GLYPH_ARROW_L} {d.description}{bits}{where}{loc}"

    def pretty_print(self, load_filter=None, limit=0):
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

        # ── Loads (narrowed to the queried bits on a bit-select) ──
        loads = self.filtered_loads(load_filter)
        shown, total, truncated = agent_json.clip(loads, limit)
        hdr = f"\n  {C.green(GLYPH_LOADS + ' LOADS')} ({total})"
        if load_filter:
            hdr += f"  {C.dim('filter: ' + load_filter)}"
        print(hdr)

        if not shown:
            print(f"    {C.dim('(none found)')}")
        else:
            by_kind = {}
            for ld in shown:
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
                    bits = f" {C.yellow(self.signal_name + ld.bits)}" if ld.bits else ""
                    print(f"    \u2192 {ld.description}{bits}{loc}")
            if truncated:
                print(f"    {C.dim(agent_json.truncation_note(len(shown), total, 'loads'))}")

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
    # True when the edge is *registered* — its target is driven by an
    # edge-triggered/level-held sequential procedure (always_ff / always_latch
    # / an edge-sensitive `always`).  A clocked edge breaks combinational
    # feedback (loop detection ignores it) and marks a flip-flop boundary
    # (CDC keys launch/capture domains off it).  Continuous assigns and port
    # connections are never clocked.  Deliberately NOT part of ``key()`` so the
    # demand-driven / whole-graph parity (test_flow_lazy) is unaffected.
    clocked: bool = False
    # Bit-level dataflow (slang-netlist parity).  source_bits / target_bits are
    # the (lo, hi) sub-ranges this edge reads / drives, or None for the whole
    # signal — so whole-signal edges serialize exactly as before (additive).
    # bit_offset is the affine map of a copy-like edge (target_bit = source_bit
    # + bit_offset), which lets fanin/fanout answer "dout[5] ← which bit"; it is
    # None when the relationship is many-to-many (arithmetic / reduction).
    # Deliberately NOT part of key() so the parity invariant is preserved.
    source_bits: Optional[tuple] = None
    target_bits: Optional[tuple] = None
    bit_offset: Optional[int] = None

    def key(self):
        return (self.source, self.target, self.kind, self.file, self.line)

    @property
    def source_label(self):
        return self.source + (bit_label(self.source_bits) if self.source_bits else "")

    @property
    def target_label(self):
        return self.target + (bit_label(self.target_bits) if self.target_bits else "")

    def to_dict(self, depth=None):
        d = dict(source=self.source, target=self.target, kind=self.kind,
                 description=self.description,
                 source_type=self.source_type, target_type=self.target_type)
        if self.source_bits is not None:
            d['source_bits'] = bit_label(self.source_bits)
        if self.target_bits is not None:
            d['target_bits'] = bit_label(self.target_bits)
        if self.file:
            d['file'] = self.file
            d['line'] = self.line
        if self.clocked:
            d['clocked'] = True
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
    bit_range: Optional[tuple] = None   # (lo, hi) when a bit-select was queried

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
        d = dict(
            mode=self.mode, signal=self.signal_name, type=self.signal_type,
            kind=self.signal_kind, scope=self.scope_path,
            module=self.scope_module, start=self.start,
            max_depth=self.max_depth,
            nodes=self.nodes,
            edges=[edge.to_dict(depth) for edge, depth in self.edges],
            edge_count=len(self.edges),
        )
        if self.bit_range is not None:
            d['bit_select'] = bit_label(self.bit_range)
        return d

    def pretty_print(self, limit=0):
        C = Color
        title = "FANIN" if self.mode == "fanin" else "FANOUT"
        name = self.signal_name + (bit_label(self.bit_range) if self.bit_range else "")
        print(f"Signal: {C.bold(name)}  {C.dim(self.signal_type)}")
        print(f"Scope:  {C.cyan(self.scope_path)}  [{C.yellow(self.scope_module)}]")
        print(f"Mode:   {C.green(title)}  {C.dim('depth <= ' + str(self.max_depth))}")
        print("\u2500" * 60)
        if not self.edges:
            print(f"\n  {C.dim('(no dataflow edges found)')}\n")
            return
        shown, total, truncated = agent_json.clip(self.edges, limit)
        cur_depth = None
        for edge, depth in shown:
            if depth != cur_depth:
                cur_depth = depth
                print(f"\n  {C.dim('depth ' + str(depth))}")
            loc = f"  {C.dim(edge.file + ':' + str(edge.line))}" if edge.file else ""
            print(f"    {C.cyan(edge.source_label)} → "
                  f"{C.cyan(edge.target_label)}  "
                  f"{C.yellow(edge.kind)} {C.dim(edge.description)}{loc}")
        if truncated:
            print(f"\n  {C.dim(agent_json.truncation_note(len(shown), total, 'edges'))}")
        print()


# ── Graph-analysis result records (CDC / combinational loops) ────────
@dataclass
class ClockDomain:
    """The clock domain(s) a registered node belongs to."""
    domains: set = field(default_factory=set)   # resolved clock *source* paths
    names: set = field(default_factory=set)     # leaf names of those sources


@dataclass
class CDCCrossing:
    """One launch→capture clock-domain crossing found on the flow graph."""
    launch: str             # launch (source) register node path
    capture: str            # capture (destination) register node path
    from_domains: list      # launch clock-domain display names
    to_domains: list        # capture clock-domain display names
    file: str = ""
    line: int = 0

    @property
    def launch_name(self):
        return self.launch.rsplit('.', 1)[-1]

    @property
    def capture_name(self):
        return self.capture.rsplit('.', 1)[-1]


@dataclass
class CombLoop:
    """One combinational feedback loop (a cyclic path of non-clocked edges)."""
    nodes: list             # cycle node paths a->b->c (closes c->a)
    file: str = ""
    line: int = 0

    @property
    def display(self):
        leaves = [n.rsplit('.', 1)[-1] for n in self.nodes]
        return " → ".join(leaves + [leaves[0]]) if leaves else ""


# ── Tarjan strongly-connected components (iterative) ─────────────────
def _tarjan_scc(nodes, succ):
    """Iterative Tarjan SCC.  ``succ`` maps a node to its successor list.

    Returns the list of strongly-connected components (each a list of nodes).
    Iterative (explicit stack) so a long combinational chain — the 200-deep
    pipeline in the tests — cannot overflow Python's recursion limit.
    """
    index = {}
    low = {}
    on_stack = set()
    stack = []
    order = [0]
    out = []

    for root in nodes:
        if root in index:
            continue
        # work stack of (node, iterator-position)
        work = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = order[0]
                order[0] += 1
                stack.append(node)
                on_stack.add(node)
            succs = succ.get(node, ())
            if pi < len(succs):
                work[-1] = (node, pi + 1)
                nxt = succs[pi]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            else:
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    out.append(comp)
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
    return out


def _reconstruct_cycle(comp, succ):
    """A representative cycle within strongly-connected component ``comp``.

    Returns the node path ``[s, …]`` whose last node has an edge back to ``s``
    (the caller renders the closing ``→ s``).  Deterministic start for stable
    output; bounded by the SCC size.
    """
    compset = set(comp)
    s = min(comp)
    # DFS for a path from s back to s using only comp-internal edges.
    stack = [(s, [s])]
    visited = {s}
    while stack:
        node, path = stack.pop()
        for nxt in succ.get(node, ()):
            if nxt not in compset:
                continue
            if nxt == s:
                return path
            if nxt not in visited:
                visited.add(nxt)
                stack.append((nxt, path + [nxt]))
    return [s]


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
        # Clock-domain caches (see clock_domain_map / cdc_crossings).
        self._clock_tmpl_cache = {}    # body key -> {driver path -> [clk path]}
        self._clock_domain_cache = None  # reg node path -> ClockDomain
        # Demand-driven flow-graph caches (see _build_flow_edges / flow).
        self._inst_proc_cache = {}     # inst path -> [FlowEdge] (proc/assign)
        self._inst_port_cache = {}     # inst path -> [FlowEdge] (own ports)
        self._child_cache = {}         # inst path -> [child InstanceSymbol]
        self._ancestor_cache = {}      # inst path -> [ancestor InstanceSymbol]
        self._owner_cache = {}         # node path -> owning InstanceSymbol
        self._flow_index_cache = {}    # inst path -> (by_source, by_target)
        self._proc_index_cache = {}    # inst path -> proc-only (by_src, by_tgt)
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
        """(display_label, bounds) for a load reading `symbol` over `bounds`."""
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

    def _norm_bits(self, sym, bounds):
        """Drop a bit range that spans the whole signal so whole-signal edges
        stay byte-for-byte identical to before (additive output); a proper
        sub-range is kept as a normalized (lo, hi) tuple."""
        if bounds is None:
            return None
        full = self._signal_full_bounds(sym)
        b = (int(bounds[0]), int(bounds[1]))
        return None if full is not None and b == full else b

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
        return FlowEdge(
            source=self._sym_path(source),
            target=self._sym_path(target),
            source_type=self._sym_type(source),
            target_type=self._sym_type(target),
            kind=kind,
            description=description,
            file=f,
            line=ln,
            source_bits=self._norm_bits(source, source_bits),
            target_bits=self._norm_bits(target, target_bits),
            bit_offset=bit_offset,
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

                for src, dst, sbits, dbits, off in pairs:
                    templates.append((
                        self._sym_path(src), self._sym_path(dst),
                        self._sym_type(src), self._sym_type(dst),
                        kind, desc, f, ln, clocked,
                        self._norm_bits(src, sbits),
                        self._norm_bits(dst, dbits), off))
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

    def _assignment_bit_pairs(self, node):
        """Bit-aware (src, dst, src_bits, dst_bits, offset) for one assignment.

        Targets are ``expr_symbols(left)`` and the read symbols are exactly
        those of ``expr_symbols(right)`` — identical connectivity to the old
        symbol-only walk (parity) — with bit ranges and a copy offset attached.
        A single target fed by a single value access (``a`` / ``a[hi:lo]`` /
        truncation / extend / constant shift) is an exact positional copy; an
        arithmetic / multi-source / multi-target RHS is reported per read over
        its read range with no single-valued bit map.
        """
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        tgts = expr_symbols(left)
        if not tgts:
            return []
        single_tgt_bits = lsp_bounds(left)[1] if len(tgts) == 1 else None
        rsym, rbits = lsp_bounds(right)
        if (len(tgts) == 1 and rsym is not None and is_data_symbol(rsym)
                and len(expr_symbols(right)) == 1):
            sb, db, off = self._copy_bits(rsym, rbits, tgts[0], single_tgt_bits)
            return [(rsym, tgts[0], sb, db, off)]
        out = []
        for src, sb in expr_reads_with_bounds(right):
            for dst in tgts:
                db = single_tgt_bits if len(tgts) == 1 else None
                out.append((src, dst, sb, db, None))
        return out

    def _continuous_assign_pairs(self, proc):
        """Bit-aware (src, dst, src_bits, dst_bits, offset) for one continuous
        assign.  Symbol pairs are the same reads x drivers set as before
        (parity); bit ranges come from slang's per-assign read/driver bounds,
        and an offset is derived for the unambiguous single-read single-driver
        positional copy by inspecting the assignment expression."""
        drv = [(d.symbol, self._driver_bounds(d)) for d in (proc.drivers or [])
               if is_data_symbol(d.symbol)]
        rds = [(r.symbol, self._read_bounds(r)) for r in (proc.readSet or [])
               if is_data_symbol(r.symbol)]
        copy = None
        if len(drv) == 1 and len(rds) == 1:
            asgn = getattr(proc.analyzedSymbol, "assignment", None)
            rsym, rbits = (lsp_bounds(getattr(asgn, "right", None))
                           if asgn is not None else (None, None))
            if rsym is not None and same_symbol(rsym, rds[0][0]):
                copy = self._copy_bits(
                    rds[0][0], rbits, drv[0][0],
                    lsp_bounds(getattr(asgn, "left", None))[1])
        out = []
        for dsym, dbnd in drv:
            for rsym, rbnd in rds:
                if copy is not None:
                    out.append((rsym, dsym) + copy)
                else:
                    out.append((rsym, dsym, rbnd, dbnd, None))
        return out

    def _proc_statement_deps(self, proc, drivers):
        """Per-statement LHS<-RHS bit-aware deps + conservative control reads
        for one procedural block.  Returns unique
        (src_sym, dst_sym, src_bits, dst_bits, bit_offset) tuples.

        Data is precise (an assignment's RHS feeds only its own LHS).  Control
        conditions (if / case / loop) are attributed to every driver in the
        block — a small, never-under-reporting over-approximation that avoids
        a per-branch scoping walk.  A symbol pair driven by more than one
        statement/condition loses its single-valued bit map (falls back to the
        whole signal), which never under-reports.
        """
        assigns = []      # [AssignmentExpression]
        control = []      # control-condition reads

        def collect(node):
            if type(node).__name__ == "AssignmentExpression":
                assigns.append(node)
            else:
                control.extend(self._statement_control_reads(node))

        try:
            proc.analyzedSymbol.body.visit(f=collect)
        except Exception:
            assigns = []

        if not assigns:
            # Degenerate walk; fall back to the coarse reads x drivers so we
            # never silently under-report.
            reads = [r.symbol for r in (proc.readSet or [])
                     if is_data_symbol(r.symbol)]
            return [(s, d, None, None, None) for s in reads for d in drivers]

        control = [s for s in control if is_data_symbol(s)]
        pairs = {}        # (src_path, dst_path) -> 5-tuple

        def add(src, dst, sb=None, db=None, off=None):
            if not (is_data_symbol(src) and is_data_symbol(dst)):
                return
            k = (self._sym_path(src), self._sym_path(dst))
            if k in pairs:
                s, d, *_ = pairs[k]
                pairs[k] = (s, d, None, None, None)
            else:
                pairs[k] = (src, dst, sb, db, off)

        for node in assigns:
            for src, dst, sb, db, off in self._assignment_bit_pairs(node):
                add(src, dst, sb, db, off)
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
                     source_bits=sb, target_bits=tb, bit_offset=off)
            for (src, dst, st, dt, kind, desc, f, ln, clk, sb, tb, off)
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
        arithmetic).  Returns _NO_OVERLAP when a concrete range misses the edge.
        """
        if rng is None:
            return None
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

    def flow(self, signal_name, scope_path, mode, max_depth=4, bit_range=None):
        inst, sym = self._lookup(signal_name, scope_path)

        start = self._sym_path(sym)

        # Demand-driven, bit-aware BFS: the frontier carries (node, range) where
        # range is the bits still of interest (None = whole signal).  An edge is
        # followed only when its bits overlap range, and range is mapped across
        # the edge to the next node — so `-s dout[5]` converges to the exact
        # driving bit.  A whole-signal query keeps range None throughout and
        # reproduces the symbol-level traversal edge-for-edge.
        traversed = []
        seen_edges = set()
        seen_nodes = {(start, bit_range)}
        frontier = [(start, bit_range)]
        depth = 0
        max_depth = max(0, int(max_depth))
        while frontier and depth < max_depth:
            depth += 1
            next_frontier = []
            for node, rng in frontier:
                for edge in self._incident_edges(node, mode):
                    nxt_rng = self._map_range(edge, rng, mode)
                    if nxt_rng is self._NO_OVERLAP:
                        continue
                    ekey = edge.key()
                    if ekey not in seen_edges:
                        seen_edges.add(ekey)
                        traversed.append((edge, depth))
                    nxt = edge.source if mode == "fanin" else edge.target
                    if (nxt, nxt_rng) not in seen_nodes:
                        seen_nodes.add((nxt, nxt_rng))
                        next_frontier.append((nxt, nxt_rng))
            frontier = next_frontier

        return FlowResult(
            mode=mode, signal_name=signal_name, signal_type=str(sym.type),
            signal_kind=sym.kind.name, scope_path=scope_path,
            scope_module=inst.body.name, start=start,
            edges=traversed, max_depth=max_depth, bit_range=bit_range,
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

    def clock_domain_map(self, is_reset):
        """``{registered_node_path -> ClockDomain}`` over the whole design.

        A registered node is any signal driven by a sequential procedure; its
        domain is the *source net* its clock resolves to (``is_reset`` drops
        async-reset events from the clock set).  Built once and cached.
        """
        if self._clock_domain_cache is not None:
            return self._clock_domain_cache
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
        self._clock_domain_cache = domains
        return domains

    # ── whole-graph analyses (CDC / combinational loops) ───────────────

    def cdc_crossings(self, is_reset):
        """Clock-domain crossings on the flow graph.

        For each capture register, walk *combinationally* backward from its
        data inputs (clocked edges, minus reset-named sources) and collect the
        launch registers reached, stopping at each register boundary.  A launch
        whose domain is disjoint from the capture's domain is an unsynchronized
        crossing.  Cross-hierarchy by construction: the whole-graph build folds
        in port-connection and hierarchical-reference edges alike.
        """
        edges = self._build_flow_edges()
        reg_domain = self.clock_domain_map(is_reset)
        if not reg_domain:
            return []

        clocked_fanin = {}   # capture node -> [clocked FlowEdge feeding it]
        comb_fanin = {}      # node -> [combinational predecessor node]
        for e in edges:
            if e.clocked:
                clocked_fanin.setdefault(e.target, []).append(e)
            else:
                comb_fanin.setdefault(e.target, []).append(e.source)

        out, seen = [], set()
        for cap in sorted(reg_domain):
            cap_dom = reg_domain[cap]
            cap_edges = clocked_fanin.get(cap, [])
            if not cap_edges:
                continue
            cap_edge = cap_edges[0]
            seeds = [e.source for e in cap_edges
                     if not is_reset(e.source.rsplit('.', 1)[-1])]
            if not seeds:
                continue

            visited, launches = set(), set()
            stack = list(seeds)
            while stack:
                n = stack.pop()
                if n in visited:
                    continue
                visited.add(n)
                if n != cap and n in reg_domain:
                    launches.add(n)          # launch flop; stop at the boundary
                    continue
                for p in comb_fanin.get(n, ()):
                    if p not in visited:
                        stack.append(p)

            for launch in sorted(launches):
                l_dom = reg_domain[launch]
                if cap_dom.domains & l_dom.domains:
                    continue                 # same physical clock => safe
                key = (launch, cap)
                if key in seen:
                    continue
                seen.add(key)
                out.append(CDCCrossing(
                    launch=launch, capture=cap,
                    from_domains=sorted(l_dom.names),
                    to_domains=sorted(cap_dom.names),
                    file=cap_edge.file, line=cap_edge.line))
        return out

    def combinational_loops(self):
        """Combinational feedback loops: Tarjan SCCs over the non-clocked edges.

        A clocked (registered) edge breaks feedback, so excluding it leaves only
        combinational connectivity; any strongly-connected component with a real
        cycle is a combinational loop.  Multi-node SCCs are always reported; a
        single-node SCC is reported only for a structural self-edge
        (``assign a = a;`` / a self port connection), not a procedural one — the
        graph's conservative control-condition modeling can otherwise synthesize
        a spurious ``a → a``.
        """
        edges = [e for e in self._build_flow_edges() if not e.clocked]
        succ = {}
        nodes = set()
        first_edge = {}      # (src, tgt) -> FlowEdge (for a location)
        self_edges = {}      # node -> structural self FlowEdge
        for e in edges:
            nodes.add(e.source)
            nodes.add(e.target)
            lst = succ.setdefault(e.source, [])
            if e.target not in lst:
                lst.append(e.target)
            first_edge.setdefault((e.source, e.target), e)
            if e.source == e.target and e.kind in ("continuous_assign",
                                                    "port_connection"):
                self_edges.setdefault(e.source, e)

        loops = []
        for comp in _tarjan_scc(nodes, succ):
            if len(comp) >= 2:
                path = _reconstruct_cycle(comp, succ)
                if len(path) < 2:
                    continue
                # location: the first edge along the reconstructed cycle.
                ekey = (path[0], path[1])
                e = first_edge.get(ekey)
                loops.append(CombLoop(nodes=path,
                                      file=e.file if e else "",
                                      line=e.line if e else 0))
            else:
                n = comp[0]
                e = self_edges.get(n)
                if e is not None:
                    loops.append(CombLoop(nodes=[n], file=e.file, line=e.line))
        loops.sort(key=lambda lp: (lp.file, lp.line, lp.nodes))
        return loops

    # ── public API ───────────────────────────────────────────────────

    def get_top_paths(self):
        paths = []
        for t in self._root.topInstances:
            try:
                paths.append(t.hierarchicalPath)
            except Exception:
                continue
        return paths

    def trace(self, signal_name, scope_path, bit_range=None):
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
        loads = self._analyze_loads(sym, inst.body, inst)
        if bit_range is not None:
            # Bit-select: narrow both the driver origin and the loads to the
            # readers/writers that actually touch those bits.
            drivers = [d for d in drivers if bits_overlap(d.bounds, bit_range)]
            loads = [ld for ld in loads if bits_overlap(ld.bounds, bit_range)]

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

# ── Shared input/dispatch helpers ────────────────────────────────────
def _prepare(args, env, *, need_signal=False):
    """Common setup for trace/fanin/fanout: resolve inputs, build
    compilation, auto-detect scope, normalize dotted -s forms, and split off
    a trailing bit-select.  Returns (tracer, scope, signal, bit_range);
    raises CliError on any input/compile/scope failure."""
    prepared = rtl_cli.prepare_compilation_checked(args, env, human_error_rc=1)
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
    g.add_argument('--filter', default=None, metavar='GLOB',
                   help='Shell glob on instance names to narrow loads')


def run_trace(args, env):
    tracer, scope, signal, bit_range = _prepare(args, env, need_signal=True)
    r = tracer.trace(signal, scope, bit_range)
    lim = agent_json.resolve_limit(args.limit)
    if env is not None:
        rd = r.to_dict(args.filter)
        load_total = int(rd.get('load_count', 0))
        if 'loads' in rd:
            shown, _t, tr = agent_json.clip(rd['loads'], lim)
            rd['loads'] = shown
        else:
            tr = False
        data = {'mode': 'signal', 'scope': scope, 'results': [rd]}
        summary = {
            'mode': 'signal', 'results': 1,
            'drivers': 1 if rd.get('driver') else 0,
            'loads':   load_total,
            'truncated': tr,
            'limit': lim,
        }
        return emit(env.ok(data, summary))
    r.pretty_print(args.filter, limit=lim)
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
    r = tracer.flow(signal, scope, mode, args.depth, bit_range=bit_range)
    if getattr(args, 'summary', False):
        return _emit_flow_summary(env, r, mode, scope, signal)
    lim = agent_json.resolve_limit(args.limit)
    if env is not None:
        rd = r.to_dict()
        # Cap the edges, then keep exactly the nodes those surviving edges
        # reference (plus the depth-0 start).  Clipping `nodes` and `edges`
        # independently could emit an edge whose endpoint was dropped from
        # `nodes`, leaving the JSON graph internally inconsistent.
        edges_shown, edges_total, e_tr = agent_json.clip(rd['edges'], lim)
        kept = {rd['start']}
        for e in edges_shown:
            kept.add(e['source'])
            kept.add(e['target'])
        nodes_shown = [n for n in rd['nodes'] if n in kept]
        nodes_total = len(rd['nodes'])
        data = {
            'mode': mode, 'scope': scope, 'signal': signal,
            'start': rd['start'], 'nodes': nodes_shown, 'edges': edges_shown,
            'max_depth': rd['max_depth'],
        }
        if 'bit_select' in rd:
            data['bit_select'] = rd['bit_select']
        summary = {
            'mode': mode, 'results': 1,
            'nodes': nodes_total, 'edges': edges_total,
            'max_depth': rd['max_depth'],
            'truncated': e_tr or len(nodes_shown) < nodes_total, 'limit': lim,
        }
        return emit(env.ok(data, summary))
    r.pretty_print(limit=lim)
    return 0

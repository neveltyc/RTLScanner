#!/usr/bin/env python3
"""
rtl_lint — Verilog/SystemVerilog Static Linter

A thin, fast linter built on pyslang's elaboration + analysis engine.
Surfaces real semantic problems — width mismatches, missing case
defaults, unused/undriven signals and ports, multi-driven nets — that
regex linters miss, using the same filelist/compilation infrastructure
as the rest of the RTLScanner family.

This module is invoked via the unified `rtlscanner lint` subcommand.

Install dependency:
    pip install pyslang
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import pyslang
    import pyslang.ast as ast
    import pyslang.analysis as analysis
except ImportError:
    print("Error: pyslang is required.  pip install pyslang", file=sys.stderr)
    sys.exit(1)

from rtl_common import (
    Color,
    safe_str,
)

import agent_json
import rtl_cli
from rtl_config import flow_config
from rtl_scope import ScopeAnalyzer
from signal_trace import SignalTracer


# ── Data Structures ──────────────────────────────────────────────────
_SEVERITY_NAME = {
    pyslang.DiagnosticSeverity.Fatal: "error",
    pyslang.DiagnosticSeverity.Error: "error",
    pyslang.DiagnosticSeverity.Warning: "warning",
    pyslang.DiagnosticSeverity.Note: "note",
    pyslang.DiagnosticSeverity.Ignored: "ignored",
}


@dataclass
class LintFinding:
    """One normalized lint finding."""
    file: str
    line: int
    col: int
    severity: str       # "error" | "warning" | "note"
    rule: str           # warning option name (e.g. "width-trunc") or code name
    message: str
    check: str          # one of CATEGORIES (semantic|unused|port|cdc|comb-loop)
    module: str = ""    # design unit (module/interface/...) the finding sits in

    def to_dict(self):
        d = dict(file=self.file, line=self.line, col=self.col,
                 severity=self.severity, rule=self.rule,
                 message=self.message, check=self.check)
        if self.module:
            d['module'] = self.module
        return d

    @property
    def location(self):
        loc = f"{self.file}:{self.line}"
        if self.col:
            loc += f":{self.col}"
        return loc


# ── Core: Lint Runner ────────────────────────────────────────────────
class LintRunner:
    """Runs pyslang's semantic + analysis checks and normalizes results."""

    def __init__(self, compilation, categories=None, root=None,
                 unroll=True, max_unroll=2048):
        self._comp = compilation
        self._sm = compilation.sourceManager
        # The closed set of check categories to run (subset of CATEGORIES).
        self._categories = set(categories) if categories else set(CATEGORIES)
        # CDC and combinational-loop checks share one flow graph (built lazily).
        # Both run on the *pruned* graph — constant if/case dead branches dropped
        # and constant-bound loops unrolled — so they match the precision of the
        # fanin/fanout/trace commands (a constant dead-branch edge is not a real
        # CDC crossing nor a real combinational loop).  Defaults match the flow
        # commands' defaults; `run()` overrides them from the [flow] config.
        self._unroll = bool(unroll)
        self._max_unroll = max(0, int(max_unroll))
        self._tracer = None
        self._eng = pyslang.DiagnosticEngine(self._sm)
        # Honor inline `pragma diagnostic push/ignore/pop waivers written
        # directly in the RTL source.  This is standard SystemVerilog and
        # lets engineers waive a finding right where it lives.
        try:
            self._eng.setMappingsFromPragmas()
        except Exception:
            pass
        self._root = (Path(root) if root else Path.cwd()).resolve()

    # ── helpers ───────────────────────────────────────────────────────

    def _rel(self, name: str) -> str:
        """Present a readable path: relative to the resolved input root.

        The root is the same ``[inputs].root`` the xref command uses, so a file
        is reported with one consistent path across subcommands (rather than
        lint keying off the process CWD while xref keys off the config root).
        """
        if not name:
            return name
        try:
            p = Path(name).resolve()
            rel = os.path.relpath(p, self._root)
            # Mirror xref's _format_file exactly so the two commands print a
            # file with byte-identical paths (same base *and* same rendering).
            if rel == ".":
                return "."
            if rel.startswith(os.pardir + os.sep) or rel == os.pardir:
                # Don't produce noisy ../../.. chains for far-away files.
                return p.as_posix()
            return "./" + Path(rel).as_posix()
        except Exception:
            return name

    # Syntax-node kinds that name a design unit a finding can be attributed to.
    _UNIT_DECL_KINDS = frozenset({
        "ModuleDeclarationSyntax", "InterfaceDeclarationSyntax",
        "ProgramDeclarationSyntax", "PackageDeclarationSyntax",
    })

    def _module_index(self):
        """Lazily map ``realpath(file) -> [(start_line, end_line, unit_name)]``.

        Built by visiting every design-unit declaration (module / interface /
        program / package) in each syntax tree, so a finding can be attributed
        to the actual unit it sits in instead of assuming file-basename ==
        module.  ``visit`` (rather than ``root.members``) is used because a
        compilation unit containing a single design unit parses with that
        declaration AS the root — its ``.members`` are then the unit's *inner*
        items, so a members-only scan would index nothing for the common
        one-module-per-file case.
        """
        idx = getattr(self, "_modidx", None)
        if idx is not None:
            return idx
        idx = {}

        def collect(node):
            if type(node).__name__ not in self._UNIT_DECL_KINDS:
                return
            hdr = getattr(node, "header", None)
            nm = getattr(hdr, "name", None) if hdr is not None else None
            name = safe_str(getattr(nm, "valueText", ""), "") if nm else ""
            sr = getattr(node, "sourceRange", None)
            if not name or sr is None:
                return
            try:
                start = int(self._sm.getLineNumber(sr.start))
                end = int(self._sm.getLineNumber(sr.end))
                key = os.path.realpath(
                    safe_str(self._sm.getFileName(sr.start), ""))
            except Exception:
                return
            if key:
                idx.setdefault(key, []).append((start, end, name))

        try:
            for tree in self._comp.getSyntaxTrees():
                root = getattr(tree, "root", None)
                if root is not None:
                    root.visit(f=collect)
        except Exception:
            pass
        self._modidx = idx
        return idx

    def _module_for(self, raw_filename: str, line: int) -> str:
        """The innermost design unit whose source range contains *line*."""
        if not raw_filename or not line:
            return ""
        try:
            key = os.path.realpath(raw_filename)
        except Exception:
            key = raw_filename
        best, best_span = "", None
        for start, end, name in self._module_index().get(key, ()):
            if start <= line <= end:
                span = end - start
                if best_span is None or span < best_span:
                    best, best_span = name, span
        return best

    def _finding(self, diag, from_analysis=False):
        """Convert a pyslang Diagnostic into a LintFinding, or None if ignored.

        The check category is derived from the rule name (see
        ``category_for_rule``).  ``from_analysis`` keeps an analysis-pass
        diagnostic the engine would otherwise ignore (those checks are opt-in)."""
        loc = diag.location
        sev_enum = self._eng.getSeverity(diag.code, loc)
        severity = _SEVERITY_NAME.get(sev_enum, "warning")
        if severity == "ignored":
            if not from_analysis:
                return None
            severity = "warning"

        rule = self._eng.getOptionName(diag.code)
        if not rule:
            # No -W option name (e.g. hard errors): use the bare code name,
            # stripping the "DiagCode(...)" wrapper from str().
            rule = safe_str(diag.code, "unknown")
            if rule.startswith("DiagCode(") and rule.endswith(")"):
                rule = rule[len("DiagCode("):-1]
        try:
            message = self._eng.formatMessage(diag)
        except Exception:
            message = safe_str(diag.code, "")
        try:
            raw_fn = safe_str(self._sm.getFileName(loc), "")
            fn = self._rel(raw_fn)
            ln = int(self._sm.getLineNumber(loc))
            col = int(self._sm.getColumnNumber(loc))
        except Exception:
            raw_fn, fn, ln, col = "", "", 0, 0
        module = self._module_for(raw_fn, ln)

        return LintFinding(file=fn, line=ln, col=col, severity=severity,
                           rule=rule, message=message,
                           check=category_for_rule(rule), module=module)

    def _shared_tracer(self):
        """A single ``SignalTracer`` (and its flow graph / analysis manager)
        shared by the CDC and combinational-loop checks, built on first use.

        It carries the same constant-condition pruning / loop unrolling the
        fanin/fanout/trace commands use, so a constant dead-branch edge never
        manufactures a phantom CDC crossing or combinational loop."""
        if self._tracer is None:
            self._tracer = SignalTracer(self._comp, unroll=self._unroll,
                                        max_unroll=self._max_unroll)
        return self._tracer

    # ── public API ────────────────────────────────────────────────────

    def run(self) -> list[LintFinding]:
        cats = self._categories
        findings = []

        # 1. Native slang diagnostics — width truncation, missing case defaults,
        # undeclared identifiers, multiple-driver conflicts, etc.  These are the
        # `semantic` category (the full diagnostic stream, so frontend /
        # preprocessor issues like a missing include are surfaced too).
        # ``getAllDiagnostics`` also forces elaboration, which the analysis pass
        # below requires, so always call it even when semantic isn't collected.
        diags = self._comp.getAllDiagnostics()
        if "semantic" in cats:
            for d in diags:
                f = self._finding(d)
                if f is not None:
                    # Every native slang diagnostic IS the semantic category by
                    # source — including slang's own port-/width- option names
                    # (e.g. `port-width-trunc`).  The `port` category is reserved
                    # for the ScopeAnalyzer connectivity checks below, so don't
                    # let a rule-name prefix reclassify a native diagnostic.
                    f.check = "semantic"
                    findings.append(f)

        # 2. Analysis-manager pass.  It produces both `unused` findings
        # (unused-*, undriven-port) and `semantic` correctness findings
        # (inferred-latch, unassigned-variable), so run it when either category
        # is selected and keep each finding by its own derived category.
        if cats & {"semantic", "unused"}:
            try:
                opts = analysis.AnalysisOptions()
                opts.flags = analysis.AnalysisFlags.CheckUnused
                mgr = analysis.AnalysisManager(opts)
                mgr.analyze(self._comp)
                for d in mgr.getDiagnostics():
                    f = self._finding(d, from_analysis=True)
                    if f is not None and f.check in cats:
                        findings.append(f)
            except Exception as e:
                print(f"Warning: analysis pass failed: {e}", file=sys.stderr)

        # 3. CDC — graph-based, cross-hierarchy clock-domain crossings.
        if "cdc" in cats:
            try:
                cdc = CDCAnalyzer(self._comp, rel=self._rel,
                                  tracer=self._shared_tracer())
                findings.extend(cdc.findings())
            except Exception as e:
                print(f"Warning: CDC analysis failed: {e}", file=sys.stderr)

        # 4. Combinational loops — cycle detection on the non-sequential edges
        # of the same flow graph.
        if "comb-loop" in cats:
            try:
                cl = CombLoopAnalyzer(self._comp, rel=self._rel,
                                      tracer=self._shared_tracer())
                findings.extend(cl.findings())
            except Exception as e:
                print(f"Warning: combinational-loop analysis failed: {e}",
                      file=sys.stderr)

        # 5. Port connectivity — unconnected ports, port/connection width
        # mismatches on child instances.
        if "port" in cats:
            try:
                analyzer = ScopeAnalyzer(self._comp)
                for issue in analyzer.connection_issues():
                    rule = {
                        "unconnected": "port-unconnected",
                        "width_mismatch": "port-width-mismatch",
                    }.get(issue.kind, "port-connect")
                    findings.append(LintFinding(
                        file=self._rel(issue.file),
                        line=issue.line,
                        col=0,
                        severity=issue.severity,
                        rule=rule,
                        message=issue.message,
                        check="port",
                    ))
            except Exception as e:
                print(f"Warning: port connection analysis failed: {e}", file=sys.stderr)

        # Attribute every finding to its enclosing design unit.  The graph-based
        # analyzers (CDC, comb-loop) and the port-connect check build findings
        # directly without a module, so backfill it from the file:line here —
        # otherwise a `--waive module:foo` (and the module half of a bare-glob
        # waiver) could never reach them.  ``_module_for`` realpath-normalizes its
        # argument, so a relative finding path resolves to the same unit index
        # key the raw source paths do.
        for f in findings:
            if not f.module:
                f.module = self._module_for(f.file, f.line)

        findings.sort(key=lambda f: (f.file, f.line, f.col, f.rule))
        return findings


# ── CDC Analyzer ─────────────────────────────────────────────────────
# Built-in reset-name heuristic, deliberately reset-*rooted*.  A bare ``*_n``
# would match any active-low data signal (``data_n``, ``sel_n``, ``q_n``,
# ``we_n`` …) and drop it from the timing/clock-domain events, silently masking
# genuine CDC crossings — so active-low resets must carry an rst/reset/arst/por/
# clr root.  Names starting with ``rst``/``reset``/``arst`` already cover
# ``rst_n``, ``resetn``, ``arst_n`` …; CDC runs with zero configuration off this
# list.  Each entry is matched case-insensitively (see ``_looks_like_reset``).  The set
# is kept minimal: a glob already covered by a broader one here is omitted (e.g.
# ``*reset*`` subsumes ``reset*`` / ``*reset_n`` / ``nreset``; ``*rst_n`` subsumes
# ``*_rst_n`` / ``*_arst_n``; ``*_rst`` subsumes ``n_rst``), so this list and the
# longer one it replaced recognize exactly the same names.
_DEFAULT_RESET_GLOBS = ("rst*", "*_rst", "*_rstn", "*rst_n",
                        "*reset*",
                        "arst*", "*_arstn",
                        "clr*", "*_clr", "*clr_n",
                        "por_n", "*_por_n",
                        "nrst")


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


def _tarjan_scc(nodes, succ):
    """Iterative Tarjan SCC.  ``succ`` maps a node to its successor list.

    Returns the list of strongly-connected components (each a list of nodes).
    Iterative (explicit stack) so a long combinational chain — the 200-deep
    pipeline in the tests — cannot overflow Python's recursion limit.
    """
    index, low, on_stack, stack, order, out = {}, {}, set(), [], [0], []
    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]      # work stack of (node, iterator-position)
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
    output; bounded by the SCC size."""
    compset = set(comp)
    s = min(comp)
    stack = [(s, [s])]
    visited = {s}
    while stack:
        node, path = stack.pop()
        for nxt in succ.get(node, ()):
            if nxt not in compset:
                continue
            # Close the cycle only after a real hop.  A self-edge on the start
            # node (s -> s) can't close a *multi-node* cycle; returning the
            # length-1 path [s] here would make the caller drop the entire SCC
            # and miss the real loop (regression: a multi-node SCC whose min
            # node also has a structural self-assign).
            if nxt == s and len(path) >= 2:
                return path
            if nxt not in visited:
                visited.add(nxt)
                stack.append((nxt, path + [nxt]))
    return [s]


class CDCAnalyzer:
    """Detect clock-domain crossings on the dataflow flow graph.

    A *launch* register in one clock domain that feeds, through combinational
    logic only, the data input of a *capture* register in a different domain is
    a CDC crossing that typically needs an explicit synchronizer.  The launch /
    capture relationship is found on the shared flow graph
    (:meth:`signal_trace.SignalTracer.flow_edges`), so it is **cross-hierarchy**
    (a launch and capture in different modules wired through ports are still
    related); each flop's clock is resolved to its **source net** (via the
    tracer's :meth:`clock_domain_map` primitive), so two flops on the same
    physical clock are one domain even when the local clock ports are named
    differently or live in different instances (and conversely one net reaching
    ports named ``clk``/``clock`` is one domain, not two).

    Reset-looking signals (the built-in name heuristic) are dropped from the
    clock set so a single-clock design with an asynchronous reset, and the
    async-reset term of an ``if (!rst_n)`` capture, are not mistaken for a
    crossing.  The detection lives here; the tracer only supplies the engine.
    """

    def __init__(self, compilation, reset_globs=None, rel=None, tracer=None):
        self._comp = compilation
        self._reset_globs = list(reset_globs or []) + list(_DEFAULT_RESET_GLOBS)
        self._rel = rel or (lambda x: x)
        self._tracer = tracer

    def _looks_like_reset(self, name: str) -> bool:
        n = (name or "").lower()
        return any(fnmatch.fnmatch(n, g.lower()) for g in self._reset_globs)

    def crossings(self, is_reset=None, reset_key=None) -> list:
        """The CDC crossings on the flow graph as :class:`CDCCrossing` records.

        For each capture register, walk *combinationally* backward from its data
        inputs (clocked edges, minus reset-named sources) and collect the launch
        registers reached, stopping at each register boundary.  A launch whose
        domain is disjoint from the capture's domain is an unsynchronized
        crossing.  ``is_reset`` defaults to the built-in heuristic; ``reset_key``
        keys the tracer's cached clock-domain map across reset configs.  A caller
        passing a custom ``is_reset`` without a key recomputes the map every call
        (so a reused tracer never serves a stale map for a different predicate)."""
        if is_reset is None:
            is_reset = self._looks_like_reset
            if reset_key is None:
                reset_key = tuple(self._reset_globs)
        tracer = self._tracer or SignalTracer(self._comp)
        edges = tracer.flow_edges()
        reg_domain = tracer.clock_domain_map(is_reset, reset_key)
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

    def findings(self) -> list:
        out = []
        for c in self.crossings():
            frm = "/".join(c.from_domains) or "?"
            to = "/".join(c.to_domains) or "?"
            msg = (f"signal '{c.launch_name}' crosses clock domains: launched "
                   f"in '{frm}' domain, captured by '{c.capture_name}' in "
                   f"'{to}' domain (launch {c.launch}, capture {c.capture})")
            out.append(LintFinding(
                file=self._rel(c.file) if c.file else "",
                line=c.line, col=0, severity="warning",
                rule="cdc-crossing", message=msg, check="cdc"))
        return out


class CombLoopAnalyzer:
    """Detect combinational feedback loops on the dataflow flow graph.

    Runs cycle detection (Tarjan SCC) over the graph's **non-sequential**
    (non-clocked) edges — a registered edge breaks feedback, so what is left is
    pure combinational connectivity, and any strongly-connected component with a
    real cycle is a combinational loop.  Cross-hierarchy by construction (the
    loop may close through port connections).  The detection lives here; the
    tracer only supplies the shared flow graph.
    """

    def __init__(self, compilation, rel=None, tracer=None):
        self._comp = compilation
        self._rel = rel or (lambda x: x)
        self._tracer = tracer

    def loops(self) -> list:
        """The combinational loops on the flow graph as :class:`CombLoop`
        records.  Multi-node SCCs are always reported; a single-node SCC is
        reported only for a structural self-edge (``assign a = a;`` / a self port
        connection), not a procedural one — the graph's conservative
        control-condition modeling can otherwise synthesize a spurious
        ``a → a``."""
        tracer = self._tracer or SignalTracer(self._comp)
        edges = [e for e in tracer.flow_edges() if not e.clocked]
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
                e = first_edge.get((path[0], path[1]))  # location: first edge
                loops.append(CombLoop(nodes=path,
                                      file=e.file if e else "",
                                      line=e.line if e else 0))
            else:
                e = self_edges.get(comp[0])
                if e is not None:
                    loops.append(CombLoop(nodes=[comp[0]], file=e.file,
                                          line=e.line))
        loops.sort(key=lambda lp: (lp.file, lp.line, lp.nodes))
        return loops

    def findings(self) -> list:
        out = []
        for lp in self.loops():
            chain = " -> ".join(lp.nodes + lp.nodes[:1]) if lp.nodes else ""
            out.append(LintFinding(
                file=self._rel(lp.file) if lp.file else "",
                line=lp.line, col=0, severity="warning",
                rule="comb-loop", message=f"combinational loop: {chain}",
                check="comb-loop"))
        return out


# `lint` is a fixed, opinionated scanner: a closed set of five check categories,
# selected (or narrowed) by the single `--rules` flag — there is no rule-glob /
# family / meta / waiver / severity-policy sub-language.
CATEGORIES = ("semantic", "unused", "port", "cdc", "comb-loop")


def category_for_rule(rule: str) -> str:
    """Map a finding's rule name to its check category (one of CATEGORIES).

    `unused-*` / `undriven-port` are *unused*; `port-*` is *port*; the two custom
    graph rules map to themselves; everything else — width truncation, missing
    case defaults, undeclared identifiers, inferred latches, never-assigned
    variables, multiple-driver conflicts — is a *semantic* diagnostic."""
    if rule.startswith("unused-") or rule == "undriven-port":
        return "unused"
    if rule.startswith("port-"):
        return "port"
    if rule == "cdc-crossing":
        return "cdc"
    if rule == "comb-loop":
        return "comb-loop"
    return "semantic"


def resolve_categories(specs):
    """Resolve `--rules` tokens to the set of check categories to run.

    No tokens → all five.  `all` → all five.  Otherwise a whitelist of category
    names.  Any token outside the closed set raises a ``CliError`` naming the
    valid categories (exit 2), rather than silently selecting nothing."""
    if not specs:
        return set(CATEGORIES)
    cats = set()
    for s in specs:
        if s == "all":
            cats.update(CATEGORIES)
        elif s in CATEGORIES:
            cats.add(s)
        else:
            raise rtl_cli.CliError(
                agent_json.ERR_BAD_CONFIG,
                f"--rules: '{s}' is not a check category. Valid categories: "
                f"{', '.join(CATEGORIES)} (or 'all').", 2)
    return cats


# ── Display ──────────────────────────────────────────────────────────
_SEV_COLOR = {
    "error":   Color.red,
    "warning": Color.yellow,
    "note":    Color.cyan,
}


def _counts(findings):
    by_sev, by_rule, by_check = {}, {}, {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        by_check[f.check] = by_check.get(f.check, 0) + 1
    return by_sev, by_rule, by_check


def print_summary(by_sev, by_rule):
    print(f"\n{'─' * 50}\n  {Color.bold('Lint Summary')}\n{'─' * 50}")
    n_err = by_sev.get("error", 0)
    n_warn = by_sev.get("warning", 0)
    n_note = by_sev.get("note", 0)
    print(f"  {Color.red('errors')}:   {n_err}")
    print(f"  {Color.yellow('warnings')}: {n_warn}")
    if n_note:
        print(f"  {Color.cyan('notes')}:    {n_note}")
    if by_rule:
        print(f"\n  {Color.cyan('By rule:')}")
        for rule, cnt in sorted(by_rule.items(), key=lambda x: -x[1]):
            print(f"    {rule:28s} {cnt:4d}  {Color.dim('█' * min(cnt, 30))}")
    print(f"{'─' * 50}")


def print_findings(findings):
    if not findings:
        print(Color.green("✓ No lint findings."))
        return
    cur_file = None
    for f in findings:
        if f.file != cur_file:
            cur_file = f.file
            print(f"\n{Color.bold(cur_file or '(unknown)')}")
        sev_fn = _SEV_COLOR.get(f.severity, Color.dim)
        loc = f"{f.line}:{f.col}" if f.col else str(f.line)
        print(f"  {Color.dim(loc):>10s}  {sev_fn(f.severity):8s}  "
              f"{f.message}  {Color.dim('[' + f.rule + ']')}")


# ── CLI ──────────────────────────────────────────────────────────────
def add_arguments(p: argparse.ArgumentParser) -> None:
    rs = p.add_argument_group("rule selection")
    rs.add_argument("--rules", action=agent_json.CommaListAction, default=[],
                    metavar="CATEGORY",
                    help="Check categories to run (whitelist): "
                         "semantic, unused, port, cdc, comb-loop — or 'all'. "
                         "Comma-list or repeat. Default: all five.")


@dataclass
class LintResult(agent_json.CommandResult):
    """Typed result of ``lint``: the normalized findings plus their counts.

    ``_counts`` runs once in ``__post_init__``; the JSON ``summary`` and the
    human ``print_summary`` table both read the same ``by_severity`` /
    ``by_rule`` / ``by_check`` maps (instead of each re-deriving them), and the
    error-driven exit code is computed in one place.
    """
    findings: list
    config_path: object = None
    files_linted: int = 0

    def __post_init__(self):
        self.has_error = any(f.severity == "error" for f in self.findings)
        self.by_severity, self.by_rule, self.by_check = _counts(self.findings)
        self.exit_code = 1 if self.has_error else 0

    def to_json(self, limit):
        shown, total, truncated = agent_json.clip(self.findings, limit)
        data = {
            'findings':    [f.to_dict() for f in shown],
            'config_path': str(self.config_path) if self.config_path else None,
        }
        summary = {
            'total':        total,
            'shown':        len(shown),
            'truncated':    truncated,
            'limit':        limit,
            'by_severity':  self.by_severity,
            'by_rule':      self.by_rule,
            'by_check':     self.by_check,
            'files_linted': self.files_linted,
            'has_error':    self.has_error,
        }
        return data, summary

    def render_human(self, limit):
        shown, total, truncated = agent_json.clip(self.findings, limit)
        print_findings(shown)
        if truncated:
            print(Color.dim(agent_json.truncation_note(len(shown), total, "findings")))
        if self.findings:
            print_summary(self.by_severity, self.by_rule)
        return self.exit_code


def run(args, env):
    prepared = rtl_cli.prepare_compilation(args)
    filelist = prepared.filelist

    categories = resolve_categories(list(args.rules))   # may raise CliError

    # The graph-based checks (CDC, comb-loop) share the dataflow flow graph with
    # the fanin/fanout/trace commands, so honor the same [flow] precision config
    # (constant-condition pruning / loop unrolling), defaulting to on.
    fcfg = flow_config(prepared.config)
    unroll = fcfg["unroll"] if fcfg["unroll"] is not None else True
    max_unroll = fcfg["max_unroll"] if fcfg["max_unroll"] is not None else 2048

    runner = LintRunner(
        prepared.comp,
        categories=categories,
        root=prepared.resolved_inputs.root,
        unroll=unroll,
        max_unroll=max_unroll,
    )

    result = LintResult(
        findings=runner.run(),
        config_path=prepared.config_path,
        files_linted=len(filelist.sources),
    )
    return agent_json.render(env, result, agent_json.resolve_limit(args.limit))

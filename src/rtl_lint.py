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
import difflib
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
from agent_json import emit
from rtl_config import lint_config
from rtl_scope import ScopeAnalyzer


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
    check: str          # "semantic" | "unused" | "shadow" | ...
    waived_reason: str = ""  # set when filtered out by a waiver / disabled rule

    def to_dict(self):
        d = dict(file=self.file, line=self.line, col=self.col,
                 severity=self.severity, rule=self.rule,
                 message=self.message, check=self.check)
        if self.waived_reason:
            d['waived_reason'] = self.waived_reason
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

    def __init__(self, compilation, check_unused=True, check_shadow=False,
                 weverything=False, check_cdc=False, cdc_reset_globs=None,
                 check_port_connect=False):
        self._comp = compilation
        self._sm = compilation.sourceManager
        self._check_unused = check_unused
        self._check_shadow = check_shadow
        self._check_cdc = check_cdc
        self._check_port_connect = check_port_connect
        self._cdc_reset_globs = cdc_reset_globs or []
        self._eng = pyslang.DiagnosticEngine(self._sm)
        if weverything:
            try:
                self._eng.setWarningOptions(["everything"])
            except Exception:
                pass
        # Honor inline `pragma diagnostic push/ignore/pop waivers written
        # directly in the RTL source.  This is standard SystemVerilog and
        # lets engineers waive a finding right where it lives.
        try:
            self._eng.setMappingsFromPragmas()
        except Exception:
            pass
        self._root = Path.cwd().resolve()

    # ── helpers ───────────────────────────────────────────────────────

    def _rel(self, name: str) -> str:
        """Present a readable path: relative to cwd when sensible."""
        if not name:
            return name
        try:
            p = Path(name).resolve()
            rel = os.path.relpath(p, self._root)
            # Don't produce noisy ../../.. chains for far-away files.
            return rel if not rel.startswith(os.pardir + os.sep) else p.as_posix()
        except Exception:
            return name

    def _finding(self, diag, check: str):
        """Convert a pyslang Diagnostic into a LintFinding, or None if ignored."""
        loc = diag.location
        sev_enum = self._eng.getSeverity(diag.code, loc)
        severity = _SEVERITY_NAME.get(sev_enum, "warning")
        # Analysis (unused/shadow) checks are opt-in: honor them even if the
        # engine's default mapping would ignore the underlying code.
        if severity == "ignored":
            if check == "semantic":
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
            fn = self._rel(safe_str(self._sm.getFileName(loc)))
            ln = int(self._sm.getLineNumber(loc))
            col = int(self._sm.getColumnNumber(loc))
        except Exception:
            fn, ln, col = "", 0, 0

        return LintFinding(file=fn, line=ln, col=col, severity=severity,
                           rule=rule, message=message, check=check)

    # ── public API ────────────────────────────────────────────────────

    def run(self) -> list[LintFinding]:
        findings = []

        # 1. Native slang diagnostics.  Keep these under the existing
        # ``semantic`` family, but use the full diagnostic stream instead of
        # semantic-only diagnostics so frontend/preprocessor issues such as
        # missing includes are surfaced by the same rule family.
        for d in self._comp.getAllDiagnostics():
            f = self._finding(d, "semantic")
            if f is not None:
                findings.append(f)

        # 2. Analysis-manager checks (unused / shadow) when requested
        flags = analysis.AnalysisFlags(0)
        if self._check_unused:
            flags |= analysis.AnalysisFlags.CheckUnused
        if self._check_shadow:
            flags |= analysis.AnalysisFlags.CheckShadow
        if flags.value:
            try:
                opts = analysis.AnalysisOptions()
                opts.flags = flags
                mgr = analysis.AnalysisManager(opts)
                mgr.analyze(self._comp)
                # A single analysis pass can surface both unused- and shadow-
                # prefixed diagnostics, so derive each finding's check family
                # from its rule name rather than from which flag ran the pass.
                default_check = "unused" if self._check_unused else "shadow"
                for d in mgr.getDiagnostics():
                    f = self._finding(d, default_check)
                    if f is not None:
                        if f.rule.startswith("shadow-"):
                            f.check = "shadow"
                        elif f.rule.startswith("unused-"):
                            f.check = "unused"
                        findings.append(f)
            except Exception as e:
                print(f"Warning: analysis pass failed: {e}", file=sys.stderr)

        # 3. CDC analysis (opt-in) — flop-to-flop clock domain crossings
        if self._check_cdc:
            try:
                cdc = CDCAnalyzer(self._comp, reset_globs=self._cdc_reset_globs,
                                  rel=self._rel)
                findings.extend(cdc.findings())
            except Exception as e:
                print(f"Warning: CDC analysis failed: {e}", file=sys.stderr)

        # 4. Port connection checks (opt-in) -- direct replacement for the
        # old ports --check surface.
        if self._check_port_connect:
            try:
                analyzer = ScopeAnalyzer(self._comp)
                for issue in analyzer.connection_issues():
                    rule = {
                        "unconnected": "port-unconnected",
                        "width_mismatch": "port-width-mismatch",
                    }.get(issue.kind, "port-connect")
                    findings.append(LintFinding(
                        file=issue.file,
                        line=issue.line,
                        col=0,
                        severity=issue.severity,
                        rule=rule,
                        message=issue.message,
                        check="port-connect",
                    ))
            except Exception as e:
                print(f"Warning: port connection analysis failed: {e}", file=sys.stderr)

        findings.sort(key=lambda f: (f.file, f.line, f.col, f.rule))
        return findings


# ── CDC Analyzer ─────────────────────────────────────────────────────
_DEFAULT_RESET_GLOBS = ("rst*", "*_rst", "*_rstn", "*_n",
                        "reset*", "*reset*", "clr*", "*clr_n")


class CDCAnalyzer:
    """Detect flop-to-flop clock domain crossings.

    A signal driven inside ``always_ff @(posedge clkA …)`` and then read
    inside ``always_ff @(posedge clkB …)`` (clkB ≠ clkA) constitutes a
    CDC crossing that typically needs an explicit synchronizer.  We
    report one ``cdc-crossing`` finding per (signal, reader-domain) pair,
    pointing at the procedural block that does the unsafe read.

    Signals that look like resets (matched against ``reset_globs``) are
    ignored in the timing event list so a single-clock design with an
    asynchronous reset doesn't get flagged.
    """

    def __init__(self, compilation, reset_globs=None, rel=None):
        self._comp = compilation
        self._sm = compilation.sourceManager
        self._reset_globs = list(reset_globs or []) + list(_DEFAULT_RESET_GLOBS)
        self._rel = rel or (lambda x: x)
        mgr = analysis.AnalysisManager(analysis.AnalysisOptions())
        mgr.analyze(compilation)
        self._mgr = mgr

    # ── helpers ───────────────────────────────────────────────────────

    def _looks_like_reset(self, name: str) -> bool:
        n = (name or "").lower()
        return any(fnmatch.fnmatch(n, g.lower()) for g in self._reset_globs)

    def _clock_and_timing_syms(self, proc):
        """Return (primary_clock_name, {all_timing_signal_names}) or (None, set())."""
        if not proc.timingControls:
            return None, set()
        tc = proc.timingControls[0].timing
        events = tc.events if hasattr(tc, 'events') else [tc]
        timing_syms = set()
        non_reset = []
        for ev in events:
            e = getattr(ev, 'expr', None)
            if e is None or not hasattr(e, 'symbol'):
                continue
            n = safe_str(e.symbol.name, "")
            if not n:
                continue
            timing_syms.add(n)
            if not self._looks_like_reset(n):
                non_reset.append(n)
        # Heuristic: if exactly one non-reset event signal, that's the
        # clock; otherwise prefer the first non-reset, else fall back to
        # whatever timing signal we saw first.
        if non_reset:
            return non_reset[0], timing_syms
        if timing_syms:
            return next(iter(timing_syms)), timing_syms
        return None, timing_syms

    def _sym_key(self, sym):
        """Stable identity key for a pyslang symbol — uses hierarchicalPath
        because Python id() of pybind11 wrappers is recycled across GC.
        """
        try:
            hp = safe_str(sym.hierarchicalPath, "")
            if hp:
                return hp
        except Exception:
            pass
        return safe_str(getattr(sym, 'name', ''), '')

    def _walk_reads(self, proc, exclude_names):
        """Walk the procedure body and collect (sym_key, name, source_loc) for
        every NamedValueExpression that isn't a clock/reset.
        """
        out = []
        sym_key = self._sym_key
        def visit(node):
            if type(node).__name__ != 'NamedValueExpression':
                return
            try:
                sym = node.symbol
                name = sym.name
            except Exception:
                return
            if name in exclude_names:
                return
            try:
                loc = node.sourceRange.start
            except Exception:
                loc = None
            out.append((sym_key(sym), name, loc))
        try:
            proc.analyzedSymbol.body.visit(f=visit)
        except Exception:
            pass
        return out

    def _loc(self, loc):
        try:
            return self._rel(safe_str(self._sm.getFileName(loc))), \
                   int(self._sm.getLineNumber(loc)), \
                   int(self._sm.getColumnNumber(loc))
        except Exception:
            return "", 0, 0

    def _proc_loc(self, proc):
        """Location of the procedure block itself (for the timing-control line)."""
        try:
            sym = proc.analyzedSymbol
            return self._loc(sym.location)
        except Exception:
            return "", 0, 0

    # ── main analysis ─────────────────────────────────────────────────

    def findings(self) -> list:
        # Per scope, build write/read maps keyed by symbol identity (we
        # use the Python id of the symbol so two same-named signals in
        # different modules don't collide).
        writers = {}   # sym_id -> {'name': str, 'clocks': set, 'locs': [(file,line,col)]}
        readers = {}   # sym_id -> list of {'clock':..., 'loc':..., 'proc_loc':...}

        insts = []
        def _ci(s):
            insts.append(s)
        try:
            self._comp.getRoot().visit(lookup_table={ast.SymbolKind.Instance: _ci})
        except Exception:
            pass

        for inst in insts:
            try:
                body = inst.body
                sc = self._mgr.getAnalyzedScope(body)
            except Exception:
                continue
            if sc is None:
                continue
            for p in sc.procedures:
                try:
                    pk = p.analyzedSymbol.procedureKind
                    if 'AlwaysFF' not in safe_str(pk, ""):
                        continue
                except Exception:
                    continue
                clk, timing_syms = self._clock_and_timing_syms(p)
                if not clk:
                    continue

                # Writes: pyslang gives us ValueDrivers
                driven_keys = set()
                driven_names = set()
                for d in (p.drivers or []):
                    try:
                        if d.flags & analysis.DriverFlags.InputPort:
                            continue
                    except Exception:
                        pass
                    try:
                        key = self._sym_key(d.symbol)
                        driven_keys.add(key)
                        driven_names.add(d.symbol.name)
                        rec = writers.setdefault(key, {
                            'name': d.symbol.name, 'clocks': set(),
                            'locs': []})
                        rec['clocks'].add(clk)
                        rec['locs'].append(self._proc_loc(p))
                    except Exception:
                        continue

                # Reads: walk the body, skip the signals used purely for
                # timing (clock + reset) and skip self-reads of the
                # signals this same proc drives (a flip-flop reading its
                # own output is fine, same domain).
                exclude = set(timing_syms) | driven_names
                for key, sym_name, loc in self._walk_reads(p, exclude):
                    readers.setdefault(key, []).append({
                        'clock': clk,
                        'loc': self._loc(loc) if loc else self._proc_loc(p),
                        'name': sym_name,
                    })

        # Build findings — one per (signal, reader-clock) crossing
        out = []
        for key, wrec in writers.items():
            w_clocks = wrec['clocks']
            for r in readers.get(key, ()):
                if r['clock'] in w_clocks:
                    continue
                rfile, rline, rcol = r['loc']
                # First writer location for context
                if wrec['locs']:
                    wfile, wline, _ = wrec['locs'][0]
                    where = f" (written at {wfile}:{wline})" if wfile else ""
                else:
                    where = ""
                msg = (f"signal '{wrec['name']}' crosses clock domains: "
                       f"written in '{'/'.join(sorted(w_clocks))}' domain, "
                       f"read in '{r['clock']}' domain{where}")
                out.append(LintFinding(
                    file=rfile, line=rline, col=rcol,
                    severity="warning", rule="cdc-crossing",
                    message=msg, check="cdc",
                ))
        return out




# ── Rule selection model ─────────────────────────────────────────────
# A `--rules SPEC[,...]` spec can contain:
#   * family alias:  semantic | unused | shadow | cdc | port-connect
#   * warning opt:   everything      (passes -Weverything to slang)
#   * meta:          default (= semantic+unused) | all | none
#   * rule name:     width-trunc | unused-port | ...
#   * glob:          width-* | unused-*

FAMILIES = {"semantic", "unused", "shadow", "cdc", "port-connect"}
DEFAULT_FAMILIES = ["semantic", "unused"]
WARNING_OPTIONS = {"everything"}
META_KEYWORDS = {"default", "all", "none", "bugs"}

# The `bugs` preset: a curated, high-precision view of rules that flag real
# functional defects (each verified to actually fire), as opposed to style
# noise.  `apply()` additionally keeps every error/fatal finding under this
# preset, since hard compile errors carry open-ended code names that can't be
# enumerated here.  cdc-crossing is deliberately excluded (heuristic, high
# false-positive rate); compose it back with `--rules bugs,cdc`.
BUGS_RULES = [
    "inferred-latch",        # unintended level-sensitive latch
    "unassigned-variable",   # variable read but never driven -> X
    "undriven-port",         # output port never driven -> X
    "port-width-mismatch",   # child instance port width mismatch
    "port-width-trunc",      # truncation at a port connection
    "width-trunc",           # implicit truncation in an assignment
]

# Map rule-name prefix → check family, so a bare rule like "unused-port"
# implies we need to RUN the unused analysis.
_RULE_PREFIX_FAMILY = {
    "unused-": "unused",
    "shadow-": "shadow",
    "cdc-":    "cdc",
    "port-":   "port-connect",
}

# Analysis-pass rules that lack a family prefix: selecting one by exact name
# must still RUN the pass that produces it (the CheckUnused analysis pass).
_RULE_RUN_FAMILY = {
    "inferred-latch":      "unused",
    "unassigned-variable": "unused",
    "undriven-port":       "unused",
}

# Rule names invented by our own checks (not slang warning options).  Used to
# validate --rules/--skip tokens: slang's findFromOptionName returns empty for
# these, so they must be recognized explicitly.
CUSTOM_RULES = frozenset({
    "cdc-crossing", "port-unconnected", "port-width-mismatch", "port-connect",
})


def _expand_meta(specs):
    """Resolve 'default'/'all'/'none'/'bugs' meta keywords into a spec list."""
    out = []
    for s in specs:
        if s == "default":
            out.extend(DEFAULT_FAMILIES)
        elif s == "all":
            out.extend(DEFAULT_FAMILIES)
            out.extend(["shadow", "cdc", "port-connect", "everything"])
        elif s == "bugs":
            out.extend(BUGS_RULES)
        elif s == "none":
            return []
        else:
            out.append(s)
    # de-dup, preserve order
    seen, deduped = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def resolve_rules(specs):
    """Split rule specs into runtime families, keep filters, warning opts.

    - run_families: check families to actually RUN (lint engine input).
                    Always includes the family any rule glob's prefix implies,
                    so explicit `--rules unused-port` still runs the unused pass.
    - keep_families: families whose findings the user wants to KEEP. Empty when
                    the user only listed rule names (display is rule-glob-only).
    - rule_globs: explicit rule names/globs to keep.
    - warning_options: slang warning option groups such as ``everything``.
    - noop: True iff specs == ['none'] (everything is suppressed).
    """
    specs = list(specs) if specs else ["default"]
    if "none" in specs:
        return set(), set(), [], set(), True
    expanded = _expand_meta(specs)
    keep_families, globs = set(), []
    warning_options = set()
    run_families = {"semantic"}  # semantic always runs (pyslang elaboration)
    for s in expanded:
        if s in FAMILIES:
            keep_families.add(s)
            run_families.add(s)
        elif s in WARNING_OPTIONS:
            warning_options.add(s)
            keep_families.add("semantic")
            run_families.add("semantic")
        else:
            globs.append(s)
            exact_fam = _RULE_RUN_FAMILY.get(s)
            if exact_fam:
                run_families.add(exact_fam)
            else:
                for pref, fam in _RULE_PREFIX_FAMILY.items():
                    if s.startswith(pref):
                        run_families.add(fam)
                        break
    return run_families, keep_families, globs, warning_options, False


def rule_matches(finding, keep_families, globs):
    """A finding is kept iff:
       (a) a family was explicitly listed and matches this finding's check, OR
       (b) a rule glob matches this finding's rule name.
    """
    if keep_families and finding.check in keep_families:
        return True
    return any(fnmatch.fnmatch(finding.rule, g) for g in globs)


def skip_matches(finding, skip_globs):
    return any(fnmatch.fnmatch(finding.rule, g) for g in skip_globs)


def validate_rule_tokens(tokens, eng, *, flag):
    """Notes for --rules/--skip tokens that match no known rule, family, or
    meta — the typo case (e.g. ``bugz``) that would otherwise select zero
    findings silently.  A real rule that simply has no findings this run is
    NOT flagged.

    A literal (wildcard-free) token is recognized iff it is a family, meta,
    warning option, one of our CUSTOM_RULES, or a real slang warning option
    (validated via ``DiagnosticEngine.findFromOptionName``).  Glob tokens are
    not second-guessed — they legitimately span open name sets.
    """
    vocab = set(FAMILIES) | set(META_KEYWORDS) | set(WARNING_OPTIONS) | set(CUSTOM_RULES)
    suggest = sorted(vocab | set(BUGS_RULES))
    notes = []
    for tok in tokens:
        if not tok or any(c in tok for c in "*?["):
            continue
        if tok in vocab:
            continue
        try:
            if eng is not None and eng.findFromOptionName(tok):
                continue
        except Exception:
            pass
        close = difflib.get_close_matches(tok, suggest, n=1, cutoff=0.5)
        hint = f" — did you mean '{close[0]}'?" if close else ""
        notes.append(f"{flag}: '{tok}' is not a known rule, family, or meta"
                     f"{hint} (it selected no findings)")
    return notes


# ── Waive (globs matched against each finding's source-file basename) ──
def _finding_file_stem(finding):
    """The finding's source-file basename without its extension.

    Waivers match against this *file name*, not the elaborated module or scope.
    Real RTL projects overwhelmingly follow one-module-per-file with matching
    names, so a file-basename glob is usually equivalent to a module glob in
    practice — but a multi-module file (or a file whose name differs from its
    module) is matched by file, not by module. (A true module/scope/instance
    waiver is planned; see CHANGELOG.)
    """
    if not finding.file:
        return ""
    return Path(finding.file).stem


def waive_matches(finding, waive_globs):
    if not waive_globs:
        return False
    stem = _finding_file_stem(finding)
    return any(fnmatch.fnmatch(stem, g) for g in waive_globs)


# ── Severity application ─────────────────────────────────────────────
SEVERITY_RANK = {"error": 3, "warning": 2, "note": 1}


def _normalize_severity(val):
    v = str(val).strip().lower()
    if v in ("error", "err", "e"):
        return "error"
    if v in ("warning", "warn", "w"):
        return "warning"
    if v in ("note", "info", "n"):
        return "note"
    return None


def apply(findings, *, rules_specs, skip_globs, waive_globs, strict,
          min_severity, lint_severity_map):
    """Filter findings through rules → skip → waive → severity overrides.

    Returns (kept, waived) where waived items carry waived_reason.
    """
    _run_families, keep_families, rule_globs, _warning_options, noop = resolve_rules(rules_specs)
    # The `bugs` preset additionally keeps every hard error/fatal finding,
    # whose code names are open-ended and can't be enumerated as rule globs.
    bugs_mode = "bugs" in (rules_specs or ["default"])
    kept, waived = [], []
    sev_map = {}
    for k, v in (lint_severity_map or {}).items():
        sev = _normalize_severity(v)
        if sev is not None:
            sev_map[k] = sev

    for f in findings:
        if noop:
            f.waived_reason = "rules=none"
            waived.append(f)
            continue
        matched = (rule_matches(f, keep_families, rule_globs)
                   or (bugs_mode and f.severity == "error"))
        if not matched:
            f.waived_reason = "rule not selected"
            waived.append(f)
            continue
        if skip_matches(f, skip_globs):
            f.waived_reason = "skipped"
            waived.append(f)
            continue
        if waive_matches(f, waive_globs):
            f.waived_reason = "waived (file glob)"
            waived.append(f)
            continue
        # Per-rule severity override (from [lint.severity])
        for pat, sev in sev_map.items():
            if fnmatch.fnmatch(f.rule, pat):
                f.severity = sev
        # --strict: warning → error
        if strict and f.severity == "warning":
            f.severity = "error"
        # Display floor: below --min-severity is suppressed, but recorded as
        # waived (never silently dropped) so summary.waived stays honest.
        if (min_severity
                and SEVERITY_RANK.get(f.severity, 0)
                < SEVERITY_RANK.get(min_severity, 0)):
            f.waived_reason = "below-min-severity"
            waived.append(f)
            continue
        kept.append(f)
    return kept, waived


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


def print_summary(findings, waived=0):
    by_sev, by_rule, _ = _counts(findings)
    print(f"\n{'─' * 50}\n  {Color.bold('Lint Summary')}\n{'─' * 50}")
    n_err = by_sev.get("error", 0)
    n_warn = by_sev.get("warning", 0)
    n_note = by_sev.get("note", 0)
    print(f"  {Color.red('errors')}:   {n_err}")
    print(f"  {Color.yellow('warnings')}: {n_warn}")
    if n_note:
        print(f"  {Color.cyan('notes')}:    {n_note}")
    if waived:
        print(f"  {Color.dim('waived')}:   {waived}")
    if by_rule:
        print(f"\n  {Color.cyan('By rule:')}")
        for rule, cnt in sorted(by_rule.items(), key=lambda x: -x[1]):
            print(f"    {rule:28s} {cnt:4d}  {Color.dim('█' * min(cnt, 30))}")
    print(f"{'─' * 50}")


def print_waived(waived):
    if not waived:
        return
    print(f"\n{Color.dim('Waived findings (' + str(len(waived)) + '):')}")
    for f in waived:
        loc = f"{f.line}:{f.col}" if f.col else str(f.line)
        print(Color.dim(f"  {f.file}:{loc}  {f.rule}  — {f.waived_reason}"))


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
                    metavar="SPEC",
                    help="Rule white list. SPEC = rule name | family "
                         "(semantic/unused/shadow/cdc/port-connect) | warning option "
                         "(everything) | glob | "
                         "default/all/none/bugs. 'bugs' = curated real-bug rules "
                         "+ all compile errors. Comma-list or repeat. "
                         "Default: 'default' (semantic + unused).")
    rs.add_argument("--skip", action=agent_json.CommaListAction, default=[],
                    metavar="RULE",
                    help="Subtract rule(s) from the white list (glob ok).")

    sc = p.add_argument_group("scope")
    sc.add_argument("--waive", action=agent_json.CommaListAction, default=[],
                    metavar="GLOB",
                    help="Suppress findings whose source-file basename matches "
                         "these globs (e.g. 'dbg_*,third_party_*').")

    sv = p.add_argument_group("severity & exit code")
    sv.add_argument("--strict", action="store_true",
                    help="Warnings count as errors AND any finding fails exit.")
    sv.add_argument("--min-severity", choices=("error", "warning", "note"),
                    default=None,
                    help="Hide findings below this severity (display floor).")

    out = p.add_argument_group("waived output")
    out.add_argument("--waived", action="store_true",
                     help="Also list findings suppressed by skip/waive/rules.")


def run(args, env):
    prepared = rtl_cli.prepare_compilation(args)
    lint_cfg = lint_config(prepared.config)
    filelist = prepared.filelist

    # CLI > config (field-level)
    rules_specs = list(args.rules) or list(lint_cfg.get("rules") or [])
    skip_globs  = list(args.skip)  or list(lint_cfg.get("skip")  or [])
    waive_globs = list(args.waive) or list(lint_cfg.get("waive") or [])
    cdc_reset_globs = list((lint_cfg.get("cdc") or {}).get("reset") or [])
    severity_map = lint_cfg.get("severity") or {}

    run_families, _, _, warning_options, _ = resolve_rules(rules_specs)

    runner = LintRunner(
        prepared.comp,
        check_unused=("unused" in run_families),
        check_shadow=("shadow" in run_families),
        weverything=("everything" in warning_options),
        check_cdc=("cdc" in run_families),
        cdc_reset_globs=cdc_reset_globs,
        check_port_connect=("port-connect" in run_families),
    )

    # Flag typo'd rule/skip tokens that would otherwise select 0 findings
    # silently (e.g. `--rules bugz`); a real rule with no findings is not flagged.
    for _flag, _toks in (("--rules", rules_specs), ("--skip", skip_globs)):
        for _note in validate_rule_tokens(_toks, getattr(runner, "_eng", None),
                                          flag=_flag):
            if env is not None:
                env.add_diagnostic("note", message=_note)
            else:
                print(f"note: {_note}", file=sys.stderr)

    findings = runner.run()

    findings, waived = apply(
        findings,
        rules_specs=rules_specs,
        skip_globs=skip_globs,
        waive_globs=waive_globs,
        strict=args.strict,
        min_severity=args.min_severity,
        lint_severity_map=severity_map,
    )

    has_error = any(f.severity == "error" for f in findings)
    strict_fail = args.strict and bool(findings)

    if env is not None:
        by_sev, by_rule, by_check = _counts(findings)
        lim = agent_json.resolve_limit(args.limit)
        shown, total, truncated = agent_json.clip(findings, lim)
        data = {
            'findings':    [f.to_dict() for f in shown],
            'waived':      [f.to_dict() for f in waived],
            'config_path': str(prepared.config_path) if prepared.config_path else None,
        }
        summary = {
            'total':        total,
            'shown':        len(shown),
            'truncated':    truncated,
            'limit':        lim,
            'by_severity':  by_sev,
            'by_rule':      by_rule,
            'by_check':     by_check,
            'waived':       len(waived),
            'files_linted': len(filelist.sources),
            'has_error':    has_error,
        }
        rc = emit(env.ok(data, summary))
        return 1 if (has_error or strict_fail) else rc

    lim = agent_json.resolve_limit(args.limit)
    shown, total, truncated = agent_json.clip(findings, lim)
    print_findings(shown)
    if truncated:
        print(Color.dim(agent_json.truncation_note(len(shown), total, "findings")))
    if args.waived:
        print_waived(waived)
    if findings:
        print_summary(findings, waived=len(waived))
    return 1 if (has_error or strict_fail) else 0

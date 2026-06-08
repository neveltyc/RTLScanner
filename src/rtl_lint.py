#!/usr/bin/env python3
"""
rtl_lint — Verilog/SystemVerilog Static Linter

A thin, fast linter built on pyslang's elaboration + analysis engine.
Surfaces real semantic problems — width mismatches, missing case
defaults, unused/undriven signals and ports, multi-driven nets — that
regex linters miss, using the same filelist/compilation infrastructure
as the rest of the RTLScanner family.

Primary usage (with filelist):
    rtl_lint --filelist rtl.f
    rtl_lint --filelist rtl.f --werror          # fail CI on any warning
    rtl_lint --filelist rtl.f --disable case-default --disable width-expand
    rtl_lint --filelist rtl.f --error width-trunc   # promote a rule to error
    rtl_lint --filelist rtl.f --summary --json

With directory scan:
    rtl_lint -d ./rtl
    rtl_lint -d ./rtl --rule 'unused-*'         # only show matching rules

Exit codes:
    0  clean (no error-level findings)
    1  one or more error-level findings (or warnings with --werror)
    2  usage / source error

Install dependency:
    pip install pyslang
"""

from __future__ import annotations

import argparse
import fnmatch
import json
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


# ── Data Structures ──────────────────────────────────────────────────
SEVERITY_ORDER = {"error": 3, "warning": 2, "note": 1, "ignored": 0}

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
    check: str          # "semantic" | "unused" | "shadow"
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
                 weverything=False, check_cdc=False, cdc_reset_globs=None):
        self._comp = compilation
        self._sm = compilation.sourceManager
        self._check_unused = check_unused
        self._check_shadow = check_shadow
        self._check_cdc = check_cdc
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

        # 1. Semantic diagnostics from elaboration (width, case, ports, …)
        for d in self._comp.getSemanticDiagnostics():
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
                check = "unused" if self._check_unused else "shadow"
                for d in mgr.getDiagnostics():
                    f = self._finding(d, check)
                    if f is not None:
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


# ── Configuration ────────────────────────────────────────────────────
CONFIG_NAMES = (".rtllint.toml", ".rtllint.json")
_SUPPRESS_WORDS = {"off", "ignore", "ignored", "none", "suppress",
                   "disable", "disabled", "waive", "waived"}


def normalize_severity(val):
    """Map a config severity string to a canonical name, or None if unknown.

    Suppression words ("off"/"ignore"/…) normalize to "ignored".
    """
    v = str(val).strip().lower()
    if v in _SUPPRESS_WORDS:
        return "ignored"
    if v in ("error", "err", "e"):
        return "error"
    if v in ("warning", "warn", "w"):
        return "warning"
    if v in ("note", "info", "n"):
        return "note"
    return None


def discover_config(start="."):
    """Walk up from *start* looking for a project lint config file."""
    d = Path(start).resolve()
    for parent in (d, *d.parents):
        for name in CONFIG_NAMES:
            cand = parent / name
            if cand.is_file():
                return cand
    return None


def load_config(path):
    """Load a TOML (.toml) or JSON (.json) lint config into a dict."""
    p = Path(path)
    text = p.read_text(errors="ignore")
    if p.suffix == ".json":
        return json.loads(text)
    # TOML — stdlib tomllib (3.11+) or the tomli backport, if available.
    for mod in ("tomllib", "tomli"):
        try:
            return __import__(mod).loads(text)
        except ModuleNotFoundError:
            continue
    raise SystemExit(
        "Error: reading a TOML config needs Python 3.11+ or the 'tomli' "
        "package.  Use a .rtllint.json config instead, or `pip install tomli`.")


def _path_matches(file_path, pattern):
    """Flexible path glob: matches full path, a */suffix, or basename."""
    if not pattern:
        return True
    return (fnmatch.fnmatch(file_path, pattern)
            or fnmatch.fnmatch(file_path, "*/" + pattern.lstrip("/"))
            or fnmatch.fnmatch(Path(file_path).name, pattern))


def _find_waiver(f, waivers):
    """Return the first waiver entry matching finding *f*, or None."""
    for w in waivers:
        rule = w.get("rule")
        if rule and not fnmatch.fnmatch(f.rule, rule):
            continue
        if not _path_matches(f.file, w.get("path") or w.get("file")):
            continue
        line = w.get("line")
        if line is not None and int(line) != f.line:
            continue
        return w
    return None


# ── Filtering / promotion ────────────────────────────────────────────
def apply_rules(findings, rules=None, waivers=None, only=None, werror=False,
                min_severity=None):
    """Filter and re-grade findings.

    *rules*   — ordered list of ``(glob, severity)``; last match wins.
                A severity of "ignored" suppresses the finding.
    *waivers* — list of dicts with optional ``rule``/``path``/``line`` and
                ``reason``; matching findings are suppressed (location-aware).
    *only*    — if set, keep only rules matching one of these globs.

    Returns ``(kept, waived)`` where *waived* carries ``waived_reason``.
    """
    rules = rules or []
    waivers = waivers or []
    kept, waived = [], []
    for f in findings:
        if only and not any(fnmatch.fnmatch(f.rule, p) for p in only):
            continue

        # 1. Location-specific waivers
        w = _find_waiver(f, waivers)
        if w is not None:
            f.waived_reason = w.get("reason", "") or "waived"
            waived.append(f)
            continue

        # 2. Rule severity map (later entries override earlier ones)
        mapped = None
        for glob, sev in rules:
            if fnmatch.fnmatch(f.rule, glob):
                mapped = sev
        if mapped == "ignored":
            f.waived_reason = "rule disabled"
            waived.append(f)
            continue
        if mapped in ("error", "warning", "note"):
            f.severity = mapped

        # 3. Global --werror promotion of anything still a warning
        if werror and f.severity == "warning":
            f.severity = "error"

        # 4. Minimum-severity gate
        if min_severity and SEVERITY_ORDER.get(f.severity, 0) < SEVERITY_ORDER.get(min_severity, 0):
            continue
        kept.append(f)
    return kept, waived


def build_rule_list(config_rules, cli_disable, cli_error):
    """Merge config [rules] + CLI --disable/--error into an ordered list.

    CLI entries are appended last so they win over the config file.
    """
    rules = []
    for name, val in (config_rules or {}).items():
        sev = normalize_severity(val)
        if sev is None:
            print(f"Warning: config rule '{name}' has unknown severity "
                  f"'{val}' (use off/warning/error)", file=sys.stderr)
            continue
        rules.append((name, sev))
    for name in cli_disable or []:
        rules.append((name, "ignored"))
    for name in cli_error or []:
        rules.append((name, "error"))
    return rules


# ── Output ───────────────────────────────────────────────────────────
_SEV_COLOR = {
    "error": Color.red,
    "warning": Color.yellow,
    "note": Color.cyan,
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
def main():
    p = argparse.ArgumentParser(
        prog='rtl-lint',
        description='rtl-lint — SV Static Linter (pyslang)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rtl-lint --filelist rtl.f                 Lint a design via filelist
  rtl-lint -d ./rtl                         Scan a directory
  rtl-lint -d ./rtl --werror                Fail (exit 1) on any warning
  rtl-lint -d ./rtl --disable case-default  Suppress a rule
  rtl-lint -d ./rtl --error width-trunc     Promote a rule to error
  rtl-lint -d ./rtl --rule 'unused-*'       Only show matching rules
  rtl-lint -d ./rtl --cdc                   Clock-domain-crossing check
  rtl-lint -d ./rtl --summary               Show only the summary
  rtl-lint -d ./rtl --json                  Machine-readable output

Config file (.rtllint.toml or .rtllint.json, auto-discovered):
  [rules]                  # severity per rule (glob ok): off | warning | error
  "case-default" = "off"
  "width-trunc"  = "error"

  [[waive]]                # location-specific waivers
  rule   = "unused-port"
  path   = "rtl/perips/*.v"
  reason = "third-party IP"

Inline waivers (standard SystemVerilog, no config needed):
  `pragma diagnostic ignore="-Wwidth-trunc"
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

    ck = p.add_argument_group('checks')
    ck.add_argument('--no-unused', action='store_true',
                    help='Disable unused/undriven signal & port analysis')
    ck.add_argument('--shadow', action='store_true',
                    help='Enable variable-shadowing analysis')
    ck.add_argument('--cdc', action='store_true',
                    help='Enable clock-domain-crossing detection '
                         '(flop-to-flop, opt-in; emits rule "cdc-crossing")')
    ck.add_argument('--cdc-reset', action='append', default=[], metavar='GLOB',
                    help='Extra signal-name glob to treat as a reset for CDC '
                         '(repeatable; defaults match rst*/reset*/*_n)')
    ck.add_argument('--weverything', action='store_true',
                    help='Enable every available pyslang warning')

    rg = p.add_argument_group('rules & severity')
    rg.add_argument('--disable', action='append', default=[], metavar='RULE',
                    help='Suppress a rule by name/glob (repeatable)')
    rg.add_argument('--rule', action='append', default=[], metavar='GLOB',
                    help='Only show rules matching glob (repeatable)')
    rg.add_argument('--error', action='append', default=[], metavar='RULE',
                    help='Promote a rule to error (repeatable)')
    rg.add_argument('--werror', action='store_true',
                    help='Treat all warnings as errors')
    rg.add_argument('--min-severity', choices=('error', 'warning', 'note'),
                    default=None, help='Hide findings below this severity')

    cf = p.add_argument_group('config')
    cf.add_argument('--config', default=None, metavar='PATH',
                    help='Lint config file (.toml/.json); default: auto-discover '
                         '.rtllint.toml/.rtllint.json')
    cf.add_argument('--no-config', action='store_true',
                    help='Ignore any auto-discovered config file')

    out = p.add_argument_group('output')
    out.add_argument('--summary', action='store_true',
                     help='Show only the summary, not individual findings')
    out.add_argument('--no-summary', action='store_true',
                     help='Suppress the trailing summary')
    out.add_argument('--show-waived', action='store_true',
                     help='List findings suppressed by waivers/disabled rules')
    out.add_argument('--json', action='store_true',
                     help='Emit findings as an agent-friendly JSON envelope (see --schema)')
    out.add_argument('--schema', action='store_true',
                     help='Print the JSON Schema for --json output and exit')
    out.add_argument('--no-color', action='store_true', help='Disable ANSI colors')

    a = p.parse_args()

    if a.schema:
        sys.exit(agent_json.print_schema('rtl-lint'))

    if a.no_color or not sys.stdout.isatty() or a.json:
        Color.disable()

    env = Envelope('rtl-lint', filter_command(a)) if a.json else None

    def die(msg, code=agent_json.ERR_INTERNAL, exit_code=2):
        if a.json:
            sys.exit(emit(env.fail(code, msg)))
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(exit_code)

    # ── Load config (CLI overrides config) ──
    cfg = {}
    cfg_path = None
    if not a.no_config:
        cfg_path = Path(a.config) if a.config else discover_config('.')
        if a.config and not Path(a.config).is_file():
            die(f"config not found: {a.config}", agent_json.ERR_BAD_CONFIG)
        if cfg_path:
            try:
                cfg = load_config(cfg_path)
            except SystemExit:
                raise
            except Exception as e:
                die(f"failed to parse config {cfg_path}: {e}",
                    agent_json.ERR_BAD_CONFIG)
    lint_cfg = cfg.get('lint', cfg) if isinstance(cfg, dict) else {}

    def cfg_bool(key, cli_flag):
        return bool(cli_flag or lint_cfg.get(key, False))

    check_unused = not (a.no_unused or lint_cfg.get('unused') is False)
    check_shadow = cfg_bool('shadow', a.shadow)
    check_cdc = cfg_bool('cdc', a.cdc)
    weverything = cfg_bool('weverything', a.weverything)
    werror = cfg_bool('werror', a.werror)
    min_severity = a.min_severity or lint_cfg.get('min_severity')
    cdc_reset_globs = list(a.cdc_reset) + list(lint_cfg.get('cdc_reset', []) or [])

    # ── Resolve sources ──
    all_paths = list(a.files) + list(a.dir)
    if not all_paths and not a.filelist:
        if a.json:
            die('no input: pass files, --dir, or --filelist',
                agent_json.ERR_INPUT_NOT_FOUND)
        p.print_help()
        sys.exit(2)

    fl_root = Path(a.filelist_root).expanduser().resolve()
    parsed = []
    for f in a.filelist:
        try:
            parsed.append(parse_filelist(f, fl_root, prefix=a.filelist_prefix))
        except FileNotFoundError as e:
            die(str(e), agent_json.ERR_BAD_FILELIST)
    scanned = collect_filelist(all_paths, excludes=a.exclude, root=fl_root) if all_paths else FileList()
    filelist = filter_filelist(merge_filelists(*parsed, scanned), a.exclude, fl_root)

    if not filelist.sources:
        die('no .v/.sv source files found', agent_json.ERR_INPUT_NOT_FOUND)

    # ── Build & lint ──
    try:
        comp, _ = build_compilation(filelist.sources, filelist.include_dirs, filelist.defines)
    except Exception as e:
        die(f'compilation failed: {e}', agent_json.ERR_COMPILE_FAILED)
    runner = LintRunner(comp, check_unused=check_unused,
                        check_shadow=check_shadow, weverything=weverything,
                        check_cdc=check_cdc, cdc_reset_globs=cdc_reset_globs)
    findings = runner.run()

    # Merge config [rules]/[[waive]] with CLI flags (CLI wins).
    rules = build_rule_list(cfg.get('rules'), a.disable, a.error)
    waivers = cfg.get('waive') or cfg.get('waivers') or []
    findings, waived = apply_rules(findings, rules=rules, waivers=waivers,
                                   only=a.rule, werror=werror,
                                   min_severity=min_severity)

    # ── Output ──
    has_error = any(f.severity == "error" for f in findings)
    if a.json:
        by_sev, by_rule, by_check = _counts(findings)
        data = {
            'findings':    [f.to_dict() for f in findings],
            'waived':      [f.to_dict() for f in waived],
            'config_path': str(cfg_path) if cfg_path else None,
        }
        summary = {
            'total':        len(findings),
            'by_severity':  by_sev,
            'by_rule':      by_rule,
            'by_check':     by_check,
            'waived':       len(waived),
            'files_linted': len(filelist.sources),
            'has_error':    has_error,
        }
        rc = emit(env.ok(data, summary))
        sys.exit(1 if has_error else rc)
    else:
        if not a.summary:
            print_findings(findings)   # prints the all-clear line when empty
        if a.show_waived:
            print_waived(waived)
        if findings and (a.summary or not a.no_summary):
            print_summary(findings, waived=len(waived))

    # ── Exit code ──
    sys.exit(1 if has_error else 0)


if __name__ == '__main__':
    main()

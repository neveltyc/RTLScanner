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

    def to_dict(self):
        return dict(file=self.file, line=self.line, col=self.col,
                    severity=self.severity, rule=self.rule,
                    message=self.message, check=self.check)

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
                 weverything=False):
        self._comp = compilation
        self._sm = compilation.sourceManager
        self._check_unused = check_unused
        self._check_shadow = check_shadow
        self._eng = pyslang.DiagnosticEngine(self._sm)
        if weverything:
            try:
                self._eng.setWarningOptions(["everything"])
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

        findings.sort(key=lambda f: (f.file, f.line, f.col, f.rule))
        return findings


# ── Filtering / promotion ────────────────────────────────────────────
def apply_rules(findings, disable=None, only=None, errors=None, werror=False,
                min_severity=None):
    """Filter and re-grade findings per user options."""
    disable = set(disable or [])
    errors = set(errors or [])
    result = []
    for f in findings:
        if disable and any(fnmatch.fnmatch(f.rule, p) for p in disable):
            continue
        if only and not any(fnmatch.fnmatch(f.rule, p) for p in only):
            continue
        if f.rule in errors or (werror and f.severity == "warning"):
            f.severity = "error"
        if min_severity and SEVERITY_ORDER.get(f.severity, 0) < SEVERITY_ORDER.get(min_severity, 0):
            continue
        result.append(f)
    return result


# ── Output ───────────────────────────────────────────────────────────
_SEV_COLOR = {
    "error": Color.red,
    "warning": Color.yellow,
    "note": Color.cyan,
}


def _counts(findings):
    by_sev, by_rule = {}, {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    return by_sev, by_rule


def print_summary(findings):
    by_sev, by_rule = _counts(findings)
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
def main():
    p = argparse.ArgumentParser(
        description='rtl_lint — SV Static Linter (pyslang)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rtl_lint --filelist rtl.f                 Lint a design via filelist
  rtl_lint -d ./rtl                         Scan a directory
  rtl_lint -d ./rtl --werror                Fail (exit 1) on any warning
  rtl_lint -d ./rtl --disable case-default  Suppress a rule
  rtl_lint -d ./rtl --error width-trunc     Promote a rule to error
  rtl_lint -d ./rtl --rule 'unused-*'       Only show matching rules
  rtl_lint -d ./rtl --summary               Show only the summary
  rtl_lint -d ./rtl --json                  Machine-readable output
""")
    p.add_argument('files', nargs='*', help='Verilog/SV source files')
    p.add_argument('-d', '--dir', action='append', default=[],
                   help='Directory to scan (recursive)')

    fl = p.add_argument_group('filelist')
    fl.add_argument('--filelist', '-f', action='append', default=[],
                    help='VCS-style .f filelist (repeatable)')
    fl.add_argument('--filelist-root', '--projpath', dest='filelist_root',
                    default='.', help='Base path for filelist relative paths')
    fl.add_argument('--filelist-prefix', default='${PROJPATH}')
    fl.add_argument('--exclude', action='append', default=[])

    ck = p.add_argument_group('checks')
    ck.add_argument('--no-unused', action='store_true',
                    help='Disable unused/undriven signal & port analysis')
    ck.add_argument('--shadow', action='store_true',
                    help='Enable variable-shadowing analysis')
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

    out = p.add_argument_group('output')
    out.add_argument('--summary', action='store_true',
                     help='Show only the summary, not individual findings')
    out.add_argument('--no-summary', action='store_true',
                     help='Suppress the trailing summary')
    out.add_argument('--json', action='store_true', help='JSON output')
    out.add_argument('--no-color', action='store_true')

    a = p.parse_args()

    if a.no_color or not sys.stdout.isatty() or a.json:
        Color.disable()

    # ── Resolve sources ──
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

    # ── Build & lint ──
    comp, _ = build_compilation(filelist.sources, filelist.include_dirs, filelist.defines)
    runner = LintRunner(comp, check_unused=not a.no_unused,
                        check_shadow=a.shadow, weverything=a.weverything)
    findings = runner.run()
    findings = apply_rules(findings, disable=a.disable, only=a.rule,
                           errors=a.error, werror=a.werror,
                           min_severity=a.min_severity)

    # ── Output ──
    if a.json:
        by_sev, by_rule = _counts(findings)
        print(json.dumps({
            'findings': [f.to_dict() for f in findings],
            'summary': {
                'total': len(findings),
                'by_severity': by_sev,
                'by_rule': by_rule,
                'files_linted': len(filelist.sources),
            },
        }, indent=2, ensure_ascii=False))
    else:
        if not a.summary:
            print_findings(findings)   # prints the all-clear line when empty
        if findings and (a.summary or not a.no_summary):
            print_summary(findings)

    # ── Exit code ──
    has_error = any(f.severity == "error" for f in findings)
    sys.exit(1 if has_error else 0)


if __name__ == '__main__':
    main()

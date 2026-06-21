#!/usr/bin/env python3
"""rtl_find — design-wide node lookup by glob/regex pattern.

``xref`` answers "where is *this exact name* declared and used" and ``tree
--filter`` narrows a single hierarchy view; neither answers "give me every node
in the whole design whose path matches a pattern".  ``find`` does — the
slang-netlist ``--find`` / ``--find-regex`` analogue.  It walks the elaborated
design, matches each signal and instance's hierarchical path against a glob
(default) or a regex (``--regex``), and reports the matches with their source
locations, so an agent can discover the nodes to feed into
``trace``/``fanin``/``fanout``/``xref`` without already knowing their names.

    rtlscanner find -d ./rtl -p 'top.**.u_fifo*'
    rtlscanner find -d ./rtl -p '*_valid' --kind signal
    rtlscanner find -d ./rtl --regex -p '.*\\.state$' --scope top.u_ctrl
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import pyslang.ast as ast
except ImportError:  # rtl_common prints the user-facing dependency error.
    ast = None

import agent_json
import rtl_cli
from rtl_common import Color, rel_path, safe_str
from rtl_glob import compile_regex, regex_match, wildcard_match
from rtl_slang import iter_instances, resolve_scope, scope_visit, symbol_key


# Coarse node categories, the vocabulary of ``--kind``.  "signal" is every
# net/variable (registers and port internals included); "instance" is every
# elaborated module/interface instance.
_KIND_CHOICES = ("all", "signal", "instance")

_SIGNAL_SYMBOL_KINDS = (
    (ast.SymbolKind.Net, ast.SymbolKind.Variable) if ast is not None else ()
)


@dataclass
class FindNode:
    """One design node matched by ``find``."""
    category: str                 # coarse: "signal" | "instance"
    name: str
    kind: str                     # elaborated SymbolKind name (Net/Variable/Instance)
    hierarchical_path: str
    type_str: str = ""            # signal type, or module name for an instance
    file: str = ""
    line: int = 0
    column: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = {
            "category": self.category,
            "name": self.name,
            "kind": self.kind,
            "hierarchical_path": self.hierarchical_path,
        }
        if self.type_str:
            # An instance's "type" is the module it elaborates; a signal's is its
            # data type.  Name the field by what it is so the JSON reads cleanly.
            d["module" if self.category == "instance" else "type"] = self.type_str
        if self.file:
            d["file"] = self.file
            d["line"] = self.line
            d["column"] = self.column
            d["location"] = {"file": self.file, "line": self.line, "column": self.column}
        return d


class FindAnalyzer:
    """Match elaborated design nodes against a glob / regex pattern."""

    def __init__(self, compilation, *, root: Optional[Path] = None):
        self._comp = compilation
        self._root = compilation.getRoot()
        self._sm = compilation.sourceManager
        self._root_dir = (root or Path.cwd())

    def get_top_paths(self) -> list[str]:
        paths = []
        for top in self._root.topInstances:
            try:
                paths.append(top.hierarchicalPath)
            except Exception:
                continue
        return paths

    def _loc(self, sym) -> tuple[str, int, int]:
        try:
            loc = sym.location
            return (
                rel_path(safe_str(self._sm.getFileName(loc), ""), self._root_dir),
                int(self._sm.getLineNumber(loc)),
                int(self._sm.getColumnNumber(loc)),
            )
        except Exception:
            return "", 0, 0

    @staticmethod
    def _under(path: str, scope_path: str) -> bool:
        if not scope_path:
            return True
        return path == scope_path or path.startswith(scope_path + ".")

    def _iter_nodes(self, want_signals: bool, want_instances: bool,
                    scope_path: str):
        """Yield every requested design node as a (category, symbol) pair.

        Signals are collected per instance body (local nets/variables, not
        descending into child instances — each child is iterated in its own
        right), so identical sibling/generate instances each contribute their
        own elaborated paths (``top.u_dp0.q`` and ``top.u_dp1.q`` both appear).
        """
        for inst in iter_instances(self._root):
            try:
                inst_path = safe_str(inst.hierarchicalPath, "")
            except Exception:
                continue
            # A signal's path is ``<inst_path>.<name>``, so neither the instance
            # node nor any of its local signals can fall under ``scope_path``
            # unless the instance itself is at or under it — skip the whole
            # subtree when it is not (a no-op when no scope was given).
            if not self._under(inst_path, scope_path):
                continue
            if want_instances:
                yield ("instance", inst)
            if not want_signals:
                continue
            body = getattr(inst, "body", None)
            if body is None:
                continue
            sigs: list = []

            def collect(sym):
                sigs.append(sym)

            try:
                scope_visit(body, {k: collect for k in _SIGNAL_SYMBOL_KINDS})
            except Exception:
                continue
            for sym in sigs:
                yield ("signal", sym)

    def _make_node(self, category: str, sym) -> Optional[FindNode]:
        name = safe_str(getattr(sym, "name", ""), "")
        if not name:
            return None
        path = safe_str(getattr(sym, "hierarchicalPath", ""), "")
        kind = safe_str(getattr(getattr(sym, "kind", None), "name", ""), "")
        if category == "instance":
            body = getattr(sym, "body", None)
            type_str = safe_str(getattr(body, "name", ""), "") if body is not None else ""
        else:
            try:
                type_str = safe_str(sym.type, "")
            except Exception:
                type_str = ""
        f, ln, col = self._loc(sym)
        return FindNode(
            category=category, name=name, kind=kind, hierarchical_path=path,
            type_str=type_str, file=f, line=ln, column=col)

    def find(self, pattern: str, *, regex: bool, kind: str,
             scope_path: str = "") -> list[FindNode]:
        """Return the matching nodes, sorted by hierarchical path then category."""
        want_signals = kind in ("all", "signal")
        want_instances = kind in ("all", "instance")

        if regex:
            compiled = compile_regex(pattern)   # may raise re.error
            matches_path = lambda p: regex_match(p, compiled)  # noqa: E731
        else:
            matches_path = lambda p: wildcard_match(p, pattern)  # noqa: E731

        out: list[FindNode] = []
        seen = set()
        for category, sym in self._iter_nodes(want_signals, want_instances,
                                              scope_path):
            try:
                path = safe_str(sym.hierarchicalPath, "")
            except Exception:
                continue
            if not path or not matches_path(path):
                continue
            key = (category, symbol_key(sym))
            if key in seen:
                continue
            seen.add(key)
            node = self._make_node(category, sym)
            if node is not None:
                out.append(node)
        out.sort(key=lambda n: (n.hierarchical_path, n.category))
        return out


# ── CLI plumbing ─────────────────────────────────────────────────────
def add_arguments(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("find")
    g.add_argument("-p", "--pattern", default=None, metavar="PATTERN",
                   help="Glob (default) or regex (--regex) matched against each "
                        "node's hierarchical path. Glob: '*' within a segment, "
                        "'**'/'...' across '.', '?' one char.")
    g.add_argument("--regex", action="store_true",
                   help="Interpret PATTERN as a Python regex (whole-path match) "
                        "instead of a glob")
    g.add_argument("--kind", choices=_KIND_CHOICES, default="all",
                   help="Restrict matches to signals (nets/variables), instances, "
                        "or all (default)")
    g.add_argument("--scope", default=None, metavar="SCOPE",
                   help="Restrict the search to this scope and its descendants")


def _fmt_loc(file: str, line: int, column: int) -> str:
    if not file:
        return "<unknown>"
    return f"{file}:{int(line or 0)}:{int(column or 0)}"


@dataclass
class FindResult(agent_json.CommandResult):
    """Typed result of ``find``: design nodes whose path matched the pattern."""
    nodes: list
    pattern: str
    regex: bool
    kind: str
    scope: str = ""

    def __post_init__(self):
        self.signal_count = sum(1 for n in self.nodes if n.category == "signal")
        self.instance_count = sum(1 for n in self.nodes if n.category == "instance")

    def to_json(self, limit):
        shown, total, truncated = agent_json.clip(self.nodes, limit)
        data = {
            "mode": "find",
            "pattern": self.pattern,
            "regex": self.regex,
            "kind": self.kind,
            "scope": self.scope,
            "matches": [n.to_dict() for n in shown],
            "match_count": total,
        }
        summary = {
            "mode": "find",
            "matches": total,
            "signals": self.signal_count,
            "instances": self.instance_count,
            "truncated": truncated,
            "limit": limit,
        }
        return data, summary

    def render_human(self, limit):
        C = Color
        mode = "regex" if self.regex else "glob"
        print(f"Pattern: {C.bold(self.pattern)}  {C.dim('(' + mode + ')')}")
        if self.scope:
            print(f"Scope:   {C.cyan(self.scope)}")
        if self.kind != "all":
            print(f"Kind:    {C.dim(self.kind)}")
        print("─" * 60)
        if not self.nodes:
            print(C.dim("(no matching nodes)"))
            print()
            return 0
        shown, total, truncated = agent_json.clip(self.nodes, limit)
        for n in shown:
            extra = f" {C.dim(n.type_str)}" if n.type_str else ""
            tag = C.yellow(n.kind)
            print(f"  {C.green(n.hierarchical_path)}  {tag}{extra}")
            print(f"      {C.cyan(_fmt_loc(n.file, n.line, n.column))}")
        counts = f"{total} match" + ("" if total == 1 else "es")
        counts += f" ({self.signal_count} signals, {self.instance_count} instances)"
        print(f"\n  {C.dim(counts)}")
        if truncated:
            print(f"  {C.dim(agent_json.truncation_note(len(shown), total, 'matches'))}")
        print()
        return 0


def run(args, env):
    pattern = getattr(args, "pattern", None)
    if not pattern:
        raise rtl_cli.CliError(
            agent_json.ERR_INPUT_NOT_FOUND,
            "specify a pattern with --pattern/-p",
        )

    prepared = rtl_cli.prepare_compilation_checked(args, env)
    analyzer = FindAnalyzer(prepared.comp, root=prepared.resolved_inputs.root)

    scope = args.scope or ""
    if scope and resolve_scope(analyzer._root, scope) is None:
        raise rtl_cli.scope_not_found_error(analyzer._root, scope)

    try:
        nodes = analyzer.find(pattern, regex=bool(args.regex),
                              kind=args.kind, scope_path=scope)
    except re.error as e:
        raise rtl_cli.CliError(
            agent_json.ERR_INPUT_NOT_FOUND,
            f"invalid regex pattern '{pattern}': {e}",
        )

    out = FindResult(nodes, pattern, bool(args.regex), args.kind, scope)
    return agent_json.render(env, out, agent_json.resolve_limit(args.limit))

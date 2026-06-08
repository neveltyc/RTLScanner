#!/usr/bin/env python3
"""rtl_xref — source-oriented symbol definition/reference index."""

from __future__ import annotations

import argparse
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

import agent_json
from agent_json import emit
from rtl_common import Color, build_compilation, safe_str
from rtl_config import build_filelist, load_config, resolve_inputs
from rtl_slang import (
    analyzed_procedures,
    expr_refs_symbol,
    expr_symbols,
    iter_instances,
    procedure_label,
    procedure_reads_symbol,
    resolve_scope,
    same_symbol,
    scope_visit,
    symbol_key,
)


@dataclass
class SymbolInfo:
    name: str
    kind: str
    type_str: str = ""
    direction: str = ""
    hierarchical_path: str = ""
    lexical_path: str = ""
    file: str = ""
    line: int = 0

    def to_dict(self):
        d = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type_str,
            "hierarchical_path": self.hierarchical_path,
            "lexical_path": self.lexical_path,
        }
        if self.direction:
            d["direction"] = self.direction
        if self.file:
            d["file"] = self.file
            d["line"] = self.line
        return d


@dataclass
class ReferenceInfo:
    access: str
    kind: str
    description: str
    scope_path: str = ""
    instance: str = ""
    port: str = ""
    direction: str = ""
    file: str = ""
    line: int = 0

    def key(self):
        return (self.access, self.kind, self.description, self.scope_path,
                self.instance, self.port, self.file, self.line)

    def to_dict(self):
        d = {
            "access": self.access,
            "kind": self.kind,
            "description": self.description,
            "scope_path": self.scope_path,
        }
        if self.instance:
            d["instance"] = self.instance
        if self.port:
            d["port"] = self.port
        if self.direction:
            d["direction"] = self.direction
        if self.file:
            d["file"] = self.file
            d["line"] = self.line
        return d


@dataclass
class XrefMatch:
    symbol: SymbolInfo
    definitions: list[SymbolInfo] = field(default_factory=list)
    references: list[ReferenceInfo] = field(default_factory=list)

    def to_dict(self):
        reads = sum(1 for r in self.references if r.access in ("read", "readwrite"))
        writes = sum(1 for r in self.references if r.access in ("write", "readwrite"))
        return {
            "symbol": self.symbol.to_dict(),
            "definitions": [d.to_dict() for d in self.definitions],
            "references": [r.to_dict() for r in self.references],
            "summary": {
                "definitions": len(self.definitions),
                "references": len(self.references),
                "reads": reads,
                "writes": writes,
                "port_connections": sum(1 for r in self.references if r.kind == "port_connection"),
            },
        }


_DIR_NAMES = {
    ast.ArgumentDirection.In: "input",
    ast.ArgumentDirection.Out: "output",
    ast.ArgumentDirection.InOut: "inout",
    ast.ArgumentDirection.Ref: "ref",
}

_SIGNAL_KINDS = tuple(
    k for k in (
        getattr(ast.SymbolKind, "Net", None),
        getattr(ast.SymbolKind, "Variable", None),
        getattr(ast.SymbolKind, "Parameter", None),
        getattr(ast.SymbolKind, "TypeParameter", None),
        getattr(ast.SymbolKind, "TypeAlias", None),
    ) if k is not None
)


def _dir_name(d) -> str:
    return _DIR_NAMES.get(d, str(d).split(".")[-1].lower())


class XrefAnalyzer:
    """Build symbol xrefs from pyslang's elaborated symbols and analysis sets."""

    def __init__(self, compilation):
        self._comp = compilation
        self._root = compilation.getRoot()
        self._sm = compilation.sourceManager
        self._cwd = Path.cwd().resolve()
        _ = compilation.getAllDiagnostics()
        self._mgr = analysis.AnalysisManager()
        self._mgr.analyze(compilation)

    def _rel(self, name: str) -> str:
        if not name:
            return name
        try:
            import os
            p = Path(name).resolve()
            rel = os.path.relpath(p, self._cwd)
            return rel if not rel.startswith(os.pardir + os.sep) else p.as_posix()
        except Exception:
            return name

    def _loc(self, sym_or_range):
        try:
            loc = getattr(sym_or_range, "start", None) or sym_or_range.location
            return self._rel(safe_str(self._sm.getFileName(loc))), int(self._sm.getLineNumber(loc))
        except Exception:
            return "", 0

    def _path(self, sym, attr: str) -> str:
        return safe_str(getattr(sym, attr, ""), "")

    def _type(self, sym) -> str:
        try:
            return safe_str(sym.type, "")
        except Exception:
            try:
                return safe_str(sym.targetType, "")
            except Exception:
                return ""

    def _symbol_info(self, sym, *, direction="") -> SymbolInfo:
        f, ln = self._loc(sym)
        return SymbolInfo(
            name=safe_str(getattr(sym, "name", ""), ""),
            kind=safe_str(getattr(getattr(sym, "kind", ""), "name", getattr(sym, "kind", "")), ""),
            type_str=self._type(sym),
            direction=direction,
            hierarchical_path=self._path(sym, "hierarchicalPath"),
            lexical_path=self._path(sym, "lexicalPath"),
            file=f,
            line=ln,
        )

    def _definition_symbols(self, body, name: str):
        out = []
        seen = set()

        def add(sym, direction=""):
            if sym is None:
                return
            key = (symbol_key(sym), direction)
            if key in seen:
                return
            seen.add(key)
            out.append((sym, direction))

        try:
            sym = body.find(name)
            if sym is not None:
                add(sym)
        except Exception:
            pass

        try:
            for port in body.portList:
                if safe_str(getattr(port, "name", ""), "") == name:
                    add(port, _dir_name(port.direction))
                    add(getattr(port, "internalSymbol", None), _dir_name(port.direction))
        except Exception:
            pass

        def collect(sym):
            if safe_str(getattr(sym, "name", ""), "") == name:
                add(sym)

        scope_visit(body, {kind: collect for kind in _SIGNAL_KINDS})
        return out

    def _scan_instances(self, base_inst, recursive: bool):
        base_path = safe_str(getattr(base_inst, "hierarchicalPath", ""), "")
        if not recursive:
            return [base_inst]
        out = []
        for inst in iter_instances(self._root):
            path = safe_str(getattr(inst, "hierarchicalPath", ""), "")
            if path == base_path or path.startswith(base_path + "."):
                out.append(inst)
        return out or [base_inst]

    def _candidate_symbols(self, base_inst, name: str, recursive: bool):
        out = []
        seen = set()
        for inst in self._scan_instances(base_inst, recursive):
            body = getattr(inst, "body", None)
            if body is None:
                continue
            for sym, direction in self._definition_symbols(body, name):
                key = (symbol_key(sym), direction)
                if key in seen:
                    continue
                seen.add(key)
                out.append((inst, sym, direction))
        return out

    def _contains_read_symbol(self, proc, symbol) -> bool:
        try:
            return any(same_symbol(rr.symbol, symbol) for rr in (proc.readSet or []))
        except Exception:
            return False

    def _contains_driver_symbol(self, proc, symbol) -> bool:
        try:
            return any(same_symbol(d.symbol, symbol) for d in (proc.drivers or []))
        except Exception:
            return False

    def _proc_kind_desc(self, proc):
        try:
            kind = proc.analyzedSymbol.kind
        except Exception:
            kind = None
        if kind == ast.SymbolKind.ContinuousAssign:
            return "continuous_assign", "assign"
        if kind == ast.SymbolKind.ProceduralBlock:
            return "procedural", procedure_label(proc)
        return "procedure", safe_str(kind, "procedure")

    def _scope_path(self, inst):
        return safe_str(getattr(inst, "hierarchicalPath", ""), "")

    def _procedural_refs(self, symbol, scan_instances):
        refs = []
        for inst in scan_instances:
            body = getattr(inst, "body", None)
            if body is None:
                continue
            for proc in analyzed_procedures(self._mgr, body):
                try:
                    psym = proc.analyzedSymbol
                except Exception:
                    continue
                kind, desc = self._proc_kind_desc(proc)
                f, ln = self._loc(psym)
                scope_path = self._scope_path(inst)
                if self._contains_read_symbol(proc, symbol):
                    refs.append(ReferenceInfo(
                        access="read", kind=kind, description=desc,
                        scope_path=scope_path, file=f, line=ln,
                    ))
                elif procedure_reads_symbol(proc, symbol):
                    refs.append(ReferenceInfo(
                        access="read", kind="timing_control", description=desc,
                        scope_path=scope_path, file=f, line=ln,
                    ))
                if self._contains_driver_symbol(proc, symbol):
                    refs.append(ReferenceInfo(
                        access="write", kind=kind, description=desc,
                        scope_path=scope_path, file=f, line=ln,
                    ))
        return refs

    def _port_refs(self, symbol, scan_instances):
        refs = []
        for parent in scan_instances:
            body = getattr(parent, "body", None)
            if body is None:
                continue
            children = []

            def collect(inst):
                children.append(inst)
                return ast.VisitAction.Skip

            scope_visit(body, {ast.SymbolKind.Instance: collect})
            for inst in children:
                try:
                    pcs = inst.portConnections
                    inst_name = safe_str(inst.name, "")
                    inst_path = safe_str(inst.hierarchicalPath, inst_name)
                except Exception:
                    continue
                for pc in pcs:
                    expr = getattr(pc, "expression", None)
                    if expr is None:
                        continue
                    try:
                        if not expr_refs_symbol(expr, symbol):
                            continue
                        port = pc.port
                        direction = _dir_name(port.direction)
                        if port.direction == ast.ArgumentDirection.Out:
                            access = "write"
                        elif port.direction == ast.ArgumentDirection.InOut:
                            access = "readwrite"
                        else:
                            access = "read"
                        f, ln = self._loc(inst)
                        refs.append(ReferenceInfo(
                            access=access,
                            kind="port_connection",
                            description=f"{inst_name}.{safe_str(port.name, '?')} {direction}",
                            scope_path=self._scope_path(parent),
                            instance=inst_path,
                            port=safe_str(port.name, ""),
                            direction=direction,
                            file=f,
                            line=ln,
                        ))
                    except Exception:
                        continue
        return refs

    def _dedupe_refs(self, refs):
        out = []
        seen = set()
        for ref in refs:
            key = ref.key()
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
        out.sort(key=lambda r: (r.file, r.line, r.kind, r.access, r.description))
        return out

    def xref(self, scope_path: str, name: str, *, recursive=False):
        base = resolve_scope(self._root, scope_path)
        if base is None:
            return None
        candidates = self._candidate_symbols(base, name, recursive)
        if not candidates:
            return []

        scan_instances = self._scan_instances(base, recursive)
        matches = []
        seen_symbols = set()
        for _inst, sym, direction in candidates:
            # PortSymbol and its internal variable often represent the same user
            # declaration.  Keep the user-facing port definition, but analyze the
            # internal symbol when present because expressions refer to it.
            analyze_sym = getattr(sym, "internalSymbol", None) or sym
            key = symbol_key(analyze_sym)
            if key in seen_symbols:
                continue
            seen_symbols.add(key)

            definitions = []
            for _dinst, dsym, ddirection in candidates:
                if same_symbol(getattr(dsym, "internalSymbol", None) or dsym, analyze_sym):
                    definitions.append(self._symbol_info(dsym, direction=ddirection))
            if not definitions:
                definitions = [self._symbol_info(sym, direction=direction)]

            refs = []
            if getattr(analyze_sym, "kind", None) in (ast.SymbolKind.Net, ast.SymbolKind.Variable, ast.SymbolKind.Parameter):
                refs.extend(self._procedural_refs(analyze_sym, scan_instances))
                refs.extend(self._port_refs(analyze_sym, scan_instances))
            refs = self._dedupe_refs(refs)
            matches.append(XrefMatch(
                symbol=self._symbol_info(analyze_sym, direction=direction),
                definitions=definitions,
                references=refs,
            ))

        matches.sort(key=lambda m: m.symbol.hierarchical_path)
        return matches

    def get_top_paths(self):
        paths = []
        for t in self._root.topInstances:
            try:
                paths.append(t.hierarchicalPath)
            except Exception:
                continue
        return paths


# ── CLI plumbing ─────────────────────────────────────────────────────
def add_arguments(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("xref")
    g.add_argument("-s", "--signal", dest="name", default=None, metavar="NAME",
                   help="Symbol/signal name to index (required)")
    g.add_argument("--name", dest="name_alt", default=None, metavar="NAME",
                   help="Alias for --signal when indexing non-signal symbols")
    g.add_argument("--scope", default=None, metavar="SCOPE",
                   help="Hierarchical scope; auto-detect when single top")
    g.add_argument("--recursive", action="store_true",
                   help="Find matching symbols in the selected scope and descendants")


def _prepare(args, env):
    cfg, cfg_path = load_config()
    ri = resolve_inputs(
        cli_files=args.files,
        cli_dir=args.dir,
        cli_filelist=args.filelist,
        cli_exclude=args.exclude,
        config=cfg,
        config_path=cfg_path,
    )
    for note in ri.notes:
        print(f"note: {note}", file=sys.stderr)

    try:
        filelist = build_filelist(ri)
    except FileNotFoundError as e:
        return _die(env, str(e), agent_json.ERR_BAD_FILELIST)
    except ValueError as e:
        return _die(env, str(e), agent_json.ERR_INPUT_NOT_FOUND)
    if not filelist.sources:
        return _die(env, "no .v/.sv source files found", agent_json.ERR_INPUT_NOT_FOUND)

    try:
        comp, _ = build_compilation(filelist.sources, filelist.include_dirs, filelist.defines)
    except Exception as e:
        return _die(env, f"compilation failed: {e}", agent_json.ERR_COMPILE_FAILED)

    xa = XrefAnalyzer(comp)
    scope = args.scope
    if scope is None:
        tops = xa.get_top_paths()
        if len(tops) == 1:
            scope = tops[0]
        elif tops:
            return _die(env, "multiple tops, specify --scope: " + ", ".join(tops),
                        agent_json.ERR_SCOPE_NOT_FOUND)
        else:
            return _die(env, "no top modules found", agent_json.ERR_NO_TOP)
    return xa, scope


def _die(env, msg, code):
    if env is not None:
        return emit(env.fail(code, msg))
    print(f"Error: {msg}", file=sys.stderr)
    return 2


def _print_pretty(scope, name, matches):
    print(f"Symbol: {Color.bold(name)}")
    print(f"Scope:  {Color.cyan(scope)}")
    print("─" * 60)
    if not matches:
        print(Color.dim("(no matching symbols)"))
        return
    for match in matches:
        sym = match.symbol
        print(f"\n  {Color.green(sym.hierarchical_path or sym.name)}  {Color.dim(sym.kind)}")
        if sym.type_str:
            print(f"    type: {Color.dim(sym.type_str)}")
        if match.definitions:
            print(f"    {Color.yellow('definitions')} ({len(match.definitions)})")
            for d in match.definitions:
                loc = f"  {Color.dim(d.file + ':' + str(d.line))}" if d.file else ""
                extra = f" {Color.dim(d.direction)}" if d.direction else ""
                print(f"      {d.kind}{extra} {Color.cyan(d.name)}{loc}")
        print(f"    {Color.yellow('references')} ({len(match.references)})")
        if not match.references:
            print(f"      {Color.dim('(none found)')}")
        for r in match.references:
            loc = f"  {Color.dim(r.file + ':' + str(r.line))}" if r.file else ""
            target = f" {Color.cyan(r.instance + '.' + r.port)}" if r.instance and r.port else ""
            print(f"      {r.access:9s} {r.kind:17s} {r.description}{target}{loc}")
    print()


def run(args, env):
    name = args.name_alt or args.name
    if not name:
        return _die(env, "specify --signal/-s NAME or --name NAME", agent_json.ERR_INPUT_NOT_FOUND)

    prepared = _prepare(args, env)
    if not isinstance(prepared, tuple):
        return prepared
    xa, scope = prepared
    matches = xa.xref(scope, name, recursive=args.recursive)
    if matches is None:
        return _die(env, f"scope '{scope}' not found", agent_json.ERR_SCOPE_NOT_FOUND)
    if not matches:
        return _die(env, f"symbol '{name}' not found in scope '{scope}'", agent_json.ERR_SIGNAL_NOT_FOUND)

    data_matches = [m.to_dict() for m in matches]
    total_refs = sum(m["summary"]["references"] for m in data_matches)
    reads = sum(m["summary"]["reads"] for m in data_matches)
    writes = sum(m["summary"]["writes"] for m in data_matches)
    data = {
        "mode": "xref",
        "scope": scope,
        "name": name,
        "recursive": bool(args.recursive),
        "matches": data_matches,
    }
    summary = {
        "mode": "xref",
        "symbols": len(data_matches),
        "definitions": sum(m["summary"]["definitions"] for m in data_matches),
        "references": total_refs,
        "reads": reads,
        "writes": writes,
        "port_connections": sum(m["summary"]["port_connections"] for m in data_matches),
    }
    if env is not None:
        return emit(env.ok(data, summary))
    _print_pretty(scope, name, matches)
    return 0

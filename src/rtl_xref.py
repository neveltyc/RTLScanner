#!/usr/bin/env python3
"""rtl_xref — source-oriented symbol definition/reference index."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
import os
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
from rtl_config import build_filelist, load_config, resolve_inputs, xref_config
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
    column: int = 0

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
            d["column"] = self.column
            d["location"] = {"file": self.file, "line": self.line, "column": self.column}
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
    column: int = 0

    def key(self):
        return (self.access, self.kind, self.description, self.scope_path,
                self.instance, self.port, self.file, self.line, self.column)

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
            d["column"] = self.column
            d["location"] = {"file": self.file, "line": self.line, "column": self.column}
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


@dataclass
class ModuleDefinitionInfo:
    name: str
    file: str = ""
    line: int = 0
    column: int = 0

    def to_dict(self):
        d = {"kind": "module", "name": self.name}
        if self.file:
            d.update({
                "file": self.file,
                "line": self.line,
                "column": self.column,
                "location": {"file": self.file, "line": self.line, "column": self.column},
            })
        return d


@dataclass
class ModuleReferenceInfo:
    kind: str
    module: str
    instance_path: str
    file: str = ""
    line: int = 0
    column: int = 0
    instance: str = ""
    parent_scope: str = ""
    parent_module: str = ""
    parameter_values: dict[str, str] = field(default_factory=dict)

    def to_dict(self):
        d = {
            "kind": self.kind,
            "module": self.module,
            "instance_path": self.instance_path,
        }
        if self.file:
            d.update({
                "file": self.file,
                "line": self.line,
                "column": self.column,
                "location": {"file": self.file, "line": self.line, "column": self.column},
            })
        details = {
            "instance": self.instance,
            "parent_scope": self.parent_scope,
            "parent_module": self.parent_module,
        }
        if self.parameter_values:
            details["parameter_values"] = dict(self.parameter_values)
        d["details"] = {k: v for k, v in details.items() if v not in ("", None, {})}
        return d


@dataclass
class ModuleXref:
    name: str
    definitions: list[ModuleDefinitionInfo] = field(default_factory=list)
    references: list[ModuleReferenceInfo] = field(default_factory=list)

    def to_dict(self):
        return {
            "mode": "xref",
            "target": {"kind": "module", "name": self.name},
            "definitions": [d.to_dict() for d in self.definitions],
            "references": [r.to_dict() for r in self.references],
            "summary": {
                "definitions": len(self.definitions),
                "references": len(self.references),
                "instances": len(self.references),
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

    def __init__(self, compilation, *, root: Optional[Path] = None,
                 path_style: str = "relative"):
        self._comp = compilation
        self._root = compilation.getRoot()
        self._sm = compilation.sourceManager
        self._root_dir = (root or Path.cwd()).expanduser().resolve()
        self._path_style = path_style if path_style in {"relative", "absolute", "name"} else "relative"
        _ = compilation.getAllDiagnostics()
        self._mgr = analysis.AnalysisManager()
        self._mgr.analyze(compilation)

    def _format_file(self, name: str) -> str:
        if not name:
            return name
        try:
            p = Path(name).resolve()
            if self._path_style == "absolute":
                return p.as_posix()
            if self._path_style == "name":
                return p.name
            rel = os.path.relpath(p, self._root_dir)
            if rel == ".":
                return "."
            if rel.startswith(os.pardir + os.sep) or rel == os.pardir:
                return p.as_posix()
            return "./" + Path(rel).as_posix()
        except Exception:
            return name

    def _loc(self, sym_or_range):
        try:
            loc = getattr(sym_or_range, "start", None) or sym_or_range.location
            return (
                self._format_file(safe_str(self._sm.getFileName(loc))),
                int(self._sm.getLineNumber(loc)),
                int(self._sm.getColumnNumber(loc)),
            )
        except Exception:
            return "", 0, 0

    def _loc_token(self, token):
        try:
            loc = token.location
            return (
                self._format_file(safe_str(self._sm.getFileName(loc))),
                int(self._sm.getLineNumber(loc)),
                int(self._sm.getColumnNumber(loc)),
            )
        except Exception:
            return "", 0, 0

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
        f, ln, col = self._loc(sym)
        return SymbolInfo(
            name=safe_str(getattr(sym, "name", ""), ""),
            kind=safe_str(getattr(getattr(sym, "kind", ""), "name", getattr(sym, "kind", "")), ""),
            type_str=self._type(sym),
            direction=direction,
            hierarchical_path=self._path(sym, "hierarchicalPath"),
            lexical_path=self._path(sym, "lexicalPath"),
            file=f,
            line=ln,
            column=col,
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
                f, ln, col = self._loc(psym)
                scope_path = self._scope_path(inst)
                if self._contains_read_symbol(proc, symbol):
                    refs.append(ReferenceInfo(
                        access="read", kind=kind, description=desc,
                        scope_path=scope_path, file=f, line=ln, column=col,
                    ))
                elif procedure_reads_symbol(proc, symbol):
                    refs.append(ReferenceInfo(
                        access="read", kind="timing_control", description=desc,
                        scope_path=scope_path, file=f, line=ln, column=col,
                    ))
                if self._contains_driver_symbol(proc, symbol):
                    refs.append(ReferenceInfo(
                        access="write", kind=kind, description=desc,
                        scope_path=scope_path, file=f, line=ln, column=col,
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
                        f, ln, col = self._loc(inst)
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
                            column=col,
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

    def _module_declarations(self, name: str) -> list[ModuleDefinitionInfo]:
        defs = []
        seen = set()
        try:
            trees = self._comp.getSyntaxTrees()
        except Exception:
            trees = []

        def collect(node):
            if type(node).__name__ != "ModuleDeclarationSyntax":
                return
            try:
                tok = node.header.name
                module_name = safe_str(tok.valueText, "")
            except Exception:
                return
            if module_name != name:
                return
            f, ln, col = self._loc_token(tok)
            key = (module_name, f, ln, col)
            if key in seen:
                return
            seen.add(key)
            defs.append(ModuleDefinitionInfo(name=module_name, file=f, line=ln, column=col))

        for tree in trees:
            try:
                tree.root.visit(f=collect)
            except Exception:
                continue
        defs.sort(key=lambda d: (d.file, d.line, d.column))
        return defs

    def _param_values(self, body) -> dict[str, str]:
        values = {}
        try:
            params = body.parameters or []
        except Exception:
            return values
        for param in params:
            pname = safe_str(getattr(param, "name", ""), "")
            if not pname:
                continue
            value = None
            try:
                value = safe_str(param.value, "")
            except Exception:
                pass
            if not value:
                try:
                    value = safe_str(param.targetType, "")
                except Exception:
                    pass
            if value:
                values[pname] = value
        return values

    def _instance_parent_path(self, path: str) -> str:
        return path.rsplit(".", 1)[0] if "." in path else ""

    def _is_under_scope(self, path: str, scope_path: str) -> bool:
        if not scope_path:
            return True
        return path == scope_path or path.startswith(scope_path + ".")

    def _module_references(self, name: str, *, scope_path: str = "") -> Optional[list[ModuleReferenceInfo]]:
        if scope_path and resolve_scope(self._root, scope_path) is None:
            return None
        refs = []
        seen = set()
        for inst in iter_instances(self._root):
            body = getattr(inst, "body", None)
            if body is None:
                continue
            module_name = safe_str(getattr(body, "name", ""), "")
            if module_name != name:
                continue
            inst_path = safe_str(getattr(inst, "hierarchicalPath", ""), "")
            if not self._is_under_scope(inst_path, scope_path):
                continue
            f, ln, col = self._loc(inst)
            ref_kind = "top_instance" if "." not in inst_path else "instance"
            parent_scope = self._instance_parent_path(inst_path)
            parent_module = ""
            try:
                parent_module = safe_str(inst.declaringDefinition.name, "")
                if parent_module == name and ref_kind == "top_instance":
                    parent_module = ""
            except Exception:
                pass
            key = (ref_kind, module_name, inst_path, f, ln, col)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ModuleReferenceInfo(
                kind=ref_kind,
                module=module_name,
                instance_path=inst_path,
                file=f,
                line=ln,
                column=col,
                instance=safe_str(getattr(inst, "name", ""), ""),
                parent_scope=parent_scope,
                parent_module=parent_module,
                parameter_values=self._param_values(body),
            ))
        refs.sort(key=lambda r: (r.file, r.line, r.column, r.instance_path))
        return refs

    def xref_module(self, name: str, *, scope_path: str = "") -> ModuleXref | None:
        refs = self._module_references(name, scope_path=scope_path or "")
        if refs is None:
            return None
        return ModuleXref(
            name=name,
            definitions=self._module_declarations(name),
            references=refs,
        )

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
                   help="Signal/symbol name to locate")
    g.add_argument("--name", dest="name_alt", default=None, metavar="NAME",
                   help="Alias for --signal when indexing non-signal symbols")
    g.add_argument("--module", default=None, metavar="MODULE",
                   help="Module name to locate and list instance sites")
    g.add_argument("--scope", default=None, metavar="SCOPE",
                   help="Hierarchical scope; auto-detect for signal xref when single top")
    g.add_argument("--recursive", action="store_true",
                   help="Find matching symbols in the selected scope and descendants")
    g.add_argument("--verbose", action="store_true",
                   help="Show detailed context in human-readable output")


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

    xcfg = xref_config(cfg)
    xa = XrefAnalyzer(comp, root=ri.root, path_style=xcfg["path_style"])
    scope = args.scope
    if scope is None and not args.module:
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


def _fmt_loc(file: str, line: int, column: int) -> str:
    if not file:
        return "<unknown>"
    return f"{file}:{int(line or 0)}:{int(column or 0)}"


def _print_signal_pretty(scope, name, matches, *, verbose=False):
    print(f"Symbol: {Color.bold(name)}")
    if scope:
        print(f"Scope:  {Color.cyan(scope)}")
    print("─" * 60)
    if not matches:
        print(Color.dim("(no matching symbols)"))
        return
    for match in matches:
        sym = match.symbol
        if verbose:
            print(f"\n  {Color.green(sym.hierarchical_path or sym.name)}  {Color.dim(sym.kind)}")
            if sym.type_str:
                print(f"    type: {Color.dim(sym.type_str)}")
        if match.definitions:
            print(f"\n{Color.yellow('Definitions')}:")
            for d in match.definitions:
                loc = _fmt_loc(d.file, d.line, d.column)
                decl = f"{d.kind} {d.name}"
                if d.direction:
                    decl += f" {d.direction}"
                if d.type_str:
                    decl += f" {d.type_str}"
                print(f"  {Color.cyan(loc):28s} {decl}")
        print(f"\n{Color.yellow('References')}:")
        if not match.references:
            print(f"  {Color.dim('(none found)')}")
        for r in match.references:
            loc = _fmt_loc(r.file, r.line, r.column)
            target = f" {r.instance}.{r.port}" if r.instance and r.port else ""
            print(f"  {Color.cyan(loc):28s} {r.access:9s} {r.kind:17s}{target}")
            if verbose:
                print(f"    scope: {r.scope_path or '<unknown>'}")
                if r.description:
                    print(f"    desc:  {r.description}")
    print()


def _print_module_pretty(result: ModuleXref, *, scope: str = "", verbose=False):
    print(f"Module: {Color.bold(result.name)}")
    if scope:
        print(f"Scope:  {Color.cyan(scope)}")
    print("─" * 60)
    print(f"\n{Color.yellow('Definitions')}:")
    if not result.definitions:
        print(f"  {Color.dim('(none found)')}")
    for d in result.definitions:
        print(f"  {Color.cyan(_fmt_loc(d.file, d.line, d.column))}")

    print(f"\n{Color.yellow('Instances')}:")
    if not result.references:
        print(f"  {Color.dim('(none found)')}")
    for r in result.references:
        print(f"  {Color.cyan(_fmt_loc(r.file, r.line, r.column)):28s} {r.instance_path}")
        if verbose:
            if r.parent_scope or r.parent_module:
                print(f"    parent: {r.parent_scope or '<top>'}"
                      f"{(' [' + r.parent_module + ']') if r.parent_module else ''}")
            if r.parameter_values:
                params = ", ".join(f"{k}={v}" for k, v in sorted(r.parameter_values.items()))
                print(f"    params: {params}")
    print()


def run(args, env):
    name = args.name_alt or args.name
    if args.module and name:
        return _die(env, "specify either --signal/--name or --module, not both", agent_json.ERR_INPUT_NOT_FOUND)
    if not name and not args.module:
        return _die(env, "specify --signal/-s NAME, --name NAME, or --module MODULE", agent_json.ERR_INPUT_NOT_FOUND)

    prepared = _prepare(args, env)
    if not isinstance(prepared, tuple):
        return prepared
    xa, scope = prepared

    if args.module:
        result = xa.xref_module(args.module, scope_path=scope or "")
        if result is None:
            return _die(env, f"scope '{scope}' not found", agent_json.ERR_SCOPE_NOT_FOUND)
        if not result.definitions and not result.references:
            return _die(env, f"module '{args.module}' not found", agent_json.ERR_SIGNAL_NOT_FOUND)
        data = result.to_dict()
        data["scope"] = scope or ""
        summary = dict(data["summary"])
        summary["mode"] = "xref"
        summary["target_kind"] = "module"
        if env is not None:
            return emit(env.ok(data, summary))
        _print_module_pretty(result, scope=scope or "", verbose=bool(args.verbose))
        return 0

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
        "target": {"kind": "signal", "name": name, "scope": scope},
        "scope": scope,
        "name": name,
        "recursive": bool(args.recursive),
        "matches": data_matches,
    }
    summary = {
        "mode": "xref",
        "target_kind": "signal",
        "symbols": len(data_matches),
        "definitions": sum(m["summary"]["definitions"] for m in data_matches),
        "references": total_refs,
        "reads": reads,
        "writes": writes,
        "port_connections": sum(m["summary"]["port_connections"] for m in data_matches),
    }
    if env is not None:
        return emit(env.ok(data, summary))
    _print_signal_pretty(scope, name, matches, verbose=bool(args.verbose))
    return 0

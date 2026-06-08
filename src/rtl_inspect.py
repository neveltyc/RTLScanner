#!/usr/bin/env python3
"""rtl_inspect — elaborated scope metadata report.

The command intentionally stays source/debug oriented: it exposes the
parameter and type information that pyslang already elaborated, without
trying to become a synthesis or formal-analysis backend.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import pyslang.ast as ast
except ImportError:
    print("Error: pyslang is required.  pip install pyslang", file=sys.stderr)
    sys.exit(1)

import agent_json
from agent_json import emit
from rtl_common import Color, build_compilation, safe_str
from rtl_config import build_filelist, load_config, resolve_inputs
from rtl_slang import resolve_scope, scope_visit, symbol_key


@dataclass
class ParameterInfo:
    """One elaborated parameter/localparam/type parameter."""
    name: str
    kind: str
    type_str: str = ""
    value: Optional[str] = None
    expression: str = ""
    hierarchical_path: str = ""
    lexical_path: str = ""
    is_overridden: Optional[bool] = None
    file: str = ""
    line: int = 0

    def to_dict(self):
        d = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type_str,
            "value": self.value,
            "expression": self.expression,
            "hierarchical_path": self.hierarchical_path,
            "lexical_path": self.lexical_path,
        }
        if self.is_overridden is not None:
            d["is_overridden"] = self.is_overridden
        if self.file:
            d["file"] = self.file
            d["line"] = self.line
        return d


@dataclass
class TypeInfo:
    """One elaborated typedef / type alias / enum / struct / union."""
    name: str
    kind: str
    type_str: str = ""
    canonical_type: str = ""
    bit_width: Optional[int] = None
    hierarchical_path: str = ""
    lexical_path: str = ""
    file: str = ""
    line: int = 0
    members: list[str] = field(default_factory=list)

    def to_dict(self):
        d = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type_str,
            "canonical_type": self.canonical_type,
            "bit_width": self.bit_width,
            "hierarchical_path": self.hierarchical_path,
            "lexical_path": self.lexical_path,
        }
        if self.members:
            d["members"] = list(self.members)
        if self.file:
            d["file"] = self.file
            d["line"] = self.line
        return d


_TYPE_KINDS = tuple(
    k for k in (
        getattr(ast.SymbolKind, "TypeAlias", None),
        getattr(ast.SymbolKind, "EnumType", None),
        getattr(ast.SymbolKind, "PackedStructType", None),
        getattr(ast.SymbolKind, "UnpackedStructType", None),
        getattr(ast.SymbolKind, "PackedUnionType", None),
        getattr(ast.SymbolKind, "UnpackedUnionType", None),
        getattr(ast.SymbolKind, "ForwardingTypedef", None),
    ) if k is not None
)


class ScopeInspector:
    """Extract parameter/type metadata from an elaborated scope."""

    def __init__(self, compilation):
        self._comp = compilation
        self._root = compilation.getRoot()
        self._sm = compilation.sourceManager
        self._cwd = Path.cwd().resolve()
        _ = compilation.getAllDiagnostics()

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

    def _expr_text(self, expr) -> str:
        if expr is None:
            return ""
        for _ in range(8):
            syn = getattr(expr, "syntax", None)
            if syn is not None:
                return safe_str(syn, "").strip()
            inner = getattr(expr, "operand", None) or getattr(expr, "left", None)
            if inner is None:
                break
            expr = inner
        return ""

    def _type_width(self, type_obj) -> Optional[int]:
        try:
            width = int(type_obj.bitWidth)
            return width if width > 0 else None
        except Exception:
            return None

    def _path(self, sym, attr: str) -> str:
        return safe_str(getattr(sym, attr, ""), "")

    def _param_kind(self, sym) -> str:
        if getattr(sym, "kind", None) == getattr(ast.SymbolKind, "TypeParameter", None):
            return "type_parameter"
        try:
            if sym.isLocalParam:
                return "localparam"
        except Exception:
            pass
        return "parameter"

    def _param_type(self, sym) -> str:
        if getattr(sym, "kind", None) == getattr(ast.SymbolKind, "TypeParameter", None):
            try:
                return safe_str(sym.targetType.type, "")
            except Exception:
                try:
                    return safe_str(sym.targetType, "")
                except Exception:
                    return ""
        return safe_str(getattr(sym, "type", ""), "")

    def _param_value(self, sym) -> Optional[str]:
        if getattr(sym, "kind", None) == getattr(ast.SymbolKind, "TypeParameter", None):
            return None
        try:
            return safe_str(sym.value, "")
        except Exception:
            return None

    def _make_param(self, sym) -> ParameterInfo:
        f, ln = self._loc(sym)
        overridden = None
        try:
            overridden = bool(sym.isOverridden)
        except Exception:
            pass
        return ParameterInfo(
            name=safe_str(getattr(sym, "name", ""), ""),
            kind=self._param_kind(sym),
            type_str=self._param_type(sym),
            value=self._param_value(sym),
            expression=self._expr_text(getattr(sym, "initializer", None)),
            hierarchical_path=self._path(sym, "hierarchicalPath"),
            lexical_path=self._path(sym, "lexicalPath"),
            is_overridden=overridden,
            file=f,
            line=ln,
        )

    def _members(self, sym) -> list[str]:
        out = []
        try:
            for m in sym.members:
                name = safe_str(getattr(m, "name", ""), "")
                if name:
                    out.append(name)
        except Exception:
            pass
        return out

    def _type_kind(self, sym) -> str:
        name = getattr(getattr(sym, "kind", None), "name", safe_str(getattr(sym, "kind", ""), ""))
        mapping = {
            "TypeAlias": "typedef",
            "EnumType": "enum",
            "PackedStructType": "struct",
            "UnpackedStructType": "struct",
            "PackedUnionType": "union",
            "UnpackedUnionType": "union",
            "ForwardingTypedef": "forward_typedef",
        }
        return mapping.get(name, name.lower() if name else "type")

    def _make_type(self, sym) -> TypeInfo:
        f, ln = self._loc(sym)
        canonical = ""
        try:
            canonical = safe_str(sym.canonicalType, "")
        except Exception:
            pass
        return TypeInfo(
            name=safe_str(getattr(sym, "name", ""), ""),
            kind=self._type_kind(sym),
            type_str=safe_str(sym, ""),
            canonical_type=canonical,
            bit_width=self._type_width(sym),
            hierarchical_path=self._path(sym, "hierarchicalPath"),
            lexical_path=self._path(sym, "lexicalPath"),
            file=f,
            line=ln,
            members=self._members(sym),
        )

    def _local_types(self, body) -> list[TypeInfo]:
        found = []
        seen = set()

        def collect(sym):
            key = symbol_key(sym)
            if key in seen:
                return
            seen.add(key)
            found.append(self._make_type(sym))

        scope_visit(body, {kind: collect for kind in _TYPE_KINDS})
        found = [t for t in found if t.name]
        found.sort(key=lambda t: (t.file, t.line, t.name))
        return found

    def inspect(self, scope_path: str, *, want_params=True, want_types=True):
        inst = resolve_scope(self._root, scope_path)
        if inst is None:
            return None
        body = getattr(inst, "body", None)
        if body is None:
            return None

        params = []
        if want_params:
            seen = set()
            try:
                for p in body.parameters or []:
                    key = symbol_key(p)
                    if key not in seen:
                        seen.add(key)
                        params.append(self._make_param(p))
            except Exception:
                pass
            params.sort(key=lambda p: (p.file, p.line, p.name))

        types = self._local_types(body) if want_types else []
        return {
            "mode": "inspect",
            "scope": scope_path,
            "module": safe_str(getattr(body, "name", ""), ""),
            "parameters": [p.to_dict() for p in params],
            "types": [t.to_dict() for t in types],
        }

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
    g = p.add_argument_group("inspect")
    g.add_argument("--scope", default=None, metavar="SCOPE",
                   help="Hierarchical scope; auto-detect when single top")
    g.add_argument("--params", action="store_true",
                   help="Show parameters, localparams, and type parameters")
    g.add_argument("--types", action="store_true",
                   help="Show typedef / enum / struct / union symbols")
    g.add_argument("--all", action="store_true",
                   help="Show all inspect sections (default)")


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

    inspector = ScopeInspector(comp)
    scope = args.scope
    if scope is None:
        tops = inspector.get_top_paths()
        if len(tops) == 1:
            scope = tops[0]
        elif tops:
            return _die(env, "multiple tops, specify --scope: " + ", ".join(tops),
                        agent_json.ERR_SCOPE_NOT_FOUND)
        else:
            return _die(env, "no top modules found", agent_json.ERR_NO_TOP)
    return inspector, scope


def _die(env, msg, code):
    if env is not None:
        return emit(env.fail(code, msg))
    print(f"Error: {msg}", file=sys.stderr)
    return 2


def _section_flags(args):
    if args.all or (not args.params and not args.types):
        return True, True
    return bool(args.params), bool(args.types)


def _print_pretty(data):
    print(f"Scope:  {Color.cyan(data['scope'])}  [{Color.yellow(data['module'])}]")
    print("─" * 60)
    if "parameters" in data:
        print(f"\n  {Color.green('PARAMETERS')} ({len(data['parameters'])})")
        if not data["parameters"]:
            print(f"    {Color.dim('(none)')}")
        for p in data["parameters"]:
            loc = f"  {Color.dim(p['file'] + ':' + str(p['line']))}" if p.get("file") else ""
            value = p.get("value")
            val = f" = {Color.yellow(value)}" if value not in (None, "") else ""
            t = f"  {Color.dim(p.get('type', ''))}" if p.get("type") else ""
            flag = ""
            if p.get("is_overridden"):
                flag = f"  {Color.magenta('[overridden]')}"
            print(f"    {Color.cyan(p['name'])}  {Color.dim(p['kind'])}{t}{val}{flag}{loc}")
    if "types" in data:
        print(f"\n  {Color.green('TYPES')} ({len(data['types'])})")
        if not data["types"]:
            print(f"    {Color.dim('(none)')}")
        for t in data["types"]:
            loc = f"  {Color.dim(t['file'] + ':' + str(t['line']))}" if t.get("file") else ""
            bw = f"  {Color.dim(str(t['bit_width']) + ' bits')}" if t.get("bit_width") else ""
            canon = t.get("canonical_type") or t.get("type") or ""
            print(f"    {Color.cyan(t['name'])}  {Color.dim(t['kind'])}  {canon}{bw}{loc}")
            if t.get("members"):
                print(f"      {Color.dim(', '.join(t['members']))}")
    print()


def run(args, env):
    prepared = _prepare(args, env)
    if not isinstance(prepared, tuple):
        return prepared
    inspector, scope = prepared
    want_params, want_types = _section_flags(args)
    data = inspector.inspect(scope, want_params=want_params, want_types=want_types)
    if data is None:
        return _die(env, f"scope '{scope}' not found", agent_json.ERR_SCOPE_NOT_FOUND)

    if not want_params:
        data.pop("parameters", None)
    if not want_types:
        data.pop("types", None)

    summary = {
        "mode": "inspect",
        "parameters": len(data.get("parameters", [])),
        "types": len(data.get("types", [])),
    }
    if env is not None:
        return emit(env.ok(data, summary))
    _print_pretty(data)
    return 0

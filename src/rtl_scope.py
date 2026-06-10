#!/usr/bin/env python3
"""rtl_scope -- direct contents of one elaborated scope."""

from __future__ import annotations

import argparse
import os
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
import rtl_cli
from agent_json import emit
from rtl_common import Color, safe_str
from rtl_slang import iter_instances, resolve_scope, scope_visit, symbol_key


_DIR_NAMES = {
    ast.ArgumentDirection.In: "input",
    ast.ArgumentDirection.Out: "output",
    ast.ArgumentDirection.InOut: "inout",
    ast.ArgumentDirection.Ref: "ref",
}


def _dir_name(direction) -> str:
    return _DIR_NAMES.get(direction, safe_str(direction, "").split(".")[-1].lower())


@dataclass
class PortInfo:
    name: str
    direction: str
    type_str: str = ""
    width: Optional[int] = None
    is_interface: bool = False
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "direction": self.direction,
            "type": self.type_str,
            "width": self.width,
        }
        if self.is_interface:
            out["is_interface"] = True
        if self.file:
            out["file"] = self.file
            out["line"] = self.line
        return out


@dataclass
class SignalInfo:
    name: str
    kind: str
    type_str: str = ""
    width: Optional[int] = None
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type_str,
            "width": self.width,
        }
        if self.file:
            out["file"] = self.file
            out["line"] = self.line
        return out


@dataclass
class InstanceInfo:
    instance: str
    module: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = {
            "instance": self.instance,
            "module": self.module,
            "path": self.path,
            "params": dict(self.params),
        }
        if self.file:
            out["file"] = self.file
            out["line"] = self.line
        return out


@dataclass
class ParameterInfo:
    name: str
    kind: str
    type_str: str = ""
    value: Optional[str] = None
    expression: str = ""
    bit_width: Optional[int] = None
    is_signed: Optional[bool] = None
    hierarchical_path: str = ""
    lexical_path: str = ""
    is_overridden: Optional[bool] = None
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type_str,
            "value": self.value,
            "expression": self.expression,
            "bit_width": self.bit_width,
            "is_signed": self.is_signed,
            "hierarchical_path": self.hierarchical_path,
            "lexical_path": self.lexical_path,
        }
        if self.is_overridden is not None:
            out["is_overridden"] = self.is_overridden
        if self.file:
            out["file"] = self.file
            out["line"] = self.line
        return out


@dataclass
class TypeDefInfo:
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
    member_details: list[dict[str, Any]] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type_str,
            "canonical_type": self.canonical_type,
            "bit_width": self.bit_width,
            "hierarchical_path": self.hierarchical_path,
            "lexical_path": self.lexical_path,
        }
        if self.members:
            out["members"] = list(self.members)
        if self.member_details:
            out["member_details"] = list(self.member_details)
        if self.fields:
            out["fields"] = list(self.fields)
        if self.file:
            out["file"] = self.file
            out["line"] = self.line
        return out


@dataclass
class ConnectionInfo:
    instance_path: str
    instance_name: str
    module_name: str
    port_name: str
    port_direction: str
    port_width: Optional[int]
    conn_text: str = ""
    conn_width: Optional[int] = None
    is_unconnected: bool = False
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance": self.instance_path,
            "module": self.module_name,
            "port": self.port_name,
            "direction": self.port_direction,
            "port_width": self.port_width,
            "connection": self.conn_text or None,
            "conn_width": self.conn_width,
            "unconnected": self.is_unconnected,
            "file": self.file,
            "line": self.line,
        }


@dataclass
class PortIssue:
    kind: str
    severity: str
    instance_path: str
    port_name: str
    port_direction: str
    message: str
    file: str = ""
    line: int = 0


_TYPE_KINDS = tuple(
    kind for kind in (
        getattr(ast.SymbolKind, "TypeAlias", None),
        getattr(ast.SymbolKind, "EnumType", None),
        getattr(ast.SymbolKind, "PackedStructType", None),
        getattr(ast.SymbolKind, "UnpackedStructType", None),
        getattr(ast.SymbolKind, "PackedUnionType", None),
        getattr(ast.SymbolKind, "UnpackedUnionType", None),
        getattr(ast.SymbolKind, "ForwardingTypedef", None),
    ) if kind is not None
)


class ScopeAnalyzer:
    """Extract direct scope contents from an elaborated compilation."""

    def __init__(self, compilation):
        self._comp = compilation
        self._root = compilation.getRoot()
        self._sm = compilation.sourceManager
        self._cwd = Path.cwd().resolve()
        # getRoot() above already elaborated the design; build_compilation also
        # elaborates, so there's no need to re-gather diagnostics here.

    def _rel(self, name: str) -> str:
        if not name:
            return name
        try:
            path = Path(name).resolve()
            rel = os.path.relpath(path, self._cwd)
            return rel if not rel.startswith(os.pardir + os.sep) else path.as_posix()
        except Exception:
            return name

    def _loc(self, sym_or_range) -> tuple[str, int]:
        try:
            loc = getattr(sym_or_range, "start", None) or sym_or_range.location
            return (
                self._rel(safe_str(self._sm.getFileName(loc))),
                int(self._sm.getLineNumber(loc)),
            )
        except Exception:
            return "", 0

    def _expr_text(self, expr) -> str:
        if expr is None:
            return ""
        for _ in range(8):
            syntax = getattr(expr, "syntax", None)
            if syntax is not None:
                return safe_str(syntax, "").strip()
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

    def _type_signed(self, type_obj) -> Optional[bool]:
        try:
            return bool(type_obj.isSigned)
        except Exception:
            return None

    def _path(self, sym, attr: str) -> str:
        return safe_str(getattr(sym, attr, ""), "")

    def _scope_body(self, scope_path: str):
        inst = resolve_scope(self._root, scope_path)
        if inst is None:
            return None, None
        body = getattr(inst, "body", None)
        if body is None:
            return None, None
        return inst, body

    def get_top_paths(self) -> list[str]:
        paths = []
        for top in self._root.topInstances:
            try:
                paths.append(top.hierarchicalPath)
            except Exception:
                continue
        return paths

    def _make_port(self, port_sym) -> PortInfo:
        try:
            direction = _dir_name(port_sym.direction)
        except Exception:
            direction = "input"
        try:
            type_obj = port_sym.type
        except Exception:
            type_obj = None
        file, line = self._loc(port_sym)
        return PortInfo(
            name=safe_str(getattr(port_sym, "name", ""), ""),
            direction=direction,
            type_str=safe_str(type_obj, "") if type_obj is not None else "",
            width=self._type_width(type_obj) if type_obj is not None else None,
            is_interface=getattr(port_sym, "kind", None) == ast.SymbolKind.InterfacePort,
            file=file,
            line=line,
        )

    def ports(self, body) -> list[PortInfo]:
        out = []
        try:
            for port in body.portList or []:
                out.append(self._make_port(port))
        except Exception:
            pass
        return out

    def _port_internal_keys(self, body) -> set[str]:
        keys = set()
        try:
            ports = body.portList or []
        except Exception:
            return keys
        for port in ports:
            for attr in ("internalSymbol", "internalSym"):
                try:
                    sym = getattr(port, attr)
                except Exception:
                    sym = None
                if sym is not None:
                    keys.add(symbol_key(sym))
            try:
                found = body.find(port.name)
            except Exception:
                found = None
            if found is not None and getattr(found, "kind", None) in (
                    ast.SymbolKind.Net, ast.SymbolKind.Variable):
                keys.add(symbol_key(found))
        return keys

    def signals(self, body) -> list[SignalInfo]:
        out = []
        seen = set()
        port_keys = self._port_internal_keys(body)

        def collect(sym):
            key = symbol_key(sym)
            if key in seen or key in port_keys:
                return ast.VisitAction.Skip
            seen.add(key)
            try:
                type_obj = sym.type
            except Exception:
                type_obj = None
            file, line = self._loc(sym)
            out.append(SignalInfo(
                name=safe_str(getattr(sym, "name", ""), ""),
                kind=safe_str(getattr(getattr(sym, "kind", None), "name", ""), ""),
                type_str=safe_str(type_obj, "") if type_obj is not None else "",
                width=self._type_width(type_obj) if type_obj is not None else None,
                file=file,
                line=line,
            ))
            return ast.VisitAction.Skip

        scope_visit(body, {
            ast.SymbolKind.Net: collect,
            ast.SymbolKind.Variable: collect,
        })
        out = [sig for sig in out if sig.name]
        out.sort(key=lambda sig: (sig.file, sig.line, sig.name))
        return out

    def _make_instance(self, inst) -> InstanceInfo:
        try:
            body = inst.body
        except Exception:
            body = None
        params = {}
        try:
            for param in body.parameters or []:
                name = safe_str(getattr(param, "name", ""), "")
                if not name:
                    continue
                try:
                    value = safe_str(param.value, "")
                except Exception:
                    value = self._param_type(param)
                params[name] = value
        except Exception:
            pass
        file, line = self._loc(inst)
        return InstanceInfo(
            instance=safe_str(getattr(inst, "name", ""), ""),
            module=safe_str(getattr(body, "name", ""), "") if body is not None else "",
            path=safe_str(getattr(inst, "hierarchicalPath", ""), ""),
            params=params,
            file=file,
            line=line,
        )

    def direct_instances(self, body) -> list:
        out = []
        seen = set()

        def collect(inst):
            key = symbol_key(inst)
            if key not in seen:
                seen.add(key)
                out.append(inst)
            return ast.VisitAction.Skip

        try:
            body.visit(lookup_table={ast.SymbolKind.Instance: collect})
        except Exception:
            pass
        out.sort(key=lambda inst: safe_str(getattr(inst, "hierarchicalPath", ""), ""))
        return out

    def instances(self, body) -> list[InstanceInfo]:
        return [self._make_instance(inst) for inst in self.direct_instances(body)]

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
                return safe_str(getattr(sym, "targetType", ""), "")
        return safe_str(getattr(sym, "type", ""), "")

    def _param_type_obj(self, sym):
        if getattr(sym, "kind", None) == getattr(ast.SymbolKind, "TypeParameter", None):
            try:
                return sym.targetType.type
            except Exception:
                return None
        try:
            return sym.type
        except Exception:
            return None

    def _param_value(self, sym) -> Optional[str]:
        if getattr(sym, "kind", None) == getattr(ast.SymbolKind, "TypeParameter", None):
            return self._param_type(sym)
        try:
            return safe_str(sym.value, "")
        except Exception:
            return None

    def _make_param(self, sym) -> ParameterInfo:
        file, line = self._loc(sym)
        overridden = None
        try:
            overridden = bool(sym.isOverridden)
        except Exception:
            pass
        type_obj = self._param_type_obj(sym)
        return ParameterInfo(
            name=safe_str(getattr(sym, "name", ""), ""),
            kind=self._param_kind(sym),
            type_str=self._param_type(sym),
            value=self._param_value(sym),
            expression=self._expr_text(getattr(sym, "initializer", None)),
            bit_width=self._type_width(type_obj) if type_obj is not None else None,
            is_signed=self._type_signed(type_obj) if type_obj is not None else None,
            hierarchical_path=self._path(sym, "hierarchicalPath"),
            lexical_path=self._path(sym, "lexicalPath"),
            is_overridden=overridden,
            file=file,
            line=line,
        )

    def params(self, body) -> list[ParameterInfo]:
        out = []
        seen = set()
        try:
            params = body.parameters or []
        except Exception:
            params = []
        for param in params:
            key = symbol_key(param)
            if key in seen:
                continue
            seen.add(key)
            out.append(self._make_param(param))
        out.sort(key=lambda param: (param.file, param.line, param.name))
        return out

    def _target_type(self, sym):
        for attr in ("canonicalType", "targetType", "declaredType"):
            try:
                obj = getattr(sym, attr)
            except Exception:
                continue
            try:
                return obj.type
            except Exception:
                if obj is not None:
                    return obj
        return sym

    def _member_details(self, sym) -> list[dict[str, Any]]:
        target = self._target_type(sym)
        enum_value_kind = getattr(ast.SymbolKind, "EnumValue", None)
        out = []

        def collect(member):
            if enum_value_kind is not None and getattr(member, "kind", None) != enum_value_kind:
                return
            name = safe_str(getattr(member, "name", ""), "")
            if not name:
                return
            item: dict[str, Any] = {"name": name}
            try:
                item["value"] = safe_str(member.value, "")
            except Exception:
                pass
            expr = self._expr_text(getattr(member, "initializer", None))
            if expr:
                item["expression"] = expr
            file, line = self._loc(member)
            if file:
                item["file"] = file
                item["line"] = line
            out.append(item)

        try:
            target.visit(collect)
        except Exception:
            pass
        return out

    def _fields(self, sym) -> list[dict[str, Any]]:
        target = self._target_type(sym)
        field_kind = getattr(ast.SymbolKind, "Field", None)
        out = []

        def collect(field):
            if field_kind is not None and getattr(field, "kind", None) != field_kind:
                return
            name = safe_str(getattr(field, "name", ""), "")
            if not name:
                return
            try:
                type_obj = field.type
            except Exception:
                type_obj = None
            item: dict[str, Any] = {
                "name": name,
                "type": safe_str(type_obj, "") if type_obj is not None else "",
                "bit_width": self._type_width(type_obj) if type_obj is not None else None,
            }
            try:
                item["bit_offset"] = int(field.bitOffset)
            except Exception:
                pass
            try:
                item["index"] = int(field.fieldIndex)
            except Exception:
                pass
            file, line = self._loc(field)
            if file:
                item["file"] = file
                item["line"] = line
            out.append(item)

        try:
            target.visit(collect)
        except Exception:
            pass
        out.sort(key=lambda item: item.get("index", 0))
        return out

    def _typedef_kind(self, sym) -> str:
        target = self._target_type(sym)
        try:
            if bool(target.isEnum):
                return "enum"
        except Exception:
            pass
        try:
            if bool(target.isStruct):
                return "struct"
        except Exception:
            pass
        try:
            if bool(target.isPackedUnion) or bool(target.isUnpackedUnion):
                return "union"
        except Exception:
            pass
        name = safe_str(getattr(getattr(sym, "kind", None), "name", ""), "")
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

    def _make_typedef(self, sym) -> TypeDefInfo:
        file, line = self._loc(sym)
        target = self._target_type(sym)
        member_details = self._member_details(sym)
        try:
            canonical = safe_str(sym.canonicalType, "")
        except Exception:
            canonical = ""
        return TypeDefInfo(
            name=safe_str(getattr(sym, "name", ""), ""),
            kind=self._typedef_kind(sym),
            type_str=safe_str(sym, ""),
            canonical_type=canonical,
            bit_width=self._type_width(target),
            hierarchical_path=self._path(sym, "hierarchicalPath"),
            lexical_path=self._path(sym, "lexicalPath"),
            file=file,
            line=line,
            members=[item.get("name", "") for item in member_details if item.get("name")],
            member_details=member_details,
            fields=self._fields(sym),
        )

    def typedefs(self, body) -> list[TypeDefInfo]:
        out = []
        seen = set()

        def collect(sym):
            key = symbol_key(sym)
            if key in seen:
                return
            seen.add(key)
            out.append(self._make_typedef(sym))

        scope_visit(body, {kind: collect for kind in _TYPE_KINDS})
        out = [item for item in out if item.name]
        out.sort(key=lambda item: (item.file, item.line, item.name))
        return out

    def _unwrap_for_user_expr(self, expr):
        for _ in range(8):
            if expr is None:
                return expr
            operand = getattr(expr, "operand", None)
            if operand is not None and hasattr(expr, "conversionKind"):
                expr = operand
                continue
            break
        return expr

    def _conn_target(self, expr, direction):
        if expr is None:
            return None
        if direction == ast.ArgumentDirection.Out and hasattr(expr, "left"):
            return expr.left
        return expr

    def _render_expr(self, expr) -> str:
        if expr is None:
            return ""
        cur = expr
        for _ in range(8):
            syntax = getattr(cur, "syntax", None)
            if syntax is not None:
                return safe_str(syntax, "").strip()
            inner = getattr(cur, "operand", None) or getattr(cur, "left", None)
            if inner is None:
                break
            cur = inner
        try:
            source_range = expr.sourceRange
            buf = self._sm.getSourceText(source_range.start.buffer)
            line = self._sm.getLineNumber(source_range.start)
            col_a = self._sm.getColumnNumber(source_range.start)
            col_b = self._sm.getColumnNumber(source_range.end)
            lines = buf.splitlines()
            if 0 < line <= len(lines):
                return lines[line - 1][col_a - 1:col_b - 1].strip()
        except Exception:
            pass
        return f"<{type(expr).__name__}>"

    def _make_connection(self, inst, port_connection) -> ConnectionInfo:
        port = port_connection.port
        try:
            body = inst.body
            module_name = safe_str(body.name, "?")
            instance_path = safe_str(inst.hierarchicalPath, inst.name)
        except Exception:
            module_name = "?"
            instance_path = safe_str(getattr(inst, "name", ""), "?")
        try:
            port_direction = _dir_name(port.direction)
        except Exception:
            port_direction = "input"
        try:
            port_type = port.type
        except Exception:
            port_type = None
        port_width = self._type_width(port_type) if port_type is not None else None
        file, line = self._loc(inst)

        if port_connection.expression is None:
            return ConnectionInfo(
                instance_path=instance_path,
                instance_name=safe_str(getattr(inst, "name", ""), "?"),
                module_name=module_name,
                port_name=safe_str(getattr(port, "name", ""), "?"),
                port_direction=port_direction,
                port_width=port_width,
                is_unconnected=True,
                file=file,
                line=line,
            )

        target = self._unwrap_for_user_expr(
            self._conn_target(port_connection.expression, port.direction)
        )
        try:
            target_type = target.type
        except Exception:
            target_type = None
        return ConnectionInfo(
            instance_path=instance_path,
            instance_name=safe_str(getattr(inst, "name", ""), "?"),
            module_name=module_name,
            port_name=safe_str(getattr(port, "name", ""), "?"),
            port_direction=port_direction,
            port_width=port_width,
            conn_text=self._render_expr(port_connection.expression),
            conn_width=self._type_width(target_type) if target_type is not None else None,
            file=file,
            line=line,
        )

    def _connections_for_instances(self, instances) -> list[ConnectionInfo]:
        out = []
        for inst in instances:
            try:
                port_connections = inst.portConnections
            except Exception:
                continue
            for port_connection in port_connections:
                try:
                    out.append(self._make_connection(inst, port_connection))
                except Exception:
                    continue
        out.sort(key=lambda conn: (conn.instance_path, conn.port_name))
        return out

    def connections(self, body) -> list[ConnectionInfo]:
        return self._connections_for_instances(self.direct_instances(body))

    def all_connections(self) -> list[ConnectionInfo]:
        return self._connections_for_instances(iter_instances(self._root))

    def connection_issues(self, body=None) -> list[PortIssue]:
        connections = self.connections(body) if body is not None else self.all_connections()
        out = []
        for conn in connections:
            if conn.is_unconnected:
                if conn.port_direction == "input":
                    severity = "warning"
                    message = f"input port '{conn.port_name}' is unconnected"
                else:
                    severity = "note"
                    message = f"{conn.port_direction} port '{conn.port_name}' is unconnected"
                out.append(PortIssue(
                    kind="unconnected",
                    severity=severity,
                    instance_path=conn.instance_path,
                    port_name=conn.port_name,
                    port_direction=conn.port_direction,
                    message=message,
                    file=conn.file,
                    line=conn.line,
                ))
            elif conn.port_width and conn.conn_width and conn.port_width != conn.conn_width:
                out.append(PortIssue(
                    kind="width_mismatch",
                    severity="warning",
                    instance_path=conn.instance_path,
                    port_name=conn.port_name,
                    port_direction=conn.port_direction,
                    message=(f"width mismatch on .{conn.port_name}: "
                             f"port is {conn.port_width} bits, "
                             f"connection is {conn.conn_width} bits"),
                    file=conn.file,
                    line=conn.line,
                ))
        out.sort(key=lambda issue: (issue.file, issue.line, issue.instance_path, issue.port_name))
        return out

    def describe(self, scope_path: str, sections: set[str]) -> Optional[dict[str, Any]]:
        _inst, body = self._scope_body(scope_path)
        if body is None:
            return None
        data: dict[str, Any] = {
            "mode": "scope",
            "scope": scope_path,
            "module": safe_str(getattr(body, "name", ""), ""),
        }
        if "ports" in sections:
            data["ports"] = [item.to_dict() for item in self.ports(body)]
        if "signals" in sections:
            data["signals"] = [item.to_dict() for item in self.signals(body)]
        if "instances" in sections:
            data["instances"] = [item.to_dict() for item in self.instances(body)]
        if "params" in sections:
            data["params"] = [item.to_dict() for item in self.params(body)]
        if "typedefs" in sections:
            data["typedefs"] = [item.to_dict() for item in self.typedefs(body)]
        if "connections" in sections:
            data["connections"] = [item.to_dict() for item in self.connections(body)]
        return data


_SECTION_NAMES = ("ports", "signals", "instances", "params", "typedefs", "connections")
_DEFAULT_SECTIONS = {"ports", "signals", "instances", "params"}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("scope")
    group.add_argument("--scope", default=None, metavar="SCOPE",
                       help="Hierarchical scope; auto-detect when single top")
    group.add_argument("--ports", action="store_true",
                       help="Show ports declared on this scope")
    group.add_argument("--signals", action="store_true",
                       help="Show local non-port nets and variables")
    group.add_argument("--instances", action="store_true",
                       help="Show direct child instances")
    group.add_argument("--params", action="store_true",
                       help="Show elaborated parameters and localparams")
    group.add_argument("--typedefs", action="store_true",
                       help="Show local typedef, enum, struct, and union definitions")
    group.add_argument("--connections", action="store_true",
                       help="Show direct child instance port connections")
    group.add_argument("--all", action="store_true",
                       help="Show all scope sections")


def _sections(args) -> set[str]:
    if args.all:
        return set(_SECTION_NAMES)
    selected = {name for name in _SECTION_NAMES if getattr(args, name, False)}
    return selected or set(_DEFAULT_SECTIONS)


def _prepare(args):
    prepared = rtl_cli.prepare_compilation(args)
    analyzer = ScopeAnalyzer(prepared.comp)
    scope = rtl_cli.resolve_scope(args.scope, analyzer.get_top_paths())
    return analyzer, scope


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    out = {"mode": "scope"}
    for name in _SECTION_NAMES:
        if name in data:
            out[name] = len(data[name])
    return out


def _print_section(title: str, rows: list[dict[str, Any]], line_fn) -> None:
    print(f"\n  {Color.green(title)} ({len(rows)})")
    if not rows:
        print(f"    {Color.dim('(none)')}")
        return
    for row in rows:
        print(line_fn(row))


def _print_pretty(data: dict[str, Any]) -> None:
    print(f"Scope:  {Color.cyan(data['scope'])}  [{Color.yellow(data['module'])}]")
    print("-" * 60)
    if "ports" in data:
        _print_section(
            "PORTS",
            data["ports"],
            lambda p: f"    {Color.cyan(p['name']):20s} {p['direction']:6s} "
                      f"{Color.dim(p.get('type', ''))}",
        )
    if "signals" in data:
        _print_section(
            "SIGNALS",
            data["signals"],
            lambda s: f"    {Color.cyan(s['name']):20s} {s['kind']:10s} "
                      f"{Color.dim(s.get('type', ''))}",
        )
    if "instances" in data:
        _print_section(
            "INSTANCES",
            data["instances"],
            lambda i: f"    {Color.cyan(i['instance']):20s} {Color.yellow(i['module'])} "
                      f"{Color.dim(i['path'])}",
        )
    if "params" in data:
        _print_section(
            "PARAMS",
            data["params"],
            lambda p: f"    {Color.cyan(p['name']):20s} {p['kind']:14s} "
                      f"{Color.yellow(str(p.get('value', '')))}",
        )
    if "typedefs" in data:
        _print_section(
            "TYPEDEFS",
            data["typedefs"],
            lambda t: f"    {Color.cyan(t['name']):20s} {t['kind']:8s} "
                      f"{Color.dim(t.get('canonical_type') or t.get('type', ''))}",
        )
    if "connections" in data:
        def fmt(conn):
            rhs = "(unconnected)" if conn.get("unconnected") else conn.get("connection")
            return (f"    {Color.cyan(conn['instance'])}.{conn['port']} "
                    f"{conn['direction']:6s} -> {rhs}")
        _print_section("CONNECTIONS", data["connections"], fmt)
    print()


def run(args, env):
    analyzer, scope = _prepare(args)
    data = analyzer.describe(scope, _sections(args))
    if data is None:
        raise rtl_cli.CliError(
            agent_json.ERR_SCOPE_NOT_FOUND,
            f"scope '{scope}' not found",
        )
    if env is not None:
        return emit(env.ok(data, _summary(data)))
    _print_pretty(data)
    return 0

"""Agent-friendly JSON envelope + schemas for the four RTLScanner CLIs.

All four tools (`rtl-tree`, `signal-trace`, `rtl-lint`, `rtl-ports`) share
the same top-level envelope when invoked with --json::

    {
      "tool":        "rtl-tree",
      "version":     "0.1.0",
      "status":      "ok" | "error",
      "command":     { <argparse Namespace echo, output flags filtered out> },
      "data":        { <tool-specific payload, see TOOL_SCHEMAS> } | null,
      "diagnostics": [ {severity, file, line, col, message}, ... ],
      "errors":      [ {code, message}, ... ],
      "summary":     { <tool-specific counts> } | null
    }

A status="error" envelope is still printed to stdout (with non-zero exit
code), so agents get structured failure info instead of a stderr stack trace.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

# Keep in sync with pyproject.toml [project].version.
TOOL_VERSION = "0.1.0"

# ── Error codes (closed enum) ───────────────────────────────────────
ERR_INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
ERR_BAD_FILELIST    = "BAD_FILELIST"
ERR_COMPILE_FAILED  = "COMPILE_FAILED"
ERR_SCOPE_NOT_FOUND = "SCOPE_NOT_FOUND"
ERR_SIGNAL_NOT_FOUND= "SIGNAL_NOT_FOUND"
ERR_BAD_CONFIG      = "BAD_CONFIG"
ERR_NO_TOP          = "NO_TOP"
ERR_INTERNAL        = "INTERNAL_ERROR"

ERROR_CODES = [
    ERR_INPUT_NOT_FOUND, ERR_BAD_FILELIST, ERR_COMPILE_FAILED,
    ERR_SCOPE_NOT_FOUND, ERR_SIGNAL_NOT_FOUND, ERR_BAD_CONFIG,
    ERR_NO_TOP, ERR_INTERNAL,
]


class AgentError(Exception):
    """Raise inside a tool's JSON path to produce a structured error envelope."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Envelope:
    """Incrementally build the shared JSON envelope.

    Typical use in a CLI's --json path::

        env = Envelope("rtl-tree", filter_command(args, {"json","schema","no_color"}))
        try:
            ... build data ...
            for d in diagnostics: env.add_diagnostic(...)
            print(dump(env.ok(data, summary)))
        except AgentError as e:
            print(dump(env.fail(e.code, e.message))); sys.exit(1)
    """

    def __init__(self, tool: str, command: Dict[str, Any]):
        self.tool = tool
        self.command = command
        self._diagnostics: List[Dict[str, Any]] = []
        self._errors: List[Dict[str, str]] = []

    # ── builders ──
    def add_diagnostic(self, severity: str, file: str = "", line: int = 0,
                       col: int = 0, message: str = "") -> None:
        self._diagnostics.append(dict(
            severity=severity, file=file or "",
            line=int(line or 0), col=int(col or 0),
            message=message or "",
        ))

    def add_error(self, code: str, message: str) -> None:
        self._errors.append(dict(code=code, message=message))

    # ── finalizers ──
    def ok(self, data: Any, summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._envelope("ok", data=data, summary=summary)

    def fail(self, code: str, message: str) -> Dict[str, Any]:
        self.add_error(code, message)
        return self._envelope("error", data=None, summary=None)

    def _envelope(self, status: str, *, data: Any, summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "version": TOOL_VERSION,
            "status": status,
            "command": self.command,
            "data": data,
            "diagnostics": list(self._diagnostics),
            "errors": list(self._errors),
            "summary": summary,
        }


# ── Helpers ─────────────────────────────────────────────────────────

# argparse fields that describe *output formatting* rather than what was
# analyzed — these are filtered out of the command echo so the agent sees
# only the semantic intent of the run.
_OUTPUT_FIELDS = frozenset({
    "json", "schema", "no_color", "markdown", "ndjson",
    "diag", "summary", "no_summary", "show_waived",
})


def filter_command(ns, extra_exclude: Optional[set] = None) -> Dict[str, Any]:
    """Convert an argparse Namespace to a JSON-safe dict, dropping output flags.

    All non-output fields are kept (even when at their default value) so the
    envelope is self-describing — an agent never has to guess whether a flag
    was passed.
    """
    exclude = set(_OUTPUT_FIELDS)
    if extra_exclude:
        exclude |= set(extra_exclude)
    out: Dict[str, Any] = {}
    for k, v in vars(ns).items():
        if k in exclude:
            continue
        out[k] = _jsonify(v)
    return out


def _jsonify(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonify(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonify(x) for k, x in v.items()}
    return str(v)  # Path, etc.


def dump(envelope: Dict[str, Any], *, pretty: bool = True) -> str:
    if pretty:
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def emit(envelope: Dict[str, Any], *, pretty: bool = True) -> int:
    """Print envelope to stdout and return the appropriate exit code (0/1)."""
    print(dump(envelope, pretty=pretty))
    return 0 if envelope.get("status") == "ok" else 1


# ── JSON Schemas (draft-07, hand-written, stdlib-only) ──────────────

_DIAG_ITEM = {
    "type": "object",
    "required": ["severity", "file", "line", "col", "message"],
    "properties": {
        "severity": {"type": "string", "enum": ["error", "warning", "note", "info"]},
        "file":     {"type": "string"},
        "line":     {"type": "integer", "minimum": 0},
        "col":      {"type": "integer", "minimum": 0},
        "message":  {"type": "string"},
    },
    "additionalProperties": False,
}

_ERROR_ITEM = {
    "type": "object",
    "required": ["code", "message"],
    "properties": {
        "code":    {"type": "string", "enum": ERROR_CODES},
        "message": {"type": "string"},
    },
    "additionalProperties": False,
}


def _envelope_schema(tool: str, data_schema: Dict[str, Any],
                     summary_schema: Dict[str, Any],
                     description: str = "") -> Dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": f"{tool} agent-mode output envelope",
        "description": description or
            f"Stable agent-mode JSON envelope produced by `{tool} --json`. "
            "All envelopes share the same top-level shape across the four "
            "RTLScanner tools.",
        "type": "object",
        "required": ["tool", "version", "status", "command",
                     "data", "diagnostics", "errors", "summary"],
        "properties": {
            "tool":        {"type": "string", "const": tool},
            "version":     {"type": "string"},
            "status":      {"type": "string", "enum": ["ok", "error"]},
            "command":     {"type": "object",
                            "description": "Echo of the parsed argparse "
                            "Namespace minus output flags."},
            "data":        {"oneOf": [data_schema, {"type": "null"}]},
            "diagnostics": {"type": "array", "items": _DIAG_ITEM},
            "errors":      {"type": "array", "items": _ERROR_ITEM},
            "summary":     {"oneOf": [summary_schema, {"type": "null"}]},
        },
        "additionalProperties": False,
    }


# ── rtl-tree ──
_TREE_NODE = {
    "type": "object",
    "required": ["instance", "module", "path"],
    "properties": {
        "instance":        {"type": "string"},
        "module":          {"type": "string"},
        "path":            {"type": "string"},
        "parameters":      {"type": "object",
                            "additionalProperties": {"type": "string"}},
        "is_interface":    {"type": "boolean"},
        "generated_scope": {"type": "string"},
        "children":        {"type": "array", "items": {"$ref": "#/$defs/node"}},
    },
    "additionalProperties": True,
}

_TREE_SCHEMA = _envelope_schema(
    "rtl-tree",
    data_schema={
        "type": "object",
        "required": ["hierarchy", "filelist"],
        "properties": {
            "hierarchy": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            "filelist": {
                "type": "object",
                "required": ["sources", "include_dirs", "defines"],
                "properties": {
                    "sources":      {"type": "array", "items": {"type": "string"}},
                    "include_dirs": {"type": "array", "items": {"type": "string"}},
                    "defines":      {"type": "object"},
                },
            },
        },
    },
    summary_schema={
        "type": "object",
        "required": ["instances", "unique_modules", "max_depth",
                     "files_parsed", "module_counts"],
        "properties": {
            "instances":      {"type": "integer"},
            "unique_modules": {"type": "integer"},
            "max_depth":      {"type": "integer"},
            "files_parsed":   {"type": "integer"},
            "module_counts":  {"type": "object",
                               "additionalProperties": {"type": "integer"}},
        },
    },
)
_TREE_SCHEMA["$defs"] = {"node": _TREE_NODE}


# ── signal-trace ──
_TRACE_DRIVER = {
    "type": "object",
    "properties": {
        "kind":        {"type": "string"},
        "source":      {"type": "string"},
        "description": {"type": "string"},
        "symbol":      {"type": "string"},
        "symbol_kind": {"type": "string"},
        "scope_path":  {"type": "string"},
        "file":        {"type": "string"},
        "line":        {"type": "integer"},
    },
    "additionalProperties": True,
}
_TRACE_LOAD = {
    "type": "object",
    "properties": {
        "kind":        {"type": "string"},
        "description": {"type": "string"},
        "scope_path":  {"type": "string"},
        "instance":    {"type": "string"},
        "port":        {"type": "string"},
        "direction":   {"type": "string"},
        "file":        {"type": "string"},
        "line":        {"type": "integer"},
    },
    "additionalProperties": True,
}
_TRACE_RESULT = {
    "type": "object",
    "required": ["signal", "scope"],
    "properties": {
        "signal":               {"type": "string"},
        "type":                 {"type": "string"},
        "kind":                 {"type": "string"},
        "scope":                {"type": "string"},
        "module":               {"type": "string"},
        "driver":               {"oneOf": [_TRACE_DRIVER, {"type": "null"}]},
        "extra_drivers":        {"type": "array", "items": _TRACE_DRIVER},
        "multi_driver_warning": {"type": "boolean"},
        "loads":                {"type": "array", "items": _TRACE_LOAD},
        "load_count":           {"type": "integer"},
        "cross_hierarchy":      {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}
_TRACE_SIGNAL_LIST_ITEM = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "kind": {"type": "string"},
    },
    "additionalProperties": True,
}

_TRACE_SCHEMA = _envelope_schema(
    "signal-trace",
    data_schema={
        "type": "object",
        "required": ["mode", "scope"],
        "properties": {
            "mode":    {"type": "string", "enum": ["signal", "all", "list"]},
            "scope":   {"type": "string"},
            "results": {"type": "array", "items": _TRACE_RESULT,
                        "description": "Populated for mode=signal|all."},
            "signals": {"type": "array", "items": _TRACE_SIGNAL_LIST_ITEM,
                        "description": "Populated for mode=list."},
        },
    },
    summary_schema={
        "type": "object",
        "required": ["mode", "results"],
        "properties": {
            "mode":    {"type": "string"},
            "results": {"type": "integer"},
            "drivers": {"type": "integer"},
            "loads":   {"type": "integer"},
            "signals": {"type": "integer"},
        },
    },
)


# ── rtl-lint ──
_LINT_FINDING = {
    "type": "object",
    "required": ["file", "line", "col", "severity", "rule", "message", "check"],
    "properties": {
        "file":     {"type": "string"},
        "line":     {"type": "integer"},
        "col":      {"type": "integer"},
        "severity": {"type": "string", "enum": ["error", "warning", "note", "ignored"]},
        "rule":     {"type": "string",
                     "description": "Open enum. Known rules include "
                     "`cdc-crossing`, `width-trunc`, `unused-port`, "
                     "`case-default`, `latch`, `multi-driven`, `used-before-declared`."},
        "message":  {"type": "string"},
        "check":    {"type": "string",
                     "enum": ["semantic", "unused", "shadow", "cdc"]},
        "waived_reason": {"type": "string"},
    },
    "additionalProperties": True,
}

_LINT_SCHEMA = _envelope_schema(
    "rtl-lint",
    data_schema={
        "type": "object",
        "required": ["findings", "waived", "config_path"],
        "properties": {
            "findings":    {"type": "array", "items": _LINT_FINDING},
            "waived":      {"type": "array", "items": _LINT_FINDING},
            "config_path": {"type": ["string", "null"]},
        },
    },
    summary_schema={
        "type": "object",
        "required": ["total", "by_severity", "by_rule", "by_check",
                     "waived", "files_linted", "has_error"],
        "properties": {
            "total":        {"type": "integer"},
            "by_severity":  {"type": "object",
                             "additionalProperties": {"type": "integer"}},
            "by_rule":      {"type": "object",
                             "additionalProperties": {"type": "integer"}},
            "by_check":     {"type": "object",
                             "additionalProperties": {"type": "integer"}},
            "waived":       {"type": "integer"},
            "files_linted": {"type": "integer"},
            "has_error":    {"type": "boolean"},
        },
    },
    description=("Stable agent-mode JSON envelope produced by `rtl-lint --json`. "
                 "CDC findings appear as regular entries in `data.findings` with "
                 "`rule=\"cdc-crossing\"` and `check=\"cdc\"`. Note: SystemVerilog "
                 "`` `pragma diagnostic ignore `` does NOT suppress `cdc-crossing` "
                 "— use `[[waive]]` in `.rtllint.toml` instead."),
)


# ── rtl-ports ──
_PORT_INFO = {
    "type": "object",
    "required": ["name", "direction", "type", "width"],
    "properties": {
        "name":         {"type": "string"},
        "direction":    {"type": "string",
                         "enum": ["input", "output", "inout", "ref"]},
        "type":         {"type": "string"},
        "width":        {"type": ["integer", "null"]},
        "is_interface": {"type": "boolean"},
        "file":         {"type": "string"},
        "line":         {"type": "integer"},
    },
    "additionalProperties": True,
}
_PORT_MODULE = {
    "type": "object",
    "required": ["module", "kind", "ports"],
    "properties": {
        "module":         {"type": "string"},
        "kind":           {"type": "string"},
        "parameters":     {"type": "object"},
        "instance_count": {"type": "integer"},
        "ports":          {"type": "array", "items": _PORT_INFO},
        "file":           {"type": "string"},
        "line":           {"type": "integer"},
    },
    "additionalProperties": True,
}
_PORT_CONNECTION = {
    "type": "object",
    "required": ["instance", "module", "port", "direction"],
    "properties": {
        "instance":    {"type": "string"},
        "module":      {"type": "string"},
        "port":        {"type": "string"},
        "direction":   {"type": "string"},
        "port_width":  {"type": ["integer", "null"]},
        "connection":  {"type": ["string", "null"]},
        "conn_width":  {"type": ["integer", "null"]},
        "unconnected": {"type": "boolean"},
        "file":        {"type": "string"},
        "line":        {"type": "integer"},
    },
    "additionalProperties": True,
}
_PORT_ISSUE = {
    "type": "object",
    "required": ["kind", "severity", "instance", "port", "message"],
    "properties": {
        "kind":      {"type": "string"},
        "severity":  {"type": "string", "enum": ["warning", "note", "error"]},
        "instance":  {"type": "string"},
        "port":      {"type": "string"},
        "direction": {"type": "string"},
        "message":   {"type": "string"},
        "file":      {"type": "string"},
        "line":      {"type": "integer"},
    },
    "additionalProperties": True,
}

_PORTS_SCHEMA = _envelope_schema(
    "rtl-ports",
    data_schema={
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode":        {"type": "string",
                            "enum": ["modules", "instances", "check"]},
            "modules":     {"type": "array", "items": _PORT_MODULE,
                            "description": "Populated for mode=modules."},
            "connections": {"type": "array", "items": _PORT_CONNECTION,
                            "description": "Populated for mode=instances."},
            "issues":      {"type": "array", "items": _PORT_ISSUE,
                            "description": "Populated for mode=check."},
        },
    },
    summary_schema={
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode":        {"type": "string"},
            "modules":     {"type": "integer"},
            "connections": {"type": "integer"},
            "issues":      {"type": "integer"},
            "by_severity": {"type": "object",
                            "additionalProperties": {"type": "integer"}},
        },
    },
)


TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "rtl-tree":     _TREE_SCHEMA,
    "signal-trace": _TRACE_SCHEMA,
    "rtl-lint":     _LINT_SCHEMA,
    "rtl-ports":    _PORTS_SCHEMA,
}


def print_schema(tool: str) -> int:
    """Dump the JSON Schema for a tool's --json output to stdout."""
    schema = TOOL_SCHEMAS.get(tool)
    if schema is None:
        print(json.dumps({"error": f"unknown tool: {tool}",
                          "known": sorted(TOOL_SCHEMAS)}, indent=2),
              file=sys.stderr)
        return 2
    print(json.dumps(schema, indent=2, ensure_ascii=False))
    return 0

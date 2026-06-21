"""Agent-friendly JSON envelope + schemas for RTLScanner subcommands.

All subcommands share the same top-level envelope when invoked with --json::

    {
      "tool":        "tree",
      "version":     "<tool-version>",
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

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional

# Keep in sync with pyproject.toml [project].version.
TOOL_VERSION = "0.3.0"

# Default cap on the number of rows/items emitted per list, so a query against a
# large design stays agent-friendly instead of dumping thousands of entries.
# `--limit 0` removes the cap; an explicit `--limit N` overrides this default.
DEFAULT_LIMIT = 200


def resolve_limit(limit, verbose: bool = False) -> int:
    """Resolve the effective per-list item cap (0 == unlimited).

    An explicit ``--limit`` wins; otherwise ``--verbose`` (where a command has
    it) disables truncation, and failing both the default cap applies.
    """
    if limit is not None:
        return limit if limit > 0 else 0
    return 0 if verbose else DEFAULT_LIMIT


def clip(items, limit: int):
    """Clip an iterable to *limit* items.

    Returns ``(shown, total, truncated)`` where ``shown`` is a list of at most
    ``limit`` items (all of them when ``limit <= 0``), ``total`` is the full
    count, and ``truncated`` is True when items were dropped.
    """
    seq = list(items)
    total = len(seq)
    if limit <= 0 or total <= limit:
        return seq, total, False
    return seq[:limit], total, True


def truncation_note(shown: int, total: int, noun: str = "items") -> str:
    """Human-readable trailing note for a truncated list."""
    return (f"... truncated: {shown}/{total} {noun} shown. "
            "(use --limit 0 to see all)")


# Path-style vocabulary shared by `tree --path-style` and the xref `path_style`
# config.  Both the long and short spellings are accepted everywhere and
# normalize to the long canonical form.  The two third options stay
# command-specific because they are different *modes*, not spellings:
#   tree:  'prefix' -> ``${PROJPATH}/<relative>``
#   xref:  'name'   -> bare file basename
_PATH_STYLE_SYNONYMS = {
    "rel": "relative", "relative": "relative",
    "abs": "absolute", "absolute": "absolute",
    "prefix": "prefix", "name": "name",
}


def canon_path_style(value, default: str = "relative") -> str:
    """Normalize a path-style spelling to its canonical long form.

    ``rel`` -> ``relative``, ``abs`` -> ``absolute``; ``prefix`` and ``name``
    pass through unchanged.  Anything unrecognized falls back to *default*.
    """
    return _PATH_STYLE_SYNONYMS.get(str(value).strip().lower(), default)

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
    """Raise inside a tool's JSON path to produce a structured error envelope.

    ``details`` is an optional JSON-safe dict with machine-readable recovery
    hints (e.g. close_matches / children for *_NOT_FOUND errors).
    """
    def __init__(self, code: str, message: str,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class Envelope:
    """Incrementally build the shared JSON envelope.

    Typical use in a CLI's --json path::

        env = Envelope("tree", filter_command(args, {"json","schema","no_color"}))
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

    def add_error(self, code: str, message: str,
                  details: Optional[Dict[str, Any]] = None) -> None:
        err = dict(code=code, message=message)
        if details:
            err["details"] = details
        self._errors.append(err)

    # ── finalizers ──
    def ok(self, data: Any, summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._envelope("ok", data=data, summary=summary)

    def fail(self, code: str, message: str,
             details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.add_error(code, message, details)
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

# argparse fields that describe output formatting or invocation plumbing rather
# than what was analyzed.  Filter them so the command echo stays focused on the
# semantic intent of the run.
_OUTPUT_FIELDS = frozenset({
    "json", "schema", "no_color", "markdown", "ndjson",
    "diag", "waived", "verbose", "config", "limit",
})


# ── Shared CLI helpers ──────────────────────────────────────────────
class CommaListAction(argparse.Action):
    """argparse Action that accepts a,b,c | [a,b,c] | {a,b,c} and is repeatable.

    Each invocation extends the accumulated list, so repeating the flag
    composes naturally:  --skip A,B --skip C  -> ["A","B","C"]
    """

    _STRIP_BRACKETS = re.compile(r"^[\[{]\s*|\s*[\]}]$")

    def __call__(self, parser, namespace, values, option_string=None):
        cur = getattr(namespace, self.dest, None) or []
        s = self._STRIP_BRACKETS.sub("", str(values))
        items = [tok.strip() for tok in s.split(",") if tok.strip()]
        setattr(namespace, self.dest, list(cur) + items)


def add_input_args(p: argparse.ArgumentParser) -> None:
    """Attach the shared input/config flags."""
    g = p.add_argument_group("inputs")
    g.add_argument("files", nargs="*", help="Verilog/SV source files (ad-hoc)")
    g.add_argument("--config", default=None, metavar="FILE",
                   help="Project config .toml file (overrides RTLSCANNER_CONFIG and CWD default)")
    g.add_argument("-d", "--dir", action=CommaListAction, default=[],
                   metavar="DIR",
                   help="Directory to scan recursively (comma-list or repeat)")
    g.add_argument("-f", "--filelist", action=CommaListAction, default=[],
                   metavar="FILE",
                   help="VCS-style .f filelist (comma-list or repeat)")
    g.add_argument("--exclude", action=CommaListAction, default=[],
                   metavar="GLOB",
                   help="Exclude paths matching glob (comma-list or repeat)")
    g.add_argument("--single-unit", action="store_true",
                   help="Compile the whole filelist as ONE compilation unit "
                        "(legacy / slang --single-unit): $unit-scoped typedefs "
                        "and `defines from an earlier file stay visible to later "
                        "files. Default: each file is its own unit "
                        "(slang-driver / VCS / Verilator behavior).")


def add_output_args(p: argparse.ArgumentParser) -> None:
    """Attach the shared output flags (--json / --schema / --no-color / --limit)."""
    g = p.add_argument_group("output")
    g.add_argument("--json", action="store_true",
                   help="Emit results as an agent-friendly JSON envelope (see --schema)")
    g.add_argument("--schema", action="store_true",
                   help="Print the JSON Schema for --json output and exit")
    g.add_argument("--no-color", action="store_true",
                   help="Disable ANSI colors")
    g.add_argument("--limit", type=int, default=None, metavar="N",
                   help=f"Max rows/items to emit per list; default {DEFAULT_LIMIT}; "
                        "0 = unlimited. For the full result, redirect --json to a "
                        "file (e.g. '--json > out.json'); the 'summary' field "
                        "carries the totals.")


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


# ── The shared result → render seam ─────────────────────────────────
class CommandResult:
    """Typed result of one subcommand, with two pure renderers.

    A subcommand's ``run()`` does all of its analysis up front and packs it into
    one of these — the single place every count / summary / total is derived.
    :func:`render` then dispatches on output mode: ``--json`` calls
    :meth:`to_json`, human mode calls :meth:`render_human`.  Because both
    renderers read the *same* typed fields (rather than each re-deriving the
    summary down its own ``if env:`` branch), the JSON and human views cannot
    drift — the class of bug the schema-conformance test was added to guard.
    """

    #: Process exit code applied in BOTH output modes.  Defaults to 0; ``lint``
    #: overrides it to 1 when the design has an error-severity finding.
    exit_code: int = 0

    def to_json(self, limit: int):
        """Return ``(data, summary)`` for the shared ``--json`` envelope."""
        raise NotImplementedError

    def render_human(self, limit: int) -> int:
        """Print the human-readable view; return the process exit code."""
        raise NotImplementedError


def render(env: Optional[Envelope], result: CommandResult, limit: int) -> int:
    """The single result → render seam shared by every subcommand.

    ``env`` is the JSON :class:`Envelope` (``--json``) or ``None`` (human mode).
    The typed ``result`` carries both renderers; this picks one.  ``result``'s
    ``exit_code`` is honored in both modes so a successful-but-flagged run (e.g.
    ``lint`` finding an error) still exits non-zero.
    """
    if env is not None:
        data, summary = result.to_json(limit)
        rc = emit(env.ok(data, summary))
        return result.exit_code or rc
    return result.render_human(limit)


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
        "details": {
            "type": "object",
            "description": "Machine-readable recovery hints. For "
                "SCOPE_NOT_FOUND: valid_prefix, failing_component, "
                "close_matches, children. For SIGNAL_NOT_FOUND: "
                "close_matches, available.",
        },
    },
    "additionalProperties": False,
}


# Every subcommand's main (non-`--summary`) JSON path reports how its output
# was capped by `--limit` (see resolve_limit/clip), so these two summary fields
# are part of every envelope's contract.  Inject them once here instead of
# repeating the boilerplate in each per-tool summary schema.  They are optional
# (not in `required`) because the fanin/fanout `--summary` view emits a
# different summary shape that omits them.
_SUMMARY_LIMIT_FIELDS = {
    "truncated": {"type": "boolean",
                  "description": "True when an emitted list was capped by "
                  "--limit; the count fields still report the true totals."},
    "limit": {"type": "integer",
              "description": "Effective per-list item cap that was applied "
              "(0 means unlimited)."},
}


def _inject_limit_fields(summary_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add the shared `truncated`/`limit` keys to an object summary schema."""
    if (isinstance(summary_schema, dict)
            and summary_schema.get("type") == "object"
            and isinstance(summary_schema.get("properties"), dict)):
        for key, spec in _SUMMARY_LIMIT_FIELDS.items():
            summary_schema["properties"].setdefault(key, spec)
    return summary_schema


def _envelope_schema(tool: str, data_schema: Dict[str, Any],
                     summary_schema: Dict[str, Any],
                     description: str = "") -> Dict[str, Any]:
    summary_schema = _inject_limit_fields(summary_schema)
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": f"{tool} agent-mode output envelope",
        "description": description or
            f"Stable agent-mode JSON envelope produced by `{tool} --json`. "
            "All RTLScanner subcommands share the same top-level shape.",
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
    "tree",
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
        "bits":        {"type": "string",
                        "description": "Normalized bit offsets this driver "
                        "covers ('[3]' / '[7:4]'); absent when it drives "
                        "the whole signal."},
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
        "bits":        {"type": "string",
                        "description": "Bit sub-range of the signal this load "
                        "reads ('[3]' / '[7:4]'); absent when it reads the "
                        "whole signal."},
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
        "bit_select":           {"type": "string",
                                 "description": "Present when the query used a "
                                 "bit-select (e.g. '[3]', '[7:4]'): both drivers "
                                 "and loads are narrowed to the readers/writers "
                                 "that actually touch those bits."},
        "driver":               {"oneOf": [_TRACE_DRIVER, {"type": "null"}]},
        "extra_drivers":        {"type": "array", "items": _TRACE_DRIVER},
        "multi_driver_warning": {"type": "boolean",
                                 "description": "True only when driver bit "
                                 "ranges overlap; multiple drivers over "
                                 "disjoint ranges (per-bit generate "
                                 "outputs) are legal and not flagged."},
        "loads":                {"type": "array", "items": _TRACE_LOAD},
        "load_count":           {"type": "integer"},
    },
    "additionalProperties": True,
}
_TRACE_FLOW_EDGE = {
    "type": "object",
    "required": ["source", "target", "kind", "description"],
    "properties": {
        "source":      {"type": "string"},
        "target":      {"type": "string"},
        "kind":        {"type": "string"},
        "description": {"type": "string"},
        "source_type": {"type": "string"},
        "target_type": {"type": "string"},
        "source_bits": {"type": "string",
                        "description": "Bit sub-range of the source that this "
                        "edge reads ('[5]' / '[7:4]'); absent when the whole "
                        "signal is read. Bit-level dataflow (slang-netlist "
                        "parity)."},
        "target_bits": {"type": "string",
                        "description": "Bit sub-range of the target that this "
                        "edge drives; absent when the whole signal is driven. "
                        "With source_bits, answers 'dout[5] comes from a[2]'."},
        "segments":    {"type": "array",
                        "items": {"type": "object",
                                  "properties": {
                                      "source_bits": {"type": "string"},
                                      "target_bits": {"type": "string"}},
                                  "required": ["source_bits", "target_bits"]},
                        "description": "Per-bit permutation map for a copy whose "
                        "offset varies (a bit reversal 'rev[i]=din[7-i]', a swap "
                        "'o={a[3:0],a[7:4]}') — a list of {source_bits, "
                        "target_bits} sub-copies, e.g. 'din[7] -> rev[0]'. "
                        "Present only for such a permutation; single-offset and "
                        "whole-signal edges omit it (source_bits/target_bits then "
                        "carry the map)."},
        "file":        {"type": "string"},
        "line":        {"type": "integer"},
        "depth":       {"type": "integer"},
        "clocked":     {"type": "boolean",
                        "description": "Present and true when the edge is "
                        "registered (its target is driven by an edge-triggered "
                        "always_ff / latch / edge-sensitive always) — i.e. a "
                        "flip-flop boundary; absent for combinational edges "
                        "(continuous assign, always_comb, port connection)."},
    },
    "additionalProperties": True,
}

_TRACE_SCHEMA = _envelope_schema(
    "trace",
    data_schema={
        "type": "object",
        "required": ["mode", "scope"],
        "properties": {
            "mode":    {"type": "string", "const": "signal"},
            "scope":   {"type": "string"},
            "results": {"type": "array", "items": _TRACE_RESULT},
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
        },
    },
)

def _flow_schema(tool_name: str) -> Dict[str, Any]:
    return _envelope_schema(
        tool_name,
        data_schema={
            "type": "object",
            "required": ["mode", "scope", "signal", "start"],
            "properties": {
                "mode":      {"type": "string", "const": tool_name},
                "scope":     {"type": "string"},
                "signal":    {"type": "string",
                              "description": "Starting signal name."},
                "bit_select": {"type": "string",
                              "description": "Present when the query used a "
                              "bit-select (e.g. '[5]', '[7:4]'): the traversal "
                              "follows only edges touching those bits and maps "
                              "the range across each hop (bit-level dataflow)."},
                "comb":      {"type": "boolean",
                              "description": "Present and true when --comb was "
                              "used: the BFS stopped at sequential (clocked) "
                              "edges, so the cone is the pure combinational "
                              "fan-in/out bounded by flip-flops. max_depth then "
                              "reports the deepest hop the cone reached."},
                "start":     {"type": "string",
                              "description": "Elaborated hierarchical path of the starting signal."},
                "max_depth": {"type": "integer"},
                "nodes":     {"type": "array", "items": {"type": "string"},
                              "description": "Full graph; absent with --summary."},
                "edges":     {"type": "array", "items": _TRACE_FLOW_EDGE,
                              "description": "Full graph; absent with --summary."},
                "summary_only": {"type": "boolean",
                              "description": "True when --summary was used: the full "
                              "nodes/edges arrays are omitted in favor of counts."},
                "node_count": {"type": "integer", "description": "--summary only."},
                "edge_count": {"type": "integer", "description": "--summary only."},
                "edges_by_depth": {"type": "object",
                              "additionalProperties": {"type": "integer"},
                              "description": "--summary only: edge count per BFS depth."},
                "direct":    {"type": "array", "items": {"type": "string"},
                              "description": "--summary only: depth-1 neighbors "
                              "(direct sources for fanin, direct sinks for fanout)."},
            },
        },
        summary_schema={
            "type": "object",
            "required": ["mode", "results"],
            "properties": {
                "mode":      {"type": "string"},
                "results":   {"type": "integer"},
                "nodes":     {"type": "integer"},
                "edges":     {"type": "integer"},
                "max_depth": {"type": "integer"},
            },
        },
    )


_FANIN_SCHEMA = _flow_schema("fanin")
_FANOUT_SCHEMA = _flow_schema("fanout")


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
                     "description": "Open enum of specific rule names. Known rules "
                     "include `inferred-latch`, `unassigned-variable`, "
                     "`undriven-port`, `width-trunc`, `port-width-mismatch`, "
                     "`cdc-crossing`, `comb-loop`, `unused-port`, `case-default`. "
                     "Each rule belongs to one of the `check` categories."},
        "message":  {"type": "string"},
        "check":    {"type": "string",
                     "description": "The check category (closed set).",
                     "enum": ["semantic", "unused", "port", "cdc", "comb-loop"]},
        "module":   {"type": "string",
                     "description": "Design unit (module / interface / ...) the "
                     "finding sits in, attributed by source range. Absent when "
                     "the finding could not be attributed to a unit."},
    },
    "additionalProperties": True,
}

_LINT_SCHEMA = _envelope_schema(
    "lint",
    data_schema={
        "type": "object",
        "required": ["findings", "config_path"],
        "properties": {
            "findings":    {"type": "array", "items": _LINT_FINDING},
            "config_path": {"type": ["string", "null"]},
        },
    },
    summary_schema={
        "type": "object",
        "required": ["total", "by_severity", "by_rule", "by_check",
                     "files_linted", "has_error"],
        "properties": {
            "total":        {"type": "integer",
                             "description": "True total finding count, even when "
                             "`findings` was capped by --limit."},
            "shown":        {"type": "integer",
                             "description": "Number of findings actually emitted "
                             "in `data.findings` after the --limit cap."},
            "by_severity":  {"type": "object",
                             "additionalProperties": {"type": "integer"}},
            "by_rule":      {"type": "object",
                             "additionalProperties": {"type": "integer"}},
            "by_check":     {"type": "object",
                             "description": "Finding count per check category.",
                             "additionalProperties": {"type": "integer"}},
            "files_linted": {"type": "integer"},
            "has_error":    {"type": "boolean"},
        },
    },
    description=("Stable agent-mode JSON envelope produced by `rtlscanner lint --json`. "
                 "`lint` runs a closed set of five check categories — semantic, "
                 "unused, port, cdc, comb-loop — selected with `--rules` (default: "
                 "all). Every finding carries `file`, `line`, `col`, `severity`, "
                 "`rule`, `message`, `check`, and `module`. CDC findings appear with "
                 "`rule=\"cdc-crossing\"` / `check=\"cdc\"`; combinational loops with "
                 "`rule=\"comb-loop\"` / `check=\"comb-loop\"`. For the complete "
                 "result on a large design, redirect `--json` to a file; `summary` "
                 "carries the by-category / by-severity counts."),
)


# ── rtl-scope ──
_SCOPE_PORT = {
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
_SCOPE_SIGNAL = {
    "type": "object",
    "required": ["name", "kind", "type", "width"],
    "properties": {
        "name":  {"type": "string"},
        "kind":  {"type": "string"},
        "type":  {"type": "string"},
        "width": {"type": ["integer", "null"]},
        "file":  {"type": "string"},
        "line":  {"type": "integer"},
    },
    "additionalProperties": True,
}
_SCOPE_INSTANCE = {
    "type": "object",
    "required": ["instance", "module", "path", "params"],
    "properties": {
        "instance": {"type": "string"},
        "module":   {"type": "string"},
        "path":     {"type": "string"},
        "params":   {"type": "object",
                     "additionalProperties": {"type": "string"}},
        "file":     {"type": "string"},
        "line":     {"type": "integer"},
    },
    "additionalProperties": True,
}
_SCOPE_PARAM = {
    "type": "object",
    "required": ["name", "kind", "type", "value", "expression",
                 "hierarchical_path", "lexical_path", "bit_width", "is_signed"],
    "properties": {
        "name":              {"type": "string"},
        "kind":              {"type": "string",
                              "enum": ["parameter", "localparam", "type_parameter"]},
        "type":              {"type": "string"},
        "value":             {"type": ["string", "null"]},
        "expression":        {"type": "string"},
        "bit_width":         {"type": ["integer", "null"]},
        "is_signed":         {"type": ["boolean", "null"]},
        "hierarchical_path": {"type": "string"},
        "lexical_path":      {"type": "string"},
        "is_overridden":     {"type": "boolean"},
        "file":              {"type": "string"},
        "line":              {"type": "integer"},
    },
    "additionalProperties": True,
}
_SCOPE_TYPEDEF = {
    "type": "object",
    "required": ["name", "kind", "type", "canonical_type",
                 "bit_width", "hierarchical_path", "lexical_path"],
    "properties": {
        "name":              {"type": "string"},
        "kind":              {"type": "string"},
        "type":              {"type": "string"},
        "canonical_type":    {"type": "string"},
        "bit_width":         {"type": ["integer", "null"]},
        "hierarchical_path": {"type": "string"},
        "lexical_path":      {"type": "string"},
        "members":           {"type": "array", "items": {"type": "string"}},
        "member_details": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name":       {"type": "string"},
                    "value":      {"type": "string"},
                    "expression": {"type": "string"},
                    "file":       {"type": "string"},
                    "line":       {"type": "integer"},
                },
                "additionalProperties": True,
            },
        },
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "type", "bit_width"],
                "properties": {
                    "name":       {"type": "string"},
                    "type":       {"type": "string"},
                    "bit_width":  {"type": ["integer", "null"]},
                    "bit_offset": {"type": "integer"},
                    "index":      {"type": "integer"},
                    "file":       {"type": "string"},
                    "line":       {"type": "integer"},
                },
                "additionalProperties": True,
            },
        },
        "file":              {"type": "string"},
        "line":              {"type": "integer"},
    },
    "additionalProperties": True,
}
_SCOPE_CONNECTION = {
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

_SCOPE_SCHEMA = _envelope_schema(
    "scope",
    data_schema={
        "type": "object",
        "required": ["mode", "scope", "module"],
        "properties": {
            "mode":        {"type": "string", "const": "scope"},
            "scope":       {"type": "string"},
            "module":      {"type": "string"},
            "ports":       {"type": "array", "items": _SCOPE_PORT},
            "signals":     {"type": "array", "items": _SCOPE_SIGNAL},
            "instances":   {"type": "array", "items": _SCOPE_INSTANCE},
            "params":      {"type": "array", "items": _SCOPE_PARAM},
            "typedefs":    {"type": "array", "items": _SCOPE_TYPEDEF},
            "connections": {"type": "array", "items": _SCOPE_CONNECTION},
        },
        "additionalProperties": False,
    },
    summary_schema={
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode":        {"type": "string"},
            "ports":       {"type": "integer"},
            "signals":     {"type": "integer"},
            "instances":   {"type": "integer"},
            "params":      {"type": "integer"},
            "typedefs":    {"type": "integer"},
            "connections": {"type": "integer"},
        },
    },
)


# ── rtl-xref ──
_SYMBOL_INFO = {
    "type": "object",
    "required": ["name", "kind", "type", "hierarchical_path", "lexical_path"],
    "properties": {
        "name":              {"type": "string"},
        "kind":              {"type": "string"},
        "type":              {"type": "string"},
        "direction":         {"type": "string"},
        "hierarchical_path": {"type": "string"},
        "lexical_path":      {"type": "string"},
        "file":              {"type": "string"},
        "line":              {"type": "integer"},
        "column":            {"type": "integer"},
        "location": {
            "type": "object",
            "required": ["file", "line", "column"],
            "properties": {
                "file":   {"type": "string"},
                "line":   {"type": "integer"},
                "column": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": True,
}
_XREF_REF = {
    "type": "object",
    "required": ["access", "kind", "description", "scope_path"],
    "properties": {
        "access":      {"type": "string", "enum": ["read", "write", "readwrite"]},
        "kind":        {"type": "string"},
        "description": {"type": "string"},
        "scope_path":  {"type": "string"},
        "instance":    {"type": "string"},
        "port":        {"type": "string"},
        "direction":   {"type": "string"},
        "file":        {"type": "string"},
        "line":        {"type": "integer"},
        "column":      {"type": "integer"},
        "location": {
            "type": "object",
            "required": ["file", "line", "column"],
            "properties": {
                "file":   {"type": "string"},
                "line":   {"type": "integer"},
                "column": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": True,
}
_XREF_MATCH = {
    "type": "object",
    "required": ["symbol", "definitions", "references", "summary"],
    "properties": {
        "symbol":      _SYMBOL_INFO,
        "definitions": {"type": "array", "items": _SYMBOL_INFO},
        "references":  {"type": "array", "items": _XREF_REF},
        "summary":     {"type": "object"},
    },
    "additionalProperties": True,
}
_XREF_MODULE_DEF = {
    "type": "object",
    "required": ["kind", "name"],
    "properties": {
        "kind": {"type": "string", "const": "module"},
        "name": {"type": "string"},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "column": {"type": "integer"},
        "location": {"type": "object"},
    },
    "additionalProperties": True,
}
_XREF_MODULE_REF = {
    "type": "object",
    "required": ["kind", "module", "instance_path"],
    "properties": {
        "kind": {"type": "string", "enum": ["instance", "top_instance"]},
        "module": {"type": "string"},
        "instance_path": {"type": "string"},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "column": {"type": "integer"},
        "location": {"type": "object"},
        "details": {"type": "object"},
    },
    "additionalProperties": True,
}
_XREF_SCHEMA = _envelope_schema(
    "xref",
    data_schema={
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode":      {"type": "string", "const": "xref"},
            "target":    {"type": "object"},
            "scope":     {"type": "string"},
            "name":      {"type": "string"},
            "recursive": {"type": "boolean"},
            "matches":   {"type": "array", "items": _XREF_MATCH},
            "definitions": {"type": "array", "items": _XREF_MODULE_DEF},
            "references":  {"type": "array", "items": _XREF_MODULE_REF},
            "summary":     {"type": "object"},
        },
        "additionalProperties": True,
    },
    summary_schema={
        "type": "object",
        "required": ["mode", "definitions", "references"],
        "properties": {
            "mode":             {"type": "string"},
            "target_kind":      {"type": "string", "enum": ["signal", "module"]},
            "symbols":          {"type": "integer"},
            "definitions":      {"type": "integer"},
            "references":       {"type": "integer"},
            "reads":            {"type": "integer"},
            "writes":           {"type": "integer"},
            "port_connections": {"type": "integer"},
            "instances":        {"type": "integer"},
        },
    },
)

# ── rtl-find ──
_FIND_NODE = {
    "type": "object",
    "required": ["category", "name", "kind", "hierarchical_path"],
    "properties": {
        "category":          {"type": "string", "enum": ["signal", "instance"],
                              "description": "Coarse node category (the --kind "
                              "vocabulary)."},
        "name":              {"type": "string", "description": "Leaf name."},
        "kind":              {"type": "string",
                              "description": "Elaborated SymbolKind name "
                              "(Net / Variable / Instance / ...)."},
        "hierarchical_path": {"type": "string",
                              "description": "Full path that matched the pattern."},
        "type":              {"type": "string",
                              "description": "Signal data type; present for "
                              "signal nodes."},
        "module":            {"type": "string",
                              "description": "Elaborated module name; present for "
                              "instance nodes."},
        "file":              {"type": "string"},
        "line":              {"type": "integer"},
        "column":            {"type": "integer"},
        "location": {
            "type": "object",
            "required": ["file", "line", "column"],
            "properties": {
                "file":   {"type": "string"},
                "line":   {"type": "integer"},
                "column": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": True,
}

_FIND_SCHEMA = _envelope_schema(
    "find",
    data_schema={
        "type": "object",
        "required": ["mode", "pattern", "matches"],
        "properties": {
            "mode":        {"type": "string", "const": "find"},
            "pattern":     {"type": "string", "description": "The query pattern."},
            "regex":       {"type": "boolean",
                            "description": "True when PATTERN was a regex "
                            "(whole-path match); false for the segment-aware glob."},
            "kind":        {"type": "string", "enum": ["all", "signal", "instance"]},
            "scope":       {"type": "string",
                            "description": "Scope the search was restricted to "
                            "(empty = whole design)."},
            "matches":     {"type": "array", "items": _FIND_NODE},
            "match_count": {"type": "integer",
                            "description": "True total match count, even when "
                            "`matches` was capped by --limit."},
        },
        "additionalProperties": True,
    },
    summary_schema={
        "type": "object",
        "required": ["mode", "matches"],
        "properties": {
            "mode":      {"type": "string"},
            "matches":   {"type": "integer"},
            "signals":   {"type": "integer"},
            "instances": {"type": "integer"},
        },
    },
    description=("Stable agent-mode JSON envelope produced by `rtlscanner find "
                 "--json`. `find` walks the whole elaborated design and reports "
                 "every signal/instance node whose hierarchical path matches the "
                 "glob (default) or regex (`--regex`) PATTERN, each with its "
                 "source location — the slang-netlist `--find` analogue. Narrow "
                 "with `--kind signal|instance` and `--scope`."),
)

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "tree":    _TREE_SCHEMA,
    "trace":   _TRACE_SCHEMA,
    "scope":   _SCOPE_SCHEMA,
    "fanin":   _FANIN_SCHEMA,
    "fanout":  _FANOUT_SCHEMA,
    "lint":    _LINT_SCHEMA,
    "xref":    _XREF_SCHEMA,
    "find":    _FIND_SCHEMA,
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

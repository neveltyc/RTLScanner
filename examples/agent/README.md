# Agent-Friendly JSON Mode

The four RTLScanner CLIs (`rtl-tree`, `signal-trace`, `rtl-lint`, `rtl-ports`)
share a single, predictable JSON output envelope when invoked with `--json`.
This makes them safe to drive from LLM agents, MCP servers, CI scripts, or
any other consumer that needs structured output.

## Envelope shape

```json
{
  "tool":        "rtl-tree",
  "version":     "0.1.0",
  "status":      "ok",
  "command":     { /* echo of parsed CLI args, output flags stripped */ },
  "data":        { /* tool-specific payload */ },
  "diagnostics": [ /* parser warnings/notes, normalized */ ],
  "errors":      [ /* structured errors when status == "error" */ ],
  "summary":     { /* tool-specific counts */ }
}
```

Top-level keys are **always present** (even when empty). The shape is the
same across all four tools — only `data` and `summary` differ.

## Discovering a tool's schema

Each tool exposes its own JSON Schema (draft-07) via `--schema`. Cache it
once per release, then validate every envelope you receive:

```bash
rtl-tree     --schema > schemas/tree.schema.json
signal-trace --schema > schemas/trace.schema.json
rtl-lint     --schema > schemas/lint.schema.json
rtl-ports    --schema > schemas/ports.schema.json
```

Pre-generated schemas live in `examples/agent/schemas/`.

## Error envelopes

When a tool can't complete, it still emits a full envelope (with non-zero
exit code) instead of dying on stderr:

```json
{
  "tool":   "rtl-tree",
  "status": "error",
  "data":   null,
  "errors": [{"code": "INPUT_NOT_FOUND", "message": "no .v/.sv source files found"}],
  ...
}
```

Closed enum of error codes:

| Code                | Meaning                                                          |
|---------------------|------------------------------------------------------------------|
| `INPUT_NOT_FOUND`   | No source files (or required flag combination missing)           |
| `BAD_FILELIST`      | Filelist could not be parsed                                     |
| `COMPILE_FAILED`    | pyslang compilation raised                                       |
| `NO_TOP`            | No top-level module found                                        |
| `SCOPE_NOT_FOUND`   | `--scope` does not exist (or ambiguous tops)                     |
| `SIGNAL_NOT_FOUND`  | `--signal` not present in the requested scope                    |
| `BAD_CONFIG`        | `.rtllint.toml/json` could not be loaded                         |
| `INTERNAL_ERROR`    | Catch-all                                                        |

## Examples

| File                          | Command                                                          |
|-------------------------------|------------------------------------------------------------------|
| `tree.out.json`               | `rtl-tree -d examples/basic --json`                              |
| `trace.out.json`              | `signal-trace -d examples/basic --signal q --scope top.u_dp0 --json` |
| `lint.out.json`               | `rtl-lint -d examples/lint --json`                               |
| `lint-cdc.out.json`           | `rtl-lint examples/lint/cdc_demo.sv --cdc --json`                |
| `ports.out.json`              | `rtl-ports -d examples/ports --json`                             |

## CDC notes for agents

CDC findings appear in `rtl-lint` output as regular lint findings:

```json
{"rule": "cdc-crossing", "check": "cdc", "severity": "warning", ...}
```

- Quick count: `data.summary.by_check.cdc`
- **`` `pragma diagnostic ignore `` does NOT suppress `cdc-crossing`.**
  pyslang's pragma engine only handles its native diag codes. To waive
  a CDC finding, use a `[[waive]]` entry in `.rtllint.toml`:

  ```toml
  [[waive]]
  rule   = "cdc-crossing"
  path   = "rtl/sync/cdc_sync.sv"
  line   = 23
  reason = "2-FF synchronizer — reviewed"
  ```

## Field-naming conventions

Stable across all tools:

- File location: `file`, `line`, `col` (never `filename`/`location`/`lineno`)
- Severity: `error` | `warning` | `note` (lint also uses `ignored` for waived)
- Hierarchical paths: `path` (on a node), `scope` (for scope-limited queries)
- Lists are **always present**, empty becomes `[]`, not absent

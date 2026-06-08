# Agent-Friendly JSON Mode

`rtlscanner` exposes nine subcommands behind a unified JSON envelope when
invoked with `--json`. The envelope shape is identical across all
subcommands, so a single consumer (LLM agent, MCP server, CI script)
can drive every one of them without per-subcommand parsing.

## Envelope shape

```json
{
  "tool":        "tree",
  "version":     "0.1.1",
  "status":      "ok",
  "command":     { /* echo of parsed CLI args, output flags stripped */ },
  "data":        { /* subcommand-specific payload */ },
  "diagnostics": [ /* parser warnings/notes, normalized */ ],
  "errors":      [ /* structured errors when status == "error" */ ],
  "summary":     { /* subcommand-specific counts */ }
}
```

Top-level keys are **always present** (even when empty). The shape is the
same across all nine subcommands — only `tool`, `data`, and `summary`
differ.

## Discovering a subcommand's schema

Each subcommand exposes its own JSON Schema (draft-07) via `--schema`.
Cache it once per release, then validate every envelope you receive:

```bash
rtlscanner tree    --schema > schemas/tree.schema.json
rtlscanner trace   --schema > schemas/trace.schema.json
rtlscanner signals --schema > schemas/signals.schema.json
rtlscanner fanin   --schema > schemas/fanin.schema.json
rtlscanner fanout  --schema > schemas/fanout.schema.json
rtlscanner lint    --schema > schemas/lint.schema.json
rtlscanner ports   --schema > schemas/ports.schema.json
rtlscanner xref    --schema > schemas/xref.schema.json
rtlscanner inspect --schema > schemas/inspect.schema.json
```

Pre-generated schemas live in `examples/agent/schemas/`.

## Error envelopes

When a subcommand can't complete, it still emits a full envelope (with
non-zero exit code) instead of dying on stderr:

```json
{
  "tool":   "tree",
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
| `BAD_CONFIG`        | `.rtlscanner.toml` could not be loaded                           |
| `INTERNAL_ERROR`    | Catch-all                                                        |

## Examples

| File                  | Command                                                          |
|-----------------------|------------------------------------------------------------------|
| `tree.out.json`       | `rtlscanner tree -d examples/basic --json`                       |
| `trace.out.json`      | `rtlscanner trace -d examples/basic --signal q --scope top.u_dp0 --json` |
| `signals.out.json`    | `rtlscanner signals -d examples/basic --scope top.u_dp0 --json`  |
| `flow.out.json`       | `rtlscanner fanout -d examples/basic --signal q --scope top.u_dp0 --json` |
| `lint.out.json`       | `rtlscanner lint -d examples/lint --json`                        |
| `lint-cdc.out.json`   | `rtlscanner lint examples/lint/cdc_demo.sv --rules default,cdc --json` |
| `ports.out.json`      | `rtlscanner ports -d examples/ports --json`                      |
| `xref.out.json`       | `rtlscanner xref -d examples/trace --scope trace_top.u_dp --signal mux_out --json` |
| `inspect.out.json`    | `rtlscanner inspect -d examples/trace --scope trace_top.u_dp.u_pipe --json` |

## CDC notes for agents

CDC findings appear in `lint` output as regular lint findings:

```json
{"rule": "cdc-crossing", "check": "cdc", "severity": "warning", ...}
```

- Quick count: `data.summary.by_check.cdc`
- **`` `pragma diagnostic ignore `` does NOT suppress `cdc-crossing`.**
  pyslang's pragma engine only handles its native diag codes. To waive
  a CDC finding, use `[lint].waive` (module-name glob) in
  `.rtlscanner.toml`:

  ```toml
  [lint]
  waive = ["cdc_sync"]
  ```

## Field-naming conventions

Stable across all subcommands:

- File location: `file`, `line`, `col` (never `filename`/`location`/`lineno`)
- Severity: `error` | `warning` | `note` (lint also uses `ignored` for waived)
- Hierarchical paths: `path` (on a node), `scope` (for scope-limited queries)
- Lists are **always present**, empty becomes `[]`, not absent

## Configuration

`rtlscanner` reads `./.rtlscanner.toml` (CWD only, no walk-up, no
`--config` flag). Five environment variables override the config:
`RTLSCANNER_FILELIST`, `RTLSCANNER_DIR`, `RTLSCANNER_EXCLUDE`,
`RTLSCANNER_ROOT`, `RTLSCANNER_PREFIX`. CLI flags override env vars.

```toml
[inputs]
filelist = ["rtl/top.f"]
root     = "."
prefix   = "${PROJPATH}"
exclude  = ["**/sim/**"]

[lint]
rules = ["default", "cdc"]
skip  = ["case-default"]
waive = ["dbg_*", "third_party_*"]

[lint.severity]
"width-trunc" = "error"

[lint.cdc]
reset = ["nrst_*"]
```

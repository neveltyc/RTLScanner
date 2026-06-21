# Agent-Friendly JSON Mode

`rtlscanner` exposes its subcommands behind a JSON envelope when
invoked with `--json`. The envelope shape is identical across all
subcommands, so a single consumer (LLM agent, MCP server, CI script)
can drive every one of them without per-subcommand parsing.

## Envelope shape

```json
{
  "tool":        "tree",
  "version":     "<tool-version>",
  "status":      "ok",
  "command":     { /* echo of parsed CLI args, output flags stripped */ },
  "data":        { /* subcommand-specific payload */ },
  "diagnostics": [ /* parser warnings/notes, normalized */ ],
  "errors":      [ /* structured errors when status == "error" */ ],
  "summary":     { /* subcommand-specific counts */ }
}
```

Top-level keys are **always present** (even when empty). Only `tool`,
`data`, and `summary` differ by subcommand.

## Discovering a subcommand's schema

Each subcommand exposes its own JSON Schema (draft-07) via `--schema`.
Use it to validate envelopes:

```bash
rtlscanner tree    --schema > schemas/tree.schema.json
rtlscanner trace   --schema > schemas/trace.schema.json
rtlscanner scope   --schema > schemas/scope.schema.json
rtlscanner fanin   --schema > schemas/fanin.schema.json
rtlscanner fanout  --schema > schemas/fanout.schema.json
rtlscanner lint    --schema > schemas/lint.schema.json
rtlscanner xref    --schema > schemas/xref.schema.json
rtlscanner find    --schema > schemas/find.schema.json
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
| `BAD_CONFIG`        | Selected config file could not be loaded                         |
| `INTERNAL_ERROR`    | Catch-all                                                        |

## Examples

| File                  | Command                                                          |
|-----------------------|------------------------------------------------------------------|
| `tree.out.json`       | `rtlscanner tree -d examples/basic --json`                       |
| `trace.out.json`      | `rtlscanner trace -d examples/basic --signal q --scope top.u_dp0 --json` |
| `scope.out.json`      | `rtlscanner scope -d examples/basic --scope top.u_dp0 --connections --json` |
| `flow.out.json`       | `rtlscanner fanout -d examples/basic --signal q --scope top.u_dp0 --json` |
| `comb.out.json`       | `rtlscanner fanin -d examples/trace --signal q --scope trace_top.u_dp.u_pipe --comb --json` |
| `lint.out.json`       | `rtlscanner lint -d examples/lint --json`                        |
| `lint-cdc.out.json`   | `rtlscanner lint examples/lint/cdc_demo.sv --rules cdc --json`   |
| `xref.out.json`       | `rtlscanner xref -d examples/trace --scope trace_top.u_dp --signal mux_out --json` |
| `find.out.json`       | `rtlscanner find -d examples/basic -p 'top.u_dp0.**' --json`     |

## CDC / combinational-loop notes for agents

Both checks run on the dataflow flow graph (the one `fanin`/`fanout` use), so
they are cross-hierarchy, and both surface as regular `lint` findings:

```json
{"rule": "cdc-crossing", "check": "cdc",       "severity": "warning", ...}
{"rule": "comb-loop",    "check": "comb-loop", "severity": "warning", ...}
```

- Both run by default; narrow with `--rules cdc` and/or `--rules comb-loop`.
- Quick counts: `data.summary.by_check.cdc`, `data.summary.by_check["comb-loop"]`.
- **CDC** compares clock domains by **source net**, not local clock name, and
  relates flops across module boundaries — two flops on the same physical clock
  are one domain even when their ports are named differently; a gated/divided
  clock is its own domain.
- **comb-loop** reports one finding per combinational feedback cycle; a register
  in the path breaks the cycle, so legitimate sequential feedback is not flagged.
- **`` `pragma diagnostic ignore `` does NOT suppress these heuristic findings.**
  Filter them out by the `module` / `rule` / `check` field in the JSON, or keep
  the offending sources out of compilation with `--exclude`.

## Field-naming conventions

Stable across all subcommands:

- Diagnostics and lint locations use `file`, `line`, `col`
- `xref` source locations also expose `column` and `location`
- Severity: `error` | `warning` | `note`
- Hierarchical paths: `path` (on a node), `scope` (for scope-limited queries)
- Reported lists are arrays; when selected but empty, they appear as `[]`

## Configuration

`rtlscanner` uses `--config FILE`, `RTLSCANNER_CONFIG`, then
`./.rtlscanner.toml` (CWD only, no walk-up) to select a project config.
Use `--config` after the subcommand, for example
`rtlscanner tree --config rtlscanner.toml --json`.
These environment variables override config values:
`RTLSCANNER_FILELIST`, `RTLSCANNER_DIR`, `RTLSCANNER_EXCLUDE`,
`RTLSCANNER_ROOT`, `RTLSCANNER_PREFIX`. CLI flags override env vars.

```toml
[inputs]
filelist = ["rtl/top.f"]
root     = "."
prefix   = "${PROJPATH}"
exclude  = ["**/sim/**"]
```

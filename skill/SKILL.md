---
name: rtlscanner
description: Use when an agent needs to inspect SystemVerilog RTL with RTLScanner: hierarchy, scope contents, signal driver/load tracing, fanin/fanout, lint, or xref.
---

# RTLScanner

RTLScanner wraps pyslang-powered RTL analysis into an agent-friendly CLI for
SystemVerilog inspection and debug. Prefer JSON mode for agent workflows:

```bash
rtlscanner <subcommand> ... --json
```

Always inspect `status` first. On `status="error"`, use `errors[0].code`
and `errors[0].message`; do not parse stderr.

## Install

From the repository root:

```bash
pip install -e .
rtlscanner --help
```

## Pick a Command

| Need | Command |
|------|---------|
| Design hierarchy or resolved filelist | `rtlscanner tree` |
| Direct contents of a scope | `rtlscanner scope --scope SCOPE` |
| A signal's driver and loads | `rtlscanner trace -s NAME --scope SCOPE` |
| Upstream dataflow | `rtlscanner fanin -s NAME --scope SCOPE` |
| Downstream dataflow | `rtlscanner fanout -s NAME --scope SCOPE` |
| Compile/lint findings | `rtlscanner lint` |
| Source definitions and references | `rtlscanner xref` |

## Common Calls

```bash
rtlscanner tree --config .rtlscanner.toml --json
rtlscanner tree -f rtl.f --json
rtlscanner trace -f rtl.f -s ready --scope top.u_dma --json
rtlscanner scope -f rtl.f --scope top.u_phy --json
rtlscanner scope -f rtl.f --scope top.u_phy --connections --json
rtlscanner fanin -f rtl.f -s data_out --scope top.u_pipe --depth 4 --json
rtlscanner xref -f rtl.f -s state --scope top.u_ctrl --json
rtlscanner xref -f rtl.f --module fifo --json
rtlscanner lint -f rtl.f --rules port-connect --json
rtlscanner lint -f rtl.f --rules default,cdc --json
```

Use `-f` when the project has a real filelist. Use `-d` for examples or
small ad-hoc directories.

## Configuration

Use `rtlscanner <subcommand> --config FILE` or `RTLSCANNER_CONFIG` to select
one project config file. If neither is set, RTLScanner tries
`./.rtlscanner.toml` in the current working directory. Config files provide
shared defaults for all commands; CLI flags still override config values.

## Workflow Hints

- Start with `tree` when the top or scope path is unknown.
- Use `scope --signals` to confirm a signal name before `trace`, `fanin`, or `fanout`.
- Use `scope --connections` for direct child instance port maps.
- Use `scope --typedefs` for local typedef, enum, struct, and union declarations.
- Use `xref` when the user asks "where is this declared or referenced?"
- Use `lint --rules port-connect` for unconnected or width-mismatch port issues.
- Use `lint --rules semantic` for compile/front-end diagnostics only.

## Notes

- `--config` belongs after the subcommand, e.g. `rtlscanner tree --config cfg.toml`.
- If a filelist is present, directory and positional sources are ignored.
- `lint` may exit 1 for real findings; still read the JSON envelope.
- Dump a contract with `rtlscanner <subcommand> --schema`.

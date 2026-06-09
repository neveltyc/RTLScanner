---
name: rtlscanner
description: Use when an agent needs to inspect SystemVerilog RTL with RTLScanner: hierarchy, signal driver/load tracing, fanin/fanout, lint, ports, xref, or elaborated parameters/types.
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
| A signal's driver and loads | `rtlscanner trace -s NAME --scope SCOPE` |
| Signals inside a scope | `rtlscanner signals --scope SCOPE` |
| Upstream dataflow | `rtlscanner fanin -s NAME --scope SCOPE` |
| Downstream dataflow | `rtlscanner fanout -s NAME --scope SCOPE` |
| Compile/lint findings | `rtlscanner lint` |
| Module ports or instance connections | `rtlscanner ports` |
| Source definitions and references | `rtlscanner xref` |
| Elaborated parameters and local types | `rtlscanner inspect` |

## Common Calls

```bash
rtlscanner tree -f rtl.f --json
rtlscanner trace -f rtl.f -s ready --scope top.u_dma --json
rtlscanner fanin -f rtl.f -s data_out --scope top.u_pipe --depth 4 --json
rtlscanner xref -f rtl.f -s state --scope top.u_ctrl --json
rtlscanner xref -f rtl.f --module fifo --json
rtlscanner inspect -f rtl.f --scope top.u_phy --json
rtlscanner ports -f rtl.f --check --json
rtlscanner lint -f rtl.f --rules default,cdc --json
```

Use `-f` when the project has a real filelist. Use `-d` for examples or
small ad-hoc directories.

## Workflow Hints

- Start with `tree` when the top or scope path is unknown.
- Use `signals` to confirm a signal name before `trace`, `fanin`, or `fanout`.
- Use `xref` when the user asks "where is this declared or referenced?"
- Use `inspect` for elaborated parameter values, localparams, type parameters,
  typedefs, enums, structs, and unions.
- Use `ports --instances` for wiring visibility; use `ports --check` for
  unconnected or width-mismatch issues.
- Use `lint --rules semantic` for compile/front-end diagnostics only.

## Notes

- `./.rtlscanner.toml` is discovered only in the current working directory.
- If a filelist is present, directory and positional sources are ignored.
- `lint` may exit 1 for real findings; still read the JSON envelope.
- Dump a contract with `rtlscanner <subcommand> --schema`.

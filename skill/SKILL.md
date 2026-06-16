---
name: rtlscanner
description: Static SystemVerilog/Verilog RTL inspection, lint, and dataflow analysis with the RTLScanner CLI. Use when the user has RTL source (.sv, .svh, .v files, a filelist .f, or an RTL directory) and wants to understand or check the design without running a simulation. Triggers include questions like "show me the module hierarchy / design tree", "what's instantiated under X", "what drives this signal / what reads it", "trace dataflow upstream or downstream from a signal", "where is this signal/module declared and referenced", "is there a width mismatch / unused signal / inferred latch / missing case default / unconnected port", "run lint on this RTL", "check for clock-domain crossings (CDC)", or "what's inside this scope/module". Also the source-side companion to waveform debugging: after a wave dump points at a suspicious signal, use this to find where it is declared, driven, and loaded in the RTL. Not for waveforms (.vcd/.fst), simulation runtime values, or non-RTL languages.
---

# RTLScanner — agent skill

`rtlscanner` wraps pyslang's SystemVerilog parse + elaborate + analysis into seven
query subcommands over RTL *source* (no simulation). **Always pass `--json` from an
agent.** The README documents the full CLI surface, the per-command output schema, the
config file, and the lint rule catalog; this file covers only what you need to drive
the tool from an agent.

Two rules apply to every call:

- **`--json` always.** Every subcommand emits one uniform envelope:
  `{tool, version, status, command, data, diagnostics, errors, summary}`.
- **Read `status` first.** On `status="error"`, branch on `errors[0].code` and read
  `errors[0].message` / `errors[0].details`. Errors print to **stdout** with a
  non-zero exit code — never parse stderr or a stack trace.

## Install

From the repo root (needs Python 3.8+; `pip install -e .` pulls in pyslang):

```bash
pip install -e .
rtlscanner --help
```

## Pick a command — and what to read from `data`

| The user wants… | Command | Read from the envelope |
|---|---|---|
| Module hierarchy / design tree / a resolved filelist | `tree` | `data.hierarchy[]` (instance, module, path, children); `summary.module_counts`; `--export FILE` writes a filelist instead |
| What's directly inside one scope (ports, signals, child instances, params) | `scope --scope S` | `data.{ports,signals,instances,params}`; `--connections` → `data.connections[]`; `--typedefs` → local typedef/enum/struct/union |
| What drives a signal and what loads it | `trace -s N --scope S` | `data.results[].driver` (one object) + `.loads[]` + `.load_count`; multi-driver note below |
| Upstream dataflow (where does this value come from) | `fanin -s N --scope S` | `data.nodes[]`, `data.edges[]` (source→target, kind, depth, file, line) |
| Downstream dataflow (what does this value reach) | `fanout -s N --scope S` | same shape as `fanin` |
| Where a signal/module is declared and referenced | `xref` | `data.matches[].{definitions,references}`; `summary.{definitions,references,reads,writes,port_connections}` |
| Compile / lint findings (widths, unused, latches, CDC, ports) | `lint` | `data.findings[]` (file, line, col, severity, rule, message); `summary.by_rule`, `summary.has_error` |

## Common calls

```bash
rtlscanner tree   -f rtl.f --json
rtlscanner tree   -f rtl.f --top cpu --depth 2 --json
rtlscanner scope  -f rtl.f --scope top.u_phy --connections --json
rtlscanner trace  -f rtl.f -s ready --scope top.u_dma --json
rtlscanner fanin  -f rtl.f -s data_out --scope top.u_pipe --depth 6 --json
rtlscanner xref   -f rtl.f -s state --scope top.u_ctrl --json
rtlscanner xref   -f rtl.f --module fifo --json
rtlscanner lint   -f rtl.f --rules bugs --json
rtlscanner lint   -f rtl.f --rules default,cdc --json
rtlscanner lint   -f rtl.f --rules default,comb-loop --json
rtlscanner lint   -f rtl.f --rules port-connect --json
```

## Inputs & syntax

- **Prefer `-f/--filelist` for a real project**; use `-d/--dir` only for examples or
  small ad-hoc trees. When a filelist is present, `-d` and positional sources are
  **ignored** (a stderr note says so) — deliberate, so a stray `-d .` can't pull
  sim/testbench dirs into the compile.
- **`--config` is a subcommand flag**, e.g. `rtlscanner tree --config proj.toml`. If
  unset, `rtlscanner` tries `./.rtlscanner.toml` in CWD (no walk-up). Priority is
  `CLI > env (RTLSCANNER_*) > config > defaults`, field by field.
- **Repeatable flags** (`-d`, `-f`, `--exclude`, `--rules`, `--skip`, `--waive`) take a
  comma list or repetition: `-d a,b` ≡ `-d a -d b`.
- **`--scope` auto-detects** when there's exactly one top module.
- **Dotted `-s`** is accepted for `trace`/`fanin`/`fanout`/`xref`: `u_dp.q` (relative to
  `--scope`) or an absolute `top.u_dp.q` is split back into signal + scope (noted in
  `diagnostics`).
- **Bit-select on `-s`** (`'status[3]'`, `'status[7:4]'`) narrows to the driver(s) of
  those bits — "where does this bit come from". Loads are dropped; the range shows as
  `bit_select`. **`trace` only** (ignored with a note on fanin/fanout). Quote the
  brackets in a shell.

## Workflow playbooks

**Cold start (top/scope unknown).** `tree` to get the hierarchy and pick a scope path →
`scope --scope <path>` to see its ports/instances → drill in with `trace`/`xref`.

**Chase a suspicious signal.** `trace -s sig --scope S` for the immediate driver/loads →
if the driver is upstream, `fanin -s sig --scope S --depth N` to walk back to the
source → `xref -s sig --scope S` to jump to the exact file/line of the declaration and
each read/write.

**Source-side of a waveform finding.** When a wave dump (e.g. from the companion
RWaveAnalyzer / `rwave`) flags a signal that's stuck or glitching at runtime, switch to
the RTL: `xref` for where it's declared and which blocks/ports touch it, then
`trace`/`fanin` for what actually drives it. The wave tells you *when*; RTLScanner tells
you *where and why* in source.

**Lint in CI.** `rtlscanner lint -f rtl.f --strict --json` — `--strict` promotes any
finding to a gate (exit 1). Use `--rules default,cdc,comb-loop,port-connect` for the broad sweep;
permanent project waivers belong in `.rtlscanner.toml` under `[lint] waive = [...]`
(module-basename globs), not on the command line.

## lint rule model (quick form)

`--rules SPEC[,...]` is a whitelist. SPEC is a rule name (`width-trunc`), a family alias
(`semantic`, `unused`, `shadow`, `cdc`, `port-connect`, `comb-loop`), a glob (`width-*`),
or a meta value (`default` = semantic+unused, `all`, `none`, `bugs`). `--skip RULE[,...]` subtracts
(glob ok). `semantic` is the normalized slang diagnostic stream (parse, type, binding,
elaboration); for compile/front-end errors only, use `--rules semantic`, and for child
instance port issues use `--rules port-connect`. **`--rules bugs`** is the curated
real-bug preset (inferred latches, unassigned/undriven values, port-width problems,
truncation) plus all compile errors — the fastest answer to "are there real bugs?". A
typo'd rule/skip token now emits a did-you-mean `note` instead of silently selecting 0.

## Agent-side gotchas

- **Self-correct from `*_NOT_FOUND`.** `SCOPE_NOT_FOUND` and `SIGNAL_NOT_FOUND` put
  recovery data in `errors[0].details`: `close_matches`, the valid scope prefix and its
  `children`, or the `available` signal names in the resolved scope. Fix the call from
  that one response instead of re-exploring with `tree`/`scope`.
- **Multi-driver is range-aware.** `trace` sets `multi_driver_warning` only when driver
  bit ranges actually *overlap*. Several drivers with disjoint `bits` (e.g. per-bit
  generate outputs) are legal single-driver RTL, not a conflict.
- **Sibling/generate instances share one body.** Identical instances (`u_dp0`/`u_dp1`)
  and generate-array elements (`gen_arr[2].u_lane`) report the *same* drivers/loads as
  the canonical instance, with paths remapped to the one you queried — don't expect them
  to differ.
- **`lint` exits 1 on real findings** (and on `--strict` with any finding). A non-zero
  exit is not a crash — still read the JSON envelope.
- **Lint is noisy on mature RTL — don't scan 100+ findings.** For "are there real
  *bugs*?", run **`--rules bugs`**: a curated high-precision set (inferred latches,
  unassigned/undriven values, port-width problems, truncation) plus all compile errors,
  with style noise dropped. Add the cross-hierarchy graph checks with
  `--rules bugs,cdc,comb-loop` (CDC crossings + combinational loops). Otherwise read the summary
  (`summary.by_severity` / `summary.has_error`) rather than the full `findings[]`; narrow
  with `--min-severity error` or `--skip case-default,...`.
- **Module name → instance path.** `--scope` wants an instance path (`testbench.uut`), not
  a module name (`picorv32`). Map one with `xref --module picorv32` (its instance sites)
  or `tree --flat` (every instance path, one per line).
- **Big `fanin`/`fanout`? Summarize, don't dump.** A real cone can be thousands of edges.
  `--summary` gives counts + an edges-by-depth histogram + the `direct` (depth-1) neighbors;
  `--depth 1` gives just the immediate drivers/loads. Procedural edges are per-statement (an
  assignment's RHS feeds only its own LHS), so `--depth 1` is the true direct fan-in, not
  every co-sensitive signal. `--depth` defaults to 4; raise it for deeper cones.
- **Dump a contract** with `rtlscanner <subcmd> --schema` (draft-07 JSON Schema) when
  you need the exact field list for a command.

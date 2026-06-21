---
name: rtlscanner
description: Static SystemVerilog/Verilog RTL inspection, lint, and dataflow analysis with the RTLScanner CLI. Use when the user has RTL source (.sv, .svh, .v files, a filelist .f, or an RTL directory) and wants to understand or check the design without running a simulation. Triggers include questions like "show me the module hierarchy / design tree", "what's instantiated under X", "what drives this signal / what reads it", "trace dataflow upstream or downstream from a signal", "where is this signal/module declared and referenced", "is there a width mismatch / unused signal / inferred latch / missing case default / unconnected port", "run lint on this RTL", "check for clock-domain crossings (CDC)", or "what's inside this scope/module". Also the source-side companion to waveform debugging: after a wave dump points at a suspicious signal, use this to find where it is declared, driven, and loaded in the RTL. Not for waveforms (.vcd/.fst), simulation runtime values, or non-RTL languages.
---

# RTLScanner — agent skill

`rtlscanner` wraps pyslang's SystemVerilog parse + elaborate + analysis into seven
query subcommands over RTL *source* (no simulation), plus a `batch` runner that
answers many queries against one loaded design. **Always pass `--json` from an
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
| Several of the above against one design, cheaply | `batch` (stdin) | One JSONL frame per query line: `{id, ok, result}` where `result` is that command's normal envelope |

## Common calls

```bash
rtlscanner tree   -f rtl.f --json
rtlscanner tree   -f rtl.f --top cpu --depth 2 --json
rtlscanner scope  -f rtl.f --scope top.u_phy --connections --json
rtlscanner trace  -f rtl.f -s ready --scope top.u_dma --json
rtlscanner fanin  -f rtl.f -s data_out --scope top.u_pipe --depth 6 --json
rtlscanner xref   -f rtl.f -s state --scope top.u_ctrl --json
rtlscanner xref   -f rtl.f --module fifo --json
rtlscanner lint   -f rtl.f --json                      # all five categories
rtlscanner lint   -f rtl.f --rules unused,cdc --json   # narrow to a subset
rtlscanner lint   -f rtl.f --json > lint.json          # full result; read summary for counts

# batch: many queries, one parse+elaborate; one JSONL frame per stdin line
printf 'trace -s ready --scope top.u_dma\nfanin -s data_out --scope top.u_pipe\nlint --rules cdc\n' \
  | rtlscanner batch -f rtl.f --json
```

## Inputs & syntax

- **Prefer `-f/--filelist` for a real project**; use `-d/--dir` only for examples or
  small ad-hoc trees. When a filelist is present, `-d` and positional sources are
  **ignored** (a stderr note says so) — deliberate, so a stray `-d .` can't pull
  sim/testbench dirs into the compile.
- **`--config` is a subcommand flag**, e.g. `rtlscanner tree --config proj.toml`. If
  unset, `rtlscanner` tries `./.rtlscanner.toml` in CWD (no walk-up). Priority is
  `CLI > env (RTLSCANNER_*) > config > defaults`, field by field.
- **Repeatable flags** (`-d`, `-f`, `--exclude`, `--rules`) take a
  comma list or repetition: `-d a,b` ≡ `-d a -d b`.
- **Per-file compilation by default.** Each source file is its own compilation unit
  (slang/VCS/Verilator behavior), so a `` `define `` or `$unit`-scoped `typedef` in one
  file is **not** visible to the next. If a project intentionally shares `$unit`-scope
  declarations across the filelist, add **`--single-unit`** to compile it all as one
  unit. A design that does not compile is a `COMPILE_FAILED` error for
  `tree`/`scope`/`xref`/`trace`/`fanin`/`fanout` (`status:"error"`, real compiler
  diagnostics in `diagnostics[]`) — never a phantom result, so reading `status` first
  tells you the structure is trustworthy. (`lint` reports the same errors as findings
  and stays `status:"ok"`.)
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

**Lint in CI.** `rtlscanner lint -f rtl.f --json` runs all five categories and exits 1
on any error-level finding. Keep noisy third-party sources out with
`--exclude '**/third_party/**'`; for finer filtering, filter the JSON by the `module`
field.

## lint categories (quick form)

`lint` runs a closed set of five categories: `semantic`, `unused`, `port`, `cdc`,
`comb-loop`. `--rules` is a whitelist that replaces the default — accepts only those five
names plus `all` (no flag = all five). `--rules unused,cdc` runs exactly those; an
out-of-set token (`default`, `width-*`, a rule name) errors with the valid list. For
compile/front-end errors only, use `--rules semantic`; for child-instance port issues,
`--rules port`. Each finding's `check` is its category and its severity is fixed — there
are no `--skip` / `--waive` / `--strict` / `--min-severity` knobs.

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
- **`lint` exits 1 on any error-level finding.** A non-zero exit is not a crash — still
  read the JSON envelope.
- **Lint can be noisy on mature RTL — don't dump 100+ findings into context.** Narrow with
  `--rules` (e.g. `--rules cdc,comb-loop` for just the cross-hierarchy graph checks), and
  read the summary (`summary.by_check` / `summary.by_severity` / `summary.has_error`)
  rather than the full `findings[]`. For the complete result, redirect `--json` to a file
  (`--json > lint.json`) — the `summary` still carries the true totals.
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
- **`batch` amortizes the load — use it for 3+ queries on one design.** Pipe one
  `<subcmd> [flags]  # label` per line into `rtlscanner batch -f rtl.f --json`; the design
  is parsed + elaborated **once** and each line streams back a JSONL frame
  `{id, ok, result}` (`result` is that command's normal envelope; a `# label` becomes the
  `id`, else a 1-based sequence number). Put the input flags (`-f`/`-d`/…) and `--json` on
  the `batch` line, only the per-query flags per line. A failing query is isolated
  (`{id, ok:false, error}`) and the batch still **exits 0** — a non-zero exit means the
  design itself could not be loaded. Blank lines and `#`-comment lines are skipped.
  Two batch-specific gotchas: `ok` means *the query ran*, so a `lint` frame is `ok:true`
  even with error-severity findings (its single-command exit-1 signal is **not**
  propagated — read `result.summary.has_error` of the `lint` frame); and a failed query's
  `error` is a plain string, without the `errors[].details` recovery hints a single
  command emits.

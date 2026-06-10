# RTLScanner

RTLScanner wraps pyslang's SystemVerilog parsing, elaboration, and
analysis capabilities into an agent-friendly CLI for RTL inspection and
debug. It provides terminal and JSON workflows for hierarchy, scope
contents, signal tracing, dataflow, lint findings, and xrefs.

Use `rtlscanner <subcommand>`:

| Subcommand | Purpose | Typical stage |
|------------|---------|---------------|
| `tree`     | Hierarchy viewer & filelist exporter      | Architecture / code organisation |
| `scope`    | Direct contents of one elaborated scope    | Architecture / debug |
| `trace`    | Single-signal driver & load analyzer      | Simulation / debug |
| `fanin`    | Upstream dataflow BFS from a signal       | Simulation / debug |
| `fanout`   | Downstream dataflow BFS from a signal     | Simulation / debug |
| `lint`     | Static linter (semantic + analysis + port checks) | Code review / CI |
| `xref`     | Symbol definitions and references         | Simulation / debug / code review |

## Install

```bash
pip install -e .
rtlscanner --help
```

Requires Python 3.8+ and pyslang. `pip install -e .` installs pyslang
and the other declared dependencies.

## Configuration

Use `-d/--dir`, `-f/--filelist`, and `--exclude` for source inputs.
Project-stable inputs such as filelist root and prefix tokens can live
in env vars or a project config file.

**Priority:** `CLI > env vars > selected config > built-in defaults`
(field-level override; not whole-layer).

### Environment variables

| Variable                | Maps to                          |
|-------------------------|----------------------------------|
| `RTLSCANNER_CONFIG`     | project config `.toml` file      |
| `RTLSCANNER_FILELIST`   | repeat of `-f` (colon-separated) |
| `RTLSCANNER_DIR`        | repeat of `-d` (colon-separated) |
| `RTLSCANNER_EXCLUDE`    | repeat of `--exclude` (colon-separated) |
| `RTLSCANNER_ROOT`       | base for `.f` relative paths (no CLI equivalent) |
| `RTLSCANNER_PREFIX`     | substituted prefix in `.f` files (default `${PROJPATH}`) |

### `./.rtlscanner.toml`

Use `--config FILE` or `RTLSCANNER_CONFIG` to select a project config
file. If neither is set, `rtlscanner` tries `./.rtlscanner.toml` in CWD
(no walk-up). Config files provide shared defaults for all subcommands
so repeated project arguments do not need to be typed every run. The
`--config` flag is a subcommand option, for example
`rtlscanner tree --config rtlscanner.toml`.

```toml
[inputs]
filelist = ["rtl/top.f"]
root     = "."
prefix   = "${PROJPATH}"
exclude  = ["**/sim/**", "**/dvt/**"]

[lint]
rules = ["default", "cdc"]            # equivalent to CLI --rules
skip  = ["case-default"]              # equivalent to CLI --skip
waive = ["dbg_*", "third_party_*"]    # module-name globs (suppress entire modules)

[lint.severity]                       # promote individual rules
"width-trunc" = "error"

[lint.cdc]
reset = ["nrst_*", "por_*"]           # extra reset-signal name globs

[xref]
path_style = "relative"                # relative | absolute | name
```

### Filelist precedence over dir scan

When a filelist is present (CLI, env, or config), positional sources and
`-d/--dir` are ignored with a stderr note. This avoids the common mistake
of running a `-d .` alongside a proper `.f` and pulling sim/testbench
directories into the compilation.

### List-valued flags

All repeatable flags (`-d`, `-f`, `--exclude`, `--rules`, `--skip`,
`--waive`) accept either a comma-list or repetition:

```bash
rtlscanner tree -d ./rtl,./common
rtlscanner tree -d ./rtl -d ./common         # equivalent
rtlscanner lint --rules width-trunc,case-default
rtlscanner lint --rules '[width-trunc,case-default]'   # bracket-style
```

## `rtlscanner tree` — hierarchy viewer

```bash
rtlscanner tree -d ./rtl                           # auto-pick top
rtlscanner tree -d ./rtl --top cpu --depth 2       # constrain
rtlscanner tree -d ./rtl --stats                   # module usage stats
rtlscanner tree -d ./rtl --flat                    # one path per line
rtlscanner tree -d ./rtl --export rtl.f            # export resolved filelist
rtlscanner tree -d ./rtl --json                    # agent envelope
```

`--export FILE` writes the resolved filelist and exits; combine with
`--path-style {rel,abs,prefix}` to control how paths appear in the output.

## `rtlscanner trace` — single-signal driver/loads

```bash
rtlscanner trace -d ./rtl -s q --scope top.u_dp
rtlscanner trace -f rtl.f -s clk --scope top --filter 'u_fifo*'
rtlscanner trace -d ./rtl -s a --scope top --cross    # follow ports
```

`--scope` auto-detects when there's a single top module.

## `rtlscanner scope` — direct scope contents

```bash
rtlscanner scope -d ./rtl --scope top.u_dp
rtlscanner scope -d ./rtl --scope top.u_dp --signals
rtlscanner scope -d ./rtl --scope top.u_dp --connections
rtlscanner scope -d ./rtl --scope top.u_dp --typedefs
```

With no section flag, `scope` reports the selected scope's ports, local
non-port signals, direct child instances, and elaborated params. Add a
section flag to narrow the output. `--connections` reports direct child
instance port maps; `--typedefs` reports local typedef, enum, struct, and
union declarations.

## `rtlscanner fanin` / `fanout` — dataflow BFS

```bash
rtlscanner fanin  -d ./rtl -s result --scope top.u_dp
rtlscanner fanout -d ./rtl -s sel    --scope top.u_dp --depth 6
```

BFS over `port_connection`, `continuous_assign`, and `procedural` edges
from the starting signal. `--depth` (default 4) caps traversal.

Reading the output:

| Term      | Meaning |
|-----------|---------|
| **node**  | A signal at one elaborated hierarchical path. The starting signal is depth 0. |
| **edge**  | Directed dataflow link `source → target`. `kind` is `port_connection`, `continuous_assign`, or `procedural`. |
| **depth** | BFS distance in hops from the starting signal. |

## `rtlscanner xref` — source cross-reference lookup

```bash
rtlscanner xref -d ./rtl -s ready --scope top.u_dma
rtlscanner xref -d ./rtl --name state --scope top.u_ctrl
rtlscanner xref -d ./rtl -s valid --scope top --recursive
rtlscanner xref -d ./rtl --module fifo
rtlscanner xref -d ./rtl --module lane --scope top.u_phy
```

`xref` is a source-location lookup for two common questions: where a
signal/symbol is declared and referenced, and where a module is declared
and instantiated. It reports file, line, and column positions so agents
and editors can jump to source quickly.

Signal xref reports the elaborated definition, port/internal-symbol
aliases when applicable, and references found through pyslang's analyzed
read/write sets plus child instance port connections. Use `--scope` to
anchor the query; add `--recursive` only when you want to find same-named
symbols in descendant instances as separate matches.

Module xref reports module declaration sites and elaborated instance
sites. With `--scope`, instance results are limited to that hierarchy
subtree; declarations are still reported globally.

By default, human-readable output prints only the definition/reference
locations and minimal labels. Add `--verbose` for extra context such as
types, parent modules, and parameter values. JSON output always preserves
the structured details.

Path style is configured in `.rtlscanner.toml` under `[xref]`:

```toml
[xref]
path_style = "relative"   # ./path/from/root.sv; also supports absolute, name
```

The `relative` style is relative to `[inputs].root` and is printed with
an explicit `./` prefix.

Typical use: after a waveform search finds a suspicious signal, ask
`xref` where that signal is declared, which procedural/continuous blocks
read or write it, and which child instance ports it feeds or is driven by.

## `rtlscanner lint` — static linter

Built on pyslang's elaboration + analysis engine. Catches width
mismatches, unused/undriven signals and ports, missing case defaults,
inferred latches, multi-driven nets, plus opt-in CDC and port connection
analysis.

```bash
rtlscanner lint -d ./rtl                              # default rule set
rtlscanner lint -d ./rtl --rules default,cdc          # add CDC
rtlscanner lint -d ./rtl --rules width-trunc          # only this rule
rtlscanner lint -d ./rtl --rules default --skip case-default
rtlscanner lint -d ./rtl --waive 'dbg_*'              # skip modules
rtlscanner lint -d ./rtl --strict                     # CI gate: warning → error
rtlscanner lint -d ./rtl --min-severity error         # display floor
rtlscanner lint -d ./rtl --rules port-connect         # instance port issues
```

### Rule selection model

`--rules SPEC[,SPEC...]` — white list. SPEC can be:

- a rule name: `width-trunc`, `unused-port`, …
- a family alias: `semantic`, `unused`, `shadow`, `cdc`, `port-connect`
- a warning option: `everything` (enable slang's broader warning set)
- a glob: `width-*`
- a meta value: `default` (= `semantic + unused`), `all`, `none`

`semantic` is the normalized slang diagnostic stream. It includes parse,
preprocessor, type, binding, and elaboration diagnostics that slang emits;
for example missing includes, undeclared identifiers, width truncation,
and port-connection diagnostics. `everything` is not a finding family and
will never appear as `check="everything"`; it only changes slang warning
configuration.

`--skip RULE[,...]` — subtract from the resulting set (glob ok).

### Waivers

`--waive MODULE[,MODULE...]` suppresses every finding in matching modules
(file-basename glob). For project-permanent waivers, put the list in
`[lint] waive = [...]` in `.rtlscanner.toml`.

### CDC

`--rules cdc` enables flop-to-flop clock-domain-crossing detection.
Findings appear as regular entries with `rule="cdc-crossing"`,
`check="cdc"`. Customize the reset-signal recognition in
`[lint.cdc] reset = [...]`.

Note: inline diagnostic pragmas do **not** suppress `cdc-crossing`.
Use `--waive` or `[lint] waive` instead.

### Port connections

`--rules port-connect` reports unconnected child instance ports and port
width mismatches as lint findings.

### Exit codes

- `0` — no error-level findings
- `1` — one or more error-level findings, or `--strict` with any finding
- `2` — usage / source error

## Agent / JSON Mode

All subcommands accept `--json`, producing a single uniform envelope:

```json
{
  "tool":        "tree",
  "version":     "<tool-version>",
  "status":      "ok" | "error",
  "command":     { /* parsed CLI args, output flags stripped */ },
  "data":        { /* subcommand-specific payload */ } | null,
  "diagnostics": [ /* parser warnings/notes */ ],
  "errors":      [ /* structured errors when status == "error" */ ],
  "summary":     { /* subcommand-specific counts */ } | null
}
```

Each subcommand ships a JSON Schema (draft-07) — dump it with
`rtlscanner <subcmd> --schema`. See `examples/agent/README.md` and the
`examples/agent/schemas/` directory for the full contract and pre-baked
schemas.

Failure is structured too — an `INPUT_NOT_FOUND` / `COMPILE_FAILED` /
`SCOPE_NOT_FOUND` / `SIGNAL_NOT_FOUND` / `BAD_FILELIST` / `BAD_CONFIG`
/ `NO_TOP` / `INTERNAL_ERROR` envelope is printed to stdout (with
non-zero exit code), never a raw stack trace.

## Code Structure

| Area | Files |
|------|-------|
| CLI and JSON envelope | `src/rtlscanner.py`, `src/rtl_cli.py`, `src/agent_json.py` |
| Inputs and compilation | `src/rtl_config.py`, `src/rtl_common.py`, `src/rtl_slang.py` |
| RTL analysis commands | `src/rtl_tree.py`, `src/rtl_scope.py`, `src/signal_trace.py`, `src/rtl_lint.py`, `src/rtl_xref.py` |
| Agent examples and contracts | `examples/agent/` |

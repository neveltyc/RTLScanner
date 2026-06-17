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

See [CHANGELOG.md](CHANGELOG.md) for release notes and version history.

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
waive = ["dbg_*", "module:fifo", "file:third_party_*"]  # bare=module|file; prefix to disambiguate

[lint.severity]                       # promote individual rules
"width-trunc" = "error"

[lint.cdc]
reset = ["nrst_*", "por_*"]           # extra reset-signal name globs

[xref]
path_style = "relative"                # relative|absolute|name (rel/abs aliases ok)

[flow]
unroll     = true                      # prune constant if/case + unroll constant loops (default on)
max_unroll = 2048                      # cap on total unrolled iterations per block
```

### Filelist precedence over dir scan

When a filelist is present (CLI, env, or config), positional sources and
`-d/--dir` are ignored with a stderr note. This avoids the common mistake
of running a `-d .` alongside a proper `.f` and pulling sim/testbench
directories into the compilation.

### Compilation units

Each source file is compiled as its **own** compilation unit (matching slang's
driver, VCS, and Verilator). A `` `define `` or other `$unit`-scoped declaration
in one file is therefore **not** visible to the next — this is what lets the
linter surface a genuine missing-`` `define ``/undeclared-identifier bug instead
of masking it. To share macros or types across files, make it explicit: pass
macros via `+define+`, `` `include `` a header from each file that needs it, or
put shared declarations in a `package`. A header merely listed first in the
filelist does **not** leak into the files after it.

If a project legitimately relies on a single compilation unit — e.g. a leading
file declares `$unit`-scoped `typedef`/`localparam`s used across the whole
filelist — pass **`--single-unit`** to compile the entire file list as one unit
(the slang `--single-unit` / pre-0.2.0 model), restoring cross-file `$unit`
visibility:

```bash
rtlscanner tree -f files.f --single-unit
```

When a design does **not** compile, the structural commands (`tree`, `scope`,
`xref`, `trace`, `fanin`, `fanout`) fail loudly instead of returning a phantom
result built from the compiler's error-recovery AST: `status` is `"error"`,
`errors[0].code` is `COMPILE_FAILED`, and the real compiler diagnostics (with
severity, file, line, and column) are listed in `diagnostics[]`. `lint`, whose
job *is* to report those errors, still returns `status:"ok"` with them as
findings.

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
`--path-style` to control how paths appear in the exported filelist: `relative` (alias `rel`, default), `absolute` (alias `abs`), or `prefix` (`${PROJPATH}/<relative>`). Both the long and short spellings are accepted; the long forms match the `xref` `path_style` config (which instead offers a `name` = bare-basename mode in place of `prefix`).

## `rtlscanner trace` — single-signal driver/loads

```bash
rtlscanner trace -d ./rtl -s q --scope top.u_dp
rtlscanner trace -f rtl.f -s clk --scope top --filter 'u_fifo*'
rtlscanner trace -d ./rtl -s u_dp.q --scope top       # dotted -s accepted
rtlscanner trace -d ./rtl -s 'status[3]' --scope top  # bit-select (quote it in a shell)
```

`--scope` auto-detects when there's a single top module. A dotted `-s`
value (`u_dp.q`, or an absolute `top.u_dp.q`) is reinterpreted as
signal + scope, with a note in stderr/`diagnostics`.

`trace` is scope-local: it reports the immediate driver and loads within
one scope. To follow a value **across** module/port boundaries, use
`fanin`/`fanout`, which traverse port connections.

Multiple drivers are reported with the bit range each one covers
(`bits: "[3]"` / `"[7:4]"` in JSON). The `MULTI-DRIVER` warning
(`multi_driver_warning` in JSON) fires only when ranges actually
overlap — per-bit generate outputs are legal single-driver RTL.

A **bit-select** on `-s` (`status[3]`, `status[7:4]`) narrows the report to
the driver(s) **and** loads that actually touch those bits — "where does this
bit come from, and who reads it". The queried range appears as `bit_select` in
JSON; each load carries the sub-range it reads (`bits`). It works on `trace`,
`fanin`, and `fanout` alike.

Analysis results are resolved through slang's canonical instance bodies,
so identical sibling instances (`u_dp0`/`u_dp1`) and generate-array
elements (`gen_arr[2].u_lane`) report the same drivers/loads as the
canonical copy, with hierarchical paths remapped to the queried
instance.

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
rtlscanner fanin  -d ./rtl -s result --scope top.u_dp --summary   # counts only
rtlscanner fanin  -d ./rtl -s result --scope top.u_dp --depth 1   # direct sources
```

BFS over `port_connection`, `continuous_assign`, and `procedural` edges
from the starting signal. `--depth` (default 4) caps traversal. The graph is
expanded on demand — only the neighborhood the BFS actually visits is built —
so a shallow query stays cheap on a large design instead of paying to construct
the whole-design flow graph first.

Procedural edges are **per-statement**: an assignment's RHS feeds only its own
LHS, while a block's `if`/`case`/loop conditions feed every driver in the
block. So `--depth 1` is the true direct fan-in, not every signal the block
reads.

A cone can be large on a real design. `--summary` replaces the full
`nodes`/`edges` with counts, an `edges_by_depth` histogram, and the `direct`
(depth-1) neighbors — the agent-friendly view.

**Bit-level dataflow.** Edges carry the bit sub-range each read/drive touches
(`source_bits` / `target_bits` in JSON, e.g. `top.a[2] → top.dout[5]`), so the
graph answers *which bit comes from which*. A `-s` bit-select then traverses
only the edges touching those bits and maps the range across each hop:

```bash
rtlscanner fanin  -d ./rtl -s 'dout[5]' --scope top   # converges to the exact driving bit
rtlscanner fanout -d ./rtl -s 'a[7:4]'  --scope top   # only where that nibble goes
```

Precision matches the RTL: bit/part-selects, concatenation, mux (`?:`), bitwise
`& | ^ ~`, truncation, and zero/sign-extend are exact; arithmetic (`a + b`),
shifts, reductions, comparisons, and dynamic indices (`a[i]`) fall back to the
whole signal — conservative, so a cone is never under-reported. Bit precision
applies to little-endian `[N:0]` packed vectors (the common case); big-endian
`[0:N]` vectors and multi-bit-element packed arrays (`[3:0][7:0]`) are handled
at signal granularity (also conservative). Whole-signal queries (no bit-select)
are unchanged. Bit ranges are shown only for proper sub-ranges (additive
output).

**Constant pruning & loop unrolling** (on by default; `--no-unroll` to disable).
Before building procedural edges the block is walked structurally: an `if`/`case`
whose condition folds to a compile-time constant keeps only the live branch
(dead-branch reads/assignments never become edges), and a `for`/`repeat` loop
with constant bounds is unrolled with the loop variable bound — so a windowed
access like `hi[i] = a[i+2]` recovers the exact slice (`hi ← a[3:2]`) instead of
blurring to the whole signal, and the loop variable stops appearing as a
spurious read. `while`/`do-while`/`foreach`, non-constant conditions/bounds, and
any loop exceeding `--max-unroll N` (default 2048 total iterations per block)
fall back to the conservative whole-signal handling, so a cone is never
under-reported. `--no-unroll` restores the pre-pass behaviour byte-for-byte.

```bash
rtlscanner fanin -d examples/unroll -s q  --scope prune              # dead branch dropped
rtlscanner fanin -d examples/unroll -s hi --scope window             # hi ← a[3:2], precise
rtlscanner fanin -d examples/unroll -s q  --scope prune --no-unroll  # conservative baseline
```

Reading the output:

| Term      | Meaning |
|-----------|---------|
| **node**  | A signal at one elaborated hierarchical path. The starting signal is depth 0. |
| **edge**  | Directed dataflow link `source → target`. `kind` is `port_connection`, `continuous_assign`, or `procedural`. A registered edge (driven by `always_ff`, a latch, or an edge-sensitive `always`) also carries `clocked: true`. |
| **bits**  | `source_bits` / `target_bits`: the bit sub-range the edge reads / drives, e.g. `a[2] → dout[5]`. Absent when the whole signal is touched. |
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
path_style = "relative"   # ./path/from/root.sv; also absolute, name (rel/abs aliases ok)
```

The `relative` style is relative to `[inputs].root` and is printed with
an explicit `./` prefix.

Typical use: after a waveform search finds a suspicious signal, ask
`xref` where that signal is declared, which procedural/continuous blocks
read or write it, and which child instance ports it feeds or is driven by.

## `rtlscanner lint` — static linter

Built on pyslang's elaboration + analysis engine. Catches width
mismatches, unused/undriven signals and ports, missing case defaults,
inferred latches, multi-driven nets, plus opt-in clock-domain-crossing,
combinational-loop, and port connection analysis. The CDC and
combinational-loop checks run on the same dataflow flow graph the
`fanin`/`fanout` commands use, so they are cross-hierarchy.

```bash
rtlscanner lint -d ./rtl                              # default rule set
rtlscanner lint -d ./rtl --rules default,cdc          # add CDC
rtlscanner lint -d ./rtl --rules default,comb-loop    # add combinational loops
rtlscanner lint -d ./rtl --rules bugs                 # real bugs only (high precision)
rtlscanner lint -d ./rtl --rules width-trunc          # only this rule
rtlscanner lint -d ./rtl --rules default --skip case-default
rtlscanner lint -d ./rtl --waive 'dbg_*'              # bare: module|file (or module:/file: prefix)
rtlscanner lint -d ./rtl --strict                     # CI gate: warning → error
rtlscanner lint -d ./rtl --min-severity error         # display floor
rtlscanner lint -d ./rtl --rules port-connect         # instance port issues
```

### Rule selection model

`--rules SPEC[,SPEC...]` — white list. SPEC can be:

- a rule name: `width-trunc`, `unused-port`, …
- a family alias: `semantic`, `unused`, `shadow`, `cdc`, `port-connect`, `comb-loop`
- a warning option: `everything` (enable slang's broader warning set)
- a glob: `width-*`
- a meta value: `default` (= `semantic + unused`), `all`, `none`, `bugs`

`semantic` is the normalized slang diagnostic stream. It includes parse,
preprocessor, type, binding, and elaboration diagnostics that slang emits;
for example missing includes, undeclared identifiers, width truncation,
and port-connection diagnostics. `everything` is not a finding family and
will never appear as `check="everything"`; it only changes slang warning
configuration.

`--skip RULE[,...]` — subtract from the resulting set (glob ok).

A typo'd `--rules`/`--skip` token (e.g. `bugz`) now emits a `note` in
`diagnostics` with a did-you-mean suggestion, instead of silently selecting
nothing. A valid rule that simply has no findings this run is **not** flagged.

### `bugs` — real bugs only

`--rules bugs` is a curated, high-precision preset for "are there real bugs
in this design?" It keeps the rules that flag functional defects:

- `inferred-latch` — an unintended level-sensitive latch
- `unassigned-variable` — a variable read but never driven (reads as X)
- `undriven-port` — an output port never driven
- `port-width-mismatch` / `port-width-trunc` — child-instance port width problems
- `width-trunc` — implicit truncation in an assignment

…plus every hard compile **error** (whose codes are open-ended), while
dropping style noise (`unused-*`, `case-default`, `empty-output-connection`,
the `port-unconnected` note). `cdc-crossing` is intentionally excluded
(heuristic, higher false-positive rate); compose it back with
`--rules bugs,cdc`.

### Waivers

`--waive GLOB[,GLOB...]` suppresses findings. Each finding is attributed to its
**module** (design unit) by source range, and a glob may carry a target prefix
to say exactly what it matches:

| Token | Matches |
|-------|---------|
| `dbg_*` (bare) | the **module** name **or** the source-file basename (backward-compatible union) |
| `module:fifo` | the **module** name only — never a sibling module in the same file |
| `file:third_party_*` | the **source-file** basename only; also waives findings with **no module** (`$unit`-scope / preprocessor / file-level compile errors), which a module glob can't reach |
| `scope:top.u_dbg` | **reserved** — instance/hierarchy-level waivers are future work; currently ignored with a note |

Use a bare glob for the common one-module-per-file case; reach for `module:` /
`file:` when a file declares several modules (or is named after one of them) and
you need to be precise. The JSON `waived_reason` names the matched token, e.g.
`waived ('module:fifo')`. For project-permanent waivers, put the list in
`[lint] waive = [...]` in `.rtlscanner.toml`.

### CDC

`--rules cdc` enables clock-domain-crossing detection. Findings appear as
regular entries with `rule="cdc-crossing"`, `check="cdc"`. Customize the
reset-signal recognition in `[lint.cdc] reset = [...]`.

The check runs on the dataflow flow graph (the same one `fanin`/`fanout` use),
so it is **cross-hierarchy**: a launch flop that feeds — through combinational
logic only — the data input of a capture flop in a *different* clock domain is
flagged even when the two flops live in different modules wired through ports.
Each flop's clock is resolved to its **source net** before domains are compared,
so two flops on the same physical clock are one domain even when their local
clock ports are named differently (e.g. `clk` vs `clock`) or sit in different
instances — and, conversely, one net reaching two differently-named ports is one
domain, not two. A **gated or divided** clock (`assign gclk = clk & en;`, a
clock-divider flop) is treated as its own domain.

Note: inline diagnostic pragmas do **not** suppress `cdc-crossing` (it is a
heuristic, not a slang diagnostic). Use `--waive` or `[lint] waive` instead.

### Combinational loops

`--rules comb-loop` enables combinational-loop detection. Findings appear as
regular entries with `rule="comb-loop"`, `check="comb-loop"`. The check runs
cycle detection (Tarjan strongly-connected components) over the **non-sequential
edges** of the same flow graph — a registered edge (driven by `always_ff`, a
latch, or an edge-sensitive `always`) breaks the feedback, so legitimate
sequential feedback is not flagged while a true combinational cycle
(`assign a = b; assign b = a;`, an `always_comb` that reads its own output) is.
Loops that close through child-instance ports are caught (cross-hierarchy).

`comb-loop` is opt-in and reported at `warning` severity; like `cdc` it is
excluded from `default` and the `bugs` preset because it inherits the flow
graph's conservative control-condition modeling and can, in rare cases,
over-report. Compose it in with `--rules bugs,comb-loop` or `--rules default,comb-loop`,
and promote it with `[lint.severity] "comb-loop" = "error"` or `--strict` in CI.

Every dataflow edge in `fanin`/`fanout` output now carries a `clocked` boolean
(present only when true) marking the registered edges this classification uses.

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

### Output capping (`--limit`)

Every subcommand accepts `--limit N` (default `200`, `0` = unlimited): each
emitted list is clipped to at most `N` rows so a query against a large design
stays agent-friendly instead of dumping thousands of entries. Count fields keep
reporting the **true** totals — only the list is shortened — and a
`summary.truncated` boolean (alongside `summary.limit`) tells the caller more
exists. Human-mode output appends a one-line note, e.g.
`... truncated: 3/11 instances shown. (use --limit 0 to see all)`. This caps the
flat/list outputs (`lint` findings, `xref` references/instances, `scope`
sections, `tree --flat`, `fanin`/`fanout` edges, `trace` loads); the nested
`tree` JSON hierarchy is capped by total node count.

Failure is structured too — an `INPUT_NOT_FOUND` / `COMPILE_FAILED` /
`SCOPE_NOT_FOUND` / `SIGNAL_NOT_FOUND` / `BAD_FILELIST` / `BAD_CONFIG`
/ `NO_TOP` / `INTERNAL_ERROR` envelope is printed to stdout (with
non-zero exit code), never a raw stack trace.

A design that does not compile is a `COMPILE_FAILED` error for every
structural command (`tree` / `scope` / `xref` / `trace` / `fanin` /
`fanout`): they return `status:"error"` with the compiler diagnostics in
`diagnostics[]` rather than a phantom structure recovered from a broken
parse, so reading `status` first is enough to know the result is
trustworthy. (`lint` is the exception — surfacing those diagnostics is its
job, so it returns `status:"ok"` with them as findings.)

`*_NOT_FOUND` errors carry machine-readable recovery hints in
`errors[0].details` so one failed call is enough to self-correct:

```json
{
  "code": "SCOPE_NOT_FOUND",
  "message": "scope 'top.u_dpX' not found; did you mean: u_dp0, u_dp1; …",
  "details": {
    "valid_prefix": "top",
    "failing_component": "u_dpX",
    "close_matches": ["u_dp0", "u_dp1"],
    "children": ["u_dp0", "u_dp1", "u_extra_reg"]
  }
}
```

`SIGNAL_NOT_FOUND` details list `close_matches` and the `available`
signal names in the resolved scope (capped at 20). The same hints are
appended to human-mode error messages.

## Code Structure

| Area | Files |
|------|-------|
| CLI and JSON envelope | `src/rtlscanner.py`, `src/rtl_cli.py`, `src/agent_json.py` |
| Inputs and compilation | `src/rtl_config.py`, `src/rtl_common.py`, `src/rtl_slang.py` |
| RTL analysis commands | `src/rtl_tree.py`, `src/rtl_scope.py`, `src/signal_trace.py`, `src/rtl_lint.py`, `src/rtl_xref.py` |
| Agent examples and contracts | `examples/agent/` |

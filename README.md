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
| `driver`   | Driver value-logic: branches, operands, timing | Simulation / debug / root-cause |
| `fanin`    | Upstream dataflow BFS from a signal       | Simulation / debug |
| `fanout`   | Downstream dataflow BFS from a signal     | Simulation / debug |
| `path`     | Point-to-point dataflow path between two nodes | Simulation / debug / timing |
| `lint`     | Static linter (semantic + analysis + port checks) | Code review / CI |
| `xref`     | Symbol definitions and references         | Simulation / debug / code review |
| `find`     | Design-wide node lookup by glob/regex pattern | Architecture / debug |
| `batch`    | Run many of the above against one loaded design | Agent / scripted workflows |

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

All repeatable flags (`-d`, `-f`, `--exclude`, `--rules`) accept either a
comma-list or repetition:

```bash
rtlscanner tree -d ./rtl,./common
rtlscanner tree -d ./rtl -d ./common         # equivalent
rtlscanner lint --rules unused,cdc
rtlscanner lint --rules '[unused,cdc]'        # bracket-style
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

## `rtlscanner driver` — driver value-logic

Where `trace` locates a signal's driver, `driver` returns its *value logic*:
for each driver, the branch structure (the if/case guard chain with polarity),
each branch's RHS operands (with bit ranges and hierarchical paths), and — for
sequential drivers — the clock/reset timing extracted from the sensitivity list.
This is the elaborated structure a waveform-aware "why is S this value at T"
analysis joins with runtime values.

```bash
rtlscanner driver -d ./rtl -s q --scope top.u_dp --json
```

Each driver reports `timing` (`sequential` with `clock`/`clock_edge`/`reset`/
`reset_edge`, or `combinational`/`latch`) and an `assignments` list; each
assignment carries `rhs_text`, `rhs_operands` (`{name, path, bits}`), and
`guards` (the `if`/`case` conditions plus polarity that make that branch active).
Reset detection is a name heuristic (marked `heuristic`); operands and branch
structure are exact. Sequential timing and reset classification are best-effort
and flagged accordingly.

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

**Combinational cone (`--comb`).** Stops the BFS at sequential (registered)
edges, so the result is the *pure combinational* fan-in/out bounded by
flip-flops — the BFS refuses to enter a register node. This is the cone for
timing-path reasoning: "what combinational logic feeds this register's D",
"where does this signal go before the next flop".

```bash
rtlscanner fanin  -d ./rtl -s data_q --scope top.u_pipe --comb   # comb logic into the flop's D
rtlscanner fanout -d ./rtl -s sel    --scope top.u_dp   --comb   # where sel goes before a register
```

A register *node* is the boundary: the cone never crosses **into** one, so the
boundary flops are excluded — except the starting signal, which is always
expanded, so a `--comb fanin` from a register output still reports that flop's
own combinational D-cone (with the `clocked` D→Q edge as the boundary). Because
registers bound the cone, `--comb` defaults to **unbounded** depth (`max_depth`
then reports the deepest hop reached); pass an explicit `--depth N` to cap it.
A `clocked: true` edge marks the registered boundary; `--comb` reports a
`comb: true` flag in JSON. (`--depth` reports as `null` in the `command` echo
when left at its default — the effective bound is always `data.max_depth`.)

The boundary is at signal granularity: a node with **any** registered driver is
a boundary, so a signal that is part-`assign`ed and part-latched is excluded
whole rather than split — a conservative simplification that never crosses a
flop.

**Bit-level dataflow.** Edges carry the bit sub-range each read/drive touches
(`source_bits` / `target_bits` in JSON, e.g. `top.a[2] → top.dout[5]`), so the
graph answers *which bit comes from which*. A `-s` bit-select then traverses
only the edges touching those bits and maps the range across each hop:

```bash
rtlscanner fanin  -d ./rtl -s 'dout[5]' --scope top   # converges to the exact driving bit
rtlscanner fanout -d ./rtl -s 'a[7:4]'  --scope top   # only where that nibble goes
```

A copy whose per-bit offset *varies* — a bit reversal `rev[i] = din[7-i]`, a half
swap `o = {a[3:0], a[7:4]}` — is a permutation no single offset can express, so
the edge carries a `segments` array of `{source_bits, target_bits}` sub-copies:
`fanout din` shows `din[7] → rev[0]`, `din[6] → rev[1]`, …, and a bit-select
trims the map to just what it asked for (`fanin rev[0]` → only `din[7] → rev[0]`).

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
| **segments** | For a per-bit permutation (reversal / swap), the list of `{source_bits, target_bits}` sub-copies, e.g. `din[7] → rev[0]`. Absent for single-offset and whole-signal edges. |
| **depth** | BFS distance in hops from the starting signal. |

## `rtlscanner path` — point-to-point dataflow path

```bash
rtlscanner path -d ./rtl --from a --to y0 --scope top              # any path
rtlscanner path -d ./rtl --from u_dp.q --to result --scope top     # dotted names
rtlscanner path -d ./rtl --from top.u_a.x --to top.u_b.y           # absolute paths
rtlscanner path -d ./rtl --from q --to result --scope top.u_dp --comb  # comb only
```

Where `fanin`/`fanout` report the whole cone *around* a signal, `path` answers a
narrower question: **is there a dataflow path between these two specific nodes,
and what is it?** The output is the path itself — the alternating node → edge →
node sequence from `--from` to `--to`, each edge carrying its kind, source
location, and description (an input/output port connection, a continuous
`assign`, or a procedural block), plus the driven bit range when it is a
sub-range.

**Directional.** The path follows dataflow *forward*: `--from` must drive
`--to`. There is no path the other way unless the design also wires it that way,
so `path --from y --to a` on `assign y = a` finds nothing.

**Mechanism — DFS + parent map + backtrack.** A single depth-first search runs
from the start node over the same demand-driven dataflow graph `fanin`/`fanout`
traverse (so a path crosses port boundaries and hierarchical references the same
way). The DFS records, for each node, the edge by which it was *first* reached;
the path to the end node is then read back along those parent pointers and
reversed. The Python API is `PathFinder.find()` / `PathFinder.findComb()` on the
dataflow engine.

**No path is a normal result.** When the two nodes are not connected (in the
`--from → --to` direction), the result is a successful, empty path —
`status:"ok"` with `found:false` and empty `nodes`/`edges`, never an error.
`--from == --to` is a found, zero-hop single-node path.

**Combinational path (`--comb`).** Restricts the search to a *purely
combinational* path: the DFS never enters a register (sequential) node — the
same flip-flop boundary `--comb` fan-in/out uses. A path that exists only
*through* a register therefore disappears under `--comb`, while a register
*start* still finds its own combinational fan-out path (the start is the DFS
seed, always expanded). This is the query for "do these two points sit in the
same timing path (no flop between them)?".

Reading the output:

| Term     | Meaning |
|----------|---------|
| **found** | Whether a path exists. `false` (empty `nodes`/`edges`) is a normal result — the nodes are not connected in the queried direction (or, with `--comb`, only through a register). |
| **nodes** | The ordered node sequence from start to end (elaborated hierarchical paths); `nodes[i]` →`edges[i]`→ `nodes[i+1]`. |
| **edges** | The dataflow edges between the nodes (one fewer than `nodes`); same shape as `fanin`/`fanout` (`kind`, `description`, `file`/`line`, bit ranges, and `clocked` on a registered edge). |
| **length** | Hop count (number of edges); `0` for a not-found or single-node path. |

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

## `rtlscanner lint` — static scanner

A fixed, opinionated scanner built on pyslang's elaboration + analysis engine.
It runs a **closed set of five check categories** and reports findings; every
finding is locatable (file, line, column, severity, rule, message, and the owning
module). There is one flag to narrow the scan — `--rules` — and nothing else to
learn.

| Category    | What it reports |
|-------------|-----------------|
| `semantic`  | Compile / elaboration diagnostics slang produces: width truncation, inferred latches, missing `case` defaults, undeclared identifiers, never-assigned variables, multiple-driver conflicts, … |
| `unused`    | Signals and ports declared but never read, or never driven. |
| `port`      | Child-instance port connectivity: unconnected ports, port/connection width mismatches. |
| `cdc`       | Clock-domain crossings — a register feeding, through combinational logic only, a register in a different domain. Heuristic. |
| `comb-loop` | Combinational feedback loops (a cycle through non-registered dataflow). |

```bash
rtlscanner lint -d ./rtl                    # run all five categories
rtlscanner lint -d ./rtl --rules all        # explicit synonym for the default
rtlscanner lint -d ./rtl --rules unused,cdc # run exactly those two
rtlscanner lint -d ./rtl --json > lint.json # full result; check summary for counts
```

### Selecting checks — `--rules`

`--rules` is a **whitelist that replaces the default set**. It accepts only the
five category names plus `all`:

- no flag → run all five (the common case needs nothing).
- `--rules all` → the same, explicit.
- `--rules unused,cdc` → run exactly those (comma-list or bracket/brace style).

Any token outside the closed set (an old family like `default`, a glob like
`width-*`, a per-rule name) fails with a clear error listing the five categories,
rather than silently selecting nothing. There is no subtractive `--skip`.

Suppression is coarse and lives where it belongs: keep noisy sources out of
compilation with `--exclude '**/third_party/**'`, and for finer filtering, filter
the JSON by the `module` field. There are no `--waive`, `--strict`, or
`--min-severity` policy knobs — each finding's severity is fixed by its category.

### CDC

`cdc` runs on the dataflow flow graph (the same one `fanin`/`fanout` use), so it
is **cross-hierarchy**: a launch flop that feeds — through combinational logic
only — the data input of a capture flop in a *different* clock domain is flagged
even when the two flops live in different modules wired through ports. Each flop's
clock is resolved to its **source net** before domains are compared, so two flops
on the same physical clock are one domain even when their local clock ports are
named differently (`clk` vs `clock`) or sit in different instances — and,
conversely, one net reaching two differently-named ports is one domain, not two.
A **gated or divided** clock is treated as its own domain. Reset signals are
recognized by a built-in name heuristic, so `cdc` runs with **zero configuration**.

### Combinational loops

`comb-loop` runs cycle detection (Tarjan strongly-connected components) over the
**non-sequential edges** of the same flow graph — a registered edge (driven by
`always_ff`, a latch, or an edge-sensitive `always`) breaks the feedback, so
legitimate sequential feedback is not flagged while a true combinational cycle
(`assign a = b; assign b = a;`, an `always_comb` that reads its own output) is.
Loops that close through child-instance ports are caught (cross-hierarchy). It
runs on the same constant-pruned graph as `fanin`/`fanout`, so a feedback path
that exists only through a constant-false `if`/`case` dead branch is not reported.

### Exit codes

- `0` — no error-level findings
- `1` — one or more error-level findings
- `2` — usage / source error (e.g. an unknown `--rules` category)

## `rtlscanner find` — design-wide node lookup

```bash
rtlscanner find -d ./rtl -p 'top.**.u_fifo*'          # every u_fifo* anywhere
rtlscanner find -d ./rtl -p '*_valid' --kind signal    # signals named *_valid
rtlscanner find -d ./rtl -p 'top.u_*' --kind instance  # direct children of top
rtlscanner find -d ./rtl --regex -p '.*\.state'        # regex over the whole path
rtlscanner find -d ./rtl -p '**' --scope top.u_ctrl    # everything under one scope
```

`xref` looks up *one exact name*; `find` is the complement — it scans the
**whole elaborated design** and reports every node whose hierarchical path
matches a pattern, with its source location. It is the way to discover the
nodes to then feed into `trace`/`fanin`/`fanout`/`xref` when you only know a
naming pattern, not the exact path.

Each match reports the leaf `name`, the elaborated `kind` (`Net` / `Variable` /
`Instance`), the matched `hierarchical_path`, the `type` (signal) or `module`
(instance), and the `file`/`line`/`column`. Because matching is over elaborated
paths, identical sibling and generate-array instances each match with their own
path (`top.u_dp0.q` **and** `top.u_dp1.q`).

`--kind` narrows to `signal` (nets/variables, including ports and registers) or
`instance`; the default `all` returns both. `--scope` restricts the search to
one subtree.

**Pattern syntax.** The default is a segment-aware **glob** (paths are
dot-separated segments):

| Pattern | Matches |
|---------|---------|
| `*`     | zero or more characters **within** one segment (never crosses `.`) |
| `**` or `...` | zero or more characters **across** segments (recursive) |
| `?`     | exactly one character within a segment (never `.`) |

A recursive wildcard next to a literal `.` makes that `.` an optional boundary,
so `a.**.b` matches `a.b`, `a.x.b`, and `a.x.y.b` alike — the gitignore `/**/`
convention. `--regex` switches to a Python regex matched against the whole path
(`re.fullmatch`).

## `rtlscanner batch` — many queries, one load

Every other subcommand parses **and elaborates** the whole design before
answering one question. When you have several questions about the *same*
design, that parse/elaborate cost — which dominates on a large design — is paid
once per process. `batch` pays it once for all of them:

```bash
rtlscanner batch [input-opts] [--json] [--limit N] [--commands FILE] < queries.txt
rtlscanner batch -d ./rtl --json < queries.txt
```

The design is loaded **once** from the input options on the `batch` line
(`-d` / `-f` / files / `--exclude` / `--config` / `--single-unit`); each line on
stdin is then one query — a subcommand and its own flags — run against that one
loaded design:

```
tree
scope --scope top.u_dp0
trace -s q --scope top.u_dp0   # the q register
fanin -s result --scope top.u_dp --depth 3
lint --rules cdc
# a full-line comment is skipped; so is a blank line
```

Lines are shell-tokenized; a trailing `#` (at a word boundary) starts a
**label** that becomes the result `id` (otherwise a 1-based sequence number is
used). Per line you give only the subcommand's own flags plus an optional
`--limit` override — the input options and `--json` are fixed by the `batch`
line.

With `--json`, each query emits one compact JSON object per line (JSONL),
flushed as it finishes:

```json
{"id":"1","ok":true,"result":{ /* the command's normal --json envelope */ }}
{"id":"the q register","ok":true,"result":{ /* ... */ }}
{"id":"4","ok":false,"error":"signal 'nope' not found in scope 'top.u_dp0'; ..."}
```

A batch `result` is **identical** to what the equivalent single command would
produce — `batch` only saves the repeated load, it never changes a command's
output. Without `--json`, each query prints a `# <id>` header followed by the
command's normal text.

**A failing query never stops the batch**, and the run still exits `0` — read
each frame's `ok` field. A non-zero exit means the design itself could not be
loaded (bad inputs / no sources), surfaced as a single error envelope **before**
any line is read. `--commands FILE` reads the query lines from a file instead of
stdin (`-` means stdin).

`ok` means *the query ran*, not *the query found nothing*. Two consequences
worth noting:

- **`lint` findings don't surface in `ok` or the exit code.** As a single
  command, `lint` exits `1` when it finds an error-severity issue; in a batch
  that signal is **not** propagated — the frame is `ok:true` and the batch still
  exits `0`. Read `result.summary.has_error` (or `result.data.findings`) of the
  `lint` frame instead.
- **A failed query's `error` is a plain string.** The structured
  `errors[].code` / `errors[].details` recovery hints a single command emits
  (e.g. `close_matches` for `SIGNAL_NOT_FOUND`) are dropped in batch; re-run that
  one query on its own if you need them.

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
| Inputs and compilation | `src/rtl_config.py`, `src/rtl_common.py`, `src/rtl_slang.py`, `src/rtl_glob.py` |
| RTL analysis commands | `src/rtl_tree.py`, `src/rtl_scope.py`, `src/signal_trace.py`, `src/signal_flow.py`, `src/signal_path.py`, `src/rtl_lint.py`, `src/rtl_xref.py`, `src/rtl_find.py` |
| Dataflow engine + shared front-end | `src/rtl_dataflow.py`, `src/signal_cli.py` |
| Batch runner | `src/rtl_batch.py` |
| Agent examples and contracts | `examples/agent/` |

# RTLScanner

A pyslang-powered toolkit for SystemVerilog RTL hierarchy inspection,
signal driver/load tracing, static linting, and module interface
reporting.

| Tool | Purpose | Typical stage |
|------|---------|---------------|
| `rtl-tree` | Hierarchy viewer & filelist generator | Architecture / code organisation |
| `signal-trace` | Signal driver & load analyzer | Simulation / debug |
| `rtl-lint` | Static linter (width, unused, case, latch, …) | Code review / CI |
| `rtl-ports` | Module interface & connectivity report | Documentation / integration |

## Install

```bash
pip install -r requirements.txt
```

For an editable command-line install:

```bash
pip install -e .
rtl-tree -d ./examples/basic
signal-trace --filelist rtl.f --signal clk --scope top
```

You can also run the scripts directly:

```bash
python3 src/rtl_tree.py -d ./examples/basic
python3 src/signal_trace.py -d ./examples/basic --signal q --scope top.u_dp0
python3 src/rtl_lint.py -d ./examples/lint
python3 src/rtl_ports.py -d ./examples/ports
```

## Hierarchy Viewer (`rtl-tree`)

```bash
# Recursively scan a directory
rtl-tree -d ./rtl

# Use a VCS-style filelist
rtl-tree --filelist rtl.f --top cpu_core

# Generate a reusable filelist
rtl-tree -d ./rtl --write-filelist rtl.f

# Limit depth / emit JSON / show stats
rtl-tree -d ./rtl --depth 2
rtl-tree -d ./rtl --json > hier.json
rtl-tree -d ./rtl --stats

# Exclude paths
rtl-tree -d ./rtl --exclude '*/tb/*' --exclude '*/postsim/*'
```

### Example

```bash
rtl-tree -d examples/basic --no-color
```

```
top : top
├── u_dp0 : datapath
│   ├── u_reg : register #(WIDTH=8)
│   └── u_alu : alu
│       └── u_add : adder #(WIDTH=8)
├── u_dp1 : datapath
│   ├── u_reg : register #(WIDTH=8)
│   └── u_alu : alu
│       └── u_add : adder #(WIDTH=8)
└── u_extra_reg : register #(WIDTH=8)

10 instances, 5 unique modules, 1 files parsed
```

Generate blocks are elaborated correctly:

```bash
rtl-tree -d examples/generate --no-color
```

```
gen_top : gen_top
└── u_mid : mid #(N=3)
    ├── u_leaf : leaf
    ├── u_gen_leaf : leaf ← gen_arr[0]
    ├── u_gen_leaf : leaf ← gen_arr[1]
    └── u_gen_leaf : leaf ← gen_arr[2]

6 instances, 3 unique modules, 1 files parsed
```

## Signal Tracer (`signal-trace`)

Designed for the debug/simulation workflow where a VCS-style filelist
already exists.  RTL convention: each signal has exactly **one driver**
but potentially **many loads**.

```bash
# Primary usage — with an existing filelist
signal-trace --filelist rtl.f --signal q --scope top.u_dp

# Scan a directory instead
signal-trace -d ./rtl --signal clk --scope top

# List all signals in a scope
signal-trace --filelist rtl.f --scope top.u_dp --list

# Trace all signals in a scope
signal-trace --filelist rtl.f --scope top.u_dp --all

# Filter loads by instance name glob
signal-trace --filelist rtl.f --signal data --scope top --filter 'u_fifo*'

# Trace through port boundaries
signal-trace --filelist rtl.f --signal q --scope top.u_dp --cross

# Dataflow fanin / fanout
signal-trace --filelist rtl.f --signal result --scope top.u_dp --fanin
signal-trace --filelist rtl.f --signal valid --scope top.u_dp --fanout --flow-depth 6

# JSON output (for scripting / IDE integration)
signal-trace --filelist rtl.f --signal q --scope top.u_dp --json
```

You can also invoke signal tracing via `rtl-tree`:

```bash
rtl-tree --filelist rtl.f --trace q --scope top.u_dp
rtl-tree --filelist rtl.f --trace-list --scope top.u_dp
rtl-tree -d ./rtl --trace clk --scope top --filter 'u_dp*'
```

### Example

```bash
signal-trace -d examples/trace --signal mux_out --scope trace_top.u_dp --no-color
```

```
Signal: mux_out  logic[7:0]
Scope:  trace_top.u_dp  [datapath_v2]
────────────────────────────────────────────────────────────

  ◀ DRIVER
    ← output port of instance u_mux  examples/trace/trace_top.sv:36

  ▶ LOADS (2)
    ── Instance port connections (1) ──
    → u_pipe.d (input)  examples/trace/trace_top.sv:37
    ── Continuous assignments (1) ──
    → assign → sum  examples/trace/trace_top.sv:35
```

### Fanin / fanout example

`--fanin` walks upstream from a signal (everything that feeds it),
`--fanout` walks downstream (everything it drives). Both report a
breadth-first set of dataflow edges grouped by traversal depth, and
both honor `--flow-depth N` (default `4`) for the maximum number of
hops to chase.

```bash
signal-trace -d examples/trace --signal mux_out --scope trace_top.u_dp --fanin --no-color
```

```
Signal: mux_out  logic[7:0]
Scope:  trace_top.u_dp  [datapath_v2]
Mode:   FANIN  depth <= 4
────────────────────────────────────────────────────────────

  depth 1
    trace_top.u_dp.u_mux.y → trace_top.u_dp.mux_out  port_connection u_mux.y output  examples/trace/trace_top.sv:36

  depth 2
    trace_top.u_dp.u_mux.a → trace_top.u_dp.u_mux.y    continuous_assign assign  examples/trace/trace_top.sv:6
    trace_top.u_dp.u_mux.b → trace_top.u_dp.u_mux.y    continuous_assign assign  examples/trace/trace_top.sv:6
    trace_top.u_dp.u_mux.sel → trace_top.u_dp.u_mux.y  continuous_assign assign  examples/trace/trace_top.sv:6

  depth 3
    trace_top.u_dp.data_a → trace_top.u_dp.u_mux.a    port_connection u_mux.a   input
    trace_top.u_dp.data_b → trace_top.u_dp.u_mux.b    port_connection u_mux.b   input
    trace_top.u_dp.sel    → trace_top.u_dp.u_mux.sel  port_connection u_mux.sel input

  depth 4
    trace_top.in_a → trace_top.u_dp.data_a  port_connection u_dp.data_a input
    trace_top.in_b → trace_top.u_dp.data_b  port_connection u_dp.data_b input
    trace_top.mode → trace_top.u_dp.sel     port_connection u_dp.sel    input
```

Reading the output:

| Term      | Meaning                                                                 |
|-----------|-------------------------------------------------------------------------|
| **node**  | A signal at one elaborated hierarchical path (e.g. `trace_top.u_dp.sel`). The starting signal is depth 0; everything else surfaces because some edge connected it. |
| **edge**  | One directed dataflow link `source → target`. Its `kind` is `port_connection`, `continuous_assign`, or `procedural`; `description` and `file:line` point at the RTL site it came from. |
| **depth** | BFS distance in hops from the starting signal. Depth 1 is the immediate upstream/downstream neighbors; depth N requires N consecutive edges to reach. Traversal stops at `--flow-depth`. |

The `--json` form returns the same information in the shared agent
envelope, with `data.nodes` (flat list of hierarchical paths) and
`data.edges` (each carrying a `depth` field) — see
`examples/agent/schemas/trace.schema.json`.

## Static Linter (`rtl-lint`)

A fast linter built on pyslang's elaboration + analysis engine.  It
catches real semantic problems that regex linters miss — width
mismatches, unused/undriven signals and ports, missing case defaults,
inferred latches, multi-driven nets — using the same filelist
infrastructure as the other tools.  Designed to drop straight into CI.

```bash
# Lint a design via filelist or directory scan
rtl-lint --filelist rtl.f
rtl-lint -d ./rtl

# Fail the build (exit 1) on any warning — ideal for CI gates
rtl-lint -d ./rtl --werror

# Suppress or promote individual rules (name or glob)
rtl-lint -d ./rtl --disable case-default --disable 'width-*'
rtl-lint -d ./rtl --error width-trunc

# Focus on a rule family, or show only the summary
rtl-lint -d ./rtl --rule 'unused-*'
rtl-lint -d ./rtl --summary

# Opt-in checks and machine-readable output
rtl-lint -d ./rtl --shadow            # variable-shadowing analysis
rtl-lint -d ./rtl --cdc               # clock-domain-crossing detection
rtl-lint -d ./rtl --weverything       # enable every pyslang warning
rtl-lint -d ./rtl --json > lint.json
```

By default `rtl-lint` runs pyslang's standard semantic checks plus
unused/undriven analysis.  Pass `--no-unused` to skip the latter.

### Clock-domain-crossing check (`--cdc`)

Opt-in flop-to-flop CDC analysis. When enabled, `rtl-lint` walks every
`always_ff` block, infers its primary clock from the timing event list
(treating `rst*`/`reset*`/`*_n` signals as resets so async-reset
domains don't get flagged), then maps which clock each signal is
written and read in. A signal written in clock domain A and read in
clock domain B ≠ A is reported as rule `cdc-crossing`, with the
location pointing at the unsafe read.

```bash
rtl-lint -d ./rtl --cdc                      # one-off
rtl-lint -d ./rtl --cdc --cdc-reset 'arst*'  # extra reset-name globs
```

```toml
# Or via config:
[lint]
cdc = true
cdc_reset = ["arst*"]      # extra reset patterns
```

`cdc-crossing` flows through the same severity/waiver pipeline as
every other rule — disable it project-wide with
`[rules] "cdc-crossing" = "off"`, or waive a specific reviewed crossing
(e.g. the first flop of a 2-FF synchronizer) with a `[[waive]]` entry.
Inline `` `pragma `` waivers do **not** apply to `cdc-crossing`
(pyslang's pragma engine only knows its native diagnostic codes); use
a `[[waive]]` block instead.

### Configuring & waiving checks

There are three complementary ways to control what `rtl-lint` reports,
from coarsest to finest:

**1. Config file** — `.rtllint.toml` (or `.rtllint.json`), auto-discovered
by walking up from the current directory, or passed via `--config`.
CLI flags always override the config.

```toml
# .rtllint.toml
[lint]
unused = true        # run unused/undriven analysis (default true)
shadow = false       # variable-shadowing analysis
werror = false       # treat all warnings as errors (CI gate)

# Per-rule severity — "off"/"ignore", "warning", or "error". Globs ok.
[rules]
"case-default" = "off"      # project doesn't require case defaults
"width-trunc"  = "error"    # truncation is a real bug here
"unused-*"     = "warning"

# Location-specific waivers — match by rule (glob), path (glob), and/or line.
[[waive]]
rule   = "unused-port"
path   = "rtl/perips/*.v"
reason = "third-party IP, frozen"

[[waive]]
rule = "case-default"
path = "rtl/core/div.v"
line = 87
```

**2. CLI flags** — for one-off runs and CI:

```bash
rtl-lint -d ./rtl --disable case-default --error width-trunc --werror
rtl-lint -d ./rtl --config ci/strict.toml
rtl-lint -d ./rtl --show-waived        # show what got suppressed, and why
```

**3. Inline `pragma` waivers** — standard SystemVerilog, no config needed.
`rtl-lint` honors pyslang's `pragma diagnostic` directives, so you can
waive a finding right where it lives:

```systemverilog
`pragma diagnostic push
`pragma diagnostic ignore="-Wwidth-trunc"
  assign q = wide_bus;          // intentional truncation
`pragma diagnostic pop
```

Suppressed findings are counted as `waived` in the summary (and listed
with `--show-waived` or in the `waived` array of `--json` output), so
nothing is silently lost.

### Example

```bash
rtl-lint -d examples/lint --no-color
```

```
examples/lint/lint_demo.sv
        8:24  warning   unused port signal 'b'  [unused-port]
       12:17  warning   variable 'dead' is assigned but its value is never used  [unused-but-set-variable]
       15:18  warning   implicit conversion truncates from 8 to 4 bits  [width-trunc]
       29:17  warning   variable 'mode' is never assigned a value  [unassigned-variable]
        32:9  warning   'case' missing 'default' label  [case-default]
       33:20  warning   latch inferred for 'result' because it is not assigned on all control paths  [inferred-latch]
       45:10  warning   output port 'y' is explicitly connected to nothing  [empty-output-connection]
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | clean (no error-level findings) |
| `1` | one or more error-level findings (or warnings with `--werror`) |
| `2` | usage / source error |

## Port Reporter (`rtl-ports`)

Auto-generates module interface documentation and finds connectivity
issues at instance sites — useful for IP integration, design reviews,
and onboarding.

```bash
# Module interface signatures (default)
rtl-ports -d ./rtl

# Connectivity per instance
rtl-ports -d ./rtl --instances

# Only connectivity issues (unconnected ports, width mismatches)
rtl-ports -d ./rtl --check
rtl-ports -d ./rtl --check --werror      # CI gate

# Filter by module or instance name (glob)
rtl-ports -d ./rtl --module cpu_*
rtl-ports -d ./rtl --instances --instance 'top.u_cpu*'

# Export Markdown docs / machine-readable JSON
rtl-ports -d ./rtl --markdown > docs/INTERFACES.md
rtl-ports -d ./rtl --json
```

### Example

```bash
rtl-ports -d examples/ports --check --no-color
```

```
top.u_alu
  note      output port 'zero' is unconnected  [unconnected]

top.u_fifo
  note      output port 'count' is unconnected  [unconnected]
  note      output port 'empty' is unconnected  [unconnected]
  warning   width mismatch on .wr_data: port is 8 bits, connection is 32 bits  [width_mismatch]
```

Unconnected **inputs** are reported as warnings (genuinely undriven);
unconnected **outputs** are reported as notes (intentionally discarding
an output is a common, benign idiom).

## Agent / JSON Mode

All four tools share a single, predictable JSON envelope when invoked
with `--json`, designed to be safe to consume from LLM agents, MCP
servers, or any script that wants structured output:

```json
{
  "tool":        "rtl-tree",
  "version":     "0.1.0",
  "status":      "ok" | "error",
  "command":     { /* echo of parsed args, output flags stripped */ },
  "data":        { /* tool-specific payload */ } | null,
  "diagnostics": [ {severity, file, line, col, message}, ... ],
  "errors":      [ {code, message}, ... ],
  "summary":     { /* tool-specific counts */ } | null
}
```

Each tool ships a JSON Schema (draft-07) for its envelope; dump it with:

```bash
rtl-tree     --schema
signal-trace --schema
rtl-lint     --schema
rtl-ports    --schema
```

Failure is structured too — an `INPUT_NOT_FOUND` / `COMPILE_FAILED` /
`SCOPE_NOT_FOUND` / `SIGNAL_NOT_FOUND` / `BAD_FILELIST` / `BAD_CONFIG`
/ `NO_TOP` / `INTERNAL_ERROR` envelope is printed to stdout (with
non-zero exit code), never a raw stack trace. See
`examples/agent/README.md` for the full contract and worked examples.

**CDC findings** (`rtl-lint --cdc`) appear as ordinary entries in
`data.findings` with `rule="cdc-crossing"` and `check="cdc"`; the count
is mirrored in `summary.by_check.cdc`. Note that
`` `pragma diagnostic ignore `` does NOT suppress `cdc-crossing` —
waive it via a `[[waive]]` entry in `.rtllint.toml` instead.

## Code structure

| File | Lines | Responsibility |
|------|-------|----------------|
| `src/rtl_common.py` | ~490 | Shared infra: Color, filelist parsing, compilation builder |
| `src/agent_json.py` | ~440 | Shared JSON envelope, error codes, per-tool JSON schemas |
| `src/rtl_tree.py` | ~400 | Hierarchy building, tree display, CLI |
| `src/signal_trace.py` | ~670 | Driver/load analysis, signal tracing, CLI |
| `src/rtl_lint.py` | ~830 | Semantic + unused/shadow lint + CDC analyzer, config/waivers, CLI |
| `src/rtl_ports.py` | ~720 | Module interface report, instance connectivity, width-mismatch check, CLI |

## Why pyslang?

SystemVerilog hierarchy and driver analysis depend on elaboration
details such as `generate` loops, parameters, and interfaces.
`pyslang` gives these tools a real parser and elaborator, which makes
them much more reliable than regex matching for non-trivial RTL.

# RTLScanner

A pyslang-powered toolkit for SystemVerilog RTL hierarchy inspection,
signal driver/load tracing, and static linting.

| Tool | Purpose | Typical stage |
|------|---------|---------------|
| `rtl-tree` | Hierarchy viewer & filelist generator | Architecture / code organisation |
| `signal-trace` | Signal driver & load analyzer | Simulation / debug |
| `rtl-lint` | Static linter (width, unused, case, latch, …) | Code review / CI |

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
rtl-lint -d ./rtl --weverything       # enable every pyslang warning
rtl-lint -d ./rtl --json > lint.json
```

By default `rtl-lint` runs pyslang's standard semantic checks plus
unused/undriven analysis.  Pass `--no-unused` to skip the latter.

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

## Code structure

| File | Lines | Responsibility |
|------|-------|----------------|
| `src/rtl_common.py` | ~490 | Shared infra: Color, filelist parsing, compilation builder |
| `src/rtl_tree.py` | ~340 | Hierarchy building, tree display, CLI |
| `src/signal_trace.py` | ~610 | Driver/load analysis, signal tracing, CLI |
| `src/rtl_lint.py` | ~580 | Semantic + unused/shadow lint, config/waivers, CLI |

## Why pyslang?

SystemVerilog hierarchy and driver analysis depend on elaboration
details such as `generate` loops, parameters, and interfaces.
`pyslang` gives these tools a real parser and elaborator, which makes
them much more reliable than regex matching for non-trivial RTL.

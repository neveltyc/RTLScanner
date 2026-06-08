# RTLHierScanner

A pyslang-powered toolkit for SystemVerilog RTL hierarchy inspection
and signal driver/load tracing.

| Tool | Purpose | Typical stage |
|------|---------|---------------|
| `rtl-tree` | Hierarchy viewer & filelist generator | Architecture / code organisation |
| `signal-trace` | Signal driver & load analyzer | Simulation / debug |

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
python3 rtl_tree.py -d ./examples/basic
python3 signal_trace.py -d ./examples/basic --signal q --scope top.u_dp0
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

## Code structure

| File | Lines | Responsibility |
|------|-------|----------------|
| `rtl_common.py` | ~490 | Shared infra: Color, filelist parsing, compilation builder |
| `rtl_tree.py` | ~340 | Hierarchy building, tree display, CLI |
| `signal_trace.py` | ~610 | Driver/load analysis, signal tracing, CLI |

## Why pyslang?

SystemVerilog hierarchy and driver analysis depend on elaboration
details such as `generate` loops, parameters, and interfaces.
`pyslang` gives these tools a real parser and elaborator, which makes
them much more reliable than regex matching for non-trivial RTL.

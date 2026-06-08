# RTLHierScanner

RTLHierScanner is a small command-line tool for accurate Verilog/SystemVerilog
RTL hierarchy inspection. It uses `pyslang` underneath, so the hierarchy comes
from slang's SystemVerilog elaboration instead of regex matching.

## Install

```bash
pip install -r requirements.txt
```

For an editable command-line install:

```bash
pip install -e .
rtl-tree -d ./examples/basic
```

You can also run the script directly:

```bash
python3 rtl_tree.py top.sv sub.sv
```

## Usage

```bash
# Directly specify files
python3 rtl_tree.py top.sv sub.sv

# Recursively scan a directory
python3 rtl_tree.py -d ./rtl

# Generate a reusable VCS-style filelist and still show hierarchy
python3 rtl_tree.py -d ./rtl --write-filelist rtl.f

# Read an existing filelist
python3 rtl_tree.py --filelist rtl.f --top cpu_core

# Only generate a filelist
python3 rtl_tree.py -d ./rtl --write-filelist rtl.f --filelist-only

# Write absolute paths
python3 rtl_tree.py -d ./rtl --write-filelist rtl_abs.f --filelist-path abs --filelist-only

# Write paths with a project prefix
python3 rtl_tree.py -d ./rtl --write-filelist rtl_proj.f --filelist-path prefix --filelist-prefix '${PROJPATH}' --filelist-only

# Exclude generated, testbench, or duplicate library paths
python3 rtl_tree.py -d ./rtl --exclude '*/tb/*' --exclude '*/postsim/*'

# Specify the top module
python3 rtl_tree.py -d ./rtl --top cpu_core

# Limit display depth
python3 rtl_tree.py -d ./rtl --depth 2

# Show hierarchical paths
python3 rtl_tree.py -d ./rtl --path

# Emit JSON for other tools
python3 rtl_tree.py -d ./rtl --json > hier.json

# Show module usage statistics
python3 rtl_tree.py -d ./rtl --stats
```

Generated filelists use a VCS-compatible style:

```text
+incdir+rtl/include
+define+SIMULATION
rtl/top.sv
rtl/block.sv
```

Header-style files (`.svh`, `.vh`, `.svi`) and `.sv` / `.v` files without a
top-level `module`, `interface`, `package`, `program`, or `primitive` are
treated as include fragments. They contribute `+incdir+` entries instead of
being compiled as standalone sources.

## Example

```bash
python3 rtl_tree.py -d examples/basic --no-color
```

```text
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

Generate blocks are elaborated as well:

```bash
python3 rtl_tree.py -d examples/generate --no-color
```

```text
gen_top : gen_top
└── u_mid : mid #(N=3)
    ├── u_leaf : leaf
    ├── u_gen_leaf : leaf ← gen_arr[0]
    ├── u_gen_leaf : leaf ← gen_arr[1]
    └── u_gen_leaf : leaf ← gen_arr[2]

6 instances, 3 unique modules, 1 files parsed
```

## Why pyslang?

SystemVerilog hierarchy can depend on elaboration details such as `generate`
loops, `generate if`, parameters, and interfaces. `pyslang` gives this tool a
real parser and elaborator, which makes it much more reliable than simple text
matching for non-trivial RTL.

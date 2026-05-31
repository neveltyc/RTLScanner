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

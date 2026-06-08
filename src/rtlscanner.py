#!/usr/bin/env python3
"""rtlscanner — unified CLI for the RTLScanner tool family.

Subcommands:
    tree     — design hierarchy
    trace    — single-signal driver/loads
    signals  — list signals in a scope
    fanin    — upstream dataflow BFS
    fanout   — downstream dataflow BFS
    lint     — semantic + unused + shadow + CDC checks
    ports    — module/instance/port reports
    xref     — symbol definitions and references
    inspect  — elaborated parameters and local types
"""

from __future__ import annotations

import argparse
import sys

import agent_json
from agent_json import (
    Envelope, add_input_args, add_output_args, filter_command,
)
from rtl_common import Color

import rtl_tree
import signal_trace
import rtl_lint
import rtl_ports
import rtl_xref
import rtl_inspect


SUBCOMMANDS = {
    "tree":    (rtl_tree.add_arguments,        rtl_tree.run,
                "Show design hierarchy"),
    "trace":   (signal_trace.add_trace_args,   signal_trace.run_trace,
                "Trace a signal's driver and loads"),
    "signals": (signal_trace.add_signals_args, signal_trace.run_signals,
                "List signals in a scope"),
    "fanin":   (signal_trace.add_flow_args,
                lambda a, e: signal_trace.run_flow(a, e, mode="fanin"),
                "Walk upstream dataflow BFS from a signal"),
    "fanout":  (signal_trace.add_flow_args,
                lambda a, e: signal_trace.run_flow(a, e, mode="fanout"),
                "Walk downstream dataflow BFS from a signal"),
    "lint":    (rtl_lint.add_arguments,        rtl_lint.run,
                "Static lint (semantic + unused + shadow + CDC)"),
    "ports":   (rtl_ports.add_arguments,       rtl_ports.run,
                "Module interface and instance connectivity report"),
    "xref":    (rtl_xref.add_arguments,         rtl_xref.run,
                "Show symbol definitions and references"),
    "inspect": (rtl_inspect.add_arguments,      rtl_inspect.run,
                "Show elaborated parameters and local types"),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rtlscanner",
        description="rtlscanner — SystemVerilog RTL analysis toolkit (pyslang)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  rtlscanner tree    -d ./rtl
  rtlscanner trace   -d ./rtl -s q --scope top.u_dp
  rtlscanner signals -d ./rtl --scope top.u_dp
  rtlscanner fanin   -d ./rtl -s result --scope top.u_dp --depth 3
  rtlscanner fanout  -d ./rtl -s q --scope top.u_dp
  rtlscanner lint    -d ./rtl --rules default,cdc
  rtlscanner ports   -d ./rtl --check --strict
  rtlscanner xref    -d ./rtl -s q --scope top.u_dp
  rtlscanner inspect -d ./rtl --scope top.u_dp

Configuration:
  ./.rtlscanner.toml is auto-discovered (CWD only).
  Env vars: RTLSCANNER_FILELIST, RTLSCANNER_DIR, RTLSCANNER_EXCLUDE,
            RTLSCANNER_ROOT, RTLSCANNER_PREFIX
  Priority: CLI > env > config > built-in defaults.
""",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCMD")
    for name, (add_fn, _run, desc) in SUBCOMMANDS.items():
        sp = sub.add_parser(name, help=desc, description=desc,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        add_input_args(sp)
        add_fn(sp)
        add_output_args(sp)
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    if args.schema:
        return agent_json.print_schema(args.cmd)

    if args.no_color or not sys.stdout.isatty() or args.json:
        Color.disable()

    env = Envelope(args.cmd, filter_command(args)) if args.json else None
    run_fn = SUBCOMMANDS[args.cmd][1]
    try:
        return run_fn(args, env) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

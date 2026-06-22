#!/usr/bin/env python3
"""rtlscanner — unified CLI for the RTLScanner tool family.

Subcommands:
    tree     — design hierarchy
    trace    — single-signal driver/loads
    scope    — direct contents of one elaborated scope
    fanin    — upstream dataflow BFS
    fanout   — downstream dataflow BFS
    path     — point-to-point dataflow path between two nodes
    lint     — semantic + unused + port + cdc + comb-loop checks
    xref     — signal/module source definitions and references
    find     — design-wide node lookup by glob/regex pattern
    batch    — run many queries against one loaded design (stdin)
"""

from __future__ import annotations

import argparse
import sys

import agent_json
from agent_json import (
    Envelope, add_input_args, add_output_args, emit, filter_command,
)
from rtl_common import Color

import rtl_tree
import signal_trace
import signal_flow
import signal_path
import rtl_lint
import rtl_xref
import rtl_scope
import rtl_find
import rtl_batch


SUBCOMMANDS = {
    "tree":    (rtl_tree.add_arguments,        rtl_tree.run,
                "Show design hierarchy"),
    "trace":   (signal_trace.add_trace_args,   signal_trace.run_trace,
                "Trace a signal's driver and loads"),
    "scope":   (rtl_scope.add_arguments,        rtl_scope.run,
                "Show direct contents of an elaborated scope"),
    "fanin":   (signal_flow.add_flow_args,
                lambda a, e: signal_flow.run_flow(a, e, mode="fanin"),
                "Walk upstream dataflow BFS from a signal"),
    "fanout":  (signal_flow.add_flow_args,
                lambda a, e: signal_flow.run_flow(a, e, mode="fanout"),
                "Walk downstream dataflow BFS from a signal"),
    "path":    (signal_path.add_path_args,      signal_path.run_path,
                "Find a dataflow path between two nodes (--from / --to)"),
    "lint":    (rtl_lint.add_arguments,        rtl_lint.run,
                "Static scan: semantic + unused + port + cdc + comb-loop"),
    "xref":    (rtl_xref.add_arguments,         rtl_xref.run,
                "Show signal/module definitions and references"),
    "find":    (rtl_find.add_arguments,         rtl_find.run,
                "Find design nodes by glob/regex pattern"),
    "batch":   (rtl_batch.add_batch_args,       rtl_batch.run_batch,
                "Run many queries against one loaded design (stdin)"),
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
  rtlscanner scope   -d ./rtl --scope top.u_dp
  rtlscanner fanin   -d ./rtl -s result --scope top.u_dp --depth 3
  rtlscanner fanout  -d ./rtl -s q --scope top.u_dp
  rtlscanner path    -d ./rtl --from a --to y0 --scope top
  rtlscanner lint    -d ./rtl --rules cdc
  rtlscanner xref    -d ./rtl -s q --scope top.u_dp
  rtlscanner find    -d ./rtl -p 'top.**.u_fifo*'
  rtlscanner batch   -d ./rtl --json < queries.txt   # many queries, one load

Configuration:
  Use rtlscanner <cmd> --config FILE to select a project config .toml file.
  Otherwise, ./.rtlscanner.toml is auto-discovered (CWD only).
  Env vars: RTLSCANNER_FILELIST, RTLSCANNER_DIR, RTLSCANNER_EXCLUDE,
            RTLSCANNER_ROOT, RTLSCANNER_PREFIX, RTLSCANNER_CONFIG
  Priority: CLI > env > selected config > built-in defaults.
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
    except agent_json.AgentError as e:
        # Structured error raised from deep in a subcommand.
        if env is not None:
            return emit(env.fail(e.code, e.message, getattr(e, "details", None)))
        print(f"Error: {e.message}", file=sys.stderr)
        return int(getattr(e, "exit_code", 2))
    except Exception as e:
        # Last-resort guard: in --json mode an unexpected failure must still
        # reach the agent as a structured envelope on stdout, never a raw
        # traceback on stderr.  In human mode, let the traceback surface so
        # developers can debug it.
        if env is not None:
            return emit(env.fail(agent_json.ERR_INTERNAL, f"internal error: {e}"))
        raise


if __name__ == "__main__":
    sys.exit(main())

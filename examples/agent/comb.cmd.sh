#!/usr/bin/env bash
# Produces comb.out.json — the combinational D-cone feeding a register's input.
# Starting from a register output, --comb walks its own combinational fan-in
# (the `clocked` always_ff edges mark the flop boundary) and stops at the next
# register up — the cone you want for setup-path / timing reasoning.
rtlscanner fanin -d examples/trace --signal q --scope trace_top.u_dp.u_pipe --comb --json

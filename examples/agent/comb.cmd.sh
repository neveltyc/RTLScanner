#!/usr/bin/env bash
# Produces comb.out.json — combinational fan-in cone (stops at flip-flops)
rtlscanner fanin -d examples/basic --signal y0 --scope top --comb --json

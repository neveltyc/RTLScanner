#!/usr/bin/env bash
# Produces trace.out.json
rtlscanner trace -d examples/basic --signal q --scope top.u_dp0 --json

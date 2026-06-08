#!/usr/bin/env bash
# Produces flow.out.json
rtlscanner fanout -d examples/basic --signal q --scope top.u_dp0 --json

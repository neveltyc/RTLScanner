#!/usr/bin/env bash
# Produces flow.out.json
signal-trace -d examples/basic --signal q --scope top.u_dp0 --fanout --json

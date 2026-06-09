#!/usr/bin/env bash
# Produces xref.out.json
rtlscanner xref -d examples/trace --scope trace_top.u_dp --signal mux_out --json

#!/usr/bin/env bash
set -euo pipefail
rtlscanner scope -d examples/basic --scope top.u_dp0 --connections --json

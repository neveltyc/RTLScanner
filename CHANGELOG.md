# Changelog

All notable changes to RTLScanner are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-14

First tagged release. RTLScanner wraps pyslang's SystemVerilog parse +
elaborate + analysis behind seven query subcommands over RTL *source* (no
simulation), each emitting one uniform JSON envelope under `--json`:
`{tool, version, status, command, data, diagnostics, errors, summary}`.

### Subcommands

- **`tree`** — module hierarchy / resolved filelist (`--export` writes a filelist).
- **`scope`** — ports, signals, child instances, params, and local typedefs of one scope.
- **`trace`** — a signal's immediate driver(s) and loads; scope-local and bit-select aware.
- **`fanin` / `fanout`** — upstream / downstream dataflow BFS; traverses port
  boundaries, per-statement procedural edges, `--summary` and `--depth` controls.
- **`xref`** — where a signal/module is declared and referenced.
- **`lint`** — compile / semantic / unused / shadow / CDC / port findings, with a
  `--rules` / `--skip` / `--waive` / `--min-severity` / `--strict` selection model.

### Added

- **`lint --rules bugs`** — a curated, high-precision preset for "are there real
  bugs?". Keeps the rules that flag functional defects (`inferred-latch`,
  `unassigned-variable`, `undriven-port`, `port-width-mismatch`,
  `port-width-trunc`, `width-trunc`) plus every hard compile error, while
  dropping style noise. `cdc-crossing` is excluded by default; compose it back
  with `--rules bugs,cdc`.
- **`lint` unknown-rule diagnostic** — a typo'd `--rules`/`--skip` token (e.g.
  `bugz`) now emits a did-you-mean `note` instead of silently selecting zero
  findings. A valid rule that simply has no findings this run is not flagged.

### Removed

- **`trace --cross`** — it only printed "crosses boundary" without following the
  connection. Cross-hierarchy dataflow is served by `fanin`/`fanout`, which
  traverse port boundaries; `trace` is now strictly scope-local. The
  `cross_hierarchy` output field is removed accordingly.

[0.1.0]: https://github.com/neveltyc/RTLScanner/releases/tag/v0.1.0

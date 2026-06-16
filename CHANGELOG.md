# Changelog

All notable changes to RTLScanner are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--limit` on every subcommand** — output lists are now capped (default
  `200` rows, `--limit 0` removes the cap) so a query against a large design
  stays agent-friendly. Count fields keep reporting the true totals; a
  `summary.truncated` flag (plus `summary.limit`) signals that more exists, and
  human-mode output appends a note such as
  `... truncated: 3/11 instances shown. (use --limit 0 to see all)`. Caps the
  flat/list outputs (`lint` findings, `xref` references/instances, `scope`
  sections, `tree --flat`, `fanin`/`fanout` edges, `trace` loads); the nested
  `tree` JSON hierarchy is capped by total node count.

### Changed

- **Waiver wording clarified** — `--waive` matches a finding's **source-file
  basename** (a glob), not the elaborated module/scope/instance name. The help
  text, the `--waive` metavar (`MODULE` → `GLOB`), the JSON `waived_reason`
  value (`"module waived"` → `"waived (file glob)"`), and the README all say so
  now. Which findings get waived is unchanged. A true module/scope/instance
  waiver is planned.

### Fixed

- **Per-file compilation units** — each source file in a file list is now
  compiled as its own compilation unit, the way slang's own driver (and
  VCS/Verilator) treat a file list. Previously every file was concatenated into
  one synthetic `` `include `` buffer, merging all files into a single
  compilation unit: a `` `define `` (or any `$unit`-scoped declaration) in one
  file leaked into the next, so a design with a genuine missing-define bug could
  lint clean. Command-line `+define+` macros remain global predefines applied to
  every file, and reported file/line locations are unchanged.

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

[Unreleased]: https://github.com/neveltyc/RTLScanner/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/neveltyc/RTLScanner/releases/tag/v0.1.0

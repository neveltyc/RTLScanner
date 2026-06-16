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

- **`module` on each lint finding** — every finding now reports the design unit
  (module / interface / ...) it sits in, attributed by source range, so an
  agent can group or filter findings by unit even in a multi-module file.

### Changed

- **Unified path-style vocabulary** — `tree --path-style` and the xref
  `path_style` config now both accept the long and short spellings
  (`relative`/`rel`, `absolute`/`abs`), normalizing to the long canonical form,
  so the same words work for either command. Non-breaking: every previously
  valid value still works. The third option stays command-specific because the
  modes differ (`tree`: `prefix` = `${PROJPATH}/<relative>`; `xref`: `name` =
  bare basename); fully reconciling those is left to a future release.

### Fixed

- **`--waive` matches the module, not the whole file** — waivers are applied per
  design unit (using the new source-range attribution) instead of by the
  "file basename == module name" heuristic, so a glob can waive one module in a
  multi-module file without suppressing its neighbours. The source-file basename
  is still matched as a fallback, so existing file-oriented waivers keep working;
  the JSON `waived_reason` is now `"waived (glob)"`.

- **`lint` and `xref` report a file with the same path** — `lint` relativized
  paths against the process CWD while `xref` used the configured
  `[inputs].root`, so running from a directory other than the root made the two
  commands disagree on the very same file. `lint` now keys off the resolved
  input root and renders paths identically to `xref` (same base, same `./`
  prefix).

- **Driver line names the enclosing instance** — a driver from an unnamed
  always/initial block read `… block in (anonymous)`; it now falls back to the
  block's hierarchical path, e.g. `always_ff block in trace_top.u_dp.u_pipe`.

- **CDC reset detection no longer over-matches `*_n`** — the default reset-name
  globs included a bare `*_n`, so any active-low *data* signal (`data_n`,
  `sel_n`, `q_n`, `we_n` …) was treated as a reset and dropped from the
  timing/clock events, masking real clock-domain crossings. The defaults are now
  reset-rooted (an active-low reset must carry an `rst`/`reset`/`arst`/`por`/`clr`
  root); genuine names like `rst_n`, `arst_n`, `por_n`, `reset_n` are still
  recognized, and `[lint.cdc] reset = [...]` still extends the list.

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

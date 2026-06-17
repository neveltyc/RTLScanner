# Changelog

All notable changes to RTLScanner are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--single-unit`** — opt back into single-compilation-unit mode (the
  pre-0.2.0 / slang `--single-unit` model) so `$unit`-scoped `typedef`s and
  macros declared in an earlier file stay visible to later files. The per-file
  default (added in 0.2.0) is unchanged; this is the escape hatch for projects
  that legitimately compile their whole filelist as one unit. Available on every
  subcommand.

- **`lint --rules comb-loop`** — combinational-loop detection. Runs cycle
  detection (Tarjan strongly-connected components) over the **non-sequential
  edges** of the dataflow flow graph (the same graph `fanin`/`fanout` use): a
  registered edge breaks feedback, so legitimate sequential feedback is left
  alone while a true combinational cycle (`assign a = b; assign b = a;`, an
  `always_comb` reading its own output, or a loop that closes through child
  instance ports) is flagged. Findings are regular entries with
  `rule="comb-loop"`, `check="comb-loop"`, at `warning` severity. Opt-in
  (excluded from `default` and `bugs`, like `cdc`); compose with
  `--rules default,comb-loop`.

- **`clocked` on dataflow edges** — every `fanin`/`fanout` edge now carries a
  `clocked` boolean (emitted only when true) marking a *registered* edge — one
  whose target is driven by an edge-triggered `always_ff` / latch / edge-sensitive
  `always`. It is the flip-flop boundary the CDC and combinational-loop checks
  key off, and it is additive (combinational edges simply omit the field).

### Changed

- **Graph-based, cross-hierarchy CDC** — `--rules cdc` now detects clock-domain
  crossings on the dataflow flow graph instead of a single-module `always_ff`
  scan, and resolves each flop's clock to its **source net** before comparing
  domains. Consequences: (1) a launch flop feeding — combinationally — a capture
  flop in a different domain is found even across module boundaries wired through
  ports; (2) two flops on the *same physical clock* are one domain even when
  their local clock ports are named differently (`clk` vs `clock`) or live in
  different instances — fixing both the false negative (same-named ports on
  different clock nets) and the false positive (one net, differently-named
  ports) of the old name comparison; (3) a gated/divided clock is its own
  domain. Findings keep the same `rule="cdc-crossing"` / `check="cdc"` shape and
  the `[lint.cdc] reset = [...]` reset-name vocabulary. The previous version
  also silently saw no procedures for deduplicated instances (generate arrays,
  repeated modules); the graph path analyses through canonical bodies, so those
  are now covered.

### Fixed

- **A user-listed source that declares only `$unit`-scoped `typedef`s is no
  longer silently dropped.** The file classifier decided a `.v`/`.sv` was a
  compilation source vs. an include header by sniffing it for a top-level
  declaration (`module`/`interface`/`package`/`program`/`primitive`). A file
  holding nothing but a `$unit` `typedef` matched none of those, so it was
  demoted to an include directory and never compiled — leaving every later file
  that used the type with an "undeclared identifier" error. Worst of all this
  defeated the brand-new `--single-unit` for exactly the files it was added to
  serve (a leading typedefs-only file sharing its `$unit` scope). Files the user
  names directly — listed in a filelist or passed as a path — are now always
  treated as sources regardless of their contents; the top-level-declaration
  heuristic is kept only for directory auto-discovery, where it still skips
  include-style `.sv` snippets.
- **Structural commands no longer hand back a phantom result for a design that
  does not compile.** `tree`/`scope`/`xref`/`trace`/`fanin`/`fanout` built their
  output from slang's error-recovery AST and reported `status:"ok"` with an empty
  `errors[]` — so an agent following the documented "read `status` first" rule
  would happily reason about the structure of a design that never compiled (a
  latent bug exposed by 0.2.0's per-file compilation units). They now return
  `status:"error"` with `errors[0].code = "COMPILE_FAILED"` and `data:null` when
  the design has compile errors. `lint` is unchanged — reporting those errors as
  findings is its job, so it stays `status:"ok"`.
- **Compilation diagnostics are now formatted correctly.** `tree` emitted each
  diagnostic as a raw `<...Diagnostic object at 0x...>` repr hardcoded to
  severity `"warning"` (so an error-level diagnostic could never flip `status`),
  and `scope`/`xref` collected none at all. All structural commands now surface
  real diagnostics — correct severity, human-readable message, and
  file/line/column — through the same `DiagnosticEngine` `lint` uses.
- **`comb-loop` no longer silently drops a real loop.** When a multi-node
  combinational cycle's lexicographically-smallest node also had a structural
  self-assign (`assign a = a & …;`), the cycle reconstruction closed on that
  trivial self-edge and returned a length-1 path, which the caller discarded —
  so the whole strongly-connected component went unreported. The reconstruction
  now requires a real hop before closing the cycle, so the multi-node loop is
  reported.
- **CDC clock-domain map cache is keyed by the reset configuration.** The cached
  `{register → clock domain}` map ignored the reset predicate, so a tracer
  reused across calls with different `[lint.cdc] reset` globs could hand back the
  first call's stale map. It is now keyed by the reset-glob set (no effect on the
  normal single-call path).

## [0.2.0] - 2026-06-16

Second release. Hardens the agent JSON contract, makes `fanin`/`fanout`
demand-driven (and correct across hierarchical references), gives `lint`
per-module finding attribution and a `module:`/`file:` waiver vocabulary, adds
a universal `--limit`, and fixes a compilation-unit isolation bug. See the
grouped notes below.

### Added

- **`--limit` on every subcommand** — output lists are now capped (default
  `200` rows, `--limit 0` removes the cap) so a query against a large design
  stays agent-friendly. Count fields keep reporting the true totals; a
  `summary.truncated` flag (plus `summary.limit`) signals that more exists, and
  human-mode output appends a note such as
  `... truncated: 3/11 instances shown. (use --limit 0 to see all)`. Caps the
  flat/list outputs (`lint` findings and waivers, `xref` references/instances,
  `scope` sections, `tree --flat`, `fanin`/`fanout` edges, `trace` loads); the
  nested `tree` JSON hierarchy is capped by total node count. `summary.truncated`
  and `summary.limit` are part of every subcommand's `--schema`, and a capped
  `fanin`/`fanout` graph keeps `nodes`/`edges` mutually consistent (an edge is
  never emitted to a node that was dropped from `nodes`).

- **`module` on each lint finding** — every finding now reports the design unit
  (module / interface / ...) it sits in, attributed by source range (works for
  one-module-per-file and multi-module files alike), so an agent can group or
  filter findings by unit. Documented in `lint --schema`.

### Changed

- **Demand-driven `fanin` / `fanout`** — the dataflow BFS now expands a node's
  incident edges only when the traversal reaches it, instead of building the
  whole design's flow graph up front and then walking it. A shallow query on a
  large design pays for the touched neighborhood rather than the full
  `O(instances × ports/statements)` graph: in a synthetic 800-instance design a
  `fanin --depth 1` drops from building ~6.4k edges (~0.8 s) to materializing a
  two-instance neighborhood (~3 ms), and the per-query cost stops growing with
  the size of the rest of the design. Results match the whole-graph build
  edge-for-edge and in order, including cross-module hierarchical procedural
  references in either direction: a *downward* reference (an ancestor procedure
  reading/driving the queried signal by hierarchical name) is resolved from each
  ancestor's procedural edges, and an *upward* or *lateral* reference (a
  descendant or cousin procedure doing the same) from a one-time
  external-reference index that is built only when the design actually contains
  such a reference — a purely port-wired design stays fully demand-driven.

- **Unified path-style vocabulary** — `tree --path-style` and the xref
  `path_style` config now both accept the long and short spellings
  (`relative`/`rel`, `absolute`/`abs`), normalizing to the long canonical form,
  so the same words work for either command. Non-breaking: every previously
  valid value still works. The third option stays command-specific because the
  modes differ (`tree`: `prefix` = `${PROJPATH}/<relative>`; `xref`: `name` =
  bare basename); fully reconciling those is left to a future release.

### Fixed

- **`--waive` gains `module:` / `file:` target prefixes** — a finding is
  attributed to its design unit by source range, and a waiver can now name the
  target explicitly to remove the module-vs-file ambiguity: `module:fifo`
  matches the module only (never a sibling module in the same file),
  `file:third_party_*` matches the source file (and, unlike a module glob,
  reaches findings with **no module** — `$unit`-scope / preprocessor /
  file-level compile errors), and `scope:` is reserved for future
  instance-level waivers (currently ignored with a note). A **bare** glob is
  unchanged and backward-compatible: it still matches the module name **or** the
  source-file basename. The JSON `waived_reason` now names the matched token,
  e.g. `waived ('module:fifo')`.

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
  *Migration:* if a design relied on a header listed first in the filelist to
  define macros/types for the files after it, make that sharing explicit —
  supply the macros via `+define+`, `` `include `` the header from each file
  that needs it, or move shared declarations into a `package`.

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

[Unreleased]: https://github.com/neveltyc/RTLScanner/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/neveltyc/RTLScanner/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/neveltyc/RTLScanner/releases/tag/v0.1.0

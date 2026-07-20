# Changelog

All notable changes to RTLScanner are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`driver` — a signal driver's value-logic (branches, operands, timing).**
  Where `trace` locates a signal's driver, `driver` returns the structure needed
  to explain a value: for each driver, the branch/guard chain (if/case conditions
  with polarity), each branch's RHS operands (name, hierarchical path, bit range),
  and — for sequential drivers — the clock/reset timing read from the sensitivity
  list. Combinational and continuous drivers report their operands directly. This
  is the elaborated structure a waveform-aware root-cause ("why is S this value at
  time T") analysis joins with runtime values. Branch structure and operands are
  exact; reset classification and sequential-timing inference are best-effort and
  flagged `heuristic`. Ships with a `--schema` contract and full envelope
  conformance coverage.

## [0.4.0] - 2026-06-22

### Added

- **`path` — point-to-point dataflow path between two nodes.** A new subcommand
  that finds a directional dataflow path from `--from` to `--to`. Where
  `fanin`/`fanout` report the whole cone around a signal, `path` answers the
  narrower "is there a path between *these two* nodes, and what is it?" and prints
  the path itself: the alternating node → edge → node walk from start to end, each
  edge carrying its kind, source location, and description (input/output port,
  continuous `assign`, procedural block) plus the driven bit range. The mechanism
  is **one depth-first search from the start that builds a parent map** (the edge
  by which each node was first reached), and the path to the end is read back
  along those pointers and reversed. It runs over the same demand-driven graph
  `fanin`/`fanout` use, so a path crosses port boundaries and hierarchical
  references identically. `find` and `findComb` differ by a single edge
  predicate: `--comb` restricts to a *purely combinational* path that never
  enters a register node (the same flop boundary `--comb` fan-in/out uses), for
  "are these two points in one timing path?". The path is **directional**
  (`--from` must drive `--to`); a nonexistent path is a normal `found:false` empty
  result (`status:"ok"`), never an error, and `--from == --to` is a zero-hop
  single-node path. Each endpoint accepts a bare name in `--scope`, a dotted
  relative path, or an absolute hierarchical path (same normalization as a
  dotted `-s`). New module `src/signal_path.py` over the shared `PathFinder` in
  `rtl_dataflow`; ships a JSON schema (`path --schema`) and the shared agent
  envelope.

- **`find` — design-wide node lookup by glob/regex.** A new subcommand that scans
  the **whole elaborated design** and reports every signal / instance node whose
  hierarchical path matches a pattern, with its source location. It complements
  `xref` (which looks up *one exact name*) and `tree --filter` (which narrows a
  single view): `find` is how you *discover* the nodes — by a naming pattern you
  know rather than a path you don't — to then feed into
  `trace`/`fanin`/`fanout`/`xref`. The default pattern is a segment-aware glob
  (`*` within a `.`-segment, `**`/`...` recursive across segments, `?` one char);
  `--regex` switches to a whole-path Python regex. `--kind signal|instance` and
  `--scope` narrow the search. Because matching is over elaborated paths,
  identical sibling/generate instances each match with their own path. Ships a
  JSON schema (`find --schema`) and the shared agent envelope.

- **Combinational-cone mode for `fanin`/`fanout` (`--comb`).** Stops the dataflow
  BFS at sequential (registered) edges, yielding the *pure combinational*
  fan-in/out bounded by flip-flops — the BFS refuses to enter a register node.
  This is the cone for timing-path reasoning. A register *node* is the
  boundary, so boundary flops are excluded — except the
  starting signal, which is always expanded, so a `--comb fanin` from a register
  output still reports that flop's own combinational D-cone (the `clocked` D→Q
  edge marks the boundary). Reuses the `clocked` edge information already on the
  flow graph (no new analysis). Because registers bound the cone, `--comb`
  defaults to **unbounded** depth (`max_depth` then reports the deepest hop
  reached); an explicit `--depth N` still caps it. JSON output carries a
  `comb: true` flag.

- **`batch` subcommand — many queries against one loaded design.** Every other
  subcommand parses + elaborates the whole design before answering one question,
  so an agent asking ten questions pays that cost ten times across ten
  processes. `rtlscanner batch [input-opts] --json < queries.txt` loads the
  design **once** (from the input options on the `batch` line) and then runs one
  query per stdin line — `<subcmd> [flags]  # optional-label`, shell-tokenized —
  against it, streaming one compact JSONL frame per query
  (`{"id":…,"ok":…,"result":…}`, flushed as each finishes; a `# label` sets the
  `id`, else a 1-based sequence number). A batch `result` is byte-identical to
  the equivalent single command's `--json` envelope — `batch` only amortizes the
  load. A failing query is isolated (`{"ok":false,"error":…}`) and the run still
  exits `0`; a non-zero exit means the design itself could not be loaded
  (surfaced before any line is read). Ports RWaveAnalyzer's `--batch` mode. Both
  reuse seams are shared and non-invasive: `rtl_cli.prepare_compilation`
  short-circuits on an injected `_prepared` compilation (load once) and
  `agent_json.emit` grows an opt-in capture sink (`capture_emit`) so each
  command's envelope can be re-framed — no per-command changes. Text mode prints
  a `# <id>` header before each command's normal output. Per-query guardrails:
  `lint` findings stay inside the frame (read `result.summary.has_error` — the
  single-command exit-1 is not propagated, the frame is `ok:true`), failures are
  reported as a plain `error` string, and `tree --export` is rejected on a batch
  line (run it standalone) so a query never corrupts the JSONL stream or writes a
  file. New module `src/rtl_batch.py`; schema via `rtlscanner batch --schema`.

### Changed

- **Unified the result → render seam across all six commands (internal).** Every
  subcommand's `run()` used to fork on output mode — `if env: <emit JSON> else:
  <print human>` — with each branch re-deriving the same totals down its own
  path (the hierarchy `summary` was computed once for `tree --json` and again for
  the human `--stats` table and footer; the lint severity/rule/check counts were
  computed once for the JSON summary and again inside `print_summary`). Those
  parallel derivations are exactly what drifts, which is why the
  schema-conformance test exists. Each command now builds **one typed result**
  (`TreeResult`, `TraceOutput`, `ScopeResult`, `FlowGraphOutput` /
  `FlowSummaryOutput`, `LintResult`, `XrefModuleOutput` / `XrefSignalOutput`)
  that derives every count/total **once**, paired with two pure renderers
  (`to_json` / `render_human`) reading the same fields; `run()` ends with the
  single shared seam `agent_json.render(env, result, limit)`. Pulls the weaker
  `tree` / `scope` paths up to the `to_dict()` shape `signal_trace` / `lint`
  already had. Purely structural — the human and `--json` output is unchanged
  (verified byte-for-byte across all six commands).

- **Split `signal_trace` into a dataflow engine and thin presentation layers
  (internal).** The ~2,500-line `signal_trace` module both *built* the model —
  driver/load analysis, the whole-design dataflow graph, the demand-driven
  bit-aware `fanin`/`fanout` BFS, bit-range mapping, and clock-domain resolution
  (the `SignalTracer` engine) — and *framed* it into each command's shape (the
  `trace` driver/load view, the `fanin`/`fanout` node/edge graph), with the
  rendering baked onto the result dataclasses. The mechanism now lives in a
  standalone `rtl_dataflow` engine that emits a typed model (`TraceResult` /
  `FlowResult` / `FlowEdge` / `DriverInfo` / `LoadInfo`); the per-command
  rendering moves into thin command modules (`signal_trace` for `trace`,
  `signal_flow` for `fanin`/`fanout`) that only shape the model, over a shared
  `signal_cli` argv front-end. `lint`'s CDC / combinational-loop checks consume
  the same engine (now imported from `rtl_dataflow`). Purely structural — the
  human and `--json` output is unchanged (verified byte-for-byte across `trace` /
  `fanin` / `fanout`, including bit-selects, segment permutations, `--summary`,
  `--filter`, and error paths).

### Fixed

- **Lint sub-analysis failures are surfaced in the result envelope.** The CDC,
  combinational-loop, port-connectivity, and analysis-manager passes each caught
  their own exceptions and only printed to stderr, leaving the result with
  status `ok`, an empty `errors`, and silently incomplete findings — an agent
  following the documented "read status first" contract could not tell a clean
  design from a crashed check. Those failures are now collected on the runner and
  surfaced as warning-level diagnostics in the JSON envelope (still stderr in
  human mode), so a failed pass is visible to the caller.

- **Port-connectivity findings use the same path base as every other finding.**
  Port findings were emitted with `ScopeAnalyzer`'s CWD-relative path (no `./`
  prefix), while every other lint finding — and `xref` — uses the
  `[inputs].root`-relative path (with the `./` prefix) via `LintRunner._rel`; the
  same source file could appear under two different strings in one run. Port
  findings now route through `LintRunner._rel` so their paths match the rest.

- **Config loading on Python 3.8–3.10.** `requirements.txt` listed only
  `pyslang`, so installing from it on Python < 3.11 left `tomllib` unavailable
  and `.rtlscanner.toml` loading failed with `BAD_CONFIG` ("needs tomli"). It now
  pulls the same conditional `tomli` backport that `pyproject.toml` already
  declares.

## [0.3.0] - 2026-06-17

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

- **`segments` on dataflow edges — per-bit permutation maps.** A copy whose
  per-bit offset *varies* — a bit reversal `for (i) rev[i] = din[7-i]`, a half
  swap `o = {a[3:0], a[7:4]}` — is a permutation no single `bit_offset` can
  express, so the edge used to blur to a whole-signal `din -> rev`. The edge now
  carries a `segments` array of `{source_bits, target_bits}` sub-copies, so
  `fanout din` shows `din[7] -> rev[0]`, `din[6] -> rev[1]`, … and a bit-select
  trims the map to just what it asked for (`fanin rev[0]` -> only
  `din[7] -> rev[0]`; a whole-signal query keeps the full map). Additive
  (single-offset and whole-signal edges are unchanged; `segments` is emitted only
  for a true permutation) and requires unrolling — `--no-unroll` keeps the
  conservative whole-signal edge.

### Changed

- **BREAKING: `lint` is now a fixed, opinionated scanner.** The whole
  rule-selection / configuration sub-language is gone, collapsed to a closed set
  of **five check categories** — `semantic`, `unused`, `port`, `cdc`,
  `comb-loop` — chosen with a single flag. `--rules` is now a whitelist that
  accepts **only** those five names plus `all` (no flag = run all five;
  `--rules unused,cdc` = run exactly those). It no longer accepts rule families,
  name globs, or the meta values `default` / `bugs` / `none` / `everything`; an
  out-of-set token now **errors** (exit 2) listing the valid categories instead
  of silently selecting nothing. Removed entirely: `--skip`, `--waive`,
  `--strict`, `--min-severity`, `--waived`, the `shadow` check, and the `[lint]`
  / `[lint.severity]` / `[lint.cdc]` config blocks. Each finding's severity is
  fixed by its category; `lint` exits `1` on any error-severity finding, `0`
  otherwise. Coarse suppression lives at the input layer (`--exclude`); for finer
  filtering, filter the JSON by the `module` field. For a large scan, redirect
  `--json` to a file and read `summary` for the by-category / by-severity counts
  (there is no `--output` flag). The JSON envelope shape is unchanged; the only
  contract change is the narrowed `check` enum (now exactly the five categories)
  — `lint --schema` reflects it. CDC runs with **zero configuration** off a
  built-in reset-name heuristic. The `port-connect` check is renamed `port`.
  Migration: `--rules default,cdc` → `--rules` (or `--rules all`);
  `--rules bugs` / globs / `--skip` / `--waive` / `--strict` /
  `--min-severity` → narrow with the five category names and filter the JSON.
- **CDC and combinational-loop analyses now live in `rtl_lint`** as consumers of
  the shared dataflow engine (`SignalTracer.flow_edges` / `clock_domain_map`),
  rather than as methods on the query-side tracer. No behavior change.

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

- **Bit-select `fanin`/`fanout` no longer reports a false-empty cone for
  generate loops (and multiple bit assigns on one source line).** Every edge
  carries a bit map, but `FlowEdge.key()` excluded it, so several assigns to the
  same `(source, target)` pair on the *same* source line — a generate-for bit
  reversal `for (i) dout[i] = din[7-i]`, or several `assign dout[..]=din[..];`
  on one line — collapsed to the first at edge dedup. `fanin dout[3]` then
  returned *no edges* (claiming `dout[3]` undriven) and whole-signal `fanin dout`
  showed a single misleading bit. The key now includes the bit map, so each is
  its own edge (matching the already-correct one-assign-per-line behavior); whole
  -signal edges carry an empty bit key, so the demand-driven / whole-graph parity
  is unchanged.
- **`fanin`/`fanout` on a multi-bit select keep the *whole* permutation map.** A
  permutation edge (a bit reversal / swap) reached from several frontier bits was
  trimmed to whichever bit-range arrived first, so e.g. `fanin y[1:0]` through a
  reversal dropped one of the two segments (and the survivor was assign-order
  dependent). The walk now collects every range that reaches an edge and trims
  its `segments` to their union.
- **`--rules comb-*` (any glob targeting the comb-loop rule) now runs the
  check.** The `comb-` prefix was missing from the run-family map, so a glob like
  `comb-*` / `comb-loop*` never started the comb-loop pass and silently selected
  zero findings (while the exact token `comb-loop` and `cdc-*` worked).
- **`--waive module:NAME` now reaches `comb-loop`, `cdc-crossing`, and
  port-connect findings.** Those checks built findings without a module, so the
  module-targeted waiver (and the module half of a bare-glob waiver) could never
  match them. Findings are now attributed to their enclosing design unit like
  every other finding.
- **Non-zero-LSB packed vectors (`[8:1]`, `[15:8]`) no longer emit mixed bit
  labels.** `_is_simple_vector` accepted any descending vector, but a non-zero
  low bound means the declared and internal bit numbering differ, so an edge came
  out with the source bit in declared coordinates and the target bit in internal
  ones. Such vectors now fall back to whole-signal granularity (conservative),
  like big-endian `[0:N]` vectors and packed arrays.
- **`lint --rules comb-loop` (and `cdc`) now run on the *pruned* dataflow graph,
  killing constant-dead-branch false positives.** The graph-based lint checks
  built their shared `SignalTracer` without the constant-condition pruning /
  loop unrolling that `fanin`/`fanout`/`trace` enable by default, so a feedback
  path that exists *only* through a constant-false `if`/`case` branch — e.g.
  `assign y = z & a; always_comb if (C) z = y; else z = a;` with `C` a constant
  `0` — was reported as a combinational loop (`top.y -> top.z -> top.y`) even
  though `fanin z` already (correctly) showed no `y -> z` edge. The lint tracer
  now honors the same `[flow]` precision config (defaulting to on), so comb-loop
  and CDC see exactly the dead-branch-pruned graph the flow commands do; a real
  loop with no constant in its path is still flagged.
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

[Unreleased]: https://github.com/neveltyc/RTLScanner/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/neveltyc/RTLScanner/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/neveltyc/RTLScanner/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/neveltyc/RTLScanner/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/neveltyc/RTLScanner/releases/tag/v0.1.0

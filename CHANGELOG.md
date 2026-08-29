# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

This record starts at 1.0.0, the first release of the Rust rewrite.

## [1.1.0] — 2026-08-30

The consumption contract moves to schema v20, aligned with the current
`RTLDebugDBKit` line.

### Changed
- **The reader accepts schema v20 and refuses every other version.** The
  exporter moved the seal from a `meta(key, value)` table to a typed
  `db_info` row; `Db::open` now reads `schema_version` there, and `Db::meta`
  reads the same row by column. A database with no seal row is refused at
  open rather than read as an export with nothing wrong in it.
- **The bundled DDL fixtures are v20.** `crates/designdb/src/ddl/*.sql` are
  re-extracted from `RTLDebugDBKit` at the pinned submodule commit, and the
  fixture builder writes a complete `db_info` row instead of `meta` pairs.
- **The submodule is pinned to the v20 producer.** `extern/RTLDebugDBKit`
  now points at the upstream commit that ships schema v20, so `make designdb`
  and `make sync-ddl` agree with the reader.

### Fixed
- Tests that seeded the old `meta` table now update `db_info`, and the
  test for a self-contradicting seal became a test of v20's `CHECK` rule,
  which makes that state impossible to write.
 What came
before it was a Python program, archived at `v0.1.0` through `v0.5.0`.

## [1.0.2] — 2026-08-27

Answers that were short, wide or unfollowable, and said none of it. Every item
here came out of running the tool over the RTLDebugDBKit construct fixtures and
one large real design, and checking the answers against each other and against
the RTL.

**This release breaks the command line and the envelope.** Three flags are
spelled differently, one moved off a command that ignored it, an unbounded cone
answers a different question, and a query that used to run for a long time can
now stop and say it will not. A caller written against 1.0.x needs reading
before it is pointed at this. Everything that changed is under **Changed**,
with what it was and why it is not that any more.

### Changed
- **A gating condition is named by a cone and no longer followed by it.** Every
  net the value cone reaches contributes the conditions gating its assignments
  — one hop, the same ones `trace` puts in `gates[]` — and the walk stops
  there. It used to follow them like any other dependency, and on real RTL the
  conditions are one connected component: reset gates the state machine, the
  state machine gates the enables, the enables gate the reset logic. An
  unbounded cone therefore returned that component, which is the same set of
  nets for every signal in it. On a large design, three unrelated signals from
  three different subsystems each answered with the identical set of nets, byte
  for byte. That answer was a constant, and a constant is not an answer about a
  signal.

  The same three now answer with tens of nets each. `--follow-ctl` restores
  the old walk, `--no-ctl` still leaves conditions out entirely, and
  `data.control` says which of the three is in force. A depth-1 cone is
  unchanged, so it still equals a `trace`.
- **Every flag naming a condition spells it `ctl`:** `--no-control` is now
  `--no-ctl`, and `trace --control` is now `trace --ctl`, beside the new
  `--follow-ctl`. One idea spelled two ways across one tool is a thing to
  explain forever. No aliases are kept: a deprecated spelling is a second
  contract to maintain, and this tool is young enough that carrying one costs
  more than the rename does.

### Changed
- **`--strip-prefix` is now `--anchor`, and is rarely needed.** It named its
  mechanism — cut this text off the front — where what it states is a fact:
  where this design's root sits in the coordinates your paths are written in.
  The type it fills has been called `Anchor` from the start; only the flag
  disagreed.

  It is also no longer a precondition. Which levels are above this design is a
  hypothesis these rows can test, so a path from a waveform is asked as it
  comes: levels are discarded while none of them names anything at the root,
  and the first that does ends it. A path that reaches the design and then goes
  wrong inside it is a mistake at that level, not a different set of
  coordinates, and is never re-read from further along. The answer names what
  it dropped in `data.anchor.discarded`, because a path reinterpreted in
  silence is one the caller cannot check.

  Stating it is now an override rather than a requirement: a path already
  relative to the design answers alongside one that is not, which is what lets
  `path FROM TO` take one of each. `find` no longer accepts it at all — it
  matches a name, never a path, and took the option without using it. Every
  command that takes a path now echoes `top` and `anchor`; only `trace` did.

  One limit is stated rather than hidden: a path whose only matching level is a
  port of the root is read as that port. That is right for `tb.u_dut.clk` and
  wrong for a path whose scopes were nonsense and whose last level happens to
  share a port's name — `anchor.discarded` is what shows the difference, and
  `--anchor` settles it. On the designs this was measured against, a path with
  a wrong level inside the design is refused rather than re-read in 99.9% of
  cases; the rest are this shape.
- **A failure that reached nothing says so.** Where no level of a path named
  anything in this design, the error used to point at the first level as a
  name to correct and offer a spelling for it. It now reports
  `anchored_elsewhere: true` and names `--anchor`, because the level is not
  misspelled — it is somewhere this design has never been.

### Added
- **A node bound on the walk itself** — `RTLSCANNER_MAX_NODES`, 100 000 nets by
  default, `0` to remove it. `--limit` bounds the answer and leaves the counts
  true, which costs the whole walk; this bounds the walk, because no rule about
  conditions makes a genuinely large value cone smaller — a wide crossbar or an
  array of FIFOs stays six figures of nets with conditions left out
  entirely. Passing it is
  `BUDGET_EXCEEDED` and never a clipped cone: a walk that stopped cannot report
  the true counts, and every count here is the true one. The error names the
  deepest level that did finish, which is a `--depth` the caller can ask for.

  It is an environment variable and not a flag, so it does not appear in
  `command.args`; the error carries `details.max_nodes` instead, since an
  outcome a bound changed has to say what changed it. A value that is not a
  number is rejected on stderr with exit 2, before any command runs. `tree` is
  not subject to it: the hierarchy is finite and small beside a dataflow
  closure, and walking all of it costs seconds.
- **`path` tells "no route" from "gave up looking".** Exhausted and found
  nothing is `status: ok` with `found: false` — the walk covered everything, so
  it is a fact about the design. Stopped by the bound is `status: error` with
  `BUDGET_EXCEEDED`. Folding the second into the first would have made the
  first untrustworthy.
- **A cone edge carries `unreachable`, and `summary.unreachable_edges` counts
  them.** `trace` has said since v19 that a constant condition rules an arm
  out; a cone said nothing, so logic a parameterisation excludes arrived as an
  ordinary dependency. On parameterised RTL that is not a rare case: one net's
  depth-1 fan-in measured 51 edges, 42 of them arms that build cannot take.
- **`summary.unresolved` on a cone**: nets it reached that have an arc it could
  not follow, because the export named the far end and did not resolve it. The
  walk has always stopped there; now the answer says it stopped, so a cone
  short of a hierarchical driver no longer reads like a complete one.
- **`hop.unresolved` on `trace`**, beside `signals`. The two were one list, and
  half of it — a subroutine formal, a path the export could not place — is not
  a signal any command accepts. `signals` is now every name that is a path and
  nothing else, which is what following one is supposed to mean.
- **`info --limit`**, and `summary.shown_sources`. `data.sources` was the one
  list in the tool with no bound: a few hundred files made it tens of
  kilobytes, and `info` is the first thing an agent runs. Every source that is not `current` is listed
  whatever the limit says.
- **`info` counts hierarchical references the export did not resolve**
  (`unresolved_ref_reads`, `unresolved_ref_writes`), which the seal does not
  carry. An unresolved *write* is the one that matters: the net it should have
  driven answers `no_driver_found`, and that reads exactly like a net nothing
  drives. Where there is one, every command now says so.

### Fixed
- **A path whose scope is an escaped identifier holding a `.` did not resolve.**
  `\u.1 .v` was looked up one dot-separated piece at a time and found an
  instance named `\u`, which is not there. A path is now split into levels
  rather than segments — LRM 5.6.1 ends an escaped name at whitespace — so the
  rule the module always claimed, that a name is matched whole, holds at every
  level and not only at the leaf. `find` and `tree` emit these paths, and until
  now no other command took them back.
- **Bits of an object with no single declared range went unreported.** A packed
  multi-dimensional array or a struct has no range to spell indices against, so
  `spell` declined — and the window vanished. Three generate instances writing
  three elements of one array produced three JSON edges identical in every
  field, and `summary.edges` counted all three. Such a span now reads
  `@[hi:lo]`, marked as an offset from the LSB so it cannot be mistaken for a
  declared index. On real RTL this recovers the window on tens of thousands of
  dependency rows.
- **`path` walked the whole design before looking for the goal.** It is
  breadth-first, so the level the goal turns up in is the last one a shortest
  route can need. Between two nets one hop apart on a large design: 12.1 s and
  830 MB, now 0.3 s and 24 MB.
- **The stale-source warning never reached any command but `info`.**
  `trust_notes` built its subject with an empty source list, so the note it
  exists to carry was unreachable from `trace`, `fanin`, `fanout` and `path` —
  the commands whose answers quote lines from those files.
- **`--strip-prefix` did not accept `/`.** Paths take either separator because
  a waveform tool may spell a hierarchy with `/`, and stripping a testbench off
  a waveform path is the whole reason the option exists; the two did not
  compose. Either separator now matches either, and the remainder must still
  start at a segment boundary.
- **A part-select running against its object's declared direction was
  accepted.** `x[0:1]` on `logic [3:0] x` answered about `[1:0]`. LRM 11.5.1
  makes it an error, and so does this now.
- **A hop's `scope` named the instance, never the generate block inside it.**
  Three generate instances of one `always` block share an instance and differ
  only in scope, so all three said the same thing about where they were.
- **`data.direct` was clipped by the cone's `--limit` and said nothing about
  it.** The first hop is the list a caller starts from, and it was whatever the
  edge budget happened to leave: on a net with more than 200 loads the default
  answer was quietly part of one. It is now counted whole (`summary.direct`),
  clipped on its own terms (`summary.shown_direct`), and every name in it is
  still a node of the answer.
- **`find` answered a hierarchical pattern with a silent nothing.** A glob is
  matched against a name, not a path, so `*.osignal` could never match; the
  empty answer now says which of the two it is.
- **A column added to `STMT_COLS` shifted the three read after it.**
  `procedure_writes` read the target's bits at a written-down index; it counts
  the list now, which is the rule the module states and this was the one place
  that did not follow it.

### Known, and not fixed here
- A cone still cannot *follow* an arc whose far end the export named and did
  not resolve — it counts them. The export resolves `$root.top.x` and does not
  resolve `top.x`, which is the same reference; that belongs upstream, and
  compensating for it here would be a layer with a known expiry.
- `u_ren.p.d`, a modport port declared under a name of its own
  (`modport m(output .d(data))`), does not resolve. The schema records a
  terminal's modport by name and nothing about that modport's ports, so there
  is nothing here to resolve it with.

## [1.0.1] — 2026-08-27

Hardening. The suite gets two processors nobody wrote for this tool to run the
invariants over, one comparison against the engine this one replaced — asked
once, to establish that the rewrite kept every answer the predecessor gave —
and a CI that builds and lints every push. And the tool gets a `--help`.

### Added
- **Continuous integration** (`.github/workflows/ci.yml`): the build and
  `make check`, on every push to main and every pull request. The test suite
  is not in it yet, and is run locally.
- **`make test-cores CORES=<dir>`**, the strictest run: the whole suite with
  both the exporter and a corpus of real processors made mandatory. It adds the
  invariants over picorv32 and tinyriscv — checkouts at the commits pinned in
  the test's own header, not vendored — with no expected values anywhere:
  fan-in against fan-out, a bounded cone inside an unbounded one, a full-width
  window against no window, one hop against a trace, a clipped answer's counts
  against the whole. The fixtures cover what those cannot reach, which is a
  defect two walks share.
- **A path may name a modport view.** `b.mst.vld`, `b.vld` and `u_p.p.vld` are
  one net; all resolve, and the answer gives the net's own path. The database
  records no modport declaration, so this rests on the `modport` a port taking
  the view carries: a view no port anywhere takes stays unknown.
- **`summary.structural_hops` on `trace`**, beside `hops`. Under `--control` a
  net read only by branch conditions answered `no_load_found` with those
  conditions listed below it. `status` deliberately does not move when
  `--control` changes how much is shown, so what was missing was the count the
  verdict rests on. With `--control` off the two are equal.
- **A hand-written `--help`.** One page, under sixty lines: the commands with
  their arguments, then every option under the commands it belongs to — which
  the generated help cannot spell. `-h` and `--help` are the same text
  everywhere, and the free-standing `help` subcommand is gone.
- `rustfmt.toml`, so `cargo fmt --check` has a style to check against.

### Fixed
- **An interface port that binds an array answered about one element under a
  name that did not say which.** `u_a.q.vld` on a `bus_if[2]` named
  `barr[0].vld`. The export records one binding per element; picking the first
  is a guess. The path is now refused as an ambiguous name is, with the
  elements named so the next call is a correction rather than a search.
- **A name mistyped after an interface port was blamed on the port.**
  `u_p.p.nope` reported `p` — a segment that resolves — as the failing one and
  offered no candidates, because the diagnosis did not follow the detours the
  walk takes. It follows them now, so the segment named is the one that failed
  and the candidates are the level it failed in.

### Changed
- **The worked answers in `examples/` no longer pin the exporter's commit.**
  `producer_revision` moves whenever the kit does, whether or not anything an
  answer shows has changed, so it is rewritten in the comparison as the host
  paths already were. `config_digest`, `tool_version` and `slang_version` are
  compared as they stand — those say what the producer is, and a change in one
  is a change worth failing on.

### Internal
- **`make check` could not pass**, so nothing ran it: there was no format
  configuration, which made the gate demand reflowing the tree, and thirteen
  clippy findings sat underneath. Both are fixed rather than suppressed; the one
  allowance is `dead_code` on the test helpers, which every integration binary
  compiles separately and only some of them call.
- **`tools/differential/`** — a one-shot migration gate against the Python
  engine at `v0.5.0`, over that history's own eleven example designs plus this
  one's: 1696 comparisons, 1147 with an oracle fact to find, no unexplained
  miss. Four rules say what a difference is, each one the rewrite meant to
  make, each pinned from both sides by tests needing neither the oracle nor an
  exporter, and the gate was run against deliberately lossy builds as a
  control. It retires at 2.0; nothing in the build, the suite or CI depends on
  it.
- Performance re-measured at release on three cores (picorv32, tinyriscv,
  veerwolf — 18k nets, 1931 instances): every query a person types is around
  ten milliseconds, and only walking a whole design and printing all seventeen
  thousand edges reaches a third of a second.

## [1.0.0] — 2026-08-27

First release of the Rust line. RTLScanner reads an `rtl-designdb` design
database and answers the code-side questions behind a signal: what drives it,
what reads it, which statement did it and under which conditions, what it
depends on transitively, and whether a route exists between two signals. See
the [README](README.md) for the command surface; this entry records what is
true of the release rather than repeating it.

### Highlights
- **Seven commands, each answering in one JSON envelope**: `info`, `tree`,
  `find`, `trace`, `fanin`, `fanout`, `path`. A failure is a well-formed
  envelope on stdout with a non-zero exit, so a caller reads the outcome before
  the answer; `*_NOT_FOUND` carries the level, the failing segment and close
  matches, so the next call is a correction.
- **No RTL and no waveform are read.** Structure comes from the exported rows
  and nowhere else, so an answer is what the export recorded, at the precision
  it recorded it. The one exception is quoting a line, and only from a file
  that still hashes to what was exported.
- **The schema version is a contract**: a database of any version but 19 is
  refused rather than read as though its columns had held still.
- **`info` is the trust panel** every other command depends on. The exporter
  writes a database and exits 0 even when elaboration errored, so whether an
  answer can be trusted is the consumer's to establish: the analysis status,
  the counts behind it, and a per-file source drift check.
- **`trace` attributes to the statement.** A hop carries the construct, the
  assignment kind, the operands it reads, the conditions gating it with which
  arm of each and its priority among siblings, the events its procedure runs
  on, and the line it was quoted from. Where a procedure writes the signal more
  than once, the writes come back in execution order with the bits each
  touches. Which of them was in effect at some moment is not a structural fact
  and is not claimed.
- **`fanin`, `fanout` and `path` follow those hops outward**, context-sensitive
  across subroutine calls, carrying a bit window across each hop for as long as
  the correspondence is exact and widening to the whole object rather than
  naming bits that might be the wrong ones. `--comb` ends a walk at the state
  elements that end the cycle; `--through-latch` crosses one anyway.
- **A bit-select is a question, not just an answer.** `bus[3]` narrows a cone
  to what feeds those bits, and `exact` tells an upper bound from a
  measurement.
- **Clipping stays honest**: the counts are of the whole cone rather than the
  window, the note says how to lift the limit, and an edge is never kept whose
  endpoints were dropped.

### Versioning
- Numbered 1.0.0 rather than 0.0.1 so the two generations in this repository
  are told apart by major version alone: `0.x` reads as the Python line, `1.x`
  as this one. The envelope's `version` is the crate's.


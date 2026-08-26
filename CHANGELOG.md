# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

This record starts at 1.0.0, the first release of the Rust rewrite. What came
before it was a Python program, archived at `v0.1.0` through `v0.5.0`.

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


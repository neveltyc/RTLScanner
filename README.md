# RTLScanner

Signal trace, driver and load analysis over an
[rtl-designdb](https://github.com/neveltyc/RTLDebugDBKit) design database.

The RTL is elaborated once, by `rtl-designdb`, into a SQLite file. RTLScanner
queries that file: who drives a signal, who reads it, which statement did it, at
what bit range, under which branch. It reads no RTL and no waveform, so an
answer is what the export recorded, at the precision the export recorded it.

This is the code side of a debug pair. A waveform tool says which signal carried
what at time T; RTLScanner says which statement in which file put it there. What
they have in common is the hierarchical path, so an agent holding both can join
them.

**Status: in development.** `info` and `trace` work; fan-in, fan-out and path
follow. See [doc/implementation-plan.md](doc/implementation-plan.md).

## Requires

* Rust 1.90+ (`make check` also needs the `clippy` and `rustfmt` components)
* `rtl-designdb`, schema **v19** — pinned as a submodule in
  `extern/RTLDebugDBKit`. `make designdb` builds it (slow the first time: the
  build fetches and compiles slang).

The schema version is a consumption contract, not a suggestion: a database of
any other version is refused rather than read as though its columns had held
still.

## Use

```bash
cargo build
./target/debug/rtlscanner info design.db
./target/debug/rtlscanner trace design.db top.u_core.u_alu.result
./target/debug/rtlscanner trace design.db 'top.u_core.status[3]' --load
./target/debug/rtlscanner trace --json design.db tb.dut.q --strip-prefix tb
```

`info` answers what every other command depends on: which schema version, what
produced it, which tops it covers, whether the analysis is `complete` or fell
short — the export writes a database and exits 0 even when elaboration errored —
and whether the source files still hash to what was exported, since a location
in a file that has moved on names a line that is no longer the one.

`trace` answers one hop: every arc the export recorded for that signal, grouped
by the statement, gate or port crossing that produced it. A hop carries the
statement's own words — its construct, its assignment kind, the operands it
reads, the conditions that gate it and which arm of each it is on, the events
its procedure triggers on, and the line it was quoted from where the file still
hashes to what was exported. Which of several drivers was in effect at some
moment is not a structural fact and is not claimed; the material to decide it
is in the answer.

Every command answers in one JSON envelope: `tool`, `version`, `status`,
`command`, `data`, `diagnostics`, `errors`, `summary`. A failure is a
well-formed envelope on stdout with a non-zero exit, so a caller reads the
outcome before the answer rather than parsing stderr. The exception is a
command line clap itself rejects — an unknown flag, a missing argument — which
is a usage error on stderr with exit 2, before any command runs.

## Layout

```
crates/designdb     the v19 reader: version gate, contract views, digests
crates/rtlscanner   the CLI: envelope, commands
extern/RTLDebugDBKit  the producer, pinned to the schema version above
doc/                design and the plan it follows
```

`make test` runs the tests. Most build their database from the kit's own DDL, so
they need no exporter; the few that must see what the current producer writes
skip without one, and `make test-exporter` builds it and makes them mandatory.
`make check` runs clippy and rustfmt; `make sync-ddl` re-extracts the DDL the
fixtures build from, and refuses to leave it disagreeing with the schema version
this reader accepts.

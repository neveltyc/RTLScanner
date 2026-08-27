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

**Status: in development.** All seven commands work: `info`, `tree`, `find`,
`trace`, `fanin`, `fanout`, `path`.

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
./target/debug/rtlscanner trace --json design.db tb.dut.q   # anchored at a testbench
./target/debug/rtlscanner fanin design.db top.u_core.status --depth 6
./target/debug/rtlscanner fanin design.db top.u_core.status --comb   # this cycle's logic
./target/debug/rtlscanner path design.db top.a top.u_core.result
./target/debug/rtlscanner tree design.db --depth 2       # what is this design
./target/debug/rtlscanner find design.db 'req*'          # where does that name live
```

Worked answers to each of these, on a design small enough to read whole, are in
[examples/](examples/). They are generated from this tree and a test
regenerates them to compare, so what they show is what the tool currently says.

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

The names a hop gives are in two lists and the split is the point: `signals`
holds paths, every one of which the next command accepts, and `unresolved`
holds the far ends the export named and resolved to nothing — a subroutine
formal, a hierarchical path it could not place. Mixing them would make half of
`signals` a question that answers `SIGNAL_NOT_FOUND`.

`tree` and `find` are where a path comes from when you do not have one yet:
what the design is made of, and where a name lives. What `find` returns, every
other command accepts.

A path may also be spelled the way a designer or a waveform writes it. A
modport view is a level of a path with nothing behind it — `b.mst.vld` and
`b.vld` are one net — so the view is crossed and the answer gives the net's own
path. Where the design has several tops, `--top` says which.

A path from a waveform is anchored at the testbench that ran the simulation,
where this design is one instance among others; the levels between are in
neither world, since the export never elaborated them. Which ones they are is a
hypothesis these rows can test, so it is tested rather than demanded: the path
is asked as it comes and the answer names what it dropped in
`data.anchor.discarded`. `--anchor` states it instead, for the case where a
testbench happens to name its instance after something this design also has.

`fanin` and `fanout` follow those hops outward, and `path` reads the walk
backwards to give one route. A cone stops where the caller says: after so many
hops, or — with `--comb` — at the state elements that end the cycle, latches
included, since a latch holds a level. `--through-latch` crosses one anyway,
which is what a glitch, a loop closing through it, or a pulse-latch borrow is
about.

A condition is named and not followed. Every net the value cone reaches
contributes the conditions gating its assignments — one hop, the same ones a
`trace` hop carries — and the walk stops there, because where a gate's own
value came from is a question about the gate. On real RTL the conditions form
one connected component, so following them transitively returns that component
rather than an answer about the signal asked for: `--follow-ctl` does it
anyway, and `--no-ctl` leaves conditions out altogether.

Two bounds, and they are different. `--limit` bounds the answer and leaves the
counts true, which costs the whole walk. `RTLSCANNER_MAX_NODES` bounds the walk
— 100 000 nets by default, `0` to remove it — because a wide crossbar or an
array of FIFOs has a value cone that really is most of the design. Passing it
is an error, never a short answer: a walk that stopped cannot report what it
did not reach, and from `path` that is why "no route" and "gave up looking" are
different outcomes rather than one.

A bit-select narrows a cone to what feeds those bits, and carries the window
across each hop for as long as the correspondence is exact — widening to the
whole object rather than naming bits that might be the wrong ones, and saying
how often it had to. A window on an object with no single declared range — a
packed multi-dimensional array, a struct — is spelled `@[hi:lo]`, an offset
from the LSB rather than a declared index it has no right to claim.

Where an answer is short, it says so rather than looking complete: an arm a
constant condition rules out is reported and marked `unreachable`, and an arc
whose far end the export named but did not resolve is counted in
`summary.unresolved` — the cone cannot walk to a name that resolves to no net,
and `trace` on that signal is what names it.

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
tools/differential  the one-shot gate against the engine this replaced
```

An agent driving this tool should read [skill/SKILL.md](skill/SKILL.md), which
says which command answers which question and how to read what comes back.

`make test` runs the tests. Most build their database from the kit's own DDL, so
they need no exporter; the few that must see what the current producer writes
skip without one, and `make test-exporter` builds it and makes them mandatory.
`make test-cores CORES=<dir>` adds the invariants over real processors — two
checkouts under that directory, at the commits pinned in
`crates/rtlscanner/tests/real_cores.rs` — and makes those mandatory too.
CI builds and runs `make check`; the test runs are local, for now.

`make check` runs clippy and rustfmt; `make sync-ddl` re-extracts the DDL the
fixtures build from, and refuses to leave it disagreeing with the schema version
this reader accepts; `make examples` regenerates the worked answers.

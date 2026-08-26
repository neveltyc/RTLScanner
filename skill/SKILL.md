---
name: rtlscanner
description: >-
  Answer structural questions about a SystemVerilog design from an exported
  design database: what drives a signal, what reads it, which statement did it
  and under which conditions, what a value depends on transitively, and whether
  a route exists between two signals. Use when you have RTL (or a design
  database built from it) and need the code-side facts behind a signal —
  typically after a waveform tool has pointed at a signal and a time, or when
  reading unfamiliar RTL. Not a simulator: it reports what the design is, never
  what it did at some moment.
---

# RTLScanner

The RTL is elaborated once, by `rtl-designdb`, into a SQLite file. This tool
queries it. Everything it reports comes from those rows — it reads no waveform,
and reads source only to quote a line it can verify — so an answer is what the
export recorded, at the precision the export recorded it.

**It supplies facts; the reasoning is yours.** Which of several drivers was in
effect at some moment, whether a value is wrong, what the root cause is — none
of those are structural facts, and the tool does not claim them. What it does
is put the material in front of you: the statement, its conditions and which
arm of each, the order the assignments run in, the bits each touches, and how
far the answer can be trusted.

## Two rules

1. **Always pass `--json`.** Every command answers in one envelope. Read
   `status` first: `"ok"` means `data` and `summary` hold the answer, `"error"`
   means `errors[0].code` says what went wrong and `errors[0].details` usually
   says how to fix it. A failure is still a well-formed envelope on stdout —
   never parse stderr.
2. **Run `info` first on a database you have not used.** The export writes a
   file and exits 0 even when elaboration errored. `info` says whether the rows
   cover the design and whether the source still matches; a `no_driver_found`
   from an export that skipped the driving procedure looks exactly like a
   signal nothing drives.

## Commands

| You want | Command | Read from |
|---|---|---|
| Whether this database can be trusted | `info <db>` | `data.analysis`, `data.sources`, `diagnostics` |
| What the design is made of | `tree <db> [scope] [--depth N]` | `data.levels[]` |
| Where a name lives | `find <db> <glob> [--instances\|--modules]` | `data.hits[].path` |
| What drives / reads this signal | `trace <db> <path> [--load]` | `data.hops[]` |
| What it depends on, transitively | `fanin <db> <path> [--depth N] [--comb]` | `data.edges[]`, `data.direct` |
| What depends on it | `fanout <db> <path> …` | same |
| Whether a route exists | `path <db> <from> <to> [--comb]` | `data.found`, `data.nodes[]` |

Every command takes `--top <name>` where the design has several tops, and
`--strip-prefix <scope>` to accept a path anchored at a testbench the design has
never heard of.

## Working from a waveform finding

A waveform tool says *which signal, at what time, carrying what*. This says
*which statement put it there*. The two meet on the hierarchical path.

1. **`--strip-prefix`** — a waveform path is usually `tb.u_dut.…` while the
   design's own root is `u_dut`'s module. Pass the testbench scope once.
2. **`trace <signal>`** — the statement, its file and line, the conditions it
   sits under and which arm of each, the events its procedure runs on, and the
   operands it reads.
3. **Read `gates[]` against the values you have.** Each level says what it is
   (`if`/`case`/`case_item`/`loop`), which arm (`sense`), which labels
   (`labels`), and its priority among siblings (`ordinal`). With the values at
   time T you can decide which arm ran — the tool will not decide it for you.
4. **Read `procedure_writes[]` where it is present.** A procedure that assigns
   the signal more than once lists all of them in execution order, marking the
   one this hop is about and which are unconditional. A `y = '0` before a case
   is a default the arms overwrite; later overwrites earlier.
5. **Follow `signals[]` into the next `trace`**, or ask `fanin` for the whole
   cone at once. Stop when `status` is `boundary_only` (the value comes from
   outside — the named signal is where to look next) or when a hop is
   `constant`, `terminal` or `external`.

## Reading a trace hop

- `kind` — folded: `procedural`, `continuous_assign`, `port`, `constant`,
  `gate`, `alias`, `external`, `system_task`, `trigger`, `terminal`,
  `sensitivity`, `control`, `call`, `other`. `raw_kind` is the database's own
  word, which is finer and may be a word this list does not have.
- `statement` and `source` — the quoted line, and whether it can be trusted.
  `source: "stale"` means the file has changed since the export: the line
  number is real, the text at it is not, and nothing is quoted.
- `bits` — a list, because a statement may write several windows.
  `bits_exact: false` means the range is an upper bound, not the bits actually
  touched — a dynamic index, or a mapping that lost precision at a boundary.
- `unreachable: true` — a constant condition rules this arm out at this
  parameterisation. It is still reported (the statement is in the design) and
  not counted as a driver.
- `call_chain` — where a statement came from a subroutine body, the calls that
  reached it. Two calls of one task are two sets of rows, and this is what tells
  them apart.
- `summary.drivers` counts **sources**, not statements: a procedure drives as a
  whole, so an `if`/`else` writing one variable is one driver reported as two
  statements. `summary.multiple_drivers` is true only where two different
  sources write overlapping bits — that is a contest; a signal assembled from
  disjoint slices is not.

## Reading a cone

- `data.direct` is the nets one hop out — usually what you want first.
- `--comb` stops at state elements, so the answer is the logic that settles in
  one cycle. Latches count as state; `--through-latch` crosses one, which is
  what a glitch, a loop closing through it, or a pulse-latch borrow is about.
  An edge with `ends_at_state: true` is where a cone stopped, and
  `summary.stopped_at_state` counts them — that is how an empty answer says
  "the value is last cycle's" rather than "nothing drives this".
- **Conditions are followed by default and are usually most of a cone.**
  `summary.control_edges` says how many; `--no-control` leaves them out when
  you want only the values. (`trace` is the other way round: its `gates[]`
  already carry the conditions, so `--control` there is opt-in.)
- `--depth N` bounds the walk, `0` is unbounded. `--limit` bounds the output
  and the counts stay true: `summary.edges` is the whole cone,
  `summary.shown_edges` is what you were given.
- `summary.widened` counts hops where a bit-select could not be carried across
  and the question widened to the whole object. Nothing is lost by it — the
  answer is wider, not wrong.

## Errors worth handling

| Code | What to do |
|---|---|
| `SIGNAL_NOT_FOUND` / `SCOPE_NOT_FOUND` | `details` has `close_matches`, `valid_prefix` and what is at that level. The next call is a correction, not a search. |
| `BAD_SELECT` | An aggregate has no single declared range to select from. Trace the whole object. |
| `NO_TOP` | Several tops; the message lists them. Pass `--top`. |
| `DB_UNREADABLE` | Wrong schema version or not a database. Re-export with a matching `rtl-designdb`. |

## What it will not tell you

- **Which driver won.** That is simulation. The conditions, the order and the
  bits are here; the decision is yours.
- **Which signal is the clock.** Both events of `@(posedge clk or negedge rst)`
  are reported and neither is elected.
- **What is inside a black box.** A trace that ends at an `unresolved` instance
  ended at something the export has no rows for — different from ending at a
  signal nothing drives.
- **Anything about time.** No values, no waveform, no "at cycle N".

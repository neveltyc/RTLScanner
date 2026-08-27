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
   signal nothing drives. Where that risk is real — a source that has moved on,
   a hierarchical write the export could not place — every command says so in
   `diagnostics`, not only `info`.

## Commands

| You want | Command | Read from |
|---|---|---|
| Whether this database can be trusted | `info <db> [--limit N]` | `data.analysis`, `data.sources`, `diagnostics` |
| What the design is made of | `tree <db> [scope] [--depth N]` | `data.levels[]` |
| Where a name lives | `find <db> <glob> [--instances\|--modules]` | `data.hits[].path` |
| What drives / reads this signal | `trace <db> <path> [--load]` | `data.hops[]` |
| What it depends on, transitively | `fanin <db> <path> [--depth N] [--comb]` | `data.edges[]`, `data.direct` |
| What depends on it | `fanout <db> <path> …` | same |
| Whether a route exists | `path <db> <from> <to> [--comb]` | `data.found`, `data.nodes[]` |

`path` has three outcomes and they are not two: a route (`ok`, `found: true`),
no route (`ok`, `found: false` — the walk covered everything, so this is a fact
about the design), and a walk that gave up (`error`, `BUDGET_EXCEEDED` — it
covered nothing like everything, so it says nothing).

Every command except `info` takes `--top <name>` where the design has several
tops. **A path anchored at a testbench needs nothing else**: the levels above
this design are worked out from the path and named in `data.anchor.discarded`,
so `tb.u_dut.u_core.q` is asked as it comes. `--anchor <path>` states where the
root sits when you want to be sure; it is an override, not a requirement, so a
path already relative to the design still answers alongside one that is not.

`tree` shows three levels unless told otherwise; `summary.depth_truncated` says
when that cut the walk short, and `--depth 0` reaches the rest. Everywhere a
`--limit` appears, `0` means no limit. `find` stops after 5000 matches —
`summary.capped` says so, and no `--limit` reaches past it; narrow the pattern
instead.

`find` matches a **name**, never a path: `*.osignal` and `top.u_x.osignal`
match nothing whatever the design holds, and the answer says which of the two
that is. A net declared inside a generate block or a subroutine is named
through it (`lane[0].sig`), so a bare name will not match one either: try
`*sig`.

A path may be spelled with `.` or `/` at any level, including under `--anchor`,
and an escaped identifier is one level however many dots it holds —
`top.\u.1 .v` names the net `v` inside the instance `\u.1 `.

A modport view is a level of a path with no object behind it: `b.mst.vld` and
`b.vld` are one net, and so is `u_p.p.vld` through the port that takes the
view. All three resolve, and the answer gives the net's own path — which is the
one `find` returns and the one to use again. An interface port that binds an
array is the exception: `q.vld` has not said which element, so it is refused
with the elements named rather than answered about the first.

`trace`'s `summary` carries two counts and they are not the same: `hops` is
everything the answer holds, `structural_hops` is what `status` rests on. They
differ only where an alias or — under `--ctl` — a condition is the answer,
so `no_load_found` with `hops: 2` and `structural_hops: 0` means the net is
read as a condition and never for its value.

## Working from a waveform finding

A waveform tool says *which signal, at what time, carrying what*. This says
*which statement put it there*. The two meet on the hierarchical path.

1. **Paste the path as the waveform spells it.** The levels above this design
   are worked out and reported in `data.anchor.discarded`; read that back to
   confirm what was dropped. Two limits worth knowing: a path that reaches the
   design and then goes wrong inside it is reported as a mistake at that level
   and never re-read from further along, and a path whose *only* matching level
   is a port of the root is taken as that port — which is right for
   `tb.u_dut.clk` and wrong for a path whose scopes were nonsense and whose
   last level happens to share a port's name. `--anchor <path>` settles it.
2. **`trace <signal>`** — the statement, its file and line, the conditions it
   sits under and which arm of each, the events its procedure runs on, and the
   operands it reads.
3. **Read `gates[]` against the values you have.** Each level says what it is
   (`if`/`case`/`case_item`/`case_default`/`loop`), which arm (`sense`), which
   labels (`labels`), and its priority among siblings (`ordinal`). With the
   values at time T you can decide which arm ran — the tool will not decide it
   for you.
4. **Read `data.procedures[]` where a procedure writes the signal more than
   once.** Each entry lists its writes in execution order with the bits each
   touches; a hop names its procedure by index (`hop.procedure`) and its own
   place in it by `hop.sequence`. A `y = '0` before a case is a default the
   arms overwrite — but *later overwrites earlier only where the two touch the
   same bits*, and two writes to disjoint windows never meet. `unconditional`
   means no condition gates that write; it does not by itself mean the write
   held.
5. **Follow `signals[]` into the next `trace`**, or ask `fanin` for the whole
   cone at once. Stop when `status` is `boundary_only` (the value comes from
   outside — the named signal is where to look next) or when a hop is
   `constant`, `terminal` or `external`.

## Reading a trace hop

- `kind` — folded: `procedural`, `continuous_assign`, `port`, `constant`,
  `gate`, `alias`, `external`, `system_task`, `trigger`, `terminal`,
  `sensitivity`, `wait`, `statement`, `control`, `call`, `other`. `raw_kind` is
  the database's own word, which is finer and may be a word this list does not
  have.
- `statement` and `source` — the quoted line, and whether it can be trusted.
  `source: "stale"` means the file has changed since the export: the line
  number is real, the text at it is not, and nothing is quoted.
- `bits` — a list, because a statement may write several windows.
  `bits_exact: false` means the range is an upper bound, not the bits actually
  touched — a dynamic index, or a mapping that lost precision at a boundary.
  A window reading `@[hi:lo]` is an offset from the LSB of the flattened
  object, not a declared index: the object has no single declared range to
  spell one against, which is what a packed multi-dimensional array or a struct
  is. Two windows of one such object are still two different windows.
- `signals` — the nets at the other end, every one a path the next `trace`
  accepts. `unresolved` beside it is the other end where the export named it
  and resolved it to nothing: a subroutine formal, or a hierarchical path it
  could not place. Those are reported and are not askable; a cone cannot even
  name them, only count them.
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

- `data.direct` is the nets one hop out — usually what you want first. It has
  its own bound and its own counts: `summary.direct` is the whole first hop,
  `summary.shown_direct` is how much of it you were given. `--limit` bounds the
  cone and the first hop separately, so a deep cone never costs you the list
  you start from.
- `--comb` stops at state elements, so the answer is the logic that settles in
  one cycle. Latches count as state; `--through-latch` crosses one, which is
  what a glitch, a loop closing through it, or a pulse-latch borrow is about.
  An edge with `ends_at_state: true` is where a cone stopped, and
  `summary.stopped_at_state` counts them — that is how an empty answer says
  "the value is last cycle's" rather than "nothing drives this".
- **A condition is named, and not followed.** Every net the value cone reaches
  contributes the conditions gating its assignments — one hop, the same ones
  `trace` puts in `gates[]` — and the walk stops at them. Where a gate's own
  value came from is a question about the gate: ask it about the gate.
  `summary.control_edges` counts them; `data.control` says which rule is in
  force (`direct` by default, `none` under `--no-ctl`, `full` under
  `--follow-ctl`).

  `--follow-ctl` follows them on instead. Expect that to answer about the
  design rather than about your signal: on real RTL the conditions form one
  connected component — reset gates the state machine, the state machine gates
  the enables — so an unbounded cone through them returns that component. That
  is the same set of nets for every signal in it, to the net, whichever one you
  asked about.
- `--depth N` bounds the walk, `0` is unbounded. `--limit` bounds the output
  and the counts stay true: `summary.edges` is the whole cone,
  `summary.shown_edges` is what you were given.
- **A walk has a node bound of its own**, because a wide crossbar or an array
  of FIFOs has a value cone that really is most of the design and no rule about
  conditions makes it smaller. Passing it is `BUDGET_EXCEEDED`, never a short
  answer: a clipped walk cannot report the true counts, and every count here is
  the true one. It is 100 000 nets, and the operator sets
  `RTLSCANNER_MAX_NODES` (`0` removes it) — not a flag, so an error that a
  bound caused carries the number in `details.max_nodes`.
- `summary.widened` counts hops where a bit-select could not be carried across
  and the question widened to the whole object. Nothing is lost by it — the
  answer is wider, not wrong.
- **`unreachable: true` on an edge** is the same fact `trace` reports: a
  constant condition rules that arm out at this parameterisation. The edge is
  kept, because the statement is in the design; `summary.unreachable_edges`
  counts them, and on parameterised IP they are often most of a cone.
- **`summary.unresolved`** counts nets in the cone with an arc the walk could
  not follow, because the export named the far end and did not resolve it.
  That is the one way a cone is short without saying so anywhere else —
  `trace` those nets and `hop.unresolved` names what is missing.

## Errors worth handling

| Code | What to do |
|---|---|
| `SIGNAL_NOT_FOUND` / `SCOPE_NOT_FOUND` | `details` has `close_matches`, `valid_prefix` and what is at that level. The next call is a correction, not a search. `anchored_elsewhere: true` means no level of the path named anything here — it is not a spelling to fix but a path from another world, and `--anchor` says where this design sits in it. |
| `BAD_SELECT` | Either an aggregate with no single declared range to select from — trace the whole object — or two options that ask for different things. The message says which. |
| `NO_TOP` | Several tops; the message lists them. Pass `--top`. |
| `DB_UNREADABLE` | Wrong schema version or not a database. Re-export with a matching `rtl-designdb`. |
| `BUDGET_EXCEEDED` | The cone passed the walk's node bound and stopped, so nothing is known about what it had not reached — this is **not** an empty cone and, from `path`, **not** "no route". `details.last_complete_depth` is the deepest level that did finish: ask for that `--depth`, or narrow with `--comb` / `--no-ctl`. |

## What it will not tell you

- **Which driver won.** That is simulation. The conditions, the order and the
  bits are here; the decision is yours.
- **Which signal is the clock.** Both events of `@(posedge clk or negedge rst)`
  are reported and neither is elected.
- **What is inside a black box.** A trace that ends at an `unresolved` instance
  ended at something the export has no rows for — different from ending at a
  signal nothing drives.
- **Anything about time.** No values, no waveform, no "at cycle N".

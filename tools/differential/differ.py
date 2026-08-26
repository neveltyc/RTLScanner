#!/usr/bin/env python3
"""The migration gate: every answer the Python engine gave, this one still gives.

The predecessor on `dev-python-bk` read RTL through pyslang and answered the
same questions this tool answers from a database. Nothing says the rewrite kept
all of them, and this repository's tests cannot say so: they were written
alongside the rewrite and share its idea of what the answers are. The oracle
does not — it was written against these same designs, from a different model —
so running both over its own examples is the one check available that this
tool's authorship did not shape.

## What is compared

The two answer in different shapes, so what is compared is the smallest fact
each shape carries.

* `tree` — the set of instance paths. Both spell one the same way.
* `fanin`/`fanout` — the set of node paths, and the set of edges as the pair of
  paths each joins.
* `path` — whether a route was found. Which route is not compared: both walk
  breadth first over relations that now differ in places, so the shortest one
  is not the same object on both sides.
* `trace` — the set of source locations, by file and line.

Bits are not compared anywhere. The oracle unrolls a loop into per-bit edges
and this tool publishes the loop's iteration space instead; both say the same
thing about which signals reach which, at different grain, and the grain is a
recorded decision rather than a regression.

## The direction, and why it is not symmetric

A miss — an oracle fact this tool does not have — fails. An extra does not: the
rewrite was undertaken to answer more, and `README.md` beside this file lists
what more. But an extra is not free either, since a walk that invents endpoints
also produces extras: they are counted, and `--max-extra` pins the count, so a
change that manufactures them shows up as a number that moved.

## The exemptions

Four, each a rule rather than a list of blessed signals, and each a difference
the rewrite meant to make:

* `covers` — the oracle attributes a driver to the *construct* and this tool to
  the *statement* inside it, so a line is covered by a line at or below it,
  bounded by the next location the oracle itself named and in any case by
  `endmodule`. Comparing lines for equality would fail on every procedure in
  the corpus while both name the same block; leaving it unbounded would let an
  unrelated block further down stand in for the right one.
* `port_crossing` — the oracle attributes an input port to the formal's
  declaration and says only that something outside drives it; this tool names
  what is bound there, at the instantiation. What is waived is which end to
  point at, so the tool's answer must at least be a kind of thing that can
  stand at one. WHICH net is on the other side the oracle never says, and the
  cone comparison checks it without exemption.
* `concatenated` — the oracle joins every operand of a concatenation to every
  target of one, and this tool follows the correspondence the database records.
  `{r1, r2} <= {d, r1}` is two assignments the oracle reports as four edges.
  The operand must be inside the braces and in the same instance as the
  target, or `assign y = {a, b} | c;` losing `c` would be forgiven.
* `EXEMPT_UNASKABLE` — the oracle cannot be asked about a signal inside a
  generate block, by either spelling. Its own `find` lists them and its own
  cones reach them; only the question is unavailable. Those are counted, not
  dropped, because a question with no oracle answer is not evidence either way
  — and only the two failures it is documented to have count, since waiving
  every failure would let a timeout remove a whole family of questions.

`test_differ.py` pins each from both sides: the shape it must classify, and the
shape it must leave alone. A rule that waives too much turns a gate that proves
something into one that proves nothing, and a green run is the output of the
rules as much as of the tool.

This is a one-shot migration gate — it retires when 2.0 ships, and the
regression line after that is the golden tests plus the invariants, which need
no oracle. `README.md` beside this file says how to run it.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from itertools import permutations
from pathlib import Path

# The oracle spells "no bound" as a number and this tool spells it as 0. Every
# design in the corpus is a few levels deep, so this is past all of them.
ORACLE_UNBOUNDED = 64

# How many of a design's signals are paired off for `path`. Every ordered pair
# is asked, so this is quadratic; the routes worth checking are between things
# a few hops apart, which a sample of one design's signals gives.
PATH_SAMPLE = 6

EXEMPT_UNASKABLE = "the oracle refuses the question"

# What "refuses" means. The oracle cannot be asked about a signal inside a
# generate block — its own `find` lists them and its own cones reach them, but
# neither spelling of the path resolves — and it says so in one of two ways.
# Any OTHER failure of the oracle is a failure to compare, not a difference to
# waive: a timeout or an internal error on a family of questions would
# otherwise remove that family from the gate and leave a number nothing pins.
ORACLE_NOT_FOUND = re.compile(r"^(signal|scope) '[^']*' not found")
EXEMPT_PORT_END = "a port's drive, named at what is connected rather than at the declaration"
EXEMPT_CONCAT = "a concatenation's bit correspondence, which the oracle joins end to end"


class Answer(dict):
    """One command's answer, or the error that came back instead."""

    @property
    def failed(self):
        return self.get("status") != "ok"


def run_oracle(cli, sources, queries):
    """One `batch` session: {index: Answer}.

    The oracle recompiles the RTL per process, which is the cost this
    comparison would otherwise pay once per question rather than once per
    design. `--limit 0` is not optional: the default clips a list at 200, and
    a clipped answer would report the difference as a miss.
    """
    lines = "".join(f"{q} # {i}\n" for i, q in enumerate(queries))
    p = subprocess.run(
        [cli, "batch", *sources, "--json", "--limit", "0"],
        input=lines, capture_output=True, text=True, timeout=3600,
    )
    out = {}
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[int(row["id"])] = Answer(
            row["result"] if row.get("ok") else {"status": "error", "error": row.get("error", "")}
        )
    if len(out) != len(queries):
        sys.exit(f"the oracle answered {len(out)} of {len(queries)}: {p.stderr[-2000:]}")
    return out


def run_scanner(scanner, db, argv):
    p = subprocess.run(
        [scanner, argv[0], "--json", str(db), *argv[1:]],
        capture_output=True, text=True, timeout=3600,
    )
    return Answer(json.loads(p.stdout))


def export(exporter, design, db):
    """Export every top, because several of these designs are several designs.

    No `--top`: the oracle elaborates each unreferenced module as its own, and
    naming one here would leave the rest unanswerable and read as a rewrite
    that lost them.
    """
    p = subprocess.run(
        [exporter, design.name, "-o", str(db), "-q"],
        cwd=design.parent, capture_output=True, text=True, timeout=3600,
    )
    if p.returncode != 0:
        sys.exit(f"exporting {design}: {p.stderr[-2000:]}")


def module_spans(text):
    """The line ranges of each module, so a construct's line can be bounded.

    What bounds `covers` below: a construct cannot reach past the `endmodule`
    of the module it is in, so a location further down the file than that is
    not the same code however few other locations lie between.
    """
    spans, start = [], None
    for n, line in enumerate(text.splitlines(), 1):
        if re.match(r"\s*(module|interface)\s", line):
            start = n
        elif re.match(r"\s*end(module|interface)\b", line) and start:
            spans.append((start, n))
            start = None
    return spans


def enclosing(spans, line):
    """The module `line` is in, as (first, last)."""
    for first, last in spans:
        if first <= line <= last:
            return (first, last)
    return (line, line)


# ---------------------------------------------------------------- projections

def oracle_tree(answer):
    """Instance paths, out of the oracle's nested hierarchy."""
    found = set()

    def walk(node):
        found.add(node["path"])
        for child in node.get("children", ()):
            walk(child)

    for root in answer["data"]["hierarchy"]:
        walk(root)
    return found


def scanner_tree(answer):
    return {row["path"] for row in answer["data"]["levels"]}


def cone_nodes(answer):
    """Node paths. The oracle lists strings, this tool lists rows."""
    return {n if isinstance(n, str) else n["path"] for n in answer["data"]["nodes"]}


def cone_edges(answer):
    return {(e["source"], e["target"]) for e in answer["data"]["edges"]}


def site(hop):
    """A location, as the two spell one in common: file basename and line."""
    if not hop.get("file") or not hop.get("line"):
        return None
    return (Path(hop["file"]).name, hop["line"])


def oracle_hops(answer):
    """What the oracle says drives and reads the signal, per direction."""
    result = answer["data"]["results"][0]
    return {
        "driver": [result["driver"]] if result.get("driver") else [],
        "load": list(result.get("loads", ())),
    }


def scanner_sites(answer):
    return {s for s in map(site, answer["data"]["hops"]) if s}


# What can stand at an instantiation as an input port's drive. The parent's
# net reached across the boundary, a constant tied to it, a reference the
# export could not resolve, or the edge of the design.
BOUND_AT_A_PORT = {"port", "constant", "external", "terminal"}


def port_crossing(old, new):
    """Whether the two are naming one port's drive from opposite ends.

    The oracle attributes an input port to the formal's declaration inside the
    module and says only that something outside drives it. This tool names what
    is actually bound there — the parent's net across a `port` hop, or the
    constant tied to it — at the instantiation. Both answer the same question
    and this tool answers it more exactly, so what is waived is *which end to
    point at*, and what is required is a driver of a kind that can be at one.

    WHICH net is on the other side is not waived and is not checked here: the
    oracle's driver object does not name it, so this comparison has nothing to
    check it against. It is checked, without exemption, by the cone comparison
    — the oracle's `fanin` of that same port net carries the edge from the
    parent's net, and a walk that named the wrong one loses it there.
    """
    return old.get("source") == "input_port" and any(
        h.get("kind") in BOUND_AT_A_PORT for h in new["data"]["hops"]
    )


def load_target(hop, scope):
    """What a load writes, as the oracle spells it: `assign \u2192 trunc8`.

    Read out of the description because that is where the oracle puts it. The
    format is not a contract, but the oracle is frozen — this gate runs against
    one commit of it and retires with it.
    """
    _, arrow, name = hop.get("description", "").partition("\u2192")
    return f"{scope}.{name.strip()}" if arrow and name.strip() else None


def concatenated(edge, texts):
    """Whether an edge the oracle has and this tool does not is one the tool
    ruled out on bits rather than failed to find.

    The oracle joins every operand of a concatenation to every target of one:
    `{r1, r2} <= {d, r1}` is two assignments and it reports four edges, and
    `trunc8 = {a, b}` keeps the low bits while it reports the high operand too.
    This tool follows the correspondence the database records. So the edge is
    exempt when the statement this tool says writes that target is a
    concatenation naming that source — which leaves an ordinary `y = a + b`
    losing an operand a miss, as it should be.
    """
    source, target = edge
    scope, name = source.rsplit(".", 1)[0], source.rsplit(".", 1)[-1]
    # The two ends of a concatenation are named in one statement, so they are
    # in one instance. Statement text carries local names, and a leaf alone
    # would let `u1.a` answer for `u2.a`.
    if scope != target.rsplit(".", 1)[0]:
        return False
    return any(
        any(re.search(rf"\b{re.escape(name)}\b", g) for g in braced(text))
        for text in texts(target)
    )


def braced(text):
    """The contents of each top-level `{…}` in a statement, nesting flattened.

    Only what is inside the braces: `assign y = {a, b} | c;` loses `c` to an
    ordinary dropped operand, not to a correspondence this tool resolved, and
    a rule keyed on the statement merely containing a brace would forgive it.
    """
    groups, depth, buf = [], 0, ""
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                groups.append(buf)
                buf = ""
        elif depth:
            buf += ch
    return groups


def covers(wanted, have, spans, others=()):
    """Whether some location in `have` is the oracle's location `wanted`.

    The oracle names the construct that drives a signal — an `always_comb`
    header, an `assign` — and this tool names the statement inside it, which is
    the same code one level down and therefore at or below that line. So a line
    covers a line above it.

    Twice bounded, because "at or below" alone would let any later statement in
    the module stand in for the right one. A construct ends where the next one
    the oracle named for this signal begins, and in any case at `endmodule`.
    `others` is the oracle's own locations for this signal and direction.
    """
    path, line = wanted
    _, last = enclosing(spans.get(path, []), line)
    below = [l for f, l in others if f == path and l > line]
    ceiling = min(below) - 1 if below else last
    return any(f == path and line <= l <= ceiling for f, l in have)


# ------------------------------------------------------------------ comparing

class Report:
    def __init__(self):
        self.checked = 0
        # Of those, the ones with an oracle fact to look for. A comparison
        # against an empty answer is a question asked, not evidence gathered,
        # and counting only the total would overstate what the run establishes.
        self.substantive = 0
        self.misses = []
        self.exempt = Counter()
        # Kept, not just counted: a pinned count that moved is only actionable
        # if the thing that moved can be looked at.
        self.extras = []

    def compare(self, what, wanted, have):
        """`have` must hold everything in `wanted`; extras are recorded."""
        self.checked += 1
        self.substantive += bool(wanted)
        self.misses.extend((what, fact) for fact in sorted(wanted - have))
        self.extras.extend((what, fact) for fact in sorted(have - wanted))

    def refused(self, error):
        """The oracle did not answer. Only the failure it is documented to have
        is a difference; anything else is the comparison not happening."""
        if ORACLE_NOT_FOUND.match(error):
            self.exempt[EXEMPT_UNASKABLE] += 1
            return True
        return False


def signals_of(answer):
    """The oracle's own view of what a design contains: (scope, name, path).

    Taken from the oracle rather than from this tool so every question is one
    the oracle can be asked at all — what is under comparison is the answers,
    not which names each side knows.
    """
    found = []
    for m in answer["data"]["matches"]:
        if m["category"] == "signal":
            path = m["hierarchical_path"]
            found.append((path[: -len(m["name"]) - 1], m["name"], path))
    return found


def root_of(path):
    """Which top a path is under. Several of these designs have more than one,
    and this tool asks which before it will resolve a name."""
    return path.split(".")[0]


def questions(signals):
    """A design's questions, as (oracle query, scanner argv, label)."""
    for scope, name, path in signals:
        where = f"-s {name} --scope {scope}"
        top = ["--top", root_of(path)]
        yield f"trace {where}", ["trace", path, *top], f"trace {path}"
        for cmd in ("fanin", "fanout"):
            yield (
                f"{cmd} {where} --depth {ORACLE_UNBOUNDED}",
                [cmd, path, *top, "--depth", "0", "--limit", "0"],
                f"{cmd} {path}",
            )

    # Routes are asked within one top: a pair from two of them is not a route
    # either side would look for.
    by_top = {}
    for _, _, path in signals:
        by_top.setdefault(root_of(path), []).append(path)
    for top, paths in by_top.items():
        for a, b in permutations(paths[:PATH_SAMPLE], 2):
            yield (
                f"path --from {a} --to {b} --scope {top}",
                ["path", a, b, "--top", top, "--depth", "0"],
                f"path {a} -> {b}",
            )


def check_design(args, design, report):
    db = Path(args.work) / f"{design.stem}.db"
    export(args.exporter, design, db)
    spans = {design.name: module_spans(design.read_text())}

    catalogue = run_oracle(args.oracle_cli, [str(design)], ["find -p '**'"])[0]
    if catalogue.failed:
        sys.exit(f"{design.name}: the oracle cannot read it: {catalogue.get('error')}")

    signals = signals_of(catalogue)

    # The statements this tool says drive a net, asked once per net: the cone
    # comparison consults them for every edge it cannot match, and a design's
    # nets are asked about many times over.
    known = {}

    def statements(path):
        if path not in known:
            answer = run_scanner(args.scanner, db, ["trace", path, "--top", root_of(path)])
            known[path] = (
                [] if answer.failed
                else [h["statement"] for h in answer["data"]["hops"] if h.get("statement")]
            )
        return known[path]

    # The tree is one answer on the oracle's side and one per top on this one,
    # so it is compared whole rather than as one of the questions below.
    tops = sorted({root_of(p) for _, _, p in signals})
    shown = set()
    for top in tops:
        answer = run_scanner(args.scanner, db, ["tree", "--top", top, "--depth", "0", "--limit", "0"])
        if answer.failed:
            report.misses.append((f"tree --top {top}", answer["errors"][0]["code"]))
        else:
            shown |= scanner_tree(answer)
    old_tree = run_oracle(args.oracle_cli, [str(design)], ["tree"])[0]
    report.compare("tree", oracle_tree(old_tree), shown)

    asked = list(questions(signals))
    oracle = run_oracle(args.oracle_cli, [str(design)], [q for q, _, _ in asked])

    for i, (_, argv, label) in enumerate(asked):
        old = oracle[i]
        if old.failed:
            if not report.refused(old.get("error", "")):
                report.misses.append((label, f"the oracle failed: {old.get('error')}"))
            continue
        # `--control` on a trace because the oracle counts a condition read as
        # a load and this tool files it under the statement's gates unless
        # asked; the flag is what makes the two the same question.
        if argv[0] == "trace":
            argv = argv + ["--control"]
        new = run_scanner(args.scanner, db, argv)
        if new.failed:
            report.misses.append((label, new["errors"][0]["code"]))
            continue

        if argv[0] == "path":
            report.checked += 1
            report.substantive += bool(old["data"]["found"])
            if old["data"]["found"] and not new["data"]["found"]:
                report.misses.append((label, "a route the oracle found"))
        elif argv[0] == "trace":
            loads = run_scanner(args.scanner, db, argv + ["--load"])
            for direction, hops in oracle_hops(old).items():
                answer = new if direction == "driver" else loads
                have = scanner_sites(answer)
                report.checked += 1
                wanted = {s for s in map(site, hops) if s}
                report.substantive += bool(wanted)
                for hop in hops:
                    where = site(hop)
                    if where and covers(where, have, spans, wanted):
                        continue
                    written = load_target(hop, hop.get("scope_path", ""))
                    if port_crossing(hop, answer):
                        report.exempt[EXEMPT_PORT_END] += 1
                    elif written and concatenated((argv[1], written), statements):
                        # The same difference the cone comparison exempts, read
                        # from the other end: a read whose bits the assignment
                        # truncates away is not a read of this signal.
                        report.exempt[EXEMPT_CONCAT] += 1
                    elif where:
                        report.misses.append((f"{label} [{direction}]", where))
                report.extras.extend(
                    (f"{label} [{direction}]", h)
                    for h in sorted(have)
                    if not any(covers(w, {h}, spans, wanted) for w in wanted)
                )
        else:
            old_edges = cone_edges(old)
            ruled_out = {
                e for e in old_edges - cone_edges(new) if concatenated(e, statements)
            }
            report.exempt[EXEMPT_CONCAT] += len(ruled_out)
            kept = old_edges - ruled_out
            report.compare(f"{label} edges", kept, cone_edges(new))
            # A node the oracle reached ONLY through a ruled-out edge goes with
            # it; one an edge that survives still reaches is still expected.
            justified = {n for e in kept for n in e} | {old["data"]["start"]}
            report.compare(label, cone_nodes(old) & justified, cone_nodes(new))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--oracle-cli", required=True, help="the Python engine's entry point")
    p.add_argument("--designs", required=True, help="a directory of its example RTL")
    p.add_argument("--scanner", required=True)
    p.add_argument("--exporter", required=True)
    p.add_argument("--work", required=True, help="where the exported databases go")
    p.add_argument("--show-extras", action="store_true",
                   help="list the facts only this tool has, so a moved count can be read")
    p.add_argument("--max-extra", type=int, default=None,
                   help="fail if this tool answers with more than this many facts the "
                        "oracle did not have, so a change that manufactures endpoints "
                        "is a number that moved rather than silence")
    args = p.parse_args()

    Path(args.work).mkdir(parents=True, exist_ok=True)
    designs = sorted(Path(args.designs).glob("*.sv"))
    if not designs:
        sys.exit(f"no designs under {args.designs}")

    report = Report()
    for design in designs:
        before = len(report.misses)
        check_design(args, design, report)
        print(f"{design.name:22} {len(report.misses) - before:3} miss(es)", flush=True)

    print(
        f"\n{report.checked} comparison(s), {report.substantive} of them with an "
        f"oracle fact to find, {len(report.extras)} extra fact(s)"
    )
    for reason, n in sorted(report.exempt.items()):
        print(f"  {n:5} exempt: {reason}")
    if args.show_extras:
        for what, fact in report.extras:
            print(f"  EXTRA {what}: {fact}")
    for what, fact in report.misses:
        print(f"  MISS {what}: {fact}")

    if report.misses:
        print(f"\n{len(report.misses)} unexplained miss(es)")
        return 1
    if args.max_extra is not None and len(report.extras) > args.max_extra:
        print(f"\n{len(report.extras)} extras, more than the pinned {args.max_extra}")
        return 1
    print("\nno unexplained miss")
    return 0


if __name__ == "__main__":
    sys.exit(main())

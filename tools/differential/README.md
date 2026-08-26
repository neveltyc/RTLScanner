# The migration gate

One run, once, against the engine this one replaced: **every answer the Python
engine gave, does this one still give?**

The tests in `crates/` cannot answer that. They were written alongside the
rewrite and share its idea of what the answers are. The predecessor does not —
it read RTL through pyslang rather than a database, and it was written against
these same example designs — so comparing the two is the one check available
that this tool's authorship did not shape.

**This is temporary.** It retires when 2.0 ships. Nothing in the build, the test
suite or CI depends on it; deleting this directory is the whole retirement. The
regression line afterwards is the golden tests plus the invariants in
`crates/rtlscanner/tests/`, which need no oracle.

## The recorded run

Against `dev-python-bk` at `fa327df` (0.5.0), over that branch's own eleven
example designs plus `examples/design.sv`, with `rtl-designdb` at the pinned
submodule commit:

```
1696 comparison(s), 146 extra fact(s)
    12 exempt: a concatenation's bit correspondence, which the oracle joins end to end
    62 exempt: a port's drive, named at what is connected rather than at the declaration
    51 exempt: the oracle refuses the question

no unexplained miss
```

The 146 extras are answers this engine has and the oracle does not, which is
what the rewrite was for: cross-hierarchy loads the oracle misses, one driver
per statement where it reports one per procedure, terminal loads at the design
boundary, dead branches published rather than pruned, and generate levels in
the tree.

## Running it

The oracle needs pyslang, which is not in any system Python, and its source is
on a branch rather than in this tree.

```bash
python3 -m venv /tmp/oracle && /tmp/oracle/bin/pip install -q pyslang
mkdir -p /tmp/oracle-src && git archive dev-python-bk | tar -x -C /tmp/oracle-src
/tmp/oracle/bin/pip install -q -e /tmp/oracle-src
mkdir -p /tmp/corpus && cp /tmp/oracle-src/examples/*/*.sv examples/design.sv /tmp/corpus/
```

Then, with a release build (`cargo build --release`) and an exporter:

```bash
python3 tools/differential/differ.py \
  --oracle-cli /tmp/oracle/bin/rtlscanner --designs /tmp/corpus \
  --scanner target/release/rtlscanner \
  --exporter extern/RTLDebugDBKit/build/rtl-designdb \
  --work /tmp/differential --max-extra 146
```

It exits non-zero on an unexplained miss, or when the extras outgrow the pin.
`--show-extras` lists them, so a count that moved can be read rather than
guessed at.

## The judgement, and its own tests

Every rule in `differ.py` waives a difference, and a rule that waives too much
turns a gate that proves something into one that proves nothing. So each is
pinned from both sides — the shape it must classify, and the shape it must
leave alone:

```bash
python3 tools/differential/test_differ.py
```

Those run anywhere; they need neither the oracle nor an exporter. The
end-to-end direction was checked the other way too, with a wrapper that drops
one edge and one hop from every answer: 567 unexplained misses, which is what
says the green run above is a statement about this tool rather than about the
harness.

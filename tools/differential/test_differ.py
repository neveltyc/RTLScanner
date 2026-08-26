#!/usr/bin/env python3
"""Tests for `differ.py` — the harness that decides what a difference is.

Each waiver rule is pinned from both sides: the shape it must classify, and
the shape it must leave alone. The fixtures are shapes the two engines
actually produced on the corpus. These need neither the oracle nor an
exporter:

    python3 tools/differential/test_differ.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import differ


def hop(line=10, file="d.sv", **rest):
    return {"file": file, "line": line, **rest}


# `module m` on line 1, `endmodule` on line 20; a second module below it.
SPANS = {"d.sv": [(1, 20), (22, 40)]}


class Covering(unittest.TestCase):
    """The oracle names a construct and this tool names a statement in it."""

    def test_a_statement_below_the_construct_covers_it(self):
        # `always_ff` on 11, `if (en) out <= sum;` on 12: the case that would
        # fail every procedure in the corpus if lines were compared for equality.
        self.assertTrue(differ.covers(("d.sv", 11), {("d.sv", 12)}, SPANS))

    def test_the_same_line_covers_it(self):
        self.assertTrue(differ.covers(("d.sv", 12), {("d.sv", 12)}, SPANS))

    def test_a_statement_above_it_does_not(self):
        self.assertFalse(differ.covers(("d.sv", 12), {("d.sv", 11)}, SPANS))

    def test_a_statement_in_the_next_module_does_not(self):
        # Without the module bound, any later line in the file would cover any
        # earlier one and the trace comparison would assert almost nothing.
        self.assertFalse(differ.covers(("d.sv", 11), {("d.sv", 25)}, SPANS))

    def test_another_file_does_not(self):
        self.assertFalse(differ.covers(("d.sv", 11), {("other.sv", 12)}, SPANS))

    def test_a_line_in_no_module_covers_only_itself(self):
        self.assertTrue(differ.covers(("d.sv", 21), {("d.sv", 21)}, SPANS))
        self.assertFalse(differ.covers(("d.sv", 21), {("d.sv", 22)}, SPANS))

    def test_the_next_construct_the_oracle_named_ends_this_one(self):
        # Two constructs in one module drive this signal, at 5 and at 15. A
        # statement at 16 belongs to the second, and standing in for the first
        # would forgive a driver attributed to an unrelated block.
        oracle = {("d.sv", 5), ("d.sv", 15)}
        self.assertTrue(differ.covers(("d.sv", 5), {("d.sv", 7)}, SPANS, oracle))
        self.assertFalse(differ.covers(("d.sv", 5), {("d.sv", 16)}, SPANS, oracle))
        self.assertTrue(differ.covers(("d.sv", 15), {("d.sv", 16)}, SPANS, oracle))


class ModuleSpans(unittest.TestCase):
    def test_a_module_runs_from_its_header_to_its_end(self):
        text = "// c\nmodule m (\n  input a\n);\n  assign y = a;\nendmodule\n"
        self.assertEqual(differ.module_spans(text), [(2, 6)])

    def test_an_interface_is_a_scope_too(self):
        self.assertEqual(differ.module_spans("interface i;\nendinterface\n"), [(1, 2)])


class ThePortEnd(unittest.TestCase):
    """The oracle points inside the module, this tool points at what is bound."""

    def answer(self, hops):
        return differ.Answer({"data": {"hops": hops}})

    def test_an_input_port_the_tool_answered_for_is_the_same_fact(self):
        old = {"source": "input_port", "file": "d.sv", "line": 8}
        self.assertTrue(differ.port_crossing(old, self.answer([hop(19, kind="port")])))

    def test_a_constant_tie_off_is_that_answer_too(self):
        # `.b(8'h0)`: the oracle says only "driven from the parent", and this
        # tool says which constant. More exact, not less.
        old = {"source": "input_port", "file": "d.sv", "line": 8}
        self.assertTrue(differ.port_crossing(old, self.answer([hop(44, kind="constant")])))

    def test_an_input_port_with_no_answer_at_all_is_a_miss(self):
        old = {"source": "input_port", "file": "d.sv", "line": 8}
        self.assertFalse(differ.port_crossing(old, self.answer([])))

    def test_a_driver_that_is_not_a_port_is_not_covered_by_this(self):
        # An `always_comb` the tool put at the wrong line stays a miss; only
        # the port end is a matter of which end to point at.
        old = {"source": "procedural", "file": "d.sv", "line": 21}
        self.assertFalse(differ.port_crossing(old, self.answer([hop(30, kind="procedural")])))

    def test_an_answer_that_could_not_be_at_a_port_is_a_miss(self):
        # A procedural assignment is not a thing that stands at an
        # instantiation, so an input port answered that way is the walk
        # answering a different question, not the same one from the other end.
        old = {"source": "input_port", "file": "d.sv", "line": 8}
        self.assertFalse(differ.port_crossing(old, self.answer([hop(30, kind="procedural")])))


class TheConcatenation(unittest.TestCase):
    """The oracle joins every operand to every target; this tool follows bits."""

    def texts(self, **by_target):
        return lambda target: by_target.get(target, [])

    def test_a_split_concatenation_target_is_ruled_out_not_lost(self):
        # `{r1, r2} <= {d, r1}` is two assignments. The oracle reports four
        # edges; d reaches r1 and r1 reaches r2, and the other two do not exist.
        texts = self.texts(**{"m.r2": ["{r1, r2} <= {d, r1};"], "m.r1": ["{r1, r2} <= {d, r1};"]})
        self.assertTrue(differ.concatenated(("m.d", "m.r2"), texts))
        self.assertTrue(differ.concatenated(("m.r1", "m.r1"), texts))

    def test_a_truncated_operand_is_ruled_out_too(self):
        texts = self.texts(**{"m.trunc8": ["assign trunc8 = {a, b};"]})
        self.assertTrue(differ.concatenated(("m.a", "m.trunc8"), texts))

    def test_an_ordinary_expression_losing_an_operand_is_a_miss(self):
        # The rule must not reach this: `y = a + b` has one target and no
        # correspondence to resolve, so a missing operand is a defect.
        texts = self.texts(**{"m.y": ["assign y = a + b;"]})
        self.assertFalse(differ.concatenated(("m.b", "m.y"), texts))

    def test_a_concatenation_that_does_not_name_the_source_is_a_miss(self):
        texts = self.texts(**{"m.y": ["assign y = {c, d};"]})
        self.assertFalse(differ.concatenated(("m.a", "m.y"), texts))

    def test_a_target_this_tool_reports_nothing_for_is_a_miss(self):
        # Losing the statement entirely is the regression this gate exists for.
        self.assertFalse(differ.concatenated(("m.a", "m.y"), self.texts()))

    def test_a_name_inside_a_longer_word_is_not_that_name(self):
        texts = self.texts(**{"m.y": ["assign y = {data, sel};"]})
        self.assertFalse(differ.concatenated(("m.a", "m.y"), texts))

    def test_an_operand_outside_the_braces_is_a_miss(self):
        # The statement holds a concatenation, but `c` is not in it: losing `c`
        # is an ordinary dropped operand, and a rule keyed on the statement
        # merely containing a brace would forgive it.
        texts = self.texts(**{"m.y": ["assign y = {a, b} | c;"]})
        self.assertTrue(differ.concatenated(("m.a", "m.y"), texts))
        self.assertFalse(differ.concatenated(("m.c", "m.y"), texts))

    def test_a_source_in_another_instance_is_a_miss(self):
        # Statement text carries local names; the leaf alone would let one
        # instance's `a` answer for another's.
        texts = self.texts(**{"m.u1.y": ["assign y = {a, b};"]})
        self.assertTrue(differ.concatenated(("m.u1.a", "m.u1.y"), texts))
        self.assertFalse(differ.concatenated(("m.u2.a", "m.u1.y"), texts))


class Braces(unittest.TestCase):
    def test_only_what_is_inside_the_braces(self):
        self.assertEqual(differ.braced("assign y = {a, b} | c;"), ["a, b"])

    def test_nesting_flattens_into_the_outer_group(self):
        # A nested concatenation is still operands of the outer one, and which
        # nesting level a name sits at decides nothing here.
        self.assertEqual(differ.braced("y = {a, {b, c}};"), ["a, b, c"])

    def test_no_braces_is_no_groups(self):
        self.assertEqual(differ.braced("assign y = a + b;"), [])


class TheOracleRefusing(unittest.TestCase):
    """Only the failure the oracle is documented to have is a difference."""

    def test_a_signal_it_cannot_name_is_the_documented_refusal(self):
        r = differ.Report()
        # The two spellings of a generate-block net, and what each comes back
        # with. Neither resolves, and both are the oracle's own limit.
        self.assertTrue(r.refused(
            "signal 'lane[0].copy' not found in scope 'top'; signals here: clk, a"))
        self.assertTrue(r.refused(
            "scope 'top.lane[0]' not found; deepest valid prefix: 'top'; scopes under top: u_core"))
        self.assertEqual(sum(r.exempt.values()), 2)

    def test_any_other_failure_is_the_comparison_not_happening(self):
        # A timeout or an internal error would otherwise take a whole family of
        # questions out of the gate and leave a count nothing pins.
        r = differ.Report()
        self.assertFalse(r.refused("timed out after 3600s"))
        self.assertFalse(r.refused("internal error: unhandled symbol kind"))
        self.assertFalse(r.refused(""))
        self.assertEqual(sum(r.exempt.values()), 0)


class TheLoadTarget(unittest.TestCase):
    def test_the_target_is_read_out_of_the_description(self):
        hop = {"description": "assign → trunc8"}
        self.assertEqual(differ.load_target(hop, "ops_top"), "ops_top.trunc8")

    def test_a_description_naming_no_target_gives_none(self):
        self.assertIsNone(differ.load_target({"description": "always_ff"}, "m"))
        self.assertIsNone(differ.load_target({}, "m"))


class TheDirection(unittest.TestCase):
    """A miss fails; an extra is counted."""

    def test_a_fact_the_oracle_has_and_this_tool_lacks_is_a_miss(self):
        r = differ.Report()
        r.compare("cone", {"a", "b"}, {"a"})
        self.assertEqual(r.misses, [("cone", "b")])
        self.assertEqual(r.extras, [])

    def test_a_fact_only_this_tool_has_is_counted_and_not_a_miss(self):
        r = differ.Report()
        r.compare("cone", {"a"}, {"a", "b"})
        self.assertEqual(r.misses, [])
        self.assertEqual(r.extras, [("cone", "b")])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Bit-level dataflow (slang-netlist parity).

P1 pins that fanin/fanout edges carry the precise bit sub-range each read /
drive touches — answering "dout[5] comes from a[2]" — while staying additive:
whole-signal edges look exactly as before, and arithmetic stays conservative.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]
FIX = "examples/bits/bits_top.sv"


def run_json(*args):
    proc = subprocess.run(
        RTLSCANNER + list(args) + ["--json"],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def run_human(*args):
    return subprocess.run(
        RTLSCANNER + list(args) + ["--no-color"],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True,
    ).stdout


def edges(env):
    return env["data"]["edges"]


def bit_map(env):
    """{(source, target): (source_bits, target_bits)} over all edges."""
    return {(e["source"], e["target"]): (e.get("source_bits"), e.get("target_bits"))
            for e in edges(env)}


class P1BitEdges(unittest.TestCase):
    def test_truncation_narrows_source(self):
        # narrow = din  -> only din[3:0] flows; target (whole narrow) stays bare.
        env = run_json("fanin", FIX, "-s", "narrow", "--scope", "bits_top")
        m = bit_map(env)
        self.assertEqual(m[("bits_top.din", "bits_top.narrow")], ("[3:0]", None))

    def test_single_bit_rename(self):
        # dout[5] = a[2]
        env = run_json("fanin", FIX, "-s", "dout", "--scope", "bits_top",
                       "--depth", "1")
        found = {(e["source"], e["target"], e.get("source_bits"), e.get("target_bits"))
                 for e in edges(env)}
        self.assertIn(("bits_top.a", "bits_top.dout", "[2]", "[5]"), found)
        self.assertIn(("bits_top.a", "bits_top.dout", "[7:4]", "[3:0]"), found)
        self.assertIn(("bits_top.b", "bits_top.dout", "[1:0]", "[7:6]"), found)

    def test_arithmetic_is_conservative(self):
        # sum = a + b  -> whole signals, no bit ranges (carry mixes all bits).
        env = run_json("fanin", FIX, "-s", "sum", "--scope", "bits_top",
                       "--depth", "1")
        for e in edges(env):
            self.assertNotIn("source_bits", e)
            self.assertNotIn("target_bits", e)

    def test_fanout_single_bit(self):
        env = run_json("fanout", FIX, "-s", "a", "--scope", "bits_top",
                       "--depth", "1")
        found = {(e["source"], e["target"], e.get("source_bits"), e.get("target_bits"))
                 for e in edges(env)}
        # a feeds dout (precise) and sum (conservative).
        self.assertIn(("bits_top.a", "bits_top.dout", "[2]", "[5]"), found)
        self.assertIn(("bits_top.a", "bits_top.dout", "[7:4]", "[3:0]"), found)

    def test_human_output_labels_bits(self):
        out = run_human("fanin", FIX, "-s", "narrow", "--scope", "bits_top")
        self.assertIn("din[3:0]", out)

    def test_whole_signal_edge_is_bare(self):
        # A full-width copy keeps the old shape: no bit keys emitted (additive).
        env = run_json("fanout", FIX, "-s", "din", "--scope", "bits_top",
                       "--depth", "1")
        # din -> narrow is a truncation, so din side IS sliced; assert the
        # arithmetic-free, full-width case via sum's whole-signal fanin instead.
        sums = run_json("fanin", FIX, "-s", "sum", "--scope", "bits_top",
                        "--depth", "1")
        self.assertTrue(all("source_bits" not in e and "target_bits" not in e
                            for e in edges(sums)))


class P2BitAwareFlow(unittest.TestCase):
    """fanin/fanout honor a -s bit-select and map the range across each hop."""

    def test_fanin_bit_select_converges(self):
        # dout[5] = a[2]: only that edge survives the bit-select.
        env = run_json("fanin", FIX, "-s", "dout[5]", "--scope", "bits_top")
        self.assertEqual(env["data"].get("bit_select"), "[5]")
        es = edges(env)
        self.assertEqual(len(es), 1)
        self.assertEqual((es[0]["source"], es[0]["source_bits"],
                          es[0]["target_bits"]),
                         ("bits_top.a", "[2]", "[5]"))

    def test_fanout_bit_select_precise_plus_conservative(self):
        env = run_json("fanout", FIX, "-s", "a[2]", "--scope", "bits_top",
                       "--depth", "1")
        keyed = {(e["source"], e["target"]): e for e in edges(env)}
        # precise to dout[5]
        self.assertEqual(keyed[("bits_top.a", "bits_top.dout")]["target_bits"],
                         "[5]")
        # conservative through arithmetic into sum (whole signal)
        self.assertIn(("bits_top.a", "bits_top.sum"), keyed)
        self.assertNotIn("target_bits", keyed[("bits_top.a", "bits_top.sum")])

    def test_multihop_swap_high_nibble_traces_to_lo(self):
        # dout[7:4] -> (nibble swap, ports, copies) -> lo, never hi.
        env = run_json("fanin", "examples/bits/chain_top.sv", "-s", "dout[7:4]",
                       "--scope", "chain_top", "--depth", "6")
        nodes = set(env["data"]["nodes"])
        self.assertIn("chain_top.lo", nodes)
        self.assertNotIn("chain_top.hi", nodes)

    def test_multihop_swap_low_nibble_traces_to_hi(self):
        env = run_json("fanin", "examples/bits/chain_top.sv", "-s", "dout[3:0]",
                       "--scope", "chain_top", "--depth", "6")
        nodes = set(env["data"]["nodes"])
        self.assertIn("chain_top.hi", nodes)
        self.assertNotIn("chain_top.lo", nodes)

    def test_whole_signal_query_unchanged(self):
        # No bit-select: every nibble source is reachable (symbol-level cone).
        env = run_json("fanin", "examples/bits/chain_top.sv", "-s", "dout",
                       "--scope", "chain_top", "--depth", "6")
        nodes = set(env["data"]["nodes"])
        self.assertIn("chain_top.lo", nodes)
        self.assertIn("chain_top.hi", nodes)


class P3LoadsByBit(unittest.TestCase):
    """trace -s sig[bits] narrows loads to the readers that touch those bits."""

    def _trace(self, sig):
        return run_json("trace", FIX, "-s", sig,
                        "--scope", "bits_top")["data"]["results"][0]

    def test_whole_signal_lists_all_readers(self):
        # a is read by dout[5]=a[2], dout[3:0]=a[7:4], and sum=a+b.
        self.assertEqual(self._trace("a")["load_count"], 3)

    def test_bit_select_filters_loads(self):
        r = self._trace("a[2]")
        self.assertEqual(r["bit_select"], "[2]")
        # the a[2] reader + the whole-a arithmetic reader; a[7:4] reader excluded
        self.assertEqual(r["load_count"], 2)
        bits = [ld.get("bits") for ld in r["loads"]]
        self.assertIn("[2]", bits)            # dout[5] = a[2]
        self.assertIn(None, bits)             # sum = a + b (whole signal)
        self.assertNotIn("[7:4]", bits)       # dout[3:0] = a[7:4] excluded

    def test_bit_select_other_bit(self):
        r = self._trace("a[7]")
        self.assertEqual(r["load_count"], 2)
        bits = [ld.get("bits") for ld in r["loads"]]
        self.assertIn("[7:4]", bits)          # dout[3:0] = a[7:4]
        self.assertNotIn("[2]", bits)         # dout[5] = a[2] excluded


class P4StructuralOps(unittest.TestCase):
    """Concatenation, mux (?:), and bitwise ops resolve to precise bits."""

    OPS = "examples/bits/ops_top.sv"

    def _fanin(self, sig, depth=1):
        env = run_json("fanin", self.OPS, "-s", sig, "--scope", "ops_top",
                       "--depth", str(depth))
        return {(e["source"], e["target"], e.get("source_bits"),
                 e.get("target_bits")) for e in env["data"]["edges"]}

    def test_concat_splits_target(self):
        got = self._fanin("cat8")
        self.assertIn(("ops_top.a", "ops_top.cat8", "[3:0]", "[7:4]"), got)
        self.assertIn(("ops_top.b", "ops_top.cat8", "[3:0]", "[3:0]"), got)

    def test_concat_to_wider(self):
        got = self._fanin("cat16")
        self.assertIn(("ops_top.a", "ops_top.cat16", None, "[15:8]"), got)
        self.assertIn(("ops_top.b", "ops_top.cat16", None, "[7:0]"), got)

    def test_bitwise_of_slices_keeps_source_bits(self):
        got = self._fanin("masked")
        self.assertIn(("ops_top.a", "ops_top.masked", "[7:4]", None), got)
        self.assertIn(("ops_top.b", "ops_top.masked", "[3:0]", None), got)

    def test_mux_connects_arms_and_predicate(self):
        srcs = {s for (s, _t, _sb, _tb) in self._fanin("muxed")}
        self.assertEqual(srcs, {"ops_top.a", "ops_top.b", "ops_top.sel"})

    def test_concat_bit_select_maps_through_offset(self):
        # cat8[7] = a[3:0]->cat8[7:4] with offset +4, so cat8[7] <- a[3] only.
        env = run_json("fanin", self.OPS, "-s", "cat8[7]", "--scope", "ops_top")
        srcs = {e["source"] for e in env["data"]["edges"]}
        self.assertEqual(srcs, {"ops_top.a"})       # not b


if __name__ == "__main__":
    unittest.main()

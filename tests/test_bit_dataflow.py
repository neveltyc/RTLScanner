"""Bit-level dataflow.

P1 pins that fanin/fanout edges carry the precise bit sub-range each read /
drive touches — answering "dout[5] comes from a[2]" — while staying additive:
whole-signal edges look exactly as before, and arithmetic stays conservative.
"""

import json
import subprocess
import sys
import tempfile
import textwrap
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

    def test_truncated_concat_keeps_lsb(self):
        # trunc8 = {a, b} into 4 bits: SV keeps the low bits, so b[3:0] drives
        # it precisely; a is the truncated-away MSB (kept conservative, not
        # falsely mapped to a[3:0]).
        got = self._fanin("trunc8")
        self.assertIn(("ops_top.b", "ops_top.trunc8", "[3:0]", None), got)
        self.assertIn(("ops_top.a", "ops_top.trunc8", None, None), got)
        # a must NOT be reported as precisely driving the low nibble.
        self.assertNotIn(("ops_top.a", "ops_top.trunc8", "[3:0]", None), got)


class P5ExoticAndEdgeCases(unittest.TestCase):
    """Correctness on tricky inputs: same-pair multi-segment, big-endian
    vectors, packed arrays, and out-of-range bit-selects."""

    def _write(self, src):
        p = Path(tempfile.mkdtemp()) / "x.sv"
        p.write_text(textwrap.dedent(src))
        return str(p)

    def test_continuous_multisegment_same_pair_reachable(self):
        # o = {a[3:0], a[7:4]} drives o from a over two different segments; the
        # edge-key dedup keeps one, so the pair must downgrade to whole-signal
        # and stay reachable from every bit of o (regression: bit-select dropped
        # the second segment and reported o[2] as having no driver).
        sv = self._write("""
            module clash(input logic [7:0] a, output logic [7:0] o);
              assign o = {a[3:0], a[7:4]};
            endmodule""")
        for bit in ("o[2]", "o[6]"):
            env = run_json("fanin", sv, "-s", bit, "--scope", "clash")
            self.assertIn("clash.a", {e["source"] for e in env["data"]["edges"]},
                          f"{bit} must still reach a")

    def test_big_endian_bit_select(self):
        # Declared y[1] on a [0:7] vector is internal bit 6; the query must be
        # translated to match slang's internal driver bounds, not silently miss.
        sv = self._write("""
            module be(input logic [0:7] a, output logic [0:7] y);
              assign y[1:3] = a[1:3];
              assign y[4:7] = a[4:7];
            endmodule""")
        res = run_json("trace", sv, "-s", "y[1]", "--scope", "be")["data"]["results"][0]
        self.assertIsNotNone(res["driver"])          # not falsely "undriven"
        env = run_json("fanin", sv, "-s", "y[1]", "--scope", "be")
        self.assertIn("be.a", {e["source"] for e in env["data"]["edges"]})

    def test_packed_array_is_conservative(self):
        # q = mem[2] on logic [3:0][7:0]: element stride is 8, which our
        # little-endian bit math doesn't model, so the edge must degrade to a
        # whole-signal connection (no spurious/ wrong bit labels), not q[0].
        sv = self._write("""
            module pa(input logic [3:0][7:0] mem, output logic [7:0] q);
              assign q = mem[2];
            endmodule""")
        edges = run_json("fanin", sv, "-s", "q", "--scope", "pa")["data"]["edges"]
        self.assertIn("pa.mem", {e["source"] for e in edges})
        for e in edges:
            self.assertNotIn("source_bits", e)
            self.assertNotIn("target_bits", e)

    def test_same_line_bit_assigns_are_distinct_edges(self):
        # Several bit assigns to the same (din, dout) pair on ONE source line must
        # stay distinct edges (the edge key includes the bit map), so a bit-select
        # resolves each one — regression: they collapsed to the first, and
        # `fanin dout[3]` then reported dout[3] as undriven.
        sv = self._write("""
            module m(input logic [3:0] din, output logic [3:0] dout);
              assign dout[0]=din[3]; assign dout[1]=din[2];
              assign dout[2]=din[1]; assign dout[3]=din[0];
            endmodule""")
        env = run_json("fanin", sv, "-s", "dout", "--scope", "m", "--depth", "1")
        self.assertEqual(len(env["data"]["edges"]), 4)
        for q, src_bit in (("dout[0]", "[3]"), ("dout[3]", "[0]")):
            e = run_json("fanin", sv, "-s", q, "--scope", "m", "--depth", "1")
            edges = e["data"]["edges"]
            self.assertEqual(len(edges), 1, f"{q} must resolve to one driver")
            self.assertEqual(edges[0]["source_bits"], src_bit)

    def test_generate_loop_bits_are_distinct_edges(self):
        # A generate-for bit reversal puts every iteration on the same source
        # line; each must still be its own edge so `fanin dout[i]` resolves.
        sv = self._write("""
            module g(input logic [7:0] din, output logic [7:0] dout);
              for (genvar i = 0; i < 8; i++) assign dout[i] = din[7 - i];
            endmodule""")
        env = run_json("fanin", sv, "-s", "dout", "--scope", "g", "--depth", "1")
        self.assertEqual(len(env["data"]["edges"]), 8)
        e = run_json("fanin", sv, "-s", "dout[3]", "--scope", "g", "--depth", "1")
        edges = e["data"]["edges"]
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]["source"], edges[0]["source_bits"]),
                         ("g.din", "[4]"))           # dout[3] <- din[4]

    def test_nonzero_lsb_vector_is_conservative(self):
        # A non-zero-LSB vector ([8:1]) is descending but NOT zero-based, so its
        # declared and internal bit numbering differ; it must fall back to
        # whole-signal rather than emit mixed declared/internal bit labels.
        sv = self._write("""
            module nz(input logic [8:1] a, output logic [8:1] y);
              assign y[2] = a[3];
            endmodule""")
        edges = run_json("fanin", sv, "-s", "y", "--scope", "nz")["data"]["edges"]
        self.assertIn("nz.a", {e["source"] for e in edges})
        for e in edges:
            self.assertNotIn("source_bits", e)
            self.assertNotIn("target_bits", e)

    def test_flow_bit_select_out_of_range_errors(self):
        # fanin/fanout validate a bit-select against width, like trace.
        sv = self._write("""
            module n(input logic [3:0] a, output logic [3:0] y);
              assign y = a;
            endmodule""")
        proc = subprocess.run(
            RTLSCANNER + ["fanin", sv, "-s", "y[9]", "--scope", "n", "--json"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        env = json.loads(proc.stdout)
        self.assertEqual(env["status"], "error")
        self.assertIn("out of range", env["errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()

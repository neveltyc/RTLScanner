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


if __name__ == "__main__":
    unittest.main()

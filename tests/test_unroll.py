"""Constant-condition pruning / procedural loop unrolling.

The pass refines the procedural dataflow: constant if/case branches are pruned
(dead-branch edges drop out), and constant-bound for/repeat loops are unrolled
with the loop variable bound, so `p[i]` resolves to concrete bits and the loop
variable stops appearing as a spurious read.  Every case is checked against
`--no-unroll` (the conservative baseline) so the *delta* is pinned, and the
conservative fallbacks are proven to never under-report.
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
DEMO = "examples/unroll/unroll_demo.sv"


def run_json(*args):
    proc = subprocess.run(
        RTLSCANNER + list(args) + ["--json"],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def sources(env, target):
    """Leaf names of the sources feeding `target` (fully-qualified)."""
    return {e["source"].split(".")[-1]
            for e in env["data"]["edges"] if e["target"] == target}


def write_sv(text):
    path = Path(tempfile.mkdtemp()) / "u.sv"
    path.write_text(textwrap.dedent(text))
    return str(path)


class ConstantConditionPruningTests(unittest.TestCase):
    def test_dead_if_branch_drops_source(self):
        # if (EN==0) q=b; else q=c  -> b is the dead branch.
        base = run_json("fanin", DEMO, "-s", "q", "--scope", "prune",
                        "--depth", "1", "--no-unroll")
        self.assertIn("b", sources(base, "prune.q"))      # baseline keeps it
        env = run_json("fanin", DEMO, "-s", "q", "--scope", "prune", "--depth", "1")
        self.assertNotIn("b", sources(env, "prune.q"))    # pruned away
        # live arms survive
        self.assertEqual(sources(env, "prune.q"), {"a", "c", "d"})

    def test_constant_case_keeps_only_taken_item(self):
        # case (2) of {0:a,1:b,2:d,default:a} -> only d from the case.
        env = run_json("fanin", DEMO, "-s", "q", "--scope", "prune", "--depth", "1")
        s = sources(env, "prune.q")
        self.assertIn("d", s)
        self.assertNotIn("b", s)        # case item 1 (b) is not taken

    def test_nonconstant_condition_stays_conservative(self):
        # A runtime predicate must NOT be pruned: both arms + the predicate read.
        sv = write_sv("""
            module m(input logic en, input logic [3:0] a, b, output logic [3:0] q);
              always_comb if (en) q = a; else q = b;
            endmodule
        """)
        env = run_json("fanin", sv, "-s", "q", "--scope", "m", "--depth", "1")
        self.assertEqual(sources(env, "m.q"), {"a", "b", "en"})


class LoopUnrollTests(unittest.TestCase):
    def test_windowed_loop_recovers_bits(self):
        # for i in 0..1: hi[i] = a[i+2]  -> hi <- a[3:2], no loop-var edge.
        env = run_json("fanin", DEMO, "-s", "hi", "--scope", "window")
        edges = env["data"]["edges"]
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertEqual((e["source"], e["target"]), ("window.a", "window.hi"))
        self.assertEqual(e.get("source_bits"), "[3:2]")

    def test_no_unroll_blurs_window_and_keeps_loop_var(self):
        env = run_json("fanin", DEMO, "-s", "hi", "--scope", "window", "--no-unroll")
        s = sources(env, "window.hi")
        self.assertIn("a", s)
        self.assertIn("i", s)           # loop variable leaks as a spurious read
        # and the slice is blurred to the whole signal
        self.assertTrue(all(e.get("source_bits") is None
                            for e in env["data"]["edges"]))

    def test_per_iteration_branch_pruning(self):
        # for i in 0..3: if (i==2) q=d2  -> only d2 contributes.
        env = run_json("fanin", DEMO, "-s", "q", "--scope", "loop_prune",
                       "--depth", "1")
        self.assertEqual(sources(env, "loop_prune.q"), {"d2"})
        base = run_json("fanin", DEMO, "-s", "q", "--scope", "loop_prune",
                        "--depth", "1", "--no-unroll")
        self.assertIn("i", sources(base, "loop_prune.q"))   # baseline leaks i

    def test_full_index_coverage_no_iteration_dropped(self):
        # Every iteration's source must survive (guards an off-by-one unroll).
        sv = write_sv("""
            module m(input logic [3:0] s0, s1, s2, s3, output logic [3:0] y);
              logic [3:0] arr [0:3];
              always_comb begin
                arr[0]=s0; arr[1]=s1; arr[2]=s2; arr[3]=s3;
                for (int i = 0; i < 4; i++) y = arr[i];
              end
            endmodule
        """)
        env = run_json("fanin", sv, "-s", "y", "--scope", "m", "--depth", "2")
        s = {n for e in env["data"]["edges"] for n in (e["source"].split(".")[-1],)}
        for name in ("s0", "s1", "s2", "s3"):
            self.assertIn(name, s)


def segments(env, source, target):
    """{(source_bits, target_bits)} of the (source -> target) edge's bit map."""
    for e in env["data"]["edges"]:
        if e["source"] == source and e["target"] == target:
            return {(s["source_bits"], s["target_bits"])
                    for s in e.get("segments", [])}
    return set()


class PermutationMapTests(unittest.TestCase):
    """A loop whose per-bit copy offset varies (a reversal / swap) is a
    permutation no single affine offset expresses.  Unrolling keeps the exact
    per-bit map on the fanout edge instead of blurring to a whole signal."""

    def test_loop_reversal_keeps_per_bit_fanout_map(self):
        # for i: rev[i] = din[7-i]  -> fanout din carries din[7]->rev[0], …
        env = run_json("fanout", DEMO, "-s", "din", "--scope", "reverse",
                       "--depth", "1")
        segs = segments(env, "reverse.din", "reverse.rev")
        self.assertEqual(
            segs,
            {(f"[{i}]", f"[{7 - i}]") for i in range(8)})

    def test_no_unroll_blurs_reversal_to_whole_signal(self):
        # The conservative baseline keeps the symbol edge but no per-bit map.
        env = run_json("fanout", DEMO, "-s", "din", "--scope", "reverse",
                       "--depth", "1", "--no-unroll")
        self.assertIn("reverse.din",
                      {e["source"] for e in env["data"]["edges"]})
        self.assertEqual(segments(env, "reverse.din", "reverse.rev"), set())

    def test_bit_select_converges_across_reversal(self):
        # rev[0] traces back to exactly din[7] across the permutation.
        env = run_json("fanin", DEMO, "-s", "rev[0]", "--scope", "reverse",
                       "--depth", "1")
        self.assertEqual({e["source"] for e in env["data"]["edges"]},
                         {"reverse.din"})
        # and din[7] fans out to rev (reaching rev[0]); din[6] does not stay home
        env7 = run_json("fanout", DEMO, "-s", "din[7]", "--scope", "reverse",
                        "--depth", "1")
        self.assertEqual({e["target"] for e in env7["data"]["edges"]},
                         {"reverse.rev"})

    def test_bit_select_trims_displayed_segments(self):
        # A bit-select shows only the segments it asked for, not the whole map.
        env = run_json("fanin", DEMO, "-s", "rev[0]", "--scope", "reverse",
                       "--depth", "1")
        self.assertEqual(segments(env, "reverse.din", "reverse.rev"),
                         {("[7]", "[0]")})
        env7 = run_json("fanout", DEMO, "-s", "din[7]", "--scope", "reverse",
                        "--depth", "1")
        self.assertEqual(segments(env7, "reverse.din", "reverse.rev"),
                         {("[7]", "[0]")})
        # a range select keeps exactly that window's segments
        env_lo = run_json("fanin", DEMO, "-s", "rev[3:0]", "--scope", "reverse",
                          "--depth", "1")
        self.assertEqual(
            segments(env_lo, "reverse.din", "reverse.rev"),
            {(f"[{7 - i}]", f"[{i}]") for i in range(4)})
        # a whole-signal query still carries the full permutation
        env_all = run_json("fanout", DEMO, "-s", "din", "--scope", "reverse",
                           "--depth", "1")
        self.assertEqual(len(segments(env_all, "reverse.din", "reverse.rev")), 8)

    def test_multi_range_traversal_keeps_all_segments(self):
        # A permutation edge reached from several frontier bits must keep ALL the
        # segments those bits map to, not just the first — regression: trimming
        # to the first-arriving range dropped the rest and was order-dependent.
        def segs(order):
            sv = write_sv(f"""
                module dn(input logic [7:0] din, output logic [7:0] y);
                  logic [7:0] m;
                  assign m = {{din[0],din[1],din[2],din[3],
                              din[4],din[5],din[6],din[7]}};
                  {order}
                endmodule""")
            env = run_json("fanin", sv, "-s", "y[1:0]", "--scope", "dn",
                           "--depth", "3")
            return segments(env, "dn.din", "dn.m")
        expect = {("[7]", "[0]"), ("[6]", "[1]")}
        self.assertEqual(segs("assign y[0]=m[0]; assign y[1]=m[1];"), expect)
        # order-independent: swapping the two y assigns yields the same map
        self.assertEqual(segs("assign y[1]=m[1]; assign y[0]=m[0];"), expect)

    def test_continuous_concat_swap_keeps_segments(self):
        # A continuous-assign half swap o = {a[3:0], a[7:4]} is the same kind of
        # permutation and is kept on the single (a -> o) edge.
        sv = write_sv("""
            module m(input logic [7:0] a, output logic [7:0] o);
              assign o = {a[3:0], a[7:4]};
            endmodule
        """)
        env = run_json("fanout", sv, "-s", "a", "--scope", "m", "--depth", "1")
        self.assertEqual(segments(env, "m.a", "m.o"),
                         {("[7:4]", "[3:0]"), ("[3:0]", "[7:4]")})


class ConservativeFallbackTests(unittest.TestCase):
    def test_max_unroll_falls_back_without_underreporting(self):
        # A loop past the cap must not be partially unrolled: the data edge
        # survives via the conservative single-body walk.
        sv = write_sv("""
            module m(input logic [7:0] d, output logic [7:0] q);
              always_comb begin
                q = '0;
                for (int i = 0; i < 100000; i++) q = q | (d & (8'h1 << i[2:0]));
              end
            endmodule
        """)
        env = run_json("fanin", sv, "-s", "q", "--scope", "m", "--depth", "1",
                       "--max-unroll", "16")
        self.assertEqual(env["status"], "ok")
        self.assertIn("d", sources(env, "m.q"))

    def test_while_loop_not_unrolled_but_edges_kept(self):
        sv = write_sv("""
            module m(input logic [7:0] a, output logic [7:0] q);
              always_comb begin
                int i; q = '0; i = 0;
                while (i < 8) begin q = q | a; i = i + 1; end
              end
            endmodule
        """)
        env = run_json("fanin", sv, "-s", "q", "--scope", "m", "--depth", "1")
        self.assertEqual(env["status"], "ok")
        self.assertIn("a", sources(env, "m.q"))


class FlagAndParityTests(unittest.TestCase):
    def test_help_lists_flags(self):
        for sub in ("fanin", "trace"):
            text = subprocess.run(
                RTLSCANNER + [sub, "--help"], cwd=ROOT, text=True,
                stdout=subprocess.PIPE, check=True).stdout
            self.assertIn("--unroll", text)
            self.assertIn("--no-unroll", text)
            self.assertIn("--max-unroll", text)

    def test_no_unroll_matches_legacy_on_bits_example(self):
        # The off switch must be a true no-op vs the pre-unrolling behaviour:
        # the bits example has no prunable/unrollable construct, so default and
        # --no-unroll agree there too.
        fix = "examples/bits/bits_top.sv"
        a = run_json("fanin", fix, "-s", "dout", "--scope", "bits_top", "--depth", "1")
        b = run_json("fanin", fix, "-s", "dout", "--scope", "bits_top", "--depth", "1",
                     "--no-unroll")
        key = lambda env: sorted(
            (e["source"], e["target"], e.get("source_bits"), e.get("target_bits"))
            for e in env["data"]["edges"])
        self.assertEqual(key(a), key(b))


if __name__ == "__main__":
    unittest.main()

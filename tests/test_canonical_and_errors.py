"""Canonical-body analysis correctness and error-envelope recovery hints.

slang's AnalysisManager deduplicates identical instance bodies and records
analysis results only on the canonical body of each class — and dedup is
subtree-level, so children of a deduplicated instance carry no results of
their own.  These tests pin the canonical-twin resolution and path remapping
across trace / fanin / fanout / xref, plus the structured *_NOT_FOUND error
details and dotted -s normalization.
"""

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]


def run_json(*args, expect_status="ok"):
    proc = subprocess.run(
        RTLSCANNER + list(args) + ["--json"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    env = json.loads(proc.stdout)
    assert env["status"] == expect_status, (
        f"expected status={expect_status}, got {env['status']}: "
        f"{env.get('errors')}")
    return env


def edge_keys(envelope):
    return {
        (edge["source"], edge["target"], edge["kind"])
        for edge in envelope["data"].get("edges", [])
    }


class CanonicalAnalysisTests(unittest.TestCase):
    """examples/basic has two identical datapath instances (u_dp0 canonical,
    u_dp1 deduplicated); examples/generate has a generate-for of leafs."""

    def test_trace_duplicate_instance_reports_driver(self):
        env = run_json("trace", "-d", "examples/basic",
                       "-s", "q", "--scope", "top.u_dp1")
        result = env["data"]["results"][0]
        self.assertIsNotNone(result["driver"], "u_dp1.q must not be undriven")
        self.assertEqual(result["driver"]["source"], "output_port")
        self.assertEqual(result["driver"]["scope_path"], "top.u_dp1.u_reg")

    def test_trace_matches_between_canonical_twins(self):
        canon = run_json("trace", "-d", "examples/basic",
                         "-s", "q", "--scope", "top.u_dp0.u_reg")
        dedup = run_json("trace", "-d", "examples/basic",
                         "-s", "q", "--scope", "top.u_dp1.u_reg")
        d0 = canon["data"]["results"][0]["driver"]
        d1 = dedup["data"]["results"][0]["driver"]
        self.assertIsNotNone(d1, "deduplicated subtree child must see driver")
        self.assertEqual(d0["source"], d1["source"])  # always_ff
        self.assertEqual(d0["file"], d1["file"])
        self.assertEqual(d0["line"], d1["line"])

    def test_trace_generate_instance_reports_driver(self):
        env = run_json("trace", "-d", "examples/generate",
                       "-s", "out",
                       "--scope", "gen_top.u_mid.gen_arr[1].u_gen_leaf")
        driver = env["data"]["results"][0]["driver"]
        self.assertIsNotNone(driver)
        self.assertEqual(driver["source"], "assign")

    def test_trace_procedural_load_in_deduped_instance(self):
        env = run_json("trace", "-d", "examples/basic",
                       "-s", "clk", "--scope", "top.u_dp1.u_reg")
        loads = env["data"]["results"][0]["loads"]
        self.assertTrue(any(ld["kind"] == "procedural" for ld in loads),
                        f"expected procedural clk load, got {loads}")

    def test_fanout_traverses_generate_instances(self):
        env = run_json("fanout", "-d", "examples/generate",
                       "-s", "in", "--scope", "gen_top.u_mid", "--depth", "2")
        keys = edge_keys(env)
        for i in range(3):
            self.assertIn(
                (f"gen_top.u_mid.gen_arr[{i}].u_gen_leaf.in",
                 f"gen_top.u_mid.gen_arr[{i}].u_gen_leaf.out",
                 "continuous_assign"),
                keys,
                f"missing internal edge of gen_arr[{i}]")

    def test_flow_edges_exclude_genvars(self):
        env = run_json("fanout", "-d", "examples/generate",
                       "-s", "in", "--scope", "gen_top", "--depth", "6")
        for node in env["data"]["nodes"]:
            self.assertFalse(node.endswith("].i"),
                             f"genvar leaked into flow nodes: {node}")

    def test_xref_procedural_write_in_deduped_instance(self):
        env = run_json("xref", "-d", "examples/basic",
                       "-s", "q", "--scope", "top.u_dp1.u_reg")
        refs = env["data"]["matches"][0]["references"]
        self.assertTrue(
            any(r["access"] == "write" and r["kind"] == "procedural"
                for r in refs),
            f"expected procedural write reference, got {refs}")


class DriverBitsTests(unittest.TestCase):
    def test_disjoint_bit_drivers_not_flagged(self):
        env = run_json("trace", "-d", "examples/generate",
                       "-s", "out", "--scope", "gen_top.u_mid")
        result = env["data"]["results"][0]
        drivers = [result["driver"]] + result.get("extra_drivers", [])
        self.assertEqual(len(drivers), 3)
        self.assertFalse(result.get("multi_driver_warning"),
                         "disjoint per-bit drivers must not warn")
        self.assertEqual({d.get("bits") for d in drivers},
                         {"[0]", "[1]", "[2]"})

    def test_overlapping_drivers_flagged(self):
        src = Path(tempfile.mkdtemp()) / "rtlscanner_multidriver.sv"
        src.write_text(textwrap.dedent(
            """
            module mdtop(input logic a, input logic b, output wire y);
              assign y = a;
              assign y = b;
            endmodule
            """
        ))
        env = run_json("trace", str(src), "-s", "y", "--scope", "mdtop")
        result = env["data"]["results"][0]
        self.assertTrue(result.get("multi_driver_warning"),
                        "overlapping continuous assigns must warn")


class ErrorEnvelopeTests(unittest.TestCase):
    def test_bad_scope_reports_scope_not_found(self):
        # A bad scope with a valid signal name used to surface as
        # SIGNAL_NOT_FOUND, pointing agents at the wrong knob.
        env = run_json("trace", "-d", "examples/basic",
                       "-s", "q", "--scope", "top.u_dpX",
                       expect_status="error")
        err = env["errors"][0]
        self.assertEqual(err["code"], "SCOPE_NOT_FOUND")
        details = err["details"]
        self.assertEqual(details["valid_prefix"], "top")
        self.assertIn("u_dp0", details["children"])
        self.assertTrue(set(details["close_matches"]) & {"u_dp0", "u_dp1"})

    def test_bad_signal_reports_suggestions(self):
        env = run_json("trace", "-d", "examples/basic",
                       "-s", "qq", "--scope", "top.u_dp0",
                       expect_status="error")
        err = env["errors"][0]
        self.assertEqual(err["code"], "SIGNAL_NOT_FOUND")
        self.assertIn("q", err["details"]["close_matches"])
        self.assertIn("clk", err["details"]["available"])

    def test_scope_subcommand_bad_scope_details(self):
        env = run_json("scope", "-d", "examples/basic",
                       "--scope", "top.u_dpX", expect_status="error")
        err = env["errors"][0]
        self.assertEqual(err["code"], "SCOPE_NOT_FOUND")
        self.assertIn("u_extra_reg", err["details"]["children"])

    def test_xref_bad_symbol_suggestions(self):
        env = run_json("xref", "-d", "examples/basic",
                       "-s", "qq", "--scope", "top.u_dp0",
                       expect_status="error")
        err = env["errors"][0]
        self.assertEqual(err["code"], "SIGNAL_NOT_FOUND")
        self.assertIn("q", err["details"]["close_matches"])


class DottedSignalTests(unittest.TestCase):
    def test_trace_dotted_signal_relative_to_scope(self):
        env = run_json("trace", "-d", "examples/basic",
                       "-s", "u_dp1.q", "--scope", "top")
        self.assertEqual(env["data"]["scope"], "top.u_dp1")
        self.assertEqual(env["data"]["results"][0]["signal"], "q")
        self.assertTrue(any("interpreted signal" in d["message"]
                            for d in env["diagnostics"]))

    def test_fanin_dotted_signal_absolute_path(self):
        env = run_json("fanin", "-d", "examples/basic",
                       "-s", "top.u_dp0.q")
        self.assertEqual(env["data"]["scope"], "top.u_dp0")
        self.assertEqual(env["data"]["start"], "top.u_dp0.q")

    def test_xref_dotted_symbol(self):
        env = run_json("xref", "-d", "examples/basic",
                       "-s", "u_dp0.q", "--scope", "top")
        self.assertEqual(env["data"]["scope"], "top.u_dp0")
        self.assertEqual(env["data"]["name"], "q")


if __name__ == "__main__":
    unittest.main()

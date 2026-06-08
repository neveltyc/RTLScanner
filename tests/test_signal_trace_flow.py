import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIGNAL_TRACE = ROOT / "src" / "signal_trace.py"


def run_signal_trace_json(*args):
    proc = subprocess.run(
        [sys.executable, str(SIGNAL_TRACE), *args, "--json"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def run_signal_trace_help():
    return subprocess.run(
        [sys.executable, str(SIGNAL_TRACE), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def edge_keys(envelope):
    return {
        (edge["source"], edge["target"], edge["kind"])
        for edge in envelope["data"].get("edges", [])
    }


class SignalTraceFlowCliTests(unittest.TestCase):
    def test_help_mentions_flow_options(self):
        help_text = run_signal_trace_help()

        self.assertIn("--fanin", help_text)
        self.assertIn("Show upstream dataflow edges feeding --signal", help_text)
        self.assertIn("--fanout", help_text)
        self.assertIn("Show downstream dataflow edges driven by --signal", help_text)
        self.assertIn("--flow-depth", help_text)

    def test_fanin_json_reports_upstream_edges(self):
        env = run_signal_trace_json(
            "-d", "examples/trace",
            "--signal", "result",
            "--scope", "trace_top.u_dp",
            "--fanin",
            "--flow-depth", "3",
        )

        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["data"]["mode"], "fanin")
        self.assertEqual(env["data"]["nodes"][0], "trace_top.u_dp.result")
        self.assertIn(
            ("trace_top.u_dp.sum", "trace_top.u_dp.result", "continuous_assign"),
            edge_keys(env),
        )
        self.assertIn(
            ("trace_top.u_dp.mux_out", "trace_top.u_dp.sum", "continuous_assign"),
            edge_keys(env),
        )
        self.assertIn(
            ("trace_top.u_dp.u_mux.y", "trace_top.u_dp.mux_out", "port_connection"),
            edge_keys(env),
        )

    def test_fanout_json_reports_downstream_edges(self):
        env = run_signal_trace_json(
            "-d", "examples/trace",
            "--signal", "mux_out",
            "--scope", "trace_top.u_dp",
            "--fanout",
            "--flow-depth", "3",
        )

        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["data"]["mode"], "fanout")
        self.assertEqual(env["data"]["nodes"][0], "trace_top.u_dp.mux_out")
        self.assertIn(
            ("trace_top.u_dp.mux_out", "trace_top.u_dp.sum", "continuous_assign"),
            edge_keys(env),
        )
        self.assertIn(
            ("trace_top.u_dp.mux_out", "trace_top.u_dp.u_pipe.d", "port_connection"),
            edge_keys(env),
        )
        self.assertIn(
            ("trace_top.u_dp.result", "trace_top.out_result", "port_connection"),
            edge_keys(env),
        )

    def test_procedural_lhs_is_not_reported_as_load(self):
        env = run_signal_trace_json(
            "-d", "examples/basic",
            "--signal", "q",
            "--scope", "top.u_dp0.u_reg",
        )
        result = env["data"]["results"][0]

        self.assertEqual(result["signal"], "q")
        self.assertEqual(result["load_count"], 0)
        self.assertEqual(result["loads"], [])

    def test_timing_control_clock_is_reported_as_load(self):
        env = run_signal_trace_json(
            "-d", "examples/basic",
            "--signal", "clk",
            "--scope", "top.u_dp0.u_reg",
        )
        result = env["data"]["results"][0]

        self.assertEqual(result["signal"], "clk")
        self.assertEqual(result["load_count"], 1)
        self.assertEqual(result["loads"][0]["kind"], "procedural")
        self.assertEqual(result["loads"][0]["description"], "always_ff")


if __name__ == "__main__":
    unittest.main()

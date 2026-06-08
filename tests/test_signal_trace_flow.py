import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RTLSCANNER = [sys.executable, "-m", "rtlscanner"]


def run_json(*args):
    proc = subprocess.run(
        RTLSCANNER + list(args) + ["--json"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def run_help(sub):
    return subprocess.run(
        RTLSCANNER + [sub, "--help"],
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


class FlowSubcommandTests(unittest.TestCase):
    def test_fanin_help_describes_depth(self):
        text = run_help("fanin")
        self.assertIn("--signal", text)
        self.assertIn("--depth", text)
        self.assertIn("BFS traversal depth", text)

    def test_fanout_help_describes_depth(self):
        text = run_help("fanout")
        self.assertIn("--signal", text)
        self.assertIn("--depth", text)

    def test_fanin_reports_upstream_edges(self):
        env = run_json(
            "fanin",
            "-d", "examples/trace",
            "--signal", "result",
            "--scope", "trace_top.u_dp",
            "--depth", "3",
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

    def test_fanout_reports_downstream_edges(self):
        env = run_json(
            "fanout",
            "-d", "examples/trace",
            "--signal", "mux_out",
            "--scope", "trace_top.u_dp",
            "--depth", "3",
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

    def test_trace_procedural_lhs_is_not_a_load(self):
        env = run_json(
            "trace",
            "-d", "examples/basic",
            "--signal", "q",
            "--scope", "top.u_dp0.u_reg",
        )
        result = env["data"]["results"][0]

        self.assertEqual(result["signal"], "q")
        self.assertEqual(result["load_count"], 0)
        self.assertEqual(result["loads"], [])

    def test_trace_timing_control_clock_is_a_load(self):
        env = run_json(
            "trace",
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

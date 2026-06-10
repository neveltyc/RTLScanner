import json
import subprocess
import sys
import textwrap
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
    def test_shared_help_describes_config(self):
        text = run_help("tree")
        self.assertIn("--config FILE", text)
        self.assertIn("RTLSCANNER_CONFIG", text)

    def test_scope_help_describes_sections(self):
        text = run_help("scope")
        self.assertIn("--signals", text)
        self.assertIn("--typedefs", text)
        self.assertIn("--connections", text)

    def test_scope_default_reports_direct_contents(self):
        env = run_json(
            "scope",
            "-d", "examples/basic",
            "--scope", "top.u_dp0",
        )

        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["data"]["mode"], "scope")
        self.assertIn("ports", env["data"])
        self.assertIn("signals", env["data"])
        self.assertIn("instances", env["data"])
        self.assertIn("params", env["data"])
        self.assertNotIn("connections", env["data"])
        self.assertEqual({s["name"] for s in env["data"]["signals"]}, {"q"})

    def test_scope_connections_are_direct_children(self):
        env = run_json(
            "scope",
            "-d", "examples/basic",
            "--scope", "top.u_dp0",
            "--connections",
        )

        instances = {c["instance"] for c in env["data"]["connections"]}
        self.assertEqual(instances, {"top.u_dp0.u_reg", "top.u_dp0.u_alu"})

    def test_scope_typedefs_reports_local_type_defs(self):
        src = Path("/tmp/rtlscanner_scope_typedefs.sv")
        src.write_text(textwrap.dedent(
            """
            module typed(input logic [7:0] in, output logic [7:0] out);
              typedef enum logic [1:0] {IDLE, BUSY} state_t;
              typedef struct packed { logic [7:0] data; logic valid; } packet_t;
              assign out = in;
            endmodule
            module top(input logic [7:0] a, output logic [7:0] y);
              typed u(.in(a), .out(y));
            endmodule
            """
        ))

        env = run_json(
            "scope",
            str(src),
            "--scope", "top.u",
            "--typedefs",
        )
        typedefs = {item["name"]: item for item in env["data"]["typedefs"]}

        self.assertIn("state_t", typedefs)
        self.assertIn("packet_t", typedefs)
        self.assertEqual(typedefs["state_t"]["kind"], "enum")
        self.assertEqual(typedefs["packet_t"]["kind"], "struct")

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

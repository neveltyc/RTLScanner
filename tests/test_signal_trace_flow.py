import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
import tempfile


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
        src = Path(tempfile.mkdtemp()) / "rtlscanner_scope_typedefs.sv"
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


class BitSelectTraceTests(unittest.TestCase):
    """`trace -s status[3]` narrows the driver origin to a bit range."""

    @classmethod
    def setUpClass(cls):
        cls.sv = Path(tempfile.mkdtemp()) / "rtlscanner_bitsel.sv"
        cls.sv.write_text(textwrap.dedent(
            """
            module div(input logic [7:0] x, y, output logic [7:0] status);
              assign status[7:4] = x[7:4];   // upper nibble from x
              assign status[3:0] = y[3:0];   // lower nibble from y
            endmodule
            """
        ))

    def _trace(self, sig):
        return run_json("trace", str(self.sv), "-s", sig, "--scope", "div")

    def test_help_mentions_bit_select(self):
        self.assertIn("status[3]", run_help("trace"))

    def test_bit3_narrows_to_lower_driver(self):
        res = self._trace("status[3]")["data"]["results"][0]
        self.assertEqual(res["bit_select"], "[3]")
        self.assertEqual(res["driver"]["bits"], "[3:0]")
        self.assertNotIn("extra_drivers", res)        # only the covering driver
        # loads-by-bit: status is an output (no internal readers) -> empty.
        self.assertEqual(res["loads"], [])
        self.assertEqual(res["load_count"], 0)

    def test_bit7_narrows_to_upper_driver(self):
        res = self._trace("status[7]")["data"]["results"][0]
        self.assertEqual(res["bit_select"], "[7]")
        self.assertEqual(res["driver"]["bits"], "[7:4]")

    def test_range_select_narrows_to_upper(self):
        res = self._trace("status[7:4]")["data"]["results"][0]
        self.assertEqual(res["bit_select"], "[7:4]")
        self.assertEqual(res["driver"]["bits"], "[7:4]")

    def test_whole_signal_unchanged(self):
        res = self._trace("status")["data"]["results"][0]
        self.assertNotIn("bit_select", res)
        self.assertIn("load_count", res)
        self.assertIsNotNone(res["driver"])
        self.assertEqual(len(res.get("extra_drivers", [])), 1)

    def test_out_of_range_is_error(self):
        proc = subprocess.run(
            RTLSCANNER + ["trace", str(self.sv), "-s", "status[99]",
                          "--scope", "div", "--json"],
            cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        env = json.loads(proc.stdout)
        self.assertEqual(env["status"], "error")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("out of range", env["errors"][0]["message"])

    def test_fanin_honors_bit_select(self):
        # status[3] is driven by `status[3:0] = y[3:0]`, so its fanin converges
        # to y; the x-driven upper nibble must not appear.
        env = run_json("fanin", str(self.sv), "-s", "status[3]", "--scope", "div")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["data"].get("bit_select"), "[3]")
        srcs = {e["source"].split(".")[-1] for e in env["data"]["edges"]}
        self.assertIn("y", srcs)
        self.assertNotIn("x", srcs)

    def test_fanin_bit_select_other_nibble(self):
        env = run_json("fanin", str(self.sv), "-s", "status[7]", "--scope", "div")
        srcs = {e["source"].split(".")[-1] for e in env["data"]["edges"]}
        self.assertIn("x", srcs)
        self.assertNotIn("y", srcs)


class ProceduralPrecisionTests(unittest.TestCase):
    """Per-statement deps: an assignment's RHS feeds only its own LHS — no
    readSet x drivers cross-product across a procedural block."""

    @classmethod
    def setUpClass(cls):
        cls.sv = Path(tempfile.mkdtemp()) / "rtlscanner_procprec.sv"
        cls.sv.write_text(textwrap.dedent(
            """
            module m(input logic clk, en, input logic [3:0] a, b,
                     output logic [3:0] q, output logic [3:0] r);
              always_ff @(posedge clk) begin
                if (en) q <= a + b;   // q <- a,b ; gated by en
                r <= a;               // r <- a only
              end
            endmodule
            """
        ))

    def _sources(self, sig):
        env = run_json("fanin", str(self.sv), "-s", sig,
                       "--scope", "m", "--depth", "1")
        goal = "m." + sig
        return {e["source"].split(".")[-1] for e in env["data"]["edges"]
                if e["target"] == goal}

    def test_rhs_feeds_only_own_lhs(self):
        # b feeds only q, so it must NOT appear as a source of r.
        self.assertEqual(self._sources("r"), {"a", "en"})

    def test_full_data_and_control_deps_kept(self):
        self.assertEqual(self._sources("q"), {"a", "b", "en"})


class FlowSummaryTests(unittest.TestCase):
    """`fanin/fanout --summary` returns counts + direct neighbors, not the
    full node/edge graph."""

    def test_summary_omits_full_graph(self):
        env = run_json("fanin", "-d", "examples/trace", "-s", "result",
                       "--scope", "trace_top.u_dp", "--summary")
        d = env["data"]
        self.assertTrue(d["summary_only"])
        self.assertNotIn("edges", d)
        self.assertNotIn("nodes", d)
        self.assertGreater(d["edge_count"], 0)
        self.assertGreater(d["node_count"], 0)
        self.assertIn("1", d["edges_by_depth"])
        self.assertEqual(d["direct"], ["trace_top.u_dp.sum"])

    def test_fanout_summary_direct_sinks(self):
        env = run_json("fanout", "-d", "examples/trace", "-s", "mux_out",
                       "--scope", "trace_top.u_dp", "--summary")
        d = env["data"]
        self.assertTrue(d["summary_only"])
        self.assertIn("trace_top.u_dp.sum", d["direct"])


class TraceCrossRemovedTests(unittest.TestCase):
    """`trace --cross` was removed: cross-hierarchy is fanin/fanout's job."""

    def test_help_has_no_cross_flag(self):
        self.assertNotIn("--cross", run_help("trace"))

    def test_cross_flag_is_rejected(self):
        proc = subprocess.run(
            RTLSCANNER + ["trace", "-d", "examples/basic", "-s", "a",
                          "--scope", "top", "--cross", "--json"],
            cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 2)   # argparse rejects unknown flag

    def test_trace_output_has_no_cross_hierarchy(self):
        env = run_json("trace", "-d", "examples/basic", "-s", "a", "--scope", "top")
        self.assertNotIn("cross_hierarchy", env["data"]["results"][0])


if __name__ == "__main__":
    unittest.main()

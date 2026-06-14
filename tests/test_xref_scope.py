import textwrap
import unittest
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def compile_paths(*paths):
    # Import pyslang-backed modules lazily.  Some CI environments have shown
    # unstable subprocess behavior when the parent pytest process imports the
    # pyslang extension before the older subprocess-based tests run.
    from rtl_common import build_compilation, collect_filelist

    fl = collect_filelist([str(ROOT / p) for p in paths])
    comp, _ = build_compilation(fl.sources, fl.include_dirs, fl.defines)
    return comp


def compile_file(path):
    from rtl_common import build_compilation

    comp, _ = build_compilation([str(path)])
    return comp


def ref_keys(matches):
    refs = []
    for match in matches:
        for ref in match.references:
            refs.append((ref.access, ref.kind, ref.port))
    return set(refs)


class XrefAnalyzerTests(unittest.TestCase):
    def test_xref_reports_assign_and_port_references(self):
        from rtl_xref import XrefAnalyzer

        xa = XrefAnalyzer(compile_paths("examples/trace"))
        matches = xa.xref("trace_top.u_dp", "mux_out")

        self.assertEqual(len(matches), 1)
        self.assertIn(("read", "continuous_assign", ""), ref_keys(matches))
        self.assertIn(("write", "port_connection", "y"), ref_keys(matches))
        self.assertIn(("read", "port_connection", "d"), ref_keys(matches))

    def test_xref_keeps_port_alias_definition(self):
        from rtl_xref import XrefAnalyzer

        xa = XrefAnalyzer(compile_paths("examples/basic"))
        matches = xa.xref("top.u_dp0.u_reg", "q")
        data = matches[0].to_dict()
        kinds = {d["kind"] for d in data["definitions"]}
        dirs = {d.get("direction") for d in data["definitions"]}

        self.assertIn("Variable", kinds)
        self.assertIn("Port", kinds)
        self.assertIn("output", dirs)
        self.assertEqual(data["summary"]["writes"], 1)

    def test_xref_reports_module_definitions_and_instances(self):
        from rtl_xref import XrefAnalyzer

        xa = XrefAnalyzer(compile_paths("examples/trace"), root=ROOT)
        result = xa.xref_module("mux2")
        data = result.to_dict()

        self.assertEqual(data["target"]["kind"], "module")
        self.assertEqual(data["definitions"][0]["location"]["file"], "./examples/trace/trace_top.sv")
        self.assertEqual(data["definitions"][0]["line"], 1)
        self.assertEqual(data["references"][0]["instance_path"], "trace_top.u_dp.u_mux")
        self.assertEqual(data["references"][0]["location"]["file"], "./examples/trace/trace_top.sv")
        self.assertEqual(data["summary"]["instances"], 1)

    def test_xref_module_can_filter_instances_by_scope(self):
        from rtl_xref import XrefAnalyzer

        xa = XrefAnalyzer(compile_paths("examples/trace"), root=ROOT)

        in_scope = xa.xref_module("mux2", scope_path="trace_top.u_dp")
        out_scope = xa.xref_module("mux2", scope_path="trace_top.u_dp.u_pipe")

        self.assertEqual(len(in_scope.references), 1)
        self.assertEqual(len(out_scope.references), 0)

    def test_xref_path_style_name(self):
        from rtl_xref import XrefAnalyzer

        xa = XrefAnalyzer(compile_paths("examples/trace"), root=ROOT, path_style="name")
        result = xa.xref_module("mux2")

        self.assertEqual(result.definitions[0].file, "trace_top.sv")


class ScopeAnalyzerTests(unittest.TestCase):
    def test_scope_reports_elaborated_parameter(self):
        from rtl_scope import ScopeAnalyzer

        analyzer = ScopeAnalyzer(compile_paths("examples/trace"))
        data = analyzer.describe("trace_top.u_dp.u_pipe", {"params"})
        params = {p["name"]: p for p in data["params"]}

        self.assertIn("W", params)
        self.assertEqual(params["W"]["value"], "8")
        self.assertTrue(params["W"]["is_overridden"])

    def test_scope_groups_params_and_typedefs_under_one_command(self):
        tmp = Path(tempfile.mkdtemp()) / "rtlscanner_scope_test.sv"
        tmp.write_text(textwrap.dedent(
            """
            module typed #(parameter int W = 8,
                           parameter type T = logic [W-1:0]) (
                input logic clk,
                input T in,
                output T out
            );
                localparam int L = W + 1;
                typedef enum logic [1:0] {IDLE, BUSY} state_t;
                typedef struct packed { logic [7:0] data; logic valid; } packet_t;
                typedef logic [L-1:0] wide_t;
                state_t state;
                wide_t tmp;
                assign out = in;
            endmodule

            module top(input logic clk, input logic [15:0] a, output logic [15:0] y);
                typed #(.W(16)) u(.clk(clk), .in(a), .out(y));
            endmodule
            """
        ))

        from rtl_scope import ScopeAnalyzer

        analyzer = ScopeAnalyzer(compile_file(tmp))
        data = analyzer.describe("top.u", {"params", "typedefs"})
        params = {p["name"]: p for p in data["params"]}
        typedefs = {t["name"]: t for t in data["typedefs"]}

        self.assertEqual(params["W"]["value"], "16")
        self.assertEqual(params["L"]["kind"], "localparam")
        self.assertEqual(params["L"]["value"], "17")
        self.assertEqual(params["L"]["bit_width"], 32)
        self.assertTrue(params["L"]["is_signed"])
        self.assertEqual(params["T"]["kind"], "type_parameter")
        self.assertEqual(params["T"]["type"], "logic[15:0]")
        self.assertEqual(params["T"]["value"], "logic[15:0]")
        self.assertEqual(params["T"]["bit_width"], 16)
        self.assertIn("state_t", typedefs)
        self.assertIn("packet_t", typedefs)
        self.assertEqual(typedefs["state_t"]["kind"], "enum")
        self.assertEqual(typedefs["state_t"]["bit_width"], 2)
        enum_values = {m["name"]: m["value"] for m in typedefs["state_t"]["member_details"]}
        self.assertEqual(enum_values["IDLE"], "2'b0")
        self.assertEqual(enum_values["BUSY"], "2'b1")
        self.assertEqual(typedefs["packet_t"]["kind"], "struct")
        fields = {f["name"]: f for f in typedefs["packet_t"]["fields"]}
        self.assertEqual(fields["data"]["bit_width"], 8)
        self.assertEqual(fields["valid"]["bit_width"], 1)
        self.assertEqual(typedefs["wide_t"]["bit_width"], 17)

    def test_scope_params_work_for_traditional_verilog(self):
        tmp = Path(tempfile.mkdtemp()) / "rtlscanner_scope_verilog_test.v"
        tmp.write_text(textwrap.dedent(
            """
            module legacy #(parameter W = 8) (input [W-1:0] a, output [W-1:0] y);
                localparam L = W + 1;
                assign y = a;
            endmodule

            module top(input [15:0] a, output [15:0] y);
                legacy #(.W(16)) u(.a(a), .y(y));
            endmodule
            """
        ))

        from rtl_scope import ScopeAnalyzer

        analyzer = ScopeAnalyzer(compile_file(tmp))
        data = analyzer.describe("top.u", {"params", "typedefs"})
        params = {p["name"]: p for p in data["params"]}

        self.assertEqual(params["W"]["value"], "16")
        self.assertEqual(params["L"]["kind"], "localparam")
        self.assertEqual(params["L"]["value"], "17")
        self.assertEqual(data["typedefs"], [])

    def test_scope_sections_are_explicit_in_api(self):
        from rtl_scope import ScopeAnalyzer

        analyzer = ScopeAnalyzer(compile_paths("examples/trace"))
        data = analyzer.describe("trace_top.u_dp.u_pipe", {"params"})

        self.assertEqual(len(data["params"]), 1)
        self.assertNotIn("typedefs", data)

    def test_scope_reports_direct_structure(self):
        from rtl_scope import ScopeAnalyzer

        analyzer = ScopeAnalyzer(compile_paths("examples/basic"))
        data = analyzer.describe("top.u_dp0", {"ports", "signals", "instances", "connections"})
        signal_names = {s["name"] for s in data["signals"]}
        instance_names = {i["instance"] for i in data["instances"]}

        self.assertEqual({p["name"] for p in data["ports"]}, {"clk", "a", "b", "y"})
        self.assertEqual(signal_names, {"q"})
        self.assertEqual(instance_names, {"u_reg", "u_alu"})
        self.assertTrue(data["connections"])


if __name__ == "__main__":
    unittest.main()

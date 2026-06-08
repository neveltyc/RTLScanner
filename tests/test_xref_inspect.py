import textwrap
import unittest
from pathlib import Path

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


class ScopeInspectorTests(unittest.TestCase):
    def test_inspect_reports_elaborated_parameter(self):
        from rtl_inspect import ScopeInspector

        inspector = ScopeInspector(compile_paths("examples/trace"))
        data = inspector.inspect("trace_top.u_dp.u_pipe")
        params = {p["name"]: p for p in data["parameters"]}

        self.assertIn("W", params)
        self.assertEqual(params["W"]["value"], "8")
        self.assertTrue(params["W"]["is_overridden"])

    def test_inspect_groups_params_and_types_under_one_command(self):
        tmp = Path("/tmp/rtlscanner_inspect_test.sv")
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

        from rtl_inspect import ScopeInspector

        inspector = ScopeInspector(compile_file(tmp))
        data = inspector.inspect("top.u")
        params = {p["name"]: p for p in data["parameters"]}
        types = {t["name"]: t for t in data["types"]}

        self.assertEqual(params["W"]["value"], "16")
        self.assertEqual(params["L"]["kind"], "localparam")
        self.assertEqual(params["L"]["value"], "17")
        self.assertEqual(params["T"]["kind"], "type_parameter")
        self.assertEqual(params["T"]["type"], "logic[15:0]")
        self.assertIn("state_t", types)
        self.assertIn("packet_t", types)
        self.assertEqual(types["wide_t"]["bit_width"], 17)

    def test_inspect_can_suppress_types_in_api(self):
        from rtl_inspect import ScopeInspector

        inspector = ScopeInspector(compile_paths("examples/trace"))
        data = inspector.inspect("trace_top.u_dp.u_pipe", want_params=True, want_types=False)

        self.assertEqual(len(data["parameters"]), 1)
        self.assertEqual(data["types"], [])


if __name__ == "__main__":
    unittest.main()

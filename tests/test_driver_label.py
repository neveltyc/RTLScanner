"""An unnamed always/initial block reports its enclosing instance.

A procedural block has no name of its own, so the driver line used to read
"always_ff block in (anonymous)".  It now falls back to the block's
hierarchical path (the instance it lives in).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]

DESIGN = (
    "module sub (input wire clk, input wire d, output reg q);\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "endmodule\n"
    "\n"
    "module top (input wire clk, input wire d, output wire q);\n"
    "  sub u_sub (.clk(clk), .d(d), .q(q));\n"
    "endmodule\n"
)


class DriverLabel(unittest.TestCase):
    def test_enclosing_instance_replaces_anonymous(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "top.sv").write_text(DESIGN)
            proc = subprocess.run(
                RTLSCANNER + ["trace", "top.sv", "-s", "q",
                              "--scope", "top.u_sub", "--json"],
                cwd=d, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            env = json.loads(proc.stdout)
            result = env["data"]["results"][0]
            desc = (result.get("driver") or {}).get("description", "")
            self.assertNotIn("anonymous", desc)
            self.assertIn("always_ff", desc)
            self.assertIn("u_sub", desc)


if __name__ == "__main__":
    unittest.main()

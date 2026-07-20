"""The generated ``--schema`` stays in sync with the actual ``--json`` output.

Regression guard for the drift where new summary fields (``truncated`` /
``limit``) and the lint finding ``module`` field were emitted at runtime but
absent from the schema contract that agents rely on (the README calls
``--schema`` "the full contract").  The structural checks are stdlib-only; a
full draft-07 validation runs additionally when ``jsonschema`` is installed.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import jsonschema  # noqa: F401
    _HAVE_JSONSCHEMA = True
except ImportError:
    _HAVE_JSONSCHEMA = False

RTLSCANNER = [sys.executable, "-m", "rtlscanner"]
ALL_TOOLS = ("tree", "trace", "driver", "scope", "fanin", "fanout", "path",
             "xref", "lint", "find")

DESIGN = (
    "module leaf(input wire a, output wire y);\n  assign y = a;\nendmodule\n"
    "module top(input wire a, output wire y);\n"
    "  leaf u_leaf(.a(a), .y(y));\nendmodule\n"
)

# A representative invoking argv per tool (the non-`--summary` JSON path, which
# is where --limit truncation lives).
CMDS = {
    "tree":   ["tree", "top.sv"],
    "scope":  ["scope", "top.sv", "--scope", "top"],
    "trace":  ["trace", "top.sv", "-s", "y", "--scope", "top"],
    "driver": ["driver", "top.sv", "-s", "y", "--scope", "top.u_leaf"],
    "fanin":  ["fanin", "top.sv", "-s", "y", "--scope", "top"],
    "fanout": ["fanout", "top.sv", "-s", "a", "--scope", "top"],
    "path":   ["path", "top.sv", "--from", "a", "--to", "y", "--scope", "top"],
    "xref":   ["xref", "top.sv", "--module", "leaf"],
    "lint":   ["lint", "top.sv", "--rules", "semantic"],
    "find":   ["find", "top.sv", "-p", "top.**"],
}


def _schema(tool):
    p = subprocess.run(RTLSCANNER + [tool, "--schema"], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(p.stdout)


def _summary_props(schema):
    return schema["properties"]["summary"]["oneOf"][0]["properties"]


def _run_json(tool, cwd):
    p = subprocess.run(RTLSCANNER + CMDS[tool] + ["--json"], text=True, cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(p.stdout)


class SummaryLimitFields(unittest.TestCase):
    def test_every_schema_declares_truncated_and_limit(self):
        for tool in ALL_TOOLS:
            props = _summary_props(_schema(tool))
            self.assertIn("truncated", props, f"{tool}: schema summary missing truncated")
            self.assertIn("limit", props, f"{tool}: schema summary missing limit")

    def test_runtime_summary_emits_truncated_and_limit(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "top.sv").write_text(DESIGN)
            for tool in ALL_TOOLS:
                env = _run_json(tool, d)
                self.assertEqual(env["status"], "ok", f"{tool}: {env.get('errors')}")
                summ = env.get("summary") or {}
                self.assertIn("truncated", summ, f"{tool}: runtime summary missing truncated")
                self.assertIn("limit", summ, f"{tool}: runtime summary missing limit")


class LintModuleAndShown(unittest.TestCase):
    def test_schema_declares_module_and_shown(self):
        sch = _schema("lint")
        finding = (sch["properties"]["data"]["oneOf"][0]["properties"]
                   ["findings"]["items"]["properties"])
        self.assertIn("module", finding)
        self.assertIn("shown", _summary_props(sch))

    def test_runtime_emits_module_on_attributed_finding(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "m.sv").write_text(
                "module m(input wire a, output wire y);\n"
                "  wire unused_w;\n  assign y = a;\nendmodule\n")
            p = subprocess.run(
                RTLSCANNER + ["lint", "m.sv", "--rules", "unused", "--json"],
                text=True, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            env = json.loads(p.stdout)
            self.assertEqual(env["data"]["findings"][0].get("module"), "m")
            self.assertIn("shown", env["summary"])


@unittest.skipUnless(_HAVE_JSONSCHEMA, "jsonschema not installed")
class FullEnvelopeValidation(unittest.TestCase):
    """Full draft-07 validation of each command's envelope against its schema."""

    def test_every_command_output_validates(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "top.sv").write_text(DESIGN)
            for tool in ALL_TOOLS:
                env = _run_json(tool, d)
                jsonschema.validate(env, _schema(tool))


if __name__ == "__main__":
    unittest.main()

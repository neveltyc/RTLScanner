"""CDC reset detection is reset-rooted, not "anything ending in _n".

The old default glob ``*_n`` matched any active-low data signal (``data_n``,
``sel_n``, ``q_n`` …), dropping it from the timing/clock events and masking real
crossings.  Active-low resets must now carry an rst/reset/arst/por/clr root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

RESETS = ["rst", "rst_n", "rstn", "arst_n", "arstn", "reset", "reset_n",
          "resetn", "aresetn", "por_n", "clr_n", "nrst", "n_rst", "n_reset",
          "cpu_rst", "s_rst_n", "cpu_por_n"]

NON_RESETS = ["data_n", "sel_n", "q_n", "we_n", "oe_n", "cs_n", "en_n",
              "valid_n", "irq_n", "ack_n", "clk_n", "addr_n"]


def _cdc():
    from rtl_common import build_compilation
    from rtl_lint import CDCAnalyzer
    with tempfile.TemporaryDirectory() as d:
        sv = Path(d) / "m.sv"
        sv.write_text("module m(input wire clk); endmodule\n")
        cr, _ = build_compilation([str(sv)])
        return CDCAnalyzer(cr.comp)


class CdcResetGlobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cdc = _cdc()

    def test_reset_names_are_resets(self):
        for n in RESETS:
            self.assertTrue(self.cdc._looks_like_reset(n),
                            f"{n!r} should be recognized as a reset")

    def test_active_low_data_is_not_reset(self):
        for n in NON_RESETS:
            self.assertFalse(self.cdc._looks_like_reset(n),
                             f"{n!r} must NOT be treated as a reset")

    def test_user_globs_extend_defaults(self):
        from rtl_common import build_compilation
        from rtl_lint import CDCAnalyzer
        with tempfile.TemporaryDirectory() as d:
            sv = Path(d) / "m.sv"
            sv.write_text("module m(input wire clk); endmodule\n")
            cr, _ = build_compilation([str(sv)])
            cdc = CDCAnalyzer(cr.comp, reset_globs=["my_special_n"])
            self.assertTrue(cdc._looks_like_reset("my_special_n"))
            self.assertTrue(cdc._looks_like_reset("rst_n"))  # defaults kept


if __name__ == "__main__":
    unittest.main()

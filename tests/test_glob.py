"""Segment-aware glob matching (``rtl_glob.wildcard_match``).

``find`` matches hierarchical paths with segment-aware wildcard semantics.
These cases pin the segment boundary (``.``) handling that makes ``*``
single-segment and ``**`` / ``...`` recursive — plus the README examples.
Pure string logic — no pyslang needed.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_glob import regex_match, compile_regex, wildcard_match  # noqa: E402


class WildcardMatch(unittest.TestCase):
    def _check(self, cases):
        for text, pattern, expected in cases:
            self.assertEqual(
                wildcard_match(text, pattern), expected,
                msg=f"match({text!r}, {pattern!r}) should be {expected}")

    def test_exact_literal(self):
        self._check([
            ("foo.bar", "foo.bar", True),
            ("foo.bar", "foo.baz", False),
            ("foo.bar", "foo", False),
            ("foo", "foo.bar", False),
            ("", "", True),
        ])

    def test_star_is_single_segment(self):
        self._check([
            ("foo", "*", True),
            ("foo.bar", "foo.*", True),
            ("foo.bar", "*.bar", True),
            ("foo.bar", "f*.b*", True),
            ("foo.bar", "*", False),            # '*' does not cross '.'
            ("foo.bar.baz", "foo.*", False),
            ("foo.bar.baz", "*.baz", False),
        ])

    def test_star_matches_empty(self):
        self._check([
            ("foo", "foo*", True),
            ("foo", "*foo", True),
            ("foo", "*foo*", True),
            ("", "*", True),
        ])

    def test_double_star_crosses_boundaries(self):
        self._check([
            ("foo.bar.baz", "**", True),
            ("foo.bar.baz", "foo.**", True),
            ("foo.bar.baz", "**.baz", True),
            ("foo.bar.baz", "foo.**.baz", True),
            ("foo.baz", "foo.**.baz", True),         # zero intermediate segments
            ("foo.x.y.z.baz", "foo.**.baz", True),
            ("foo", "**", True),
            ("", "**", True),
        ])

    def test_double_star_zero_segment_boundaries(self):
        self._check([
            ("foo", "foo.**", True),
            ("foo.x", "foo.**", True),
            ("foo.x.y", "foo.**", True),
            ("fo", "foo.**", False),
            ("foox", "foo.**", False),
            ("baz", "**.baz", True),
            ("x.baz", "**.baz", True),
            ("x.y.baz", "**.baz", True),
            ("xbaz", "**.baz", False),
            ("fooXbaz", "foo.**.baz", False),
        ])

    def test_triple_dot_equivalent_to_double_star(self):
        self._check([
            ("foo.bar.baz", "...", True),
            ("foo.bar.baz", "foo...", True),
            ("foo.bar.baz", "...baz", True),
            ("foo.bar.baz", "foo....baz", True),
            ("foo.baz", "foo....baz", True),
            ("foo.x.y.z.baz", "foo....baz", True),
            ("", "...", True),
        ])

    def test_question_is_single_non_dot(self):
        self._check([
            ("a", "?", True),
            ("foo.a", "foo.?", True),
            ("foo.ab", "foo.??", True),
            ("foo.ab", "foo.?", False),
            ("", "?", False),
            ("a.b", "a?b", False),     # '?' does not match '.'
            (".", "?", False),
        ])

    def test_combinations(self):
        self._check([
            ("top.u_a", "top.u_*", True),
            ("top.u_bar", "top.u_*", True),
            ("top.u_a.x", "top.u_*", False),
            ("top.u_a.x.y", "top.**.y", True),
            ("top.u_a", "top.u_?", True),
            ("top.u_ab", "top.u_?", False),
            ("top.u_ab", "top.u_?*", True),
        ])


class RegexMatch(unittest.TestCase):
    """Regex matching is whole-string (``re.fullmatch`` ≈ ``std::regex_match``)."""

    def test_anchored_whole_string(self):
        rx = compile_regex(r"top\..*\.q")
        self.assertTrue(regex_match("top.u_dp.q", rx))
        self.assertTrue(regex_match("top.a.b.q", rx))
        # A trailing-only or leading-only hit must NOT match (full match, not
        # search) — `xtop.u.q` has extra leading chars, `top.u.qx` trailing.
        self.assertFalse(regex_match("xtop.u.q", rx))
        self.assertFalse(regex_match("top.u.qx", rx))


if __name__ == "__main__":
    unittest.main()

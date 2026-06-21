"""Segment-aware glob matching for hierarchical names.

A small, pyslang-free helper used by ``find`` to match elaborated hierarchical
paths (``top.sub.signal``) against a wildcard pattern.  Path segments are
separated by ``.`` and the wildcards respect that boundary, mirroring
slang-netlist's ``wildcardMatch`` (``include/common/Wildcard.hpp``) so a pattern
written for one tool means the same in the other:

    ``*``          zero or more characters *within* one segment (never ``.``)
    ``**`` / ``...``  zero or more characters *across* segments (recursive)
    ``?``          exactly one character within a segment (never ``.``)
    anything else  matched literally

When a recursive wildcard sits next to a literal ``.`` (``a.**.b``, ``a.**``,
``**.b``) the adjacent ``.`` is an *optional* segment boundary, so ``a.**.b``
matches ``a.b``, ``a.x.b``, and ``a.x.y.b`` alike — the gitignore ``/**/`` and
LRM ``.../`` convention.
"""

from __future__ import annotations

import re

# A sentinel that cannot appear in a real path/pattern, used as the past-the-end
# "character" so the index-based walk reads like the C pointer arithmetic it is
# ported from (``*p == '\0'``).
_NUL = "\0"


def _at(s: str, i: int) -> str:
    return s[i] if 0 <= i < len(s) else _NUL


def wildcard_match(text: str, pattern: str) -> bool:
    """Return True when ``text`` matches the glob ``pattern`` (segment-aware).

    A faithful port of slang-netlist's recursive ``wildcardMatch``; see the
    module docstring for the wildcard semantics.
    """

    def m(t: int, p: int) -> bool:
        while _at(pattern, p) != _NUL:
            # A recursive wildcard token (`**` / `...`), optionally preceded by a
            # `.` we can absorb as part of a segment-boundary match.
            has_lead = False
            pp = p
            if _at(pattern, pp) == ".":
                q = pp + 1
                is_recur = (_at(pattern, q) == "*" and _at(pattern, q + 1) == "*") or (
                    _at(pattern, q) == "." and _at(pattern, q + 1) == "."
                    and _at(pattern, q + 2) == ".")
                if is_recur:
                    has_lead = True
                    pp = q

            recur_len = 0
            if _at(pattern, pp) == "*" and _at(pattern, pp + 1) == "*":
                recur_len = 2
            elif (_at(pattern, pp) == "." and _at(pattern, pp + 1) == "."
                  and _at(pattern, pp + 2) == "."):
                recur_len = 3

            if recur_len != 0:
                after = pp + recur_len
                has_trail = _at(pattern, after) == "."
                rest = after + 1 if has_trail else after

                if has_lead and has_trail:
                    # `.**.` — consume a segment boundary `.` plus zero or more
                    # whole `<chars>.` segments before the boundary.
                    if _at(text, t) != ".":
                        return False
                    t += 1
                    if m(t, rest):
                        return True
                    while _at(text, t) != _NUL:
                        if _at(text, t) == "." and m(t + 1, rest):
                            return True
                        t += 1
                    return False

                if has_lead:
                    # `.**` with no trailing dot: match nothing, or `.` then any
                    # suffix (including further `.`s).
                    if m(t, rest):
                        return True
                    if _at(text, t) != ".":
                        return False
                    t += 1
                    while True:
                        if m(t, rest):
                            return True
                        if _at(text, t) == _NUL:
                            return False
                        t += 1

                if has_trail:
                    # `**.` with no leading dot: match nothing, or any prefix
                    # ending at a `.` (which the trailing `.` absorbs).
                    if m(t, rest):
                        return True
                    while _at(text, t) != _NUL:
                        if _at(text, t) == "." and m(t + 1, rest):
                            return True
                        t += 1
                    return False

                # Standalone `**` / `...`: match any (possibly empty) run.
                while True:
                    if m(t, rest):
                        return True
                    if _at(text, t) == _NUL:
                        return False
                    t += 1

            if _at(pattern, p) == "*":
                # Single-segment wildcard: zero or more non-`.` characters.
                rest = p + 1
                while True:
                    if m(t, rest):
                        return True
                    c = _at(text, t)
                    if c == _NUL or c == ".":
                        return False
                    t += 1

            if _at(pattern, p) == "?":
                c = _at(text, t)
                if c == _NUL or c == ".":
                    return False
                p += 1
                t += 1
                continue

            if _at(pattern, p) != _at(text, t):
                return False
            p += 1
            t += 1

        return _at(text, t) == _NUL

    return m(0, 0)


def compile_regex(pattern: str):
    """Compile a regex pattern, raising ``re.error`` on a bad pattern.

    Kept here so ``find`` can share one definition of "full match" semantics —
    callers use :func:`regex_match`, which anchors the whole string the way
    slang-netlist's ``std::regex_match`` does.
    """
    return re.compile(pattern)


def regex_match(text: str, compiled) -> bool:
    """Whole-string regex match (``re.fullmatch`` semantics)."""
    return compiled.fullmatch(text) is not None

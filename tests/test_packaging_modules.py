"""Packaging guard: every top-level module under src/ must be declared in
pyproject.toml's ``[tool.setuptools] py-modules``.

A non-editable install (``pip install .``, wheel, PyPI, pipx) ships exactly the
modules listed in ``py-modules``; anything in src/ that gets imported but is not
listed becomes a ``ModuleNotFoundError`` the first time the ``rtlscanner`` entry
point runs.  An editable install (``pip install -e .``) puts all of src/ on
``sys.path`` regardless, so it masks the omission -- which is why this needs an
explicit test instead of relying on the suite (which also runs from source).
"""

import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.8-3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover - tomli is a runtime dep there
        tomllib = None

ROOT = Path(__file__).resolve().parents[1]


class PackagingModules(unittest.TestCase):
    @unittest.skipIf(tomllib is None, "no TOML reader (tomllib/tomli) available")
    def test_py_modules_matches_src(self):
        actual = {p.stem for p in (ROOT / "src").glob("*.py")}
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
        declared = set(meta["tool"]["setuptools"]["py-modules"])

        missing = sorted(actual - declared)   # in src/, not packaged
        stale = sorted(declared - actual)     # declared, no such file
        self.assertEqual(
            (missing, stale), ([], []),
            "\npyproject.toml [tool.setuptools] py-modules is out of sync with src/:\n"
            f"  missing (would ModuleNotFoundError on a non-editable install): {missing}\n"
            f"  stale   (declared, no such file): {stale}",
        )


if __name__ == "__main__":
    unittest.main()

"""
RTLScanner config + env var + input resolution.

Three-tier merge: CLI args > RTLSCANNER_* env > ./.rtlscanner.toml > built-in defaults.
The `resolve_inputs` function is the single entry point all subcommands call.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rtl_common import (
    FileList,
    collect_filelist,
    parse_filelist,
    merge_filelists,
    filter_filelist,
)

CONFIG_NAME = ".rtlscanner.toml"
LEGACY_CONFIG_NAME = ".rtllint.toml"
DEFAULT_PREFIX = "${PROJPATH}"


# ── Config file loading ─────────────────────────────────────────────
def _load_toml(text: str) -> dict:
    for mod in ("tomllib", "tomli"):
        try:
            return __import__(mod).loads(text)
        except ModuleNotFoundError:
            continue
    raise SystemExit(
        "Error: reading TOML config needs Python 3.11+ (tomllib) or `pip install tomli`."
    )


def load_config(cwd: Optional[Path] = None) -> Tuple[dict, Optional[Path]]:
    """Look for ./.rtlscanner.toml; return (config_dict, path_or_None).

    Also emits a stderr note when a legacy .rtllint.toml is found (without
    reading it).
    """
    cwd = (cwd or Path.cwd()).resolve()
    legacy = cwd / LEGACY_CONFIG_NAME
    if legacy.is_file():
        print(
            f"note: found {LEGACY_CONFIG_NAME}; rename to {CONFIG_NAME} "
            "(lint config moves under [lint.*])",
            file=sys.stderr,
        )

    cfg_path = cwd / CONFIG_NAME
    if not cfg_path.is_file():
        return {}, None
    try:
        return _load_toml(cfg_path.read_text(errors="ignore")), cfg_path
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"Error: failed to parse {cfg_path}: {e}")


# ── Env var helpers ─────────────────────────────────────────────────
ENV_FILELIST = "RTLSCANNER_FILELIST"
ENV_DIR = "RTLSCANNER_DIR"
ENV_EXCLUDE = "RTLSCANNER_EXCLUDE"
ENV_ROOT = "RTLSCANNER_ROOT"
ENV_PREFIX = "RTLSCANNER_PREFIX"


def _env_list(name: str) -> List[str]:
    val = os.environ.get(name, "").strip()
    if not val:
        return []
    return [s for s in val.split(":") if s]


def _env_str(name: str) -> Optional[str]:
    val = os.environ.get(name)
    return val if val else None


# ── Resolved inputs ─────────────────────────────────────────────────
@dataclass
class ResolvedInputs:
    """The final, merged set of inputs after CLI/env/config resolution."""

    filelist_files: List[str] = field(default_factory=list)
    dir_paths: List[str] = field(default_factory=list)
    positional_files: List[str] = field(default_factory=list)
    excludes: List[str] = field(default_factory=list)
    root: Path = field(default_factory=lambda: Path("."))
    prefix: str = DEFAULT_PREFIX
    notes: List[str] = field(default_factory=list)
    config_path: Optional[Path] = None
    filelist: Optional[FileList] = None


# ── Three-tier merge ────────────────────────────────────────────────
def _pick_list(*candidates: List[str]) -> List[str]:
    """Return the first non-empty list (CLI > env > config), else [].

    Field-level override: higher-priority layer wins outright.
    """
    for c in candidates:
        if c:
            return list(c)
    return []


def _pick_str(*candidates: Optional[str]) -> Optional[str]:
    for c in candidates:
        if c:
            return c
    return None


def resolve_inputs(
    *,
    cli_files: List[str],
    cli_dir: List[str],
    cli_filelist: List[str],
    cli_exclude: List[str],
    config: Optional[dict] = None,
    config_path: Optional[Path] = None,
) -> ResolvedInputs:
    """Merge CLI / env / config into a final set, parse filelists, scan dirs.

    Does NOT raise on missing sources — the caller decides whether empty is fatal.
    """
    cfg_inputs = ((config or {}).get("inputs") or {}) if isinstance(config, dict) else {}

    filelist_files = _pick_list(
        cli_filelist,
        _env_list(ENV_FILELIST),
        cfg_inputs.get("filelist") or [],
    )
    dir_paths = _pick_list(
        cli_dir,
        _env_list(ENV_DIR),
        cfg_inputs.get("dir") or [],
    )
    excludes = _pick_list(
        cli_exclude,
        _env_list(ENV_EXCLUDE),
        cfg_inputs.get("exclude") or [],
    )

    root_str = _pick_str(
        None,
        _env_str(ENV_ROOT),
        cfg_inputs.get("root"),
    ) or "."
    prefix = _pick_str(
        None,
        _env_str(ENV_PREFIX),
        cfg_inputs.get("prefix"),
    ) or DEFAULT_PREFIX

    out = ResolvedInputs(
        filelist_files=filelist_files,
        dir_paths=list(dir_paths),
        positional_files=list(cli_files),
        excludes=list(excludes),
        root=Path(root_str).expanduser().resolve(),
        prefix=prefix,
        config_path=config_path,
    )

    # Filelist wins over dir/positional
    if filelist_files and (dir_paths or cli_files):
        out.notes.append(
            "using filelist; ignoring -d/--dir and positional sources"
        )
        out.dir_paths = []
        out.positional_files = []

    return out


def build_filelist(ri: ResolvedInputs) -> FileList:
    """Parse filelists and/or scan dirs into a FileList; apply excludes.

    Raises FileNotFoundError on missing .f files; raises ValueError if a
    filelist uses the configured prefix token but no root was configured.
    """
    parsed = []
    for fl in ri.filelist_files:
        try:
            pl = parse_filelist(fl, ri.root, prefix=ri.prefix)
        except FileNotFoundError:
            raise
        # Detect "prefix used but root looks unconfigured"
        if (
            ri.prefix
            and ri.prefix in Path(fl).read_text(errors="ignore")
            and ri.root == Path(".").expanduser().resolve()
            and not os.environ.get(ENV_ROOT)
            and not (ri.config_path)
        ):
            # User likely needs to set root somewhere
            raise ValueError(
                f"filelist {fl!r} uses {ri.prefix!r} but no root is configured\n"
                f"  set [inputs].root in ./{CONFIG_NAME}, or\n"
                f"  export {ENV_ROOT}=<path>"
            )
        parsed.append(pl)

    paths = list(ri.positional_files) + list(ri.dir_paths)
    scanned = (
        collect_filelist(paths, excludes=ri.excludes, root=ri.root)
        if paths
        else FileList()
    )
    merged = merge_filelists(*parsed, scanned)
    return filter_filelist(merged, ri.excludes, ri.root)


# ── Lint config helpers (extracted from old rtl_lint.discover/load) ──
def lint_config(cfg: dict) -> dict:
    """Return the [lint] section of a config dict, or {} if absent."""
    if not isinstance(cfg, dict):
        return {}
    return cfg.get("lint") or {}

# ── Xref config helpers ─────────────────────────────────────────────
def xref_config(cfg: dict) -> dict:
    """Return normalized [xref] config options."""
    raw = (cfg.get("xref") or {}) if isinstance(cfg, dict) else {}
    path_style = str(raw.get("path_style") or "relative").strip().lower()
    if path_style not in {"relative", "absolute", "name"}:
        path_style = "relative"
    return {"path_style": path_style}

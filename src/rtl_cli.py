"""Shared CLI preparation helpers for RTLScanner subcommands."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import agent_json
from rtl_common import FileList, build_compilation
from rtl_config import (
    ResolvedInputs,
    build_filelist,
    load_config,
    resolve_inputs,
)


class CliError(agent_json.AgentError):
    """Structured CLI failure with a human-mode exit code."""

    def __init__(self, code: str, message: str, exit_code: int = 2):
        super().__init__(code, message)
        self.exit_code = int(exit_code)


@dataclass
class PreparedInputs:
    config: dict
    config_path: Optional[Path]
    resolved_inputs: ResolvedInputs
    filelist: FileList


@dataclass
class PreparedCompilation(PreparedInputs):
    comp: Any = None
    diagnostics: List[str] = field(default_factory=list)


def prepare_inputs(
    args,
    *,
    require_sources: bool = True,
    human_error_rc: int = 2,
) -> PreparedInputs:
    """Load config, resolve inputs, and build the effective filelist."""
    try:
        cfg, cfg_path = load_config()
    except agent_json.AgentError as e:
        # load_config raises a bare AgentError (e.g. BAD_CONFIG) which has no
        # exit_code, so it would fall through main()'s getattr(e, "exit_code", 2)
        # default and ignore this command's human_error_rc.  Re-wrap it as a
        # CliError so a malformed config exits with the same human-mode code the
        # command uses for every other input failure.
        raise CliError(e.code, e.message, human_error_rc)
    ri = resolve_inputs(
        cli_files=list(getattr(args, "files", []) or []),
        cli_dir=list(getattr(args, "dir", []) or []),
        cli_filelist=list(getattr(args, "filelist", []) or []),
        cli_exclude=list(getattr(args, "exclude", []) or []),
        config=cfg,
        config_path=cfg_path,
    )
    for note in ri.notes:
        print(f"note: {note}", file=sys.stderr)

    try:
        filelist = build_filelist(ri)
    except FileNotFoundError as e:
        raise CliError(agent_json.ERR_BAD_FILELIST, str(e), human_error_rc)
    except ValueError as e:
        raise CliError(agent_json.ERR_INPUT_NOT_FOUND, str(e), human_error_rc)

    if require_sources and not filelist.sources:
        raise CliError(
            agent_json.ERR_INPUT_NOT_FOUND,
            "no .v/.sv source files found",
            human_error_rc,
        )

    return PreparedInputs(
        config=cfg,
        config_path=cfg_path,
        resolved_inputs=ri,
        filelist=filelist,
    )


def prepare_compilation(
    args,
    *,
    require_sources: bool = True,
    human_error_rc: int = 2,
) -> PreparedCompilation:
    """Prepare inputs and build a pyslang compilation."""
    prepared = prepare_inputs(
        args,
        require_sources=require_sources,
        human_error_rc=human_error_rc,
    )
    fl = prepared.filelist
    try:
        comp, diagnostics = build_compilation(
            fl.sources,
            fl.include_dirs,
            fl.defines,
        )
    except Exception as e:
        raise CliError(
            agent_json.ERR_COMPILE_FAILED,
            f"compilation failed: {e}",
            human_error_rc,
        )

    return PreparedCompilation(
        config=prepared.config,
        config_path=prepared.config_path,
        resolved_inputs=prepared.resolved_inputs,
        filelist=prepared.filelist,
        comp=comp,
        diagnostics=diagnostics,
    )


def resolve_scope(
    provided_scope: Optional[str],
    top_paths,
    *,
    human_error_rc: int = 2,
) -> str:
    """Return a provided scope or auto-select the sole top scope."""
    if provided_scope is not None:
        return provided_scope

    tops = list(top_paths or [])
    if len(tops) == 1:
        return tops[0]
    if tops:
        raise CliError(
            agent_json.ERR_SCOPE_NOT_FOUND,
            "multiple tops, specify --scope: " + ", ".join(tops),
            human_error_rc,
        )
    raise CliError(
        agent_json.ERR_NO_TOP,
        "no top modules found",
        human_error_rc,
    )

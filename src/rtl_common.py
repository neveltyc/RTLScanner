"""
Shared utilities for the RTLScanner tool family.

Provides Color, FileList, file collection, filelist parsing, and
compilation building.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
from pathlib import Path
from dataclasses import dataclass, field

try:
    import pyslang
    import pyslang.ast as ast
    from pyslang.syntax import SyntaxTree
except ImportError:
    print("Error: pyslang is required.  pip install pyslang", file=sys.stderr)
    sys.exit(1)


# ── ANSI Colors ──────────────────────────────────────────────────────
class Color:
    """ANSI color codes, auto-disabled when not writing to a terminal."""
    _enabled = True

    @classmethod
    def disable(cls):
        cls._enabled = False

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls._enabled else text

    @classmethod
    def green(cls, t):   return cls._wrap("1;32", t)
    @classmethod
    def cyan(cls, t):    return cls._wrap("1;36", t)
    @classmethod
    def yellow(cls, t):  return cls._wrap("33", t)
    @classmethod
    def dim(cls, t):     return cls._wrap("2", t)
    @classmethod
    def red(cls, t):     return cls._wrap("1;31", t)
    @classmethod
    def magenta(cls, t): return cls._wrap("35", t)
    @classmethod
    def bold(cls, t):    return cls._wrap("1", t)
    @classmethod
    def blue(cls, t):    return cls._wrap("1;34", t)


# ── Data Structures ──────────────────────────────────────────────────
@dataclass
class FileList:
    """Normalized VCS-style filelist content."""
    sources: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)


SOURCE_EXTENSIONS = {'.v', '.sv'}
INCLUDE_EXTENSIONS = {'.svh', '.vh', '.svi'}
ALL_EXTENSIONS = SOURCE_EXTENSIONS | INCLUDE_EXTENSIONS
TOP_LEVEL_RE = re.compile(
    r'(?m)^\s*(?:\(\*.*?\*\)\s*)*'
    r'(module|interface|package|program|primitive)\b'
)
OPTIONS_WITH_VALUE = {
    '-cm_dir',
    '-cm_hier',
    '-f',
    '-F',
    '-l',
    '-Mdir',
    '-o',
    '-top',
    '-v',
    '-xprop',
    '-y',
}


# ── Core: Parse & Build Hierarchy ────────────────────────────────────
def _norm_abs(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve())


def _dedupe(items: list[str]) -> list[str]:
    result = []
    seen = set()
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _strip_sv_comments(text: str) -> str:
    """Remove comments well enough for declaration sniffing."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//.*', '', text)


def has_top_level_declaration(path: str) -> bool:
    """Return True if a .v/.sv file defines a compilation-unit item."""
    try:
        text = Path(path).read_text(errors='ignore')
    except OSError:
        return False
    return bool(TOP_LEVEL_RE.search(_strip_sv_comments(text)))


def is_excluded(path: Path | str, patterns: list[str], root: Path) -> bool:
    if not patterns:
        return False

    path = Path(path).expanduser()
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        rel = path.as_posix()

    abs_posix = path.resolve().as_posix()
    name = path.name
    for pattern in patterns:
        p = pattern.replace(os.sep, '/')
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(abs_posix, p):
            return True
        if '/' not in p and fnmatch.fnmatch(name, p):
            return True
    return False


def classify_hdl_file(path: str) -> str:
    """Classify a file as source, include, or unsupported."""
    suffix = Path(path).suffix
    if suffix in INCLUDE_EXTENSIONS:
        return 'include'
    if suffix in SOURCE_EXTENSIONS:
        return 'source' if has_top_level_declaration(path) else 'include'
    return 'unsupported'


def collect_filelist(
    paths: list[str],
    recursive: bool = True,
    excludes: list[str] = None,
    root: Path = None,
) -> FileList:
    """Gather HDL sources and include dirs from paths."""
    excludes = excludes or []
    root = (root or Path.cwd()).resolve()
    candidates = []
    for p in paths:
        path = Path(p).expanduser()
        if path.is_file() and path.suffix in ALL_EXTENSIONS:
            if not is_excluded(path, excludes, root):
                candidates.append(_norm_abs(path))
        elif path.is_dir():
            glob_fn = path.rglob if recursive else path.glob
            for ext in ALL_EXTENSIONS:
                for f in glob_fn(f'*{ext}'):
                    if not is_excluded(f, excludes, root):
                        candidates.append(_norm_abs(f))

    filelist = FileList()
    for path in sorted(_dedupe(candidates)):
        kind = classify_hdl_file(path)
        if kind == 'source':
            filelist.sources.append(path)
        elif kind == 'include':
            filelist.include_dirs.append(_norm_abs(Path(path).parent))

    filelist.include_dirs = _dedupe(sorted(filelist.include_dirs))
    return filelist


def collect_files(paths: list[str], recursive: bool = True) -> list[str]:
    """Backward-compatible helper that returns source files only."""
    return collect_filelist(paths, recursive=recursive).sources


def resolve_filelist_path(
    token: str,
    root: Path,
    current_dir: Path,
    prefix: str = None,
) -> Path:
    token = os.path.expandvars(token)
    token = os.path.expanduser(token)

    if prefix and token.startswith(prefix):
        suffix = token[len(prefix):].lstrip('/\\')
        return root / suffix

    path = Path(token)
    if path.is_absolute():
        return path

    root_candidate = root / path
    if root_candidate.exists():
        return root_candidate
    return current_dir / path


def parse_filelist(
    path: str,
    root: Path,
    prefix: str = None,
    seen: set[str] = None,
    current_dir: Path = None,
) -> FileList:
    """Parse a VCS-style .f file into normalized sources, dirs, and defines."""
    root = root.resolve()
    base_dir = (current_dir or root).resolve()
    seen = seen or set()
    filelist_path = resolve_filelist_path(path, root, base_dir, prefix).resolve()
    if str(filelist_path) in seen:
        return FileList()
    seen.add(str(filelist_path))

    if not filelist_path.exists():
        raise FileNotFoundError(f"filelist not found: {path}")

    result = FileList()
    logical_lines = []
    pending = ''
    for raw in filelist_path.read_text(errors='ignore').splitlines():
        line = raw.strip()
        if not line or line.startswith('//') or line.startswith('#'):
            continue
        line = line.split('//', 1)[0].split('#', 1)[0].strip()
        if not line:
            continue
        if line.endswith('\\'):
            pending += line[:-1] + ' '
            continue
        logical_lines.append(pending + line)
        pending = ''
    if pending:
        logical_lines.append(pending)

    current_dir = filelist_path.parent
    skip_next = False
    for line in logical_lines:
        try:
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()

        i = 0
        while i < len(tokens):
            token = tokens[i]
            if skip_next:
                skip_next = False
                i += 1
                continue

            if token in ('-f', '-F'):
                if i + 1 < len(tokens):
                    nested = parse_filelist(
                        tokens[i + 1], root, prefix=prefix,
                        seen=seen, current_dir=current_dir)
                    result.include_dirs.extend(nested.include_dirs)
                    result.defines.extend(nested.defines)
                    result.sources.extend(nested.sources)
                    i += 2
                    continue
            elif token.startswith('-f') and len(token) > 2:
                nested = parse_filelist(
                    token[2:], root, prefix=prefix,
                    seen=seen, current_dir=current_dir)
                result.include_dirs.extend(nested.include_dirs)
                result.defines.extend(nested.defines)
                result.sources.extend(nested.sources)
                i += 1
                continue
            elif token.startswith('+incdir+'):
                incs = [part for part in token[len('+incdir+'):].split('+') if part]
                for inc in incs:
                    inc_path = resolve_filelist_path(inc, root, current_dir, prefix)
                    result.include_dirs.append(_norm_abs(inc_path))
                i += 1
                continue
            elif token.startswith('+define+'):
                defines = token[len('+define+'):]
                if defines:
                    result.defines.extend(d for d in defines.split('+') if d)
                i += 1
                continue
            elif token in OPTIONS_WITH_VALUE:
                skip_next = True
                i += 1
                continue
            elif token.startswith('-') or token.startswith('+'):
                i += 1
                continue

            path_token = resolve_filelist_path(token, root, current_dir, prefix)
            if path_token.suffix in ALL_EXTENSIONS and path_token.exists():
                path_abs = _norm_abs(path_token)
                kind = classify_hdl_file(path_abs)
                if kind == 'source':
                    result.sources.append(path_abs)
                elif kind == 'include':
                    result.include_dirs.append(_norm_abs(Path(path_abs).parent))
            elif path_token.suffix in INCLUDE_EXTENSIONS:
                result.include_dirs.append(_norm_abs(path_token.parent))
            elif path_token.suffix in SOURCE_EXTENSIONS:
                print(f"Warning: filelist source not found: {token}", file=sys.stderr)

            i += 1

    result.sources = _dedupe(result.sources)
    result.include_dirs = _dedupe(result.include_dirs)
    result.defines = _dedupe(result.defines)
    return result


def merge_filelists(*filelists: FileList) -> FileList:
    merged = FileList()
    for fl in filelists:
        merged.sources.extend(fl.sources)
        merged.include_dirs.extend(fl.include_dirs)
        merged.defines.extend(fl.defines)
    merged.sources = _dedupe(merged.sources)
    merged.include_dirs = _dedupe(merged.include_dirs)
    merged.defines = _dedupe(merged.defines)
    return merged


def filter_filelist(filelist: FileList, excludes: list[str], root: Path) -> FileList:
    if not excludes:
        return filelist
    return FileList(
        sources=[s for s in filelist.sources if not is_excluded(s, excludes, root)],
        include_dirs=[d for d in filelist.include_dirs if not is_excluded(d, excludes, root)],
        defines=list(filelist.defines),
    )


def format_filelist_path(path: str, root: Path, mode: str, prefix: str) -> str:
    path = Path(path).resolve()
    root = root.resolve()
    if mode == 'absolute':
        return path.as_posix()

    rel = Path(os.path.relpath(path, root)).as_posix()
    if mode == 'prefix':
        base = (prefix or '${PROJPATH}').rstrip('/\\')
        return f"{base}/{rel}" if rel != '.' else base
    return rel


def render_filelist(filelist: FileList, root: Path, path_mode: str, prefix: str) -> str:
    lines = [
        "# Generated by rtlscanner tree",
        f"# sources: {len(filelist.sources)}",
        f"# include_dirs: {len(filelist.include_dirs)}",
        "",
    ]
    for inc in sorted(filelist.include_dirs):
        lines.append(f"+incdir+{format_filelist_path(inc, root, path_mode, prefix)}")
    if filelist.include_dirs and (filelist.defines or filelist.sources):
        lines.append("")
    for define in filelist.defines:
        lines.append(f"+define+{define}")
    if filelist.defines and filelist.sources:
        lines.append("")
    for source in filelist.sources:
        lines.append(format_filelist_path(source, root, path_mode, prefix))
    return "\n".join(lines) + "\n"


def write_filelist_file(
    path: str,
    filelist: FileList,
    root: Path,
    path_mode: str,
    prefix: str,
):
    text = render_filelist(filelist, root, path_mode, prefix)
    if path == '-':
        print(text, end='')
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)


def sv_string_literal(text: str) -> str:
    return text.replace('\\', '/').replace('"', '\\"')


def define_to_directive(define: str) -> str:
    if '=' in define:
        name, value = define.split('=', 1)
        return f"`define {name} {value}"
    return f"`define {define}"




# ── Compilation Builder ──────────────────────────────────────────────
class CompilationResult:
    """Wraps a pyslang ``Compilation`` together with the objects whose
    C++ memory it references (SourceManager, SyntaxTree).  Without this
    the garbage collector may free the buffers while the Compilation
    still points at them."""
    __slots__ = ('comp', '_keep_alive')

    def __init__(self, comp, *refs):
        self.comp = comp
        self._keep_alive = refs

    def __getattr__(self, name):
        # Guard the slot names: if this instance is ever created without
        # __init__ running (copy/deepcopy/pickle), self.comp is unset and a
        # naive delegation would recurse __getattr__('comp') forever.  Raising
        # AttributeError here keeps the failure a normal one.
        if name in ('comp', '_keep_alive'):
            raise AttributeError(name)
        return getattr(self.comp, name)


# ── Compilation diagnostics ──────────────────────────────────────────
# pyslang severity -> agent-schema severity string.  Matches rtl_lint's
# _SEVERITY_NAME, but Ignored (and anything unmapped) is dropped because the
# shared JSON schema only allows error/warning/note/info.
_DIAG_SEVERITY = {
    pyslang.DiagnosticSeverity.Fatal:   "error",
    pyslang.DiagnosticSeverity.Error:   "error",
    pyslang.DiagnosticSeverity.Warning: "warning",
    pyslang.DiagnosticSeverity.Note:    "note",
}


@dataclass
class CompileDiag:
    """One formatted compilation diagnostic.

    Unlike the old ``str(d)`` on a raw pyslang ``Diagnostic`` (which yields a
    useless ``<...Diagnostic object at 0x...>`` repr), these carry the real
    severity, source location, and human-readable message -- formatted through a
    ``DiagnosticEngine`` exactly like ``lint`` does.
    """
    severity: str
    file: str
    line: int
    col: int
    message: str

    def __str__(self) -> str:
        loc = self.file or "<unknown>"
        if self.line:
            loc += f":{self.line}"
            if self.col:
                loc += f":{self.col}"
        return f"{loc}: {self.severity}: {self.message}"


def rel_path(name: str, root=None) -> str:
    """Render *name* relative to *root* for readable diagnostics.

    Mirrors rtl_lint.LintRunner._rel / rtl_xref._format_file so the same compile
    error prints a byte-identical path across commands.  Far-away files (outside
    *root*) keep an absolute path rather than a noisy ``../../..`` chain.
    """
    if not name:
        return name
    try:
        p = Path(name).resolve()
        if root is None:
            return p.as_posix()
        rel = os.path.relpath(p, Path(root).resolve())
        if rel == ".":
            return "."
        if rel.startswith(os.pardir + os.sep) or rel == os.pardir:
            return p.as_posix()
        return "./" + Path(rel).as_posix()
    except Exception:
        return name


def _collect_diagnostics(comp, source_manager, root=None):
    """Return formatted, structured diagnostics for an elaborated compilation.

    Uses a ``DiagnosticEngine`` (honoring inline ``pragma diagnostic`` waivers)
    so severities and messages match what ``lint`` reports.  ``Ignored``
    diagnostics are skipped.
    """
    out = []
    try:
        eng = pyslang.DiagnosticEngine(source_manager)
        try:
            eng.setMappingsFromPragmas()
        except Exception:
            pass
        for d in comp.getAllDiagnostics():
            loc = d.location
            severity = _DIAG_SEVERITY.get(eng.getSeverity(d.code, loc))
            if severity is None:
                continue  # Ignored / unmapped -> not in the schema enum
            try:
                message = eng.formatMessage(d)
            except Exception:
                message = safe_str(d.code, "")
            try:
                fn = rel_path(safe_str(source_manager.getFileName(loc), ""), root)
                ln = int(source_manager.getLineNumber(loc))
                col = int(source_manager.getColumnNumber(loc))
            except Exception:
                fn, ln, col = "", 0, 0
            out.append(CompileDiag(severity, fn, ln, col, message))
    except Exception:
        pass
    return out


def _add_source_trees(comp, source_manager, files, defines, single_unit):
    """Add each source file to *comp* and return the parsed SyntaxTrees.

    Default (per-file): every file is its OWN compilation unit, the way slang's
    driver (and VCS/Verilator) treat a file list -- a `define or other
    $unit-scoped declaration in one file does NOT leak into the next, so the
    linter surfaces real "unknown macro" / undeclared-identifier bugs instead of
    masking them.  Command-line +define+ macros are global predefines, so the
    define directives are prepended to every per-file unit.  The real file is
    pulled in via `include (not by prepending its text) so slang's reported
    file/line points at the true source location.

    ``single_unit=True`` restores the legacy (v0.1.0 / slang ``--single-unit``)
    behavior: the whole file list becomes one compilation unit, so $unit-scoped
    typedefs/macros declared in a leading file are shared with the rest.
    """
    define_lines = [define_to_directive(d) for d in (defines or [])]
    trees = []
    if single_unit:
        preamble = define_lines + [
            f'`include "{sv_string_literal(_norm_abs(f))}"' for f in files]
        try:
            tree = SyntaxTree.fromText('\n'.join(preamble) + '\n', source_manager)
            comp.addSyntaxTree(tree)
            trees.append(tree)
        except Exception as e:
            print(f"Warning: parse error: {e}", file=sys.stderr)
        return trees

    define_prefix = ''.join(line + '\n' for line in define_lines)
    for f in files:
        unit = f'{define_prefix}`include "{sv_string_literal(_norm_abs(f))}"\n'
        try:
            tree = SyntaxTree.fromText(unit, source_manager)
            comp.addSyntaxTree(tree)
            trees.append(tree)
        except Exception as e:
            print(f"Warning: parse error in {f}: {e}", file=sys.stderr)
    return trees


def build_compilation(files, include_dirs=None, defines=None,
                      collect_diagnostics=True, single_unit=False, root=None):
    """
    Create a pyslang Compilation from source files.

    Supports include directories and ``+define+`` macros just like a
    VCS/Verilator invocation.

    Returns ``(compilation_result, diag_messages)`` where the first
    element is a :class:`CompilationResult` that transparently
    delegates to the underlying ``ast.Compilation`` while preventing
    premature garbage collection.

    The returned compilation is always elaborated, so callers receive a
    walkable design either way.  Gathering diagnostics is a separate concern
    controlled by ``collect_diagnostics``: when True, ``diag_messages`` is a
    list of :class:`CompileDiag` (real severity + location + message, formatted
    through a ``DiagnosticEngine``); when False, the design is elaborated via
    ``getRoot()`` and ``diag_messages`` comes back empty.

    ``single_unit`` selects the compilation-unit model: the default (False)
    compiles each file as its own unit; True merges the whole file list into one
    unit (legacy / slang ``--single-unit``).  ``root``, when given, renders
    diagnostic file paths relative to it.
    """
    comp = ast.Compilation()
    source_manager = pyslang.SourceManager()

    all_include_dirs = list(include_dirs or [])
    all_include_dirs.extend(str(Path(f).resolve().parent) for f in files)
    for inc in _dedupe(all_include_dirs):
        try:
            source_manager.addUserDirectories(inc)
        except Exception as e:
            print(f"Warning: could not add include dir {inc}: {e}", file=sys.stderr)

    trees = _add_source_trees(comp, source_manager, files, defines, single_unit)

    diag_messages = []
    if collect_diagnostics:
        # getAllDiagnostics() both elaborates the design and yields its
        # diagnostics; format each (severity + location + message) so callers
        # surface real text instead of a raw "<Diagnostic object at 0x...>".
        diag_messages = _collect_diagnostics(comp, source_manager, root)
    else:
        # Elaborate the design without gathering/formatting diagnostics;
        # getRoot() yields the same elaborated AST as getAllDiagnostics().
        try:
            comp.getRoot()
        except Exception:
            pass

    return CompilationResult(comp, source_manager, *trees), diag_messages


# ── Safe Symbol Access ───────────────────────────────────────────────
def safe_str(val, default=""):
    """Get a string from a pyslang object, returning *default* on decode errors.

    The ``fromText`` + include-directive compilation path can produce
    phantom symbols whose names contain raw bytes that are not valid
    UTF-8.  This helper catches the resulting ``UnicodeDecodeError``
    so callers never crash on garbled names.
    """
    try:
        return str(val)
    except Exception:
        return default

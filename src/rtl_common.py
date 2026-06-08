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
) -> FileList:
    """Parse a VCS-style .f file into normalized sources, dirs, and defines."""
    root = root.resolve()
    seen = seen or set()
    filelist_path = resolve_filelist_path(path, root, root, prefix).resolve()
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
                    nested = parse_filelist(tokens[i + 1], root, prefix, seen)
                    result.include_dirs.extend(nested.include_dirs)
                    result.defines.extend(nested.defines)
                    result.sources.extend(nested.sources)
                    i += 2
                    continue
            elif token.startswith('-f') and len(token) > 2:
                nested = parse_filelist(token[2:], root, prefix, seen)
                result.include_dirs.extend(nested.include_dirs)
                result.defines.extend(nested.defines)
                result.sources.extend(nested.sources)
                i += 1
                continue
            elif token.startswith('+incdir+'):
                inc = token[len('+incdir+'):]
                if inc:
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
    if mode == 'abs':
        return path.as_posix()

    rel = Path(os.path.relpath(path, root)).as_posix()
    if mode == 'prefix':
        base = (prefix or '${PROJPATH}').rstrip('/\\')
        return f"{base}/{rel}" if rel != '.' else base
    return rel


def render_filelist(filelist: FileList, root: Path, path_mode: str, prefix: str) -> str:
    lines = [
        "# Generated by rtl_tree.py",
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
        return getattr(self.comp, name)


def build_compilation(files, include_dirs=None, defines=None):
    """
    Create a pyslang Compilation from source files.

    Supports include directories and ``+define+`` macros just like a
    VCS/Verilator invocation.

    Returns ``(compilation_result, diag_messages)`` where the first
    element is a :class:`CompilationResult` that transparently
    delegates to the underlying ``ast.Compilation`` while preventing
    premature garbage collection.
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

    preamble = []
    for define in defines or []:
        preamble.append(define_to_directive(define))
    for f in files:
        preamble.append(f'`include "{sv_string_literal(_norm_abs(f))}"')

    tree = None
    try:
        tree = SyntaxTree.fromText('\n'.join(preamble) + '\n', source_manager)
        comp.addSyntaxTree(tree)
    except Exception as e:
        print(f"Warning: parse error: {e}", file=sys.stderr)

    diag_messages = []
    try:
        for d in comp.getAllDiagnostics():
            diag_messages.append(str(d))
    except Exception:
        pass

    return CompilationResult(comp, source_manager, tree), diag_messages


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

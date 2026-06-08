"""
Small pyslang query helpers shared by RTLScanner tools.

The pyslang Python API exposes rich elaborated symbols and analysis objects,
but many common tasks need defensive access because some attributes only exist
for specific symbol / expression kinds.  Keep those probes here so individual
tools can stay focused on their own reporting logic.
"""

from __future__ import annotations

try:
    import pyslang.ast as ast
except ImportError:  # rtl_common will print the user-facing dependency error.
    ast = None

from rtl_common import safe_str


def symbol_key(sym) -> str:
    """Return a stable identity key for an elaborated symbol."""
    try:
        hp = safe_str(sym.hierarchicalPath, "")
        if hp:
            return hp
    except Exception:
        pass
    kind = safe_str(getattr(sym, 'kind', ''), '')
    name = safe_str(getattr(sym, 'name', ''), '')
    return f"{kind}:{name}"


def same_symbol(a, b) -> bool:
    return symbol_key(a) == symbol_key(b)


def expr_refs_symbol(expr, symbol) -> bool:
    """Return True when an analyzed expression/timing control references symbol."""
    hit = []

    def visit(node):
        if hasattr(node, 'symbol') and same_symbol(node.symbol, symbol):
            hit.append(True)

    try:
        expr.visit(f=visit)
    except Exception:
        pass
    return bool(hit)


def expr_symbols(expr) -> list:
    """Return unique symbols referenced by an analyzed expression."""
    out = []
    seen = set()

    def visit(node):
        try:
            sym = node.symbol
        except Exception:
            return
        key = symbol_key(sym)
        if key not in seen:
            seen.add(key)
            out.append(sym)

    try:
        expr.visit(f=visit)
    except Exception:
        pass
    return out


def iter_instances(root):
    """Yield elaborated instances under root in hierarchy traversal order."""
    if ast is None:
        return
    items = []

    def collect(sym):
        items.append(sym)

    try:
        root.visit(lookup_table={ast.SymbolKind.Instance: collect})
    except Exception:
        return

    seen = set()
    for inst in items:
        key = symbol_key(inst)
        if key in seen:
            continue
        seen.add(key)
        yield inst


def scope_visit(body, kinds):
    """Visit body with a lookup table, skipping child instances by default."""
    if ast is None:
        return
    table = dict(kinds)
    table.setdefault(ast.SymbolKind.Instance, lambda _: ast.VisitAction.Skip)
    body.visit(lookup_table=table)


def resolve_scope(root, scope_path):
    """Resolve a hierarchical scope path to an InstanceSymbol."""
    if ast is not None:
        try:
            sym = root.lookupName(scope_path)
            if sym is not None and sym.kind == ast.SymbolKind.Instance:
                return sym
        except Exception:
            pass

    parts = scope_path.split('.')
    current = None
    for top in root.topInstances:
        try:
            if top.name == parts[0] or top.body.name == parts[0]:
                current = top
                break
        except Exception:
            continue
    if current is None:
        return None
    for part in parts[1:]:
        try:
            found = current.body.find(part)
        except Exception:
            return None
        if found is None or found.kind != ast.SymbolKind.Instance:
            return None
        current = found
    return current


def find_signal(body, name):
    """Find a local net / variable signal by name in an instance body."""
    if ast is None:
        return None
    try:
        sym = body.find(name)
    except Exception:
        return None
    if sym and sym.kind in (ast.SymbolKind.Net, ast.SymbolKind.Variable):
        return sym
    return None


def analyzed_procedures(manager, body):
    """Return analyzed procedures for a body, or an empty list."""
    try:
        analyzed_scope = manager.getAnalyzedScope(body)
        if analyzed_scope is not None:
            return list(analyzed_scope.procedures or [])
    except Exception:
        pass
    return []


def procedure_label(proc) -> str:
    """Return a user-facing label for an analyzed procedural block."""
    try:
        pk = safe_str(proc.analyzedSymbol.procedureKind, "")
    except Exception:
        return "procedural block"
    labels = {
        "AlwaysFF": "always_ff",
        "AlwaysComb": "always_comb",
        "AlwaysLatch": "always_latch",
        "Always": "always",
        "Initial": "initial",
        "Final": "final",
    }
    for key, label in labels.items():
        if key in pk:
            return label
    return "procedural block"


def procedure_reads_symbol(proc, symbol) -> bool:
    """Return True when proc reads symbol as data or timing control."""
    try:
        if any(same_symbol(rr.symbol, symbol) for rr in (proc.readSet or [])):
            return True
    except Exception:
        pass

    # Clocks are used in timing controls rather than expression read sets.
    for tc in getattr(proc, 'timingControls', []) or []:
        timing = getattr(tc, 'timing', None)
        if timing is not None and expr_refs_symbol(timing, symbol):
            return True
    return False

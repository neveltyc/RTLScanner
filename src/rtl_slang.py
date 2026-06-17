"""
Small pyslang query helpers shared by RTLScanner tools.

The pyslang Python API exposes rich elaborated symbols and analysis objects,
but many common tasks need defensive access because some attributes only exist
for specific symbol / expression kinds.  Keep those probes here so individual
tools can stay focused on their own reporting logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

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


# ── Bit-range (longest-static-prefix) extraction ─────────────────────
#
# slang-netlist resolves dataflow to the bit level: every dependency carries
# the bit range it touches.  pyslang exposes the building blocks (selection
# expressions + foldable constants) but no LSPUtilities binding, so we compute
# the "longest static prefix" symbol and its constant bit range here.  These
# live alongside expr_symbols (which stays symbol-only); callers that want bits
# opt in, everything else is unaffected.

# ── Constant evaluation (with optional loop-variable binding) ────────
#
# slang pre-folds genuinely-constant expressions into ``expr.constant`` (static
# select indices, parameter values) — all the bit-range extraction below needs
# by default.  Constant-condition pruning and loop unrolling need more: an
# ``if (EN)`` predicate or a loop bound ``i < N`` is not pre-folded, and an index
# ``p[i]`` only becomes constant once the loop variable ``i`` is bound to an
# iteration value.  An EvalContext (anchored at the procedure, carrying
# ``createLocal`` loop-var bindings) provides exactly that.  Every helper here
# no-ops to "non-constant" when given no context, so the default path is
# unchanged.

def make_eval_context(symbol):
    """A constant-evaluation context anchored at `symbol`, or None."""
    if ast is None or symbol is None:
        return None
    try:
        return ast.EvalContext(symbol)
    except Exception:
        return None


def _cv_to_int(cv):
    """ConstantValue -> Python int, or None when not a determinate integer."""
    if cv is None:
        return None
    try:
        if not bool(cv) or cv.hasUnknown():
            return None
    except Exception:
        return None
    try:
        return int(cv.convertToInt().value)
    except Exception:
        pass
    try:
        return int(cv.value)
    except Exception:
        return None


def try_eval_bool(expr, eval_ctx):
    """True / False / None(=non-constant) for a condition expression."""
    if eval_ctx is None or expr is None:
        return None
    try:
        cv = expr.eval(eval_ctx)
        if not bool(cv) or cv.hasUnknown():
            return None
        if cv.isTrue():
            return True
        if cv.isFalse():
            return False
    except Exception:
        return None
    return None


def try_eval_int(expr, eval_ctx):
    """Python int, or None when the expression isn't a determinate constant."""
    if eval_ctx is None or expr is None:
        return None
    try:
        return _cv_to_int(expr.eval(eval_ctx))
    except Exception:
        return None


def _const_int(expr, eval_ctx=None):
    """Fold an expression to a Python int, or None when not statically known.

    With an ``eval_ctx`` (e.g. inside an unrolled loop, where the loop variable
    is bound) an otherwise dynamic-looking index such as ``p[i]`` folds to a
    concrete value; without one, only slang's pre-computed ``expr.constant`` is
    consulted — byte-for-byte the original behaviour.
    """
    if expr is None:
        return None
    if eval_ctx is not None:
        v = try_eval_int(expr, eval_ctx)
        if v is not None:
            return v
    try:
        c = expr.constant
    except Exception:
        c = None
    if c is None:
        return None
    for attr in ("integer", "value"):
        v = getattr(c, attr, None)
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass
    try:
        return int(c)
    except Exception:
        return None


def full_bounds(sym):
    """The whole-signal bit range (0, width-1) of a value symbol, or None."""
    try:
        w = int(sym.type.bitWidth)
        if w > 0:
            return (0, w - 1)
    except Exception:
        pass
    return None


def _range_select_bounds(expr, base_lo, eval_ctx=None):
    """Constant (lo, hi) of a RangeSelectExpression in base coordinates, or
    None when a governing index is non-constant (dynamic part-select)."""
    kind = getattr(getattr(expr, "selectionKind", None), "name", "")
    left = _const_int(getattr(expr, "left", None), eval_ctx)
    if kind == "Simple":
        right = _const_int(getattr(expr, "right", None), eval_ctx)
        if left is None or right is None:
            return None
        lo, hi = (right, left) if left >= right else (left, right)
        return (base_lo + lo, base_lo + hi)
    # Indexed part-select a[base +: w] / a[base -: w]: width is constant, the
    # base may be dynamic (then the covered range is unknown -> conservative).
    width = _const_int(getattr(expr, "right", None), eval_ctx)
    if left is None or width is None or width <= 0:
        return None
    if kind == "IndexedUp":
        lo, hi = left, left + width - 1
    elif kind == "IndexedDown":
        lo, hi = left - width + 1, left
    else:
        return None
    return (base_lo + lo, base_lo + hi)


def lsp_bounds(expr, eval_ctx=None):
    """Longest static prefix of a value access: (symbol, (lo, hi) | None).

    Walks down a (possibly nested) bit/part-select chain to the named value at
    its base and returns the *constant* bit range selected, in that symbol's
    own 0-based coordinates.  A dynamic index or unsupported construct yields
    (symbol, None) — read conservatively as "the whole signal".  (None, None)
    when no single named value underlies the expression (e.g. ``a + b``, a
    concatenation).  An ``eval_ctx`` folds loop-variable indices (``p[i]``) to
    concrete bits during unrolling; without one only static selects resolve.
    """
    if ast is None or expr is None:
        return (None, None)
    tn = type(expr).__name__
    if tn == "ConversionExpression":
        return lsp_bounds(getattr(expr, "operand", None), eval_ctx)
    if tn in ("NamedValueExpression", "HierarchicalValueExpression"):
        sym = getattr(expr, "symbol", None)
        return (sym, full_bounds(sym)) if sym is not None else (None, None)
    if tn == "ElementSelectExpression":
        sym, base = lsp_bounds(getattr(expr, "value", None), eval_ctx)
        if sym is None:
            return (None, None)
        if base is None:
            return (sym, None)
        idx = _const_int(getattr(expr, "selector", None), eval_ctx)
        if idx is None:
            return (sym, None)
        return (sym, (base[0] + idx, base[0] + idx))
    if tn == "RangeSelectExpression":
        sym, base = lsp_bounds(getattr(expr, "value", None), eval_ctx)
        if sym is None:
            return (None, None)
        if base is None:
            return (sym, None)
        return (sym, _range_select_bounds(expr, base[0], eval_ctx))
    return (None, None)


def expr_reads_with_bounds(expr, eval_ctx=None):
    """Like expr_symbols, but pairs each read symbol with the constant bit
    range it is read over: ``[(symbol, (lo, hi) | None), ...]``.

    The symbol set (and order) matches expr_symbols exactly — so a caller that
    swaps this in keeps identical connectivity — while bits are filled in where
    a static select makes them knowable.  A symbol read over two different
    static ranges, or via a dynamic index / arithmetic, is reported with None
    (whole signal): the conservative answer.  An ``eval_ctx`` folds loop-variable
    indices to concrete bits during unrolling.
    """
    precise = {}   # symbol_key -> (lo, hi); a conflict downgrades to None

    def capture(sym, bounds):
        if sym is None or bounds is None:
            return
        k = symbol_key(sym)
        if k in precise and precise[k] != bounds:
            precise[k] = None
        else:
            precise.setdefault(k, bounds)

    def walk(e):
        if e is None:
            return
        tn = type(e).__name__
        if tn in ("NamedValueExpression", "HierarchicalValueExpression",
                  "ElementSelectExpression", "RangeSelectExpression"):
            capture(*lsp_bounds(e, eval_ctx))
            # A dynamic index / part-select base is itself a read.
            if tn == "ElementSelectExpression":
                walk(getattr(e, "selector", None))
            elif tn == "RangeSelectExpression":
                walk(getattr(e, "left", None))
                walk(getattr(e, "right", None))
            return
        if tn == "ConversionExpression":
            walk(getattr(e, "operand", None))
            return
        if tn == "ConcatenationExpression":
            for op in (getattr(e, "operands", None) or []):
                walk(op)
            return
        if tn == "ReplicationExpression":
            walk(getattr(e, "concat", None))
            return
        if tn == "BinaryExpression":
            walk(getattr(e, "left", None))
            walk(getattr(e, "right", None))
            return
        if tn == "UnaryExpression":
            walk(getattr(e, "operand", None))
            return
        if tn == "ConditionalExpression":
            walk(getattr(e, "left", None))
            walk(getattr(e, "right", None))
            return
        # Unknown node kind: its symbols are still reported (as whole) by the
        # authoritative expr_symbols pass below — only precision is lost here.

    try:
        walk(expr)
    except Exception:
        precise = {}

    return [(sym, precise.get(symbol_key(sym))) for sym in expr_symbols(expr)]


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


_NON_DATA_KINDS = frozenset(
    k for k in (
        getattr(ast.SymbolKind, name, None)
        for name in ('Parameter', 'TypeParameter', 'Genvar', 'EnumValue',
                     'Specparam')
    ) if k is not None
) if ast is not None else frozenset()


def is_data_symbol(sym) -> bool:
    """True when sym can carry runtime dataflow (not a param/genvar/enum)."""
    if ast is None or sym is None:
        return False
    return getattr(sym, 'kind', None) not in _NON_DATA_KINDS


# ── Canonical-body handling ──────────────────────────────────────────
#
# slang's AnalysisManager deduplicates identical instance bodies (same module,
# same parameters) and records analysis results — drivers, read sets, analyzed
# procedures — only against one *canonical* body per equivalence class.
# Dedup is subtree-level: once an instance is deduplicated, analysis never
# descends into it, so its children carry neither results nor their own
# canonicalBody link — their analysis twin lives under the *ancestor's*
# canonical body.  Querying a deduplicated instance through its own body
# silently returns nothing, which reads as "undriven" / "no procedures".
# Every analysis-manager consumer must therefore query through the canonical
# twin and remap reported hierarchical paths back into the queried instance.

@dataclass
class CanonicalView:
    """Analysis-capable view of one elaborated instance."""
    body: object                    # body that carries analysis results
    remap: Callable[[str], str]     # canonical-namespace path -> inst namespace
    contains: Callable[[str], bool] # path lies inside the canonical subtree
    deduped: bool                   # True when inst is not its own canonical


def analysis_instance(root, inst):
    """Follow canonicalBody links of inst and its ancestors to the instance
    whose body actually carries analysis results."""
    body = getattr(inst, 'body', None)
    cur_path = safe_str(getattr(body, 'hierarchicalPath', ''), '')
    if not cur_path:
        return inst
    cur = inst
    for _ in range(64):  # guard against pathological canonical chains
        parts = cur_path.split('.')
        jumped = False
        for i in range(len(parts), 0, -1):
            prefix = '.'.join(parts[:i])
            anc = cur if i == len(parts) else resolve_scope(root, prefix)
            canon = getattr(anc, 'canonicalBody', None) if anc is not None else None
            if canon is None:
                continue
            canon_prefix = safe_str(getattr(canon, 'hierarchicalPath', ''), '')
            if not canon_prefix or canon_prefix == prefix:
                continue
            nxt = resolve_scope(root, canon_prefix + cur_path[len(prefix):])
            if nxt is None:
                continue
            cur_path = safe_str(
                getattr(getattr(nxt, 'body', None), 'hierarchicalPath', ''), '')
            cur = nxt
            jumped = True
            break
        if not jumped or not cur_path:
            break
    return cur


def canonical_view(root, inst) -> CanonicalView:
    """Return the CanonicalView for analysis-manager queries on inst."""
    identity = lambda p: p  # noqa: E731
    body = getattr(inst, 'body', None)
    inst_prefix = safe_str(getattr(body, 'hierarchicalPath', ''), '')

    twin = analysis_instance(root, inst)
    canon_body = getattr(twin, 'body', None) if twin is not None else None
    canon_prefix = safe_str(getattr(canon_body, 'hierarchicalPath', ''), '')

    if (canon_body is None or not canon_prefix or not inst_prefix
            or canon_prefix == inst_prefix):
        prefix = inst_prefix
        return CanonicalView(
            body=body, remap=identity,
            contains=lambda p: bool(prefix) and (p == prefix or p.startswith(prefix + '.')),
            deduped=False)

    def contains(path: str) -> bool:
        return path == canon_prefix or path.startswith(canon_prefix + '.')

    def remap(path: str) -> str:
        if contains(path):
            return inst_prefix + path[len(canon_prefix):]
        return path

    return CanonicalView(body=canon_body, remap=remap, contains=contains,
                         deduped=True)


def canonical_twin(view: CanonicalView, symbol):
    """Return the canonical body's symbol matching `symbol`, or `symbol`.

    Analysis sets (readSet, drivers) reference canonical-body symbols, so
    membership checks against a symbol from a deduplicated body must use its
    canonical twin.
    """
    if not view.deduped or symbol is None or view.body is None:
        return symbol
    name = safe_str(getattr(symbol, 'name', ''), '')
    if not name:
        return symbol
    try:
        twin = view.body.find(name)
    except Exception:
        return symbol
    if twin is not None and getattr(twin, 'kind', None) == getattr(symbol, 'kind', None):
        return twin
    return symbol


# ── Name candidates for did-you-mean errors ──────────────────────────

def signal_names(body) -> list:
    """Names of ports and nets/variables findable in an instance body."""
    out, seen = [], set()

    def add(name):
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    if ast is None or body is None:
        return out
    try:
        for member in body:
            if getattr(member, 'kind', None) in (ast.SymbolKind.Net,
                                                 ast.SymbolKind.Variable):
                add(safe_str(member.name, ''))
    except Exception:
        pass
    return out


def child_scope_names(inst) -> list:
    """Child instance paths relative to inst (e.g. 'u_reg', 'gen_arr[0].u_leaf')."""
    out = []
    body = getattr(inst, 'body', None)
    if body is None:
        return out
    prefix = safe_str(getattr(body, 'hierarchicalPath', ''), '')

    children = []

    def collect(sym):
        children.append(sym)
        return ast.VisitAction.Skip

    scope_visit(body, {ast.SymbolKind.Instance: collect})
    seen = set()
    for child in children:
        path = safe_str(getattr(child, 'hierarchicalPath', ''), '')
        if prefix and path.startswith(prefix + '.'):
            path = path[len(prefix) + 1:]
        elif not path:
            path = safe_str(getattr(child, 'name', ''), '')
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def scope_suggestions(root, scope_path: str) -> dict:
    """Describe why scope_path failed to resolve: deepest valid prefix and
    the child scopes available there."""
    parts = [p for p in (scope_path or '').split('.') if p]
    valid_prefix = ''
    base = None
    for i in range(len(parts) - 1, 0, -1):
        candidate = '.'.join(parts[:i])
        inst = resolve_scope(root, candidate)
        if inst is not None:
            valid_prefix = candidate
            base = inst
            break

    if base is not None:
        children = child_scope_names(base)
        failing = parts[len(valid_prefix.split('.'))] if valid_prefix else ''
    else:
        children = []
        try:
            for top in root.topInstances:
                children.append(safe_str(top.name, '') or
                                safe_str(top.body.name, ''))
        except Exception:
            pass
        failing = parts[0] if parts else ''

    import difflib
    close = difflib.get_close_matches(failing, children, n=5, cutoff=0.5) \
        if failing else []
    return {
        'scope': scope_path,
        'valid_prefix': valid_prefix,
        'failing_component': failing,
        'close_matches': close,
        'children': children[:20],
        'children_truncated': len(children) > 20,
    }


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

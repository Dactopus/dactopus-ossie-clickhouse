"""Turn a semantic query (metrics, dimensions, filters) into one ClickHouse SELECT.

Minimum planner: one root dataset plus many-to-one joins along the one path
to each dataset the query touches. A row filter on a dataset that reaches the
root but that the root does not reach is a semi-join (IN), and so is one on a
dataset that meets the root at a dataset both reach, or meets a fact another
filter names. Metrics that no one
root can answer together are each answered from their own root and merged on
the dimensions (stitching). Anything else is rejected with a message that
names the nearest supported thing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache

from sqlglot import exp
from sqlglot.dialects.clickhouse import ClickHouse

from dactopus_ossie_clickhouse._vendor.ossie import (
    OssieDataset,
    OssieDataType,
    OssieDocument,
    OssieField,
    OssieMetric,
    OssieRelationship,
)
from dactopus_ossie_clickhouse.model import (
    SOURCE_QUALIFIER,
    all_metrics,
    covers_key,
    declared_keys,
    join_problem,
    reads_source,
    suggest,
    time_zone,
)
from dactopus_ossie_clickhouse.translate import (
    outside_spec,
    parse,
    pick_expression,
    rewrite_tree,
    untranslatable,
)


class PlanError(ValueError):
    pass


class _BlendError(PlanError):
    """Planner._refuse_blends. A metric refused so is refused in every question with it,
    which unanswerable_metrics reports."""


# A string compared with a field or metric of these types is read as TIMESTAMP '...'
# is, in the model's zone; ClickHouse reads it in the column's (#58).
_TIMES = (OssieDataType.DATE_TIME, OssieDataType.DATE_TIME_TZ)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)


_QUOTED = r"`(?:[^`]|``)+`|\"(?:[^\"]|\"\")+\""  # non-empty, a doubled quote inside
# A quoted identifier, or unquoted text without a quote, space, `;`, parenthesis
# or dot.
_PART = re.compile(rf"{_QUOTED}|[^\s`\"'();.]+")
_SOURCE = re.compile(rf"(?:{_PART.pattern})(?:\.(?:{_PART.pattern})){{0,2}}")
# Where a space or parenthesis does not mark a query: quoted names and string
# literals (group 1), comments.
_INERT = re.compile(rf"({_QUOTED}|'(?:[^'\\]|\\.|'')*')|--.*|/\*[\s\S]*?\*/")


@dataclass(frozen=True)
class TableInfo:
    """What the executor learned about a dataset's physical source."""

    engine: str
    columns: frozenset[str]
    dedup: bool = False  # ReplacingMergeTree and not overridden: read with FINAL
    dictionary_key: str | None = None  # single-key dictionary: read with dictGetOrNull
    # Column name -> type, as system.columns gives it; empty when not read. Types the NULL
    # of an unmatched row in a deduplicated join (Planner._unmatched).
    types: Mapping[str, str] = field(default_factory=dict, compare=False)

    def type_of(self, column: str) -> str | None:
        """The type of ``column``, its name matched in any case."""
        if column in self.types:
            return self.types[column]
        return next((t for n, t in self.types.items() if n.upper() == column.upper()), None)


class Catalog(dict[str, TableInfo]):
    """What the executor learned from the server: TableInfo by dataset name, and
    ``aggregate``, whether a function outside the spec is an aggregate (None when
    system.functions could not be read: the planner then asks
    _known_clickhouse_aggregate), and ``nullable_tuple``, whether this server makes a
    tuple Nullable in a LEFT JOIN's unmatched rows (Planner._unmatched)."""

    def __init__(
        self,
        tables=(),
        aggregate: Callable[[str], bool] | None = None,
        nullable_tuple: bool = False,
    ):
        super().__init__(tables)
        self.aggregate = aggregate
        self.nullable_tuple = nullable_tuple


def _known_clickhouse_aggregate(name: str) -> bool:
    """Whether SQLGlot's list of ClickHouse aggregates has ``name`` as ClickHouse
    spells it, with any combinators after it (countIf, uniqExactOrNull). For a
    planner without the server's word. The list lacks about half of them
    (medianExact, group_concat, any user-defined one); those stay plain calls."""
    if name in ClickHouse.Parser.AGG_FUNCTIONS:
        return True
    return any(
        len(name) > len(c) and name.endswith(c) and _known_clickhouse_aggregate(name[: -len(c)])
        for c in ClickHouse.Parser.AGG_FUNCTIONS_SUFFIXES
    )


@dataclass(frozen=True)
class Query:
    metrics: tuple[str, ...] = ()  # a dataset's metric as "dataset.metric"
    dimensions: tuple[str, ...] = ()  # "dataset.field"
    # Ossie expressions over dataset.field (WHERE) or over metric names and
    # aggregates (HAVING); window metrics cannot be filtered in one SELECT.
    filters: tuple[str, ...] = ()
    # Metric or dimension names, "name desc" for descending. Empty with metrics
    # and dimensions: first metric descending, so callers that rank rows
    # themselves (agents do, and get it wrong) see the top rows first. Ties
    # are broken by the dimensions, ascending.
    order_by: tuple[str, ...] = ()
    limit: int | None = None


def source_table(source: str) -> exp.Table:
    """Ossie source (`db.schema.table`, `db.table`, `table`) to a ClickHouse table.

    Parts may be quoted with backticks or double quotes; `.db` and `.name` of
    the result are unquoted, and the emitted SQL keeps the quotes."""
    parts = [exp.to_identifier(name, quoted=quoted or None) for name, quoted in _parts(source)]
    # ClickHouse has no schema level: keep database and table, drop the middle.
    return exp.table_(parts[-1], db=parts[0] if len(parts) > 1 else None)


@cache
def _parts(source: str) -> tuple[tuple[str, bool], ...]:
    """Each dot-separated part of a source as (name, quoted).

    A part is one quoted identifier (a doubled quote stands for itself) or
    unquoted text taken as the name it spells, as 0.2.3 did: `t$x`, `t#1`,
    `select`, `x-y`. Whitespace or a parenthesis outside quotes, strings and
    comments marks a query, checked first so a query is reported as one.
    Refused rather than read as some other table: `--` or `/*` outside quotes
    (a comment pasted in), `;` or an unclosed quote, and `\\` anywhere: in a
    quoted name ClickHouse reads it as an escape, which is not decoded here."""
    s = source.strip()
    text = _INERT.sub(lambda m: "_" if m[1] else "", s).strip()
    if not re.search(r"[`\"]", text) and re.search(r"[\s()]", text):
        raise PlanError(f"query sources are not supported yet: {source!r}")
    if "\\" in s or re.search(r"--|/\*", re.sub(_QUOTED, "_", s)) or not _SOURCE.fullmatch(s):
        raise PlanError(f"cannot map source {source!r} to a ClickHouse table")
    return tuple(
        (p[1:-1].replace(p[0] * 2, p[0]), True) if p[0] in '`"' else (p, False)
        for p in _PART.findall(s)
    )


def source_name(source: str) -> str:
    """`db.table` unquoted, as dictGet* takes a dictionary's name."""
    t = source_table(source)
    return f"{t.db}.{t.name}" if t.db else t.name


# Spec aggregates whose value does not change when an input row repeats. Only
# these may read a joined dataset's columns on their own: each row of a
# many-to-one target repeats once per root row that references it.
_REPEAT_SAFE = {
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.ApproxDistinct: "APPROX_COUNT_DISTINCT",
}
_REPEAT_SAFE_NAMES = (
    ", ".join(_REPEAT_SAFE.values()) + ", DISTINCT and ClickHouse's own such as uniq, any, argMax"
)
# The same for ClickHouse aggregates outside the spec, named as ClickHouse
# registers them; those it matches in any case (system.functions) in lower case.
_CLICKHOUSE_REPEAT_SAFE = frozenset({
    "any", "anyLast", "anyRespectNulls", "anyLastRespectNulls", "argMin", "argMax",
    "uniq", "uniqExact", "uniqCombined", "uniqCombined64", "uniqHLL12", "uniqTheta",
    "groupUniqArray", "groupBitAnd", "groupBitOr", "groupBitmap", "singleValueOrNull",
})  # fmt: skip
_CLICKHOUSE_REPEAT_SAFE_ANY_CASE = frozenset({
    "min", "max", "any_value", "min_by", "max_by", "first_value_respect_nulls",
})  # fmt: skip
# Combinators that keep an aggregate repeat-safe, longest first as ClickHouse
# tries them: If drops rows, OrNull and OrDefault change only the value over no
# rows, and Distinct makes any aggregate repeat-safe. No aggregate's own name
# ends in one of them except singleValueOrNull (ClickHouse 26.9).
_KEEPING_COMBINATORS = ("OrDefault", "Distinct", "OrNull", "If")


def _clickhouse_repeat_safe(name: str) -> bool:
    """Whether a ClickHouse aggregate outside the spec, combinators and all
    (uniqExactIf, anyLastOrNull, sumDistinct), ignores repeated input rows."""
    while (
        name not in _CLICKHOUSE_REPEAT_SAFE and name.lower() not in _CLICKHOUSE_REPEAT_SAFE_ANY_CASE
    ):
        suffix = next(
            (c for c in _KEEPING_COMBINATORS if name.endswith(c) and len(name) > len(c)), None
        )
        if suffix is None:
            return False
        if suffix == "Distinct":
            return True
        name = name[: -len(suffix)]
    return True


Inputs = list[tuple[exp.AggFunc, set[str]]]


def _repeat_safe(agg: exp.AggFunc) -> bool:
    # A parametric aggregate, name(p)(args), keeps its arguments in params:
    # MEDIAN(DISTINCT x) is rewritten to one. One outside the spec keeps them in
    # expressions: sumIf(DISTINCT x, c).
    if isinstance(agg, exp.ParameterizedAgg):
        args = agg.args.get("params") or []
    else:
        args = agg.expressions if isinstance(agg, exp.AnonymousAggFunc) else [agg.this]
    if agg.meta.get("outside_spec") and _clickhouse_repeat_safe(agg.name):
        return True
    return isinstance(agg, tuple(_REPEAT_SAFE)) or any(isinstance(a, exp.Distinct) for a in args)


def _aggregate_inputs(tree: exp.Expression) -> Inputs:
    """Each aggregate over joined rows, with the datasets of the columns it reads
    (none for COUNT(*), except in a dataset's metric: its dataset, from meta).

    A column counts for its nearest aggregate only, so SUM(SUM(x)) OVER () reads x
    once, and a FILTER (WHERE ...) column for the aggregate it filters. The function
    of an OVER clause reads grouped rows, not joined ones, and PARTITION BY / ORDER BY
    columns are grouping keys: neither counts."""
    found = {
        id(a): (a, {a.meta["dataset"]} if "dataset" in a.meta else set())
        for a in tree.find_all(exp.AggFunc)
        if not _window_function(a)
    }
    for col in tree.find_all(exp.Column):
        node = col.parent
        while node is not None and id(node) not in found:
            filtered = isinstance(node, exp.Filter) and id(node.this) in found
            node = node.this if filtered else node.parent
        if node is not None:
            found[id(node)][1].add(col.table)
    return list(found.values())


Reads = list[tuple[exp.AggFunc, set[str], set[str]]]


def _aggregate_reads(tree: exp.Expression) -> Reads:
    """Each aggregate with the datasets its arguments read and, apart, the datasets
    its FILTER (WHERE ...) reads. The home of a component comes from the arguments
    only (apache/ossie#653, section 5.2: the FILTER "does not determine the home
    dataset")."""
    found: dict[int, tuple[exp.AggFunc, set[str], set[str]]] = {
        id(a): (a, set(), set()) for a in tree.find_all(exp.AggFunc) if not _window_function(a)
    }
    for col in tree.find_all(exp.Column):
        node, in_filter = col.parent, False
        while node is not None and id(node) not in found:
            if isinstance(node, exp.Filter) and id(node.this) in found:
                node, in_filter = node.this, True
            else:
                node = node.parent
        if node is not None:
            found[id(node)][2 if in_filter else 1].add(col.table)
    return list(found.values())


def _mixed(reads: Reads) -> tuple[exp.AggFunc, exp.AggFunc] | None:
    """An aggregate that reads several datasets, or filters on another, and another
    aggregate whose arguments read a dataset it does not read. Section 5.2 rejects
    the first (deferred), and one join would count the second's dataset over
    the first's rows."""
    for agg, args, filtered in reads:
        if len(args) > 1 or (args and not filtered <= args):
            for other, theirs, _ in reads:
                if other is not agg and theirs and not theirs <= args:
                    return agg, other
    return None


def _window_function(agg: exp.AggFunc) -> bool:
    parent = agg.parent
    while isinstance(parent, exp.RespectNulls | exp.IgnoreNulls | exp.Filter):
        parent = parent.parent
    return isinstance(parent, exp.Window)


def _component_node(agg: exp.AggFunc) -> exp.Expression:
    """The aggregate with the wrappers that belong to it: FILTER (WHERE ...),
    RESPECT NULLS, IGNORE NULLS."""
    node: exp.Expression = agg
    while isinstance(node.parent, exp.Filter | exp.RespectNulls | exp.IgnoreNulls) and (
        node.parent.this is node
    ):
        node = node.parent
    return node


def _blend(
    tree: exp.Expression, owner: dict[str, int], row_filter: bool
) -> tuple[int, exp.Expression, bool] | None:
    """A part of ``tree`` that Planner._dedup would carry from a subtree and that sits in
    a larger expression on joined rows where two copies of a key that differ in the part
    can give the same result, so the root row would count once for each: the subtree
    (index into ``owner``'s values), the expression to name and whether it is a condition.

    A condition (a row filter, a FILTER) is safe when the part is one of its conjuncts:
    only the copies that pass it remain, and a carried condition has one value that
    passes. An aggregate's argument is safe when it holds one part and gives each value
    of the part its own result, or results that cannot change the aggregate (_injective).
    Anything else counts a root row once for each copy where Layer 2 counts it once
    (docs/decisions/key-stored-twice.md); #653 rejects such expressions
    anyway (E_CROSS_DATASET_FILTER, E_AMBIGUOUS_MEASURE_GRAIN)."""
    parts: dict[int, int] = {}  # id of an aggregate -> carried parts in its argument

    def check(node: exp.Expression, i: int) -> tuple[int, exp.Expression, bool] | None:
        steps: list[tuple[exp.Expression, exp.Expression]] = []  # (parent, child), upwards
        agg = None
        p = node
        while (parent := p.parent) is not None and not isinstance(parent, exp.Where):
            if isinstance(parent, exp.AggFunc) and not _window_function(parent):
                agg = parent
                break
            if not isinstance(parent, exp.Paren):
                steps.append((parent, p))
            p = parent
        if isinstance(parent, exp.Where) and isinstance(parent.parent, exp.Filter):
            filtered = parent.parent.this.find(exp.AggFunc)
            if filtered is not None and _repeat_safe(filtered):
                return None  # MAX(x) FILTER (...): a copy repeats nothing it returns
        if agg is not None and _repeat_safe(agg):
            return None
        if agg is None:
            conjunct = next((o for o, _ in reversed(steps) if not isinstance(o, exp.And)), None)
            return None if conjunct is None else (i, conjunct, True)
        parts[id(agg)] = parts.get(id(agg), 0) + 1
        if parts[id(agg)] > 1 or not _injective(steps, isinstance(agg, exp.Sum)):
            return i, _component_node(agg), False
        return None

    def visit(node: exp.Expression, joined: bool) -> tuple[int, exp.Expression, bool] | None:
        if isinstance(node, exp.Subquery | exp.Select):
            return None
        if joined and isinstance(node, exp.Condition):
            own = {owner.get(c.table.upper()) for c in node.find_all(exp.Column)}
            if len(own) == 1 and None not in own and not node.find(exp.AggFunc, exp.Window):
                return check(node, next(iter(own)))
        if isinstance(node, exp.Where) or (
            isinstance(node, exp.AggFunc) and not _window_function(node)
        ):
            joined = True
        for child in node.iter_expressions():
            if found := visit(child, joined):
                return found
        return None

    return visit(tree, row_filter)


def _regroup(tree: exp.Expression, owner: dict[str, int]) -> exp.Expression:
    """``tree`` with the factors of a product, and the terms of a sum, that read only one
    subtree gathered in one parenthesized operand, inside aggregates: ``lines.qty *
    products.price * (1 - products.discount)`` becomes ``lines.qty * (products.price * (1 -
    products.discount))``, so Planner._dedup carries the product of the two as one value
    and two copies of a key that give the same product collapse, as in Layer 2's DISTINCT
    (_blend refuses two parts). Products and sums are reordered only; ``/`` is left alone."""

    def side(n: exp.Expression) -> int | None:
        own = {owner.get(c.table.upper()) for c in n.find_all(exp.Column)}
        if len(own) == 1 and None not in own and not n.find(exp.AggFunc, exp.Window, exp.Select):
            return next(iter(own))
        return None

    def bare(n: exp.Expression) -> exp.Expression:
        while isinstance(n, exp.Paren):
            n = n.this
        return n

    def wrap(n: exp.Expression) -> exp.Expression:
        return exp.Paren(this=n) if isinstance(n, exp.Binary | exp.Neg) else n

    def factors(n: exp.Expression) -> list[exp.Expression]:
        b = bare(n)
        return factors(b.this) + factors(b.expression) if isinstance(b, exp.Mul) else [n]

    def terms(n: exp.Expression, neg: bool = False) -> list[tuple[bool, exp.Expression]]:
        b = bare(n)
        if isinstance(b, exp.Add | exp.Sub):
            return terms(b.this, neg) + terms(b.expression, neg ^ isinstance(b, exp.Sub))
        return [(neg, n)]

    def in_aggregate(n: exp.Expression) -> bool:
        p = n.parent
        while p is not None:
            if isinstance(p, exp.AggFunc) and not _window_function(p):
                return True
            p = p.parent
        return False

    def gather(items: list, key) -> tuple[list, dict[int, list]] | None:
        groups: dict[int, list] = {}
        for item in items:
            i = side(key(item))
            if i is not None:
                groups.setdefault(i, []).append(item)
        if not any(len(g) > 1 for g in groups.values()) or len(groups) == len(items) == 1:
            return None
        many = {i for i, g in groups.items() if len(g) > 1}
        return [it for it in items if side(key(it)) not in many], {i: groups[i] for i in many}

    def product(items: list[exp.Expression]) -> exp.Expression:
        out = wrap(items[0].copy())
        for f in items[1:]:
            out = exp.Mul(this=wrap(out), expression=wrap(f.copy()))
        return out

    def total(items: list[tuple[bool, exp.Expression]]) -> exp.Expression:
        neg, first = items[0]
        out = exp.Neg(this=wrap(first.copy())) if neg else wrap(first.copy())
        for neg, t in items[1:]:
            op = exp.Sub if neg else exp.Add
            out = op(this=wrap(out), expression=wrap(t.copy()))
        return out

    def fun(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Mul) and not isinstance(bare_parent(n), exp.Mul) and in_aggregate(n):
            found = gather(factors(n), lambda f: f)
            if found:
                rest, groups = found
                parts = [*rest, *(exp.Paren(this=product(g)) for g in groups.values())]
                return product([_regroup(p, owner) for p in parts])
        if (
            isinstance(n, exp.Add | exp.Sub)
            and not isinstance(bare_parent(n), exp.Add | exp.Sub)
            and in_aggregate(n)
        ):
            found = gather(terms(n), lambda t: t[1])
            if found:
                rest, groups = found
                items = [*rest, *((False, exp.Paren(this=total(g))) for g in groups.values())]
                return total([(neg, _regroup(t, owner)) for neg, t in items])
        return n

    def bare_parent(n: exp.Expression) -> exp.Expression | None:
        p = n.parent
        while isinstance(p, exp.Paren):
            p = p.parent
        return p

    return tree.transform(fun)


def _injective(steps: list[tuple[exp.Expression, exp.Expression]], in_sum: bool) -> bool:
    """Whether the path from a carried part up to an aggregate's argument (``steps``,
    (parent, child) upwards) gives two copies that differ in the part results the
    aggregate tells apart as Layer 2's DISTINCT does: ``+`` and ``-`` give each value its
    own result; ``*`` and ``/`` too unless the other side is zero, which a ``SUM`` adds
    nothing for. A part that is the condition of a ``CASE`` with one ``WHEN`` (or an
    ``IF``), joined there only with AND, is a FILTER: the copies that fail it give NULL,
    which an aggregate skips, or 0, which a ``SUM`` adds nothing for."""
    state = "start"  # "cond": the part is a condition so far; "value": it is a value
    for parent, child in steps:
        if isinstance(parent, exp.And) and state in ("start", "cond"):
            state = "cond"
        elif isinstance(parent, exp.If) and child is parent.this and state in ("start", "cond"):
            case = parent.parent if isinstance(parent.parent, exp.Case) else None
            if case is not None and len(case.args.get("ifs") or []) != 1:
                return False
            rest = case.args.get("default") if case is not None else parent.args.get("false")
            zero = isinstance(rest, exp.Literal) and rest.is_number and float(rest.name) == 0
            if not (rest is None or isinstance(rest, exp.Null) or (zero and in_sum)):
                return False
            state = "value"
        elif isinstance(parent, exp.Case) and isinstance(child, exp.If) and state == "value":
            continue
        elif state in ("start", "value") and (
            isinstance(parent, exp.Add | exp.Sub | exp.Neg)
            or (in_sum and isinstance(parent, exp.Mul | exp.Div))
        ):
            state = "value"
        else:
            return False
    return state != "cond"


def _owners(subtrees: list[_Subtree]) -> dict[str, int]:
    """Upper-cased dataset name -> index of the subtree of Planner._dedup it is in."""
    return {d: i for i, s in enumerate(subtrees) for d in s.datasets}


def _loose_column(tree: exp.Expression) -> exp.Column | None:
    """A column outside every aggregate and every window clause: the spec's
    unaggregated field (E_UNAGGREGATED_FINER_GRAIN_REFERENCE)."""
    for col in tree.find_all(exp.Column):
        node = col.parent
        while node is not None and not (
            isinstance(node, exp.AggFunc | exp.Window)
            or (isinstance(node, exp.Filter) and isinstance(node.this, exp.AggFunc))
        ):
            node = node.parent
        if node is None:
            return col
    return None


def _grouping_aggregate(tree: exp.Expression) -> bool:
    """Whether ``tree`` has an aggregate that is not itself a window function: one
    that makes a SELECT without GROUP BY a single group."""
    return any(not _window_function(agg) for agg in tree.find_all(exp.AggFunc))


@dataclass(eq=False)  # compared by identity: units.index(u), u in members
class _Unit:
    """What a stitched branch computes: a model's metric as a whole, or one
    component of a metric whose aggregates read different datasets."""

    alias: str  # the metric's name, or __c<n> for a component
    tree: exp.Expression
    label: str  # "metric 'x'", as errors name it
    key: frozenset[str]  # the datasets whose rows it aggregates: its branch
    metric: str  # the metric the unit belongs to
    home: str = ""  # a component's dataset; empty for a whole metric
    carry: frozenset[str] = frozenset()  # what it reads from the one side, Planner._carry

    @property
    def component(self) -> bool:
        return bool(self.home)


@dataclass(eq=False)
class _Subtree:
    """What a root joins first and everything reachable from it along the planned
    joins, read once as a derived table (Planner._dedup)."""

    first: OssieRelationship  # the root's relationship to the subtree's first dataset
    rest: list[OssieRelationship]  # the other joins of the subtree, nearest first
    datasets: set[str]  # upper-cased names of all its datasets


# What an unmatched row of a LEFT JOIN with join_use_nulls = 1 holds, by type (ClickHouse
# 26.9): a type that cannot be Nullable gets its default value, the arrays among them (the
# geo types are arrays); a Variant or Dynamic is NULL already, and its default is NULL. A
# tuple, a Point and a JSON are NULL on a server that makes them Nullable inside a join
# (Catalog.nullable_tuple) and take the default on one that does not. Anything else is NULL.
_DEFAULT_TYPES = (
    "Array(", "Map(", "Ring", "Polygon", "MultiPolygon", "LineString", "MultiLineString",
    "AggregateFunction(", "Variant(", "Dynamic", "Object(",
)  # fmt: skip
_TUPLE_TYPES = ("Tuple(", "Point", "JSON")


def _nested_aggregate(tree: exp.Expression) -> str | None:
    """Why ``tree`` cannot run, when it has an aggregate inside another that is not
    a window function: AVG(COUNT(x)), AVG(SUM(x) OVER ()), or an aggregate in
    another's FILTER (WHERE ...). ClickHouse refuses them (ILLEGAL_AGGREGATION), and
    the merged specification does not define them. SUM(SUM(x)) OVER () is a window
    over grouped rows and passes."""
    for inner in tree.find_all(exp.AggFunc):
        prev, node = inner, inner.parent
        while node is not None and not isinstance(node, exp.AggFunc):
            if isinstance(node, exp.Filter) and prev is not node.this:
                node = node.this  # its condition belongs to the aggregate it filters
                break
            prev, node = node, node.parent
        if isinstance(node, exp.AggFunc) and not _window_function(node):
            outer = node.sql("clickhouse").partition("(")[0]
            shown = inner.find_ancestor(exp.Window) if _window_function(inner) else inner
            return (
                f"applies {outer} to {shown.sql('clickhouse')}, an aggregate; an aggregate "
                "over aggregates is answered only as a window, such as SUM(SUM(x)) OVER ()"
            )
    return None


def _homes(inputs: Inputs) -> list[str]:
    return sorted({d for _, ds in inputs for d in ds})


def _repeated(inputs: Inputs, root: str, one_to_one: set[str]) -> Inputs:
    """Duplicate-sensitive aggregates that read only datasets whose rows repeat over ``root``'s."""
    return [
        (agg, ds)
        for agg, ds in inputs
        if ds and root not in ds and not ds <= one_to_one and not _repeat_safe(agg)
    ]


def unanswerable_metrics(model: OssieDocument, catalog: Catalog | None = None) -> dict[str, str]:
    """Metrics the planner refuses in every question, with why.

    One that names an unknown field or reads no column, one with a column
    outside every aggregate, one whose aggregates read different datasets and
    that also has a window function (_split), one with a duplicate-sensitive
    aggregate over each of two datasets that is not split: whichever is the
    root, the other one's rows repeat, and one that reads a joined dataset with
    the root in an expression a copy of a key could count twice (_blend).
    Untranslatable metrics are reported by untranslatable()."""
    planner = Planner(model, catalog)
    skip = set(untranslatable(model)[1])
    out = {}
    for name, _, _ in all_metrics(model):
        if name in skip:
            continue
        try:
            # Through the planner, so names are the model's spelling, as in plan().
            m = planner.metric(name)
            tree = planner._metric_tree(m, set())
            if planner._split(m, tree) is not None:
                continue  # each aggregate is answered over its own dataset
            inputs = _aggregate_inputs(tree)
            try:
                planner.plan(Query((name,)))
            except _BlendError:
                raise
            except PlanError:
                pass  # refused in this question only, or for a reason found below
        except PlanError as e:
            out[name] = str(e)
            continue
        except ValueError:
            continue  # reads an untranslatable field, reported as such
        homes = _homes(inputs)
        refused = [_repeated(inputs, root, planner._one_to_one(root)) for root in homes]
        if homes and all(refused):
            aggs = ", ".join(r[0][0].sql("clickhouse") for r in refused)
            out[name] = (
                f"aggregates columns of {' and '.join(homes)} separately with functions that "
                f"count repeated rows ({aggs}); whichever "
                "is the root of a question, the other's rows repeat, so every question "
                "with it is refused"
            )
    return out


def _settings() -> list[exp.Expression]:
    """Settings every generated SELECT carries, so ClickHouse answers as SQL does.

    join_use_nulls: unmatched LEFT JOIN columns are NULL, not the type's default.
    aggregate_functions_null_for_empty: an aggregate over no rows (no GROUP BY, or a
    FILTER that matches nothing) is NULL, not 0; counts stay 0.
    transform_null_in: NULL IN (...) is not true, so a semi-join's NULL key
    references nothing, whatever the user's profile says."""
    return [
        exp.EQ(this=exp.var(name), expression=exp.Literal.number(value))
        for name, value in (
            ("join_use_nulls", 1),
            ("aggregate_functions_null_for_empty", 1),
            ("transform_null_in", 0),
        )
    ]


def _empty_value(tree: exp.Expression) -> exp.Expression | None:
    """A metric over no rows: what a stitched group missing from the metric's fact
    shows. The empty-input value of apache/ossie#653 section 5.2 (#354 section 8.1):
    NULL for SUM, 0 for COUNT, 0 for COALESCE(SUM(x), 0).

    None for a metric with an aggregate outside the spec: ClickHouse gives 0 for
    countIf and uniq, NULL for sumIf, [] for groupArray, and system.functions
    does not say which; Planner._over_no_rows asks ClickHouse instead."""
    if any(agg.meta.get("outside_spec") for agg in tree.find_all(exp.AggFunc)):
        return None

    def empty(node: exp.Expression) -> exp.Expression:
        agg = node  # COUNT(x) FILTER (...), ANY_VALUE(x) IGNORE NULLS
        while isinstance(agg, exp.Filter | exp.RespectNulls | exp.IgnoreNulls):
            agg = agg.this
        if isinstance(agg, exp.AggFunc):
            counts = isinstance(agg, exp.Count | exp.ApproxDistinct)
            return exp.Literal.number(0) if counts else exp.Null()
        return node

    out = tree.copy().transform(empty)
    return exp.Null() if out.find(exp.Column) else out


def _check_rows(
    label: str, inputs: Inputs, root: str, touched: dict[str, set[str]], one_to_one: set[str]
) -> None:
    """Refuse an aggregate expression whose answer would be about other rows than the root's.

    Joins are many-to-one, so a joined dataset's rows repeat per root row
    and include only those the root references."""
    homes = _homes(inputs)
    repeated = _repeated(inputs, root, one_to_one)
    if homes and root not in homes:
        by = [item for item, ds in touched.items() if root in ds and item != label] or [label]
        home = " and ".join(homes)
        raise PlanError(
            f"{label} aggregates {home} rows, but this question is answered over "
            f"{root} rows (because of {', '.join(by)}): it would see only {home} "
            f"rows that {root} references{', once per reference' if repeated else ''}. "
            f"Filter or group a {home} metric only by fields of {home} or of datasets "
            f"{home} joins to"
        )
    if repeated:
        agg, joined = repeated[0][0].sql("clickhouse"), " and ".join(sorted(repeated[0][1]))
        raise PlanError(
            f"{label} applies {agg} to {joined} columns alone over {root} rows, "
            f"so each {joined} row would count once per {root} row that references it. "
            f"Only {_REPEAT_SAFE_NAMES} aggregates may read a joined dataset on their own, "
            f"unless it is joined on a key of {root} (one row each); "
            "this version cannot answer it"
        )


def _ambiguous(start: str, target: str, paths: list[tuple[OssieRelationship, ...]]) -> str:
    shown = " and ".join(
        " -> ".join(repr(r.name) for r in p)
        for p in sorted(paths, key=lambda p: (len(p), [r.name for r in p]))
    )
    return (
        f"ambiguous join from {start!r} to {target!r}: relationship paths "
        f"{shown}; this version cannot choose between them"
    )


def _in(keys: list[exp.Expression], sub: exp.Select) -> exp.In:
    """keys IN (sub): a tuple for a key of several fields."""
    this = keys[0] if len(keys) == 1 else exp.Tuple(expressions=keys)
    return exp.In(this=this, query=sub.subquery())


@dataclass
class Planner:
    model: OssieDocument
    catalog: Catalog | None = None  # from Executor.introspect(); None means plain tables
    _datasets: dict[str, OssieDataset] = field(init=False)
    _fields: dict[str, dict[str, OssieField]] = field(init=False)
    _metrics: dict[str, OssieMetric] = field(init=False)  # a dataset's as "dataset.metric"
    _scopes: dict[str, OssieDataset] = field(init=False)  # a dataset's metric -> its dataset
    _zone: str = field(init=False)  # model.time_zone(): CURRENT_DATE and literals

    def __post_init__(self):
        self._zone = time_zone(self.model)
        # Spec: unquoted identifiers are case-insensitive; index by upper case,
        # but always emit the physical names as written in the model.
        self._datasets = {d.name.upper(): d for d in self.model.datasets}
        self._fields = {
            d.name.upper(): {f.name.upper(): f for f in d.fields or []} for d in self.model.datasets
        }
        self._metrics, self._scopes = {}, {}
        for name, m, ds in all_metrics(self.model):
            self._metrics[name.upper()] = m if ds is None else m.model_copy(update={"name": name})
            if ds is not None:
                self._scopes[name.upper()] = ds
        self._out: dict[str, list[OssieRelationship]] = {}
        for r in self.model.relationships or []:
            if r.to.upper() in self._datasets:
                self._out.setdefault(r.from_dataset.upper(), []).append(r)

    # --- resolution ---------------------------------------------------------

    def dataset(self, name: str) -> OssieDataset:
        ds = self._datasets.get(name.upper())
        if ds is None:
            raise PlanError(
                f"unknown dataset {name!r}{suggest(name, (d.name for d in self.model.datasets))}"
            )
        return ds

    def metric(self, name: str) -> OssieMetric:
        """A metric by the name a question gives it; a dataset's metric comes
        named ``dataset.metric``."""
        m = self._metrics.get(name.upper())
        if m is None:
            if not (fields := self._fields_named(name)):
                names = (x.name for x in self._metrics.values())
                raise PlanError(f"unknown metric {name!r}{suggest(name, names)}")
            raise PlanError(f"unknown metric {name!r}: {self._field_not_metric(name, fields)}")
        return m

    def _fields_named(self, name: str) -> list[str]:
        """``dataset.field`` of the field ``name`` names: dataset.field, or a bare name
        that is a field of one or more datasets (#66). ".field" names none."""
        ds_name, dot, f_name = name.rpartition(".")
        if dot and not ds_name:
            return []
        return [
            f"{d.name}.{self._fields[d.name.upper()][f_name.upper()].name}"
            for d in self.model.datasets
            if (not ds_name or d.name.upper() == ds_name.upper())
            and f_name.upper() in self._fields[d.name.upper()]
        ]

    def _field_not_metric(self, name: str, fields: list[str]) -> str:
        """The metrics whose expressions read the fields, by name: since a dataset's
        metric is ``dataset.metric``, a field reads like one, and suggestions by
        spelling offer metrics over other fields instead (#66). A bare name may
        still be a dataset's metric asked without its dataset."""
        wanted = {f.upper() for f in fields}
        over = []
        for metric, m, ds in all_metrics(self.model):
            try:
                tree = parse(pick_expression(m.expression))
            except ValueError:  # refused in every question, and hidden by the executor
                continue
            # A dataset's source.column reads no field (unless the dataset declares one
            # named source, and then the metric is refused).
            from_source = ds is not None and reads_source(ds)
            read = {
                f"{c.table or (ds.name if ds else '')}.{c.name}".upper()
                for c in tree.find_all(exp.Column)
                if not (from_source and c.table.upper() == SOURCE_QUALIFIER)
            }
            if read & wanted:
                over.append(metric)
        it = "it" if len(fields) == 1 else "them"
        if "." in name:
            what, same = f"it is a field of {fields[0].partition('.')[0]}", []
        else:
            what = f"it names the field{'s' * (len(fields) > 1)} {', '.join(fields)}"
            suffix = f".{name.upper()}"
            same = [x.name for x in self._metrics.values() if x.name.upper().endswith(suffix)]
        listed = f"metrics over {it}: {', '.join(over)}" if over else f"no metric reads {it}"
        hint = f"; dataset metrics of that name: {', '.join(same)}" if same else ""
        return f"{what}, not a metric; {listed}{hint}"

    def _named_metric(self, col: exp.Column) -> OssieMetric | None:
        """The metric a filter's column names: a bare metric name, or dataset.metric
        for a dataset's metric. None for a field."""
        if not col.table:
            return self._metrics.get(col.name.upper())
        if self._fields.get(col.table.upper(), {}).get(col.name.upper()):
            return None
        return self._metrics.get(f"{col.table}.{col.name}".upper())

    def field_ref(self, ref: str) -> tuple[OssieDataset, OssieField]:
        if ref.count(".") != 1:
            raise PlanError(f"field reference must be dataset.field, got {ref!r}")
        ds_name, f_name = ref.split(".")
        ds = self.dataset(ds_name)
        f = self._fields[ds.name.upper()].get(f_name.upper())
        if f is None:
            names = (f"{ds.name}.{x.name}" for x in ds.fields or [])
            raise PlanError(f"unknown field {ref!r}{suggest(ref, names)}")
        return ds, f

    def _parse(self, expression: str) -> exp.Expression:
        """parse(), with a call of a function outside the spec that the server
        says is an aggregate (countIf, uniqExactOrNull) typed as one, so every
        check on aggregates covers it. Without the server's word, SQLGlot's list
        of ClickHouse aggregates decides (_known_clickhouse_aggregate)."""
        tree = parse(expression)
        # A plain dict has no aggregate; a catalog has None without system.functions.
        aggregate = getattr(self.catalog, "aggregate", None) or _known_clickhouse_aggregate

        def mark(node: exp.Expression) -> exp.Expression:
            # ClickHouse runs "countIf"(x) as countIf(x); an aggregate's name never
            # needs the quotes, which AnonymousAggFunc would not print.
            if outside_spec(node) and aggregate(node.name):
                agg = exp.AnonymousAggFunc(this=node.name, expressions=node.expressions)
                agg.meta["outside_spec"] = True
                return agg
            return node

        return tree.transform(mark)

    def resolve(
        self,
        expression: str,
        used: set[str],
        metrics: bool = False,
        scope: OssieDataset | None = None,
        parsed: exp.Expression | None = None,
    ) -> exp.Expression:
        """Parse an expression, inline `dataset.field` references, qualify columns.
        ``parsed`` is a tree already parsed from ``expression`` (a part of a filter).

        With ``metrics``, a metric's name inlines that metric's expression
        (filters may say ``total_sales > 1000`` or ``store_sales.total_quantity > 5``).
        With ``scope``, the expression is a metric of that dataset: a bare name is
        one of its fields, and nothing else may be named (spec, "Metric Scoping").
        Records the datasets touched in ``used`` (by model name); with ``scope``,
        _metric_tree records it, since COUNT(*) reads no field.
        """
        if parsed is not None:
            tree = parsed
        else:
            try:
                tree = self._parse(expression)
            except ValueError as e:
                raise PlanError(str(e)) from e
            self._time_strings(tree, metrics, scope)

        def inline(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            if scope is not None:
                return self._own_field(scope, node, expression)
            if metrics and (m := self._named_metric(node)):
                return self._metric_tree(m, used)
            if node.table.upper() == SOURCE_QUALIFIER and not self._datasets.get(SOURCE_QUALIFIER):
                raise PlanError(
                    f"{node.sql()} in {expression!r}: source columns are read only by a "
                    "dataset's own metrics, as source.column; use a field of the dataset "
                    "(dataset.field)"
                )
            if not node.table:
                hint = ""
                if metrics:
                    names = [x.name for x in self._metrics.values()] + [
                        f"{d.name}.{x.name}" for d in self.model.datasets for x in d.fields or []
                    ]
                    hint = f"; use dataset.field or a metric name{suggest(node.name, names)}"
                raise PlanError(f"unqualified column {node.name!r} in {expression!r}{hint}")
            ds, f = self.field_ref(f"{node.table}.{node.name}")
            used.add(ds.name)
            return self._field_expr(ds, f)

        return tree.transform(inline)

    def _own_field(self, ds: OssieDataset, node: exp.Column, expression: str) -> exp.Expression:
        """A column of a metric of ``ds``: one of its fields by bare name, or one of its
        source's columns as ``source.column`` (rules 1 to 3 of "Metric Scoping")."""
        if node.table:
            if node.table.upper() == SOURCE_QUALIFIER:
                if not reads_source(ds):
                    raise PlanError(
                        f"{node.sql()} in {expression!r}: dataset {ds.name!r} declares a field "
                        f"named {SOURCE_QUALIFIER.lower()}, which takes precedence over the "
                        "qualifier, so its metrics cannot read source columns"
                    )
                return self._source_column(ds, node)
            why = (
                f"names its own fields without the dataset, as {node.name}"
                if node.table.upper() == ds.name.upper()
                else "reads only its own fields and, as source.column, its source's columns; a "
                "metric over other datasets belongs in the model's metrics"
            )
            raise PlanError(
                f"{node.sql()} in {expression!r}: a metric of dataset {ds.name!r} {why}"
            )
        f = self._fields[ds.name.upper()].get(node.name.upper())
        if f is None:
            names = (x.name for x in ds.fields or [])
            raise PlanError(
                f"{node.name!r} in {expression!r} is not a field of {ds.name!r}"
                f"{suggest(node.name, names)}"
            )
        return self._field_expr(ds, f)

    @staticmethod
    def _source_column(ds: OssieDataset, node: exp.Column) -> exp.Expression:
        """``source.column`` as ``column`` under the dataset's alias; no field's
        expression is substituted, even for a field of the same name (spec, "Name
        Resolution": "each source.column reference with column")."""
        return exp.column(node.this, table=ds.name)

    def _time_strings(
        self, tree: exp.Expression, metrics: bool, scope: OssieDataset | None = None
    ) -> None:
        """Make a string compared with a DateTime or DateTimeTz field or metric a
        TIMESTAMP, which rewrite_tree reads in the model's zone, an offset kept.

        ClickHouse reads it in the column's zone, so the same time written with and
        without TIMESTAMP named two instants (#58). Only a reference compared directly
        is known to be one: the datatype says so, before inlining replaces the name."""

        def is_time(node: exp.Expression) -> bool:
            node = node.unnest()
            if not isinstance(node, exp.Column):
                return False
            if scope is not None:
                own = self._fields[scope.name.upper()]
                obj = None if node.table else own.get(node.name.upper())
            elif metrics and (m := self._named_metric(node)):
                obj = m
            elif node.table:
                ds = self._datasets.get(node.table.upper())
                obj = ds and self._fields[ds.name.upper()].get(node.name.upper())
            else:
                obj = None
            return getattr(obj, "datatype", None) in _TIMES

        def to_timestamp(nodes: list[exp.Expression]) -> None:
            for node in nodes:
                node = node.unnest()
                if isinstance(node, exp.Literal) and node.is_string:
                    node.replace(exp.cast(node.copy(), exp.DType.TIMESTAMP))

        for node in list(tree.find_all(*_COMPARISONS, exp.Between, exp.In)):
            # Each side against the other: '2026-09-15' BETWEEN p.start_at AND p.end_at too.
            if isinstance(node, exp.Between):
                one, others = node.this, [node.args["low"], node.args["high"]]
            elif isinstance(node, exp.In):
                one, others = node.this, node.expressions
            else:
                one, others = node.this, [node.expression]
            if any(is_time(other) for other in others):
                to_timestamp([one])
            if is_time(one):
                to_timestamp(others)

    def _metric_tree(self, m: OssieMetric, used: set[str]) -> exp.Expression:
        """A metric's expression, resolved and rewritten for ClickHouse.

        A dataset's metric aggregates that dataset's rows, COUNT(*) too: each
        aggregate says so in its meta, which _aggregate_inputs reads. For a model's
        metric, refuses an aggregate that reads no column (COUNT(*),
        MAX(CURRENT_TIMESTAMP)) unless the metric's other aggregates name the one
        dataset whose rows it aggregates: otherwise it would aggregate rows of
        whichever dataset a question is answered over. A metric without aggregates
        that reads no column (the current time) is answered over any rows, or none."""
        scope = self._scopes.get(m.name.upper())
        resolved = self.resolve(pick_expression(m.expression), used, scope=scope)
        tree = rewrite_tree(resolved, self._zone)
        if problem := _nested_aggregate(tree):
            raise PlanError(f"metric {m.name!r} {problem}")
        # A call of a function outside the spec may be an aggregate, and only the server
        # says (_parse); without its word, ClickHouse decides these two checks.
        unsure = getattr(self.catalog, "aggregate", None) is None and any(
            outside_spec(f) for f in resolved.find_all(exp.Anonymous)
        )
        tree.meta["unsure"] = unsure
        if scope is not None:
            used.add(scope.name)
            for agg in tree.find_all(exp.AggFunc):  # after rewrite_tree, which replaces some
                agg.meta["dataset"] = scope.name
            return tree
        if len(self.model.datasets) > 1:
            reads = _aggregate_reads(tree)
            homes = _homes(_aggregate_inputs(tree))
            unnamed = [agg for agg, args, _ in reads if not args]
            if unnamed and len(homes) != 1:
                agg = unnamed[0]
                filtered = isinstance(agg.parent, exp.Filter)
                why = (
                    "count rows of whichever dataset a question is answered over; count a "
                    "column of the dataset it means, such as COUNT(dataset.key)"
                    if isinstance(agg, exp.Count)
                    else "aggregate rows of whichever dataset a question is answered over; "
                    "read a column of the dataset it means, or leave out the aggregate for "
                    "a value that reads no dataset, such as CURRENT_TIMESTAMP"
                )
                raise PlanError(
                    f"metric {m.name!r} uses {agg.sql('clickhouse')}, which reads no "
                    f"column{', and a FILTER names no dataset' if filtered else ''}, so it "
                    f"would {why}"
                )
            if pair := _mixed(reads):
                raise PlanError(self._mixed_message(f"metric {m.name!r}", *pair))
        if not unsure and (loose := _loose_column(tree)):
            # E_UNAGGREGATED_FINER_GRAIN_REFERENCE (apache/ossie#653 section 5.2): whether
            # or not a question also groups by the column.
            split = bool(self._split_homes(tree))
            raise PlanError(
                f"metric {m.name!r} reads {loose.sql('clickhouse')} outside every aggregate"
                + (
                    ", and its aggregates read different datasets, so each is answered over "
                    "its own dataset's rows and a bare column has no row to come from"
                    if split
                    else ", so it has no single value for the rows the metric aggregates"
                )
                + "; aggregate it, or take it out of the metric and ask for the field as a "
                "dimension"
            )
        return tree

    @staticmethod
    def _mixed_message(what: str, agg: exp.AggFunc, other: exp.AggFunc) -> str:
        """Why ``what`` is refused for an aggregate that reads several datasets (or
        filters on another) next to one over a dataset it does not read."""
        first = _component_node(agg).sql("clickhouse")
        second = _component_node(other).sql("clickhouse")
        return (
            f"{what} applies {first}, which reads several datasets or filters on another, "
            f"next to {second}, which reads a dataset the first does not. The first is "
            "computed over one join of its datasets, so the second would count the rows "
            "of its dataset as that join repeats them; Layer 3 (apache/ossie#653, "
            "section 5.2) rejects the first. Split it into two metrics, or declare the "
            "metric on the dataset it means (a dataset's metric reads only that "
            "dataset's rows)"
        )

    @staticmethod
    def _split_homes(tree: exp.Expression) -> set[str]:
        """The datasets the aggregates of a model's metric read, one each, when they
        are at least two; empty otherwise (_split)."""
        reads = _aggregate_reads(tree)
        if not reads or any(len(args) != 1 or not filtered <= args for _, args, filtered in reads):
            return set()
        homes = {next(iter(args)) for _, args, _ in reads}
        return homes if len(homes) > 1 else set()

    def _split(
        self, m: OssieMetric, tree: exp.Expression
    ) -> list[tuple[exp.Expression, str]] | None:
        """The components of a model's metric, [(aggregate with its wrappers, home
        dataset)] in ``tree``, when its aggregates read different datasets; None
        when it is one branch, planned as a whole.

        Layer 3 (apache/ossie#653, "Metric components"): each aggregate of a
        model's metric is evaluated over the rows of the one dataset its
        arguments read, and the metric's expression over the values. A
        dataset's metric is one component whatever its expression. A metric with
        an aggregate that reads several datasets (SUM(lines.qty * products.price),
        or a FILTER on another dataset) is not split, which #653 defers and
        refuses: it is planned as before, unless another aggregate reads a
        dataset it does not (_mixed). An aggregate's FILTER does not give its
        home. Refused when split: a window function (deferred). _metric_tree has
        refused a column outside every aggregate, split or not."""
        if m.name.upper() in self._scopes:
            return None
        if not self._split_homes(tree):
            return self._split_carries(m, tree)
        reads = _aggregate_reads(tree)
        if tree.find(exp.Window):
            raise PlanError(
                f"metric {m.name!r} is a window function over aggregates that read different "
                "datasets; this version cannot answer it (the spec defers it). Put the "
                "window in a metric of one dataset, or ask for each part on its own"
            )
        return [(_component_node(agg), next(iter(args))) for agg, args, _ in reads]

    def _carry(self, node: exp.Expression) -> frozenset[str]:
        """What an aggregate (with its FILTER) reads from datasets other than its root,
        as the largest expressions that read only those: ``customers.region = 'WEST'`` in
        ``SUM(orders.amount) FILTER (WHERE customers.region = 'WEST')``, or
        ``customers.weight`` in ``SUM(orders.amount * customers.weight)``. _dedup carries
        these in the derived table of the one side, where they keep apart the copies of
        a key, for every aggregate of the SELECT: aggregates with other values go to
        other SELECTs (_stitch)."""
        datasets = {
            self._datasets[c.table.upper()].name
            for c in node.find_all(exp.Column)
            if c.table.upper() in self._datasets
        }
        if len(datasets) < 2:
            return frozenset()
        try:
            root = self._root(datasets).name.upper()
        except PlanError:
            return frozenset()
        parts: set[str] = set()

        def visit(n: exp.Expression) -> None:
            if isinstance(n, exp.Condition) and not n.find(exp.AggFunc, exp.Window, exp.Select):
                tables = {c.table.upper() for c in n.find_all(exp.Column)}
                if tables and root not in tables:
                    if not all(self._one_row_per_dataset(root, t) for t in tables):
                        parts.add(n.sql("clickhouse"))
                    return
            for child in n.iter_expressions():
                visit(child)

        visit(node)
        return frozenset(parts)

    def _one_row_per_dataset(self, root: str, name: str) -> bool:
        """Whether every relationship on the one path from ``root`` to ``name`` joins a
        dictionary on its key: no copies to keep apart, so nothing to carry."""
        paths = self._paths(root, name)
        return len(paths) == 1 and all(self._one_row_per_key(r) for r in paths[0])

    def _unit_root(self, u: _Unit) -> str | None:
        """The dataset whose rows a unit aggregates, None if it has no one."""
        if u.home:
            return u.home
        try:
            return self._root({self.dataset(d).name for d in u.key}).name if u.key else None
        except PlanError:
            return None

    def _carries(self, trees: list[exp.Expression]) -> list[tuple[exp.AggFunc, frozenset[str]]]:
        """Each aggregate of ``trees`` that is not a window function, with its _carry."""
        return [
            (agg, self._carry(_component_node(agg)))
            for tree in trees
            for agg in tree.find_all(exp.AggFunc)
            if not _window_function(agg)
        ]

    def _carries_clash(self, trees: list[exp.Expression]) -> bool:
        """Whether the aggregates of ``trees`` carry values that keep apart copies of a key
        for an aggregate that counts repeated rows and does not carry them itself."""
        found = self._carries(trees)
        return any(
            not carried <= mine
            for agg, mine in found
            if not _repeat_safe(agg)
            for _, carried in found
        )

    def _split_carries(
        self, m: OssieMetric, tree: exp.Expression
    ) -> list[tuple[exp.Expression, str]] | None:
        """The components of a model's metric whose aggregates read one root's datasets
        but need other values from the one side (``SUM(x) FILTER (WHERE d.a = 1) /
        SUM(x)``): each is evaluated in a SELECT of its own, since a value carried for
        one would keep apart copies of a key that the other must count once. None when
        they need the same values, or none of them counts repeated rows."""
        reads = _aggregate_reads(tree)
        if not self._carries_clash([tree]):
            return None
        if tree.find(exp.Window):
            raise PlanError(
                f"metric {m.name!r} is a window function over aggregates that read different "
                "values of a dataset they join to (a FILTER on it, or its column in an "
                "argument), which must be answered apart; this version cannot answer it. Put "
                "the window in a metric of one dataset, or ask for each part on its own"
            )
        names = {d for _, args, filtered in reads for d in args | filtered}
        try:
            whole = self._root({self.dataset(d).name for d in names}).name
        except PlanError:
            return None
        out = []
        for agg, args, filtered in reads:
            own = {self.dataset(d).name for d in args | filtered if d}
            try:
                home = self._root(own).name if own else whole
            except PlanError:
                return None
            if home != whole:
                return None  # an aggregate over other rows than the root's: refused as before
            out.append((_component_node(agg), home))
        return out

    def _field_expr(self, ds: OssieDataset, f: OssieField) -> exp.Expression:
        """A field's expression with its bare columns qualified by the dataset alias."""
        tree = self._parse(pick_expression(f.expression))

        def qualify(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column) and not node.table:
                return exp.column(node.this, table=ds.name)
            if isinstance(node, exp.Column):
                raise PlanError(
                    f"field {ds.name}.{f.name} references another dataset ({node.sql()}); "
                    "fields may only use their own columns"
                )
            return node

        return tree.transform(qualify)

    def _keys(self, ds: OssieDataset, names: list[str]) -> list[exp.Expression]:
        """The expressions of the fields of ``ds`` that a key or a relationship's
        columns name (spec, "Name Resolution"): o_orderkey for a field order_id that
        reads it. Rewritten for ClickHouse, and in parentheses unless a column, since
        they go under an operator after rewrite_tree."""
        out = []
        for name in names:
            _, f = self.field_ref(f"{ds.name}.{name}")  # load_model checks; model_validate does not
            e = rewrite_tree(self._field_expr(ds, f), self._zone)
            out.append(e if isinstance(e, exp.Column) else exp.paren(e, copy=False))
        return out

    # --- joins --------------------------------------------------------------

    def _outgoing(self, name: str) -> list[OssieRelationship]:
        """Relationships from ``name`` to a dataset of the model."""
        return self._out.get(name.upper(), [])

    def _paths(self, start: str, target: str) -> list[tuple[OssieRelationship, ...]]:
        """Up to two acyclic paths of relationships from ``start`` to ``target``
        (apache/ossie#653 section 4: no dataset twice); a second one already makes the
        join ambiguous."""
        found: list[tuple[OssieRelationship, ...]] = []

        def walk(at: str, path: tuple[OssieRelationship, ...], seen: frozenset[str]) -> None:
            if at == target.upper():
                found.append(path)
                return
            for r in self._outgoing(at):
                if len(found) < 2 and r.to.upper() not in seen:
                    walk(r.to.upper(), (*path, r), seen | {r.to.upper()})

        walk(start.upper(), (), frozenset({start.upper()}))
        return found

    def _joins(self, root: OssieDataset, used: set[str]) -> list[OssieRelationship]:
        """The relationships that join each used dataset to ``root``, nearest first.

        apache/ossie#653 section 4: the one path of many-to-one relationships to each
        dataset, ambiguous when there are two. Datasets on a path that the query
        does not name are joined too (section 5.4, the join tree)."""
        depth: dict[str, tuple[int, OssieRelationship]] = {}
        for name in sorted(used - {root.name}):
            paths = self._paths(root.name, name)
            if len(paths) > 1:
                raise PlanError(_ambiguous(root.name, name, paths))
            for i, r in enumerate(paths[0]):  # _root chose a dataset that reaches every other
                depth[r.name] = (i, r)
        joins = [r for _, r in sorted(depth.values(), key=lambda d: (d[0], d[1].name))]
        for r in joins:
            problem = join_problem(r, self.dataset(r.to))
            if problem:
                raise PlanError(problem)
        return joins

    def _one_to_one(self, root: str) -> set[str]:
        """Datasets joined from ``root`` through relationships each on a key of its
        from side, the only one between the two: none of their rows repeats."""
        out: set[str] = set()
        todo = [self.dataset(root)]
        while todo:
            ds = todo.pop()
            found: dict[str, list[OssieRelationship]] = {}
            for r in self._outgoing(ds.name):
                found.setdefault(r.to.upper(), []).append(r)
            for to, rs in found.items():
                target = self._datasets[to]
                if len(rs) == 1 and covers_key(rs[0].from_columns, ds) and target.name not in out:
                    if target.name.upper() != root.upper():
                        out.add(target.name)
                        todo.append(target)
        return out

    def _reachable(self, name: str) -> set[str]:
        """Datasets ``name`` reaches from the many side to the one side, itself included."""
        seen, todo = {name.upper()}, [name.upper()]
        while todo:
            for r in self._outgoing(todo.pop()):
                if r.to.upper() not in seen:
                    seen.add(r.to.upper())
                    todo.append(r.to.upper())
        return {self._datasets[n].name for n in seen}

    def _root(self, used: set[str]) -> OssieDataset:
        """The used dataset that reaches every other one through many-to-one relationships."""
        candidates = [self.dataset(n) for n in sorted(used) if used <= self._reachable(n)]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise PlanError(
                f"datasets {sorted(used)} are not joined through many-to-one relationships "
                "from one root"
            )
        raise PlanError(f"ambiguous root dataset among {[c.name for c in candidates]}")

    # --- assembly -----------------------------------------------------------

    def plan(self, q: Query) -> exp.Select:
        return self._plan(q)

    def _plan(
        self,
        q: Query,
        components: dict[str, tuple[str, exp.Expression]] | None = None,
        nested: bool = False,
        keys_of: OssieDataset | None = None,
    ) -> exp.Select:
        """``plan``. A branch of a stitched question (``nested``) is planned as a
        question of its own: it is never stitched again. Its ``components``
        name aggregates of a model's metric by internal alias, as (label, tree);
        they stand in for metrics of the model. ``keys_of`` selects and groups
        by the fields of that dataset's first declared key too, as __k0, __k1...
        (_bridge)."""
        components = components or {}
        if not q.metrics and not q.dimensions:
            raise PlanError("a query needs at least one metric or dimension")
        if q.limit is not None and q.limit < 0:
            # ClickHouse reads LIMIT -n as the last n rows, which no question means.
            raise PlanError(f"limit {q.limit} is negative; use 0 or more")
        # Query item ("metric 'x'") -> datasets it reads: picks the root, explains it.
        touched: dict[str, set[str]] = {}
        checked: list[tuple[str, Inputs]] = []  # aggregate expressions
        selects: list[exp.Expression] = []
        group: list[exp.Expression] = []
        windowed: list[str] = []  # metrics with a window function that reads a column
        measured: list[tuple[str, exp.Expression]] = []  # metrics and aggregate filters
        seen_aliases: set[str] = set()  # upper-cased: ORDER BY resolves names case-insensitively

        def alias(name: str) -> str:
            if name.upper() in seen_aliases:
                raise PlanError(
                    f"two selected columns named {name!r}: a metric and a dimension, or "
                    "fields of two datasets, cannot share a name in one query"
                )
            seen_aliases.add(name.upper())
            return name

        for ref in q.dimensions:
            ds, f = self.field_ref(ref)
            touched.setdefault(f"dimension {ref!r}", set()).add(ds.name)
            e = rewrite_tree(self._field_expr(ds, f), self._zone)
            selects.append(e.as_(alias(f.name)))
            group.append(e.copy())
        if keys_of is not None:
            touched.setdefault(f"dataset {keys_of.name!r}", set()).add(keys_of.name)
            for i, e in enumerate(self._keys(keys_of, list(declared_keys(keys_of)[0]))):
                selects.append(e.as_(alias(f"__k{i}")))
                group.append(e.copy())

        for name in q.metrics:
            if name in components:
                label, tree = components[name]
                inputs = _aggregate_inputs(tree)
                touched.setdefault(label, set()).update(d for _, ds in inputs for d in ds)
                checked.append((label, inputs))
                measured.append((label, tree))
                selects.append(tree.copy().as_(alias(name)))
                continue
            m = self.metric(name)
            label = f"metric {m.name!r}"
            tree = self._metric_tree(m, touched.setdefault(label, set()))
            checked.append((label, _aggregate_inputs(tree)))
            measured.append((label, tree))
            if tree.find(exp.Window) and tree.find(exp.Column) and not tree.meta.get("unsure"):
                windowed.append(m.name)
            selects.append(tree.as_(alias(m.name)))

        rows: dict[str, exp.Expression] = {}  # row filter label -> rewritten tree
        having: list[exp.Expression] = []  # filters over aggregates or metric names
        row_filters: list[str] = []
        aggregate_filters: list[str] = []
        # Every part is rewritten once, before inspecting: APPROX_PERCENTILE is a
        # plain function name to SQLGlot and an aggregate only after rewrite.
        grouped = {e.sql() for e in group}
        for f in q.filters:
            label = f"filter {f!r}"
            tree = self.resolve(f, touched.setdefault(label, set()), metrics=True)
            tree = rewrite_tree(tree, self._zone)
            if tree.find(exp.Window):
                raise PlanError(
                    f"filter {f!r} uses a window function; a window metric cannot be "
                    "filtered in the same SELECT, select it and filter the rows instead"
                )
            if problem := _nested_aggregate(tree):
                raise PlanError(f"filter {f!r} {problem}")
            if not tree.find(exp.AggFunc):
                rows[label] = tree
                row_filters.append(f)
                continue
            named = self._written_aggregates(f)
            # HAVING may only see aggregates and GROUP BY expressions.
            for col in tree.find_all(exp.Column):
                if col.find_ancestor(exp.AggFunc):
                    continue
                node: exp.Expression | None = col
                while node is not None and node.sql() not in grouped:
                    node = node.parent
                if node is None:
                    raise PlanError(
                        f"filter {f!r} mixes an aggregate with {col.sql()}, which is not a "
                        "dimension of this query; add it to dimensions or put it in its own filter"
                    )
            having.append(tree)
            measured.append((label, tree))
            aggregate_filters.append(f)
            checked.append((label, _aggregate_inputs(tree)))
            # Each metric it names on its own too, after the filter, which errors name first.
            checked += [
                (f"metric {m.name!r}", _aggregate_inputs(self._metric_tree(m, set())))
                for m in named
            ]

        if windowed and not group and not any(_grouping_aggregate(e) for e in [*selects, *having]):
            # Nothing groups the rows, so the window would run over the root's rows and
            # return one for each, not the one row of a total.
            raise PlanError(
                f"metric {windowed[0]!r} is a window function, and this question has no "
                "dimension and no aggregate outside a window, so nothing groups the rows and "
                "it would return a row for each row of the data; ask for it with a dimension, "
                "or put an aggregate inside the window, such as SUM(SUM(x)) OVER ()"
            )
        if not nested and self._splits(q, aggregate_filters):
            # Never one SELECT over a join: a component counts the rows of its own dataset.
            return self._stitch(q, row_filters, aggregate_filters)
        used = set().union(*touched.values())
        where = list(rows.values())
        semi_joins: list[exp.Expression] = []
        aggregates = any(e.find(exp.AggFunc) for e in [*selects, *having])
        if not used and len(self.model.datasets) == 1 and aggregates:
            # An aggregate that reads no column (COUNT(*)) aggregates the one dataset's
            # rows; with several, _metric_tree refuses it unless another names one.
            used = {self.model.datasets[0].name}
        root = None
        if used:
            try:
                root = self._root(used)
                one_to_one = self._one_to_one(root.name)
                for label, inputs in checked:
                    _check_rows(label, inputs, root.name, touched, one_to_one)
            except PlanError:
                split = self._split_filters(touched, rows, checked)
                if split is None:
                    stitched = None if nested else self._stitch(q, row_filters, aggregate_filters)
                    if stitched is not None:
                        return stitched
                    raise
                root, joined, semi_joins = split  # joined: row filters the root's joins answer
                where = [rows[label] for label in joined]
                kept = [ds for label, ds in touched.items() if label not in rows or label in joined]
                used = set().union(*kept)
        elif aggregates:
            raise PlanError(
                "this question's aggregates read no column, so nothing says whose rows "
                "they aggregate; read a column of the dataset meant, such as "
                "COUNT(dataset.key)"
            )
        if root is not None and not nested and self._carries_clash([*selects, *having]):
            # The question is answered over one root, but its aggregates need other values
            # from the one side (_carry): one SELECT for each set of values.
            # One metric alone that needs several, or an aggregate written in a filter, stays
            # in one SELECT (docs/decisions/key-stored-twice.md).
            stitched = self._stitch(q, row_filters, aggregate_filters)
            if stitched is not None:
                return stitched
        subtrees: list[_Subtree] | None = None
        if root is None:
            # Reads no dataset (the current time): one row, without FROM.
            sel, dict_keys = exp.select(*selects), {}
        else:
            if self._duplicate_sensitive([*selects, *having]):
                subtrees = []
            sel, dict_keys = self._from(exp.select(*selects), root, used, subtrees)
        if where:
            sel = sel.where(exp.and_(*where))
        if group:
            sel = sel.group_by(*group)
        if having:
            sel = sel.having(exp.and_(*having))
        if order := self._order(q, selects):
            sel = sel.order_by(*order)
        if q.limit is not None:
            sel = sel.limit(q.limit)
        if root is not None and subtrees:
            sel = _regroup(sel, _owners(subtrees))
            filtered = [(label, t, True) for label, t in rows.items() if any(t is w for w in where)]
            self._refuse_blends(
                [*filtered, *((label, t, False) for label, t in measured)], root, subtrees
            )
            sel = self._dedup(sel, root, subtrees)
        if dict_keys:
            sel = sel.transform(lambda n: self._dict_get(n, dict_keys))
        if semi_joins:  # after the transform: their subqueries read datasets of their own
            sel = sel.where(exp.and_(*semi_joins))
        sel.set("settings", _settings())
        return sel

    def _from(
        self,
        sel: exp.Select,
        root: OssieDataset,
        used: set[str],
        subtrees: list[_Subtree] | None = None,
    ) -> tuple[exp.Select, dict[str, exp.Expression]]:
        """FROM ``root`` with a LEFT JOIN to each used dataset, and the dictionary
        datasets read with dictGetOrNull instead, by dataset name -> key expression;
        the caller rewrites their columns once the SELECT is complete.

        With ``subtrees`` (a list to fill), the datasets the root joins through one
        first relationship whose rows may repeat a key are not joined: they are
        left in ``subtrees`` for _dedup to join as a derived table."""
        sel = sel.from_(self._table(root))
        joins = self._joins(root, used)
        if subtrees is not None:
            joins = self._subtrees(root, joins, subtrees)
        return self._join_all(sel, joins)

    def _join_all(
        self, sel: exp.Select, joins: list[OssieRelationship]
    ) -> tuple[exp.Select, dict[str, exp.Expression]]:
        """``sel`` with a LEFT JOIN along each of ``joins``, nearest first; see _from."""
        dict_keys: dict[str, exp.Expression] = {}
        joined_from = {r.from_dataset.upper() for r in joins}
        for r in joins:
            source, target = self.dataset(r.from_dataset), self.dataset(r.to)
            froms, tos = self._keys(source, r.from_columns), self._keys(target, r.to_columns)
            if self._is_dictionary_key(target, tos) and target.name.upper() not in joined_from:
                dict_keys[target.name] = froms[0]  # nothing joins on from it
                continue
            on = exp.and_(*(exp.EQ(this=a, expression=b) for a, b in zip(froms, tos, strict=True)))
            sel = sel.join(self._table(target), on=on, join_type="left")
        return sel, dict_keys

    def _is_dictionary_key(self, target: OssieDataset, tos: list[exp.Expression]) -> bool:
        """Whether a relationship's key into ``target`` is its dictionary's one key: one
        row for each key by construction."""
        info = (self.catalog or {}).get(target.name)
        return bool(
            info
            and info.dictionary_key
            and len(tos) == 1
            and isinstance(tos[0], exp.Column)
            and tos[0].name.upper() == info.dictionary_key.upper()
        )

    def _one_row_per_key(self, r: OssieRelationship) -> bool:
        """Whether a dictionary holds one row for each key ``r`` joins on: the key is the
        dictionary's, or a superset of it."""
        target = self.dataset(r.to)
        info = (self.catalog or {}).get(target.name)
        if not (info and info.dictionary_key):
            return False
        return any(
            isinstance(t, exp.Column) and t.name.upper() == info.dictionary_key.upper()
            for t in self._keys(target, r.to_columns)
        )

    # --- repeated keys on the one side ----------------------------------------

    @staticmethod
    def _duplicate_sensitive(trees: list[exp.Expression]) -> bool:
        """Whether an aggregate of ``trees`` changes when an input row repeats."""
        return any(
            not _window_function(agg) and not _repeat_safe(agg)
            for tree in trees
            for agg in tree.find_all(exp.AggFunc)
        )

    def _subtrees(
        self, root: OssieDataset, joins: list[OssieRelationship], found: list[_Subtree]
    ) -> list[OssieRelationship]:
        """Split ``joins`` by the first relationship from ``root`` they go through. A
        group whose every key is a dictionary's one key is left as it is and returned
        (the dictionary has one row for each key); the rest are appended to ``found``.

        ClickHouse does not enforce the uniqueness of a key the model declares, so a
        dataset on the one side can hold a key twice, and a plain LEFT JOIN would
        count the root row once for each (docs/decisions/key-stored-twice.md)."""
        first_of: dict[str, OssieRelationship] = {}
        groups: dict[str, list[OssieRelationship]] = {}
        for r in joins:
            head = (
                r
                if r.from_dataset.upper() == root.name.upper()
                else first_of.get(r.from_dataset.upper())
            )
            if head is None:  # joins that do not start at the root: leave as they are
                return joins
            first_of[r.to.upper()] = head
            groups.setdefault(head.name, []).append(r)
        plain: list[OssieRelationship] = []
        for group in groups.values():
            unique = all(self._one_row_per_key(r) for r in group)
            if unique:
                plain += group
            else:
                found.append(_Subtree(group[0], group[1:], {r.to.upper() for r in group}))
        return sorted(plain, key=joins.index)

    def _dedup(self, sel: exp.Select, root: OssieDataset, subtrees: list[_Subtree]) -> exp.Select:
        """Join each subtree as ``(SELECT DISTINCT key, values ... ) AS first_dataset``.

        The values are what the question reads from the subtree: each largest
        expression of the question that reads columns of one subtree and nothing
        else (a dimension, a filter, an aggregate's argument, a part of an
        expression that also reads other datasets) is computed inside, so two rows of
        the subtree that differ in a column the question never reads collapse into one,
        and a root row counts once for each distinct value it matches. That is the plan of
        Layer 2 (apache/ossie#653 section 4; relational_semantics.md section 8.1: "a
        home row counts once in each group it matches after the filter"). A filter is
        carried as a value, never applied inside: a root row with no match has NULL for
        the subtree's columns and a filter may pass it (``region IS NULL``). ``__m`` is 1
        in a row that matched, so ``__m IS NULL`` finds the root rows that matched
        nothing, which take the expression over NULL columns (_unmatched). Over grouped
        rows (HAVING, the clause of a window) only a GROUP BY key is carried as a whole."""
        owner = {d: i for i, s in enumerate(subtrees) for d in s.datasets}
        values: list[dict[str, tuple[str, exp.Expression]]] = [{} for _ in subtrees]
        matched = [False] * len(subtrees)  # whether the SELECT reads __m of the subtree

        def owned(node: exp.Expression) -> set[int | None]:
            return {owner.get(c.table.upper()) for c in node.find_all(exp.Column)}

        group = sel.args.get("group")
        grouped = {e.sql("clickhouse") for e in group.expressions} if group else set()

        def before_grouping(node: exp.Expression) -> bool:
            """Whether ``node`` is evaluated on joined rows (WHERE, an aggregate's
            arguments and FILTER, GROUP BY) and not on grouped ones."""
            parent = node.parent
            while parent is not None:
                if isinstance(parent, exp.Where | exp.Group) or (
                    isinstance(parent, exp.AggFunc) and not _window_function(parent)
                ):
                    return True
                parent = parent.parent
            return False

        def carry(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Condition) or isinstance(node, exp.Subquery):
                return node
            own = owned(node)
            if len(own) != 1 or None in own or node.find(exp.AggFunc, exp.Window, exp.Select):
                return node
            # Over grouped rows (the SELECT list outside aggregates, HAVING, the clause of a
            # window) only a GROUP BY expression may be read, and as the same carried
            # expression, so ClickHouse finds it among the keys; the parts of anything
            # else are carried one by one.
            if not before_grouping(node) and node.sql("clickhouse") not in grouped:
                return node
            i = next(iter(own))
            name = values[i].setdefault(
                node.sql("clickhouse"), (f"__e{len(values[i]) + 1}", node.copy())
            )[0]
            table = self.dataset(subtrees[i].first.to).name
            if isinstance(node, exp.Column):
                # A root row that matched nothing gets what a plain LEFT JOIN gives it:
                # NULL, or the default of a type that cannot be NULL.
                return exp.column(name, table=table)
            matched[i] = True
            unmatched = node.copy().transform(
                lambda n: self._unmatched(n) if isinstance(n, exp.Column) else n
            )
            return exp.If(
                this=exp.Is(this=exp.column("__m", table=table), expression=exp.Null()),
                true=unmatched,
                false=exp.column(name, table=table),
            )

        sel = sel.transform(carry)
        for i, s in enumerate(subtrees):
            target = self.dataset(s.first.to)
            keys = self._keys(target, s.first.to_columns)
            cols = [k.as_(f"__k{j}") for j, k in enumerate(keys)]
            cols += [e.as_(name) for name, e in values[i].values()]
            if matched[i]:
                cols.append(exp.Literal.number(1).as_("__m"))
            inner = exp.select(*cols, copy=False).distinct().from_(self._table(target))
            inner, dict_keys = self._join_all(inner, s.rest)
            if dict_keys:
                inner = inner.transform(lambda n, d=dict_keys: self._dict_get(n, d))
            froms = self._keys(root, s.first.from_columns)
            on = exp.and_(
                *(
                    exp.EQ(this=a, expression=exp.column(f"__k{j}", table=target.name))
                    for j, a in enumerate(froms)
                )
            )
            sel = sel.join(inner.subquery(target.name), on=on, join_type="left")
        return sel

    def _refuse_blends(
        self,
        items: list[tuple[str, exp.Expression, bool]],
        root: OssieDataset,
        subtrees: list[_Subtree],
    ) -> None:
        """Refuse a question whose filter or metric (label, tree, whether a row filter)
        reads a subtree of _dedup with another dataset in one expression that could count
        a row of ``root`` once for each copy of a key (_blend)."""
        owner = _owners(subtrees)
        for label, tree, row_filter in items:
            found = _blend(_regroup(tree, owner), owner, row_filter)
            if found is None:
                continue
            i, shown, condition = found
            side = self.dataset(subtrees[i].first.to).name
            where = "one condition" if condition else "the argument of"
            head = (
                f"{label} reads {side} and another dataset in {where} {shown.sql()}, and "
                f"ClickHouse does not enforce the key of {side}: with a key stored twice, a "
                f"row of {root.name} would count once for each copy "
            )
            if condition:
                raise _BlendError(
                    head + "that passes it, and this version cannot answer it. Join the "
                    f"condition on {side} to the rest with AND, or ask for each part on its own"
                )
            raise _BlendError(
                head + "that gives the same argument, and this version cannot answer it. "
                f"Join the part on {side} with + or - (or * and / in SUM), put a condition on "
                f"{side} in a FILTER, or ask for each part on its own"
            )

    def _unmatched(self, col: exp.Column) -> exp.Expression:
        """What a column of a LEFT JOIN holds in a root row that matched nothing: NULL,
        typed as the column is when the catalog knows it (a function such as
        ``has`` refuses an untyped NULL), or its type's default when the type cannot be
        Nullable (an array, a map)."""
        ds = self._datasets.get(col.table.upper())
        info = (self.catalog or {}).get(ds.name) if ds else None
        kind = info.type_of(col.name) if info else None
        if not kind:
            return exp.Null()
        while kind.startswith("LowCardinality(") and kind.endswith(")"):
            kind = kind[len("LowCardinality(") : -1]
        literal = exp.Literal.string(kind)
        nullable_tuple = getattr(self.catalog, "nullable_tuple", False)
        if kind.startswith(_DEFAULT_TYPES) or (
            kind.startswith(_TUPLE_TYPES) and not nullable_tuple
        ):
            return exp.Anonymous(this="defaultValueOfTypeName", expressions=[literal])
        if not kind.startswith("Nullable("):
            literal = exp.Literal.string(f"Nullable({kind})")
        return exp.Anonymous(this="CAST", expressions=[exp.Null(), literal])

    def _split_filters(
        self,
        touched: dict[str, set[str]],
        rows: dict[str, exp.Expression],
        checked: list[tuple[str, Inputs]],
    ) -> tuple[OssieDataset, list[str], list[exp.Expression]] | None:
        """A question refused over the root of all it reads, planned from the root
        of its metrics, dimensions and aggregate filters instead, with each row
        filter that root cannot join to as a semi-join: the root, the row filters
        it joins to (labels), and the semi-joins. None when that root does not
        answer the metrics either, or every row filter joins to it.

        A row of the root stays when at least one row of the filtered dataset M
        references it and passes the filter, so no root row repeats; NOT stays
        inside (docs/decisions/semi-joins.md: #653
        rejects such a filter, section 5.3, and this planner answers it). Filters on M and on
        datasets M reaches go into one subquery, so one row of M passes all of
        them, as a joined row would. The subquery is a question over M, with its
        own joins, FINAL and dictionaries; M reaches the root along one path of
        relationships, one IN per step. An M that does not reach the root meets it
        at the first dataset S on its relationships that the root reaches (#69):
        the semi-join keeps S rows, and the root's rows that reach one of them.
        An M that meets no dataset the root reaches does so in the subquery of
        the one fact another filter names that M meets: orders by the categories
        of their lines' products, with a filter on the lines (#69 item 2). With
        no such filter, the fact is the one every fact that links them passes
        through (_link)."""
        core = {label: ds for label, ds in touched.items() if label not in rows}
        if not set().union(*core.values()):
            return None
        try:
            root = self._root(set().union(*core.values()))
            one_to_one = self._one_to_one(root.name)
            for label, inputs in checked:
                _check_rows(label, inputs, root.name, core, one_to_one)
        except PlanError:
            return None
        near = self._reachable(root.name)
        joined = [label for label in rows if touched[label] <= near]
        far = [label for label in rows if label not in joined]
        if not far:
            return None
        # A dataset both ways from the root is refused as ambiguous, as before.
        self._root(set().union(*core.values(), *(touched[label] for label in joined)))
        for label in far:
            if mixed := " and ".join(sorted(touched[label] & near)):
                other = " and ".join(sorted(touched[label] - near))
                raise PlanError(
                    f"{label} reads {mixed} and {other} in one expression, but {mixed} is "
                    f"read with each {root.name} row and {other} only searched for a row "
                    "that matches. Split it into one filter per side; an OR across the "
                    "sides cannot be split, and this version cannot answer it"
                )
        group: dict[str, list[str]] = {}  # M -> labels of its filters
        far_datasets = set().union(*(touched[label] for label in far))
        reach = {d: self._reachable(d) for d in far_datasets}
        tops = [
            d
            for d in sorted(far_datasets)
            if not any(d != u and d in reach[u] for u in far_datasets)
        ]
        for label in far:
            owners = [t for t in tops if touched[label] <= reach[t]]
            if not owners:
                raise PlanError(
                    f"{label} reads {' and '.join(sorted(touched[label]))} in one expression, "
                    "and no one of them reaches the others through many-to-one "
                    "relationships; split it into one filter per dataset"
                )
            if len(owners) > 1:
                raise PlanError(
                    f"{label} could be checked against rows of {' or '.join(owners)}, which "
                    f"the other filters name; this version cannot choose between them"
                )
            group.setdefault(owners[0], []).append(label)

        def parts(top: str) -> tuple[list[exp.Expression], set[str]]:
            labels = group[top]
            return [rows[lb] for lb in labels], set().union({top}, *(touched[lb] for lb in labels))

        # An M that meets the root nowhere rides in the subquery of a named fact it
        # meets, as a filter of the question over that fact's rows (#69 item 2).
        direct = [t for t in group if root.name in reach[t] or self._meets(t, near)]
        extra: dict[str, list[exp.Expression]] = {t: [] for t in direct}
        named: dict[str, str] = {}  # a fact no filter names -> the filter that needs it
        for top, labels in list(group.items()):
            if top in extra:
                continue
            # A fact linked for an earlier filter that reaches this M reads both
            # filters in its one subquery, as if the question named it. (A fact a
            # filter names reaches no M: M would not be a top.)
            if owner := next((t for t in direct if top in reach[t]), None):
                group[owner].extend(group.pop(top))
                continue
            carriers = [t for t in direct if self._meets(top, reach[t])]
            if not carriers:
                # The one fact every link between them passes through: orders by
                # their lines' products, where lines by brand are lines too.
                link = self._link(labels[0], top, root, near)
                reach[link] = self._reachable(link)
                direct.append(link)
                extra[link] = []
                if top in reach[link]:  # as if the question named it in a filter
                    group[link] = group.pop(top)
                    continue
                group[link] = []
                named[link] = labels[0]
                carriers = [link]
            if len(carriers) > 1:
                raise PlanError(
                    f"{labels[0]} reads {top}, which meets {root.name} through "
                    f"{' and '.join(sorted(carriers))}, which the other filters name: a "
                    f"{root.name} row could be kept through either, and this version cannot "
                    "choose between them"
                )
            carrier = self.dataset(carriers[0])
            filters, used = parts(top)
            on = self._through(labels[0], top, carrier, reach[carrier.name], filters, used)
            extra[carrier.name].append(on)
        semi_joins = []
        for top in direct:
            filters, used = parts(top)
            if root.name in reach[top]:
                semi_joins.append(
                    self._semi_join(root, self.dataset(top), filters, used, extra[top])
                )
            else:
                semi_joins.append(
                    self._through(
                        named.get(top) or group[top][0], top, root, near, filters, used, extra[top]
                    )
                )
        return root, joined, semi_joins

    def _through(
        self,
        label: str,
        m: str,
        at: OssieDataset,
        near: set[str],
        filters: list[exp.Expression],
        used: set[str],
        extra: list[exp.Expression] | None = None,
    ) -> exp.Expression:
        """``at``'s rows that reach a dataset S of ``near`` that ``m`` meets, kept
        when an ``m`` row that passes ``filters`` references their S row."""
        meets = self._meets(m, near)
        if len(meets) > 1:
            raise PlanError(
                f"{label} reads {m}, which meets {at.name} at {' and '.join(meets)}: a "
                f"{at.name} row could be kept through either, and this version cannot "
                "choose between them"
            )
        shared = self.dataset(meets[0])
        if problem := self._not_a_property(m, shared, at):
            raise PlanError(f"{label} {problem}")
        on_shared = self._semi_join(shared, self.dataset(m), filters, used, extra)
        return self._semi_join_to(at, shared, on_shared)

    def _link(self, label: str, m: str, root: OssieDataset, near: set[str]) -> str:
        """The fact that links ``m`` to ``root`` for a filter on ``m`` alone, when
        every fact that links them passes through it on each of its ways to both:
        its rows are rows of that fact (lines by brand are lines), so naming it
        only narrows the question. Two facts that link them apart, lines and
        refunds, are two meanings, and the question names one (#69 item 2)."""
        examples = self._links(m, root, near)
        linkers = self._linkers(m, root, near)
        found = [d for d in examples if all(self._passes(c, d, ts) for c, ts in linkers if c != d)]
        if len(found) == 1:
            return found[0]
        raise PlanError(self._unlinked(label, m, root, near, examples))

    def _linkers(self, m: str, root: OssieDataset, near: set[str]) -> list[tuple[str, list[str]]]:
        """Each fact that links ``m`` to ``root``, with the datasets it ends at on
        either way: the root or where it meets the datasets the root reaches, and
        ``m`` or where ``m`` meets the datasets it reaches."""
        out = []
        for d in self.model.datasets:
            if d.name in near or d.name == m:
                continue
            to_root = [root.name] if self._paths(d.name, root.name) else self._meets(d.name, near)
            reach = self._reachable(d.name)
            to_m = [m] if m in reach else self._meets(m, reach)
            if to_root and to_m:
                out.append((d.name, to_root + to_m))
        return out

    def _passes(self, start: str, via: str, targets: list[str]) -> bool:
        """Whether the one path from ``start`` to each of ``targets`` reads ``via``."""
        for t in targets:
            paths = self._paths(start, t)
            if len(paths) != 1 or via not in {self.dataset(r.to).name for r in paths[0]}:
                return False
        return True

    def _unlinked(
        self, label: str, m: str, root: OssieDataset, near: set[str], examples: dict[str, str]
    ) -> str:
        """Why a filter on ``m`` keeps no ``root`` row, with each fact a filter could
        name to link them (_links): which one does is a choice of meaning, so the
        question names it."""
        message = (
            f"{label} reads {m}, which {root.name} does not join to and which does not "
            f"reach {root.name} through many-to-one relationships"
        )
        if not examples:
            return message
        via = sorted(examples)
        which = "the dataset that links" if len(via) == 1 else "one of the datasets that link"
        return (
            f"{message}; name {which} them ({', '.join(via)}) in a filter, such as "
            f"{' or '.join(examples[d] for d in via)}"
        )

    def _links(self, m: str, root: OssieDataset, near: set[str]) -> dict[str, str]:
        """Each fact a filter could name to link ``m`` to ``root``, as _split_filters
        would plan it, with a filter on it that keeps every row of the fact that
        references a root row, or the dataset S it meets the root at."""
        examples = {}  # dataset -> a filter on it that drops none of its rows that count
        for d in self.model.datasets:
            if d.name in near or d.name == m:
                continue
            if paths := self._paths(d.name, root.name):
                ok = len(paths) == 1
            else:
                meets = self._meets(d.name, near)
                if len(meets) != 1:
                    continue
                paths = self._paths(d.name, meets[0])
                ok = self._one_way(d.name, root, self.dataset(meets[0]))
            reach = self._reachable(d.name)
            if m in reach:
                ok = ok and len(self._paths(d.name, m)) == 1
            else:
                meets = self._meets(m, reach)
                ok = ok and len(meets) == 1 and self._one_way(m, self.dataset(d.name), meets[0])
            if ok:
                examples[d.name] = f"{d.name}.{paths[0][0].from_columns[0]} IS NOT NULL"
        return examples

    def _one_way(self, m: str, at: OssieDataset, shared: OssieDataset | str) -> bool:
        """Whether _through plans ``m`` meeting ``at`` at ``shared``: one path each
        way, and ``m``'s rows properties of ``shared``'s."""
        shared = self.dataset(shared) if isinstance(shared, str) else shared
        return (
            len(self._paths(m, shared.name)) == 1
            and len(self._paths(at.name, shared.name)) == 1
            and self._not_a_property(m, shared, at) is None
        )

    def _meets(self, start: str, near: set[str]) -> list[str]:
        """Where the relationships from ``start`` first reach a dataset of ``near``:
        each path stops at the first one, so two entries are two meanings."""
        found: set[str] = set()
        seen, todo = {start.upper()}, [start]
        while todo:
            for r in self._outgoing(todo.pop()):
                to = self.dataset(r.to).name
                if to in near:
                    found.add(to)
                elif to.upper() not in seen:
                    seen.add(to.upper())
                    todo.append(to)
        return sorted(found)

    def _not_a_property(self, m: str, shared: OssieDataset, root: OssieDataset) -> str | None:
        """Why the rows of ``m`` are not properties of ``shared`` rows, or None.

        A key of each dataset on the path holds its columns to the next one:
        ``product_categories (product_id, category)`` lists properties of a
        product. ``returns (return_id)`` may be a fact of its own about a
        customer, and orders of customers with a return are not the orders
        returned, so it is refused rather than read that way (#69)."""
        for path in self._paths(m, shared.name):  # two are refused as ambiguous later
            for r in path:
                at = self.dataset(r.from_dataset)
                cols = {c.upper() for c in r.from_columns}
                if not any(cols <= {c.upper() for c in k} for k in declared_keys(at)):
                    columns = ", ".join(r.from_columns)
                    return (
                        f"reads {m}, which meets {root.name} only at {shared.name}, and no key "
                        f"of {at.name} holds {columns}: its rows may be facts of their own "
                        f"rather than properties of a {r.to} row, so {root.name} filtered by "
                        f"them could mean other rows. Relate {m} to {root.name}, or, if each "
                        f"{at.name} row describes its {r.to} row, declare a key of {at.name} "
                        f"that includes {columns}"
                    )
        return None

    def _semi_join_to(
        self, root: OssieDataset, shared: OssieDataset, on_shared: exp.Expression
    ) -> exp.Expression:
        """root's key IN the keys of ``shared`` rows that pass ``on_shared``, one IN
        per relationship on the one path from ``root`` to ``shared``. Each dataset on
        it is read, ``shared`` included: a key that matches no row keeps nothing, as
        a join from root would find nothing there."""
        paths = self._paths(root.name, shared.name)
        if len(paths) > 1:
            raise PlanError(_ambiguous(root.name, shared.name, paths))
        cond = on_shared
        for r in reversed(paths[0]):
            to = self.dataset(r.to)
            if problem := join_problem(r, to):
                raise PlanError(problem)
            sub = exp.select(*self._keys(to, r.to_columns)).from_(self._table(to)).where(cond)
            cond = _in(self._keys(self.dataset(r.from_dataset), r.from_columns), sub)
        return cond

    def _semi_join(
        self,
        root: OssieDataset,
        m: OssieDataset,
        filters: list[exp.Expression],
        used: set[str],
        extra: list[exp.Expression] | None = None,
    ) -> exp.Expression:
        """root's key IN the keys of ``m`` rows that pass ``filters`` and ``extra``,
        semi-joins of ``m``'s own, one IN per relationship on the one path from
        ``m`` to ``root``."""
        paths = self._paths(m.name, root.name)
        if len(paths) > 1:
            raise PlanError(_ambiguous(m.name, root.name, paths))
        path = paths[0]
        for r in path:
            if problem := join_problem(r, self.dataset(r.to)):
                raise PlanError(problem)
        first = path[0]
        sub = exp.select(*self._keys(m, first.from_columns))
        sub, dict_keys = self._from(sub, m, used)
        if filters:
            sub = sub.where(exp.and_(*filters))
        if dict_keys:
            sub = sub.transform(lambda n: self._dict_get(n, dict_keys))
        if extra:  # after the transform: their subqueries read datasets of their own
            sub = sub.where(exp.and_(*extra))
        for before, r in zip(path, path[1:], strict=False):
            at = self.dataset(r.from_dataset)
            sub = (
                exp.select(*self._keys(at, r.from_columns))
                .from_(self._table(at))
                .where(_in(self._keys(at, before.to_columns), sub))
            )
        return _in(self._keys(root, path[-1].to_columns), sub)

    def _written_aggregates(self, f: str) -> list[OssieMetric]:
        """Refuse a filter that writes aggregates the way a split or a mixed metric
        is written, such as ``SUM(a.x) / COUNT(DISTINCT b.k) > 5``: over one join,
        ``b`` would be counted over the rows ``a`` references. A metric of the
        model that does the same is answered by components; a filter names it.
        An aggregate that names no dataset (``COUNT(*)``) does not lift the check,
        and a metric the filter names beside a written aggregate counts with the
        datasets it aggregates (``total_sales > COUNT(DISTINCT b.k)``).

        Returns the metrics the filter names. plan() checks each against the root
        on its own, as a selected metric: in ``total_sales > store_count`` the
        filter as a whole reads the root, and one join would count stores over
        the sales rows."""
        tree = self._parse(f)
        metrics: dict[str, OssieMetric] = {}
        for col in tree.find_all(exp.Column):
            if m := self._named_metric(col):
                metrics.setdefault(m.name, m)
        # A bare name in an aggregate is a metric that reads no dataset (CURRENT_TIMESTAMP):
        # it names none, as COUNT(*) does.
        reads: Reads = [
            (
                agg,
                {self.dataset(t).name for t in args if t},
                {self.dataset(t).name for t in filtered if t},
            )
            for agg, args, filtered in _aggregate_reads(tree)
        ]
        if pair := _mixed(reads):
            raise PlanError(self._mixed_message(f"filter {f!r}", *pair))
        named = [(agg, next(iter(args))) for agg, args, filtered in reads if args]
        if all(len(args) == 1 and filtered <= args for _, args, filtered in reads if args):
            written = {home for _, home in named}
            if written:
                for m in metrics.values():
                    written.update(_homes(_aggregate_inputs(self._metric_tree(m, set()))))
            homes = sorted(written)
            if len(homes) > 1:
                raise PlanError(
                    f"filter {f!r} applies aggregates of {' and '.join(homes)} rows in one "
                    "expression, which one SELECT over joined rows would count over the rows "
                    "of only one of them; name a metric of the model that does this, or "
                    "filter on an aggregate of one dataset"
                )
        return list(metrics.values())

    def _merge_names(self, q: Query, having: list[str]) -> tuple[list[str], list[exp.Expression]]:
        """The metrics a stitched question answers, selected or only named in an
        aggregate filter, as the model spells them, and the filters parsed."""
        names = list(dict.fromkeys(self.metric(n).name for n in q.metrics))
        trees = []
        for f in having:
            tree = self._parse(f)
            self._time_strings(tree, metrics=True)
            for col in tree.find_all(exp.Column):
                m = self._named_metric(col)
                if m and m.name not in names:
                    names.append(m.name)  # filtered on, not selected
            trees.append(tree)
        return names, trees

    def _splits(self, q: Query, having: list[str]) -> bool:
        """Whether a metric of the question has components of different datasets."""
        names, _ = self._merge_names(q, having)
        return any(
            self._split(m, self._metric_tree(m, set())) is not None
            for m in (self.metric(n) for n in names)
        )

    def _stitch(self, q: Query, where: list[str], having: list[str]) -> exp.Select | None:
        """Metrics no one root answers together, each answered from its own root and
        merged on the dimensions (Layer 3, apache/ossie#653 sections 5.4 and 5.5);
        None if they form one group, or a group is refused even on its own.

        The units of a question are its metrics, and the components of a model's
        metric whose aggregates read different datasets (_split). Units are grouped
        by the datasets they aggregate, across metrics: the aggregates of a
        dataset, components or whole metrics, form one branch. Each group is
        planned as a question of its own with the dimensions and the row filters,
        which apply to every unit. The groups' rows are merged with UNION ALL
        and GROUP BY on the dimensions: every group of either side appears
        (S3), a NULL dimension value merges into one row as NULL-safe equality
        would, and no dimensions leave one row of totals.
        A unit missing from a group shows its value over no rows (_empty_value).
        A split metric's expression is then evaluated over the merged values of
        its components. Aggregate filters (#653 has no metric-value predicates) run on
        the merged rows and may name metrics, selected or not, and the question's
        dimensions. ``where`` and ``having`` are the question's filters as plan()
        split them."""
        names, having_trees = self._merge_names(q, having)
        metric_trees = {n: self._metric_tree(self.metric(n), set()) for n in names}
        units: list[_Unit] = []
        outer: dict[str, exp.Expression] = {}  # a split metric -> its expression over components
        for n, tree in metric_trees.items():
            m = self.metric(n)
            parts = self._split(m, tree)
            if parts is None:
                key = frozenset(_homes(_aggregate_inputs(tree)))
                carry = frozenset().union(*(c for _, c in self._carries([tree])))
                units.append(_Unit(n, tree, f"metric {n!r}", key, n, carry=carry))
                continue
            standing = []
            for node, home in parts:
                alias = f"__c{len(units)}"
                carry = self._carry(node)
                label = f"metric {n!r}"
                units.append(_Unit(alias, node.copy(), label, frozenset({home}), n, home, carry))
                standing.append((node, alias))
            for node, alias in standing:  # after the copies: the nodes are disjoint
                node.replace(exp.column(alias))
            outer[n] = tree
        # When every unit is answered over one root (they differ only in what they need from
        # the one side), an aggregate written in a filter over that root is a unit too, in
        # the SELECT of the units that need the same values; the filter is applied to the
        # merged rows. Over the rows of other datasets it is refused below.
        roots = {self._unit_root(u) for u in units}
        for f, tree in zip(having, having_trees, strict=True):
            for agg in [a for a in tree.find_all(exp.AggFunc) if not _window_function(a)]:
                node = _component_node(agg)
                comp = rewrite_tree(self.resolve(f, set(), parsed=node.copy()), self._zone)
                read = {
                    self.dataset(c.table).name
                    for c in comp.find_all(exp.Column)
                    if c.table.upper() in self._datasets
                }
                try:
                    home = self._root(read).name if read else None
                except PlanError:
                    home = None
                if home is None or roots != {home}:
                    continue  # refused below, as before
                alias = f"__w{len(units)}"
                carry = self._carry(comp)
                units.append(_Unit(alias, comp, f"filter {f!r}", frozenset(read), "", home, carry))
                node.replace(exp.column(alias))
        # By the datasets a unit aggregates and the values it needs from the one side.
        groups: dict[tuple[frozenset[str], frozenset[str]], list[_Unit]] = {}
        for u in units:
            groups.setdefault((u.key, u.carry), []).append(u)
        if len(groups) > 1 and (frozenset(), frozenset()) in groups:
            # A metric that reads no dataset (the current time) takes its value in any
            # group; a group of its own would add a row for every dimension value.
            loose = groups.pop((frozenset(), frozenset()))
            next(iter(groups.values())).extend(loose)
        branches: list[tuple[list[_Unit], exp.Select]] = []
        spines: dict[int, exp.Select] = {}  # a bridged group -> its spine (_bridge)
        if len(groups) == 1:
            # One group is answered by plan() unless its home does not reach a
            # dimension: then it is bridged, and merged with its spine below.
            members = next(iter(groups.values()))
            bridged = self._bridge(members, q.dimensions, where)
            if bridged is None:
                return None
            branches.append((members, bridged[0]))
            spines[0] = bridged[1]
        if not groups:
            return None
        splitting = bool(outer)
        for g, members in enumerate(groups.values()):
            if g in spines:
                continue
            try:
                branches.append(self._branch(members, q.dimensions, where))
            except PlanError as e:
                if (bridged := self._bridge(members, q.dimensions, where)) is not None:
                    branches.append((members, bridged[0]))
                    spines[g] = bridged[1]
                    continue
                if not splitting:
                    try:
                        self.plan(Query(tuple(u.alias for u in members)))
                    except PlanError:
                        return None  # refused alone too: the single-root error says why
                    # A dimension or row filter the group cannot reach: its own refusal
                    # names the datasets, the single-root one only says the facts do not join.
                    raise PlanError(
                        "metrics of different datasets are answered each on their own, and "
                        f"{', '.join(repr(u.alias) for u in members)} cannot be with this "
                        f"question's dimensions and row filters: {e}"
                    ) from None
                raise self._branch_error(members, units, q.dimensions, where, e) from None
        for n, tree in metric_trees.items():
            if n not in outer and tree.find(exp.Window):
                if len(roots) == 1:
                    why = (
                        "metrics that need other values of the repeated rows of a dataset "
                        "they join to (a FILTER on it, or its column in an argument), which "
                        "are answered apart"
                    )
                else:
                    why = "metrics that are answered from different datasets"
                raise PlanError(
                    f"metric {n!r} is a window over the merged rows of {why}; this version "
                    "cannot answer it, ask for it in its own question"
                )
        for f, tree in zip(having, having_trees, strict=True):
            if tree.find(exp.AggFunc):
                raise PlanError(
                    f"filter {f!r} applies an aggregate function to metrics answered from "
                    "different datasets; this version cannot answer it, name a metric instead"
                )
        empties = []
        for u in units:
            empty = _empty_value(u.tree)
            empties.append(self._over_no_rows(u) if empty is None else empty)

        dims = [self.field_ref(d)[1].name for d in q.dimensions]
        group_of = {u.alias: g for g, members in enumerate(groups.values()) for u in members}
        marked = [  # groups with a unit whose value over no rows is not NULL
            g
            for g, members in enumerate(groups.values())
            if any(not isinstance(empties[units.index(u)], exp.Null) for u in members)
        ]
        parts = []
        for g, (members, (_, sel)) in enumerate(zip(groups.values(), branches, strict=True)):
            planned = {e.alias.upper(): e.this for e in sel.expressions}
            cols = [planned[d.upper()].as_(d) for d in dims]
            cols += [
                (planned[u.alias.upper()] if u in members else exp.Null()).as_(f"__m{i}")
                for i, u in enumerate(units)
            ]
            cols += [
                (exp.Literal.number(1) if h == g else exp.Null()).as_(f"__b{h}") for h in marked
            ]
            part = sel.copy()
            part.set("expressions", cols)
            for arg in ("order", "settings"):
                part.set(arg, None)
            parts.append(part)
        for spine in spines.values():
            # The groups a bridged branch's rows make, each with no value of its units
            # (relational_semantics.md, section 8.1: a group whose input set is empty
            # is kept and takes the empty-input value).
            cols = [exp.column(d, table="__bridge").as_(d) for d in dims]
            cols += [exp.Null().as_(f"__m{i}") for i in range(len(units))]
            cols += [exp.Null().as_(f"__b{h}") for h in marked]
            parts.append(exp.select(*cols).from_(spine.copy().subquery("__bridge")))

        def merged(i: int) -> exp.Expression:
            u = units[i]
            value = exp.AnyValue(this=exp.column(f"__m{i}"))  # any() skips NULLs
            g = group_of[u.alias]
            if g not in marked or isinstance(empties[i], exp.Null):
                return value
            present = exp.GT(
                this=exp.Count(this=exp.column(f"__b{g}")), expression=exp.Literal.number(0)
            )
            return exp.If(this=present, true=value, false=empties[i])

        by_alias = {u.alias: merged(i) for i, u in enumerate(units)}

        def component(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column) and not node.table and node.name in by_alias:
                return by_alias[node.name].copy()
            return node

        values = {n: outer[n].transform(component) if n in outer else by_alias[n] for n in names}
        selects = [exp.column(d).as_(d) for d in dims]
        selects += [values[self.metric(n).name].copy().as_(self.metric(n).name) for n in q.metrics]
        sel = exp.select(*selects).from_(exp.union(*parts, distinct=False).subquery("stitched"))
        if dims:
            sel = sel.group_by(*(exp.column(d) for d in dims))
        if having_trees:
            by_ref = {ref.upper(): name for ref, name in zip(q.dimensions, dims, strict=True)}

            def to_merged(node: exp.Expression) -> exp.Expression:
                if isinstance(node, exp.Column) and not node.table and node.name in by_alias:
                    return by_alias[node.name].copy()  # an aggregate written in the filter
                if isinstance(node, exp.Column) and (m := self._named_metric(node)):
                    return values[m.name].copy()
                if isinstance(node, exp.Column):
                    ref = f"{node.table}.{node.name}"
                    if ref.upper() not in by_ref:
                        raise PlanError(f"filter column {ref} is not a dimension of this query")
                    return exp.column(by_ref[ref.upper()])
                return node

            conditions = [rewrite_tree(t, self._zone).transform(to_merged) for t in having_trees]
            sel = sel.having(exp.and_(*conditions))
        if order := self._order(q, selects):
            sel = sel.order_by(*order)
        if q.limit is not None:
            sel = sel.limit(q.limit)
        sel.set("settings", _settings())
        return sel

    def _branch(
        self, members: list[_Unit], dimensions: tuple[str, ...], where: list[str]
    ) -> tuple[list[_Unit], exp.Select]:
        """A branch: ``members`` planned as a question of their own."""
        stand_in = {u.alias: (u.label, u.tree) for u in members if u.component}
        names = tuple(u.alias for u in members)
        return members, self._plan(Query(names, dimensions, tuple(where)), stand_in, nested=True)

    def _bridge(
        self, members: list[_Unit], dimensions: tuple[str, ...], where: list[str]
    ) -> tuple[exp.Select, exp.Select] | None:
        """A branch whose home H does not reach a dimension's dataset, answered over
        the question's rows as relational_semantics.md section 8.1 defines it: each
        H row counts once in each group that a row of the question derives from it,
        and never in another. None when that does not apply, or it cannot be planned.

        The spine is the question with the dimensions, H's key and the row filters
        and no metric: the distinct (dimensions, key) pairs over the root that reaches
        H and the dimensions, with its joins, FINAL, dictionaries and semi-joins.
        The branch is the members planned over H, inner-joined to the spine on the
        key and grouped by its dimensions: an H row joins once per pair, so once per
        group, and a NULL or orphan key joins nothing. Only whole metrics whose
        aggregates read H and nothing from the one side; a metric of components keeps
        the reachability rule of apache/ossie#653 section 5.3
        (docs/decisions/one-side-by-many-side.md). Not a window metric: _stitch
        cannot rank merged rows, and the single-root refusal says why it is asked."""
        if not dimensions or any(
            u.component or u.carry or len(u.key) != 1 or u.tree.find(exp.Window) for u in members
        ):
            return None
        if len({u.key for u in members}) != 1:
            return None
        home = self.dataset(next(iter(members[0].key)))
        reach = self._reachable(home.name)
        if all(self.field_ref(d)[0].name in reach for d in dimensions):
            return None  # not this case: plan() answers it or says why not
        if not declared_keys(home):
            return None
        try:
            spine = self._plan(Query((), dimensions, tuple(where)), nested=True, keys_of=home)
            rows = self._plan(Query(tuple(u.alias for u in members)), nested=True)
        except PlanError:
            return None
        for sel in (spine, rows):
            for arg in ("order", "limit", "settings"):
                sel.set(arg, None)
        keys = self._keys(home, list(declared_keys(home)[0]))
        on = exp.and_(
            *(
                exp.EQ(this=k, expression=exp.column(f"__k{i}", table="__bridge"))
                for i, k in enumerate(keys)
            )
        )
        dims = [self.field_ref(d)[1].name for d in dimensions]
        rows = rows.join(spine.copy().subquery("__bridge"), on=on, join_type="inner")
        rows = rows.select(*(exp.column(d, table="__bridge").as_(d) for d in dims))
        rows = rows.group_by(*(exp.column(d, table="__bridge") for d in dims))
        return rows, spine

    def _branch_error(
        self,
        members: list[_Unit],
        units: list[_Unit],
        dimensions: tuple[str, ...],
        where: list[str],
        error: PlanError,
    ) -> PlanError:
        """Why a branch of a question with split metrics is refused: the unit that
        cannot be with the dimensions and row filters, and the dimension or filter
        that it cannot reach (#653 section 8 names both)."""
        for u in members:
            try:
                self._branch([u], dimensions, where)
            except PlanError as e:
                first = e
                break
        else:
            return error
        try:
            self._branch([u], (), [])
        except PlanError as alone:
            return alone  # refused with nothing else asked: its own error says why
        if not u.component:
            return PlanError(
                "metrics of different datasets are answered each on their own, and "
                f"{u.alias!r} cannot be with this question's dimensions and row filters: {first}"
            )
        what = f"{u.label}, whose aggregate {u.tree.sql('clickhouse')} reads {u.home} rows,"
        reach = self._reachable(u.home)
        for d in dimensions:
            try:
                self._branch([u], (d,), [])
            except PlanError as e_d:
                ds = self.field_ref(d)[0].name
                if ds in reach:
                    return PlanError(f"dimension {d!r} cannot be with {what}: {e_d}")
                return PlanError(
                    f"dimension {d!r} cannot be with {what} because {u.home} does not reach "
                    f"{ds} through many-to-one relationships.{self._other_home(u, units, ds)}"
                )
        for f in where:
            try:
                self._branch([u], (), [f])
            except PlanError as e_f:
                return PlanError(f"row filter {f!r} cannot be with {what}: {e_f}")
        return PlanError(f"dimensions and row filters cannot be with {what}: {first}")

    def _other_home(self, u: _Unit, units: list[_Unit], target: str) -> str:
        """What would answer a question by ``target``, or "" when nothing the planner
        can see does. Only a count of the distinct keys of ``u.home`` has an
        equivalent among a dataset's metrics, which read that dataset's own fields:
        COUNT(DISTINCT <the field of dataset R that references the key>), for a
        dataset R that reaches ``target`` and that the metric's other aggregates
        read, through one visible relationship from R to ``u.home``."""
        agg = u.tree
        if not (isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct)):
            return ""
        if len(agg.this.expressions) != 1:
            return ""
        counted = agg.this.expressions[0].sql()
        homes = sorted({o.home for o in units if o.metric == u.metric and o.component} - {u.home})
        for r in homes:
            if target not in self._reachable(r):
                continue
            link = [rel for rel in self._outgoing(r) if rel.to.upper() == u.home.upper()]
            if len(link) != 1 or len(link[0].from_columns) != 1:
                continue
            keys = self._keys(self.dataset(u.home), link[0].to_columns)
            if len(keys) == 1 and keys[0].sql() == counted:
                return (
                    f" To ask for it by {target}, define a metric of dataset {r}, such as "
                    f"COUNT(DISTINCT {link[0].from_columns[0]}): it counts the {u.home} rows "
                    f"that {r} rows reference."
                )
        return ""

    def _order(self, q: Query, selects: list[exp.Expression]) -> list[exp.Expression]:
        """ORDER BY over selected aliases; names are metrics, dataset.field, or field names.

        Without order_by, the first metric descending. Then each dimension not
        ordered yet, ascending: rows are grouped by the dimensions, so ties are
        broken and a limit keeps the same rows on every run."""
        aliases = {e.alias.upper(): e.alias for e in selects}
        for ref in q.dimensions:  # allow the dataset.field spelling too
            aliases[ref.upper()] = self.field_ref(ref)[1].name
        order_by = q.order_by or ((f"{q.metrics[0]} desc",) if q.metrics and q.dimensions else ())
        out = []
        for item in order_by:
            name, _, direction = item.partition(" ")
            desc = direction.strip().upper() == "DESC"
            if direction and not desc and direction.strip().upper() != "ASC":
                raise PlanError(f"order_by item {item!r}: use 'name', 'name asc' or 'name desc'")
            alias = aliases.get(name.upper())
            if alias is None:
                raise PlanError(
                    f"order_by {name!r} is not a selected metric or dimension"
                    f"{suggest(name, dict.fromkeys(aliases.values()))}"
                )
            out.append(exp.Ordered(this=exp.column(alias), desc=desc, nulls_first=False))
        ordered = {o.this.name.upper() for o in out}
        for ref in q.dimensions:
            alias = self.field_ref(ref)[1].name
            if alias.upper() not in ordered:
                ordered.add(alias.upper())
                out.append(exp.Ordered(this=exp.column(alias), desc=False, nulls_first=False))
        return out

    def _over_no_rows(self, unit: _Unit) -> exp.Expression:
        """A unit's value over no rows as ClickHouse computes it: the metric (or the
        component) planned on its own, as a scalar subquery over WHERE FALSE,
        which reads nothing. It runs under the outer query's SETTINGS."""
        sel = self._plan(
            Query((unit.alias,)),
            {unit.alias: (unit.label, unit.tree)} if unit.component else None,
            nested=True,
        )
        for arg in ("order", "limit", "settings"):
            sel.set(arg, None)
        return sel.where(exp.false()).subquery()

    def _table(self, ds: OssieDataset) -> exp.Expression:
        table = source_table(ds.source).as_(ds.name)
        info = (self.catalog or {}).get(ds.name)
        return exp.Final(this=table) if info and info.dedup else table

    def _dict_get(self, node: exp.Expression, dict_keys: dict[str, exp.Expression]):
        """dataset.column on a dictionary dataset -> dictGetOrNull('db.dict', 'column', key).

        The key column is not an attribute, so dictGet* cannot read it: emit the
        key itself, NULL when the dictionary lacks it, as a LEFT JOIN would."""
        if isinstance(node, exp.Column) and node.table in dict_keys:
            ds = self.dataset(node.table)
            name = exp.Literal.string(source_name(ds.source))
            key = dict_keys[node.table].copy()
            if node.name.upper() == (self.catalog[ds.name].dictionary_key or "").upper():
                has = exp.Anonymous(this="dictHas", expressions=[name, key])
                return exp.If(this=has, true=key.copy(), false=exp.Null())
            return exp.Anonymous(
                this="dictGetOrNull", expressions=[name, exp.Literal.string(node.name), key]
            )
        return node

    def sql(self, q: Query, pretty: bool = False) -> str:
        return self.plan(q).sql(dialect="clickhouse", pretty=pretty)

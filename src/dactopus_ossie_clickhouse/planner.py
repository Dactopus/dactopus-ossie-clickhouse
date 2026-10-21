"""Turn a semantic query (metrics, dimensions, filters) into one ClickHouse SELECT.

Minimum planner: one root dataset plus many-to-one joins along the one path
to each dataset the query touches. Metrics that no one root can answer together
are each answered from their own root and merged on the dimensions
(stitching). Anything else is rejected with a message that names the
nearest supported thing.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

from ossie import OssieDataset, OssieDocument, OssieField, OssieRelationship
from sqlglot import exp

from dactopus_ossie_clickhouse.model import covers_key, join_problem
from dactopus_ossie_clickhouse.translate import (
    outside_spec,
    parse,
    pick_expression,
    rewrite_tree,
    untranslatable,
)


class PlanError(ValueError):
    pass


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


class Catalog(dict[str, TableInfo]):
    """What the executor learned from the server: TableInfo by dataset name, and
    ``aggregate``, whether a function outside the spec is an aggregate (None when
    system.functions could not be read)."""

    def __init__(self, tables=(), aggregate: Callable[[str], bool] | None = None):
        super().__init__(tables)
        self.aggregate = aggregate


@dataclass(frozen=True)
class Query:
    metrics: tuple[str, ...] = ()
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
    (none for COUNT(*)).

    A column counts for its nearest aggregate only, so SUM(SUM(x)) OVER () reads x
    once, and a FILTER (WHERE ...) column for the aggregate it filters. The function
    of an OVER clause reads grouped rows, not joined ones, and PARTITION BY / ORDER BY
    columns are grouping keys: neither counts."""
    found = {id(a): (a, set()) for a in tree.find_all(exp.AggFunc) if not _window_function(a)}
    for col in tree.find_all(exp.Column):
        node = col.parent
        while node is not None and id(node) not in found:
            filtered = isinstance(node, exp.Filter) and id(node.this) in found
            node = node.this if filtered else node.parent
        if node is not None:
            found[id(node)][1].add(col.table)
    return list(found.values())


def _window_function(agg: exp.AggFunc) -> bool:
    parent = agg.parent
    while isinstance(parent, exp.RespectNulls | exp.IgnoreNulls | exp.Filter):
        parent = parent.parent
    return isinstance(parent, exp.Window)


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

    One that names an unknown field or reads no column, and one with a
    duplicate-sensitive aggregate over each of two datasets: whichever is the
    root, the other one's rows repeat. Untranslatable metrics are reported by
    untranslatable()."""
    planner = Planner(model, catalog)
    skip = set(untranslatable(model)[1])
    out = {}
    for m in model.metrics or []:
        if m.name in skip:
            continue
        try:
            # Through the planner, so names are the model's spelling, as in plan().
            inputs = _aggregate_inputs(planner._metric_tree(m, set()))
        except PlanError as e:
            out[m.name] = str(e)
            continue
        except ValueError:
            continue  # reads an untranslatable field, reported as such
        homes = _homes(inputs)
        refused = [_repeated(inputs, root, planner._one_to_one(root)) for root in homes]
        if homes and all(refused):
            aggs = ", ".join(r[0][0].sql("clickhouse") for r in refused)
            out[m.name] = (
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
    FILTER that matches nothing) is NULL, not 0; counts stay 0."""
    return [
        exp.EQ(this=exp.var(name), expression=exp.Literal.number(1))
        for name in ("join_use_nulls", "aggregate_functions_null_for_empty")
    ]


def _empty_value(tree: exp.Expression) -> exp.Expression | None:
    """A metric over no rows: what a stitched group missing from the metric's fact
    shows. #246 §6.11.5: NULL for SUM, 0 for COUNT, 0 for COALESCE(SUM(x), 0).

    None for a metric with an aggregate outside the spec: ClickHouse gives 0 for
    countIf and uniq, NULL for sumIf, [] for groupArray, and system.functions
    does not say which; Planner._over_no_rows asks ClickHouse instead."""
    if any(agg.meta.get("outside_spec") for agg in tree.find_all(exp.AggFunc)):
        return None

    def empty(node: exp.Expression) -> exp.Expression:
        agg = node.this if isinstance(node, exp.Filter) else node  # COUNT(x) FILTER (...)
        if isinstance(agg, exp.AggFunc):
            counts = isinstance(agg, exp.Count | exp.CountIf | exp.ApproxDistinct)
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


def _suggest(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=3, cutoff=0.5)
    return f" (did you mean: {', '.join(close)}?)" if close else ""


@dataclass
class Planner:
    model: OssieDocument
    catalog: Catalog | None = None  # from Executor.introspect(); None means plain tables
    _datasets: dict[str, OssieDataset] = field(init=False)
    _fields: dict[str, dict[str, OssieField]] = field(init=False)
    _metrics: dict[str, object] = field(init=False)

    def __post_init__(self):
        # Spec: unquoted identifiers are case-insensitive; index by upper case,
        # but always emit the physical names as written in the model.
        self._datasets = {d.name.upper(): d for d in self.model.datasets}
        self._fields = {
            d.name.upper(): {f.name.upper(): f for f in d.fields or []} for d in self.model.datasets
        }
        self._metrics = {m.name.upper(): m for m in self.model.metrics or []}
        self._out: dict[str, list[OssieRelationship]] = {}
        for r in self.model.relationships or []:
            if r.to.upper() in self._datasets:
                self._out.setdefault(r.from_dataset.upper(), []).append(r)

    # --- resolution ---------------------------------------------------------

    def dataset(self, name: str) -> OssieDataset:
        ds = self._datasets.get(name.upper())
        if ds is None:
            raise PlanError(
                f"unknown dataset {name!r}{_suggest(name, (d.name for d in self.model.datasets))}"
            )
        return ds

    def metric(self, name: str):
        m = self._metrics.get(name.upper())
        if m is None:
            names = (x.name for x in self.model.metrics or [])
            raise PlanError(f"unknown metric {name!r}{_suggest(name, names)}")
        return m

    def field_ref(self, ref: str) -> tuple[OssieDataset, OssieField]:
        if ref.count(".") != 1:
            raise PlanError(f"field reference must be dataset.field, got {ref!r}")
        ds_name, f_name = ref.split(".")
        ds = self.dataset(ds_name)
        f = self._fields[ds.name.upper()].get(f_name.upper())
        if f is None:
            names = (f"{ds.name}.{x.name}" for x in ds.fields or [])
            raise PlanError(f"unknown field {ref!r}{_suggest(ref, names)}")
        return ds, f

    def _parse(self, expression: str) -> exp.Expression:
        """parse(), with a call of a function outside the spec that the server
        says is an aggregate (countIf, uniqExactOrNull) typed as one, so every
        check on aggregates covers it. Without a catalog it stays a plain call."""
        tree = parse(expression)
        aggregate = self.catalog.aggregate if self.catalog is not None else None
        if aggregate is None:
            return tree

        def mark(node: exp.Expression) -> exp.Expression:
            # A quoted name stays a plain call: AnonymousAggFunc prints it unquoted.
            if outside_spec(node) and isinstance(node.this, str) and aggregate(node.name):
                agg = exp.AnonymousAggFunc(this=node.this, expressions=node.expressions)
                agg.meta["outside_spec"] = True
                return agg
            return node

        return tree.transform(mark)

    def resolve(self, expression: str, used: set[str], metrics: bool = False) -> exp.Expression:
        """Parse an expression, inline `dataset.field` references, qualify columns.

        With ``metrics``, a bare name that is a metric inlines that metric's
        expression (filters may say ``total_sales > 1000``).
        Records the datasets touched in ``used`` (by model name).
        """
        try:
            tree = self._parse(expression)
        except ValueError as e:
            raise PlanError(str(e)) from e

        def inline(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            if not node.table:
                if metrics and node.name.upper() in self._metrics:
                    return self._metric_tree(self.metric(node.name), used)
                hint = ""
                if metrics:
                    names = [x.name for x in self.model.metrics or []] + [
                        f"{d.name}.{x.name}" for d in self.model.datasets for x in d.fields or []
                    ]
                    hint = f"; use dataset.field or a metric name{_suggest(node.name, names)}"
                raise PlanError(f"unqualified column {node.name!r} in {expression!r}{hint}")
            ds, f = self.field_ref(f"{node.table}.{node.name}")
            used.add(ds.name)
            return self._field_expr(ds, f)

        return tree.transform(inline)

    def _metric_tree(self, m, used: set[str]) -> exp.Expression:
        """A metric's expression, resolved and rewritten for ClickHouse.

        Refuses an aggregate that reads no column (COUNT(*)) unless the metric's
        other aggregates name the one dataset whose rows it counts: otherwise it
        would count rows of whichever dataset a question is answered over."""
        tree = rewrite_tree(self.resolve(pick_expression(m.expression), used))
        if len(self.model.datasets) > 1:
            inputs = _aggregate_inputs(tree)
            unnamed = [agg for agg, ds in inputs if not ds]
            if unnamed and len(_homes(inputs)) != 1:
                raise PlanError(
                    f"metric {m.name!r} uses {unnamed[0].sql('clickhouse')}, which reads no "
                    "column, so it would count rows of whichever dataset a question is "
                    "answered over; count a column of the dataset it means, such as "
                    "COUNT(dataset.key)"
                )
        return tree

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

    # --- joins --------------------------------------------------------------

    def _outgoing(self, name: str) -> list[OssieRelationship]:
        """Relationships from ``name`` to a dataset of the model."""
        return self._out.get(name.upper(), [])

    def _paths(self, start: str, target: str) -> list[tuple[OssieRelationship, ...]]:
        """Up to two acyclic paths of relationships from ``start`` to ``target``
        (apache/ossie#246 §6.9 rule 4); a second one already makes the join ambiguous."""
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

        apache/ossie#246 §6.9: the one path of many-to-one relationships to each
        dataset, ambiguous when there are two. Datasets on a path that the query
        does not name are joined too (§6.2, datasets involved in a query)."""
        depth: dict[str, tuple[int, OssieRelationship]] = {}
        for name in sorted(used - {root.name}):
            paths = self._paths(root.name, name)
            if len(paths) > 1:
                shown = " and ".join(
                    " -> ".join(repr(r.name) for r in p)
                    for p in sorted(paths, key=lambda p: (len(p), [r.name for r in p]))
                )
                raise PlanError(
                    f"ambiguous join from {root.name!r} to {name!r}: relationship paths "
                    f"{shown}; this version cannot choose between them"
                )
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
        if not q.metrics and not q.dimensions:
            raise PlanError("a query needs at least one metric or dimension")
        # Query item ("metric 'x'") -> datasets it reads: picks the root, explains it.
        touched: dict[str, set[str]] = {}
        checked: list[tuple[str, Inputs]] = []  # aggregate expressions
        selects: list[exp.Expression] = []
        group: list[exp.Expression] = []
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
            e = rewrite_tree(self._field_expr(ds, f))
            selects.append(e.as_(alias(f.name)))
            group.append(e.copy())

        for name in q.metrics:
            m = self.metric(name)
            label = f"metric {m.name!r}"
            tree = self._metric_tree(m, touched.setdefault(label, set()))
            checked.append((label, _aggregate_inputs(tree)))
            selects.append(tree.as_(alias(m.name)))

        where: list[exp.Expression] = []
        having: list[exp.Expression] = []  # filters over aggregates or metric names
        row_filters: list[str] = []
        aggregate_filters: list[str] = []
        # Every part is rewritten once, before inspecting: APPROX_PERCENTILE is a
        # plain function name to SQLGlot and an aggregate only after rewrite.
        grouped = {e.sql() for e in group}
        for f in q.filters:
            label = f"filter {f!r}"
            tree = self.resolve(f, touched.setdefault(label, set()), metrics=True)
            tree = rewrite_tree(tree)
            if tree.find(exp.Window):
                raise PlanError(
                    f"filter {f!r} uses a window function; a window metric cannot be "
                    "filtered in the same SELECT, select it and filter the rows instead"
                )
            if not tree.find(exp.AggFunc):
                where.append(tree)
                row_filters.append(f)
                continue
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
            aggregate_filters.append(f)
            checked.append((label, _aggregate_inputs(tree)))

        used = set().union(*touched.values())
        try:
            root = self._root(used)
            one_to_one = self._one_to_one(root.name)
            for label, inputs in checked:
                _check_rows(label, inputs, root.name, touched, one_to_one)
        except PlanError:
            stitched = self._stitch(q, row_filters, aggregate_filters)
            if stitched is not None:
                return stitched
            raise
        sel = exp.select(*selects).from_(self._table(root))
        dict_keys: dict[str, exp.Expression] = {}  # dictionary dataset -> key expression
        joins = self._joins(root, used)
        joined_from = {r.from_dataset.upper() for r in joins}
        for r in joins:
            source, target = self.dataset(r.from_dataset), self.dataset(r.to)
            info = (self.catalog or {}).get(target.name)
            if (
                info
                and info.dictionary_key
                and [c.upper() for c in r.to_columns] == [info.dictionary_key.upper()]
                and target.name.upper() not in joined_from  # nothing joins on from it
            ):
                dict_keys[target.name] = exp.column(r.from_columns[0], table=source.name)
                continue
            on = exp.and_(
                *(
                    exp.EQ(
                        this=exp.column(a, table=source.name),
                        expression=exp.column(b, table=target.name),
                    )
                    for a, b in zip(r.from_columns, r.to_columns, strict=True)
                )
            )
            sel = sel.join(self._table(target), on=on, join_type="left")
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
        if dict_keys:
            sel = sel.transform(lambda n: self._dict_get(n, dict_keys))
        sel.set("settings", _settings())
        return sel

    def _stitch(self, q: Query, where: list[str], having: list[str]) -> exp.Select | None:
        """Metrics no one root answers together, each answered from its own root and
        merged on the dimensions (#246 §6.2 step A.7, §6.6); None if they form one
        group, or a group is refused even on its own.

        Metrics are grouped by the datasets their aggregates read, and each group is
        planned as a question of its own with the dimensions and the row filters,
        which apply to every metric (#246 §6.2 step A.6.1). The groups' rows are
        merged with UNION ALL and GROUP BY on the dimensions: every group of either
        side appears (§6.6 row 3), a NULL dimension value merges into one row as
        NULL-safe equality would, and no dimensions leave one row of totals (§6.6
        row 4). A metric missing from a group shows its value over no rows
        (_empty_value). Aggregate filters run after the merge (step A.9) and may
        name metrics, selected or not, and the question's dimensions. ``where`` and
        ``having`` are the question's filters as plan() split them."""
        names = list(dict.fromkeys(self.metric(n).name for n in q.metrics))
        having_trees = []
        for f in having:
            tree = self._parse(f)
            for col in tree.find_all(exp.Column):
                if not col.table and self.metric(col.name).name not in names:
                    names.append(self.metric(col.name).name)  # filtered on, not selected
            having_trees.append(tree)

        metric_trees = {n: self._metric_tree(self.metric(n), set()) for n in names}
        groups: dict[frozenset[str], list[str]] = {}
        for n, tree in metric_trees.items():
            groups.setdefault(frozenset(_homes(_aggregate_inputs(tree))), []).append(n)
        if len(groups) < 2:
            return None
        branches = []
        for members in groups.values():
            try:
                branch = Query(tuple(members), q.dimensions, tuple(where))
                branches.append((set(members), self.plan(branch)))
            except PlanError as e:
                try:
                    self.plan(Query(tuple(members)))
                except PlanError:
                    return None  # refused alone too: the single-root error says why
                # A dimension or row filter the group cannot reach: its own refusal
                # names the datasets, the single-root one only says the facts do not join.
                raise PlanError(
                    "metrics of different datasets are answered each on their own, and "
                    f"{', '.join(map(repr, members))} cannot be with this question's "
                    f"dimensions and row filters: {e}"
                ) from None
        for n, tree in metric_trees.items():
            if tree.find(exp.Window):
                raise PlanError(
                    f"metric {n!r} is a window over the merged rows of metrics that are "
                    "answered from different datasets; this version cannot answer it, "
                    "ask for it in its own question"
                )
        empties = {}
        for n in names:
            empty = _empty_value(metric_trees[n])
            empties[n] = self._over_no_rows(n) if empty is None else empty
        for f, tree in zip(having, having_trees, strict=True):
            if tree.find(exp.AggFunc):
                raise PlanError(
                    f"filter {f!r} applies an aggregate function to metrics answered from "
                    "different datasets; this version cannot answer it, name a metric instead"
                )

        dims = [self.field_ref(d)[1].name for d in q.dimensions]
        marked = [  # groups with a metric whose value over no rows is not NULL
            g
            for g, (members, _) in enumerate(branches)
            if any(not isinstance(empties[n], exp.Null) for n in members)
        ]
        parts = []
        for g, (members, sel) in enumerate(branches):
            planned = {e.alias.upper(): e.this for e in sel.expressions}
            cols = [planned[d.upper()].as_(d) for d in dims]
            cols += [
                (planned[n.upper()] if n in members else exp.Null()).as_(f"__m{i}")
                for i, n in enumerate(names)
            ]
            cols += [
                (exp.Literal.number(1) if h == g else exp.Null()).as_(f"__b{h}") for h in marked
            ]
            part = sel.copy()
            part.set("expressions", cols)
            for arg in ("order", "settings"):
                part.set(arg, None)
            parts.append(part)

        def merged(i: int, n: str) -> exp.Expression:
            value = exp.AnyValue(this=exp.column(f"__m{i}"))  # any() skips NULLs
            g = next(g for g, (members, _) in enumerate(branches) if n in members)
            if g not in marked or isinstance(empties[n], exp.Null):
                return value
            present = exp.GT(
                this=exp.Count(this=exp.column(f"__b{g}")), expression=exp.Literal.number(0)
            )
            return exp.If(this=present, true=value, false=empties[n])

        values = {n: merged(i, n) for i, n in enumerate(names)}
        selects = [exp.column(d).as_(d) for d in dims]
        selects += [values[self.metric(n).name].copy().as_(self.metric(n).name) for n in q.metrics]
        sel = exp.select(*selects).from_(exp.union(*parts, distinct=False).subquery("stitched"))
        if dims:
            sel = sel.group_by(*(exp.column(d) for d in dims))
        if having_trees:
            by_ref = {ref.upper(): name for ref, name in zip(q.dimensions, dims, strict=True)}

            def to_merged(node: exp.Expression) -> exp.Expression:
                if isinstance(node, exp.Column) and not node.table:
                    return values[self.metric(node.name).name].copy()
                if isinstance(node, exp.Column):
                    ref = f"{node.table}.{node.name}"
                    if ref.upper() not in by_ref:
                        raise PlanError(f"filter column {ref} is not a dimension of this query")
                    return exp.column(by_ref[ref.upper()])
                return node

            conditions = [rewrite_tree(t).transform(to_merged) for t in having_trees]
            sel = sel.having(exp.and_(*conditions))
        if order := self._order(q, selects):
            sel = sel.order_by(*order)
        if q.limit is not None:
            sel = sel.limit(q.limit)
        sel.set("settings", _settings())
        return sel

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
                    f"{_suggest(name, dict.fromkeys(aliases.values()))}"
                )
            out.append(exp.Ordered(this=exp.column(alias), desc=desc, nulls_first=False))
        ordered = {o.this.name.upper() for o in out}
        for ref in q.dimensions:
            alias = self.field_ref(ref)[1].name
            if alias.upper() not in ordered:
                ordered.add(alias.upper())
                out.append(exp.Ordered(this=exp.column(alias), desc=False, nulls_first=False))
        return out

    def _over_no_rows(self, name: str) -> exp.Expression:
        """A metric's value over no rows as ClickHouse computes it: the metric
        planned on its own, as a scalar subquery over WHERE FALSE, which reads
        nothing. It runs under the outer query's SETTINGS."""
        sel = self.plan(Query((name,)))
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

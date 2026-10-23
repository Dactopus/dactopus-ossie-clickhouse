"""Turn a semantic query (metrics, dimensions, filters) into one ClickHouse SELECT.

Minimum planner: one root dataset plus many-to-one joins along the one path
to each dataset the query touches. A row filter on a dataset that reaches the
root but that the root does not reach is a semi-join (IN). Metrics that no one
root can answer together are each answered from their own root and merged on
the dimensions (stitching). Anything else is rejected with a message that
names the nearest supported thing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

from ossie import (
    OssieDataset,
    OssieDataType,
    OssieDocument,
    OssieField,
    OssieMetric,
    OssieRelationship,
)
from sqlglot import exp
from sqlglot.dialects.clickhouse import ClickHouse

from dactopus_ossie_clickhouse.model import (
    all_metrics,
    covers_key,
    join_problem,
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


class Catalog(dict[str, TableInfo]):
    """What the executor learned from the server: TableInfo by dataset name, and
    ``aggregate``, whether a function outside the spec is an aggregate (None when
    system.functions could not be read: the planner then asks
    _known_clickhouse_aggregate)."""

    def __init__(self, tables=(), aggregate: Callable[[str], bool] | None = None):
        super().__init__(tables)
        self.aggregate = aggregate


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
    for name, _, _ in all_metrics(model):
        if name in skip:
            continue
        try:
            # Through the planner, so names are the model's spelling, as in plan().
            inputs = _aggregate_inputs(planner._metric_tree(planner.metric(name), set()))
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
    shows. #246 §6.11.5: NULL for SUM, 0 for COUNT, 0 for COALESCE(SUM(x), 0).

    None for a metric with an aggregate outside the spec: ClickHouse gives 0 for
    countIf and uniq, NULL for sumIf, [] for groupArray, and system.functions
    does not say which; Planner._over_no_rows asks ClickHouse instead."""
    if any(agg.meta.get("outside_spec") for agg in tree.find_all(exp.AggFunc)):
        return None

    def empty(node: exp.Expression) -> exp.Expression:
        agg = node.this if isinstance(node, exp.Filter) else node  # COUNT(x) FILTER (...)
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
            names = (x.name for x in self._metrics.values())
            raise PlanError(f"unknown metric {name!r}{suggest(name, names)}")
        return m

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
    ) -> exp.Expression:
        """Parse an expression, inline `dataset.field` references, qualify columns.

        With ``metrics``, a metric's name inlines that metric's expression
        (filters may say ``total_sales > 1000`` or ``store_sales.total_quantity > 5``).
        With ``scope``, the expression is a metric of that dataset: a bare name is
        one of its fields, and nothing else may be named (spec, "Metric Scoping").
        Records the datasets touched in ``used`` (by model name); with ``scope``,
        _metric_tree records it, since COUNT(*) reads no field.
        """
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
        """A column of a metric of ``ds``: one of its fields by bare name, as
        apache/ossie's validator requires (rule 1 of "Metric Scoping")."""
        if node.table:
            why = (
                f"names its own fields without the dataset, as {node.name}"
                if node.table.upper() == ds.name.upper()
                else "reads only its own fields; a metric over other datasets "
                "belongs in the model's metrics"
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
        if scope is not None:
            used.add(scope.name)
            for agg in tree.find_all(exp.AggFunc):  # after rewrite_tree, which replaces some
                agg.meta["dataset"] = scope.name
            return tree
        if len(self.model.datasets) > 1:
            inputs = _aggregate_inputs(tree)
            unnamed = [agg for agg, ds in inputs if not ds]
            if unnamed and len(_homes(inputs)) != 1:
                agg = unnamed[0]
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
                    f"column, so it would {why}"
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
            e = rewrite_tree(self._field_expr(ds, f), self._zone)
            selects.append(e.as_(alias(f.name)))
            group.append(e.copy())

        for name in q.metrics:
            m = self.metric(name)
            label = f"metric {m.name!r}"
            tree = self._metric_tree(m, touched.setdefault(label, set()))
            checked.append((label, _aggregate_inputs(tree)))
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
            if not tree.find(exp.AggFunc):
                rows[label] = tree
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
                    stitched = self._stitch(q, row_filters, aggregate_filters)
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
        if root is None:
            # Reads no dataset (the current time): one row, without FROM.
            sel, dict_keys = exp.select(*selects), {}
        else:
            sel, dict_keys = self._from(exp.select(*selects), root, used)
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
        if semi_joins:  # after the transform: their subqueries read datasets of their own
            sel = sel.where(exp.and_(*semi_joins))
        sel.set("settings", _settings())
        return sel

    def _from(
        self, sel: exp.Select, root: OssieDataset, used: set[str]
    ) -> tuple[exp.Select, dict[str, exp.Expression]]:
        """FROM ``root`` with a LEFT JOIN to each used dataset, and the dictionary
        datasets read with dictGetOrNull instead, by dataset name -> key expression;
        the caller rewrites their columns once the SELECT is complete."""
        sel = sel.from_(self._table(root))
        dict_keys: dict[str, exp.Expression] = {}
        joins = self._joins(root, used)
        joined_from = {r.from_dataset.upper() for r in joins}
        for r in joins:
            source, target = self.dataset(r.from_dataset), self.dataset(r.to)
            info = (self.catalog or {}).get(target.name)
            froms, tos = self._keys(source, r.from_columns), self._keys(target, r.to_columns)
            if (
                info
                and info.dictionary_key
                and len(tos) == 1
                and isinstance(tos[0], exp.Column)
                and tos[0].name.upper() == info.dictionary_key.upper()
                and target.name.upper() not in joined_from  # nothing joins on from it
            ):
                dict_keys[target.name] = froms[0]
                continue
            on = exp.and_(*(exp.EQ(this=a, expression=b) for a, b in zip(froms, tos, strict=True)))
            sel = sel.join(self._table(target), on=on, join_type="left")
        return sel, dict_keys

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
        inside (#246 §6.5, filtering join; Semantic 2). Filters on M and on
        datasets M reaches go into one subquery, so one row of M passes all of
        them, as a joined row would. The subquery is a question over M, with its
        own joins, FINAL and dictionaries; M reaches the root along one path of
        relationships, one IN per step."""
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
        semi_joins = []
        for top, labels in group.items():
            if root.name not in reach[top]:
                via = sorted(
                    d.name
                    for d in self.model.datasets
                    if {root.name, top} <= self._reachable(d.name) and d.name != top
                )
                hint = (
                    f"; name the dataset that links them ({', '.join(via)}) in a filter"
                    if via
                    else ""
                )
                raise PlanError(
                    f"{labels[0]} reads {top}, which {root.name} does not join to and which "
                    f"does not reach {root.name} through many-to-one relationships{hint}"
                )
            used = set().union({top}, *(touched[label] for label in labels))
            semi_joins.append(
                self._semi_join(root, self.dataset(top), [rows[label] for label in labels], used)
            )
        return root, joined, semi_joins

    def _semi_join(
        self, root: OssieDataset, m: OssieDataset, filters: list[exp.Expression], used: set[str]
    ) -> exp.Expression:
        """root's key IN the keys of ``m`` rows that pass ``filters``, one IN per
        relationship on the one path from ``m`` to ``root``."""
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
        sub = sub.where(exp.and_(*filters))
        if dict_keys:
            sub = sub.transform(lambda n: self._dict_get(n, dict_keys))
        for before, r in zip(path, path[1:], strict=False):
            at = self.dataset(r.from_dataset)
            sub = (
                exp.select(*self._keys(at, r.from_columns))
                .from_(self._table(at))
                .where(_in(self._keys(at, before.to_columns), sub))
            )
        return _in(self._keys(root, path[-1].to_columns), sub)

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
            self._time_strings(tree, metrics=True)
            for col in tree.find_all(exp.Column):
                m = self._named_metric(col)
                if m and m.name not in names:
                    names.append(m.name)  # filtered on, not selected
            having_trees.append(tree)

        metric_trees = {n: self._metric_tree(self.metric(n), set()) for n in names}
        groups: dict[frozenset[str], list[str]] = {}
        for n, tree in metric_trees.items():
            groups.setdefault(frozenset(_homes(_aggregate_inputs(tree))), []).append(n)
        if len(groups) > 1 and frozenset() in groups:
            # A metric that reads no dataset (the current time) takes its value in any
            # group; a group of its own would add a row for every dimension value.
            loose = groups.pop(frozenset())
            next(iter(groups.values())).extend(loose)
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
        for f, tree in zip(having, having_trees, strict=True):
            if tree.find(exp.AggFunc):
                raise PlanError(
                    f"filter {f!r} applies an aggregate function to metrics answered from "
                    "different datasets; this version cannot answer it, name a metric instead"
                )
        empties = {}
        for n in names:
            empty = _empty_value(metric_trees[n])
            empties[n] = self._over_no_rows(n) if empty is None else empty

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

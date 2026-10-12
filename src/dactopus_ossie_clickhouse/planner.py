"""Turn a semantic query (measures, dimensions, where, having) into one ClickHouse SELECT.

The query is the aggregation query of Ossie's Foundational Semantics draft
(apache/ossie#246 §5.1.1, revision LAYER3_REVISION). Minimum planner: one root
dataset plus many-to-one joins along the one path to each dataset the query
touches.
Measures that no one root can answer together are each answered from their own
root and merged on the dimensions (stitching). Anything else is refused with a
code (Code) and a message that names the
nearest supported thing.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from typing import Any

from ossie import OssieDataset, OssieDocument, OssieField, OssieRelationship
from sqlglot import exp

from dactopus_ossie_clickhouse.model import covers_key, declared_keys, join_problem
from dactopus_ossie_clickhouse.translate import parse, pick_expression, rewrite_tree, untranslatable

# The revision of apache/ossie#246 whose query shape and codes this planner follows.
LAYER3_REVISION = "apache/ossie#246@cc0d070"


class Code(StrEnum):
    """Why a query is refused, for an agent to repair it by.

    E_* and E3013_* are the codes of apache/ossie#246 (Appendix A). Where #246
    has none, the common codes of the execute_query profile (apache/ossie#529):
    INVALID_ARGUMENT for a clause value its query schema rejects,
    QUERY_INVALID for a query that is otherwise wrong, UNSUPPORTED_QUERY for one this
    version cannot answer, SOURCE_UNAVAILABLE for a model the connected user
    cannot read at all, BACKEND_ERROR for ClickHouse failing a query."""

    E_NAME_NOT_FOUND = "E_NAME_NOT_FOUND"
    E_NO_PATH = "E_NO_PATH"
    E_AMBIGUOUS_PATH = "E_AMBIGUOUS_PATH"
    E3013_NO_STITCHING_DIMENSION = "E3013_NO_STITCHING_DIMENSION"
    E_EMPTY_AGGREGATION_QUERY = "E_EMPTY_AGGREGATION_QUERY"
    E_EMPTY_SCALAR_QUERY = "E_EMPTY_SCALAR_QUERY"
    E_MIXED_QUERY_SHAPE = "E_MIXED_QUERY_SHAPE"
    E_AGGREGATE_IN_WHERE = "E_AGGREGATE_IN_WHERE"
    E_WINDOW_IN_WHERE = "E_WINDOW_IN_WHERE"
    E_NON_AGGREGATE_IN_HAVING = "E_NON_AGGREGATE_IN_HAVING"
    E_MIXED_PREDICATE_LEVEL = "E_MIXED_PREDICATE_LEVEL"
    E_PRIMARY_KEY_REQUIRED = "E_PRIMARY_KEY_REQUIRED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    QUERY_INVALID = "QUERY_INVALID"
    UNSUPPORTED_QUERY = "UNSUPPORTED_QUERY"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    BACKEND_ERROR = "BACKEND_ERROR"


@dataclass(frozen=True)
class Suggestion:
    """Advice that comes with a refusal; ``kind`` as in apache/ossie#529."""

    kind: str  # "name": a known name to use instead
    message: str


class PlanError(ValueError):
    def __init__(
        self,
        message: str,
        code: Code = Code.QUERY_INVALID,
        suggestions: tuple[Suggestion, ...] = (),
    ):
        super().__init__(message)
        self.code = code
        self.suggestions = suggestions


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


Catalog = dict[str, TableInfo]  # by dataset name


@dataclass(frozen=True)
class Order:
    """An order_by entry of #246 §5.1: a selected measure or dimension and a direction."""

    field: str
    direction: str = "ASC"  # or "DESC"
    # "FIRST" or "LAST". None is #246's default: NULL sorts as the highest value,
    # last ascending and first descending.
    nulls: str | None = None

    @classmethod
    def parse(cls, text: str) -> Order:
        """The CLI's spelling: 'name', 'name desc', 'name desc nulls last'."""
        words = text.split()
        tail = [w.upper() for w in words[1:]]
        direction = tail.pop(0) if tail and tail[0] in ("ASC", "DESC") else "ASC"
        if not words or tail not in ([], ["NULLS", "FIRST"], ["NULLS", "LAST"]):
            raise PlanError(
                f"order_by item {text!r}: use 'name', 'name desc' or 'name desc nulls last'"
            )
        return cls(words[0], direction, tail[1] if tail else None)


@dataclass(frozen=True)
class Query:
    """An aggregation query, #246 §5.1.1."""

    measures: tuple[str, ...] = ()  # metric names
    dimensions: tuple[str, ...] = ()  # "dataset.field"
    # Ossie predicates, each list an AND. where filters rows before aggregation:
    # over dataset.field, no aggregate. having filters the aggregated rows: over
    # metric names, aggregates and dimensions of the query.
    where: tuple[str, ...] = ()
    having: tuple[str, ...] = ()
    # Empty with measures and dimensions: first measure descending, NULLs last,
    # so callers that rank rows themselves (agents do, and get it wrong) see
    # the top rows first. Ties are broken by the dimensions, ascending.
    order_by: tuple[Order, ...] = ()
    limit: int | None = None
    fields: tuple[str, ...] = ()  # a scalar query (#246 §5.1.2): refused

    def __post_init__(self):
        for name in ("where", "having"):  # one predicate, as #246 allows, is a list of one
            if isinstance(getattr(self, name), str):
                object.__setattr__(self, name, (getattr(self, name),))

    @classmethod
    def from_dict(cls, obj: Any) -> Query:
        """A query given as the Layer 3 JSON object of the execute_query profile
        (apache/ossie#529, its query schema): #246 §5.1 with `order_by` objects.

        A value the schema rejects is INVALID_ARGUMENT; a shape it rejects (both
        fields and dimensions or measures, or neither) gets the #246 code."""
        if not isinstance(obj, dict):
            raise PlanError(
                f"a query is a JSON object with {', '.join(_CLAUSES)}", Code.INVALID_ARGUMENT
            )
        for key in obj:
            if key in _RENAMED:
                hint = " or ".join(_RENAMED[key])
                raise PlanError(
                    f"unknown query clause {key!r}; use {hint}",
                    Code.INVALID_ARGUMENT,
                    tuple(Suggestion("name", n) for n in _RENAMED[key]),
                )
            if key not in _CLAUSES:
                raise _not_found(
                    f"unknown query clause {key!r}", key, _CLAUSES, Code.INVALID_ARGUMENT
                )

        def text(v: Any) -> bool:
            return isinstance(v, str) and not v.isspace() and v != ""

        def strings(key: str) -> tuple[str, ...]:
            v = obj.get(key, [])
            if not isinstance(v, list) or not all(text(x) for x in v):
                raise PlanError(
                    f"query clause {key!r} must be a list of non-blank strings",
                    Code.INVALID_ARGUMENT,
                )
            return tuple(v)

        def predicates(key: str) -> tuple[str, ...]:
            v = obj.get(key, [])
            if key in obj and text(v):
                return (v,)
            if not isinstance(v, list) or not all(text(x) for x in v) or (key in obj and not v):
                raise PlanError(
                    f"query clause {key!r} must be a non-blank string or a non-empty list "
                    "of them (AND)",
                    Code.INVALID_ARGUMENT,
                )
            return tuple(v)

        order = []
        items = obj.get("order_by", [])
        if not isinstance(items, list) or ("order_by" in obj and not items):
            raise PlanError(
                'order_by must be a non-empty list of objects such as {"field": "<measure or '
                'dimension>", "direction": "DESC"}',
                Code.INVALID_ARGUMENT,
            )
        for item in items:
            if (
                not isinstance(item, dict)
                or not text(item.get("field"))
                or set(item) - {"field", "direction", "nulls"}
                or item.get("direction", "ASC") not in ("ASC", "DESC")
                or item.get("nulls", "LAST") not in ("FIRST", "LAST")
            ):
                raise PlanError(
                    f"order_by item {item!r}: keys are field (a selected measure or dimension), "
                    'direction ("ASC" or "DESC") and nulls ("FIRST" or "LAST")',
                    Code.INVALID_ARGUMENT,
                )
            order.append(Order(item["field"], item.get("direction", "ASC"), item.get("nulls")))
        limit = obj.get("limit")
        if type(limit) is float and limit.is_integer():  # JSON Schema's integer: 5.0 is one
            limit = int(limit)
        if "limit" in obj and (type(limit) is not int or limit < 0):
            raise PlanError(
                f"limit must be a non-negative integer, got {limit!r}", Code.INVALID_ARGUMENT
            )
        q = cls(
            strings("measures"),
            strings("dimensions"),
            predicates("where"),
            predicates("having"),
            tuple(order),
            limit,
            strings("fields"),
        )
        # The profile reads the shape from the clauses present, empty or not.
        if "fields" in obj:
            if mixed := [k for k in ("dimensions", "measures", "having") if k in obj]:
                raise PlanError(
                    f"a query has either fields (a scalar query) or {' and '.join(mixed)} "
                    "(of an aggregation query), not both",
                    Code.E_MIXED_QUERY_SHAPE,
                )
            if not q.fields:
                raise PlanError(
                    "a scalar query needs at least one field", Code.E_EMPTY_SCALAR_QUERY
                )
        elif not q.measures and not q.dimensions:
            raise PlanError(
                "a query needs at least one measure or dimension", Code.E_EMPTY_AGGREGATION_QUERY
            )
        return q


_CLAUSES = ("dimensions", "measures", "where", "having", "order_by", "limit", "fields")
# Clauses of the query shape before Layer 3 (0.2), which agents may still send.
_RENAMED = {"metrics": ("measures",), "filters": ("where", "having")}


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
        raise PlanError(f"query sources are not supported yet: {source!r}", Code.UNSUPPORTED_QUERY)
    if "\\" in s or re.search(r"--|/\*", re.sub(_QUOTED, "_", s)) or not _SOURCE.fullmatch(s):
        raise PlanError(
            f"cannot map source {source!r} to a ClickHouse table", Code.UNSUPPORTED_QUERY
        )
    return tuple(
        (p[1:-1].replace(p[0] * 2, p[0]), True) if p[0] in '`"' else (p, False)
        for p in _PART.findall(s)
    )


def source_name(source: str) -> str:
    """`db.table` unquoted, as dictGet* takes a dictionary's name."""
    t = source_table(source)
    return f"{t.db}.{t.name}" if t.db else t.name


# Aggregates whose value does not change when an input row repeats. Only these
# may read a joined dataset's columns on their own: each row of a many-to-one
# target repeats once per root row that references it.
_REPEAT_SAFE = {
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.AnyValue: "ANY_VALUE",
    exp.LogicalAnd: "BOOL_AND",
    exp.LogicalOr: "BOOL_OR",
    exp.ApproxDistinct: "APPROX_COUNT_DISTINCT",
}
_REPEAT_SAFE_NAMES = ", ".join(_REPEAT_SAFE.values()) + " and DISTINCT"

Inputs = list[tuple[exp.AggFunc, set[str]]]


def _repeat_safe(agg: exp.AggFunc) -> bool:
    # A parametric aggregate, name(p)(args), keeps its arguments in params:
    # MEDIAN(DISTINCT x) is rewritten to one.
    args = (agg.args.get("params") or []) if isinstance(agg, exp.ParameterizedAgg) else [agg.this]
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


def unanswerable_metrics(model: OssieDocument) -> dict[str, str]:
    """Metrics the planner refuses in every question, with why.

    One that names an unknown field or reads no column, and one with a
    duplicate-sensitive aggregate over each of two datasets: whichever is the
    root, the other one's rows repeat. Untranslatable metrics are reported by
    untranslatable()."""
    planner = Planner(model)
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


def _empty_value(tree: exp.Expression) -> exp.Expression:
    """A metric over no rows: what a stitched group missing from the metric's fact
    shows. #246 §6.11.5: NULL for SUM, 0 for COUNT, 0 for COALESCE(SUM(x), 0)."""

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
            f"{home} joins to",
            Code.UNSUPPORTED_QUERY,
        )
    if repeated:
        agg, joined = repeated[0][0].sql("clickhouse"), " and ".join(sorted(repeated[0][1]))
        raise PlanError(
            f"{label} applies {agg} to {joined} columns alone over {root} rows, "
            f"so each {joined} row would count once per {root} row that references it. "
            f"Only {_REPEAT_SAFE_NAMES} aggregates may read a joined dataset on their own, "
            f"unless it is joined on a key of {root} (one row each); "
            "this version cannot answer it",
            Code.UNSUPPORTED_QUERY,
        )


def _check_level(clause: str, text: str, tree: exp.Expression, grouped: set[str]) -> None:
    """Refuse a where or having predicate at the wrong level (#246 §6.2 step A.3, §6.3).

    where takes row-level predicates; having takes aggregates (metric names
    included) and the query's dimensions."""
    label = f"{clause} {text!r}"
    if tree.find(exp.Window):
        if clause == "where":
            raise PlanError(
                f"{label} uses a window function, which runs after where; filter it in having",
                Code.E_WINDOW_IN_WHERE,
            )
        raise PlanError(
            f"{label} uses a window function; this version cannot filter a window metric in "
            "the same SELECT, so select it and filter the rows instead",
            Code.UNSUPPORTED_QUERY,
        )
    aggregate = tree.find(exp.AggFunc) is not None
    row_level = []  # columns outside aggregates; in having, those that are not dimensions
    for col in tree.find_all(exp.Column):
        if col.find_ancestor(exp.AggFunc):
            continue
        node: exp.Expression | None = col
        while clause == "having" and node is not None and node.sql() not in grouped:
            node = node.parent
        if clause == "where" or node is None:
            row_level.append(col)
    if aggregate and row_level:
        raise PlanError(
            f"{label} mixes an aggregate with {row_level[0].sql()}, a row-level column"
            + (" that is not a dimension of this query" if clause == "having" else "")
            + "; put row-level conditions in where and aggregate ones in having",
            Code.E_MIXED_PREDICATE_LEVEL,
        )
    if aggregate and clause == "where":
        raise PlanError(
            f"{label} uses an aggregate (a metric or an aggregate function); "
            "filter aggregated rows in having",
            Code.E_AGGREGATE_IN_WHERE,
        )
    if clause == "having" and not aggregate and row_level:
        raise PlanError(
            f"{label} has no aggregate and reads {row_level[0].sql()}, which is not a "
            "dimension of this query; filter rows in where",
            Code.E_NON_AGGREGATE_IN_HAVING,
        )


def _not_found(message: str, name: str, options, code: Code = Code.E_NAME_NOT_FOUND) -> PlanError:
    """A refusal for an unknown name, with the closest known names in text and as suggestions."""
    close = difflib.get_close_matches(name, list(options), n=3, cutoff=0.5)
    hint = f" (did you mean: {', '.join(close)}?)" if close else ""
    return PlanError(message + hint, code, tuple(Suggestion("name", c) for c in close))


_EXPRESSION = re.compile(r"[\s()]")  # in a name slot: an expression or an alias, not a name


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
            names = (d.name for d in self.model.datasets)
            raise _not_found(f"unknown dataset {name!r}", name, names)
        return ds

    def metric(self, name: str):
        m = self._metrics.get(name.upper())
        if m is None:
            names = (x.name for x in self.model.metrics or [])
            raise _not_found(f"unknown metric {name!r}", name, names)
        return m

    def field_ref(self, ref: str) -> tuple[OssieDataset, OssieField]:
        if ref.count(".") != 1:
            names = (f"{d.name}.{x.name}" for d in self.model.datasets for x in d.fields or [])
            raise _not_found(f"field reference must be dataset.field, got {ref!r}", ref, names)
        ds_name, f_name = ref.split(".")
        ds = self.dataset(ds_name)
        f = self._fields[ds.name.upper()].get(f_name.upper())
        if f is None:
            names = (f"{ds.name}.{x.name}" for x in ds.fields or [])
            raise _not_found(f"unknown field {ref!r}", ref, names)
        return ds, f

    def _known(self, name: str) -> bool:
        """Whether ``name`` is a metric or a dataset.field of the model."""
        try:
            self.field_ref(name) if "." in name else self.metric(name)
        except PlanError:
            return False
        return True

    def resolve(self, expression: str, used: set[str], metrics: bool = False) -> exp.Expression:
        """Parse an expression, inline `dataset.field` references, qualify columns.

        With ``metrics``, a bare name that is a metric inlines that metric's
        expression (filters may say ``total_sales > 1000``).
        Records the datasets touched in ``used`` (by model name).
        """
        try:
            tree = parse(expression)
        except ValueError as e:
            raise PlanError(str(e)) from e

        def inline(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            if not node.table:
                if metrics and node.name.upper() in self._metrics:
                    return self._metric_tree(self.metric(node.name), used)
                if not metrics:
                    raise PlanError(f"unqualified column {node.name!r} in {expression!r}")
                names = [x.name for x in self.model.metrics or []] + [
                    f"{d.name}.{x.name}" for d in self.model.datasets for x in d.fields or []
                ]
                raise _not_found(
                    f"unqualified column {node.name!r} in {expression!r}; "
                    "use dataset.field or a metric name",
                    node.name,
                    names,
                )
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
                    "COUNT(dataset.key)",
                    Code.UNSUPPORTED_QUERY,
                )
        return tree

    def _field_expr(self, ds: OssieDataset, f: OssieField) -> exp.Expression:
        """A field's expression with its bare columns qualified by the dataset alias."""
        tree = parse(pick_expression(f.expression))

        def qualify(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column) and not node.table:
                return exp.column(node.this, table=ds.name)
            if isinstance(node, exp.Column):
                raise PlanError(
                    f"field {ds.name}.{f.name} references another dataset ({node.sql()}); "
                    "fields may only use their own columns",
                    Code.UNSUPPORTED_QUERY,
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
        dataset, E_AMBIGUOUS_PATH when there are two. Datasets on a path that
        the query does not name are joined too (§6.2, datasets involved in a query)."""
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
                    f"{shown}; #246 has no way to choose between them yet",
                    Code.E_AMBIGUOUS_PATH,
                )
            for i, r in enumerate(paths[0]):  # _root chose a dataset that reaches every other
                depth[r.name] = (i, r)
        joins = [r for _, r in sorted(depth.values(), key=lambda d: (d[0], d[1].name))]
        for r in joins:
            target = self.dataset(r.to)
            problem = join_problem(r, target)
            if problem:
                # Joins need the target's key: E_PRIMARY_KEY_REQUIRED by analogy with
                # #246 §4.2, where an engine may refuse a model without primary keys.
                keyed = declared_keys(target)
                raise PlanError(
                    problem, Code.UNSUPPORTED_QUERY if keyed else Code.E_PRIMARY_KEY_REQUIRED
                )
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

    def _reachable(self, name: str, directed: bool = True) -> set[str]:
        """Datasets ``name`` reaches through relationships, itself included; with
        ``directed``, only from the many side to the one side."""
        seen, todo = {name.upper()}, [name.upper()]
        while todo:
            at = todo.pop()
            for r in self.model.relationships or []:
                ends = (r.from_dataset.upper(), r.to.upper())
                for a, b in (ends,) if directed else (ends, ends[::-1]):
                    if a == at and b in self._datasets and b not in seen:
                        seen.add(b)
                        todo.append(b)
        return {self._datasets[n].name for n in seen}

    def _unjoined(self, used: set[str], touched: dict[str, set[str]]) -> PlanError:
        """Why no used dataset reaches all the others: no path between some of them
        (#246 codes), or a shape this version cannot plan."""
        # A stitching dimension (#246 §6.8.2) is a dataset reachable from each fact
        # through many-to-one relationships. Facts without one are unrelated: E3013,
        # whether one metric reads them or two measures do (Appendix A).
        measures = {k: d for k, d in touched.items() if k.startswith("metric ") and d}
        for label, ds in measures.items():
            if not set.intersection(*(self._reachable(d) for d in ds)):
                return PlanError(
                    f"{label} reads {' and '.join(sorted(ds))}, which share no dimension",
                    Code.E3013_NO_STITCHING_DIMENSION,
                )
        reach = [set().union(*(self._reachable(d) for d in ds)) for ds in measures.values()]
        if len(reach) > 1 and not set.intersection(*reach):
            return PlanError(
                f"{' and '.join(measures)} read "
                f"{' and '.join(', '.join(sorted(d)) for d in measures.values())}, which share "
                "no dimension: no dataset is reachable from each through many-to-one "
                "relationships, so together they would pair every row of one with every row "
                "of the other",
                Code.E3013_NO_STITCHING_DIMENSION,
            )
        # From what the first measure reads: the rest reads as missing from it.
        start = next((min(d) for k, d in touched.items() if k.startswith("metric ") and d), None)
        first = self._reachable(start or min(used), directed=False)
        if not used <= first:
            return PlanError(
                f"no relationship path connects {', '.join(sorted(used & first))} with "
                f"{', '.join(sorted(used - first))}",
                Code.E_NO_PATH,
            )
        return PlanError(
            f"no dataset among {sorted(used)} joins all the others through many-to-one "
            "relationships, so each would have to be aggregated on its own and the results "
            "combined; this version cannot answer it",
            Code.UNSUPPORTED_QUERY,
        )

    def _root(self, used: set[str], touched: dict[str, set[str]]) -> OssieDataset:
        """The used dataset that reaches every other one through many-to-one relationships."""
        if not used:  # metrics that read no column, such as a constant
            raise PlanError(
                "the query reads no column of any dataset, so there are no rows to answer it "
                "over; add a dimension or a measure that reads a column",
                Code.UNSUPPORTED_QUERY,
            )
        candidates = [self.dataset(n) for n in sorted(used) if used <= self._reachable(n)]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise self._unjoined(used, touched)
        raise PlanError(
            f"ambiguous root dataset among {[c.name for c in candidates]}",
            Code.UNSUPPORTED_QUERY,
        )

    # --- assembly -----------------------------------------------------------

    def plan(self, q: Query) -> exp.Select:
        if q.fields:
            if q.measures or q.dimensions:
                raise PlanError(
                    "a query has either fields (a scalar query) or dimensions and measures "
                    "(an aggregation query), not both",
                    Code.E_MIXED_QUERY_SHAPE,
                )
            raise PlanError(
                "scalar queries (fields) are not supported yet; ask for dimensions and measures",
                Code.UNSUPPORTED_QUERY,
            )
        if not q.measures and not q.dimensions:
            raise PlanError(
                "a query needs at least one measure or dimension", Code.E_EMPTY_AGGREGATION_QUERY
            )
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
                    "fields of two datasets, cannot share a name in one query",
                    Code.UNSUPPORTED_QUERY,
                )
            seen_aliases.add(name.upper())
            return name

        for ref in q.dimensions:
            if _EXPRESSION.search(ref) and not self._known(ref):
                raise PlanError(
                    f"dimension {ref!r}: dimensions are dataset.field names; aliases and "
                    "expressions are not supported yet",
                    Code.UNSUPPORTED_QUERY,
                )
            ds, f = self.field_ref(ref)
            touched.setdefault(f"dimension {ref!r}", set()).add(ds.name)
            e = rewrite_tree(self._field_expr(ds, f))
            selects.append(e.as_(alias(f.name)))
            group.append(e.copy())

        for name in q.measures:
            if not re.fullmatch(r"\w+", name) and name.upper() not in self._metrics:
                raise PlanError(
                    f"measure {name!r}: measures are metric names; ad-hoc aggregates and "
                    "window expressions are not supported yet, so ask for a metric of the model",
                    Code.UNSUPPORTED_QUERY,
                )
            m = self.metric(name)
            label = f"metric {m.name!r}"
            tree = self._metric_tree(m, touched.setdefault(label, set()))
            checked.append((label, _aggregate_inputs(tree)))
            selects.append(tree.as_(alias(m.name)))

        where: list[exp.Expression] = []
        having: list[exp.Expression] = []
        # Every part is rewritten once, before inspecting: APPROX_PERCENTILE is a
        # plain function name to SQLGlot and an aggregate only after rewrite.
        grouped = {e.sql() for e in group}
        for clause, items in (("where", q.where), ("having", q.having)):
            for f in items:
                label = f"{clause} {f!r}"
                tree = self.resolve(f, touched.setdefault(label, set()), metrics=True)
                tree = rewrite_tree(tree)
                _check_level(clause, f, tree, grouped)
                if clause == "where":
                    where.append(tree)
                else:
                    having.append(tree)
                    checked.append((label, _aggregate_inputs(tree)))

        used = set().union(*touched.values())
        try:
            root = self._root(used, touched)
            one_to_one = self._one_to_one(root.name)
            for label, inputs in checked:
                _check_rows(label, inputs, root.name, touched, one_to_one)
        except PlanError as e:
            # Unrelated facts by a dimension: no dimension is shared, so stitching
            # cannot help (E3013); without dimensions their totals are answered.
            if e.code == Code.E3013_NO_STITCHING_DIMENSION and q.dimensions:
                raise
            stitched = self._stitch(q)
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

    def _stitch(self, q: Query) -> exp.Select | None:
        """Metrics no one root answers together, each answered from its own root and
        merged on the dimensions (#246 §6.2 step A.7, §6.6); None if they form one
        group, or a group is refused even on its own.

        Metrics are grouped by the datasets their aggregates read, and each group is
        planned as a question of its own with the dimensions and where, which
        applies to every metric (#246 §6.2 step A.6.1). The groups' rows are
        merged with UNION ALL and GROUP BY on the dimensions: every group of either
        side appears (§6.6 row 3), a NULL dimension value merges into one row as
        NULL-safe equality would, and no dimensions leave one row of totals (§6.6
        row 4). A metric missing from a group shows its value over no rows
        (_empty_value). having runs after the merge (step A.9) and may name
        metrics, selected or not, and the question's dimensions."""
        names = list(dict.fromkeys(self.metric(n).name for n in q.measures))
        having_trees = []
        for f in q.having:
            tree = parse(f)
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
                branch = Query(tuple(members), q.dimensions, q.where)
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
                    f"dimensions and where: {e}",
                    e.code,
                    e.suggestions,
                ) from None
        for n, tree in metric_trees.items():
            if tree.find(exp.Window):
                raise PlanError(
                    f"metric {n!r} is a window over the merged rows of metrics that are "
                    "answered from different datasets; this version cannot answer it, "
                    "ask for it in its own question",
                    Code.UNSUPPORTED_QUERY,
                )
        for f, tree in zip(q.having, having_trees, strict=True):
            if tree.find(exp.AggFunc):
                raise PlanError(
                    f"having {f!r} applies an aggregate function to metrics answered from "
                    "different datasets; this version cannot answer it, name a metric instead",
                    Code.UNSUPPORTED_QUERY,
                )

        dims = [self.field_ref(d)[1].name for d in q.dimensions]
        empties = {n: _empty_value(metric_trees[n]) for n in names}
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
        selects += [values[self.metric(n).name].copy().as_(self.metric(n).name) for n in q.measures]
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
                        raise PlanError(f"having column {ref} is not a dimension of this query")
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

        Without order_by, the first measure descending, NULLs last. Then each
        dimension not ordered yet, ascending: rows are grouped by the dimensions,
        so ties are broken and a limit keeps the same rows on every run. NULL
        placement follows #246 §5.1: unless an entry says otherwise, NULL sorts
        as the highest value, last ascending and first descending."""
        aliases = {e.alias.upper(): e.alias for e in selects}
        for ref in q.dimensions:  # allow the dataset.field spelling too
            aliases[ref.upper()] = self.field_ref(ref)[1].name
        order_by = q.order_by
        if not order_by and q.measures and q.dimensions:
            order_by = (Order(q.measures[0], "DESC", "LAST"),)
        out = []
        for item in order_by:
            direction, nulls = item.direction.upper(), (item.nulls or "").upper()
            if direction not in ("ASC", "DESC") or nulls not in ("", "FIRST", "LAST"):
                raise PlanError(
                    f"order_by {item.field!r}: direction is ASC or DESC, nulls FIRST or LAST"
                )
            alias = aliases.get(item.field.upper())
            if alias is None:
                if _EXPRESSION.search(item.field) or self._known(item.field):
                    raise PlanError(
                        f"order_by {item.field!r}: this version orders only by names of the "
                        "measures and dimensions the query selects; select it to order by it",
                        Code.UNSUPPORTED_QUERY,
                    )
                raise _not_found(
                    f"order_by {item.field!r} is not a selected measure or dimension",
                    item.field,
                    dict.fromkeys(aliases.values()),
                )
            desc = direction == "DESC"
            nulls_first = nulls == "FIRST" if nulls else desc
            out.append(exp.Ordered(this=exp.column(alias), desc=desc, nulls_first=nulls_first))
        ordered = {o.this.name.upper() for o in out}
        for ref in q.dimensions:
            alias = self.field_ref(ref)[1].name
            if alias.upper() not in ordered:
                ordered.add(alias.upper())
                out.append(exp.Ordered(this=exp.column(alias), desc=False, nulls_first=False))
        return out

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

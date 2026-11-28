"""Translate Ossie expressions to ClickHouse SQL.

Parsing uses SQLGlot's default (ANSI-like) dialect plus the few spec shapes
it gets wrong, typing only the spec's function catalog; any other function
reaches ClickHouse as written. Generation uses SQLGlot's ClickHouse dialect
after rewriting the spec functions ClickHouse lacks. Every rewrite works on
the AST, never on SQL text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from decimal import Decimal
from functools import cache

import sqlglot
import sqlglot.errors
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.parser import Parser
from sqlglot.tokens import TokenType

from dactopus_ossie_clickhouse._vendor.ossie import OssieDataset, OssieDocument, OssieExpression
from dactopus_ossie_clickhouse.model import (
    CLICKHOUSE_DIALECT,
    DEFAULT_TIME_ZONE,
    SOURCE_QUALIFIER,
    all_metrics,
    dialects_of,
    expression_choice,
    reads_source,
)


def pick_expression(expression: OssieExpression) -> str:
    """The expression text we translate (model.expression_choice). A text in the
    CLICKHOUSE dialect is refused unless it reaches ClickHouse as written
    (clickhouse_problem), so every layer that reads an expression reads it here."""
    choice = expression_choice(expression)
    if choice is None:
        raise ValueError(f"no expression in a supported dialect (has {dialects_of(expression)})")
    dialect, text = choice
    if dialect == CLICKHOUSE_DIALECT and (problem := clickhouse_problem(text)):
        raise ValueError(f"{problem}: {text!r}")
    return text


# --- parsing ----------------------------------------------------------------


# Spec signatures of the functions rewritten below or by SQLGlot's ClickHouse
# generator, checked on the arguments as written. The default dialect also
# parses other engines' longer forms, such as Snowflake's REGEXP_COUNT(str,
# pattern, position, flags). They are not the spec's, and a rewrite may drop
# the extra arguments and return a different value, so parse() rejects them:
# SQLGlot prints REGEXP_LIKE(s, p, 'i') as match(s, p), MOD(a, b, c) as a % b,
# LOG10() as LOG(10). A longer form in brackets is not the spec's either, but
# reaches ClickHouse whole and means there what it means in DuckDB, Snowflake or
# Postgres, so it stays, as a function outside the catalog would ("Dialect
# Extensions").
_SIGNATURES = {
    "APPROX_PERCENTILE": ("APPROX_PERCENTILE(expr, p)", 2, 2),
    "TO_DATE": ("TO_DATE(string[, format])", 1, 2),
    "TO_TIMESTAMP": ("TO_TIMESTAMP(string[, format])", 1, 2),
    "IFF": ("IFF(condition, true_result, false_result)", 3, 3),
    "ZEROIFNULL": ("ZEROIFNULL(expr)", 1, 1),
    "NULLIFZERO": ("NULLIFZERO(expr)", 1, 1),
    "MEDIAN": ("MEDIAN(expr)", 1, 1),
    "PERCENTILE_CONT": ("PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)", 1, 1),
    "PERCENTILE_DISC": ("PERCENTILE_DISC(p) WITHIN GROUP (ORDER BY expr)", 1, 1),
    "CURRENT_DATE": ("CURRENT_DATE()", 0, 0),
    "CURRENT_TIME": ("CURRENT_TIME()", 0, 0),
    "CURRENT_TIMESTAMP": ("CURRENT_TIMESTAMP()", 0, 0),
    "DATE_TRUNC": ("DATE_TRUNC(part, date_expr[, zone])", 2, 3),
    "LENGTH": ("LENGTH(str)", 1, 1),
    "LTRIM": ("LTRIM(str[, characters])", 1, 2),
    "RTRIM": ("RTRIM(str[, characters])", 1, 2),
    "CHARINDEX": ("CHARINDEX(substr, str[, start])", 2, 3),
    "REGEXP_LIKE": ("REGEXP_LIKE(str, pattern)", 2, 2),
    "REGEXP_EXTRACT": ("REGEXP_EXTRACT(str, pattern[, group])", 2, 3),
    "ROUND": ("ROUND(x[, d])", 1, 2),
    "FLOOR": ("FLOOR(x[, d])", 1, 2),
    "CEIL": ("CEIL(x[, d])", 1, 2),
    "CEILING": ("CEILING(x[, d])", 1, 2),
    "MOD": ("MOD(x, y)", 2, 2),
    "LOG": ("LOG(base, x)", 2, 2),
    "LOG10": ("LOG10(x)", 1, 1),
    "TO_CHAR": ("TO_CHAR(date_expr, format)", 2, 2),
    "SPLIT_PART": ("SPLIT_PART(str, delimiter, part)", 3, 3),
    "REGEXP_COUNT": ("REGEXP_COUNT(str, pattern)", 2, 2),
    "CONTAINS": ("CONTAINS(str, substr)", 2, 2),
    "DAYOFYEAR": ("DAYOFYEAR(date_expr)", 1, 1),
    "DATEADD": ("DATEADD(part, amount, date_expr)", 3, 3),
    "DATEDIFF": ("DATEDIFF(part, start_date, end_date)", 3, 3),
    "DATE_PART": ("DATE_PART(part, date_expr)", 2, 2),
    "LAG": ("LAG(expr[, offset[, default]])", 1, 3),
    "LEAD": ("LEAD(expr[, offset[, default]])", 1, 3),
    "NTH_VALUE": ("NTH_VALUE(expr, n)", 2, 2),
    "FIRST_VALUE": ("FIRST_VALUE(expr)", 1, 1),
    "LAST_VALUE": ("LAST_VALUE(expr)", 1, 1),
}
# Added to the refusal of a call, by name and argument count, that engines read
# differently.
_HINTS = {
    ("LOG", 1): "LOG(x) is the natural logarithm in ClickHouse and base 10 in Postgres and "
    "DuckDB; write LN(x) or LOG(10, x)",
}

# Spec: supported date parts for EXTRACT and DATE_PART.
_DATE_PARTS = {
    "YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "DAYOFWEEK", "DAYOFYEAR",
    "HOUR", "MINUTE", "SECOND", "MILLISECOND",
}  # fmt: skip

# The spec's function catalog (core-spec/expression_language.md, "Aggregation
# Functions" through "Type Conversion Functions"): the names parse() gives
# SQLGlot's typed nodes, which a rewrite here or SQLGlot's ClickHouse generator
# translates. Any other function reaches ClickHouse as written, the spec's
# default for what it does not define ("Dialect Extensions"): SQLGlot knows
# many functions of other engines and would rename them, DAYOFWEEK(d) to
# DAY_OF_WEEK(d), which ClickHouse lacks, though it has DAYOFWEEK itself
# (docs/decisions/functions-pass-through.md).
_CATALOG = frozenset({
    # aggregates
    "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
    "VARIANCE", "VAR_POP", "VAR_SAMP", "MEDIAN", "PERCENTILE_CONT", "PERCENTILE_DISC",
    "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE",
    # date and time
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "YEAR", "QUARTER", "MONTH",
    "DAY", "DAYOFYEAR", "HOUR", "MINUTE", "SECOND", "EXTRACT", "DATE_PART", "DATE_TRUNC",
    "DATEADD", "DATEDIFF", "TO_DATE", "TO_TIMESTAMP", "TO_CHAR",
    # strings
    "CONCAT", "LENGTH", "LOWER", "UPPER", "TRIM", "LTRIM", "RTRIM", "LEFT", "RIGHT",
    "SUBSTRING", "REPLACE", "SPLIT_PART", "POSITION", "CHARINDEX", "CONTAINS",
    "STARTSWITH", "ENDSWITH", "REGEXP_LIKE", "REGEXP_EXTRACT", "REGEXP_REPLACE",
    "REGEXP_COUNT",
    # math
    "ABS", "ROUND", "FLOOR", "CEIL", "CEILING", "TRUNC", "TRUNCATE", "MOD", "SIGN",
    "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10", "SIN", "COS", "TAN", "ASIN", "ACOS",
    "ATAN", "ATAN2", "RADIANS", "DEGREES", "PI", "GREATEST", "LEAST",
    # conditionals
    "IF", "IFF", "NULLIF", "COALESCE", "IFNULL", "NVL", "NVL2", "ZEROIFNULL", "NULLIFZERO",
    # windows
    "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "PERCENT_RANK", "CUME_DIST", "LAG",
    "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE",
    # type conversion
    "CAST", "TRY_CAST",
})  # fmt: skip


def _date_delta(cls):
    """Spec puts the date part first: DATEADD(day, 7, d), DATEDIFF(day, d1, d2)."""

    def build(args: Sequence[exp.Expression]) -> exp.Expression:
        return cls(this=args[2], expression=args[1], unit=args[0])

    return build


def _date_part(args: Sequence[exp.Expression]) -> exp.Expression:
    """DATE_PART('year', d) is EXTRACT(YEAR FROM d); parse() checks the part."""
    part = args[0]
    if not (part.is_string or isinstance(part, exp.Column) and not part.table):
        raise sqlglot.errors.ParseError(f"DATE_PART: {part.sql()} is not a date part")
    return exp.Extract(this=exp.var(part.name.upper()), expression=args[1])


def _checked(name: str, build=None):
    """The builder for ``name``, rejecting argument counts outside its spec signature."""
    signature, low, high = _SIGNATURES[name]
    build = (
        build
        or Parser.FUNCTIONS.get(name)
        or (lambda args: exp.Anonymous(this=name, expressions=args))
    )

    def checked(args: Sequence[exp.Expression], **kwargs) -> exp.Expression:
        if not low <= len(args) <= high:
            hint = f"; {_HINTS[name, len(args)]}" if (name, len(args)) in _HINTS else ""
            raise sqlglot.errors.ParseError(
                f"expected {signature}, got {len(args)} argument(s){hint}"
            )
        return build(args, **kwargs)  # SQLGlot retries with dialect= on TypeError

    return checked


_BUILDERS = {
    "DATEADD": _date_delta(exp.DateAdd),
    "DATEDIFF": _date_delta(exp.DateDiff),
    "DATE_PART": _date_part,
}


class _OssieParser(Parser):
    # Only the catalog's functions get SQLGlot's typed nodes; any other name
    # parses as exp.Anonymous and prints as written. FUNCTION_PARSERS holds the
    # special syntaxes (CAST, EXTRACT, TRIM, SUBSTRING ... FROM, POSITION ... IN).
    # One of a name with a signature would skip its check: CEIL(x TO DAY) and
    # FLOOR(x TO DAY) are not the spec's, so those two parse as plain calls.
    FUNCTIONS = {
        **{name: Parser.FUNCTIONS[name] for name in _CATALOG if name in Parser.FUNCTIONS},
        **{name: _checked(name, _BUILDERS.get(name)) for name in _SIGNATURES},
    }
    FUNCTION_PARSERS = {
        name: parser
        for name, parser in Parser.FUNCTION_PARSERS.items()
        if name in _CATALOG and name not in _SIGNATURES
    }
    # ANY is x = ANY (subquery), which the spec has no use for; without this,
    # ClickHouse's any(x) printed as ANY(x), and ANY(x) + 1 parsed as ANY((x) + 1).
    NO_PAREN_FUNCTION_PARSERS = {
        name: parser for name, parser in Parser.NO_PAREN_FUNCTION_PARSERS.items() if name != "ANY"
    }

    def _parse_position(self, haystack_first: bool = False) -> exp.StrPosition:
        # POSITION(substr IN str) keeps its special syntax, which the signature check
        # skips; SQLGlot keeps three arguments of POSITION(a, b, c, d) and drops the rest.
        index = self._index
        if len(args := self._parse_csv(self._parse_bitwise)) > 3:
            self.raise_error(
                "expected POSITION(substr IN str) or POSITION(substr, str[, start]), "
                f"got {len(args)} argument(s)"
            )
        self._retreat(index)
        return super()._parse_position(haystack_first)

    def _parse_ordered(self, parse_method=None):
        ordered = super()._parse_ordered(parse_method)
        # The spec allows NULLS FIRST | LAST in a window's ORDER BY only; parse() checks
        # where it stands. The AST cannot tell an explicit NULLS LAST from the default,
        # so look at the tokens just read.
        last = [t.text.upper() for t in self._tokens[max(self._index - 2, 0) : self._index]]
        if ordered and last in (["NULLS", "FIRST"], ["NULLS", "LAST"]):
            ordered.meta["nulls"] = last[1]
        return ordered


class OssieSQL(Dialect):
    """SQLGlot's default dialect plus the spec shapes it parses differently.

    Until an upstream parser is on PyPI (#4); tests/test_expression_fixtures.py
    checks parse() against the spec's fixtures."""

    Parser = _OssieParser
    # NULLs sort last both ways where the spec says nothing, as in ClickHouse: a query's
    # ORDER BY, which the planner builds, and WITHIN GROUP. A window's ORDER BY has the
    # spec's default, NULL as the largest value, set in parse()
    # (docs/decisions/null-placement.md); this setting does not give it, as it would
    # flip every DESC the parser reads.
    NULL_ORDERING = "nulls_are_last"


class _QuestionParser(_OssieParser):
    """OssieSQL plus ``metric FILTER (WHERE condition)``: a condition on a metric name
    in an expression over metrics (docs/decisions/expressions-over-metrics.md). SQLGlot
    takes FILTER only after a function; here it also follows a column, and only a
    column: ``(m) FILTER (...)`` and ``1 FILTER (...)`` stay errors."""

    def _parse_column(self):
        column = super()._parse_column()
        if isinstance(column, exp.Column) and self._match_pair(TokenType.FILTER, TokenType.L_PAREN):
            self._match(TokenType.WHERE)
            where = self._parse_where(skip_where_token=True)
            self._match_r_paren()
            return self.expression(exp.Filter(this=column, expression=where))
        return column


class QuestionSQL(OssieSQL):
    Parser = _QuestionParser


_DISALLOWED = (
    exp.Select, exp.Subquery, exp.With, exp.Union, exp.Intersect, exp.Except, exp.Join,
    exp.Create, exp.Drop, exp.Alter, exp.Insert, exp.Update, exp.Delete,
    exp.Placeholder, exp.Parameter,
)  # fmt: skip


def _bound(value: str | exp.Expression | None, side: str | None) -> str:
    """A frame bound in upper case, a number as N: UNBOUNDED PRECEDING, N PRECEDING..."""
    if isinstance(value, exp.Literal) and value.is_number:
        value = "N"
    return f"{value or 'CURRENT ROW'} {side or ''}".strip().upper()


# Spec frames: ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW, ROWS BETWEEN n PRECEDING
# AND n FOLLOWING, RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW; its examples also
# use n PRECEDING AND CURRENT ROW. ROWS frames that hold the current row are accepted.
# Others go wrong in ClickHouse: an empty frame sums to 0, not NULL, and RANGE offsets
# put NULL keys within any offset.
_ROWS_STARTS = {"UNBOUNDED PRECEDING", "N PRECEDING", "CURRENT ROW"}
_ROWS_ENDS = {"CURRENT ROW", "N FOLLOWING", "UNBOUNDED FOLLOWING"}


def _frame_problem(spec: exp.WindowSpec) -> str | None:
    kind = (spec.args.get("kind") or "").upper()
    start = _bound(spec.args.get("start"), spec.args.get("start_side"))
    end = _bound(spec.args.get("end"), spec.args.get("end_side"))
    if kind == "ROWS" and start in _ROWS_STARTS and end in _ROWS_ENDS:
        return None
    if (kind, start, end) == ("RANGE", "UNBOUNDED PRECEDING", "CURRENT ROW"):
        return None
    return (
        f"window frame {spec.sql()} is not supported; use ROWS BETWEEN UNBOUNDED or "
        "n PRECEDING (or CURRENT ROW) AND CURRENT ROW (or n FOLLOWING), or "
        "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
    )


def _in_window_order(node: exp.Ordered) -> bool:
    return isinstance(node.parent, exp.Order) and isinstance(node.parent.parent, exp.Window)


def _in_within_group(node: exp.Ordered) -> bool:
    return isinstance(node.parent, exp.Order) and isinstance(node.parent.parent, exp.WithinGroup)


def _round_digits(node: exp.Round) -> int | None:
    """The d of ROUND(x, d), 0 without one; None unless a whole number literal."""
    d = node.args.get("decimals")
    if d is None:
        return 0
    sign = 1
    if isinstance(d, exp.Neg):
        d, sign = d.this, -1
    if isinstance(d, exp.Literal) and d.is_int:
        return sign * int(d.this)
    return None


# Spec, expression_language.md "Measure Evaluation: AGG" and spec.md "Metric Scoping"
# rule 6 (apache/ossie#703): the name is reserved in every capitalization, and only a
# model's metric may call it, as AGG(dataset.metric), without FILTER. SQLGlot parses
# it as an anonymous function, which would reach ClickHouse: parse() checks the shape,
# the planner expands the call into the dataset's metric and refuses it anywhere else,
# and untranslatable() refuses it in a field or a dataset's metric
# (docs/decisions/agg-dataset-metric.md). AGG(source.x) in a dataset's metric, which the
# spec allows over a measure relation (apache/ossie#726), has a reason of its own,
# AGG_SOURCE_RULE.
AGG_RULE = (
    "this executor allows AGG only in a model's metric, as AGG(dataset.metric): a single "
    "reference to a metric of a dataset, without FILTER, OVER or another modifier"
)


def agg_call(node: exp.Expression) -> bool:
    """A call of AGG, in any capitalization, in a tree from parse()."""
    return isinstance(node, exp.Anonymous) and node.name.upper() == "AGG"


def agg_calls(tree: exp.Expression) -> list[exp.Anonymous]:
    return [node for node in tree.find_all(exp.Anonymous) if agg_call(node)]


# Spec, expression_language.md "Metric Definition: METRIC (RESERVED)" (apache/ossie#774):
# the name is reserved in every capitalization, an expression MUST NOT call it, and an
# implementation MUST NOT give it another meaning. A field, a metric or a column named
# metric stays allowed: only a call is reserved. SQLGlot parses the call as an anonymous
# function, which would reach ClickHouse or be checked against system.functions, so
# parse() refuses it, in every expression the executor reads: a field, a metric of a
# dataset or of the model, a filter, an expression of a question.
_METRIC_RESERVED = (
    "METRIC is a function name that the spec reserves (expression_language.md, "
    '"Metric Definition: METRIC (RESERVED)"); no expression may call it, and this '
    "executor gives it no meaning"
)
# In a field or a metric of the model: the metric's expression is METRIC's operand.
METRIC_RULE = (
    f"{_METRIC_RESERVED}. A metric's expression is the operand of METRIC already: write "
    "SUM(amount), not METRIC(SUM(amount))"
)


def metric_call(node: exp.Expression) -> bool:
    """A call of METRIC, in any capitalization, in a tree from SQLGlot."""
    return isinstance(node, exp.Anonymous) and node.name.upper() == "METRIC"


def metric_refusal(node: exp.Anonymous, named: bool = False) -> str:
    """Why a call of METRIC is refused. With ``named``, in a question's expression or
    filter, which names a model's metric and defines none: the advice is to name it,
    by the call's own argument when that is a single column."""
    call = node.sql()
    if not named:
        return f"{call} is not allowed; {METRIC_RULE}"
    arg = node.expressions[0] if len(node.expressions) == 1 else None
    metric = arg.sql() if isinstance(arg, exp.Column) else "the metric's name"
    return (
        f"{call} is not allowed; {_METRIC_RESERVED}. A question names a metric of the "
        f"model itself: write {metric}, not {call}"
    )


def _agg_shape_problem(node: exp.Anonymous) -> str | None:
    """Why a call of AGG is not AGG(dataset.metric) without a modifier, or None."""
    args = node.expressions
    arg = args[0] if len(args) == 1 else None
    if (
        not isinstance(arg, exp.Column)
        or isinstance(arg.this, exp.Star)
        or arg.args.get("db")
        or not arg.table
    ):
        return f"{node.sql()} is not allowed; {AGG_RULE}"
    # The spec forbids FILTER. OVER would put the window over the dataset metric's
    # input rows, not over its values; SUM(AGG(d.m)) OVER () is a window over them.
    # IGNORE NULLS, RESPECT NULLS and WITHIN GROUP would land on the dataset metric's
    # whole expression. The outermost modifier names the call in the message.
    modifiers = exp.Filter | exp.Window | exp.RespectNulls | exp.IgnoreNulls | exp.WithinGroup
    inner, outer = node, None
    while isinstance(inner.parent, modifiers) and inner.arg_key == "this":
        inner = outer = inner.parent
    if outer is not None:
        return f"{outer.sql()} is not allowed; {AGG_RULE}"
    return None


# Spec, spec.md "Metric Scoping" rules 7 and 9 (apache/ossie#726): a dataset's metric
# may call AGG(source.measure) when its source is a measure relation; a consumer must
# refuse AGG of a column that is no measure of the source, "including when the source
# has no measures", and must not run it as a plain aggregate. A ClickHouse table
# declares no measures (docs/decisions/agg-dataset-metric.md).
AGG_SOURCE_RULE = (
    'AGG(source.measure) needs a source that is a measure relation (spec, "Metric '
    'Scoping" rules 7 and 9), and a ClickHouse table has no measure columns; aggregate '
    "the column itself, as SUM(source.column), if that is what it holds"
)


def agg_refusal(tree: exp.Expression, where: str) -> str | None:
    """Why ``tree``, an expression in ``where``, cannot call AGG; None without a call."""
    calls = agg_calls(tree)
    if not calls:
        return None
    call, arg = calls[0], calls[0].expressions[0] if calls[0].expressions else None
    if (
        where == "a metric of a dataset"
        and isinstance(arg, exp.Column)
        and arg.table.upper() == SOURCE_QUALIFIER
    ):
        return f"{call.sql()} in {where} is not allowed; {AGG_SOURCE_RULE}"
    return f"{call.sql()} in {where} is not allowed; {AGG_RULE}"


# Spec, spec.md "Fields" and "Expression Object": a field's expression is scalar, with
# no aggregation; a window function gives one value for each row and is allowed
# (apache/ossie#792, validate_field_aggregates). Sent as written, an aggregate in a
# field lands in GROUP BY, which ClickHouse refuses, or in a filter, which the planner
# reads as a condition on groups (HAVING) and answers with another number
# (docs/decisions/field-aggregates.md).
FIELD_AGGREGATE_RULE = (
    "a field's expression is scalar, without an aggregate outside a window (spec, "
    '"Fields"); add OVER to make the aggregate a window function, or write it as a metric'
)

_WINDOWED_CALL = exp.Filter | exp.WithinGroup | exp.IgnoreNulls | exp.RespectNulls


def unwindowed_aggregate(
    tree: exp.Expression, aggregate: Callable[[str], bool] | None = None
) -> exp.Expression | None:
    """The first aggregate in ``tree`` that is not the function of a window, or None.

    An aggregate in a window's PARTITION BY or ORDER BY, or in the argument of the
    windowed call (SUM(SUM(x)) OVER ()), is one. APPROX_PERCENTILE is a plain call to
    SQLGlot and counts by name; a function outside the spec counts when ``aggregate``
    says its name is one (the planner types those as aggregates itself). AGG is
    agg_refusal's."""
    for node in tree.find_all(exp.AggFunc, exp.Anonymous, bfs=False):
        if isinstance(node, exp.Anonymous) and not (
            node.name.upper() == "APPROX_PERCENTILE"
            or (aggregate is not None and outside_spec(node) and aggregate(node.name))
        ):
            continue
        inner = node
        while isinstance(inner.parent, _WINDOWED_CALL) and inner.arg_key == "this":
            inner = inner.parent
        if not (isinstance(inner.parent, exp.Window) and inner.arg_key == "this"):
            return node
    return None


def _shape_problem(node: exp.Expression) -> str | None:
    """Spec shapes an argument count cannot express."""
    # SQLGlot reads ClickHouse's quantile(0.5)(x) as quantile(0.5) AS (x): SQL that
    # fails, with x an alias the grant checks never see as a column.
    if isinstance(node, exp.Aliases):
        return "ClickHouse's parametric call name(params)(args) is not supported"
    if isinstance(node, exp.Alias):
        return "an alias (AS) is not allowed in an expression"
    if agg_call(node):
        return _agg_shape_problem(node)
    if isinstance(node, exp.PercentileCont | exp.PercentileDisc):
        signature = _SIGNATURES[node.sql_name()][0]
        if not isinstance(node.parent, exp.WithinGroup):
            return f"expected {signature}"
        keys = len(node.parent.expression.expressions)
        if keys != 1:
            return f"expected {signature}, got {keys} ORDER BY keys"
        # The PERCENTILE_DISC rewrite is several aggregates, which one OVER cannot
        # cover: refuse it here rather than send SQL ClickHouse rejects.
        outer = node.parent.parent
        while isinstance(outer, exp.Filter | exp.RespectNulls | exp.IgnoreNulls):
            outer = outer.parent
        if isinstance(node, exp.PercentileDisc) and isinstance(outer, exp.Window):
            return "PERCENTILE_DISC cannot take OVER"
    # FILTER belongs to an aggregate; the rewrite would push it into any inside.
    if isinstance(node, exp.Filter) and not isinstance(
        node.this, exp.AggFunc | exp.WithinGroup | exp.Anonymous
    ):
        return f"FILTER applies to an aggregate, not to {node.this.sql()}"
    # The signature check counts DISTINCT x, y as one argument.
    if isinstance(node, exp.Median) and isinstance(node.this, exp.Distinct):
        if (n := len(node.this.expressions)) != 1:
            return f"expected MEDIAN(expr), got {n} argument(s)"
    if isinstance(node, exp.Extract) and node.name.upper() not in _DATE_PARTS:
        return f"{node.name} is not a date part; expected one of " + ", ".join(sorted(_DATE_PARTS))
    if isinstance(node, exp.WindowSpec):
        return _frame_problem(node)
    # NULLS FIRST | LAST is the spec's in a window's ORDER BY. The reference grammar
    # (core-spec/grammar, apache/ossie#657) and compliance/expressions/valid.txt also
    # accept it in WITHIN GROUP, where it changes nothing: a percentile skips NULLs.
    if (
        isinstance(node, exp.Ordered)
        and "nulls" in node.meta
        and not _in_window_order(node)
        and not _in_within_group(node)
    ):
        return f"NULLS {node.meta['nulls']} is allowed only in a window's ORDER BY or WITHIN GROUP"
    # Rejected by compliance/expressions/invalid.txt; SQLGlot reads both.
    if isinstance(node, exp.In) and not node.expressions and not node.args.get("query"):
        return "IN needs at least one value"
    if isinstance(node, exp.Distinct) and not node.expressions and not node.args.get("on"):
        return "DISTINCT needs an argument"
    # ClickHouse accepts IGNORE NULLS here and silently ignores it.
    if isinstance(node, exp.IgnoreNulls) and isinstance(
        node.this, exp.Lag | exp.Lead | exp.NthValue
    ):
        return f"IGNORE NULLS is not supported for {node.this.sql_name()}"
    return None


def parse(expression: str, question: bool = False, named: bool = False) -> exp.Expression:
    """Parse an Ossie expression, rejecting constructs the spec disallows. With
    ``question``, a metric name may take a FILTER (QuestionSQL): only an expression over
    metrics in a question does. ``question`` and ``named`` (a question's filter) say that
    the text names the model's metrics, which changes the advice a call of METRIC gets."""
    try:
        tree = sqlglot.parse_one(expression, read=QuestionSQL if question else OssieSQL)
    except sqlglot.errors.SqlglotError as e:
        raise ValueError(f"cannot parse {expression!r}: {str(e).splitlines()[0]}") from e
    for node in tree.walk():
        if isinstance(node, _DISALLOWED):
            raise ValueError(
                f"{type(node).__name__} is not allowed in an Ossie expression: {expression!r}"
            )
        if metric_call(node):
            raise ValueError(f"{metric_refusal(node, question or named)}: {expression!r}")
        filtered_metric = (
            question and isinstance(node, exp.Filter) and isinstance(node.this, exp.Column)
        )
        if not filtered_metric and (problem := _shape_problem(node)):
            raise ValueError(f"{problem}: {expression!r}")
        # Spec, "Window ORDER BY NULL Placement": without NULLS FIRST | LAST, NULL is the
        # largest value, so DESC puts it first. ClickHouse's default is last both ways,
        # which already is the spec's for ASC. Set here, not in a rewrite, which reads
        # meta["nulls"] after a copy.
        if (
            isinstance(node, exp.Ordered)
            and node.args.get("desc")
            and "nulls" not in node.meta
            and _in_window_order(node)
        ):
            node.set("nulls_first", True)
    return tree


def source_columns(tree: exp.Expression, ds: OssieDataset) -> list[str]:
    """The source columns a parsed metric of ``ds`` reads as ``source.column``, in order
    of appearance. Empty when ``ds`` declares a field named ``source``: the qualifier is
    then that field, and the planner refuses the metric (spec, "Metric Scoping")."""
    if not reads_source(ds):
        return []
    return [
        c.name for c in tree.find_all(exp.Column) if c.table and c.table.upper() == SOURCE_QUALIFIER
    ]


def outside_spec(node: exp.Expression) -> bool:
    """A call, in a tree from parse(), of a function that is not the spec's. AGG is
    the spec's, and the planner expands it before any SQL is built. METRIC is reserved
    and parse() refuses it, so it never reaches the server."""
    return isinstance(node, exp.Anonymous) and node.name.upper() not in _CATALOG | {
        "AGG",
        "METRIC",
    }


def passthrough_functions(tree: exp.Expression) -> set[str]:
    """Function names in a tree from parse() that are not the spec's.

    They reach ClickHouse as written, so only the server can say whether they
    exist (Executor checks them against system.functions)."""
    return {f.name for f in tree.find_all(exp.Anonymous) if outside_spec(f)}


# --- ClickHouse rewrites ----------------------------------------------------


def _f(name: str, *args: exp.Expression | None) -> exp.Anonymous:
    return exp.Anonymous(this=name, expressions=[a for a in args if a is not None])


def _param_agg(name: str, param: exp.Expression, arg: exp.Expression) -> exp.Expression:
    """ClickHouse parametric aggregate: name(param)(arg)."""
    return exp.ParameterizedAgg(this=exp.to_identifier(name), expressions=[param], params=[arg])


# Spec TO_CHAR / TO_DATE format tokens -> ClickHouse formatDateTime / parseDateTime.
_FORMAT_TOKENS = {
    "YYYY": "%Y", "YY": "%y", "MONTH": "%M", "MON": "%b", "MM": "%m", "MI": "%i",
    "DD": "%d", "DAY": "%W", "DY": "%a", "HH24": "%H", "HH12": "%I", "HH": "%I",
    "SS": "%S", "AM": "%p", "PM": "%p",
}  # fmt: skip
_FORMAT_RE = re.compile("|".join(sorted(_FORMAT_TOKENS, key=len, reverse=True)) + "|%")


def convert_format(fmt: str) -> str:
    return _FORMAT_RE.sub(lambda m: "%%" if m[0] == "%" else _FORMAT_TOKENS[m[0]], fmt)


def _fmt_literal(node: exp.Expression | None) -> exp.Literal:
    if not isinstance(node, exp.Literal) or not node.is_string:
        raise ValueError(
            f"date format must be a string literal, got {node.sql() if node else None}"
        )
    return exp.Literal.string(convert_format(node.this))


# ClickHouse's EXTRACT does not accept these parts.
_EXTRACT_PARTS = {
    "DAYOFWEEK": "toDayOfWeek",
    "DAYOFYEAR": "toDayOfYear",
}

# Functions SQLGlot keeps as exp.Anonymous (no dedicated node): name -> rewrite(args).
_ANONYMOUS = {
    # quantileTDigest refuses Decimal, as quantileExactInclusive does (_percentile_cont).
    "APPROX_PERCENTILE": lambda a: _param_agg("quantileTDigest", a[1], _to_float(a[0])),
    "TO_DATE": lambda a: (
        _f("toDate", a[0])
        if len(a) == 1
        else _f("toDate", _f("parseDateTime", a[0], _fmt_literal(a[1])))
    ),
    "IFF": lambda a: _f("if", *a),
    "ZEROIFNULL": lambda a: _f("ifNull", a[0], exp.Literal.number(0)),
    "NULLIFZERO": lambda a: _f("nullIf", a[0], exp.Literal.number(0)),
}


# The same, for those that read a date or time in the model's time zone: (args, zone).
_ZONED = {
    "TO_TIMESTAMP": lambda a, z: (
        _f("parseDateTimeBestEffort", a[0], z)
        if len(a) == 1
        else _f("parseDateTime", a[0], _fmt_literal(a[1]), z)
    ),
}

# ClickHouse's own clock, written in a filter or a model, is the server's zone without
# a zone argument; the answer must not be, as with CURRENT_DATE. Agents write today()
# for today (#56). None keeps the call as written: a zone is already there.
_CLOCKS = {
    "NOW": lambda a, z: None if a else _f("now", z),
    "NOW64": lambda a, z: None if len(a) > 1 else _f("now64", *a or [exp.Literal.number(3)], z),
    "TODAY": lambda a, z: None if a else _f("toDate", _f("now", z)),
    "YESTERDAY": lambda a, z: (
        None if a else _f("subtractDays", _f("toDate", _f("now", z)), exp.Literal.number(1))
    ),
}
# ClickHouse's aliases of the same functions (system.functions alias_to).
_CLOCKS |= {"CURDATE": _CLOCKS["TODAY"], "LOCALTIMESTAMP": _CLOCKS["NOW"]}

# With a zone too: a string without an offset is read in the model's, not the server's.
_TIMESTAMPS = (
    exp.DType.TIMESTAMP,
    exp.DType.TIMESTAMPNTZ,
    exp.DType.TIMESTAMPTZ,
    exp.DType.TIMESTAMPLTZ,
    exp.DType.DATETIME,
)
_FRACTION = re.compile(r":\d\d\.(\d{1,9})")

_ONE = exp.Literal.number("1.0")


def _to_float(x: exp.Expression) -> exp.Expression:
    """``x * 1.0``: Float64. Unlike toFloat64, ClickHouse refuses it for a Date,
    DateTime or String, as Postgres does, where toFloat64 would take a date's day
    count or parse the string."""
    return exp.Mul(this=x, expression=_ONE.copy())


def _percentile_cont(p: exp.Expression, arg: exp.Expression) -> exp.Expression:
    """Spec PERCENTILE_CONT (Postgres semantics): exact, interpolated between the two
    nearest values, NULL for an empty set.

    ClickHouse's quantile and median keep a sample of 8192 values, so they are exact
    only below that. quantileExactInclusive interpolates as the spec does but rejects
    Decimal (Postgres computes in double precision too) and answers an empty set with
    nan, which COALESCE and comparisons take for a value. OrNull makes that NULL and,
    unlike if(COUNT(x) = 0, ...), keeps a single aggregate that FILTER can apply to.

    The argument is made Float64 with _to_float; quantileExactInclusive alone would
    answer a date with its day count.
    """
    if isinstance(arg, exp.Distinct):
        arg = exp.Distinct(expressions=[_to_float(arg.expressions[0])])
    else:
        arg = _to_float(arg)
    return _param_agg("quantileExactInclusiveOrNull", p, arg)


def _round(node: exp.Round) -> exp.Expression:
    """Spec ROUND (expression_language.md, "Numeric Semantics"): a value exactly halfway
    rounds away from zero, ROUND(2.5, 0) = 3 and ROUND(0.125, 2) = 0.13.

    ClickHouse's round does that for integers and Decimal but rounds a Float tie to
    even (round(2.5) = 2, round(0.125, 2) = 0.12), and the translator does not know an
    argument's type. So every ROUND is the spec's equivalent expression: with
    y = abs(x) * 10^d, sign(x) * (floor(y) + if(2 * (y - floor(y)) >= 1, 1, 0)) / 10^d.
    Integer factors and literals keep Decimal arithmetic in Decimal, exact and at x's
    scale; a Float tie is best effort, as the spec says (2.675 is 2.68: 2.675 * 100 is
    267.5 in Float64). Integers come back as Int64 for d = 0 and as Float64 otherwise,
    exact below 2^53.

    A negative d rounds to a multiple of g = 10^-d through the rest r of abs(x) after
    the multiples of g, so no factor is a fraction: sign(x) * (q + if(2 * r >= g, 1,
    0)) * g with q = floor(abs(x) / g). Decimal division keeps x's scale and drops
    digits, which floor drops anyway; r is not x % g, which is wrong for a Decimal
    (the MOD rule below).

    The quotient is divide(), not exp.Div, which the rewrite would make Float64 when it
    runs again over an inlined metric.

    A d that is not a whole number literal, a column say, keeps ClickHouse's round:
    the spec writes 10^d as an exact literal, and round is right there for integers
    and Decimal; a Float tie rounds to even, as it did."""
    x, digits = node.this, _round_digits(node)
    if digits is None:
        return node
    one, two = exp.Literal.number(1), exp.Literal.number(2)
    zero = exp.Literal.number(0)
    size = _f("abs", x.copy())
    if digits >= 0:
        factor = exp.Literal.number(10**digits)
        y = exp.Mul(this=size, expression=factor.copy()) if digits else size
        whole = _f("floor", y.copy())
        half = exp.Mul(this=two, expression=exp.Sub(this=y, expression=whole.copy()))
        up = _f("if", exp.GTE(this=half, expression=one), one.copy(), zero)
        rounded = exp.Mul(this=_f("sign", x.copy()), expression=exp.Add(this=whole, expression=up))
        return _f("divide", rounded, factor) if digits else rounded
    step = exp.Literal.number(10**-digits)
    whole = _f("floor", _f("divide", size, step.copy()))
    rest = exp.Sub(this=size.copy(), expression=exp.Mul(this=whole.copy(), expression=step.copy()))
    half = exp.Mul(this=two, expression=rest)
    up = _f("if", exp.GTE(this=half, expression=step.copy()), one, zero)
    count = exp.Add(this=whole, expression=up)
    return exp.Mul(this=_f("sign", x.copy()), expression=exp.Mul(this=count, expression=step))


def _timestamp_literal(node: exp.Cast, zone: exp.Literal) -> exp.Expression:
    """A wall-clock timestamp written as a string, read in the model's zone.

    ClickHouse reads CAST(s AS DateTime) in the server's zone, which a model's
    answers must not depend on, and drops a fraction. An offset in the string
    (``+00:00``, as values are shown) still names the instant. TRY_CAST is
    accurateCastOrNull to the zoned type: toDateTimeOrNull returns NULL for a
    string with an offset."""
    text = node.this.this
    fraction = _FRACTION.search(text)
    digits = exp.Literal.number(len(fraction[1])) if fraction else None
    if isinstance(node, exp.TryCast):
        args = ", ".join(a.sql("clickhouse") for a in (digits, zone) if a is not None)
        to = exp.Literal.string(f"{'DateTime64' if fraction else 'DateTime'}({args})")
        return _f("accurateCastOrNull", node.this, to)
    return _f("toDateTime64" if fraction else "toDateTime", node.this, digits, zone)


def rewrite(node: exp.Expression, zone: str = DEFAULT_TIME_ZONE) -> exp.Expression:
    """``node`` for ClickHouse. ``zone`` is the model's time zone (model.time_zone())."""
    if isinstance(node, exp.Anonymous):
        name = node.name.upper()
        if name in _ZONED:
            return _ZONED[name](node.expressions, exp.Literal.string(zone))
        if name in _CLOCKS:
            return _CLOCKS[name](node.expressions, exp.Literal.string(zone)) or node
        fn = _ANONYMOUS.get(name)
        return fn(node.expressions) if fn else node
    # The current date and a wall-clock time are those of the model's zone; now(zone)
    # is the same instant as now(), so comparisons and differences do not change.
    if isinstance(node, exp.CurrentTimestamp):
        return _f("now", exp.Literal.string(zone))
    if isinstance(node, exp.CurrentDate):
        return _f("toDate", _f("now", exp.Literal.string(zone)))
    if isinstance(node, exp.CurrentTime):
        return _f("toTime", _f("now", exp.Literal.string(zone)))
    # dateDiff counts boundaries in its arguments' zone, and when they have two it
    # miscounts hours and longer: a DateTime('UTC') column 2 hours before now('America/
    # New_York') is 7 hours apart. Counted in the model's zone, as CURRENT_DATE is.
    if isinstance(node, exp.DateDiff):
        unit = exp.Literal.string(node.unit.name.lower())
        return _f("dateDiff", unit, node.expression, node.this, exp.Literal.string(zone))
    if (
        isinstance(node, exp.Cast)  # TryCast too
        and node.this.is_string
        and node.to.this in _TIMESTAMPS
    ):
        return _timestamp_literal(node, exp.Literal.string(zone))
    if isinstance(node, exp.Round):
        return _round(node)
    # ClickHouse's modulo does not align Decimal scales: toDecimal64(125.50, 2) % 7 is
    # 0.06, the rest of 12550 % 7, and % toDecimal64(7, 0) is too; the spec's MOD is
    # 6.5. intDiv aligns them and truncates toward zero, so a - b * intDiv(a, b) keeps
    # the dividend's sign, as % does; integers stay integers and Decimal stays Decimal,
    # exact. Rewritten twice (an inlined metric), the minus and intDiv stay.
    if isinstance(node, exp.Mod):
        a, b = node.this, node.expression
        quotient = _f("intDiv", a.copy(), b.copy())
        return exp.Sub(this=a, expression=exp.Mul(this=b, expression=quotient))
    if isinstance(node, exp.Median):
        return _percentile_cont(exp.Literal.number(0.5), node.this)
    if isinstance(node, exp.WithinGroup) and isinstance(
        node.this, exp.PercentileCont | exp.PercentileDisc
    ):
        order = node.expression.expressions[0]
        p, arg, desc = node.this.this, order.this, bool(order.args.get("desc"))
        if isinstance(node.this, exp.PercentileCont):
            if desc:
                p = exp.Sub(this=exp.Literal.number(1), expression=p)
            return _percentile_cont(p, arg)
        # Spec (Postgres semantics): the first value whose cumulative share reaches p,
        # so element ceil(p * n) of the sorted values (1-based, at least 1), NULL for
        # an empty set. quantileExact takes element floor(p * n) + 1: one too high
        # whenever p * n is whole.
        n = exp.Count(this=arg)
        index = _f(
            "toUInt64",
            exp.Greatest(
                this=exp.Ceil(this=exp.Mul(this=p, expression=n)),
                expressions=[exp.Literal.number(1)],
            ),
        )
        values = _f(
            "arrayReverseSort" if desc else "arraySort",
            exp.AnonymousAggFunc(this="groupArray", expressions=[arg]),
        )
        return _f(
            "if",
            exp.EQ(this=n.copy(), expression=exp.Literal.number(0)),
            exp.Null(),
            _f("arrayElement", values, index),
        )
    # FILTER applies to one aggregate, and ClickHouse reads it as the -If combinator.
    # A rewrite above turned the aggregate under it into an expression of several
    # (PERCENTILE_DISC), so each of them takes the condition. An aggregate SQLGlot does
    # not know (Anonymous) holds none and keeps its FILTER.
    if isinstance(node, exp.Filter) and not isinstance(node.this, exp.AggFunc):
        aggs = list(node.this.find_all(exp.AggFunc))
        if not aggs:
            return node
        for agg in aggs:
            agg.replace(exp.Filter(this=agg.copy(), expression=node.expression.copy()))
        return node.this
    # ClickHouse keeps the dividend's scale when it divides a Decimal: 10.00 / 350.00 is
    # 0.02, and SUM(price) / 3 drops digits the same way. Postgres (numeric) and DuckDB
    # (double) give 0.0285714..., so the dividend becomes Float64, as DuckDB divides;
    # integers already divide to Float64 in ClickHouse.
    if isinstance(node, exp.Div):
        # Idempotent: a metric inlined into a filter is rewritten again with it. The
        # quotient of a division is Float64 already (a / b / c).
        this = node.this.unnest()
        floated = isinstance(this, exp.Mul) and this.expression == _ONE
        if not floated and not isinstance(this, exp.Div):
            node.set("this", _to_float(node.this))
        return node
    # ClickHouse's log takes one argument, the natural logarithm. SQLGlot prints LOG(2, x)
    # and LOG(10, x) as log2 and log10, and any other base as written, which fails.
    if isinstance(node, exp.Log) and node.expression:
        base = node.this
        # SQLGlot's generator tells 2 and 10 by the text, past a minus sign: LOG(-2, x)
        # printed as log2(x). By value here, so 2.0 and 1e1 stay exact; LN(x) / LN(10.0)
        # is 2.9999999999999996 for x = 1000.
        if isinstance(base, exp.Literal) and not base.is_string:
            value = Decimal(base.this)
            if value in (2, 10):
                node.set("this", exp.Literal.number(int(value)))
                return node
        # Floated as the Div rule floats it: rewritten twice, the same.
        return exp.Div(this=_to_float(exp.Ln(this=node.expression)), expression=exp.Ln(this=base))
    if isinstance(node, exp.Extract) and node.name.upper() in _EXTRACT_PARTS:
        return _f(_EXTRACT_PARTS[node.name.upper()], node.expression)
    if isinstance(node, exp.ToChar):
        return _f("formatDateTime", node.this, _fmt_literal(node.args.get("format")))
    # Spec: the first match. ClickHouse's regexpExtract returns the first capture
    # group when the pattern has one; group 0 is the whole match, as in DuckDB. A
    # third argument is the group in both, though SQLGlot reads it as a position.
    if isinstance(node, exp.RegexpExtract):
        group = node.args.get("position") or exp.Literal.number(0)
        return _f("regexpExtract", node.this, node.expression, group)
    # SPLIT_PART is NULL when an argument is. splitByString returns an Array, which
    # ClickHouse never makes Nullable: a NULL string gives an empty one and the part
    # '', and a NULL delimiter splits into characters. A NULL part is NULL already.
    if isinstance(node, exp.SplitPart):
        text, delimiter = node.this, node.args["delimiter"]
        parts = _f("splitByString", delimiter, text)
        return _f(
            "if",
            exp.or_(
                exp.Is(this=text.copy(), expression=exp.Null()),
                exp.Is(this=delimiter.copy(), expression=exp.Null()),
            ),
            exp.Null(),
            _f("arrayElement", parts, node.args["part_index"]),
        )
    # Spec windows follow ANSI SQL: NULL when the row does not exist, and NULLs are
    # respected. ClickHouse returns the type's default (0) for a non-Nullable argument
    # and skips NULLs in first_value / last_value. A Nullable argument also lets a
    # NULL or Nullable default through (ClickHouse wants the argument's type).
    if isinstance(node, exp.Lag | exp.Lead | exp.NthValue):
        # Idempotent: a metric inlined into a filter is rewritten again with it.
        if not (isinstance(node.this, exp.Anonymous) and node.this.name == "toNullable"):
            node.set("this", _f("toNullable", node.this))
        return node
    if isinstance(node, exp.FirstValue | exp.LastValue) and not isinstance(
        node.parent, exp.RespectNulls | exp.IgnoreNulls
    ):
        return exp.RespectNulls(this=node)
    return node


# Nodes SQLGlot prints between or before their operands. AND, OR and COLLATE are also
# functions to SQLGlot; other functions print as calls.
_OPERATOR = exp.Binary | exp.Unary | exp.Predicate


def _operator(node: exp.Expression | None) -> bool:
    if isinstance(node, exp.Connector | exp.Collate):
        return True
    return isinstance(node, _OPERATOR) and not isinstance(node, exp.Func | exp.Paren)


def rewrite_tree(
    tree: exp.Expression,
    zone: str = DEFAULT_TIME_ZONE,
    keep: Callable[[exp.Expression], bool] | None = None,
) -> exp.Expression:
    """A copy of ``tree`` with every node rewritten for ClickHouse, except those
    ``keep`` says to leave as they are (clickhouse_problem).

    SQLGlot prints a tree as it stands, without parentheses for precedence, so an
    operator a rewrite builds or an inlined field or metric brings under another
    operator would be regrouped: ``net * 2``, ``net`` being ``amount - 5``, must not
    print as ``amount - 5 * 2``. Every operator under another operator ends up in
    parentheses, except the left operand of the same operator (``a - b - c``), which
    reads the same without them."""
    tree = tree.copy()
    # Bottom-up, so a rewrite that replaces a node still sees its arguments rewritten;
    # Expression.transform does not descend into a replaced node.
    for node in reversed(list(tree.dfs())):
        parent, arg_key, index = node.parent, node.arg_key, node.index
        new = node if keep and keep(node) else rewrite(node, zone)
        if new is node:
            continue
        if parent is None:
            tree = new
        else:
            parent.set(arg_key, new, index)
    for node in list(tree.walk()):
        parent, arg_key, index = node.parent, node.arg_key, node.index
        # A list element (IN (a + 1, 2)) is set off by commas already. ESCAPE and the
        # dot bind tighter than any operator: `(x LIKE 'a!%') ESCAPE '!'` does not parse.
        if index is not None or isinstance(parent, exp.Escape | exp.Dot):
            continue
        if not (_operator(node) and _operator(parent)):
            continue
        if arg_key == "this" and type(node) is type(parent) and isinstance(node, exp.Binary):
            continue
        parent.set(arg_key, exp.paren(node, copy=False), index)
    return tree


def to_clickhouse(expression: str | exp.Expression, zone: str = DEFAULT_TIME_ZONE) -> str:
    """ClickHouse SQL for an Ossie expression."""
    tree = parse(expression) if isinstance(expression, str) else expression
    return rewrite_tree(tree, zone).sql(dialect="clickhouse")


def translate(expression: OssieExpression) -> str:
    """ClickHouse SQL for a model field or metric expression."""
    return to_clickhouse(pick_expression(expression))


# --- ClickHouse's own SQL --------------------------------------------------
#
# A text in the CLICKHOUSE dialect is ClickHouse SQL. It is read as any other text
# (parse, rewrite_tree) where the SQL that reaches ClickHouse is what its author wrote,
# give or take the differences below; elsewhere it is refused with what to write
# instead. Accepting what is sent as written, rather than refusing a list of known
# differences, keeps a difference no list names from changing an answer
# (docs/decisions/clickhouse-dialect.md).

# Why a construct ClickHouse reads differently is refused, and what to write instead.
# Each test takes a node of the text read as Ossie SQL and the text as ClickHouse reads
# it (_canonical), and holds only where that node is read otherwise: a text refused for
# another reason is not refused in the name of a construct it writes alike.
_CLICKHOUSE_READINGS: tuple[tuple[Callable[[exp.Expression, exp.Expression], bool], str], ...] = (
    (
        lambda n, _: isinstance(n, exp.Length),
        "`length` counts characters in Ossie SQL and bytes in ClickHouse; call "
        "`lengthUTF8` for characters, or `OCTET_LENGTH` for bytes or an array's elements",
    ),
    (
        # position(needle IN haystack) is read alike, position(a, b) is not.
        lambda n, written: (
            isinstance(n, exp.StrPosition)
            and n.sql(dialect="clickhouse")
            not in {p.sql(dialect="clickhouse") for p in written.find_all(exp.StrPosition)}
        ),
        "`position(haystack, needle)` takes the string first in ClickHouse and the "
        "substring first in Ossie SQL; write `position(needle IN haystack)`, which "
        "both read alike",
    ),
    (
        lambda n, _: isinstance(n, exp.Bracket),
        "an array index counts from 1 in ClickHouse and from 0 in this executor's "
        "reading of `[i]`; call `arrayElement(array, i)`",
    ),
    (
        # round(x, d) with a d that is not a whole number literal is sent as written.
        lambda n, _: isinstance(n, exp.Round) and _round_digits(n) is not None,
        "`round` takes a Float tie to the even neighbour in ClickHouse and away from "
        "zero in Ossie SQL; call `roundBankers` for the even neighbour of a Float, or "
        "write the expression as `sql`, whose ROUND takes every tie away from zero, as "
        "ClickHouse's `round` does for a Decimal",
    ),
    (
        lambda n, _: isinstance(n, exp.Mod),
        "`%` keeps Decimal scales apart in ClickHouse and aligns them in Ossie SQL; call "
        "`modulo(a, b)` for ClickHouse's, or write the expression as `sql` for the "
        "spec's MOD",
    ),
    (
        lambda n, _: isinstance(n, exp.Median),
        "`median` is approximate in ClickHouse and exact in Ossie SQL; write the "
        "expression as `sql`, where MEDIAN is exact",
    ),
    (
        lambda n, _: (
            isinstance(n, exp.Ordered)
            and bool(n.args.get("desc"))
            and _in_window_order(n)
            and "nulls" not in n.meta
        ),
        "a window's `ORDER BY ... DESC` puts NULL last in ClickHouse and first in Ossie "
        "SQL; write `NULLS LAST` or `NULLS FIRST`",
    ),
    (
        # '\d' is read alike: ClickHouse keeps a backslash it does not know as an escape.
        lambda n, _: (
            isinstance(n, exp.Literal)
            and n.is_string
            and "\\" in n.this
            and _clickhouse_string(n.this) != n.this
        ),
        "a backslash in a string escapes the next character in ClickHouse and is a "
        "character of its own in Ossie SQL; write the expression as `sql` with each "
        "backslash once ('\\\\d' as '\\d')",
    ),
    (
        # SQLGlot's default dialect does not know these types and prints their names in
        # upper case inside Nullable: LowCardinality(String) as
        # Nullable(LowCardinality(STRING)), which ClickHouse refuses.
        lambda n, _: isinstance(n, exp.Cast) and n.to.this == exp.DType.USERDEFINED,
        "a cast to a type of ClickHouse's own, such as LowCardinality or Tuple, does "
        "not reach ClickHouse as written; call a conversion function, such as "
        "`toLowCardinality(x)`",
    ),
    (
        lambda n, _: isinstance(n, exp.Cast) and n.this.is_string and n.to.this in _TIMESTAMPS,
        "a string cast to a time is read in the model's time zone in Ossie SQL and in "
        "the server's in ClickHouse; call `toDateTime(string, zone)`, or write the "
        "expression as `sql`",
    ),
)


# A string literal as ClickHouse writes one, with '' or a backslash escaping a quote.
_STRING = re.compile(r"'(?:[^'\\]|\\.|'')*'")


def _clickhouse_string(text: str) -> str:
    """The string ClickHouse reads from ``text`` written between single quotes."""
    quoted = "'" + text.replace("'", "''") + "'"
    return sqlglot.parse_one(quoted, read="clickhouse").this


def _same_as_written(node: exp.Expression) -> bool:
    """A node whose rewrite is a difference a text in CLICKHOUSE accepts: the model's
    time zone for ClickHouse's clocks and dateDiff, as in `sql`
    (docs/decisions/time-zone.md), and a division made Float64
    (docs/decisions/division-to-float64.md), which changes only a Decimal quotient."""
    if isinstance(node, exp.Anonymous):
        return node.name.upper() in _CLOCKS
    return isinstance(node, exp.CurrentDate | exp.CurrentTimestamp | exp.DateDiff | exp.Div)


def _canonical(tree: exp.Expression) -> exp.Expression:
    """``tree``, read in ClickHouse's dialect, without what changes no meaning there:
    parentheses (the tree holds the grouping), if() for CASE and Nullable around a
    type that holds no other type.
    SQLGlot reads CURRENT_TIMESTAMP without parentheses as a column, ClickHouse as its
    clock (a canary in tests/test_canaries.py)."""
    tree = tree.copy()
    for node in reversed(list(tree.dfs())):
        new = node
        if isinstance(node, exp.Paren):
            new = node.this
        elif isinstance(node, exp.If) and not isinstance(node.parent, exp.Case):
            new = exp.Case(
                ifs=[exp.If(this=node.this, true=node.args["true"])],
                default=node.args.get("false"),
            )
        elif isinstance(node, exp.DataType) and not any(
            isinstance(e, exp.DataType) for e in node.expressions
        ):
            node.set("nullable", None)
        elif (
            isinstance(node, exp.Column)
            and not node.table
            and not node.this.quoted
            and node.name.upper() == "CURRENT_TIMESTAMP"
        ):
            new = exp.CurrentTimestamp()
        if new is node:
            continue
        if node.parent is None:
            tree = new
        else:
            node.replace(new)
    return tree


@cache
def clickhouse_problem(text: str) -> str | None:
    """Why ``text``, an expression in the CLICKHOUSE dialect, is refused, or None.

    Accepted when the SQL sent to ClickHouse reads, in ClickHouse's dialect, as the
    text does, but for _same_as_written's rewrites and _canonical's spellings.
    A text parse() refuses is refused with parse()'s reason."""
    try:
        tree = parse(text)
        sent = rewrite_tree(tree, keep=_same_as_written).sql(dialect="clickhouse")
    except ValueError as e:
        if "`" in _STRING.sub("", text):
            return (
                "written for ClickHouse: a name in backticks is not read; quote it in "
                'double quotes ("order id"), which ClickHouse reads alike'
            )
        # parse() reads every text as Ossie SQL and names what it refuses.
        return f"written for ClickHouse, read as Ossie SQL: {str(e).removesuffix(f': {text!r}')}"
    try:
        written = sqlglot.parse_one(text, read="clickhouse")
        ours = sqlglot.parse_one(sent, read="clickhouse")
    except sqlglot.errors.SqlglotError as e:
        return f"written for ClickHouse, but not ClickHouse SQL: {str(e).splitlines()[0]}"
    written = _canonical(written)
    if written == _canonical(ours):
        return None
    for node in tree.walk():
        for differs, why in _CLICKHOUSE_READINGS:
            if differs(node, written):
                return f"written for ClickHouse: {why}"
    return (
        f"written for ClickHouse: this executor would send {sent} instead; write that, "
        "or the expression as `sql`"
    )


def untranslatable(model: OssieDocument) -> tuple[dict[str, str], dict[str, str]]:
    """Fields ("dataset.field") and metrics (a dataset's as "dataset.metric") whose
    expression does not translate, with why.

    The executor hides them: an expression that does not parse cannot be shown
    to read only columns the user may see. A call of AGG is refused outside a model's
    metric; the planner checks what one names. A call of METRIC is refused everywhere,
    by parse(), before any server check. A field that aggregates outside a window
    is refused too; the executor adds those whose aggregate only the server knows."""

    def problem(expression: OssieExpression, where: str | None) -> str | None:
        try:
            text = pick_expression(expression)
            tree = parse(text)
            if where and (refusal := agg_refusal(tree, where)):
                return f"{refusal}: {text!r}"
            if where == "a field" and unwindowed_aggregate(tree):
                return f"{FIELD_AGGREGATE_RULE}: {text!r}"
            to_clickhouse(tree)
        except ValueError as e:
            return str(e)
        return None

    fields = {
        f"{d.name}.{f.name}": p
        for d in model.datasets
        for f in d.fields or []
        if (p := problem(f.expression, "a field"))
    }
    metrics = {
        n: p
        for n, m, ds in all_metrics(model)
        if (p := problem(m.expression, "a metric of a dataset" if ds else None))
    }
    return fields, metrics

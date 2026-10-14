"""Translate Ossie expressions to ClickHouse SQL.

Parsing uses SQLGlot's default (ANSI-like) dialect plus the few spec shapes
it gets wrong. Generation uses SQLGlot's ClickHouse dialect after rewriting
the spec functions ClickHouse lacks. Every rewrite works on the AST, never on
SQL text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlglot
import sqlglot.errors
from ossie import OssieDialect, OssieDocument, OssieExpression
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.parser import Parser

# Preference order when a field or metric carries several dialects.
DIALECT_PREFERENCE = (OssieDialect.OSSIE_SQL_2026, OssieDialect.ANSI_SQL)


def pick_expression(expression: OssieExpression) -> str:
    """The expression text in the most preferred dialect we can translate."""
    by_dialect = {d.dialect: d.expression for d in expression.dialects}
    for dialect in DIALECT_PREFERENCE:
        if dialect in by_dialect:
            return by_dialect[dialect]
    have = ", ".join(sorted(d.dialect.value for d in expression.dialects))
    raise ValueError(f"no expression in a supported dialect (has {have})")


# --- parsing ----------------------------------------------------------------


# Spec signatures of the functions rewritten below or by SQLGlot's ClickHouse
# generator, checked on the arguments as written. The default dialect also
# parses other engines' longer forms, such as Snowflake's REGEXP_COUNT(str,
# pattern, position, flags). They are not the spec's, and a rewrite may drop
# the extra arguments and return a different value, so parse() rejects them.
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
    "CURRENT_TIME": ("CURRENT_TIME()", 0, 0),
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

# Spec: supported date parts for EXTRACT and DATE_PART.
_DATE_PARTS = {
    "YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "DAYOFWEEK", "DAYOFYEAR",
    "HOUR", "MINUTE", "SECOND", "MILLISECOND",
}  # fmt: skip


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
            raise sqlglot.errors.ParseError(f"expected {signature}, got {len(args)} argument(s)")
        return build(args, **kwargs)  # SQLGlot retries with dialect= on TypeError

    return checked


_BUILDERS = {
    "DATEADD": _date_delta(exp.DateAdd),
    "DATEDIFF": _date_delta(exp.DateDiff),
    "DATE_PART": _date_part,
}


class _OssieParser(Parser):
    FUNCTIONS = {
        **Parser.FUNCTIONS,
        **{name: _checked(name, _BUILDERS.get(name)) for name in _SIGNATURES},
    }

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

    Mirrors apache/ossie PR #222; switch to ``read="ossie"`` once it merges."""

    Parser = _OssieParser
    # Without NULLS FIRST | LAST the spec leaves NULL placement open (apache/ossie#498);
    # NULLs sort last both ways, as in ClickHouse and DuckDB (docs/design.md).
    NULL_ORDERING = "nulls_are_last"


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


def _shape_problem(node: exp.Expression) -> str | None:
    """Spec shapes an argument count cannot express."""
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
    if (
        isinstance(node, exp.Ordered)
        and "nulls" in node.meta
        and not (isinstance(node.parent, exp.Order) and isinstance(node.parent.parent, exp.Window))
    ):
        return f"NULLS {node.meta['nulls']} is allowed only in a window's ORDER BY"
    # ClickHouse accepts IGNORE NULLS here and silently ignores it.
    if isinstance(node, exp.IgnoreNulls) and isinstance(
        node.this, exp.Lag | exp.Lead | exp.NthValue
    ):
        return f"IGNORE NULLS is not supported for {node.this.sql_name()}"
    return None


def parse(expression: str) -> exp.Expression:
    """Parse an Ossie expression, rejecting constructs the spec disallows."""
    try:
        tree = sqlglot.parse_one(expression, read=OssieSQL)
    except sqlglot.errors.SqlglotError as e:
        raise ValueError(f"cannot parse {expression!r}: {str(e).splitlines()[0]}") from e
    for node in tree.walk():
        if isinstance(node, _DISALLOWED):
            raise ValueError(
                f"{type(node).__name__} is not allowed in an Ossie expression: {expression!r}"
            )
        if problem := _shape_problem(node):
            raise ValueError(f"{problem}: {expression!r}")
    return tree


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
    "MILLISECOND": "toMillisecond",
}

# Functions SQLGlot keeps as exp.Anonymous (no dedicated node): name -> rewrite(args).
_ANONYMOUS = {
    "APPROX_PERCENTILE": lambda a: _param_agg("quantileTDigest", a[1], a[0]),
    "TO_DATE": lambda a: (
        _f("toDate", a[0])
        if len(a) == 1
        else _f("toDate", _f("parseDateTime", a[0], _fmt_literal(a[1])))
    ),
    "TO_TIMESTAMP": lambda a: (
        _f("parseDateTimeBestEffort", a[0])
        if len(a) == 1
        else _f("parseDateTime", a[0], _fmt_literal(a[1]))
    ),
    "IFF": lambda a: _f("if", *a),
    "ZEROIFNULL": lambda a: _f("ifNull", a[0], exp.Literal.number(0)),
    "NULLIFZERO": lambda a: _f("nullIf", a[0], exp.Literal.number(0)),
}


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


def rewrite(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Anonymous):
        fn = _ANONYMOUS.get(node.name.upper())
        return fn(node.expressions) if fn else node
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
    if isinstance(node, exp.CurrentTime):
        return _f("toTime", _f("now"))
    if isinstance(node, exp.Extract) and node.name.upper() in _EXTRACT_PARTS:
        return _f(_EXTRACT_PARTS[node.name.upper()], node.expression)
    if isinstance(node, exp.ToChar):
        return _f("formatDateTime", node.this, _fmt_literal(node.args.get("format")))
    if isinstance(node, exp.SplitPart):
        parts = _f("splitByString", node.args["delimiter"], node.this)
        return _f("arrayElement", parts, node.args["part_index"])
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


def rewrite_tree(tree: exp.Expression) -> exp.Expression:
    """A copy of ``tree`` with every node rewritten for ClickHouse.

    SQLGlot prints a tree as it stands, without parentheses for precedence, so an
    operator a rewrite builds or an inlined field or metric brings under another
    operator would be regrouped: ``net * 2``, ``net`` being ``amount - 5``, must not
    print as ``amount - 5 * 2``. Every operator under another operator ends up in
    parentheses, except the left operand of the same operator (``a - b - c``), which
    reads the same without them. CONTAINS counts as an operator here: SQLGlot prints
    it as ``POSITION(a, 'x') > 0`` and parenthesizes that only under a binary
    operator, so ``-CONTAINS(a, 'x')`` would print as ``-POSITION(a, 'x') > 0``."""
    tree = tree.copy()
    # Bottom-up, so a rewrite that replaces a node still sees its arguments rewritten;
    # Expression.transform does not descend into a replaced node.
    for node in reversed(list(tree.dfs())):
        parent, arg_key, index = node.parent, node.arg_key, node.index
        new = rewrite(node)
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
        if not ((_operator(node) or isinstance(node, exp.Contains)) and _operator(parent)):
            continue
        if arg_key == "this" and type(node) is type(parent) and isinstance(node, exp.Binary):
            continue
        parent.set(arg_key, exp.paren(node, copy=False), index)
    return tree


def to_clickhouse(expression: str | exp.Expression) -> str:
    """ClickHouse SQL for an Ossie expression."""
    tree = parse(expression) if isinstance(expression, str) else expression
    return rewrite_tree(tree).sql(dialect="clickhouse")


def translate(expression: OssieExpression) -> str:
    """ClickHouse SQL for a model field or metric expression."""
    return to_clickhouse(pick_expression(expression))


def untranslatable(model: OssieDocument) -> tuple[dict[str, str], dict[str, str]]:
    """Fields ("dataset.field") and metrics whose expression does not translate, with why.

    The executor hides them: an expression that does not parse cannot be shown
    to read only columns the user may see."""

    def problem(expression: OssieExpression) -> str | None:
        try:
            translate(expression)
        except ValueError as e:
            return str(e)
        return None

    fields = {
        f"{d.name}.{f.name}": p
        for d in model.datasets
        for f in d.fields or []
        if (p := problem(f.expression))
    }
    metrics = {m.name: p for m in model.metrics or [] if (p := problem(m.expression))}
    return fields, metrics

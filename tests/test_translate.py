import pytest
from ossie import OssieDialect, OssieDialectExpression, OssieExpression

from dactopus_ossie_clickhouse.translate import (
    convert_format,
    pick_expression,
    to_clickhouse,
    translate,
)


def dialects(**kw) -> OssieExpression:
    return OssieExpression(
        dialects=[
            OssieDialectExpression(dialect=OssieDialect[k], expression=v) for k, v in kw.items()
        ]
    )


def test_pick_prefers_ossie_sql_then_ansi():
    assert pick_expression(dialects(ANSI_SQL="a", OSSIE_SQL_2026="o")) == "o"
    assert pick_expression(dialects(DAX="x", ANSI_SQL="a")) == "a"
    with pytest.raises(ValueError, match="has DAX"):
        pick_expression(dialects(DAX="x"))


def test_translate_uses_picked_dialect():
    assert translate(dialects(ANSI_SQL="SUM(x)", DAX="SUM([x])")) == "SUM(x)"


@pytest.mark.parametrize(
    ("ossie", "expected"),
    [
        # parser: spec argument order
        ("DATEDIFF(day, d, d2)", "DATE_DIFF(DAY, d, d2)"),
        ("DATEADD(month, -1, d)", "DATE_ADD(MONTH, -1, d)"),
        ("DATE_PART('year', d)", "EXTRACT(YEAR FROM d)"),
        # aggregates
        ("VAR_POP(x)", "varPop(x)"),
        (
            "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)",
            "quantileExactInclusiveOrNull(0.5)(x * 1.0)",
        ),
        ("MEDIAN(x)", "quantileExactInclusiveOrNull(0.5)(x * 1.0)"),
        ("MEDIAN(x - y)", "quantileExactInclusiveOrNull(0.5)((x - y) * 1.0)"),
        (
            "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY x DESC)",
            "quantileExactInclusiveOrNull(1 - 0.9)(x * 1.0)",
        ),
        (
            "PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY x DESC)",
            "if(COUNT(x) = 0, NULL, arrayElement(arrayReverseSort(groupArray(x)), "
            "toUInt64(GREATEST(CEIL(0.9 * COUNT(x)), 1))))",
        ),
        (
            "PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x) FILTER (WHERE x > 1)",
            "if(COUNT(x) FILTER(WHERE x > 1) = 0, NULL, arrayElement(arraySort(groupArray(x) "
            "FILTER(WHERE x > 1)), toUInt64(GREATEST(CEIL(0.5 * COUNT(x) FILTER(WHERE x > 1)), "
            "1))))",
        ),
        # An aggregate SQLGlot does not know keeps its FILTER.
        ("uniqExact(x) FILTER (WHERE x > 1)", "uniqExact(x) FILTER(WHERE x > 1)"),
        (
            "PERCENTILE_CONT(0.2 + 0.3) WITHIN GROUP (ORDER BY x DESC)",
            "quantileExactInclusiveOrNull(1 - (0.2 + 0.3))(x * 1.0)",
        ),
        (
            "PERCENTILE_CONT(0.5 - 0.2) WITHIN GROUP (ORDER BY x DESC)",
            "quantileExactInclusiveOrNull(1 - (0.5 - 0.2))(x * 1.0)",
        ),
        (
            "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) OVER (PARTITION BY y)",
            "quantileExactInclusiveOrNull(0.5)(x * 1.0) OVER (PARTITION BY y)",
        ),
        ("APPROX_PERCENTILE(x, 0.5)", "quantileTDigest(0.5)(x)"),
        ("APPROX_COUNT_DISTINCT(x)", "uniq(x)"),
        # windows: NULL for a missing row, NULLs respected (ANSI)
        ("LAG(v, 1) OVER (ORDER BY k)", "lag(toNullable(v), 1) OVER (ORDER BY k)"),
        ("LEAD(v, 1, 0) OVER (ORDER BY k)", "lead(toNullable(v), 1, 0) OVER (ORDER BY k)"),
        ("NTH_VALUE(v, 2) OVER (ORDER BY k)", "NTH_VALUE(toNullable(v), 2) OVER (ORDER BY k)"),
        ("FIRST_VALUE(v) OVER (ORDER BY k)", "FIRST_VALUE(v) RESPECT NULLS OVER (ORDER BY k)"),
        ("LAST_VALUE(v) IGNORE NULLS OVER ()", "LAST_VALUE(v) IGNORE NULLS OVER ()"),
        # NULLS FIRST stays; NULLS LAST is ClickHouse's default both ways and is dropped
        ("SUM(v) OVER (ORDER BY k NULLS FIRST)", "SUM(v) OVER (ORDER BY k NULLS FIRST)"),
        ("SUM(v) OVER (ORDER BY k DESC NULLS FIRST)", "SUM(v) OVER (ORDER BY k DESC NULLS FIRST)"),
        ("SUM(v) OVER (ORDER BY k NULLS LAST)", "SUM(v) OVER (ORDER BY k)"),
        ("SUM(v) OVER (ORDER BY k DESC NULLS LAST)", "SUM(v) OVER (ORDER BY k DESC)"),
        (
            "LAG(v, 1) OVER (PARTITION BY p ORDER BY k ASC NULLS FIRST, j)",
            "lag(toNullable(v), 1) OVER (PARTITION BY p ORDER BY k ASC NULLS FIRST, j)",
        ),
        # nested rewrites survive a rewrite of the enclosing node
        (
            "FIRST_VALUE(ZEROIFNULL(v)) OVER (ORDER BY k)",
            "FIRST_VALUE(ifNull(v, 0)) RESPECT NULLS OVER (ORDER BY k)",
        ),
        ("DAYOFYEAR(TO_DATE(s))", "toDayOfYear(toDate(s))"),
        # date/time
        ("CURRENT_TIME", "toTime(now())"),
        ("DAYOFYEAR(d)", "toDayOfYear(d)"),
        ("DAYOFMONTH(d)", "toDayOfMonth(d)"),
        ("EXTRACT(DAYOFWEEK FROM d)", "toDayOfWeek(d)"),
        ("EXTRACT(DAYOFYEAR FROM d)", "toDayOfYear(d)"),
        ("TO_DATE('2024-01-15')", "toDate('2024-01-15')"),
        ("TO_DATE('15/01/2024', 'DD/MM/YYYY')", "toDate(parseDateTime('15/01/2024', '%d/%m/%Y'))"),
        ("TO_TIMESTAMP('2024-01-15 10:30:00')", "parseDateTimeBestEffort('2024-01-15 10:30:00')"),
        ("TO_CHAR(d, 'YYYY-MM-DD HH24:MI:SS')", "formatDateTime(d, '%Y-%m-%d %H:%i:%S')"),
        # strings
        ("SPLIT_PART(s, '-', 2)", "arrayElement(splitByString('-', s), 2)"),
        ("CONTAINS(s, 'a')", "POSITION(s, 'a') > 0"),
        # CONTAINS prints as an operator and stays one operand
        ("CONTAINS(s, 'a') + 1", "(POSITION(s, 'a') > 0) + 1"),
        ("NOT CONTAINS(s, 'a')", "NOT (POSITION(s, 'a') > 0)"),
        ("-CONTAINS(s, 'a')", "-(POSITION(s, 'a') > 0)"),
        # every operator under another is one operand, except the left one of its kind
        ("a OR b AND c", "a OR (b AND c)"),
        ("a - b - c + d", "(a - b - c) + d"),
        ("x LIKE 'a!%' ESCAPE '!' AND y", "(x LIKE 'a!%' ESCAPE '!') AND y"),
        ("REGEXP_COUNT(s, 'a')", "countMatches(s, 'a')"),
        # conditionals
        ("IFF(x > 1, 1, 0)", "if(x > 1, 1, 0)"),
        ("ZEROIFNULL(x)", "ifNull(x, 0)"),
        ("NULLIFZERO(x)", "nullIf(x, 0)"),
        # untouched: SQLGlot's ClickHouse dialect already handles these
        ("STDDEV(x)", "stddevSamp(x)"),
        ("LENGTH(s)", "CHAR_LENGTH(s)"),
        ("store_sales.ss_ext_sales_price", "store_sales.ss_ext_sales_price"),
        # ClickHouse reads `\` in a quoted name as an escape: doubled, or "a\x41" is aA
        ('"a\\x41"', '"a\\\\x41"'),
    ],
)
def test_to_clickhouse(ossie, expected):
    assert to_clickhouse(ossie) == expected


def test_convert_format_tokens():
    assert convert_format("DD MON YYYY, DAY HH12:MI PM 100%") == "%d %b %Y, %W %I:%i %p 100%%"
    assert convert_format("MONTH MM MI") == "%M %m %i"


def test_format_must_be_literal():
    with pytest.raises(ValueError, match="string literal"):
        to_clickhouse("TO_CHAR(d, fmt_col)")


@pytest.mark.parametrize(
    ("ossie", "message"),
    [
        # longer forms from other engines are not the spec's
        ("REGEXP_COUNT(s, 'a', 3, 'i')", r"expected REGEXP_COUNT\(str, pattern\), got 4"),
        ("CONTAINS(s, 'a', 'x')", r"expected CONTAINS\(str, substr\), got 3"),
        ("DAYOFYEAR(d, 1)", r"expected DAYOFYEAR\(date_expr\), got 2"),
        ("SPLIT_PART(s, ',', 1, 2)", r"expected SPLIT_PART\(str, delimiter, part\), got 4"),
        ("TO_CHAR(d, 'YYYY', 'x')", r"expected TO_CHAR\(date_expr, format\), got 3"),
        ("TO_DATE(s, 'YYYY', 'x')", r"expected TO_DATE\(string\[, format\]\), got 3"),
        ("TO_TIMESTAMP(s, 'YYYY', 'x')", r"expected TO_TIMESTAMP\(string\[, format\]\), got 3"),
        ("APPROX_PERCENTILE(x, 0.5, 100)", r"expected APPROX_PERCENTILE\(expr, p\), got 3"),
        ("ZEROIFNULL(x, 5)", r"expected ZEROIFNULL\(expr\), got 2"),
        ("NULLIFZERO(x, 5)", r"expected NULLIFZERO\(expr\), got 2"),
        ("IFF(a, 1, 2, 3)", r"expected IFF\(condition, true_result, false_result\), got 4"),
        ("CURRENT_TIME(3)", r"expected CURRENT_TIME\(\), got 1"),
        (
            "LAG(v, 1, 0, 2) OVER (ORDER BY k)",
            r"expected LAG\(expr\[, offset\[, default\]\]\), got 4",
        ),
        ("NTH_VALUE(v) OVER (ORDER BY k)", r"expected NTH_VALUE\(expr, n\), got 1"),
        # ClickHouse ignores IGNORE NULLS on these and answers as if respected
        ("LAG(v) IGNORE NULLS OVER (ORDER BY k)", r"IGNORE NULLS is not supported for LAG"),
        ("NTH_VALUE(v, 2 IGNORE NULLS) OVER ()", r"IGNORE NULLS is not supported for NTH_VALUE"),
        ("DATEDIFF(day, a, b, c)", r"expected DATEDIFF\(part, start_date, end_date\), got 4"),
        ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x, y)", r"got 2 ORDER BY keys"),
        ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x, y)", r"got 2 ORDER BY keys"),
        ("PERCENTILE_CONT(0.5, 1) WITHIN GROUP (ORDER BY x)", r"got 2 argument"),
        ("MEDIAN(x, 1)", r"expected MEDIAN\(expr\), got 2 argument"),
        ("MEDIAN(DISTINCT x, y)", r"expected MEDIAN\(expr\), got 2 argument"),
        ("PERCENTILE_CONT(0.5)", r"expected PERCENTILE_CONT\(p\) WITHIN GROUP"),
        ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x) OVER ()", r"cannot take OVER"),
        (
            "PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x) FILTER (WHERE x > 1) "
            "OVER (PARTITION BY y)",
            r"PERCENTILE_DISC cannot take OVER",
        ),
        (
            "PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x) IGNORE NULLS OVER ()",
            r"PERCENTILE_DISC cannot take OVER",
        ),
        ("COALESCE(SUM(x), 0) FILTER (WHERE x > 1)", r"FILTER applies to an aggregate"),
        # too few: a clear error, not an IndexError
        ("APPROX_PERCENTILE(x)", r"got 1"),
        ("TO_DATE()", r"got 0"),
        ("DATEADD(day, 7)", r"got 2"),
        ("DATE_PART()", r"expected DATE_PART\(part, date_expr\), got 0"),
        ("REGEXP_COUNT(s)", r"expected REGEXP_COUNT\(str, pattern\), got 1"),
        ("CONTAINS(s)", r"expected CONTAINS\(str, substr\), got 1"),
        # a column where the spec wants a date part
        ("DATE_PART(p, d)", r"P is not a date part"),
        ("DATE_PART(t.year, d)", r"t.year is not a date part"),
        ("EXTRACT(EPOCH FROM d)", r"EPOCH is not a date part"),
        # window frames outside the spec: an empty frame or a RANGE offset goes wrong
        ("SUM(v) OVER (ORDER BY k ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING)", r"window frame"),
        ("SUM(v) OVER (ORDER BY k ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)", r"frame"),
        ("SUM(v) OVER (ORDER BY k RANGE BETWEEN 1 PRECEDING AND CURRENT ROW)", r"frame"),
        ("SUM(v) OVER (ORDER BY k RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)", r"frame"),
        ("SUM(v) OVER (ORDER BY k GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW)", r"frame"),
        # NULLS FIRST | LAST belongs to a window's ORDER BY only
        ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x NULLS LAST)", r"NULLS LAST is allowed"),
        (
            "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x NULLS FIRST) OVER (ORDER BY k)",
            r"NULLS FIRST is allowed only in a window's ORDER BY",
        ),
        # does not tokenize
        ("'abc", r"cannot parse"),
    ],
)
def test_rejects_arguments_outside_spec_signature(ossie, message):
    with pytest.raises(ValueError, match=message):
        to_clickhouse(ossie)

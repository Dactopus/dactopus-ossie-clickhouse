"""Every function in the spec catalog (core-spec/expression_language.md),
translated, executed in ClickHouse, and compared by value.

Expected values come from DuckDB unless the entry pins one by hand. Pinned
entries say why: DuckDB lacks the function, or disagrees with the spec, or
the spec is silent and we chose. The spec text wins over any engine.
"""

import datetime as dt
import decimal
import math

import pytest

from dactopus_ossie_clickhouse.translate import to_clickhouse

DUCK = object()  # expected value: whatever DuckDB says
D = dt.date
DT = dt.datetime

# Base row: x=2.5, n=NULL, s='abc-def', d=2024-03-15 (Friday), d2=2024-05-20, ts=2024-03-15 10:30:45
CH_BASE = (
    "(SELECT 2.5 AS x, CAST(NULL AS Nullable(Float64)) AS n, 'abc-def' AS s, "
    "toDate('2024-03-15') AS d, toDate('2024-05-20') AS d2, "
    "toDateTime('2024-03-15 10:30:45') AS ts)"
)
DUCK_BASE = (
    "(SELECT 2.5 AS x, CAST(NULL AS DOUBLE) AS n, 'abc-def' AS s, "
    "DATE '2024-03-15' AS d, DATE '2024-05-20' AS d2, TIMESTAMP '2024-03-15 10:30:45' AS ts)"
)
# Aggregate base: x in (1, 2.5, 4, 10, NULL). Five rows, so a quantile has something to
# choose between and NULL handling shows.
CH_AGG = "(SELECT arrayJoin([1, 2.5, 4, 10, NULL]) AS x)"
DUCK_AGG = "(SELECT UNNEST(CAST([1, 2.5, 4, 10, NULL] AS DOUBLE[])) AS x)"

AGGREGATES = [
    ("SUM(x)", DUCK), ("COUNT(x)", DUCK), ("COUNT(*)", DUCK), ("COUNT(DISTINCT x)", DUCK),
    ("AVG(x)", DUCK), ("MIN(x)", DUCK), ("MAX(x)", DUCK),
    ("STDDEV(x)", DUCK), ("STDDEV_SAMP(x)", DUCK), ("VARIANCE(x)", DUCK), ("VAR_SAMP(x)", DUCK),
    ("STDDEV_POP(x)", DUCK), ("VAR_POP(x)", DUCK),
    ("MEDIAN(x)", DUCK),
    ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY x DESC)", DUCK),
    ("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_CONT(0) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_CONT(1) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) FILTER (WHERE x > 1)", DUCK),
    ("MEDIAN(x) FILTER (WHERE x > 1)", DUCK), ("MEDIAN(DISTINCT x)", DUCK),
    # Discrete: the first value whose cumulative share reaches p (Postgres semantics).
    ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_DISC(0) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_DISC(1) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY x DESC)", DUCK),
    ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x DESC)", DUCK),
    ("PERCENTILE_CONT(0.2 + 0.3) WITHIN GROUP (ORDER BY x DESC)", DUCK),
    ("PERCENTILE_DISC(0.5 + 0.25) WITHIN GROUP (ORDER BY x)", DUCK),
    ("PERCENTILE_CONT(0.5 - 0.2) WITHIN GROUP (ORDER BY x DESC)", DUCK),
    ("PERCENTILE_DISC(0.9 - 0.4) WITHIN GROUP (ORDER BY x DESC)", DUCK),
    # FILTER over the rewritten PERCENTILE_DISC, which is several aggregates.
    ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x) FILTER (WHERE x > 1)", DUCK),
    ("PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY x DESC) FILTER (WHERE x > 1)", DUCK),
    ("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x) FILTER (WHERE x > 100)", DUCK),
    ("APPROX_COUNT_DISTINCT(x)", DUCK),
    ("APPROX_PERCENTILE(x, 0.5)", 2.5),  # DuckDB lacks it; quantileTDigest is approximate by design
    ("SUM(DISTINCT x)", DUCK),
    ("SUM(CASE WHEN x > 1 THEN x ELSE 0 END)", DUCK), ("COUNT(CASE WHEN x > 1 THEN 1 END)", DUCK),
]  # fmt: skip

CATALOG = [
    # --- date/time extraction
    ("YEAR(d)", DUCK), ("QUARTER(d)", DUCK), ("MONTH(d)", DUCK), ("DAY(d)", DUCK), ("DAYOFYEAR(d)", DUCK),
    ("DAYOFMONTH(d)", DUCK),
    ("HOUR(ts)", DUCK), ("MINUTE(ts)", DUCK), ("SECOND(ts)", DUCK),
    ("EXTRACT(YEAR FROM d)", DUCK), ("EXTRACT(MONTH FROM d)", DUCK), ("EXTRACT(DAY FROM d)", DUCK),
    ("EXTRACT(WEEK FROM d)", DUCK), ("EXTRACT(DAYOFYEAR FROM d)", DUCK),
    # Spec does not number weekdays. ISO (Monday=1) as ClickHouse does; DuckDB uses Sunday=0.
    ("EXTRACT(DAYOFWEEK FROM d)", 5),
    # Spec is silent; the millisecond component (0 here), not DuckDB's seconds*1000+ms.
    ("EXTRACT(MILLISECOND FROM ts)", 0),
    ("DATE_PART('year', d)", DUCK), ("DATE_PART('month', d)", DUCK), ("DATE_PART('day', d)", DUCK),
    # --- truncation and arithmetic
    ("DATE_TRUNC('year', d)", DUCK), ("DATE_TRUNC('quarter', d)", DUCK), ("DATE_TRUNC('month', d)", DUCK),
    ("DATE_TRUNC('week', d)", DUCK), ("DATE_TRUNC('day', d)", DUCK), ("DATE_TRUNC('hour', ts)", DUCK),
    ("DATE_TRUNC('minute', ts)", DUCK), ("DATE_TRUNC('second', ts)", DUCK),
    ("DATEADD(day, 7, d)", D(2024, 3, 22)), ("DATEADD(month, -1, d)", D(2024, 2, 15)),  # DuckDB lacks DATEADD
    ("DATEADD(year, 1, d)", D(2025, 3, 15)),
    ("DATEDIFF(day, d, d2)", 66), ("DATEDIFF(month, d, d2)", 2), ("DATEDIFF(year, d, d2)", 0),
    # --- construction
    ("DATE '2024-01-15'", DUCK),
    ("TIMESTAMP_NTZ '2024-01-15 10:30:00'", DT(2024, 1, 15, 10, 30)),  # DuckDB lacks the type name
    ("CAST('2024-01-15' AS DATE)", DUCK),
    ("CAST('2024-01-15 10:30:00' AS TIMESTAMP_NTZ)", DT(2024, 1, 15, 10, 30)),
    ("TO_DATE('2024-01-15')", D(2024, 1, 15)), ("TO_TIMESTAMP('2024-01-15 10:30:00')", DT(2024, 1, 15, 10, 30)),
    ("TO_DATE('15/01/2024', 'DD/MM/YYYY')", D(2024, 1, 15)),
    ("TO_CHAR(d, 'YYYY-MM-DD')", "2024-03-15"), ("TO_CHAR(ts, 'DD MON YYYY HH12:MI PM')", "15 Mar 2024 10:30 AM"),
    # --- strings
    ("CONCAT(s, s)", DUCK), ("s || s", DUCK), ("LENGTH(s)", DUCK), ("LOWER(s)", DUCK), ("UPPER(s)", DUCK),
    ("TRIM(s)", DUCK), ("LTRIM(s)", DUCK), ("RTRIM(s)", DUCK), ("LEFT(s, 2)", DUCK), ("RIGHT(s, 2)", DUCK),
    ("SUBSTRING(s, 1, 2)", DUCK), ("REPLACE(s, 'a', 'b')", DUCK), ("SPLIT_PART(s, '-', 1)", DUCK),
    ("SPLIT_PART(s, '-', 5)", ""),  # out of range: empty string, as in Snowflake/Postgres
    ("POSITION('c' IN s)", DUCK), ("CHARINDEX('c', s)", 3), ("CONTAINS(s, 'c')", DUCK),
    ("CONTAINS(s, 'a') = CONTAINS(s, 'c')", DUCK), ("NOT CONTAINS(s, 'z')", DUCK),
    ("STARTSWITH(s, 'ab')", 1), ("ENDSWITH(s, 'ab')", 0),
    ("s LIKE 'a%'", DUCK), ("s ILIKE 'A%'", DUCK), ("REGEXP_LIKE(s, 'a.*')", 1),
    ("(s LIKE 'a!%' ESCAPE '!') = 0", DUCK),
    ("REGEXP_EXTRACT(s, 'b.')", DUCK), ("REGEXP_COUNT(s, '[a-c]')", 3),
    # Spec: "replace matches", plural, so all occurrences (Snowflake, BigQuery); DuckDB replaces the first only.
    ("REGEXP_REPLACE(s, '[ab]', 'x')", "xxc-def"),
    # --- math
    ("ABS(x)", DUCK), ("ROUND(x, 1)", DUCK), ("FLOOR(x)", DUCK), ("CEIL(x)", DUCK), ("CEILING(x)", DUCK),
    ("TRUNC(x, 1)", DUCK), ("TRUNCATE(x, 1)", 2.5), ("MOD(x, 2)", DUCK), ("SIGN(x)", DUCK),
    ("POWER(x, 2)", DUCK), ("SQRT(x)", DUCK), ("EXP(x)", DUCK), ("LN(x)", DUCK), ("LOG(10, x)", DUCK),
    ("LOG(2, x)", DUCK), ("LOG10(x)", DUCK),
    ("SIN(x)", DUCK), ("COS(x)", DUCK), ("TAN(x)", DUCK), ("ASIN(0.5)", DUCK), ("ACOS(0.5)", DUCK),
    ("ATAN(x)", DUCK), ("ATAN2(x, x)", DUCK), ("RADIANS(x)", DUCK), ("DEGREES(x)", DUCK), ("PI()", DUCK),
    ("GREATEST(x, 1, 2)", DUCK), ("LEAST(x, 1, 2)", DUCK),
    # --- conditionals and operators
    ("CASE WHEN x > 1 THEN 'a' ELSE 'b' END", DUCK), ("CASE x WHEN 1 THEN 'a' ELSE 'b' END", DUCK),
    ("IF(x > 1, 1, 0)", DUCK), ("IFF(x > 1, 1, 0)", 1), ("NULLIF(x, 1)", DUCK), ("COALESCE(x, 1)", DUCK),
    ("IFNULL(x, 1)", DUCK), ("NVL(x, 1)", 2.5), ("NVL2(x, 1, 0)", 1), ("ZEROIFNULL(x)", 2.5), ("NULLIFZERO(x)", 2.5),
    # the NULL branches (DuckDB lacks NVL, NVL2, ZEROIFNULL, NULLIFZERO)
    ("COALESCE(n, 1)", DUCK), ("IFNULL(n, 1)", DUCK), ("NVL(n, 1)", 1), ("NVL2(n, 1, 0)", 0),
    ("ZEROIFNULL(n)", 0), ("NULLIFZERO(x - 2.5)", None), ("NULLIF(x, 2.5)", DUCK),
    ("n IS NULL", DUCK), ("n IS NOT NULL", DUCK), ("n > 1", DUCK), ("n + 1", DUCK),
    ("CASE WHEN n > 1 THEN 'a' ELSE 'b' END", DUCK), ("IF(n IS NULL, 'none', 'some')", DUCK),
    ("x BETWEEN 1 AND 3", DUCK), ("x IN (1, 2, 3)", DUCK), ("x NOT IN (1, 2)", DUCK), ("x IS NULL", DUCK),
    ("x IS NOT NULL", DUCK), ("NOT x > 1", DUCK), ("x > 1 AND x < 3 OR x = 5", DUCK), ("x % 2", DUCK),
    ("x <> 1", DUCK), ("x != 1", DUCK), ("TRUE", DUCK), ("FALSE", DUCK),
]  # fmt: skip


def norm(v):
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return "nan" if math.isnan(v) else round(v, 6)
    if isinstance(v, DT):
        v = v.replace(tzinfo=None)
        return v.date() if v.time() == dt.time() else v  # midnight timestamp == date
    return v


def check(expr, expected, clickhouse, duck, ch_base, duck_base):
    got = norm(clickhouse.query(f"SELECT {to_clickhouse(expr)} FROM {ch_base}").result_rows[0][0])
    if expected is DUCK:
        expected = duck.execute(f"SELECT {expr} FROM {duck_base}").fetchone()[0]
    assert got == norm(expected)


@pytest.mark.parametrize(("expr", "expected"), CATALOG, ids=[c[0] for c in CATALOG])
def test_catalog(expr, expected, clickhouse, duck):
    check(expr, expected, clickhouse, duck, CH_BASE, DUCK_BASE)


@pytest.mark.parametrize(("expr", "expected"), AGGREGATES, ids=[c[0] for c in AGGREGATES])
def test_aggregates(expr, expected, clickhouse, duck):
    check(expr, expected, clickhouse, duck, CH_AGG, DUCK_AGG)


# Exact percentiles on other inputs: one row, Decimal (which quantileExactInclusive
# rejects), and more rows than the 8192 that ClickHouse's quantile and median sample.
PERCENTILE_BASES = {
    "one row": ("(SELECT 2.5 AS x)", "(SELECT 2.5 AS x)"),
    "decimal": (
        "(SELECT toDecimal64(arrayJoin([1, 2.5, 4, 10]), 2) AS x)",
        "(SELECT UNNEST(CAST([1, 2.5, 4, 10] AS DECIMAL(18, 2)[])) AS x)",
    ),
    "100k rows": (
        "(SELECT (number * 7919) % 100003 AS x FROM numbers(100000))",
        "(SELECT (range * 7919) % 100003 AS x FROM range(100000))",
    ),
}
PERCENTILES = [
    "MEDIAN(x)",
    "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)",
    "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY x DESC)",
]


# Division of Decimal values, which ClickHouse would cut to the dividend's scale.
DECIMAL_BASE = (
    "(SELECT toDecimal64(arrayJoin([10, 350]), 2) AS x, toInt32(3) AS i)",
    "(SELECT UNNEST(CAST([10, 350] AS DECIMAL(18, 2)[])) AS x, 3 AS i)",
)
DIVISIONS = ["MIN(x) / MAX(x)", "SUM(x) / COUNT(x)", "SUM(x) / 3", "MAX(i) / 2", "SUM(x) / MAX(i)"]


@pytest.mark.parametrize("expr", DIVISIONS)
def test_division_keeps_digits(expr, clickhouse, duck):
    check(expr, DUCK, clickhouse, duck, *DECIMAL_BASE)


@pytest.mark.parametrize("dividend", ["toDate('2026-01-01')", "'3'"])
def test_division_refuses_non_numbers(dividend, clickhouse):
    # Postgres refuses a date or a string dividend; toFloat64 would take a day count
    # or parse the string.
    with pytest.raises(Exception, match="Illegal types"):
        clickhouse.query(f"SELECT {to_clickhouse(f'{dividend} / 2')}")


@pytest.mark.parametrize("base", PERCENTILE_BASES)
@pytest.mark.parametrize("expr", PERCENTILES)
def test_percentile_cont_is_exact(expr, base, clickhouse, duck):
    check(expr, DUCK, clickhouse, duck, *PERCENTILE_BASES[base])


@pytest.mark.parametrize("expr", PERCENTILES)
def test_percentile_cont_of_nothing_is_null(expr, clickhouse):
    # A non-Nullable argument: quantileExactInclusive alone would answer nan.
    sql = to_clickhouse(expr)
    base = "(SELECT arrayJoin([1, 2.5]) AS x)"
    assert clickhouse.query(f"SELECT {sql} FROM {base} WHERE x > 100").result_rows == [(None,)]


@pytest.mark.parametrize(
    "base",
    [
        "(SELECT toDate('2026-01-01') + number AS x FROM numbers(4))",
        "(SELECT toDateTime('2026-01-01 00:00:00') + number AS x FROM numbers(4))",
        "(SELECT toString(number) AS x FROM numbers(4))",
    ],
)
@pytest.mark.parametrize("expr", PERCENTILES)
def test_percentile_cont_refuses_non_numbers(expr, base, clickhouse):
    # Postgres defines PERCENTILE_CONT for numbers only; ClickHouse would answer a
    # date with its day count. PERCENTILE_DISC takes any sortable type.
    with pytest.raises(Exception, match="Illegal types"):
        clickhouse.query(f"SELECT {to_clickhouse(expr)} FROM {base}")


def test_percentile_disc_of_dates_is_a_date(clickhouse):
    sql = to_clickhouse("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x)")
    base = "(SELECT toDate('2026-01-01') + number AS x FROM numbers(4))"
    assert clickhouse.query(f"SELECT {sql} FROM {base}").result_rows == [(D(2026, 1, 2),)]


def test_percentile_disc_of_nothing_is_null(clickhouse):
    sql = to_clickhouse("PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x)")
    assert clickhouse.query(f"SELECT {sql} FROM {CH_AGG} WHERE x > 100").result_rows == [(None,)]


@pytest.mark.parametrize(
    "expr", ["CURRENT_DATE", "CURRENT_DATE()", "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"]
)
def test_current(expr, clickhouse):
    got = clickhouse.query(f"SELECT {to_clickhouse(expr)}").result_rows[0][0]
    assert isinstance(got, dt.date)


@pytest.mark.parametrize("expr", ["TIME '10:30:00'", "CAST('10:30:00' AS TIME)"])
def test_time_values(expr, clickhouse):
    # ClickHouse's Time is a signed duration; clickhouse-connect reads it as a timedelta.
    got = clickhouse.query(f"SELECT {to_clickhouse(expr)}").result_rows[0][0]
    assert got == dt.timedelta(hours=10, minutes=30)


def test_current_time(clickhouse):
    got = clickhouse.query(f"SELECT {to_clickhouse('CURRENT_TIME')}").result_rows[0][0]
    assert dt.timedelta(0) <= got < dt.timedelta(days=1)


# Window base: order key k with a NULL; id identifies the row in the comparison.
CH_WINDOW = (
    "(SELECT tupleElement(r, 1) AS id, tupleElement(r, 2) AS k, tupleElement(r, 3) AS v FROM "
    "(SELECT arrayJoin([(1, 1, 10.0), (2, NULL, 20.0), (3, 2, 30.0)]::"
    "Array(Tuple(UInt8, Nullable(Int32), Float64))) AS r))"
)
DUCK_WINDOW = "(SELECT * FROM (VALUES (1, 1, 10.0), (2, NULL, 20.0), (3, 2, 30.0)) t(id, k, v))"
# Without NULLS FIRST | LAST the spec leaves NULL placement open; NULLs sort last both
# ways, as DuckDB does (docs/design.md). With it, NULLs go where it says in either direction.
WINDOWS = [
    "SUM(v) OVER (ORDER BY k ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k DESC RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING)",
    "sum(v) over (order by k rows between 1 preceding and current row)",
    "AVG(v) OVER (ORDER BY k DESC ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)",
    "ROW_NUMBER() OVER (ORDER BY k)",
    "RANK() OVER (ORDER BY k DESC)",
    "LAG(v, 1) OVER (ORDER BY k)",
    "MEDIAN(v) OVER (ORDER BY k ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k NULLS FIRST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k DESC NULLS FIRST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k ASC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k DESC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k NULLS FIRST RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k DESC NULLS FIRST RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "LAG(v, 1) OVER (ORDER BY k NULLS FIRST)",
    "FIRST_VALUE(v) OVER (ORDER BY k DESC NULLS FIRST)",
    "RANK() OVER (ORDER BY k DESC NULLS LAST)",
]


def check_rows(expr, clickhouse, duck, ch_base, duck_base):
    got = clickhouse.query(f"SELECT id, {to_clickhouse(expr)} FROM {ch_base} ORDER BY id")
    expected = duck.execute(f"SELECT id, {expr} FROM {duck_base} ORDER BY id").fetchall()
    assert [tuple(map(norm, r)) for r in got.result_rows] == [tuple(map(norm, r)) for r in expected]


@pytest.mark.parametrize("expr", WINDOWS)
def test_window_null_order(expr, clickhouse, duck):
    check_rows(expr, clickhouse, duck, CH_WINDOW, DUCK_WINDOW)


# Offset base: v is not Nullable, w holds NULLs. ClickHouse answers a missing row with
# the type's default (0) and skips NULLs in first_value / last_value; the spec (ANSI)
# and DuckDB give NULL and respect NULLs.
CH_OFFSET = (
    "(SELECT tupleElement(r, 1) AS id, tupleElement(r, 2) AS v, tupleElement(r, 3) AS w FROM "
    "(SELECT arrayJoin([(1, 10.0, NULL), (2, 20.0, 5.0), (3, 30.0, NULL)]::"
    "Array(Tuple(UInt8, Float64, Nullable(Float64)))) AS r))"
)
DUCK_OFFSET = (
    "(SELECT * FROM (VALUES (1, 10.0, NULL), (2, 20.0, 5.0), (3, 30.0, NULL)) t(id, v, w))"
)
ALL_ROWS = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
OFFSETS = [
    "LAG(v) OVER (ORDER BY id)",
    "LAG(v, 2) OVER (ORDER BY id)",
    "LAG(v, 1, -1) OVER (ORDER BY id)",
    "LAG(w, 1) OVER (ORDER BY id)",
    "LEAD(v, 1) OVER (ORDER BY id)",
    "LEAD(v, 1, 0) OVER (ORDER BY id)",
    "NTH_VALUE(v, 2) OVER (ORDER BY id)",
    "NTH_VALUE(w, 2) OVER (ORDER BY id)",
    "FIRST_VALUE(w) OVER (ORDER BY id)",
    f"LAST_VALUE(w) OVER (ORDER BY id {ALL_ROWS})",
    "FIRST_VALUE(v) OVER (ORDER BY id)",
    # a NULL or Nullable default on a non-Nullable argument
    "LAG(v, 1, NULL) OVER (ORDER BY id)",
    "LEAD(v, 1, w) OVER (ORDER BY id)",
    # CONTAINS, which ClickHouse lacks, inside one
    "LAST_VALUE(CONTAINS(CAST(v AS VARCHAR), '2')) OVER (ORDER BY id)",
]


@pytest.mark.parametrize("expr", OFFSETS)
def test_window_offsets(expr, clickhouse, duck):
    check_rows(expr, clickhouse, duck, CH_OFFSET, DUCK_OFFSET)


@pytest.mark.parametrize("expr", [c[0] for c in AGGREGATES + CATALOG] + WINDOWS + OFFSETS)
def test_rewrite_is_idempotent(expr):
    # The planner rewrites a metric, then again inside a filter that names it.
    from dactopus_ossie_clickhouse.translate import parse, rewrite_tree

    once = rewrite_tree(parse(expr))
    assert rewrite_tree(once).sql("clickhouse") == once.sql("clickhouse")

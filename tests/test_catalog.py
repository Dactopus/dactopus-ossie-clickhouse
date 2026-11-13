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
    # Outside the spec, sent as written: ClickHouse's DAYOFMONTH and DAYOFWEEK (ISO).
    ("DAYOFMONTH(d)", DUCK), ("DAYOFWEEK(d)", 5),
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
    # Postgres's form (DuckDB lacks it): truncated in the zone, midnight there
    ("DATE_TRUNC('day', CAST('2024-03-15 02:00:00+00:00' AS TIMESTAMP), 'America/Los_Angeles')", D(2024, 3, 14)),
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
    ("TRIM(s)", DUCK), ("LTRIM(s)", DUCK), ("RTRIM(s)", DUCK),
    ("LTRIM(s, 'ba')", DUCK), ("RTRIM(s, 'fe')", DUCK), ("LEFT(s, 2)", DUCK), ("RIGHT(s, 2)", DUCK),
    ("SUBSTRING(s, 1, 2)", DUCK), ("REPLACE(s, 'a', 'b')", DUCK), ("SPLIT_PART(s, '-', 1)", DUCK),
    ("SPLIT_PART(s, '-', 5)", ""),  # out of range: empty string, as in Snowflake/Postgres
    # NULL in any argument: NULL, though splitByString never returns a Nullable array
    ("SPLIT_PART(NULLIF(s, 'abc-def'), '-', 2)", DUCK), ("SPLIT_PART(s, NULLIF('-', '-'), 2)", DUCK),
    ("SPLIT_PART(s, '-', CAST(n AS INTEGER))", DUCK),
    ("POSITION('c' IN s)", DUCK), ("CHARINDEX('c', s)", 3),
    # a start, as in Snowflake (DuckDB lacks CHARINDEX)
    ("CHARINDEX('c', CONCAT(s, s), 4)", 10), ("POSITION('c', CONCAT(s, s), 4)", 10),
    ("CONTAINS(s, 'c')", DUCK),
    ("CONTAINS(s, 'a') = CONTAINS(s, 'c')", DUCK), ("NOT CONTAINS(s, 'z')", DUCK),
    ("STARTSWITH(s, 'ab')", 1), ("ENDSWITH(s, 'ab')", 0),
    ("s LIKE 'a%'", DUCK), ("s ILIKE 'A%'", DUCK), ("REGEXP_LIKE(s, 'a.*')", 1),
    ("(s LIKE 'a!%' ESCAPE '!') = 0", DUCK),
    ("REGEXP_EXTRACT(s, 'b.')", DUCK), ("REGEXP_EXTRACT(s, 'a(b.)')", DUCK),
    ("REGEXP_EXTRACT(s, '(a)(b)', 2)", DUCK),
    ("REGEXP_EXTRACT(s, 'z')", DUCK), ("REGEXP_COUNT(s, '[a-c]')", 3),
    # Spec: "replace matches", plural, so all occurrences (Snowflake, BigQuery); DuckDB replaces the first only.
    ("REGEXP_REPLACE(s, '[ab]', 'x')", "xxc-def"),
    # --- math
    ("ABS(x)", DUCK), ("ROUND(x, 1)", DUCK), ("FLOOR(x)", DUCK), ("CEIL(x)", DUCK), ("CEILING(x)", DUCK),
    # to d digits, as in Snowflake (DuckDB lacks the form)
    ("FLOOR(x * 1.23, 1)", 3.0), ("CEIL(x * 1.23, 1)", 3.1), ("CEILING(x * 1.23, 1)", 3.1),
    ("TRUNC(x, 1)", DUCK), ("TRUNCATE(x, 1)", 2.5), ("MOD(x, 2)", DUCK), ("SIGN(x)", DUCK),
    ("POWER(x, 2)", DUCK), ("SQRT(x)", DUCK), ("EXP(x)", DUCK), ("LN(x)", DUCK), ("LOG(10, x)", DUCK),
    ("LOG(2, x)", DUCK), ("LOG(3, x)", DUCK), ("LOG(2.0, x)", DUCK), ("LOG(x, 8)", DUCK), ("LOG10(x)", DUCK),
    # exact for a base written 10.0: LN(1000) / LN(10.0) is 2.9999999999999996
    ("FLOOR(LOG(10.0, 1000))", DUCK),
    # no logarithm to a negative base: nan (None from the executor); Postgres and DuckDB fail
    ("LOG(-2, 8)", math.nan),
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


# Spec, expression_language.md "Numeric Semantics": a value exactly halfway rounds
# away from zero. Expected values written from the spec, not DuckDB. ClickHouse's round
# gives a Float tie to the even neighbour (2, 0.12); `whole` marks the values an integer
# column can hold.
ROUNDS = [
    ("ROUND(x, 0)", "2.5", "3"), ("ROUND(x, 0)", "-2.5", "-3"),
    ("ROUND(x, 2)", "0.125", "0.13"), ("ROUND(x, 2)", "-0.125", "-0.13"),
    ("ROUND(x)", "0.5", "1"), ("ROUND(x)", "-1.5", "-2"), ("ROUND(x)", "2.4", "2"),
    ("ROUND(x)", "-2.6", "-3"), ("ROUND(x, 1)", "0.25", "0.3"), ("ROUND(x, 1)", "-0.04", "0"),
    ("ROUND(x, -1)", "25", "30"), ("ROUND(x, -1)", "-25", "-30"), ("ROUND(x, -1)", "24.9", "20"),
    ("ROUND(x, -2)", "150", "200"), ("ROUND(x, -2)", "-149", "-100"), ("ROUND(x, 3)", "7", "7"),
]  # fmt: skip
ROUND_TYPES = {
    "Float64": "toFloat64('{}')",
    "Decimal(18, 3)": "toDecimal64('{}', 3)",
    "Decimal(18, 0)": "toDecimal64('{}', 0)",
    "Int64": "toInt64('{}')",
}


@pytest.mark.parametrize("type_", ROUND_TYPES)
@pytest.mark.parametrize(("expr", "x", "expected"), ROUNDS, ids=[f"{e} {x}" for e, x, _ in ROUNDS])
def test_round_ties_away_from_zero(expr, x, expected, type_, clickhouse):
    if type_ in ("Decimal(18, 0)", "Int64") and "." in x:
        pytest.skip("not a whole number")
    sql = to_clickhouse(expr)
    value, kind = clickhouse.query(
        f"SELECT {sql}, toTypeName({sql}) FROM (SELECT {ROUND_TYPES[type_].format(x)} AS x)"
    ).result_rows[0]
    assert decimal.Decimal(str(value)) == decimal.Decimal(expected)
    if type_.startswith("Decimal"):
        assert kind == type_  # exact, at x's scale


def test_round_by_a_column(clickhouse):
    # ClickHouse's round, as before: ties away from zero for Decimal
    sql = to_clickhouse("ROUND(x, d)")
    rows = clickhouse.query(
        f"SELECT {sql} FROM (SELECT toDecimal64('0.125', 3) AS x, 2 - number * 3 AS d "
        "FROM numbers(2)) ORDER BY d DESC"
    ).result_rows
    assert [decimal.Decimal(str(v)) for (v,) in rows] == [decimal.Decimal("0.13"), 0]


def test_round_of_null_is_null(clickhouse):
    sql = to_clickhouse("ROUND(n, 2) IS NULL AND ROUND(n) IS NULL AND ROUND(n, -1) IS NULL")
    assert clickhouse.query(f"SELECT {sql} FROM {CH_BASE}").result_rows[0][0] == 1


# MOD and % of a Decimal: ClickHouse's modulo takes 125.50 % 7 as 12550 % 7 and answers
# 0.06. The rest keeps the dividend's sign (DuckDB and Postgres too).
MODS = [
    ("MOD(x, 7)", "125.50", "6.5"), ("x % 7", "-125.50", "-6.5"), ("x % 10", "125.53", "5.53"),
    ("MOD(x, y)", "125.50", "5.5"), ("x % y", "-0.25", "-0.25"),
]  # fmt: skip


@pytest.mark.parametrize(("expr", "x", "expected"), MODS, ids=[f"{e} {x}" for e, x, _ in MODS])
def test_mod_of_decimal(expr, x, expected, clickhouse, duck):
    sql = to_clickhouse(expr)
    base = f"(SELECT toDecimal64('{x}', 2) AS x, toDecimal64('7.5', 1) AS y)"
    value, kind = clickhouse.query(f"SELECT {sql}, toTypeName({sql}) FROM {base}").result_rows[0]
    assert decimal.Decimal(str(value)) == decimal.Decimal(expected)
    assert kind.startswith("Decimal")
    duck_base = f"(SELECT CAST('{x}' AS DECIMAL(18, 2)) AS x, CAST('7.5' AS DECIMAL(18, 1)) AS y)"
    assert duck.execute(f"SELECT {expr} FROM {duck_base}").fetchone()[0] == decimal.Decimal(
        expected
    )


def test_mod_keeps_integers(clickhouse):
    sql = to_clickhouse("MOD(i, 3) + x % -2")
    base = "(SELECT toInt32(-7) AS i, toInt64(7) AS x)"
    value, kind = clickhouse.query(f"SELECT {sql}, toTypeName({sql}) FROM {base}").result_rows[0]
    assert value == 0  # -1 + 1
    assert kind.startswith("Int")


@pytest.mark.parametrize("dividend", ["toDate('2026-01-01')", "'3'"])
def test_division_refuses_non_numbers(dividend, clickhouse):
    # Postgres refuses a date or a string dividend; toFloat64 would take a day count
    # or parse the string.
    with pytest.raises(Exception, match="Illegal types"):
        clickhouse.query(f"SELECT {to_clickhouse(f'{dividend} / 2')}")


# Interpolated, as the spec's Percentile Functions say: over 10, 4, 2.5, 1 the
# 0.9 position is 2.7, so 2.5 + 0.7 * (1 - 2.5) = 1.45. DuckDB 2.0 answers 1.44
# for this DESC form over DECIMAL (1.5.6 answers 1.45, and so does the ASC form).
PERCENTILE_BY_HAND = {("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY x DESC)", "decimal"): 1.45}


@pytest.mark.parametrize("base", PERCENTILE_BASES)
@pytest.mark.parametrize("expr", PERCENTILES)
def test_percentile_cont_is_exact(expr, base, clickhouse, duck):
    expected = PERCENTILE_BY_HAND.get((expr, base), DUCK)
    check(expr, expected, clickhouse, duck, *PERCENTILE_BASES[base])


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
# Compared with DuckDB, whose NULLs sort last both ways: ascending without NULLS FIRST |
# LAST, which is the spec's default ("Window ORDER BY NULL Placement", NULL as the
# largest value), and every explicit clause, in either direction.
WINDOWS = [
    "SUM(v) OVER (ORDER BY k ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    "SUM(v) OVER (ORDER BY k ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING)",
    "sum(v) over (order by k rows between 1 preceding and current row)",
    "ROW_NUMBER() OVER (ORDER BY k)",
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


# Descending without NULLS FIRST | LAST: DuckDB puts NULLs last, the spec first ("Window
# ORDER BY NULL Placement": NULL is the largest value, last for ASC, first for DESC).
# Expected values by hand, as (id, value) for ids 1, 2, 3, over k = 1, NULL, 2 and
# v = 10, 20, 30; descending, the rows come in the order id 2, 3, 1.
DESC_WINDOWS = [
    (
        "SUM(v) OVER (ORDER BY k DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
        [(1, 60.0), (2, 20.0), (3, 50.0)],
    ),
    (
        "SUM(v) OVER (ORDER BY k DESC RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
        [(1, 60.0), (2, 20.0), (3, 50.0)],
    ),
    (
        "AVG(v) OVER (ORDER BY k DESC ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)",
        [(1, 10.0), (2, 20.0), (3, 20.0)],
    ),
    ("ROW_NUMBER() OVER (ORDER BY k DESC)", [(1, 3), (2, 1), (3, 2)]),
    ("RANK() OVER (ORDER BY k DESC)", [(1, 3), (2, 1), (3, 2)]),
    ("LAG(v, 1) OVER (ORDER BY k DESC)", [(1, 30.0), (2, None), (3, 20.0)]),
    ("FIRST_VALUE(v) OVER (ORDER BY k DESC)", [(1, 20.0), (2, 20.0), (3, 20.0)]),
    # an explicit clause wins over the direction
    ("RANK() OVER (ORDER BY k DESC NULLS LAST)", [(1, 2), (2, 3), (3, 1)]),
    ("RANK() OVER (ORDER BY k DESC NULLS FIRST)", [(1, 3), (2, 1), (3, 2)]),
    ("RANK() OVER (ORDER BY k NULLS FIRST)", [(1, 2), (2, 1), (3, 3)]),
    # the default is per ORDER BY item
    ("ROW_NUMBER() OVER (ORDER BY id > 0 DESC, k DESC)", [(1, 3), (2, 1), (3, 2)]),
    ("ROW_NUMBER() OVER (ORDER BY k DESC NULLS LAST, id DESC)", [(1, 2), (2, 3), (3, 1)]),
]


@pytest.mark.parametrize(("expr", "expected"), DESC_WINDOWS)
def test_window_desc_puts_null_first(expr, expected, clickhouse):
    got = clickhouse.query(f"SELECT id, {to_clickhouse(expr)} FROM {CH_WINDOW} ORDER BY id")
    assert [tuple(map(norm, r)) for r in got.result_rows] == expected


# The spec's own example: revenue over three rows, 10, NULL, 5.
SPEC_REVENUE = "(SELECT arrayJoin([10, NULL, 5]::Array(Nullable(Int32))) AS revenue)"


@pytest.mark.parametrize(
    ("expr", "numbers"),
    [
        # ROW_NUMBER() OVER (ORDER BY revenue)       -- 5, 10, NULL
        ("ROW_NUMBER() OVER (ORDER BY revenue)", {5: 1, 10: 2, None: 3}),
        # ROW_NUMBER() OVER (ORDER BY revenue DESC)  -- NULL, 10, 5
        ("ROW_NUMBER() OVER (ORDER BY revenue DESC)", {None: 1, 10: 2, 5: 3}),
    ],
)
def test_window_null_order_spec_example(expr, numbers, clickhouse):
    got = clickhouse.query(f"SELECT revenue, {to_clickhouse(expr)} FROM {SPEC_REVENUE}")
    assert dict(got.result_rows) == numbers


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


@pytest.mark.parametrize(
    "expr",
    [c[0] for c in AGGREGATES + CATALOG] + WINDOWS + [d[0] for d in DESC_WINDOWS] + OFFSETS,
)
def test_rewrite_is_idempotent(expr):
    # The planner rewrites a metric, then again inside a filter that names it.
    from dactopus_ossie_clickhouse.translate import parse, rewrite_tree

    once = rewrite_tree(parse(expr))
    assert rewrite_tree(once).sql("clickhouse") == once.sql("clickhouse")

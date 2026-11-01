"""The query compliance cases of apache/ossie (``compliance/query``), answered
on ClickHouse.

Each case is a Relational Query Interface query (``query.ossie.sql``, with
``AGG()``) and its expected result as standard SQL over literals
(``expected.sql``). This executor compiles questions of the Semantic Query
Interface straight to ClickHouse SQL and does not read ``AGG()`` SQL
(docs/design.md, "Questions are the only query input"), so each case is written here
as the question it asks; a case the checkout has and this file does not fails
until it is added. The data and the model are the suite's own: ``scott.sql``
loaded through DuckDB, the reference engine, and ``scott_model.ossie.yaml``
as it is. Results are compared by the suite's README ("Comparing results").

Needs ``OSSIE_REPO``, a checkout of apache/ossie, and a ClickHouse server.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.model import load_model
from dactopus_ossie_clickhouse.planner import Query

pytestmark = pytest.mark.skipif(
    "OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie"
)

# Case directory -> the question its query.ossie.sql asks.
CASES = {
    "a01_grand_total": Query(("emp.sum_sal", "emp.emp_count")),
}

# DuckDB column types of scott.sql -> ClickHouse. Nullable follows DESCRIBE.
TYPES = {"INTEGER": "Int32", "VARCHAR": "String", "DATE": "Date32"}


def suite() -> Path:
    return Path(os.environ["OSSIE_REPO"]) / "compliance" / "query"


def clickhouse_type(duckdb_type: str, nullable: bool) -> str:
    m = re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", duckdb_type)
    t = f"Decimal({m[1]}, {m[2]})" if m else TYPES[duckdb_type]
    return f"Nullable({t})" if nullable else t


@pytest.fixture(scope="module")
def scott_duck():
    """DuckDB with scott.sql run as written: the reference engine's copy of the data."""
    import duckdb

    con = duckdb.connect()
    con.execute((suite() / "scott" / "scott.sql").read_text())
    yield con
    con.close()


@pytest.fixture(scope="module")
def ex(clickhouse, scott_duck):
    """The executor over the suite's model, the data copied from DuckDB table by
    table so the file stays the suite's and the values stay what the file says."""
    clickhouse.command("DROP DATABASE IF EXISTS scott")
    clickhouse.command("CREATE DATABASE scott")
    tables = scott_duck.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'scott'"
    ).fetchall()
    for (table,) in tables:
        described = scott_duck.execute(f"DESCRIBE scott.{table}").fetchall()
        columns = [(name, clickhouse_type(t, null == "YES")) for name, t, null, *_ in described]
        clickhouse.command(
            f"CREATE TABLE scott.{table} ({', '.join(f'{n} {t}' for n, t in columns)}) "
            "ENGINE = MergeTree ORDER BY tuple()"
        )
        rows = scott_duck.execute(f"SELECT * FROM scott.{table}").fetchall()
        clickhouse.insert(table, rows, column_names=[n for n, _ in columns], database="scott")
    yield Executor(clickhouse, load_model(suite() / "scott" / "scott_model.ossie.yaml"))
    clickhouse.command("DROP DATABASE scott")


def expected(scott_duck, case: str):
    """Columns and rows of expected.sql, run on DuckDB: standard SQL over literals."""
    res = scott_duck.execute((suite() / "cases" / case / "expected.sql").read_text())
    return [d[0] for d in res.description], res.fetchall()


def values_match(actual, wanted) -> bool:
    """README, "Comparing results": NULL matches NULL, exact numerics are equal,
    an approximate one within 1e-9 relative (1e-9 absolute near zero)."""
    if actual is None or wanted is None:
        return actual is None and wanted is None
    if isinstance(actual, float) or isinstance(wanted, float):
        a, e = float(actual), float(wanted)
        return math.isfinite(a) and abs(a - e) <= 1e-9 * max(abs(a), abs(e), 1)
    if isinstance(actual, (int, Decimal)) and isinstance(wanted, (int, Decimal)):
        return Decimal(actual) == Decimal(wanted)
    return actual == wanted


def rows_match(actual: list[tuple], wanted: list[tuple], ordered: bool) -> bool:
    """Rows as multisets, duplicates counted; in order when the query orders them."""
    if len(actual) != len(wanted):
        return False
    if ordered:
        return all(
            all(values_match(a, w) for a, w in zip(ra, rw, strict=True))
            for ra, rw in zip(actual, wanted, strict=True)
        )
    left = Counter(actual)
    for row in wanted:
        match = next(
            (
                r
                for r in left
                if left[r] and all(values_match(a, w) for a, w in zip(r, row, strict=True))
            ),
            None,
        )
        if match is None:
            return False
        left[match] -= 1
    return True


def test_every_case_has_its_question():
    cases = sorted(p.name for p in (suite() / "cases").iterdir() if p.is_dir())
    missing = [c for c in cases if c not in CASES]
    assert not missing, f"apache/ossie added cases without a question here: {missing}"


@pytest.mark.parametrize("case", sorted(CASES))
def test_case(ex, scott_duck, case):
    query_sql = (suite() / "cases" / case / "query.ossie.sql").read_text()
    columns, rows = expected(scott_duck, case)
    result = ex.execute(CASES[case])
    # The case aliases each AGG(m) as m; this executor names a column dataset.metric.
    names = [c.lower().rpartition(".")[2] for c in result.columns]
    assert names == [c.lower() for c in columns]
    ordered = re.search(r"^\s*ORDER\s+BY\b", query_sql, re.IGNORECASE | re.MULTILINE) is not None
    assert rows_match(result.rows, rows, ordered), (result.rows, rows, result.sql)


def test_aggregates_over_no_rows(ex):
    """README, "Result semantics": standard SQL, so SUM over no rows is NULL and
    COUNT is 0, whatever ClickHouse's own defaults."""
    result = ex.execute(Query(("emp.sum_sal", "emp.emp_count"), filters=("emp.sal < 0",)))
    assert result.rows == [(None, 0)]

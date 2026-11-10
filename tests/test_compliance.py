"""The query compliance cases of apache/ossie (``compliance/query``), answered
on ClickHouse.

Each case is a Relational Query Interface query (``query.ossie.sql``, with
``AGG()``) and its expected result as standard SQL over literals
(``expected.sql``). This executor compiles questions of the Semantic Query
Interface straight to ClickHouse SQL and does not read ``AGG()`` SQL
(docs/design.md, "Questions are the only query input"), so each case is written here
as the question it asks. A case whose query has a form no question has is
listed with that form (``NOT_ASKED``), a question the planner refuses is checked
to be refused (``REFUSED``), and a case the checkout has and this file does not
fails until it is added. The data and the model are the suite's own: ``scott.sql``
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
import sqlglot

from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.model import load_model
from dactopus_ossie_clickhouse.planner import PlanError, Query

pytestmark = pytest.mark.skipif(
    "OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie"
)

# Case directory -> the question its query.ossie.sql asks.
CASES = {
    "a01_grand_total": Query(("emp.sum_sal", "emp.emp_count")),
    "a02_measure_per_group": Query(
        ("emp.sum_sal", "emp.emp_count"), ("emp.deptno",), order_by=("emp.deptno",)
    ),
    "a03_where_before_measure": Query(
        ("emp.sum_sal", "emp.emp_count"),
        ("emp.deptno",),
        ("emp.job <> 'CLERK'",),
        ("emp.deptno",),
    ),
    "a04_having_on_unselected_measure": Query(
        ("emp.sum_sal",), ("emp.job",), ("emp.emp_count > 2",), ("emp.sum_sal desc",), 3
    ),
    "a06_nulls_follow_aggregate": Query(
        ("emp.emp_count", "emp.comm_count", "emp.sum_comm", "emp.avg_comm"),
        ("emp.deptno",),
        order_by=("emp.deptno",),
    ),
    "a07_empty_input_set": Query(
        ("emp.emp_count", "emp.sum_sal", "emp.avg_sal"), filters=("emp.deptno = 40",)
    ),
    "b01_many_to_one_no_fan_out": Query(
        ("emp.sum_sal", "emp.emp_count"), ("dept.dname",), order_by=("dept.dname",)
    ),
    "b02_filter_on_joined_relation": Query(
        ("emp.sum_sal", "emp.emp_count"),
        ("emp.job",),
        ("dept.loc = 'NEW YORK'",),
        ("emp.job",),
    ),
}

# Questions this executor refuses: a metric of the one side (dept) grouped or
# filtered by the many side (emp) would have to count each department once per
# group (relational_semantics.md, section 9), and the planner answers a question
# over the rows of one root (docs/design.md, "Refuse a metric over another
# dataset's rows"). A refusal is not a wrong number; a case answered one day
# moves to CASES.
REFUSED = {
    "b04_one_side_counted_once": Query(
        ("dept.dept_count", "emp.emp_count"), ("emp.job",), order_by=("emp.job",)
    ),
    "b18_filter_restricts_one_side_measure": Query(
        ("dept.dept_count", "emp.emp_count"),
        ("emp.job",),
        ("emp.sal > 1000",),
        ("emp.job",),
    ),
    "b20_having_on_one_side_measure": Query(
        ("dept.dept_count",), ("emp.job",), ("dept.dept_count > 1",), ("emp.job",)
    ),
}

# Cases a question cannot ask, each with the form of its query that a question
# does not have: metrics, dimensions and filters by name, joined along the
# model's relationships from the metric's dataset.
_EXPRESSION = "an expression over measures in the SELECT list"
_TWICE = "one dataset read twice, as an employee and as a manager"
_SUBQUERY = "a join to a subquery"
_RANGE = "a join on a range, with no relationship in the model"
NOT_ASKED = {
    "a05_scalar_ops_on_measure_values": _EXPRESSION,
    "a08_group_by_all_skips_measures": "grouping by an expression that is not a field",
    "a09_rollup_not_average_of_averages": "ROLLUP",
    "a10_cte_passes_measures_through": "a CTE that renames measures",
    "a11_agg_in_where_subquery": "a measure in a WHERE subquery",
    "a12_aggregate_over_agg_values": "an aggregate over measure values, and UNION ALL",
    "b03_top_n_join_as_filter": _SUBQUERY,
    "b05_manager_counted_once": _TWICE,
    "b06_range_join_fan_out": _RANGE,
    "b07_three_way_join_fan_out": _RANGE,
    "b08_many_to_many_chasm_trap": "a join on columns that are no key, one dataset read twice",
    "b09_inner_join_matched_rows": _TWICE,
    "b10_left_join_null_padding": _TWICE,
    "b11_right_join_keeps_non_managers": "RIGHT JOIN",
    "b12_full_join_each_row_once": "FULL JOIN",
    "b13_cross_join_no_inflation": "CROSS JOIN",
    "b14_ratio_across_relations": _EXPRESSION,
    "b15_left_join_changes_ratio": _EXPRESSION,
    "b16_on_filter_keeps_left_rows": _EXPRESSION,
    # The question with these names keeps emp rows, so it has no row for a
    # department without employees.
    "b17_left_join_empty_group": "LEFT JOIN from the one side",
    "b19_nonunique_filter_no_fan_out": _SUBQUERY,
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
    an approximate one within 1e-9 relative (1e-9 absolute near zero). Whether a
    value is approximate is the expected literal's call: a float returned for an
    exact one must equal it exactly."""
    if actual is None or wanted is None:
        return actual is None and wanted is None
    if isinstance(wanted, float) and isinstance(actual, (int, float, Decimal)):
        a = float(actual)
        return math.isfinite(a) and abs(a - wanted) <= 1e-9 * max(abs(a), abs(wanted), 1)
    if isinstance(actual, (int, float, Decimal)) and isinstance(wanted, (int, Decimal)):
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
    known = [*CASES, *REFUSED, *NOT_ASKED]
    assert len(known) == len(set(known)), "a case is listed twice"
    missing = [c for c in cases if c not in known]
    assert not missing, f"apache/ossie added cases without a question here: {missing}"


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_refused_case(ex, case):
    with pytest.raises(PlanError, match="aggregates dept rows"):
        ex.execute(REFUSED[case])


@pytest.mark.parametrize("case", sorted(CASES))
def test_case(ex, scott_duck, case):
    query_sql = (suite() / "cases" / case / "query.ossie.sql").read_text()
    columns, rows = expected(scott_duck, case)
    result = ex.execute(CASES[case])
    # The case aliases each AGG(m) as m; this executor names a column dataset.metric.
    names = [c.lower().rpartition(".")[2] for c in result.columns]
    assert names == [c.lower() for c in columns]
    # The query's own ORDER BY, not one in a window or a subquery.
    ordered = sqlglot.parse_one(query_sql).args.get("order") is not None
    assert rows_match(result.rows, rows, ordered), (result.rows, rows, result.sql)


def test_aggregates_over_no_rows(ex):
    """README, "Result semantics": standard SQL, so SUM over no rows is NULL and
    COUNT is 0, whatever ClickHouse's own defaults."""
    result = ex.execute(Query(("emp.sum_sal", "emp.emp_count"), filters=("emp.sal < 0",)))
    assert result.rows == [(None, 0)]

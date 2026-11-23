"""The query compliance cases of apache/ossie (``compliance/query``), answered
on ClickHouse.

Each case is a script for Ossie's ``ost`` test runner (``<case>.ost``,
apache/ossie#769): a Relational Query Interface query with ``AGG()``, its
expansion into a DuckDB query over the source tables, and the table that
expansion returns. This executor compiles questions of the Semantic Query
Interface straight to ClickHouse SQL and does not read ``AGG()`` SQL
(docs/decisions/questions-only.md), so each case is written here as the
question it asks, and its expected rows are the expansion run on DuckDB,
which keeps whether each value is exact or approximate. A case whose query
has a form no question has is listed with that form (``NOT_ASKED``), a
question the planner refuses is checked to be refused (``REFUSED``), and a
case the checkout has and this file does not fails until it is added. The
data and the model are the suite's own: ``scott.sql`` loaded through DuckDB,
the reference engine, and ``scott_model.ossie.yaml`` as it is. Results are
compared by the suite's README ("Comparing results").

Needs ``OSSIE_REPO``, a checkout of apache/ossie, and a ClickHouse server.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
import sqlglot

from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.model import load_model
from dactopus_ossie_clickhouse.planner import PlanError, Query

pytestmark = pytest.mark.skipif(
    "OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie"
)

# Case script -> the question its query asks.
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
    # A ratio of measures of two datasets is an expression over metrics in the question
    # (docs/decisions/expressions-over-metrics.md). B14 joins inner: the departments with
    # employees, which a filter on the employees asks for as a semi-join of dept_count.
    # B15 joins LEFT from dept: every department.
    "b14_ratio_across_relations": Query(
        (
            "dept.dept_count",
            "emp.emp_count",
            "emp.sum_sal",
            "emp.sum_sal / dept.dept_count AS sal_per_dept",
        ),
        filters=("emp.deptno IS NOT NULL",),
    ),
    # B16's filter is in the ON clause: dept_count counts every department, and the other
    # measures only the clerks. A question's row filter would apply to dept_count too, so
    # the condition goes on the parts that see it.
    "b16_on_filter_keeps_left_rows": Query(
        (
            "dept.dept_count",
            "emp.emp_count FILTER (WHERE emp.job = 'CLERK') AS emp_count",
            "emp.sum_sal FILTER (WHERE emp.job = 'CLERK') AS sum_sal",
            "emp.sum_sal FILTER (WHERE emp.job = 'CLERK') / dept.dept_count AS sal_per_dept",
        )
    ),
    "b15_left_join_changes_ratio": Query(
        (
            "dept.dept_count",
            "emp.emp_count",
            "emp.sum_sal",
            "emp.sum_sal / dept.dept_count AS sal_per_dept",
        )
    ),
}

# Questions this executor refuses, with why in a comment: none today. A refusal
# is not a wrong number; a case answered one day moves to CASES.
REFUSED: dict[str, Query] = {}

# Cases a question cannot ask, each with the form of its query that a question
# does not have: metrics, dimensions and filters by name, joined along the
# model's relationships from the metric's dataset.
_COALESCE = (
    "a function over measure values, COALESCE: an expression over metrics takes "
    "+ - * / and no function (docs/decisions/expressions-over-metrics.md)"
)
_TWICE = "one dataset read twice, as an employee and as a manager"
_SUBQUERY = "a join to a subquery"
_RANGE = "a join on a range, with no relationship in the model"
NOT_ASKED = {
    "a05_scalar_ops_on_measure_values": _COALESCE,
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
    # The question with these names keeps emp rows, so it has no row for a
    # department without employees.
    "b17_left_join_empty_group": "LEFT JOIN from the one side",
    "b19_nonunique_filter_no_fan_out": _SUBQUERY,
}

# DuckDB column types of scott.sql -> ClickHouse. Nullable follows DESCRIBE.
TYPES = {"INTEGER": "Int32", "VARCHAR": "String", "DATE": "Date32"}


# A call of executeExpand in a script: the query, then its expansion between {_| and |_}.
CALL = re.compile(
    r"^executeExpand\((?P<query>.*?)\);\n> \{_\|\n(?P<expansion>.*?)\n\|_\} : string\n",
    re.S | re.M,
)


def suite() -> Path:
    return Path(os.environ["OSSIE_REPO"]) / "compliance" / "query"


def cases() -> list[str]:
    found = sorted(p.stem for p in suite().glob("*.ost"))
    if not found:
        pytest.skip("this checkout of apache/ossie predates its ost scripts (#769)")
    return found


def call(case: str) -> re.Match:
    """The one query of a case's script, with its expansion."""
    calls = list(CALL.finditer((suite() / f"{case}.ost").read_text()))
    assert len(calls) == 1, f"{case}.ost has {len(calls)} queries; a question asks one"
    return calls[0]


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
    """Columns and rows of the case's expansion, run on DuckDB over scott.sql: the
    rows the script's table shows, with the types DuckDB gives them."""
    res = scott_duck.execute(call(case)["expansion"])
    return [d[0] for d in res.description], res.fetchall()


def _cents(x) -> Decimal:
    """Rounded to 2 decimal places, halves away from zero."""
    return Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def values_match(actual, wanted) -> bool:
    """README, "Comparing results": NULL matches NULL, exact numerics are equal,
    an approximate one rounded to 2 decimal places (halves away from zero) equals
    the expected value, which the script's table shows rounded so. Whether a value
    is approximate is the expansion's call: a float returned for an exact one must
    equal it exactly."""
    if actual is None or wanted is None:
        return actual is None and wanted is None
    if isinstance(wanted, float) and isinstance(actual, (int, float, Decimal)):
        return math.isfinite(float(actual)) and _cents(actual) == _cents(wanted)
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
    found = cases()
    known = [*CASES, *REFUSED, *NOT_ASKED]
    assert len(known) == len(set(known)), "a case is listed twice"
    missing = [c for c in found if c not in known]
    assert not missing, f"apache/ossie added cases without a question here: {missing}"
    stale = [c for c in known if c not in found]
    assert not stale, f"cases listed here that apache/ossie no longer has: {stale}"


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_refused_case(ex, case):
    with pytest.raises(PlanError, match="aggregates dept rows"):
        ex.execute(REFUSED[case])


@pytest.mark.parametrize("case", sorted(CASES))
def test_case(ex, scott_duck, case):
    query_sql = call(case)["query"]
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

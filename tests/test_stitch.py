"""Metrics of two facts in one question, against ClickHouse, compared with DuckDB.

The data is the F_PRELUDE fixture of the Ossie compliance suite (apache/ossie#237)
with one more orphan return, so both facts have a NULL-region group. Expected
rows come from DuckDB running the plan apache/ossie#246 (cc0d070) describes:
each metric aggregated on its own, then FULL OUTER JOIN on the dimensions with
NULL-safe equality (§6.6 row 3), CROSS JOIN of totals with no dimension (§6.6
row 4). A metric missing from a group shows its value over no rows: NULL for
SUM, 0 for COUNT (§6.11.5)."""

from pathlib import Path

import pytest
from ossie import OssieDocument

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import (
    Catalog,
    PlanError,
    Planner,
    Query,
    unanswerable_metrics,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stitch.yaml"
ROWS = {
    "customers": "(1, 'EAST', 'retail'), (2, 'EAST', 'retail'), (3, 'WEST', 'wholesale'), "
    "(4, 'NORTH', 'retail')",
    # Order 105 is an orphan: customer 99 does not exist.
    "orders": "(101, 1, 100.00, 'completed'), (102, 1, 50.00, 'completed'), "
    "(103, 2, 200.00, 'pending'), (104, 3, 75.00, 'completed'), (105, 99, 30.00, 'completed')",
    # Customer 4 has a return but no orders; return 204 is an orphan.
    "returns": "(201, 1, 10.00), (202, 3, 5.00), (203, 4, 15.00), (204, 98, 7.00)",
}
COLUMNS = {
    "customers": (
        "id Int32, region String, segment String",
        "id INTEGER, region VARCHAR, segment VARCHAR",
    ),
    "orders": (
        "id Int32, customer_id Int32, amount Decimal(10, 2), status String",
        "id INTEGER, customer_id INTEGER, amount DECIMAL(10, 2), status VARCHAR",
    ),
    "returns": (
        "id Int32, customer_id Int32, amount Decimal(10, 2)",
        "id INTEGER, customer_id INTEGER, amount DECIMAL(10, 2)",
    ),
}

# Each fact aggregated by region, as #246 §6.2 step A.6 resolves a metric.
REVENUE = """SELECT c.region, SUM(o.amount) AS total_revenue, COUNT(o.id) AS order_count,
    COUNT(o.id) FILTER (WHERE o.status = 'completed') AS completed_orders,
    COALESCE(SUM(o.amount), 0) AS revenue_or_zero
  FROM orders o LEFT JOIN customers c ON o.customer_id = c.id {where} GROUP BY c.region"""
RETURNS = """SELECT c.region, SUM(x.amount) AS total_returns
  FROM returns x LEFT JOIN customers c ON x.customer_id = c.id {where} GROUP BY c.region"""


RETAIL = "WHERE c.segment = 'retail'"


def merged(where: str = "") -> str:
    """Both facts by region: FULL OUTER JOIN, NULL-safe (§6.6 row 3); a fact without
    the group gives its metric over no rows (§6.11.5)."""
    return f"""
SELECT COALESCE(r.region, t.region) AS region, r.total_revenue,
       COALESCE(r.order_count, 0) AS order_count,
       COALESCE(r.completed_orders, 0) AS completed_orders,
       COALESCE(r.revenue_or_zero, 0) AS revenue_or_zero, t.total_returns
FROM ({REVENUE.format(where=where)}) r
FULL OUTER JOIN ({RETURNS.format(where=where)}) t ON r.region IS NOT DISTINCT FROM t.region"""


CASES = [
    # Every region of either fact; one NULL row, not one per fact.
    (
        Query(("total_revenue", "total_returns"), ("customers.region",)),
        f"SELECT region, total_revenue, total_returns FROM ({merged()})",
    ),
    # NORTH has no orders: a count of them is 0, not NULL.
    (
        Query(("order_count", "total_returns"), ("customers.region",)),
        f"SELECT region, order_count, total_returns FROM ({merged()})",
    ),
    (
        Query(("completed_orders", "total_returns"), ("customers.region",)),
        f"SELECT region, completed_orders, total_returns FROM ({merged()})",
    ),
    (
        Query(("revenue_or_zero", "total_returns"), ("customers.region",)),
        f"SELECT region, revenue_or_zero, total_returns FROM ({merged()})",
    ),
    # No dimension: one row of each fact's total (§6.6 row 4).
    (
        Query(("total_revenue", "total_returns")),
        "SELECT (SELECT SUM(amount) FROM orders), (SELECT SUM(amount) FROM returns)",
    ),
    # Over no rows a sum is NULL and a count 0 (§6.11.5), not ClickHouse's 0 for both.
    (
        Query(("order_count", "total_returns"), (), ("customers.region = 'SOUTH'",)),
        "SELECT (SELECT COUNT(o.id) FROM orders o JOIN customers c ON o.customer_id = c.id "
        "WHERE c.region = 'SOUTH'), (SELECT SUM(x.amount) FROM returns x "
        "JOIN customers c ON x.customer_id = c.id WHERE c.region = 'SOUTH')",
    ),
    # A row filter applies to every metric (§6.2 step A.6.1).
    (
        Query(
            ("total_revenue", "total_returns"),
            ("customers.region",),
            ("customers.segment = 'retail'",),
        ),
        f"SELECT region, total_revenue, total_returns FROM ({merged(RETAIL)})",
    ),
    # Aggregate filters run on the merged rows (§6.2 step A.9), on a metric not
    # selected too.
    (
        Query(("total_revenue", "total_returns"), ("customers.region",), ("total_returns > 6",)),
        f"SELECT region, total_revenue, total_returns FROM ({merged()}) WHERE total_returns > 6",
    ),
    (
        Query(("total_returns",), ("customers.region",), ("order_count > 1",)),
        f"SELECT region, total_returns FROM ({merged()}) WHERE order_count > 1",
    ),
    (
        Query(
            ("total_revenue", "total_returns"),
            ("customers.region",),
            ("customers.region <> 'EAST'", "total_revenue > 0 OR total_returns > 0"),
        ),
        f"SELECT region, total_revenue, total_returns FROM ({merged()}) "
        "WHERE region <> 'EAST' AND (total_revenue > 0 OR total_returns > 0)",
    ),
    # An aggregate filter may name a dimension too: it reads the merged column.
    (
        Query(
            ("total_revenue", "total_returns"),
            ("customers.region",),
            ("customers.region <> 'EAST' AND total_returns > 6",),
        ),
        f"SELECT region, total_revenue, total_returns FROM ({merged()}) "
        "WHERE region <> 'EAST' AND total_returns > 6",
    ),
]
ORDERED = [
    (
        Query(
            ("total_revenue", "total_returns"),
            ("customers.region",),
            order_by=("total_returns desc",),
            limit=2,
        ),
        f"SELECT region, total_revenue, total_returns FROM ({merged()}) "
        "ORDER BY total_returns DESC LIMIT 2",
    ),
    # Default order: the first metric descending, NULLs last.
    (
        Query(("total_revenue", "total_returns"), ("customers.region",)),
        f"SELECT region, total_revenue, total_returns FROM ({merged()}) "
        "ORDER BY total_revenue DESC NULLS LAST",
    ),
    # WEST and the NULL region tie at one order: the dimension breaks the tie, so
    # the limit keeps WEST on every run.
    (
        Query(("order_count", "total_returns"), ("customers.region",), limit=2),
        f"SELECT region, order_count, total_returns FROM ({merged()}) "
        "ORDER BY order_count DESC, region ASC NULLS LAST LIMIT 2",
    ),
]


@pytest.fixture(scope="module")
def ex(clickhouse):
    clickhouse.command("CREATE DATABASE IF NOT EXISTS ossie_stitch")
    for table, (columns, _) in COLUMNS.items():
        clickhouse.command(f"DROP TABLE IF EXISTS ossie_stitch.{table}")
        clickhouse.command(
            f"CREATE TABLE ossie_stitch.{table} ({columns}) ENGINE = MergeTree ORDER BY id"
        )
        clickhouse.command(f"INSERT INTO ossie_stitch.{table} VALUES {ROWS[table]}")
    yield Executor(clickhouse, load_model(FIXTURE))
    clickhouse.command("DROP DATABASE ossie_stitch")


@pytest.fixture(scope="module")
def stitch_duck():
    """Its own DuckDB connection: the shared one has other tables named alike."""
    import duckdb

    con = duckdb.connect()
    for table, (_, columns) in COLUMNS.items():
        con.execute(f"CREATE TABLE {table} ({columns})")
        con.execute(f"INSERT INTO {table} VALUES {ROWS[table]}")
    yield con
    con.close()


def _key(row: tuple) -> tuple:
    return tuple((v is None, v if v is not None else 0) for v in row)


@pytest.mark.parametrize(("query", "gold"), CASES)
def test_stitched_answers_match_the_reference(ex, stitch_duck, query, gold):
    got = ex.execute(query)
    assert " UNION ALL " in got.sql
    assert sorted(got.rows, key=_key) == sorted(stitch_duck.sql(gold).fetchall(), key=_key)


@pytest.mark.parametrize(("query", "gold"), ORDERED)
def test_stitched_order_and_limit(ex, stitch_duck, query, gold):
    assert ex.execute(query).rows == stitch_duck.sql(gold).fetchall()


def test_wrong_plans_differ_on_this_data(stitch_duck):
    """The data tells the reference plan from the plausible wrong ones."""
    right = sorted(stitch_duck.sql(CASES[0][1]).fetchall(), key=_key)
    wrong = [
        # The dimension's values, LEFT JOIN by plain equality: the NULL group is lost.
        f"""SELECT d.region, r.total_revenue, t.total_returns
            FROM (SELECT DISTINCT region FROM customers) d
            LEFT JOIN ({REVENUE.format(where="")}) r ON r.region = d.region
            LEFT JOIN ({RETURNS.format(where="")}) t ON t.region = d.region""",
        # FULL OUTER JOIN by plain equality: the NULL group splits in two.
        f"""SELECT COALESCE(r.region, t.region), r.total_revenue, t.total_returns
            FROM ({REVENUE.format(where="")}) r
            FULL OUTER JOIN ({RETURNS.format(where="")}) t ON r.region = t.region""",
        # One SELECT from orders: NORTH is lost and returns repeat per order.
        """SELECT c.region, SUM(o.amount), SUM(x.amount) FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.id
            LEFT JOIN returns x ON x.customer_id = c.id GROUP BY c.region""",
    ]
    for sql in wrong:
        assert sorted(stitch_duck.sql(sql).fetchall(), key=_key) != right


def test_counts_are_zero_and_sums_null_for_a_missing_group(ex):
    rows = {r[0]: r[1:] for r in ex.execute(CASES[1][0]).rows}
    assert rows["NORTH"] == (0, 15)
    rows = {r[0]: r[1:] for r in ex.execute(CASES[0][0]).rows}
    assert rows["NORTH"][0] is None and rows[None] == (30, 7)


def test_one_metric_over_both_facts_is_still_refused():
    model = load_model(FIXTURE)
    assert "return_rate" in unanswerable_metrics(model)
    with pytest.raises(PlanError):
        Planner(model).sql(Query(("return_rate",), ("customers.region",)))


def test_a_group_that_cannot_reach_a_dimension_names_its_metrics():
    """An orders field next to a returns metric: the refusal is the returns group's,
    not the single-root one that only says the facts do not join."""
    with pytest.raises(
        PlanError, match=r"'total_returns' cannot be .*: datasets \['orders', 'returns'\]"
    ):
        Planner(load_model(FIXTURE)).sql(
            Query(("total_revenue", "total_returns"), ("orders.status",))
        )


def test_an_aggregate_outside_the_spec_is_not_stitched():
    """Over no rows countIf is 0 and sumIf NULL; nothing here can tell which."""
    data = load_model(FIXTURE).model_dump(by_alias=True, exclude_none=True)
    data["metrics"].append(
        {
            "name": "big_orders",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "countIf(orders.amount > 60)"}]
            },
        }
    )
    planner = Planner(
        OssieDocument.model_validate(data), Catalog(aggregate=lambda name: name == "countIf")
    )
    assert "countIf(orders.amount > 60)" in planner.sql(
        Query(("big_orders",), ("customers.region",))
    )
    with pytest.raises(PlanError, match="uses countIf, an aggregate outside the spec"):
        planner.sql(Query(("big_orders", "total_returns"), ("customers.region",)))

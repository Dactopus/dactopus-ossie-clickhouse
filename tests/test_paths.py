"""Paths of more than one relationship, against ClickHouse, compared with DuckDB.

Order lines and refunds reach customers only through orders (#246 §6.9 rule 1).
Expected rows come from DuckDB running LEFT JOINs along the path from the fact
outward (§6.6 row 1): a fact row without a match at either step lands in the
NULL group, and a customer no fact reaches does not appear (§6.2 step A.7). The
intermediate dataset takes part in the query like any other (§6.2, datasets
involved in a query): where may filter it."""

from pathlib import Path

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import Query

FIXTURE = Path(__file__).parent / "fixtures" / "paths.yaml"
ROWS = {
    # NORTH has no orders.
    "customers": "(1, 'EAST'), (2, 'WEST'), (3, 'NORTH')",
    # Order 13 is an orphan: customer 99 does not exist.
    "orders": "(10, 1, 'paid'), (11, 1, 'paid'), (12, 2, 'open'), (13, 99, 'paid')",
    # Line 104 is on the orphan order; line 105 is an orphan itself: order 77 does not exist.
    "lines": "(100, 10, 5.00), (101, 10, 7.00), (102, 11, 3.00), (103, 12, 4.00), "
    "(104, 13, 6.00), (105, 77, 2.00)",
    "refunds": "(200, 10, 1.00), (201, 12, 2.00), (202, 88, 4.00)",
}
COLUMNS = {
    "customers": ("id Int32, region String", "id INTEGER, region VARCHAR"),
    "orders": (
        "id Int32, customer_id Int32, status String",
        "id INTEGER, customer_id INTEGER, status VARCHAR",
    ),
    "lines": (
        "id Int32, order_id Int32, amount Decimal(10, 2)",
        "id INTEGER, order_id INTEGER, amount DECIMAL(10, 2)",
    ),
    "refunds": (
        "id Int32, order_id Int32, amount Decimal(10, 2)",
        "id INTEGER, order_id INTEGER, amount DECIMAL(10, 2)",
    ),
}

LINES = """SELECT c.region, SUM(l.amount) AS line_amount, COUNT(l.id) AS line_count
  FROM lines l LEFT JOIN orders o ON l.order_id = o.id
  LEFT JOIN customers c ON o.customer_id = c.id {where} GROUP BY c.region"""
REFUNDS = """SELECT c.region, SUM(r.amount) AS refund_amount
  FROM refunds r LEFT JOIN orders o ON r.order_id = o.id
  LEFT JOIN customers c ON o.customer_id = c.id GROUP BY c.region"""

PAID = "WHERE o.status = 'paid'"

CASES = [
    # Both orphans in one NULL group; NORTH absent.
    (
        Query(("line_amount", "line_count"), ("customers.region",)),
        f"SELECT region, line_amount, line_count FROM ({LINES.format(where='')})",
    ),
    # A filter on the dataset in the middle of the path.
    (
        Query(("line_amount",), ("customers.region",), ("orders.status = 'paid'",)),
        f"SELECT region, line_amount FROM ({LINES.format(where=PAID)})",
    ),
    # Two facts by a dimension two steps from each, stitched (§6.6 row 3).
    (
        Query(("line_amount", "refund_amount"), ("customers.region",)),
        f"""SELECT COALESCE(a.region, b.region), a.line_amount, b.refund_amount
            FROM ({LINES.format(where="")}) a
            FULL OUTER JOIN ({REFUNDS}) b ON a.region IS NOT DISTINCT FROM b.region""",
    ),
]


@pytest.fixture(scope="module")
def ex(clickhouse):
    clickhouse.command("CREATE DATABASE IF NOT EXISTS ossie_paths")
    for table, (columns, _) in COLUMNS.items():
        clickhouse.command(f"DROP TABLE IF EXISTS ossie_paths.{table}")
        clickhouse.command(
            f"CREATE TABLE ossie_paths.{table} ({columns}) ENGINE = MergeTree ORDER BY id"
        )
        clickhouse.command(f"INSERT INTO ossie_paths.{table} VALUES {ROWS[table]}")
    yield Executor(clickhouse, load_model(FIXTURE))
    clickhouse.command("DROP DATABASE ossie_paths")


@pytest.fixture(scope="module")
def paths_duck():
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
def test_paths_match_the_reference(ex, paths_duck, query, gold):
    got = ex.execute(query).rows
    assert sorted(got, key=_key) == sorted(paths_duck.sql(gold).fetchall(), key=_key)


def test_wrong_plans_differ_on_this_data(paths_duck):
    """The data tells the reference plan from the plausible wrong ones."""
    right = sorted(paths_duck.sql(CASES[0][1]).fetchall(), key=_key)
    wrong = [
        # Inner joins: the orphans at both steps are lost.
        """SELECT c.region, SUM(l.amount), COUNT(l.id) FROM lines l
            JOIN orders o ON l.order_id = o.id JOIN customers c ON o.customer_id = c.id
            GROUP BY c.region""",
        # From the dimension outward: NORTH appears and the orphans are lost.
        """SELECT c.region, SUM(l.amount), COUNT(l.id) FROM customers c
            LEFT JOIN orders o ON o.customer_id = c.id LEFT JOIN lines l ON l.order_id = o.id
            GROUP BY c.region""",
    ]
    for sql in wrong:
        assert sorted(paths_duck.sql(sql).fetchall(), key=_key) != right


def test_both_orphans_share_the_null_group(ex):
    rows = {r[0]: r[1:] for r in ex.execute(CASES[0][0]).rows}
    assert rows == {"EAST": (15, 3), "WEST": (4, 1), None: (8, 2)}

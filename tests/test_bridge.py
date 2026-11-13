"""A metric of the one side by a dimension of the many side, against ClickHouse,
compared with DuckDB running the reference evaluation strategy of
relational_semantics.md section 9 (docs/decisions/one-side-by-many-side.md).

The data is that of tests/test_semi_join.py in a database of its own, with one
more line: order 10 has two toy lines, so a sum of order totals over the joined
lines would count it twice. Customers are counted by the status of their orders
(ann has two paid orders, cid a paid and an open one, dee none, and two orders
have a NULL or a missing customer), and orders are summed and counted by the
category of their lines' items. orders is a ReplacingMergeTree whose order 12 was
paid and is now open, so a spine or a branch read without FINAL gives another
answer. Accounts are counted by the day of their logins, on a key of two columns.
"""

import copy

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query
from tests.test_semi_join import COLUMNS, ENGINES, FIXTURE, ROWS, UPDATE

DATABASE = "ossie_bridge"
TEXT = FIXTURE.read_text().replace("ossie_semi.", f"{DATABASE}.") + (
    "  - name: order_total\n"
    '    expression: {dialects: [{dialect: ANSI_SQL, expression: "SUM(orders.total)"}]}\n'
)
DATA = copy.deepcopy(ROWS)
DATA["lines"] += ", (109, 10, 1000, 4)"


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    path = tmp_path_factory.mktemp("bridge") / "bridge.yaml"
    path.write_text(TEXT)
    return load_model(path)


@pytest.fixture(scope="module")
def ex(clickhouse, model):
    clickhouse.command(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
    for table, columns in COLUMNS.items():
        clickhouse.command(f"DROP TABLE IF EXISTS {DATABASE}.{table}")
        defs = ", ".join(f"{name} {types[0]}" for name, types in columns)
        engine = ENGINES.get(table, "MergeTree ORDER BY id")
        clickhouse.command(f"CREATE TABLE {DATABASE}.{table} ({defs}) ENGINE = {engine}")
        clickhouse.command(f"SYSTEM STOP MERGES {DATABASE}.{table}")
        clickhouse.command(f"INSERT INTO {DATABASE}.{table} VALUES {DATA[table]}")
    clickhouse.command(f"INSERT INTO {DATABASE}.orders VALUES {UPDATE}")
    yield Executor(clickhouse, model)
    clickhouse.command(f"DROP DATABASE {DATABASE}")


@pytest.fixture(scope="module")
def ref():
    import duckdb

    con = duckdb.connect()
    for table, columns in COLUMNS.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(f'{n} {t[1]}' for n, t in columns)})")
        con.execute(f"INSERT INTO {table} VALUES {DATA[table]}")
    con.execute("DELETE FROM orders WHERE id = 12")
    con.execute(f"INSERT INTO orders VALUES {UPDATE}")
    yield con
    con.close()


def reference(joined: str, measures: list[tuple[str, str, str]]) -> str:
    """Section 9: for each group g of ``joined`` (columns g, and the key of each
    measure's home), each measure (home table, its key, aggregate over alias h)
    over the distinct home rows the group's rows derive from: each home row once,
    whatever number of the group's rows derive from it."""
    cols = [
        f"""(SELECT {agg} FROM {home} h WHERE EXISTS (
                SELECT 1 FROM joined j WHERE j.k{i} IS NOT NULL AND j.k{i} = {key}
                AND j.g IS NOT DISTINCT FROM s.g))"""
        for i, (home, key, agg) in enumerate(measures)
    ]
    return f"""WITH joined AS ({joined}) SELECT s.g, {", ".join(cols)}
        FROM (SELECT DISTINCT g FROM joined) s"""


BY_STATUS = """SELECT o.status AS g, c.id AS k0, o.id AS k1 FROM orders o
    LEFT JOIN customers c ON o.customer_id = c.id"""
LINES = """FROM lines l LEFT JOIN orders o ON l.order_id = o.id
    LEFT JOIN items i ON l.item_id = i.id LEFT JOIN customers c ON o.customer_id = c.id"""
CUSTOMERS = ("customers", "h.id", "COUNT(h.name)")
ORDERS = ("orders", "h.id", "COUNT(h.id)")
TOTAL = ("orders", "h.id", "SUM(h.total)")

CASES = [
    # ann's two paid orders count her once, cid counts under both statuses, dee
    # under none; orders 13 and 15 reference no customer.
    (
        Query(("customer_count",), ("orders.status",)),
        reference(BY_STATUS, [CUSTOMERS]),
    ),
    # A dimension of the filtered dataset makes it the root of the spine: customers
    # with an order of that status and a total above 0.
    (
        Query(("customer_count",), ("orders.status",), ("orders.total > 0",)),
        reference(f"{BY_STATUS} WHERE o.total > 0", [CUSTOMERS]),
    ),
    # Stitched with a metric of the root (b04): orders count as before.
    (
        Query(("customer_count", "order_count"), ("orders.status",)),
        reference(BY_STATUS, [CUSTOMERS, ORDERS]),
    ),
    # Order 10's two lines of item 1000 add its total once; order 12 counts at its
    # latest version, once.
    (
        Query(("order_total", "order_count"), ("lines.item_id",)),
        reference(f"SELECT l.item_id AS g, o.id AS k0, o.id AS k1 {LINES}", [TOTAL, ORDERS]),
    ),
    # Two steps from the many side to the home, lines to orders to customers, with
    # a dimension of a dataset the lines reach and the lines named by a filter.
    # Line 105's item does not exist, so ann is in the NULL group too.
    (
        Query(
            ("customer_count", "order_count"),
            ("items.category",),
            ("lines.order_id IS NOT NULL",),
        ),
        reference(
            f"SELECT i.category AS g, c.id AS k0, o.id AS k1 {LINES} WHERE l.order_id IS NOT NULL",
            [CUSTOMERS, ORDERS],
        ),
    ),
    # A row filter restricts the rows the groups are made of (b18): one line passes
    # it and makes the group, as one joined row would.
    (
        Query(("customer_count",), ("lines.item_id",), ("lines.qty > 1",)),
        reference(f"SELECT l.item_id AS g, c.id AS k0 {LINES} WHERE l.qty > 1", [CUSTOMERS]),
    ),
    # A filter on the home: order 12 was paid and is open now, so a spine read
    # without FINAL keeps it under item 1001.
    (
        Query(("order_count",), ("lines.item_id",), ("orders.status = 'paid'",)),
        reference(f"SELECT l.item_id AS g, o.id AS k0 {LINES} WHERE o.status = 'paid'", [ORDERS]),
    ),
    # A key of two columns; login 4's NULL references no account.
    (
        Query(("account_count",), ("logins.day",)),
        reference(
            """SELECT g.day AS g,
            CASE WHEN a.tenant IS NOT NULL THEN row(a.tenant, a.account) END AS k0
            FROM logins g
            LEFT JOIN accounts a ON g.tenant = a.tenant AND g.account = a.account""",
            [("accounts", "row(h.tenant, h.account)", "COUNT(h.plan)")],
        ),
    ),
]


def _key(row: tuple) -> tuple:
    return tuple((v is None, v if v is not None else 0) for v in row)


@pytest.mark.parametrize(("query", "gold"), CASES)
def test_one_side_by_the_many_side_matches_the_reference(ex, ref, query, gold):
    got = ex.execute(query).rows
    assert sorted(got, key=_key) == sorted(ref.sql(gold).fetchall(), key=_key)


def test_an_aggregate_filter_runs_on_the_counted_values(ex):
    """b20: a filter on the metric sees each customer once per category."""
    q = Query(
        ("customer_count",),
        ("items.category",),
        ("lines.order_id IS NOT NULL", "customer_count > 1"),
    )
    assert ex.execute(q).rows == [("books", 2), ("toys", 2)]


def test_a_group_without_a_home_row_takes_the_empty_value(ex):
    """Order 15's customer does not exist: its line makes a group with no customer,
    kept with a count of 0 and no total (relational_semantics.md 8.1)."""
    q = Query(("customer_count",), ("lines.id",), ("lines.order_id = 15",))
    assert ex.execute(q).rows == [(106, 0)]


def test_the_fact_between_two_dimensions_is_named(model):
    """Customers by category: neither reaches the other. Lines link them, and a
    filter on lines names that fact
    (docs/decisions/semi-joins.md#through-a-fact-that-links-them)."""
    planner = Planner(model)
    with pytest.raises(PlanError, match="not joined through many-to-one relationships"):
        planner.plan(Query(("customer_count",), ("items.category",)))
    named = Query(("customer_count",), ("items.category",), ("lines.order_id IS NOT NULL",))
    assert "__bridge" in planner.sql(named)


def test_a_metric_of_components_keeps_the_reachability_rule(tmp_path):
    """apache/ossie#653 section 5.3: each component's home must reach the dimension."""
    path = tmp_path / "components.yaml"
    path.write_text(
        TEXT
        + "  - name: total_per_customer\n"
        + "    expression: {dialects: [{dialect: ANSI_SQL, expression: "
        + '"SUM(orders.total) / COUNT(customers.name)"}]}\n'
    )
    with pytest.raises(PlanError, match="does not reach"):
        Planner(load_model(path)).plan(Query(("total_per_customer",), ("lines.qty",)))

"""Row filters on a dataset that reaches the root but that the root does not
reach, against ClickHouse, compared with DuckDB.

Orders, order lines and items are filtered for a count of customers; logins for
a count of accounts, joined on a key of two columns. Expected rows come from
DuckDB running EXISTS: a customer counts once when at least one of its orders
passes every filter on orders, and not at all without one (#246 §6.5,
filtering join; Semantic 2). orders is a ReplacingMergeTree with an order
updated after it was paid, so a subquery read without FINAL counts it."""

import copy
from pathlib import Path

import clickhouse_connect
import pytest
from ossie import OssieDocument

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import Catalog, PlanError, Planner, Query, TableInfo

FIXTURE = Path(__file__).parent / "fixtures" / "semi_join.yaml"
MODEL = load_model(FIXTURE)
ROWS = {
    # dee has no orders; nobody has a NULL id, which only order 13 could match.
    "customers": "(1, 'ann', 'EAST'), (2, 'bob', 'EAST'), (3, 'cid', 'WEST'), "
    "(4, 'dee', 'NORTH'), (NULL, 'nobody', 'NOWHERE')",
    # ann has two paid orders; cid has a paid one of 0 and an open one of 8, so two
    # filters passed by different orders must not count cid. Order 15 is an orphan.
    "orders": "(10, 1, 'paid', 5, 1), (11, 1, 'paid', 7, 1), (12, 2, 'paid', 3, 1), "
    "(13, NULL, 'paid', 9, 1), (14, 3, 'paid', 0, 1), (15, 99, 'paid', 4, 1), "
    "(16, 3, 'open', 8, 1)",
    # Line 103 is on an order that does not exist, line 104 on none.
    "lines": "(100, 10, 1000, 2), (101, 12, 1001, 1), (102, 16, 1000, 5), "
    "(103, 77, 1000, 1), (104, NULL, 1000, 1)",
    "items": "(1000, 'toys'), (1001, 'books')",
    "accounts": "(1, 1, 'a'), (1, 2, 'b'), (2, 1, 'c'), (1, NULL, 'z')",
    # Login 4 has a NULL in one column of the key: it references no account.
    "logins": "(1, 1, 1, 'd1'), (2, 1, 1, 'd1'), (3, 2, 1, 'd2'), (4, 1, NULL, 'd1'), "
    "(5, 3, 3, 'd1')",
}
# Inserted after ROWS into ClickHouse, replacing order 12 (bob's) with an open
# one; DuckDB gets only the latest version.
UPDATE = "(12, 2, 'open', 3, 2)"
NULLABLE_INT = ("Nullable(Int32)", "INTEGER")
COLUMNS = {
    "customers": (
        ("id", NULLABLE_INT),
        ("name", ("String", "VARCHAR")),
        ("region", ("String", "VARCHAR")),
    ),
    "orders": (
        ("id", ("Int32", "INTEGER")),
        ("customer_id", NULLABLE_INT),
        ("status", ("String", "VARCHAR")),
        ("total", ("Int32", "INTEGER")),
        ("version", ("UInt32", "INTEGER")),
    ),
    "lines": (
        ("id", ("Int32", "INTEGER")),
        ("order_id", NULLABLE_INT),
        ("item_id", ("Int32", "INTEGER")),
        ("qty", ("Int32", "INTEGER")),
    ),
    "items": (("id", ("Int32", "INTEGER")), ("category", ("String", "VARCHAR"))),
    "accounts": (
        ("tenant", ("Int32", "INTEGER")),
        ("account", NULLABLE_INT),
        ("plan", ("String", "VARCHAR")),
    ),
    "logins": (
        ("id", ("Int32", "INTEGER")),
        ("tenant", ("Int32", "INTEGER")),
        ("account", NULLABLE_INT),
        ("day", ("String", "VARCHAR")),
    ),
}
ENGINES = {
    "customers": "MergeTree ORDER BY tuple()",
    "orders": "ReplacingMergeTree(version) ORDER BY id",
    "accounts": "MergeTree ORDER BY tenant",
}


def _customers(where: str) -> str:
    return f"""SELECT c.region, COUNT(c.name) AS customer_count FROM customers c
        WHERE EXISTS (SELECT 1 FROM {where}) GROUP BY c.region"""


ORDERS = "orders o WHERE o.customer_id = c.id AND"
LINES = (
    "lines l LEFT JOIN orders o ON l.order_id = o.id LEFT JOIN items i ON l.item_id = i.id "
    "WHERE o.customer_id = c.id AND"
)

CASES = [
    # ann's two paid orders count her once; dee, without orders, and nobody, whose
    # NULL id only a NULL customer_id would match, are absent.
    (
        Query(("customer_count",), ("customers.region",), ("orders.status = 'paid'",)),
        _customers(f"{ORDERS} o.status = 'paid'"),
    ),
    # Two filters on orders: one order passes both, as a joined row would.
    (
        Query(
            ("customer_count",),
            ("customers.region",),
            ("orders.status = 'paid'", "orders.total > 4"),
        ),
        _customers(f"{ORDERS} o.status = 'paid' AND o.total > 4"),
    ),
    # NOT stays inside: customers with an order that is not paid.
    (
        Query(("customer_count",), ("customers.region",), ("NOT orders.status = 'paid'",)),
        _customers(f"{ORDERS} NOT o.status = 'paid'"),
    ),
    # Two steps, lines to orders to customers.
    (
        Query(("customer_count",), ("customers.region",), ("lines.qty >= 1",)),
        _customers(f"{LINES} l.qty >= 1"),
    ),
    # A filter on orders next to one on lines: one line and its order pass both.
    (
        Query(
            ("customer_count",),
            ("customers.region",),
            ("lines.qty > 1", "orders.status = 'paid'"),
        ),
        _customers(f"{LINES} l.qty > 1 AND o.status = 'paid'"),
    ),
    # items, which lines reach, in the same subquery as lines.
    (
        Query(
            ("customer_count",),
            ("customers.region",),
            ("lines.qty > 0", "items.category = 'books'"),
        ),
        _customers(f"{LINES} l.qty > 0 AND i.category = 'books'"),
    ),
    # A filter the root joins to stays a plain WHERE next to the semi-join.
    (
        Query(("customer_count",), (), ("orders.status = 'paid'", "customers.region = 'EAST'")),
        "SELECT COUNT(c.name) FROM customers c WHERE c.region = 'EAST' AND EXISTS "
        f"(SELECT 1 FROM {ORDERS} o.status = 'paid')",
    ),
    # A key of two columns; login 4's NULL references no account, not even (1, NULL).
    (
        Query(("account_count",), (), ("logins.day = 'd1'",)),
        "SELECT COUNT(a.plan) FROM accounts a WHERE EXISTS (SELECT 1 FROM logins g "
        "WHERE g.tenant = a.tenant AND g.account = a.account AND g.day = 'd1')",
    ),
    # Stitched: customers through the semi-join, orders with a plain WHERE, merged
    # by region with NULL-safe equality (§6.6 row 3).
    (
        Query(
            ("customer_count", "order_count"), ("customers.region",), ("orders.status = 'paid'",)
        ),
        f"""SELECT COALESCE(a.region, b.region), COALESCE(a.customer_count, 0),
                COALESCE(b.order_count, 0)
            FROM ({_customers(f"{ORDERS} o.status = 'paid'")}) a
            FULL OUTER JOIN (SELECT c.region, COUNT(o.id) AS order_count FROM orders o
                LEFT JOIN customers c ON o.customer_id = c.id WHERE o.status = 'paid'
                GROUP BY c.region) b ON a.region IS NOT DISTINCT FROM b.region""",
    ),
]


@pytest.fixture(scope="module")
def ex(clickhouse):
    clickhouse.command("CREATE DATABASE IF NOT EXISTS ossie_semi")
    for table, columns in COLUMNS.items():
        clickhouse.command(f"DROP TABLE IF EXISTS ossie_semi.{table}")
        defs = ", ".join(f"{name} {types[0]}" for name, types in columns)
        engine = ENGINES.get(table, "MergeTree ORDER BY id")
        clickhouse.command(f"CREATE TABLE ossie_semi.{table} ({defs}) ENGINE = {engine}")
        # A merge would drop the paid version of order 12 that FINAL must hide.
        clickhouse.command(f"SYSTEM STOP MERGES ossie_semi.{table}")
        clickhouse.command(f"INSERT INTO ossie_semi.{table} VALUES {ROWS[table]}")
    clickhouse.command(f"INSERT INTO ossie_semi.orders VALUES {UPDATE}")
    yield Executor(clickhouse, MODEL)
    clickhouse.command("DROP DATABASE ossie_semi")


@pytest.fixture(scope="module")
def semi_duck():
    """Its own DuckDB connection: the shared one has other tables named alike."""
    import duckdb

    con = duckdb.connect()
    for table, columns in COLUMNS.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(f'{n} {t[1]}' for n, t in columns)})")
        con.execute(f"INSERT INTO {table} VALUES {ROWS[table]}")
    con.execute("DELETE FROM orders WHERE id = 12")
    con.execute(f"INSERT INTO orders VALUES {UPDATE}")
    yield con
    con.close()


def _key(row: tuple) -> tuple:
    return tuple((v is None, v if v is not None else 0) for v in row)


@pytest.mark.parametrize(("query", "gold"), CASES)
def test_semi_joins_match_the_reference(ex, semi_duck, query, gold):
    got = ex.execute(query).rows
    assert sorted(got, key=_key) == sorted(semi_duck.sql(gold).fetchall(), key=_key)


def test_the_data_tells_the_semi_join_from_wrong_plans(semi_duck):
    """Each plausible wrong plan gives another answer on this data."""
    paid = sorted(semi_duck.sql(CASES[0][1]).fetchall(), key=_key)
    assert paid == [("EAST", 1), ("WEST", 1)]
    # Joining orders counts ann once per paid order.
    joined = """SELECT c.region, COUNT(c.name) FROM orders o JOIN customers c
        ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.region"""
    assert sorted(semi_duck.sql(joined).fetchall(), key=_key) != paid
    # One semi-join per filter counts cid, whose paid order is of 0.
    both = sorted(semi_duck.sql(CASES[1][1]).fetchall(), key=_key)
    apart = f"""SELECT c.region, COUNT(c.name) FROM customers c
        WHERE EXISTS (SELECT 1 FROM {ORDERS} o.status = 'paid')
        AND EXISTS (SELECT 1 FROM {ORDERS} o.total > 4) GROUP BY c.region"""
    assert sorted(semi_duck.sql(apart).fetchall(), key=_key) != both


def test_order_read_without_final_would_count_bob(ex, clickhouse):
    result = ex.execute(CASES[0][0])
    assert dict(result.rows) == {"EAST": 1, "WEST": 1}
    assert "FROM ossie_semi.orders AS orders FINAL WHERE" in result.sql
    plain = clickhouse.query(
        "SELECT COUNT() FROM ossie_semi.customers WHERE id IN "
        "(SELECT customer_id FROM ossie_semi.orders WHERE status = 'paid' AND id = 12)"
    )
    assert plain.result_rows == [(1,)]  # the paid version is still stored


@pytest.mark.parametrize("transform_null_in", [0, 1])
def test_a_null_key_references_nothing_only_with_transform_null_in_off(
    clickhouse, transform_null_in
):
    """Why SETTINGS carries transform_null_in = 0: with 1, NULL IN (NULL) is true."""
    sql = (
        "SELECT CAST(NULL, 'Nullable(Int32)') IN (SELECT CAST(NULL, 'Nullable(Int32)')), "
        "(1, CAST(NULL, 'Nullable(Int32)')) IN (SELECT 1, CAST(NULL, 'Nullable(Int32)')) "
        f"SETTINGS transform_null_in = {transform_null_in}"
    )
    got = clickhouse.query(sql).result_rows[0]
    assert got == ((None, 0) if transform_null_in == 0 else (1, 1))


def test_the_comment_questions_have_sql():
    """The two questions of Dactopus/dactopus-ossie-clickhouse#10, in this model's terms."""
    sql = Planner(MODEL).sql(Query(("customer_count",), (), ("orders.total > 0",)))
    assert sql == (
        "SELECT COUNT(customers.name) AS customer_count FROM ossie_semi.customers AS customers "
        "WHERE customers.id IN (SELECT orders.customer_id FROM ossie_semi.orders AS orders "
        "WHERE orders.total > 0) "
        "SETTINGS join_use_nulls = 1, aggregate_functions_null_for_empty = 1, transform_null_in = 0"
    )


def test_two_steps_nest_one_in_per_relationship():
    sql = Planner(MODEL).sql(Query(("customer_count",), (), ("lines.qty > 1",)))
    assert (
        "WHERE customers.id IN (SELECT orders.customer_id FROM ossie_semi.orders AS orders "
        "WHERE orders.id IN (SELECT lines.order_id FROM ossie_semi.lines AS lines "
        "WHERE lines.qty > 1))"
    ) in sql


def test_a_key_of_two_columns_is_a_tuple():
    sql = Planner(MODEL).sql(Query(("account_count",), (), ("logins.day = 'd1'",)))
    assert (
        "WHERE (accounts.tenant, accounts.account) IN "
        "(SELECT logins.tenant, logins.account FROM ossie_semi.logins AS logins "
        "WHERE logins.day = 'd1')"
    ) in sql


def test_subquery_reads_final_and_its_own_dictionaries():
    """The subquery is a question over its dataset: FINAL on a deduplicated table,
    dictGetOrNull on a dictionary it joins, keyed by its own column."""
    catalog = Catalog(
        {
            "orders": TableInfo("ReplacingMergeTree", frozenset(), dedup=True),
            "items": TableInfo("Dictionary", frozenset(), dictionary_key="id"),
        }
    )
    sql = Planner(MODEL, catalog).sql(
        Query(("customer_count",), (), ("lines.qty > 0", "items.category = 'books'"))
    )
    assert "FROM ossie_semi.orders AS orders FINAL WHERE orders.id IN" in sql
    assert (
        "FROM ossie_semi.lines AS lines WHERE lines.qty > 0 AND "
        "dictGetOrNull('ossie_semi.items', 'category', lines.item_id) = 'books'"
    ) in sql


def test_a_metric_filtered_on_the_other_dataset_stays_over_its_rows():
    """FILTER (WHERE orders.x) in a metric makes orders one of its datasets, so
    the question is answered over orders rows as before, not with a semi-join."""
    sql = Planner(MODEL).sql(Query(("paid_customers",), (), ("orders.total > 0",)))
    assert "FROM ossie_semi.orders AS orders LEFT JOIN ossie_semi.customers" in sql
    assert " IN (" not in sql


def test_the_same_question_gives_the_same_sql():
    q = Query(("customer_count", "order_count"), ("customers.region",), ("lines.qty > 1",))
    assert Planner(MODEL).sql(q) == Planner(load_model(FIXTURE)).sql(q)


def _variant(*relationships: dict, metrics: dict | None = None) -> Planner:
    m = copy.deepcopy(MODEL.model_dump(by_alias=True))
    m["relationships"] += list(relationships)
    for name, e in (metrics or {}).items():
        m["metrics"].append(
            {"name": name, "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": e}]}}
        )
    return Planner(OssieDocument.model_validate(m))


@pytest.mark.parametrize(
    ("planner", "query", "message"),
    [
        # One expression over the root and the filtered dataset.
        (
            Planner(MODEL),
            Query(
                ("customer_count",), (), ("orders.status = 'paid' OR customers.region = 'EAST'",)
            ),
            "filter \"orders.status = 'paid' OR customers.region = 'EAST'\" reads customers "
            "and orders in one expression, but customers is read with each customers row "
            "and orders only searched for a row that matches. Split it into one filter per "
            "side; an OR across the sides cannot be split, and this version cannot answer it",
        ),
        # items does not reach customers; lines, which links them, is not named.
        (
            Planner(MODEL),
            Query(("customer_count",), (), ("items.category = 'books'",)),
            "filter \"items.category = 'books'\" reads items, which customers does not join "
            "to and which does not reach customers through many-to-one relationships; name "
            "the dataset that links them (lines) in a filter",
        ),
        # A dimension of the filtered dataset still makes it the root.
        (
            Planner(MODEL),
            Query(("customer_count",), ("orders.status",), ("orders.total > 0",)),
            "metric 'customer_count' aggregates customers rows, but this question is "
            "answered over orders rows (because of dimension 'orders.status', filter "
            "'orders.total > 0')",
        ),
        # Two paths from lines to customers (#246 §6.9).
        (
            _variant(
                {
                    "name": "lines_to_customer",
                    "from": "lines",
                    "to": "customers",
                    "from_columns": ["order_id"],
                    "to_columns": ["id"],
                }
            ),
            Query(("customer_count",), (), ("lines.qty > 1",)),
            "ambiguous join from 'lines' to 'customers': relationship paths "
            "'lines_to_customer' and 'lines_to_order' -> 'orders_to_customer'",
        ),
        # orders reaches customers, and customers reaches orders.
        (
            _variant(
                {
                    "name": "customer_to_last_order",
                    "from": "customers",
                    "to": "orders",
                    "from_columns": ["id"],
                    "to_columns": ["id"],
                }
            ),
            Query(("customer_count",), (), ("orders.status = 'paid'",)),
            "ambiguous root dataset among ['customers', 'orders']",
        ),
    ],
)
def test_refusals(planner, query, message):
    with pytest.raises(PlanError) as e:
        planner.sql(query)
    assert message in str(e.value)


WEB = load_model(Path(__file__).parent / "fixtures" / "web_analytics.yaml")
SESSIONS_WITH_REVENUE = Query(("sessions",), (), ("purchases.revenue > 0",))


def test_the_question_of_issue_10_has_sql():
    """Sessions with a purchase of a value, refused until #10."""
    assert Planner(WEB).sql(SESSIONS_WITH_REVENUE) == (
        "SELECT COUNT(sessions.session_key) AS sessions FROM sessions AS sessions "
        "WHERE sessions.session_key IN (SELECT purchases.session_key FROM purchases AS "
        "purchases WHERE purchases.revenue > 0) "
        "SETTINGS join_use_nulls = 1, aggregate_functions_null_for_empty = 1, transform_null_in = 0"
    )


def test_the_question_of_issue_10_on_the_ga4_sample(clickhouse):
    """As many sessions as a join counts with uniqExact; fewer than has_purchase,
    which counts purchases without a revenue too."""
    if not clickhouse.query("EXISTS TABLE dactopus.purchases").result_rows[0][0]:
        pytest.skip("GA4 sample not loaded into the database dactopus (see dactopus-data-models)")
    client = clickhouse_connect.get_client(dsn=clickhouse.url, database="dactopus")
    [[sessions]] = Executor(client, WEB).execute(SESSIONS_WITH_REVENUE).rows
    [[joined]] = client.query(
        "SELECT uniqExact(s.session_key) FROM sessions s INNER JOIN purchases p "
        "ON p.session_key = s.session_key WHERE p.revenue > 0"
    ).result_rows
    [[flagged]] = client.query("SELECT countIf(has_purchase = 1) FROM sessions").result_rows
    assert sessions == joined == 4446 < flagged

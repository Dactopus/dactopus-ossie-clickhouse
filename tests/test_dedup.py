"""A key repeated on the one side of a relationship (docs/design.md, "Many-to-one
inferred from keys").

ClickHouse does not enforce the uniqueness of a declared key, so a customer can be
stored twice. Layer 3 (apache/ossie#653, wide_table_semantics.md section 4) answers
such a question as Layer 2 does (relational_semantics.md section 8.1): the root rows
are unique by their key, and the input of an aggregate in a group is the DISTINCT of
the root's key, the aggregate's arguments and the dimensions, after the join and the
filter, so a root row counts once in each group it matches. DuckDB runs that plan
(``layer2``) for the expected values; the spec's Appendix B.1 is the data, with C1
stored twice (both EAST) and C3 twice (WEST and SOUTH), and an order O5, for the
customer C9 that does not exist, of 40.

The numbers of the acceptance list are written out in the tests as well, so a
change of the reference shows."""

import pytest
import yaml
from sqlglot import exp

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import (
    Catalog,
    Planner,
    Query,
    TableInfo,
    unanswerable_metrics,
)
from tests.conftest import dictionary_credentials

DB = "ossie_dedup"


def f(name: str, expression: str | None = None) -> dict:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression or name}]},
    }


def dataset(name: str, *fields: dict) -> dict:
    return {"name": name, "source": f"{DB}.{name}", "primary_key": ["id"], "fields": list(fields)}


def metric(name: str, expression: str) -> dict:
    return {"name": name, "expression": f(name, expression)["expression"]}


def relationship(frm: str, to: str, column: str, target: str = "id") -> dict:
    return {
        "name": f"{frm}_to_{to}",
        "from": frm,
        "to": to,
        "from_columns": [column],
        "to_columns": [target],
    }


MODEL = {
    "version": "0.3.0.dev0",
    "name": "dedup",
    "datasets": [
        dataset(
            "customers",
            f("id"),
            f("region"),
            f("weight"),
            f("region_or_unknown", "COALESCE(region, 'unknown')"),
            f("is_vip", "has(tags, 'vip')"),
        ),
        dataset("channels", f("id"), f("name")),
        dataset("channel_dict", f("id"), f("name")),
        dataset("orders", f("id"), f("customer_id"), f("channel_id"), f("amount")),
        dataset("returns", f("id"), f("customer_id"), f("amount")),
        dataset("regions", f("id"), f("name")),
        dataset("region_dict", f("id"), f("name")),
        dataset("people", f("id"), f("region_id")),
        dataset("sales", f("id"), f("person_id"), f("amount")),
    ],
    "relationships": [
        relationship("orders", "customers", "customer_id"),
        relationship("orders", "channels", "channel_id"),
        relationship("orders", "channel_dict", "channel_id"),
        relationship("returns", "customers", "customer_id"),
        relationship("sales", "people", "person_id"),
        relationship("people", "regions", "region_id"),
        relationship("people", "region_dict", "region_id"),
    ],
    "metrics": [
        metric("total_revenue", "SUM(orders.amount)"),
        metric("total_returns", "SUM(returns.amount)"),
        metric("revenue_per_customer", "SUM(orders.amount) / COUNT(DISTINCT customers.id)"),
        metric("east_revenue", "SUM(orders.amount) FILTER (WHERE customers.region = 'EAST')"),
        metric("west_revenue", "SUM(orders.amount) FILTER (WHERE customers.region = 'WEST')"),
        metric(
            "web_share",
            "SUM(orders.amount) FILTER (WHERE channel_dict.name = 'web') / SUM(orders.amount)",
        ),
        metric(
            "web_rank",
            "RANK() OVER (ORDER BY SUM(orders.amount) FILTER (WHERE channel_dict.name = 'web') "
            "/ SUM(orders.amount))",
        ),
        metric("west_orders", "COUNT(orders.id) FILTER (WHERE customers.region = 'WEST')"),
        metric(
            "west_share",
            "SUM(orders.amount) FILTER (WHERE customers.region = 'WEST') / SUM(orders.amount)",
        ),
        metric("max_order", "MAX(orders.amount)"),
        metric("running", "SUM(SUM(orders.amount)) OVER (ORDER BY LOWER(customers.region))"),
        metric("order_count", "COUNT(DISTINCT orders.id)"),
        metric("sales_total", "SUM(sales.amount)"),
        metric("weighted_by_channel", "SUM(orders.amount * channels.id)"),
        metric("weighted_by_customer", "SUM(orders.amount * customers.weight)"),
    ],
}

# Appendix B.1 with C1 and C3 stored twice. The weights tell the copies of C1 apart.
DATA = {
    "customers": (
        "id Int32, region Nullable(String), weight Int32, tags Array(String)",
        "(1, 'EAST', 1, []), (1, 'EAST', -1, []), (2, 'EAST', 1, []), (3, 'WEST', 1, []), "
        "(3, 'SOUTH', 1, []), (4, 'NORTH', 1, ['vip'])",
    ),
    "channels": ("id Int32, name String", "(1, 'web'), (1, 'web'), (2, 'shop')"),
    "channel_dict_source": ("id Int32, name String", "(1, 'web'), (2, 'shop')"),
    "orders": (
        "id Int32, customer_id Int32, channel_id Int32, amount Decimal(10, 2)",
        "(1, 1, 1, 100), (2, 1, 2, 200), (3, 2, 1, 50), (4, 3, 1, 300), (5, 9, 2, 40)",
    ),
    "returns": ("id Int32, customer_id Int32, amount Decimal(10, 2)", "(1, 1, 25), (2, 4, 10)"),
    # Region 10 is stored twice, and customer 1 has two region ids that lead to EAST.
    "regions": ("id Int32, name String", "(10, 'EAST'), (10, 'EAST'), (11, 'EAST'), (12, 'WEST')"),
    "region_dict_source": ("id Int32, name String", "(10, 'EAST'), (11, 'EAST'), (12, 'WEST')"),
    "people": ("id Int32, region_id Int32", "(1, 10), (1, 11), (2, 10), (3, 12)"),
    "sales": (
        "id Int32, person_id Int32, amount Decimal(10, 2)",
        "(1, 1, 100), (2, 1, 200), (3, 2, 50), (4, 3, 300)",
    ),
}
CUSTOMERS = (
    "(1, 'EAST', 1), (1, 'EAST', -1), (2, 'EAST', 1), (3, 'WEST', 1), (3, 'SOUTH', 1), "
    "(4, 'NORTH', 1)"
)
DUCK = {  # the same rows for DuckDB; customers without the array
    "customers": "id INTEGER, region VARCHAR, weight INTEGER",
    "channels": "id INTEGER, name VARCHAR",
    "orders": "id INTEGER, customer_id INTEGER, channel_id INTEGER, amount DECIMAL(10, 2)",
    "returns": "id INTEGER, customer_id INTEGER, amount DECIMAL(10, 2)",
    "regions": "id INTEGER, name VARCHAR",
    "people": "id INTEGER, region_id INTEGER",
    "sales": "id INTEGER, person_id INTEGER, amount DECIMAL(10, 2)",
}


@pytest.fixture(scope="module")
def ex(clickhouse, tmp_path_factory):
    clickhouse.command(f"CREATE DATABASE IF NOT EXISTS {DB}")
    for name in ("channel_dict", "region_dict"):
        clickhouse.command(f"DROP DICTIONARY IF EXISTS {DB}.{name}")
    for table, (columns, rows) in DATA.items():
        clickhouse.command(f"DROP TABLE IF EXISTS {DB}.{table}")
        clickhouse.command(f"CREATE TABLE {DB}.{table} ({columns}) ENGINE = MergeTree ORDER BY id")
        clickhouse.command(f"INSERT INTO {DB}.{table} VALUES {rows}")
    for name in ("channel_dict", "region_dict"):
        clickhouse.command(
            f"CREATE DICTIONARY {DB}.{name} (id Int32, name String) PRIMARY KEY id "
            f"SOURCE(CLICKHOUSE(TABLE '{name}_source' DB '{DB}' {dictionary_credentials()})) "
            "LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(0)"
        )
    path = tmp_path_factory.mktemp("dedup") / "dedup.yaml"
    path.write_text(yaml.safe_dump(MODEL, sort_keys=False))
    yield Executor(clickhouse, load_model(path))
    clickhouse.command(f"DROP DATABASE {DB}")


@pytest.fixture(scope="module")
def duck_db():
    import duckdb

    con = duckdb.connect()
    for table, columns in DUCK.items():
        con.execute(f"CREATE TABLE {table} ({columns})")
        rows = CUSTOMERS if table == "customers" else DATA[table][1]
        con.execute(f"INSERT INTO {table} VALUES {rows}")
    yield con
    con.close()


def layer2(
    con, root: str, args: str, dims: dict[str, str], joins: str, where: str = "TRUE", fn="SUM"
):
    """The plan of Layer 2: DISTINCT of the root's key, the argument and the dimensions
    over the join, after the filter; then the aggregate for each group.
    ``dims``: name -> expression."""
    cols = "".join(f", {e} AS {n}" for n, e in dims.items())
    names = ", ".join(dims)
    group = f"GROUP BY {names}" if dims else ""
    lead = f"{names}, " if dims else ""
    sql = (
        f"SELECT {lead}{fn}(a) FROM (SELECT DISTINCT {root}.id, {args} AS a{cols} "
        f"FROM {joins} WHERE {where}) {group}"
    )
    return con.sql(sql).fetchall()


ORDERS_JOIN = (
    "orders o LEFT JOIN customers c ON o.customer_id = c.id "
    "LEFT JOIN channels h ON o.channel_id = h.id"
)


def _key(row: tuple) -> tuple:
    return tuple((v is None, str(v)) for v in row[:-1])


def same(got: list[tuple], gold: list[tuple]) -> None:
    """Rows equal as sets, the last column a number or NULL."""

    def norm(rows):
        return sorted(((*r[:-1], None if r[-1] is None else float(r[-1])) for r in rows), key=_key)

    got, gold = norm(got), norm(gold)
    assert len(got) == len(gold), (got, gold)
    for g, e in zip(got, gold, strict=True):
        assert g[:-1] == e[:-1], (got, gold)
        assert (g[-1] is None and e[-1] is None) or g[-1] == pytest.approx(e[-1]), (got, gold)


def test_the_data_tells_a_plain_join_apart(duck_db):
    """A LEFT JOIN from orders counts the orders of C1 and C3 twice: 650 for EAST."""
    rows = duck_db.sql(
        "SELECT c.region, SUM(o.amount) FROM orders o LEFT JOIN customers c "
        "ON o.customer_id = c.id GROUP BY c.region"
    ).fetchall()
    assert dict(rows)["EAST"] == 650


# --- the acceptance list ---------------------------------------------------------


def test_1_a_root_row_counts_once_in_each_group_it_matches(ex, duck_db):
    got = ex.execute(Query(("total_revenue",), ("customers.region",)))
    assert {r[0]: float(r[1]) for r in got.rows} == {
        "EAST": 350,
        "WEST": 300,
        "SOUTH": 300,
        None: 40,
    }
    gold = layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN)
    same(got.rows, gold)
    assert "SELECT DISTINCT" in got.sql  # the customers are read once for each distinct value


def test_2_a_filter_on_the_one_side_counts_a_root_row_once(ex, duck_db):
    got = ex.execute(Query(("total_revenue",), (), ("customers.region = 'EAST'",)))
    assert [float(r[0]) for r in got.rows] == [350]
    gold = layer2(duck_db, "o", "o.amount", {}, ORDERS_JOIN, "c.region = 'EAST'")
    assert float(gold[0][0]) == 350


def test_3_two_facts_by_region(ex, duck_db):
    got = ex.execute(Query(("total_revenue", "total_returns"), ("customers.region",))).rows
    assert [float(v) for v in next(r for r in got if r[0] == "EAST")[1:]] == [350, 25]
    # Each fact on its own through Layer 2, merged on the region (section 5.5).
    revenue = dict(layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN))
    returns = dict(
        layer2(
            duck_db,
            "x",
            "x.amount",
            {"region": "c.region"},
            "returns x LEFT JOIN customers c ON x.customer_id = c.id",
        )
    )

    def number(v):
        return None if v is None else float(v)

    assert sorted(
        ((r[0], number(r[1]), number(r[2])) for r in got), key=lambda r: (r[0] is None, str(r[0]))
    ) == sorted(
        (
            (region, number(revenue.get(region)), number(returns.get(region)))
            for region in set(revenue) | set(returns)
        ),
        key=lambda r: (r[0] is None, str(r[0])),
    )


def test_4_a_metric_over_two_facts(ex, duck_db):
    got = ex.execute(Query(("revenue_per_customer",), ("customers.region",))).rows
    rows = {r[0]: r[1] for r in got}
    assert rows["EAST"] == pytest.approx(175)
    revenue = dict(layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN))
    counts = dict(
        duck_db.sql("SELECT region, COUNT(DISTINCT id) FROM customers GROUP BY region").fetchall()
    )
    expected = {}
    for region in set(revenue) | set(counts):
        n = counts.get(region, 0)
        expected[region] = (
            None if not n or revenue.get(region) is None else float(revenue[region]) / n
        )
    assert set(rows) == set(expected)
    for region, value in expected.items():
        assert rows[region] == pytest.approx(value) if value is not None else rows[region] is None
    total = ex.execute(Query(("revenue_per_customer",))).rows
    assert total[0][0] == pytest.approx(172.5)  # 690 / 4 customers


def test_5_a_root_row_that_matches_nothing_is_read_as_before(ex, duck_db):
    # O5 has no customer: coalesce reads NULL there, and a filter on NULL can keep it.
    got = ex.execute(Query(("total_revenue",), ("customers.region_or_unknown",))).rows
    assert {r[0]: float(r[1]) for r in got} == {
        "EAST": 350,
        "WEST": 300,
        "SOUTH": 300,
        "unknown": 40,
    }
    gold = layer2(
        duck_db, "o", "o.amount", {"region": "COALESCE(c.region, 'unknown')"}, ORDERS_JOIN
    )
    same(got, gold)
    kept = ex.execute(Query(("total_revenue",), (), ("customers.region IS NULL",))).rows
    assert [float(r[0]) for r in kept] == [40]
    gold = layer2(duck_db, "o", "o.amount", {}, ORDERS_JOIN, "c.region IS NULL")
    assert float(gold[0][0]) == 40


def test_5_the_unmatched_value_has_the_type_of_the_column(ex):
    """has(NULL, 'vip') is refused, so the NULL of an array column is the array
    ClickHouse gives a row that matched nothing."""
    got = ex.execute(Query(("total_revenue",), ("customers.is_vip",))).rows
    assert {r[0]: float(r[1]) for r in got} == {0: 690}  # O5 too


def test_a_condition_on_a_dimension_after_grouping(ex, duck_db):
    """HAVING reads grouped rows: the dimension is the GROUP BY expression, not a condition
    carried like a WHERE one (ClickHouse refuses the second, error 215)."""
    q = Query(
        ("total_revenue",),
        ("customers.region",),
        ("total_revenue > 100 AND UPPER(customers.region) <> 'WEST'",),
    )
    got = ex.execute(q)
    gold = layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN)
    kept = [r for r in gold if r[1] > 100 and r[0] is not None and r[0].upper() != "WEST"]
    assert {r[0] for r in kept} == {"EAST", "SOUTH"}
    same(got.rows, kept)


def test_a_window_over_a_function_of_a_dimension(ex, duck_db):
    """The window's ORDER BY applies a function to the dimension, over grouped rows."""
    got = ex.execute(Query(("running",), ("customers.region",)))
    gold = sorted(
        layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN),
        key=lambda r: (r[0] is None, str(r[0]).lower()),
    )
    total, expected = 0, []
    for region, revenue in gold:  # NULL is the largest value: last (section 5.2)
        total += revenue
        expected.append((region, total))
    same(got.rows, expected)


def test_6_copies_that_differ_only_in_a_filter_column(ex, duck_db):
    q = Query(("total_revenue",), ("customers.region",), ("customers.weight > 0",))
    got = ex.execute(q)
    rows = {r[0]: float(r[1]) for r in got.rows}
    assert rows == {"EAST": 350, "WEST": 300, "SOUTH": 300}  # C1's two copies: O1, O2 once
    gold = layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN, "c.weight > 0")
    same(got.rows, gold)


def test_6_both_copies_pass_the_filter_with_different_values(ex, duck_db):
    """C1's weights are 1 and -1 and both pass weight <> 0: the filter is carried as one
    value, so the copies collapse. A column carried raw would keep both and count O1 and
    O2 twice."""
    q = Query(("total_revenue",), ("customers.region",), ("customers.weight <> 0",))
    got = ex.execute(q)
    assert {r[0]: float(r[1]) for r in got.rows} == {"EAST": 350, "WEST": 300, "SOUTH": 300}
    gold = layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN, "c.weight <> 0")
    same(got.rows, gold)


def test_7_a_key_repeated_on_the_second_step(ex, duck_db):
    """The region 10 is stored twice, and the person 1 has two region ids that lead to
    EAST: a plain join counts the first sale three times."""
    got = ex.execute(Query(("sales_total",), ("regions.name",)))
    assert {r[0]: float(r[1]) for r in got.rows} == {"EAST": 350, "WEST": 300}
    gold = layer2(
        duck_db,
        "s",
        "s.amount",
        {"name": "r.name"},
        "sales s LEFT JOIN people p ON s.person_id = p.id "
        "LEFT JOIN regions r ON p.region_id = r.id",
    )
    same(got.rows, gold)
    assert got.sql.count("SELECT DISTINCT") == 1  # the chain is one derived table


@pytest.mark.parametrize(
    "q",
    [
        Query(("max_order",), ("customers.region",)),
        Query(("order_count",), ("customers.region",), ("customers.weight > 0",)),
        Query((), ("orders.id", "customers.region"), ("customers.weight > 0",)),
    ],
)
def test_8_repeat_safe_aggregates_and_questions_without_metrics_keep_their_sql(ex, q):
    sql = ex.execute(q).sql
    assert "SELECT DISTINCT" not in sql
    assert f"LEFT JOIN {DB}.customers AS customers" in sql


def test_8_values_of_repeat_safe_aggregates(ex, duck_db):
    got = ex.execute(Query(("max_order", "order_count"), ("customers.region",)))
    gold = duck_db.sql(
        "SELECT c.region, MAX(o.amount), COUNT(DISTINCT o.id) FROM orders o "
        "LEFT JOIN customers c ON o.customer_id = c.id GROUP BY c.region"
    ).fetchall()
    assert sorted(((r[0], float(r[1]), r[2]) for r in got.rows), key=_key) == sorted(
        ((r[0], float(r[1]), r[2]) for r in gold), key=_key
    )


def test_9_a_dictionary_has_one_row_for_each_key(ex, duck_db):
    got = ex.execute(Query(("total_revenue",), ("channel_dict.name",)))
    assert "dictGetOrNull" in got.sql and "SELECT DISTINCT" not in got.sql
    assert {r[0]: float(r[1]) for r in got.rows} == {"web": 450, "shop": 240}


# --- beyond the list ---------------------------------------------------------------


def test_a_dictionary_carries_nothing(ex, duck_db):
    """A dictionary has one row for each key, so a FILTER on it keeps no copies apart: the
    metrics stay in one SELECT, and a window over them is answered."""
    assert {"web_share", "web_rank"}.isdisjoint(unanswerable_metrics(ex.model, ex.catalog))
    got = ex.execute(Query(("web_share", "total_revenue"), ("customers.region",)))
    assert " UNION ALL " not in got.sql and "SELECT DISTINCT" in got.sql
    sums = dict(layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN))
    web = dict(
        layer2(duck_db, "o", "o.amount", {"region": "c.region"}, ORDERS_JOIN, "h.name = 'web'")
    )
    share = {r: None if r not in web else float(web[r]) / float(v) for r, v in sums.items()}
    assert {r[0]: (number(r[1]), float(r[2])) for r in got.rows} == {
        r: (share[r], float(v)) for r, v in sums.items()
    }
    ranked = ex.execute(Query(("web_rank",), ("customers.region",)))
    known = sorted(v for v in share.values() if v is not None)
    expected = {
        r: (known.index(v) + 1 if v is not None else len(known) + 1) for r, v in share.items()
    }
    assert {r[0]: r[1] for r in ranked.rows} == expected


def number(v):
    return None if v is None else float(v)


def test_a_dictionary_inside_a_subtree_that_repeats_a_key(ex, duck_db):
    """People repeats the person 1, and the region is a dictionary read after it: the
    dictionary is read inside the derived table, from the person's region id."""
    got = ex.execute(Query(("sales_total",), ("region_dict.name",)))
    assert {r[0]: float(r[1]) for r in got.rows} == {"EAST": 350, "WEST": 300}
    assert got.sql.count("SELECT DISTINCT") == 1
    inner = got.sql.split("FROM ossie_dedup.people AS people")[0]
    assert "dictGetOrNull('ossie_dedup.region_dict', 'name', people.region_id)" in inner
    gold = layer2(
        duck_db,
        "s",
        "s.amount",
        {"name": "r.name"},
        "sales s LEFT JOIN people p ON s.person_id = p.id "
        "LEFT JOIN regions r ON p.region_id = r.id",
    )
    same(got.rows, gold)


def test_two_datasets_repeated_at_once(ex, duck_db):
    """Customers and channels each have a repeated key: a derived table for each."""
    q = Query(("total_revenue",), ("customers.region", "channels.name"), ("channels.id > 0",))
    got = ex.execute(q)
    gold = layer2(
        duck_db,
        "o",
        "o.amount",
        {"region": "c.region", "name": "h.name"},
        ORDERS_JOIN,
        "h.id > 0",
    )
    rows = sorted(((r[0], r[1], float(r[2])) for r in got.rows), key=_key)
    assert rows == sorted(((r[0], r[1], float(r[2])) for r in gold), key=_key)
    assert got.sql.count("SELECT DISTINCT") == 2


def test_a_filter_of_an_aggregate_on_the_one_side(ex, duck_db):
    """The condition reads only customers, so it is carried as a value: C1's copies
    agree on it, and each EAST order counts once."""
    got = ex.execute(Query(("east_revenue",), ("channels.name",)))
    gold = layer2(duck_db, "o", "o.amount", {"name": "h.name"}, ORDERS_JOIN, "c.region = 'EAST'")
    assert {r[0]: float(r[1]) for r in got.rows} == {r[0]: float(r[1]) for r in gold}


@pytest.mark.parametrize(
    ("metric_name", "argument", "expected"),
    [
        # Channel 1 is stored twice, identically: O1, O3 and O4 count once.
        ("weighted_by_channel", "o.amount * h.id", 930),
        # C1's copies carry weights 1 and -1, so the product differs: both stay, as the
        # DISTINCT of Layer 2 over the argument keeps them (O1 and O2 give 0).
        ("weighted_by_customer", "o.amount * c.weight", None),
    ],
)
def test_an_aggregate_over_two_datasets_collapses_copies_equal_in_what_it_reads(
    ex, duck_db, metric_name, argument, expected
):
    got = ex.execute(Query((metric_name,)))
    gold = layer2(duck_db, "o", argument, {}, ORDERS_JOIN)
    assert float(got.rows[0][0]) == pytest.approx(float(gold[0][0]))
    if expected is not None:
        assert float(got.rows[0][0]) == expected


def test_unmatched_values_by_type(ex):
    catalog = Catalog(
        {
            "customers": TableInfo(
                "MergeTree",
                frozenset(),
                types={
                    "region": "LowCardinality(Nullable(String))",
                    "weight": "Int32",
                    "tags": "Array(String)",
                    "ids": "LowCardinality(String)",
                    "ring": "Ring",
                    "multi": "MultiPolygon",
                    "tup": "Tuple(a String, b Int32)",
                    "pt": "Point",
                    "var": "Variant(String, Int32)",
                    "agg": "AggregateFunction(sum, UInt64)",
                },
            )
        }
    )
    planner = Planner(ex.model, catalog)

    def show(column: str) -> str:
        return planner._unmatched(exp.column(column, table="customers")).sql("clickhouse")

    assert show("ring") == "defaultValueOfTypeName('Ring')"  # the geo types are arrays
    assert show("multi") == "defaultValueOfTypeName('MultiPolygon')"
    assert show("var") == "defaultValueOfTypeName('Variant(String, Int32)')"
    assert show("agg") == "defaultValueOfTypeName('AggregateFunction(sum, UInt64)')"
    # A tuple is NULL where the server makes it Nullable inside a join, the default where not.
    assert show("tup") == "defaultValueOfTypeName('Tuple(a String, b Int32)')"
    assert show("pt") == "defaultValueOfTypeName('Point')"
    planner.catalog.nullable_tuple = True
    assert show("tup") == "CAST(NULL, 'Nullable(Tuple(a String, b Int32))')"
    assert show("pt") == "CAST(NULL, 'Nullable(Point)')"
    planner.catalog.nullable_tuple = False
    assert show("region") == "CAST(NULL, 'Nullable(String)')"
    assert show("weight") == "CAST(NULL, 'Nullable(Int32)')"
    assert show("ids") == "CAST(NULL, 'Nullable(String)')"
    assert show("tags") == "defaultValueOfTypeName('Array(String)')"
    assert show("REGION") == "CAST(NULL, 'Nullable(String)')"  # any case
    assert show("unknown") == "NULL"
    assert Planner(ex.model)._unmatched(exp.column("tags", table="customers")).sql() == "NULL"


# --- the NULL of a row that matched nothing is the plain join's ----------------------

TYPES_DB = "ossie_dedup_types"
TYPED = {  # field -> (column type, expression); the expression fails on a bare NULL
    "tup": ("Tuple(a String, b Int32)", "tupleElement(tup, 1)"),
    "pt": ("Point", "tupleElement(pt, 1)"),
    "ring": ("Ring", "empty(ring)"),
    "poly": ("Polygon", "empty(poly)"),
    "mpoly": ("MultiPolygon", "empty(mpoly)"),
    "line": ("LineString", "empty(line)"),
    "mline": ("MultiLineString", "empty(mline)"),
    "arr": ("Array(String)", "has(arr, 'x')"),
    "mp": ("Map(String, String)", "mapContains(mp, 'k')"),
    "lc": ("LowCardinality(String)", "upper(lc)"),
    "dec": ("Decimal(10, 2)", "dec * 2"),
    "en": ("Enum8('a' = 1, 'b' = 2)", "toString(en)"),
}


@pytest.fixture(scope="module")
def typed(clickhouse, tmp_path_factory):
    """Customers of each type, none for the order 2: its row matches nothing."""
    db = TYPES_DB
    clickhouse.command(f"CREATE DATABASE IF NOT EXISTS {db}")
    for table in ("customers", "orders"):
        clickhouse.command(f"DROP TABLE IF EXISTS {db}.{table}")
    columns = ", ".join(f"{name} {kind}" for name, (kind, _) in TYPED.items())
    clickhouse.command(
        f"CREATE TABLE {db}.customers (id Int32, {columns}) ENGINE = MergeTree ORDER BY id"
    )
    clickhouse.command(
        f"CREATE TABLE {db}.orders (id Int32, customer_id Int32, amount Decimal(10, 2)) "
        "ENGINE = MergeTree ORDER BY id"
    )
    clickhouse.command(
        f"INSERT INTO {db}.customers (id) VALUES (1)"
    )  # every other column takes its default
    clickhouse.command(f"INSERT INTO {db}.orders VALUES (1, 1, 10), (2, 9, 5)")
    model = {
        "version": "0.3.0.dev0",
        "name": "typed",
        "datasets": [
            {
                "name": "customers",
                "source": f"{db}.customers",
                "primary_key": ["id"],
                "fields": [
                    f("id"),
                    *(f(name, expression) for name, (_, expression) in TYPED.items()),
                ],
            },
            {
                "name": "orders",
                "source": f"{db}.orders",
                "primary_key": ["id"],
                "fields": [f("id"), f("customer_id"), f("amount")],
            },
        ],
        "relationships": [relationship("orders", "customers", "customer_id")],
        "metrics": [metric("total_revenue", "SUM(orders.amount)")],
    }
    path = tmp_path_factory.mktemp("typed") / "typed.yaml"
    path.write_text(yaml.safe_dump(model, sort_keys=False))
    yield clickhouse, Executor(clickhouse, load_model(path))
    clickhouse.command(f"DROP DATABASE {db}")


@pytest.mark.parametrize("field", list(TYPED))
def test_an_unmatched_row_reads_what_the_plain_join_gives(typed, field):
    """Order 2 has no customer. The expression over its columns is what a LEFT JOIN with
    join_use_nulls = 1 leaves there on this server: NULL for a tuple, a Point, a
    string; an empty array for an array, a map or a geo type (Ring, Polygon, ...)."""
    clickhouse, ex = typed
    expression = TYPED[field][1]  # a bare column name: the one customers table has it
    plain = clickhouse.query(
        f"SELECT {expression} AS v, SUM(o.amount) FROM {TYPES_DB}.orders AS o "
        f"LEFT JOIN {TYPES_DB}.customers AS c ON o.customer_id = c.id GROUP BY v "
        "SETTINGS join_use_nulls = 1"
    ).result_rows
    got = ex.execute(Query(("total_revenue",), (f"customers.{field}",))).rows
    assert sorted(((str(v), float(s)) for v, s in got)) == sorted(
        ((str(v), float(s)) for v, s in plain)
    )

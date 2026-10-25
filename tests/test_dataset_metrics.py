"""Metrics declared on a dataset (spec, "Metric Scoping"; apache/ossie#591).

A dataset's metric reads its own fields by bare name and is asked for as
``dataset.metric``; it aggregates that dataset's rows, COUNT(*) too. Values
against ClickHouse are compared with DuckDB over the data of test_stitch.py."""

import copy
from pathlib import Path

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from dactopus_ossie_clickhouse.translate import untranslatable
from tests.conftest import FakeClickHouse
from tests.test_stitch import COLUMNS, FIXTURE, ROWS


def metric(name: str, expression: str) -> dict:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]},
    }


SCOPED = {
    "orders": [metric("order_rows", "COUNT(*)"), metric("revenue", "SUM(amount)")],
    # The same name on two datasets: the qualified name tells them apart.
    "returns": [metric("revenue", "SUM(amount)")],
    "customers": [metric("customer_rows", "COUNT(*)")],
}


def model_data(database: str = "ossie_scoped") -> dict:
    data = yaml.safe_load(FIXTURE.read_text().replace("ossie_stitch.", f"{database}."))
    for d in data["datasets"]:
        if d["name"] in SCOPED:
            d["metrics"] = copy.deepcopy(SCOPED[d["name"]])
    return data


def write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    return load_model(write(tmp_path_factory.mktemp("scoped"), model_data()))


@pytest.fixture(scope="module")
def planner(model):
    return Planner(model)


def refusal(planner: Planner, q: Query) -> str:
    with pytest.raises(PlanError) as e:
        planner.sql(q)
    return str(e.value)


# --- planning, no server -------------------------------------------------------


def test_bare_names_read_the_own_fields(planner):
    sql = planner.sql(Query(("orders.revenue",), ("customers.region",)))
    assert 'SUM(orders.amount) AS "orders.revenue"' in sql
    assert "FROM ossie_scoped.orders AS orders LEFT JOIN ossie_scoped.customers" in sql


def test_two_datasets_metrics_of_one_name(planner):
    sql = planner.sql(Query(("orders.revenue", "returns.revenue"), ("customers.region",)))
    assert 'any(__m0) AS "orders.revenue", any(__m1) AS "returns.revenue"' in sql


def test_names_resolve_case_insensitively(planner):
    sql = planner.sql(Query(("ORDERS.Revenue",)))
    assert 'AS "orders.revenue"' in sql


def test_count_star_counts_its_own_dataset(planner):
    # Over customers, not over the orders a dimension of customers would suggest.
    sql = planner.sql(Query(("customers.customer_rows",), ("customers.region",)))
    assert "FROM ossie_scoped.customers AS customers GROUP BY" in sql
    sql = planner.sql(Query(("orders.order_rows",), ("customers.region",)))
    assert "FROM ossie_scoped.orders AS orders LEFT JOIN" in sql


def test_count_star_over_another_root_is_refused(planner):
    # Each customer row would repeat once per order.
    q = Query(("customers.customer_rows",), ("orders.status",))
    assert "aggregates customers rows, but this question is answered over orders rows" in (
        refusal(planner, q)
    )


def test_a_model_metric_cannot_name_a_dataset_metric(tmp_path):
    data = model_data()
    data["metrics"].append(metric("twice", "orders.revenue * 2"))
    doc = load_model(write(tmp_path, data))
    assert "unknown field 'orders.revenue'" in unanswerable_metrics(doc)["twice"]


@pytest.mark.parametrize(
    ("expression", "error"),
    [
        (
            "SUM(orders.amount)",
            "orders.amount in 'SUM(orders.amount)': a metric of dataset 'orders' names its "
            "own fields without the dataset, as amount",
        ),
        (
            "SUM(amount) / COUNT(customers.id)",
            "customers.id in 'SUM(amount) / COUNT(customers.id)': a metric of dataset 'orders' "
            "reads only its own fields; a metric over other datasets belongs in the model's "
            "metrics",
        ),
        (
            "SUM(amout)",
            "'amout' in 'SUM(amout)' is not a field of 'orders' (did you mean: amount?)",
        ),
        # Another metric is not a field either (spec: no metric reads a metric).
        ("revenue * 2", "'revenue' in 'revenue * 2' is not a field of 'orders'"),
    ],
)
def test_scope_rules_are_reported_and_the_metric_refused(tmp_path, expression, error):
    data = model_data()
    data["datasets"][1]["metrics"].append(metric("bad", expression))
    doc = load_model(write(tmp_path, data))
    assert unanswerable_metrics(doc)["orders.bad"] == error
    with pytest.raises(PlanError, match="a metric of dataset|is not a field"):
        Planner(doc).sql(Query(("orders.bad",)))


def test_filters_name_a_dataset_metric(planner):
    sql = planner.sql(Query(("total_revenue",), ("customers.region",), ("orders.order_rows > 1",)))
    assert "HAVING COUNT(*) > 1" in sql


def test_time_strings_in_and_on_a_dataset_metric(tmp_path):
    # A string compared with a DateTime is read in the model's zone, not the column's.
    data = model_data()
    orders = data["datasets"][1]
    orders["fields"].append(
        {
            "name": "placed_at",
            "datatype": "DateTime",
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "placed_at"}]},
        }
    )
    orders["metrics"] += [
        metric("late", "SUM(CASE WHEN placed_at > '2026-01-01 00:00:00' THEN 1 ELSE 0 END)"),
        metric("last_order", "MAX(placed_at)") | {"datatype": "DateTime"},
    ]
    planner = Planner(load_model(write(tmp_path, data)))
    stamp = "toDateTime('2026-01-01 00:00:00', 'UTC')"
    assert f"orders.placed_at > {stamp}" in planner.sql(Query(("orders.late",)))
    q = Query(("orders.revenue",), ("customers.region",), ("orders.last_order > '2026-01-01'",))
    assert "HAVING MAX(orders.placed_at) > toDateTime('2026-01-01'" in planner.sql(q)


def test_order_by_a_dataset_metric(planner):
    q = Query(("orders.revenue",), ("customers.region",), order_by=("orders.revenue asc",))
    assert 'ORDER BY "orders.revenue" ASC' in planner.sql(q)


def test_unknown_metric_suggests_the_dataset_metric(planner):
    assert "did you mean: orders.revenue" in refusal(planner, Query(("orders.revenu",)))


def test_a_field_asked_for_as_a_metric_names_the_metrics_over_it(planner, tmp_path):
    """returns.amount reads like a dataset's metric. By spelling it got orders.revenue,
    which adds up another field (#66); the metrics that read it are the answer."""
    assert refusal(planner, Query(("Returns.Amount",))) == (
        "unknown metric 'Returns.Amount': it is a field of returns, not a metric; "
        "metrics over it: total_returns, return_rate, returns.revenue"
    )
    assert refusal(planner, Query(("amount",))) == (
        "unknown metric 'amount': it names the fields orders.amount, returns.amount, not "
        "a metric; metrics over them: total_revenue, revenue_or_zero, total_returns, "
        "return_rate, orders.revenue, returns.revenue"
    )
    # A leading dot names no dataset: suggestions by spelling, not a field of one.
    assert "it is a field" not in refusal(planner, Query((".amount",)))
    assert refusal(planner, Query(("customers.segment",))).endswith(
        "it is a field of customers, not a metric; no metric reads it"
    )
    # A bare name may be a dataset's metric asked without its dataset.
    data = model_data()
    data["datasets"][1]["metrics"].append(metric("segment", "COUNT(*)"))
    other = Planner(load_model(write(tmp_path, data)))
    assert refusal(other, Query(("segment",))).endswith(
        "no metric reads it; dataset metrics of that name: orders.segment"
    )


def test_untranslatable_names_a_dataset_metric(tmp_path):
    data = model_data()
    data["datasets"][1]["metrics"].append(metric("odd", "SUM(amount) +"))
    _, metrics = untranslatable(load_model(write(tmp_path, data)))
    assert list(metrics) == ["orders.odd"]


def test_executor_hides_what_it_refuses(tmp_path):
    data = model_data()
    data["datasets"][1]["metrics"].append(metric("bad", "SUM(orders.amount)"))
    data["datasets"][1]["metrics"].append(metric("odd", "SUM(amount) +"))
    tables = {
        ("ossie_scoped", d["source"].split(".")[1]): (
            "MergeTree",
            "MergeTree",
            ("", ""),
            {f["name"] for f in d["fields"]},
        )
        for d in data["datasets"]
    }
    ex = Executor(FakeClickHouse(tables), load_model(write(tmp_path, data)))
    orders = next(d for d in ex.model.datasets if d.name == "orders")
    assert [m.name for m in orders.metrics] == ["order_rows", "revenue"]
    assert "unknown metric 'orders.bad'" in refusal(ex.planner, Query(("orders.bad",)))
    assert set(ex.untranslatable_metrics) == {"orders.odd"}


# --- values, against ClickHouse ---------------------------------------------------

REGION = "LEFT JOIN customers c ON {t}.customer_id = c.id GROUP BY c.region"
CASES = [
    (
        Query(("orders.order_rows", "orders.revenue"), ("customers.region",)),
        f"SELECT c.region, COUNT(*), SUM(o.amount) FROM orders o {REGION.format(t='o')}",
    ),
    (
        Query(("customers.customer_rows",), ("customers.region",)),
        "SELECT region, COUNT(*) FROM customers GROUP BY region",
    ),
    # Stitched: a group without orders has no order rows, 0, not NULL.
    (
        Query(("orders.order_rows", "orders.revenue", "returns.revenue"), ("customers.region",)),
        f"""SELECT COALESCE(o.region, r.region), COALESCE(o.n, 0), o.s, r.s
        FROM (SELECT c.region, COUNT(*) AS n, SUM(o.amount) AS s FROM orders o
          {REGION.format(t="o")}) o
        FULL OUTER JOIN (SELECT c.region, SUM(x.amount) AS s FROM returns x
          {REGION.format(t="x")}) r ON o.region IS NOT DISTINCT FROM r.region""",
    ),
    (
        Query(("total_revenue",), ("customers.region",), ("orders.order_rows > 1",)),
        f"SELECT c.region, SUM(o.amount) FROM orders o {REGION.format(t='o')} HAVING COUNT(*) > 1",
    ),
    # Customers counted in a filter: one per region, whatever the orders.
    (
        Query(("total_revenue",), ("customers.region",), ("customers.customer_rows = 1",)),
        f"""SELECT COALESCE(r.region, c.region), r.s
        FROM (SELECT c.region, SUM(o.amount) AS s FROM orders o {REGION.format(t="o")}) r
        FULL OUTER JOIN (SELECT region, COUNT(*) AS n FROM customers GROUP BY region) c
          ON r.region IS NOT DISTINCT FROM c.region
        WHERE COALESCE(c.n, 0) = 1""",
    ),
    # A filter on orders, which reach customers: customers with a completed order.
    (
        Query(("customers.customer_rows",), (), ("orders.status = 'completed'",)),
        "SELECT COUNT(*) FROM customers "
        "WHERE id IN (SELECT customer_id FROM orders WHERE status = 'completed')",
    ),
]


@pytest.fixture(scope="module")
def ex(clickhouse, model):
    clickhouse.command("CREATE DATABASE IF NOT EXISTS ossie_scoped")
    for table, (columns, _) in COLUMNS.items():
        clickhouse.command(f"DROP TABLE IF EXISTS ossie_scoped.{table}")
        clickhouse.command(
            f"CREATE TABLE ossie_scoped.{table} ({columns}) ENGINE = MergeTree ORDER BY id"
        )
        clickhouse.command(f"INSERT INTO ossie_scoped.{table} VALUES {ROWS[table]}")
    yield Executor(clickhouse, model)
    clickhouse.command("DROP DATABASE ossie_scoped")


@pytest.fixture(scope="module")
def scoped_duck():
    import duckdb

    con = duckdb.connect()
    for table, (_, columns) in COLUMNS.items():
        con.execute(f"CREATE TABLE {table} ({columns})")
        con.execute(f"INSERT INTO {table} VALUES {ROWS[table]}")
    yield con
    con.close()


def _sorted(rows) -> list[tuple]:
    return sorted((tuple(r) for r in rows), key=lambda r: tuple((v is None, v or 0) for v in r))


@pytest.mark.parametrize(("q", "expected"), CASES)
def test_values_match_duckdb(ex, scoped_duck, q, expected):
    assert _sorted(ex.execute(q).rows) == _sorted(scoped_duck.execute(expected).fetchall())


def test_dataset_metric_equals_the_model_metric(ex):
    by_region = Query(("orders.revenue", "total_revenue"), ("customers.region",))
    assert all(a == b for _, a, b in ex.execute(by_region).rows)


def test_ordered_by_a_dataset_metric(ex):
    q = Query(("orders.order_rows",), ("customers.region",), order_by=("orders.order_rows desc",))
    r = ex.execute(q)
    assert r.columns == ["region", "orders.order_rows"]
    assert [n for _, n in r.rows] == sorted((n for _, n in r.rows), reverse=True)

"""``AGG(dataset.metric)`` in a model's metric, and ``AGG`` everywhere else.

Spec, "Metric Scoping" rule 6 and "Name Resolution" (apache/ossie#703): a model's metric
may evaluate a dataset's metric as ``AGG(dataset.metric)``, a single reference without
FILTER; a bare reference, AGG of a field, of a model's metric or of an expression is
invalid. ``expression_language.md``, "Measure Evaluation: AGG": no other expression
calls AGG, in any capitalization. ``wide_table_semantics.md`` section 5.2: the call is
one component whose home is the dataset, evaluated as the dataset's metric."""

import copy
import re
from pathlib import Path

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.access import Hidden, Policy, restrict
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from dactopus_ossie_clickhouse.translate import AGG_RULE, parse, untranslatable
from tests.conftest import FakeClickHouse


def expr(text: str) -> dict:
    return {"sql": text}


def metric(name: str, text: str) -> dict:
    return {"name": name, "expression": expr(text)}


def field(name: str, text: str) -> dict:
    return {"name": name, "expression": expr(text)}


# Each metric with AGG has a twin with the dataset metric's expression written out, which
# must give the same number. COUNT(*) of a dataset's metric counts its dataset's rows, so
# its twin counts orders' key.
BASE = {
    "version": "0.3.0.dev0",
    "name": "agg",
    "datasets": [
        {
            "name": "orders",
            "source": "ossie_agg.orders",
            "primary_key": ["id"],
            "fields": [
                field("id", "id"),
                field("customer_id", "customer_id"),
                field("amount", "amount"),
                field("status", "status"),
            ],
            "metrics": [
                metric("revenue", "SUM(amount)"),
                metric("avg_order", "SUM(amount) / NULLIF(COUNT(*), 0)"),
                metric("completed", "SUM(amount) FILTER (WHERE status = 'completed')"),
            ],
        },
        {
            "name": "customers",
            "source": "ossie_agg.customers",
            "primary_key": ["id"],
            "fields": [field("id", "id"), field("region", "region")],
        },
    ],
    "relationships": [
        {
            "name": "orders_to_customers",
            "from": "orders",
            "to": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
        }
    ],
    "metrics": [
        metric("rev", "AGG(orders.revenue)"),
        metric("rev_inline", "SUM(orders.amount)"),
        metric("per_customer", "AGG(orders.revenue) / COUNT(DISTINCT customers.id)"),
        metric("per_customer_inline", "SUM(orders.amount) / COUNT(DISTINCT customers.id)"),
        metric("avg_twice", "agg(orders.avg_order) * 2"),
        metric("avg_twice_inline", "SUM(orders.amount) / NULLIF(COUNT(orders.id), 0) * 2"),
        metric("avg_per_customer", "Agg(orders.avg_order) / COUNT(DISTINCT customers.id)"),
        metric(
            "avg_per_customer_inline",
            "SUM(orders.amount) / NULLIF(COUNT(orders.id), 0) / COUNT(DISTINCT customers.id)",
        ),
        metric("share", "AGG(orders.completed) / AGG(orders.revenue)"),
        metric(
            "share_inline",
            "SUM(orders.amount) FILTER (WHERE orders.status = 'completed') / SUM(orders.amount)",
        ),
    ],
}
TWINS = ["rev", "per_customer", "avg_twice", "avg_per_customer", "share"]


def build(tmp_path: Path, mutate=lambda data: None):
    data = copy.deepcopy(BASE)
    mutate(data)
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_model(path)


@pytest.fixture
def model(tmp_path):
    return build(tmp_path)


def sql(model, *metrics: str, **kw) -> str:
    return Planner(model).sql(Query(metrics, **kw))


def refusal(model, *metrics: str, **kw) -> str:
    with pytest.raises(PlanError) as e:
        sql(model, *metrics, **kw)
    return str(e.value)


def with_model_metric(tmp_path, text: str):
    return build(tmp_path, lambda data: data["metrics"].append(metric("m", text)))


# --- a model's metric, no server ---------------------------------------------


def test_every_metric_of_the_model_translates_and_is_answerable(model):
    assert untranslatable(model) == ({}, {})
    assert unanswerable_metrics(model) == {}


def test_agg_of_a_dataset_metric_is_its_expression(model):
    assert sql(model, "rev") == sql(model, "rev_inline").replace("rev_inline", "rev")
    assert "SUM(orders.amount) AS rev" in sql(model, "rev")
    assert "agg(" not in sql(model, "per_customer", dimensions=("customers.region",)).lower()


def test_count_star_of_a_dataset_metric_counts_that_dataset(model):
    # Over one dataset, and as a component next to another dataset's (split).
    assert "nullIf(COUNT(*), 0)" in sql(model, "avg_twice")
    split = sql(model, "avg_per_customer", dimensions=("customers.region",))
    assert "UNION ALL" in split and "COUNT(*)" in split


def test_agg_as_a_whole_metric_is_bridged_as_the_dataset_metric(tmp_path):
    # A dimension orders does not reach through many-to-one: the dataset's metric is
    # joined to a spine, and so is a model metric that is only AGG of it.
    def mutate(data):
        data["datasets"].append(
            {
                "name": "visits",
                "source": "ossie_agg.visits",
                "primary_key": ["id"],
                "fields": [field("id", "id"), field("order_id", "order_id")],
            }
        )
        data["relationships"].append(
            {
                "name": "visits_to_orders",
                "from": "visits",
                "to": "orders",
                "from_columns": ["order_id"],
                "to_columns": ["id"],
            }
        )

    model = build(tmp_path, mutate)
    direct = sql(model, "orders.revenue", dimensions=("visits.id",))
    assert "__bridge" in direct
    assert sql(model, "rev", dimensions=("visits.id",)) == direct.replace('"orders.revenue"', "rev")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (
            "AGG(orders.amount)",
            "AGG(orders.amount) in 'AGG(orders.amount)' names a field of 'orders', not one of "
            f"its metrics; {AGG_RULE}. Aggregate the field itself, such as SUM(orders.amount)",
        ),
        (
            "AGG(orders.revenu)",
            "AGG(orders.revenu) in 'AGG(orders.revenu)' names no metric of 'orders' (did you "
            "mean: orders.revenue,",
        ),
        (
            "AGG(order.revenue)",
            "AGG(order.revenue) in 'AGG(order.revenue)' names unknown dataset 'order' (did you "
            "mean: orders?)",
        ),
        (
            "AGG(orders.rev)",  # a model's metric of that name does not count
            "AGG(orders.rev) in 'AGG(orders.rev)' names no metric of 'orders'",
        ),
        (
            "SUM(AGG(orders.revenue))",
            "metric 'm' applies SUM to SUM(orders.amount), an aggregate",
        ),
    ],
)
def test_agg_of_anything_but_a_dataset_metric_is_refused(tmp_path, text, error):
    model = with_model_metric(tmp_path, text)
    assert untranslatable(model) == ({}, {})
    assert unanswerable_metrics(model)["m"].startswith(error)


@pytest.mark.parametrize(
    "text",
    [
        "AGG(rev)",  # unqualified, a model's metric
        "AGG(revenue)",  # unqualified
        "AGG(orders.revenue + 1)",
        "AGG(orders.revenue, orders.completed)",
        "AGG()",
        "AGG(*)",
        "AGG(DISTINCT orders.revenue)",
        "AGG(x.orders.revenue)",
        "AGG(orders.revenue) FILTER (WHERE orders.status = 'completed')",
        "AGG(orders.revenue) OVER ()",
        "AGG(orders.revenue) IGNORE NULLS OVER ()",
    ],
)
def test_a_misshapen_agg_call_does_not_translate(tmp_path, text):
    model = with_model_metric(tmp_path, text)
    _, metrics = untranslatable(model)
    assert metrics["m"].endswith(f"is not allowed; {AGG_RULE}: {text!r}")


def test_a_bare_dataset_metric_reference_is_refused(tmp_path):
    model = with_model_metric(tmp_path, "orders.revenue / COUNT(DISTINCT customers.id)")
    assert "unknown field 'orders.revenue'" in unanswerable_metrics(model)["m"]


def test_a_window_over_agg(tmp_path):
    model = with_model_metric(tmp_path, "SUM(AGG(orders.revenue)) OVER ()")
    assert "SUM(SUM(orders.amount)) OVER () AS m" in sql(model, "m", dimensions=("orders.status",))


# --- AGG anywhere else, no server ----------------------------------------------


@pytest.mark.parametrize("call", ["AGG(x)", "agg(x)", "Agg(x)", "aGG(x)", "AGG(x) OVER ()"])
def test_agg_is_checked_in_every_capitalization(call):
    with pytest.raises(ValueError, match=re.escape(f"AGG(x) is not allowed; {AGG_RULE}")):
        parse(f"1 + {call}")


@pytest.mark.parametrize(
    "dialects",
    [
        None,  # the top-level sql
        [{"dialect": "OSSIE_SQL_2026", "expression": "AGG(orders.revenue)"}],
        [{"dialect": "ANSI_SQL", "expression": "AGG(orders.revenue)"}],
    ],
)
@pytest.mark.parametrize("where", ["a field", "a metric of a dataset"])
def test_agg_is_refused_in_a_field_and_a_dataset_metric(tmp_path, where, dialects):
    item = {"name": "x", "expression": {"sql": "AGG(orders.revenue)"}}
    if dialects is not None:
        item["expression"] = {"dialects": dialects}

    def mutate(data):
        data["datasets"][0]["fields" if where == "a field" else "metrics"].append(item)

    fields, metrics = untranslatable(build(tmp_path, mutate))
    found = fields if where == "a field" else metrics
    assert found == {
        "orders.x": f"AGG(orders.revenue) in {where} is not allowed; {AGG_RULE}: "
        "'AGG(orders.revenue)'"
    }


def test_agg_in_an_ansi_sql_model_metric_is_read_as_in_sql(tmp_path):
    def mutate(data):
        data["metrics"].append(
            {
                "name": "m",
                "expression": {
                    "dialects": [{"dialect": "ANSI_SQL", "expression": "AGG(orders.revenue)"}]
                },
            }
        )

    assert "SUM(orders.amount) AS m" in sql(build(tmp_path, mutate), "m")


def test_the_planner_refuses_agg_in_a_field_and_a_dataset_metric(tmp_path):
    # Without an executor nothing is hidden: the planner says why itself.
    def mutate(data):
        data["datasets"][0]["fields"].append(field("f", "AGG(orders.revenue)"))
        data["datasets"][0]["metrics"].append(metric("m", "AGG(orders.revenue)"))

    model = build(tmp_path, mutate)
    assert refusal(model, "rev", dimensions=("orders.f",)) == (
        f"field orders.f: AGG(orders.revenue) in a field is not allowed; {AGG_RULE}"
    )
    assert refusal(model, "orders.m") == (
        f"AGG(orders.revenue) in a metric of a dataset is not allowed; {AGG_RULE}: "
        "'AGG(orders.revenue)'"
    )


@pytest.mark.parametrize("call", ["AGG(orders.revenue) > 1", "agg(orders.revenue) > 1"])
def test_agg_in_a_filter_is_refused_with_the_name_to_use(model, call):
    assert refusal(model, "rev", filters=(call,)) == (
        f"AGG(orders.revenue) in a filter is not allowed; {AGG_RULE}; a filter names a "
        f"dataset's metric as orders.revenue: {call!r}"
    )


def test_a_filter_on_a_model_metric_with_agg(model):
    text = sql(model, "rev", dimensions=("customers.region",), filters=("per_customer > 10",))
    assert "HAVING" in text and "agg(" not in text.lower()


def test_agg_as_a_name_is_valid(tmp_path):
    def mutate(data):
        ds = data["datasets"][0]
        ds["fields"].append(field("Agg_amount", "agg"))
        ds["metrics"] += [metric("agg_total", "SUM(source.agg)"), metric("agg", "COUNT(status)")]
        data["metrics"] += [
            metric("agg_count", "COUNT(orders.Agg_amount)"),
            metric("of_agg", "AGG(orders.agg)"),
        ]

    model = build(tmp_path, mutate)
    assert untranslatable(model) == ({}, {})
    assert unanswerable_metrics(model) == {}
    assert "COUNT(orders.agg) AS agg_count" in sql(model, "agg_count")
    assert 'SUM(orders.agg) AS "orders.agg_total"' in sql(model, "orders.agg_total")
    assert 'COUNT(orders.status) AS "orders.agg"' in sql(model, "orders.agg")
    assert "COUNT(orders.status) AS of_agg" in sql(model, "of_agg")
    sql(model, "rev", filters=("orders.Agg_amount > 1",))


# --- access ----------------------------------------------------------------------


def tables(orders: set[str]) -> dict:
    return {
        ("ossie_agg", "orders"): ("MergeTree", "MergeTree", ("", ""), orders),
        ("ossie_agg", "customers"): ("MergeTree", "MergeTree", ("", ""), {"id", "region"}),
    }


def model_metrics(model) -> set[str]:
    return {m.name for m in model.metrics or []}


@pytest.mark.parametrize(
    "hidden",
    [
        Hidden(metrics=frozenset({"orders.revenue"})),
        Hidden(metrics=frozenset({"ORDERS.REVENUE"})),
        Hidden(fields=frozenset({"orders.amount"})),
        Hidden(datasets=frozenset({"orders"})),
    ],
)
def test_a_model_metric_goes_with_the_dataset_metric_it_evaluates(model, hidden):
    kept = model_metrics(restrict(model, hidden))
    assert not {"rev", "per_customer", "share"} & kept
    if not hidden.fields and not hidden.datasets:
        assert {"avg_twice", "avg_per_customer", "rev_inline"} <= kept


def test_a_model_metric_over_an_unreadable_column_is_hidden(model):
    ex = Executor(FakeClickHouse(tables({"id", "customer_id", "status"})), model)
    assert not {"rev", "per_customer", "avg_twice", "share"} & model_metrics(ex.model)
    for name in ("rev", "per_customer"):
        msg = refusal(ex.model, name)
        assert "revenue" not in msg and "amount" not in msg


def test_a_policy_hides_a_model_metric_with_its_dataset_metric(model, tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"u": {"hidden_metrics": ["orders.revenue"]}}))
    readable = tables({"id", "customer_id", "amount", "status"})
    ex = Executor(FakeClickHouse(readable), model, Policy.load(path))
    assert {"avg_twice", "avg_per_customer"} <= model_metrics(ex.model)
    assert not {"rev", "per_customer", "share"} & model_metrics(ex.model)
    assert "revenue" not in refusal(ex.model, "per_customer")
    assert ex.withheld()["policy"] == {"metrics": 4}


# --- values, against ClickHouse --------------------------------------------------

SETUP = [
    "DROP DATABASE IF EXISTS ossie_agg",
    "CREATE DATABASE ossie_agg",
    "CREATE TABLE ossie_agg.orders (id Int32, customer_id Int32, amount Decimal(10, 2), "
    "status String) ENGINE = MergeTree ORDER BY id",
    "INSERT INTO ossie_agg.orders VALUES (1, 1, 100.00, 'completed'), "
    "(2, 1, 50.00, 'completed'), (3, 2, 200.00, 'pending'), (4, 3, 20.00, 'completed'), "
    "(5, 3, 30.00, 'pending')",
    "CREATE TABLE ossie_agg.customers (id Int32, region String) ENGINE = MergeTree ORDER BY id",
    "INSERT INTO ossie_agg.customers VALUES (1, 'west'), (2, 'west'), (3, 'east'), (4, 'north')",
]


@pytest.fixture
def ex(clickhouse, model):
    for stmt in SETUP:
        clickhouse.command(stmt)
    yield Executor(clickhouse, model)
    clickhouse.command("DROP DATABASE ossie_agg")


def test_agg_gives_the_number_of_its_expression_written_out(ex):
    # The server's function list does not hide a call of AGG: the planner expands it.
    assert set(TWINS) <= model_metrics(ex.model)
    assert ex.check() == []
    for dimensions in [(), ("customers.region",), ("orders.status",)]:
        for name in TWINS:
            got, want = (answer(ex, n, dimensions) for n in (name, f"{name}_inline"))
            if isinstance(want, str):  # refused alike: customers by an order's status
                assert got == want.replace(f"{name}_inline", name)
            else:
                assert got == pytest.approx(want), (name, dimensions)


def answer(ex, name: str, dimensions: tuple[str, ...]) -> dict | str:
    """The rows of a question by their dimensions, or why it is refused."""
    try:
        rows = ex.execute(Query((name,), dimensions)).rows
    except PlanError as e:
        return str(e)
    return {row[:-1]: row[-1] for row in rows}


def test_agg_values(ex):
    # Hand-computed from the five orders of three customers and one customer without any.
    def by_region(name: str) -> dict:
        return dict(ex.execute(Query((name,), ("customers.region",))).rows)

    def total(name: str):
        return ex.execute(Query((name,))).rows[0][0]

    assert total("rev") == 400
    assert total("per_customer") == pytest.approx(100)  # 400 over 4 customers
    assert total("avg_twice") == pytest.approx(160)  # 400 over 5 orders, twice
    assert total("avg_per_customer") == pytest.approx(20)
    assert total("share") == pytest.approx(170 / 400)
    assert by_region("per_customer") == pytest.approx({"west": 175, "east": 50, "north": None})
    assert by_region("avg_per_customer") == pytest.approx(
        {"west": 350 / 3 / 2, "east": 25, "north": None}
    )
    # As the dataset's metric asked directly: orders' rows only, no north.
    assert by_region("rev") == by_region("orders.revenue") == {"west": 350, "east": 50}

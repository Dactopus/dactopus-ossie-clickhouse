"""``source.column`` in a dataset's metric, and the reserved ``AGG``.

Spec, "Metric Scoping" rules 1 to 3 and "Name Resolution" (apache/ossie#633): a dataset's
metric reads a column of its source as ``source.column``, with the qualifier case
insensitive, whatever a field of the same name says; a dataset that declares a field
named ``source`` has no such access. ``expression_language.md``, "Measure Evaluation:
AGG (RESERVED)" (apache/ossie#662): no expression calls ``AGG`` yet."""

import copy
from pathlib import Path

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from dactopus_ossie_clickhouse.translate import parse, untranslatable
from tests.conftest import FakeClickHouse


def expr(text: str) -> dict:
    return {"sql": text}


def metric(name: str, text: str) -> dict:
    return {"name": name, "expression": expr(text)}


def field(name: str, text: str) -> dict:
    return {"name": name, "expression": expr(text)}


# `amount` is a field that differs from the column it shares a name with; `raw_amount`
# reads the column as it is.
BASE = {
    "version": "0.2.0.dev0",
    "name": "source_columns",
    "datasets": [
        {
            "name": "orders",
            "source": "ossie_srccol.orders",
            "primary_key": ["id"],
            "fields": [
                field("id", "id"),
                field("amount", "amount * 100"),
                field("raw_amount", "amount"),
                field("status", "status"),
            ],
            "metrics": [
                metric("via_field", "SUM(raw_amount)"),
                metric("via_source", "SUM(source.amount)"),
                metric("via_upper", "SUM(SOURCE.amount)"),
                metric("via_mixed", "SUM(Source.amount)"),
                metric("shifted", "SUM(amount)"),
                metric("filtered", "SUM(source.amount) FILTER (WHERE status = 'completed')"),
                metric("rows_over_25", "COUNT(*) FILTER (WHERE source.amount > 25)"),
            ],
        }
    ],
    "metrics": [metric("total", "SUM(orders.raw_amount)")],
}


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


# --- source.column, no server --------------------------------------------------


def test_source_column_gives_the_sql_of_the_field_that_reads_it(model):
    via_field = sql(model, "orders.via_field")
    assert 'SUM(orders.amount) AS "orders.via_field"' in via_field
    for name in ("via_source", "via_upper", "via_mixed"):
        assert sql(model, f"orders.{name}") == via_field.replace("via_field", name)


def test_source_amount_reads_the_column_not_the_field_of_that_name(model):
    # The field `amount` is `amount * 100`; source.amount is the column (rule 2).
    assert 'SUM(orders.amount * 100) AS "orders.shifted"' in sql(model, "orders.shifted")
    assert 'SUM(orders.amount) AS "orders.via_source"' in sql(model, "orders.via_source")


def test_source_column_beside_fields_and_a_dimension(model):
    out = sql(model, "orders.filtered", dimensions=("orders.status",))
    assert "SUM(orders.amount) FILTER(WHERE orders.status = 'completed')" in out.replace(
        "FILTER (", "FILTER("
    )
    assert "COUNT(*) FILTER" in sql(model, "orders.rows_over_25")


def test_a_source_column_is_not_substituted_by_a_field_of_another_name(tmp_path):
    def mutate(data):
        data["datasets"][0]["fields"].append(field("net", "amount - 1"))
        data["datasets"][0]["metrics"].append(metric("on_net", "SUM(source.net)"))

    # `net` is a field, not a column of the source: the server decides whether the column
    # exists; the planner reads it as written and substitutes nothing.
    out = sql(build(tmp_path, mutate), "orders.on_net")
    assert 'SUM(orders.net) AS "orders.on_net"' in out
    assert "- 1" not in out


def test_every_metric_of_the_model_is_answerable(model):
    assert untranslatable(model) == ({}, {})
    assert unanswerable_metrics(model) == {}


def test_a_column_keeps_the_spelling_the_metric_gives_it(tmp_path):
    # Only the qualifier is case insensitive; ClickHouse's column names are not.
    def mutate(data):
        data["datasets"][0]["metrics"].append(metric("upper", "SUM(source.AMOUNT)"))

    assert 'SUM(orders.AMOUNT) AS "orders.upper"' in sql(build(tmp_path, mutate), "orders.upper")


def test_a_field_named_source_takes_the_qualifier(tmp_path):
    def mutate(data):
        data["datasets"][0]["fields"].append(field("source", "status"))
        data["datasets"][0]["metrics"].append(metric("bare", "COUNT(source)"))

    model = build(tmp_path, mutate)
    problems = unanswerable_metrics(model)
    assert problems["orders.via_source"] == (
        "source.amount in 'SUM(source.amount)': dataset 'orders' declares a field named "
        "source, which takes precedence over the qualifier, so its metrics cannot read "
        "source columns"
    )
    assert "orders.via_upper" in problems
    # The bare name is the field.
    assert "orders.bare" not in problems
    assert 'COUNT(orders.status) AS "orders.bare"' in sql(model, "orders.bare")


def test_a_field_named_source_is_found_in_any_case(tmp_path):
    def mutate(data):
        data["datasets"][0]["fields"].append(field("Source", "status"))

    assert "orders.via_source" in unanswerable_metrics(build(tmp_path, mutate))


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (
            "SUM(orders.amount)",
            "orders.amount in 'SUM(orders.amount)': a metric of dataset 'orders' names its "
            "own fields without the dataset, as amount",
        ),
        (
            "SUM(other.amount)",
            "other.amount in 'SUM(other.amount)': a metric of dataset 'orders' reads only its "
            "own fields and, as source.column, its source's columns; a metric over other "
            "datasets belongs in the model's metrics",
        ),
        (
            "SUM(amt)",
            "'amt' in 'SUM(amt)' is not a field of 'orders' (did you mean: amount?)",
        ),
        (
            "SUM(source.db.amount)",
            "source.db.amount in 'SUM(source.db.amount)'",
        ),
    ],
)
def test_other_names_in_a_dataset_metric_stay_errors(tmp_path, text, error):
    def mutate(data):
        data["datasets"][0]["metrics"].append(metric("bad", text))

    model = build(tmp_path, mutate)
    try:
        found = unanswerable_metrics(model)["orders.bad"]
    except KeyError:
        pytest.fail("the metric was accepted")
    assert found.startswith(error)


def test_source_column_in_a_model_metric_is_refused(tmp_path):
    def mutate(data):
        data["metrics"].append(metric("bad", "SUM(source.amount)"))
        data["metrics"].append(metric("also_bad", "SUM(orders.source.amount)"))

    problems = unanswerable_metrics(build(tmp_path, mutate))
    assert problems["bad"] == (
        "source.amount in 'SUM(source.amount)': source columns are read only by a dataset's "
        "own metrics, as source.column; use a field of the dataset (dataset.field)"
    )
    assert "also_bad" in problems


def test_source_column_in_a_filter_is_refused(model):
    msg = refusal(model, "total", filters=("source.amount > 1",))
    assert "source columns are read only by a dataset's own metrics" in msg
    msg = refusal(model, "orders.via_source", filters=("source.amount > 1",))
    assert "source columns are read only by a dataset's own metrics" in msg


def test_source_column_in_a_key_or_relationship_is_refused(tmp_path):
    from dactopus_ossie_clickhouse.model import ModelError

    def key(data):
        data["datasets"][0]["primary_key"] = ["source.id"]

    with pytest.raises(ModelError, match="'source.id' is not a field of 'orders'"):
        build(tmp_path, key)


def test_a_dataset_named_source_keeps_its_own_fields_in_a_model_metric(tmp_path):
    def mutate(data):
        data["datasets"].append(
            {
                "name": "source",
                "source": "ossie_srccol.src",
                "primary_key": ["id"],
                "fields": [field("id", "id"), field("v", "v")],
            }
        )
        data["metrics"].append(metric("v_total", "SUM(source.v)"))

    model = build(tmp_path, mutate)
    assert "SUM(source.v) AS v_total" in sql(model, "v_total")


# --- AGG, no server ------------------------------------------------------------

RESERVED = (
    "AGG() is reserved by the Ossie spec for a future version and cannot be called in an "
    "expression yet"
)


@pytest.mark.parametrize("call", ["AGG(x)", "agg(x)", "Agg(x)", "aGG(x)", "AGG(x) OVER ()"])
def test_agg_is_reserved_in_every_capitalization(call):
    with pytest.raises(ValueError, match="AGG\\(\\) is reserved by the Ossie spec"):
        parse(f"1 + {call}")


@pytest.mark.parametrize(
    "dialects",
    [
        None,  # the top-level sql
        [{"dialect": "OSSIE_SQL_2026", "expression": "AGG(amount)"}],
        [{"dialect": "ANSI_SQL", "expression": "AGG(amount)"}],
    ],
)
@pytest.mark.parametrize("where", ["field", "dataset metric", "model metric"])
def test_agg_is_refused_in_a_field_and_a_metric(tmp_path, where, dialects):
    def expression(inner: str) -> dict:
        if dialects is None:
            return {"sql": f"AGG({inner})"}
        return {"dialects": dialects}

    def mutate(data):
        ds = data["datasets"][0]
        if where == "field":
            ds["fields"].append({"name": "f", "expression": expression("amount")})
        elif where == "dataset metric":
            ds["metrics"].append({"name": "m", "expression": expression("amount")})
        else:
            data["metrics"].append({"name": "m", "expression": expression("orders.raw_amount")})

    model = build(tmp_path, mutate)
    fields, metrics = untranslatable(model)
    found = {"field": fields, "dataset metric": metrics, "model metric": metrics}[where]
    assert len(found) == 1
    assert next(iter(found.values())).startswith(RESERVED)


def test_agg_in_a_dataset_metric_over_a_source_column(tmp_path):
    def mutate(data):
        data["datasets"][0]["metrics"].append(metric("m", "agg(source.amount)"))

    _, metrics = untranslatable(build(tmp_path, mutate))
    assert metrics == {"orders.m": RESERVED + ": 'agg(source.amount)'"}


@pytest.mark.parametrize("call", ["AGG(orders.raw_amount)", "agg(orders.raw_amount) > 1"])
def test_agg_in_a_filter_is_refused_before_clickhouse(model, call):
    msg = refusal(model, "total", filters=(call,))
    assert msg.startswith(RESERVED)


def test_agg_as_a_name_is_valid(tmp_path):
    def mutate(data):
        ds = data["datasets"][0]
        ds["fields"] += [field("agg", "status"), field("Agg_amount", "agg")]
        ds["metrics"].append(metric("agg_total", "SUM(source.agg)"))
        data["metrics"].append(metric("agg", "COUNT(orders.agg)"))

    model = build(tmp_path, mutate)
    assert untranslatable(model) == ({}, {})
    assert unanswerable_metrics(model) == {}
    assert "COUNT(orders.status) AS agg" in sql(model, "agg")
    assert 'SUM(orders.agg) AS "orders.agg_total"' in sql(model, "orders.agg_total")
    sql(model, "total", filters=("orders.agg = 'completed'",))


def test_agg_as_the_name_of_a_dataset_metric_is_valid(tmp_path):
    def mutate(data):
        data["datasets"][0]["metrics"].append(metric("agg", "COUNT(status)"))

    model = build(tmp_path, mutate)
    assert unanswerable_metrics(model) == {}
    assert 'COUNT(orders.status) AS "orders.agg"' in sql(model, "orders.agg")


# --- access: a source column the user cannot read -------------------------------


def tables(columns: set[str]) -> dict:
    return {
        ("ossie_srccol", "orders"): ("MergeTree", "MergeTree", ("", ""), columns),
    }


def test_a_metric_over_an_unreadable_source_column_is_hidden(model):
    # The caller may read id and amount, not status: `rows_over_25` reads only amount.
    ex = Executor(FakeClickHouse(tables({"id", "amount"})), model)
    visible = {m.name for m in ex.model.datasets[0].metrics}
    assert {"via_source", "via_upper", "via_mixed", "via_field", "shifted"} <= visible
    assert "filtered" not in visible  # reads the field status, which reads the column status
    assert "rows_over_25" in visible
    ex = Executor(FakeClickHouse(tables({"id", "status"})), model)
    visible = {m.name for m in ex.model.datasets[0].metrics or []}
    assert "via_source" not in visible and "rows_over_25" not in visible
    assert "filtered" not in visible  # source.amount
    msg = refusal(ex.model, "orders.via_source")
    assert "amount" not in msg


def test_an_unreadable_source_column_never_appears_in_an_error(model):
    ex = Executor(FakeClickHouse(tables({"id", "status"})), model)
    for name in ("orders.via_source", "orders.via_sourc", "orders.rows_over_25"):
        msg = refusal(ex.model, name)
        assert "amount" not in msg and "source.amount" not in msg
    msg = refusal(ex.model, "orders.status", filters=("source.amount > 1",))
    assert "amount" not in msg.replace("source.amount > 1", "")


# --- values, against ClickHouse --------------------------------------------------

SETUP = [
    "DROP DATABASE IF EXISTS ossie_srccol",
    "CREATE DATABASE ossie_srccol",
    "CREATE TABLE ossie_srccol.orders (id Int32, amount Decimal(10, 2), status String) "
    "ENGINE = MergeTree ORDER BY id",
    "INSERT INTO ossie_srccol.orders VALUES "
    "(1, 100.00, 'completed'), (2, 50.00, 'completed'), (3, 200.00, 'pending'), "
    "(4, 20.00, 'completed')",
]


@pytest.fixture
def ex(clickhouse, model):
    for stmt in SETUP:
        clickhouse.command(stmt)
    yield Executor(clickhouse, model)
    clickhouse.command("DROP DATABASE ossie_srccol")


def test_source_column_values(ex):
    # Hand-computed from the four rows; the field `amount` is the column times 100.
    def one(name: str, **kw):
        return ex.execute(Query((name,), **kw)).rows[0][0]

    assert one("orders.via_field") == one("orders.via_source") == 370
    assert one("orders.via_upper") == one("orders.via_mixed") == 370
    assert one("orders.shifted") == 37000
    assert one("orders.filtered") == 170
    assert one("orders.rows_over_25") == 3
    assert one("orders.via_source", filters=("orders.status = 'pending'",)) == 200
    rows = ex.execute(Query(("orders.via_source", "orders.shifted"), ("orders.status",))).rows
    assert sorted(rows) == [("completed", 170, 17000), ("pending", 200, 20000)]
    # A filter on a metric that reads a source column.
    kept = ex.execute(
        Query(("orders.via_source",), ("orders.status",), ("orders.via_source > 180",))
    ).rows
    assert kept == [("pending", 200)]
    # A model metric over the same rows, through the field.
    assert one("total") == 370


def test_validate_checks_the_columns_a_metric_reads(clickhouse, model, tmp_path):
    for stmt in SETUP:
        clickhouse.command(stmt)
    try:
        assert Executor(clickhouse, model).check() == []

        def mutate(data):
            data["datasets"][0]["metrics"].append(metric("gone", "SUM(source.no_such_column)"))

        problems = Executor(clickhouse, build(tmp_path, mutate)).check()
        assert problems == [
            "dataset 'orders': column 'no_such_column' (metric 'orders.gone') not in "
            "ossie_srccol.orders"
        ]
    finally:
        clickhouse.command("DROP DATABASE ossie_srccol")


def test_a_source_column_is_no_field_a_metric_reads(tmp_path):
    # A dataset named source with a field `amount`: orders' source.amount is not a metric
    # over source.amount, which stays free of metrics.
    def mutate(data):
        data["datasets"].append(
            {
                "name": "source",
                "source": "ossie_srccol.src",
                "primary_key": ["id"],
                "fields": [field("id", "id"), field("amount", "amount")],
            }
        )

    msg = refusal(build(tmp_path, mutate), "source.amount")
    assert msg.endswith("no metric reads it")

"""``source.column`` in a dataset's metric, and the reserved ``AGG`` and ``METRIC``.

Spec, "Metric Scoping" rules 1 to 3 and "Name Resolution" (apache/ossie#633): a dataset's
metric reads a column of its source as ``source.column``, with the qualifier case
insensitive, whatever a field of the same name says; a dataset that declares a field
named ``source`` has no such access."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from dactopus_ossie_clickhouse.translate import (
    FIELD_AGGREGATE_RULE,
    METRIC_FIELD_RULE,
    METRIC_RESERVED,
    METRIC_RULE,
    parse,
    untranslatable,
)
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
    "version": "0.3.0.dev0",
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


# --- the reserved METRIC ----------------------------------------------------------

# Spec, expression_language.md "Metric Definition: METRIC (RESERVED)" (apache/ossie#774):
# a call is refused in every capitalization; a field, a metric or a column named metric
# is allowed.


def metric_refusal(call: str, text: str, rule: str = METRIC_RULE) -> str:
    return f"{call} is not allowed; {rule}: {text!r}"


def field_refusal(call: str, text: str) -> str:
    """In a field only the reservation: a field is scalar, so SUM(amount), the advice
    for a metric, would be refused again as an aggregate in a field."""
    return metric_refusal(call, text, METRIC_FIELD_RULE)


def test_the_reason_says_ossie_sql_and_where_a_field_or_metric_may_call_metric(tmp_path):
    # expression_choice reads `sql` before `dialects`, so a CLICKHOUSE text counts only
    # when the expression has no `sql`: the way out says so. A question has none.
    fields, metrics = untranslatable(build(tmp_path, with_metric_calls))
    way_out = (
        "write the expression in the CLICKHOUSE dialect only, with no `sql` and no "
        "OSSIE_SQL_2026 or ANSI_SQL text"
    )
    for why in (*fields.values(), *metrics.values()):
        assert "no Ossie SQL expression may call it" in why and "no expression may call" not in why
        assert "rename the server function" in why and way_out in why
    model = build(tmp_path)
    found = refusal(model, "total", filters=("METRIC(orders.raw_amount) > 1",))
    assert "no Ossie SQL expression may call it" in found
    assert "CLICKHOUSE" not in found and "rename the server function" not in found


@pytest.mark.parametrize("other", ["sql", "OSSIE_SQL_2026", "ANSI_SQL"])
def test_another_text_beside_a_clickhouse_text_keeps_the_field_hidden(tmp_path, other):
    # expression_choice reads `sql`, then OSSIE_SQL_2026, ANSI_SQL and CLICKHOUSE.
    def mutate(data):
        text = "metric(amount)"
        both = (
            {"sql": text}
            if other == "sql"
            else {"dialects": [{"dialect": other, "expression": text}]}
        )
        both.setdefault("dialects", []).append({"dialect": "CLICKHOUSE", "expression": text})
        data["datasets"][0]["fields"].append({"name": "both", "expression": both})

    fields, _ = untranslatable(build(tmp_path, mutate))
    assert "no Ossie SQL expression may call it" in fields["orders.both"]


def with_metric_calls(data):
    data["datasets"][0]["fields"].append(field("bad_field", "metric(amount)"))
    data["datasets"][0]["metrics"].append(metric("bad_metric", "METRIC(SUM(amount))"))
    data["metrics"].append(metric("bad_model_metric", "Metric(orders.via_field)"))


def test_a_call_of_metric_is_refused_in_a_field_and_in_each_kind_of_metric(tmp_path):
    fields, metrics = untranslatable(build(tmp_path, with_metric_calls))
    assert fields == {"orders.bad_field": field_refusal("METRIC(amount)", "metric(amount)")}
    assert metrics == {
        "orders.bad_metric": metric_refusal("METRIC(SUM(amount))", "METRIC(SUM(amount))"),
        "bad_model_metric": metric_refusal("METRIC(orders.via_field)", "Metric(orders.via_field)"),
    }


def test_a_call_of_metric_is_refused_without_system_functions(tmp_path):
    # FakeClickHouse has no system.functions: nothing checks a function outside the spec,
    # so only the refusal of METRIC itself hides these.
    ex = Executor(
        FakeClickHouse(tables({"id", "amount", "status"})), build(tmp_path, with_metric_calls)
    )
    assert ex.functions is None
    assert set(ex.untranslatable_fields) == {"orders.bad_field"}
    assert set(ex.untranslatable_metrics) == {"orders.bad_metric", "bad_model_metric"}
    assert "bad_field" not in {f.name for f in ex.model.datasets[0].fields}
    assert {m.name for m in ex.model.datasets[0].metrics}.isdisjoint({"bad_metric"})
    assert "bad_model_metric" not in {m.name for m in ex.model.metrics}
    msg = refusal(ex.model, "orders.bad_metric")
    assert "METRIC(" not in msg and "SUM(amount)" not in msg


@pytest.mark.parametrize(
    "call", ["METRIC(x)", "metric(x)", "Metric(x)", "METRIC(SUM(x))", "METRIC()"]
)
def test_metric_is_refused_in_every_capitalization_and_never_parsed_as_a_function(call):
    with pytest.raises(ValueError, match="spec reserves"):
        parse(call)
    with pytest.raises(ValueError, match="spec reserves"):
        parse(f"SUM(x) + {call}", question=True)


def test_a_call_of_metric_is_refused_in_a_question(model):
    # An expression of a question and a filter are parsed by the planner too. A question
    # names the model's metrics and fields and defines none, so the advice is to name
    # it (a filter's column may be a field, as orders.raw_amount is), by the call's own
    # argument when it is a single column, and never SUM(.
    for found, advice in (
        (refusal(model, "metric(total) AS bad"), "write total, not METRIC(total)"),
        (
            refusal(model, "total", filters=("METRIC(orders.raw_amount) > 1",)),
            "write orders.raw_amount, not METRIC(orders.raw_amount)",
        ),
        (
            refusal(model, "total", filters=("METRIC(SUM(orders.raw_amount)) > 1",)),
            "write the metric's name, not METRIC(SUM(orders.raw_amount))",
        ),
    ):
        assert "spec reserves" in found
        assert f"A question names a metric or a field itself: {advice}" in found
        assert "SUM(amount)" not in found and "operand of METRIC" not in found


def test_a_dimension_on_a_field_that_calls_metric_is_a_plan_error(tmp_path):
    # The Executor hides such a field; the Planner alone, given the model, refuses it.
    model = build(tmp_path, with_metric_calls)
    msg = refusal(model, "total", dimensions=("orders.bad_field",))
    assert msg.startswith("field orders.bad_field: METRIC(amount) is not allowed; ")
    assert METRIC_RESERVED in msg and "operand of METRIC" not in msg


class WithFunctions(FakeClickHouse):
    """A server whose system.functions lists what ``rows`` say, as (name,
    case_insensitive, is_aggregate)."""

    def __init__(self, tables, rows):
        super().__init__(tables)
        self.rows = rows

    def query(self, sql, parameters=None):
        if "system.functions" in sql:
            return SimpleNamespace(result_rows=self.rows)
        if "aggregate_function_combinators" in sql:
            return SimpleNamespace(result_rows=[("If",)])
        return super().query(sql, parameters)


@pytest.mark.parametrize("is_aggregate", [0, 1])
def test_a_user_defined_function_named_metric_does_not_let_a_call_through(tmp_path, is_aggregate):
    # A server that has a function (a UDF) metric: the reservation still wins, with
    # the spec's reason, and a question that calls it is refused.
    server = WithFunctions(
        tables({"id", "amount", "status"}), [("metric", 0, is_aggregate), ("sum", 1, 1)]
    )
    ex = Executor(server, build(tmp_path, with_metric_calls))
    assert ex.functions is not None and ex.functions.has("metric")
    assert ex.untranslatable_fields == {
        "orders.bad_field": field_refusal("METRIC(amount)", "metric(amount)")
    }
    assert ex.untranslatable_metrics == {
        "orders.bad_metric": metric_refusal("METRIC(SUM(amount))", "METRIC(SUM(amount))"),
        "bad_model_metric": metric_refusal("METRIC(orders.via_field)", "Metric(orders.via_field)"),
    }
    assert "bad_field" not in {f.name for f in ex.model.datasets[0].fields}
    for found in (
        refusal(ex.model, "metric(total) AS t"),
        refusal(ex.model, "total", filters=("metric(orders.status) > 1",)),
    ):
        assert "spec reserves" in found


def clickhouse_text(text: str) -> dict:
    return {"dialects": [{"dialect": "CLICKHOUSE", "expression": text}]}


def with_clickhouse_metric_calls(data):
    # The spec reserves METRIC in OSSIE_SQL_2026 only, "not in vendor dialects or in SQL
    # engines": a CLICKHOUSE text may call a server function of that name.
    data["datasets"][0]["fields"].append(
        {"name": "ch_field", "expression": clickhouse_text("metric(amount)")}
    )
    data["datasets"][0]["metrics"].append(
        {"name": "ch_metric", "expression": clickhouse_text("SUM(metric(amount))")}
    )
    data["metrics"].append({"name": "ch_model", "expression": clickhouse_text("SUM(metric(1))")})


COLUMNS = {"id", "amount", "status"}


def test_a_call_of_metric_in_the_clickhouse_dialect_runs_when_the_server_has_it(tmp_path):
    server = WithFunctions(tables(COLUMNS), [("metric", 0, 0), ("sum", 1, 1)])
    model = build(tmp_path, with_clickhouse_metric_calls)
    assert untranslatable(model) == ({}, {})
    ex = Executor(server, model)
    assert ex.untranslatable_fields == ex.untranslatable_metrics == {}
    assert "ch_field" in {f.name for f in ex.model.datasets[0].fields}
    sent = ex.planner.sql(Query(("orders.ch_metric", "ch_model"), dimensions=("orders.ch_field",)))
    assert "SUM(metric(orders.amount * 100))" in sent and "metric(orders.amount) AS" in sent
    assert "metric(1)" in sent


@pytest.mark.parametrize("known", [None, []])
def test_a_call_of_metric_in_the_clickhouse_dialect_passes_without_system_functions(
    tmp_path, known
):
    # Nothing checks a function outside the spec without system.functions.
    server = (
        FakeClickHouse(tables(COLUMNS)) if known is None else WithFunctions(tables(COLUMNS), known)
    )
    ex = Executor(server, build(tmp_path, with_clickhouse_metric_calls))
    assert ex.functions is None and ex.untranslatable_fields == {}


def test_a_call_of_metric_in_the_clickhouse_dialect_is_hidden_when_the_server_lacks_it(tmp_path):
    server = WithFunctions(tables(COLUMNS), [("sum", 1, 1), ("matches", 0, 0)])
    ex = Executor(server, build(tmp_path, with_clickhouse_metric_calls))
    assert set(ex.untranslatable_fields) == {"orders.ch_field"}
    assert set(ex.untranslatable_metrics) == {"orders.ch_metric", "ch_model"}
    for why in (*ex.untranslatable_fields.values(), *ex.untranslatable_metrics.values()):
        assert why.startswith("calls ") and "a function this ClickHouse server does not have" in why
        assert "spec reserves" not in why
    assert "ch_field" not in {f.name for f in ex.model.datasets[0].fields}
    assert "ch_model" not in {m.name for m in ex.model.metrics}


def test_an_aggregate_function_named_metric_in_the_clickhouse_dialect(tmp_path):
    # A server aggregate `metric`: a CLICKHOUSE dataset metric calls it, a model metric
    # evaluates that metric with AGG, and a field with the same text aggregates outside
    # a window, so it is hidden as any aggregate in a field is.
    def mutate(data):
        data["datasets"][0]["metrics"].append(
            {"name": "ch_agg", "expression": clickhouse_text("metric(amount)")}
        )
        data["datasets"][0]["fields"].append(
            {"name": "ch_field", "expression": clickhouse_text("metric(amount)")}
        )
        data["metrics"].append(metric("via_agg", "AGG(orders.ch_agg) + 1"))

    server = WithFunctions(tables(COLUMNS), [("metric", 0, 1), ("sum", 1, 1)])
    ex = Executor(server, build(tmp_path, mutate))
    assert set(ex.untranslatable_fields) == {"orders.ch_field"}
    assert ex.untranslatable_fields["orders.ch_field"].startswith(FIELD_AGGREGATE_RULE)
    assert ex.untranslatable_metrics == {}
    assert "metric(orders.amount * 100) AS" in ex.planner.sql(Query(("orders.ch_agg",)))
    assert "metric(orders.amount * 100) + 1 AS via_agg" in ex.planner.sql(Query(("via_agg",)))


def test_a_call_of_metric_in_ossie_sql_stays_refused_beside_a_clickhouse_one(tmp_path):
    def mutate(data):
        with_clickhouse_metric_calls(data)
        with_metric_calls(data)

    fields, metrics = untranslatable(build(tmp_path, mutate))
    assert set(fields) == {"orders.bad_field"}
    assert set(metrics) == {"orders.bad_metric", "bad_model_metric"}
    assert all("spec reserves" in why for why in (*fields.values(), *metrics.values()))


@pytest.mark.parametrize(
    "bad", ["METRIC(SUM(amount))", "SUM(amount", "REGEXP_COUNT(status, 'e', 1, 'i')"]
)
def test_a_metric_over_a_dataset_metric_that_does_not_parse_is_not_reported_again(
    tmp_path, capsys, bad
):
    # untranslatable() reports the dataset's metric; the model's metric that evaluates it
    # with AGG has no problem of its own, as one that reads an untranslatable field has none.
    def mutate(data):
        data["datasets"][0]["metrics"].append(metric("bad_metric", bad))
        data["metrics"].append(metric("via_agg", "AGG(orders.bad_metric) + 1"))
        data["metrics"].append(
            metric("also_via_agg", "AGG(orders.bad_metric) / AGG(orders.via_field)")
        )

    model = build(tmp_path, mutate)
    assert set(untranslatable(model)[1]) == {"orders.bad_metric"}
    assert unanswerable_metrics(model) == {}
    assert main(["validate", str(tmp_path / "m.yaml")]) == 1
    err = capsys.readouterr().err
    assert "metric 'orders.bad_metric'" in err and "via_agg" not in err
    assert "1 problem(s)" in err


def metric_named_metric(data):
    # A field `metric` of orders (a name is unique within a dataset), a metric `metric`
    # of the model and of a second dataset.
    data["datasets"][0]["fields"].append(field("metric", "amount"))
    data["datasets"][0]["metrics"].append(metric("metric_sum", "SUM(metric)"))
    data["datasets"].append(
        {
            "name": "other",
            "source": "ossie_srccol.orders",
            "primary_key": ["id"],
            "fields": [field("id", "id"), field("amount", "amount")],
            "metrics": [metric("metric", "SUM(amount)")],
        }
    )
    data["metrics"].append(metric("metric", "SUM(orders.metric)"))


def test_a_field_and_a_metric_named_metric_are_no_calls(tmp_path):
    model = build(tmp_path, metric_named_metric)
    assert untranslatable(model) == ({}, {})
    assert "SUM(orders.amount) AS metric FROM" in sql(model, "metric")
    assert 'SUM(other.amount) AS "other.metric" FROM' in sql(model, "other.metric")
    by_field = sql(model, "orders.metric_sum", dimensions=("orders.metric",))
    assert "SELECT orders.amount AS metric, SUM(orders.amount)" in by_field
    assert parse("SUM(metric)").sql() == "SUM(metric)"
    assert parse("orders.metric > 1").sql() == "orders.metric > 1"


def test_values_of_a_field_and_a_metric_named_metric(clickhouse, tmp_path):
    for stmt in SETUP:
        clickhouse.command(stmt)
    try:
        ex = Executor(clickhouse, build(tmp_path, metric_named_metric))
        assert ex.untranslatable_fields == ex.untranslatable_metrics == {}
        # Hand-computed from the four rows of SETUP: 100 + 50 + 200 + 20.
        for name in ("orders.metric_sum", "metric", "other.metric"):
            assert ex.execute(Query((name,))).rows == [(370,)], name
        rows = ex.execute(Query(("orders.metric_sum",), ("orders.status",))).rows
        assert sorted(rows) == [("completed", 170), ("pending", 200)]
    finally:
        clickhouse.command("DROP DATABASE ossie_srccol")

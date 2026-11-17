"""Expressions in the CLICKHOUSE dialect (docs/decisions/clickhouse-dialect.md).

A text written for ClickHouse is read where the SQL sent to ClickHouse is the text
as written, give or take the model's time zone for ClickHouse's clocks, a division
made Float64 and spellings that change nothing; anything else is refused with what
to write instead. The formulas are this project's own, of the kinds found in public
ClickHouse projects."""

import copy
from pathlib import Path

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse._vendor.ossie import OssieDialectExpression, OssieExpression
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.model import check_model
from dactopus_ossie_clickhouse.planner import Planner, Query, unanswerable_metrics
from dactopus_ossie_clickhouse.translate import clickhouse_problem, pick_expression, untranslatable
from tests.conftest import FakeClickHouse


def ch(text: str) -> dict:
    return {"dialects": [{"dialect": "CLICKHOUSE", "expression": text}]}


def sql(text: str) -> dict:
    return {"sql": text}


def named(name: str, expression: dict) -> dict:
    return {"name": name, "expression": expression}


BASE = {
    "version": "0.3.0.dev0",
    "name": "clickhouse_dialect",
    "datasets": [
        {
            "name": "orders",
            "source": "ossie_ch_dialect.orders",
            "primary_key": ["id"],
            "fields": [
                named("id", sql("id")),
                named("customer_id", sql("customer_id")),
                named("status", sql("status")),
                named("amount", sql("amount")),
                named("created_at", sql("created_at")),
                named("month", ch("toStartOfMonth(created_at)")),
                named("size", ch("if(amount >= 100, 'large', 'small')")),
                named("status_length", sql("LENGTH(status)")),
            ],
            "metrics": [
                named("paid", ch("countIf(status = 'paid')")),
                named("paid_amount", ch("sumIf(amount, status = 'paid')")),
                named("buyers", ch("uniqExact(customer_id)")),
                named("revenue", sql("SUM(amount)")),
                # A metric in sql over a field in CLICKHOUSE, and the other way round.
                named("large_orders", sql("COUNT(*) FILTER (WHERE size = 'large')")),
                named("longest_status", ch("max(status_length)")),
            ],
        }
    ],
    "metrics": [
        # Decimal over Decimal: ClickHouse alone would keep the dividend's scale (0.42);
        # the division is made Float64, as in sql (division-to-float64.md).
        named(
            "paid_share", ch("sumIf(orders.amount, orders.status = 'paid') / sum(orders.amount)")
        ),
        named("age_days", ch("max(dateDiff('day', orders.created_at, now()))")),
    ],
}


def build(tmp_path: Path, mutate=lambda data: None, **kw):
    data = copy.deepcopy(BASE)
    mutate(data)
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_model(path, **kw)


@pytest.fixture
def model(tmp_path):
    return build(tmp_path)


def plan(model, *metrics: str, **kw) -> str:
    return Planner(model).sql(Query(metrics, **kw))


def with_field(tmp_path, expression: dict):
    def mutate(data):
        data["datasets"][0]["fields"].append(named("f", expression))
        data["datasets"][0]["metrics"].append(named("m", sql("COUNT(DISTINCT f)")))

    return build(tmp_path, mutate)


# --- which text is read ---------------------------------------------------------


def dialects(text: str | None = None, **kw) -> OssieExpression:
    entries = [OssieDialectExpression(dialect=k, expression=v) for k, v in kw.items()]
    return OssieExpression(sql=text, dialects=entries or None)


def test_clickhouse_comes_after_sql_and_the_portable_dialects():
    assert pick_expression(dialects(CLICKHOUSE="c")) == "c"
    assert pick_expression(dialects("s", CLICKHOUSE="c")) == "s"
    assert pick_expression(dialects(ANSI_SQL="a", CLICKHOUSE="c")) == "a"
    assert pick_expression(dialects(OSSIE_SQL_2026="o", CLICKHOUSE="c")) == "o"
    # The text read is checked; one that would be refused does not stop sql.
    assert pick_expression(dialects("LENGTH(s)", CLICKHOUSE="length(s)")) == "LENGTH(s)"


def test_a_model_written_for_clickhouse_loads_and_answers_as_written(model):
    assert check_model(model) == []
    assert untranslatable(model) == ({}, {})
    assert unanswerable_metrics(model) == {}
    assert "countIf(orders.status = 'paid') AS" in plan(model, "orders.paid")
    assert "sumIf(orders.amount, orders.status = 'paid') AS" in plan(model, "orders.paid_amount")
    assert "uniqExact(orders.customer_id) AS" in plan(model, "orders.buyers")
    by_month = plan(model, "orders.paid", dimensions=("orders.month",))
    assert "toStartOfMonth(orders.created_at) AS month" in by_month


def test_a_division_and_the_clock_are_the_allowed_differences(model):
    assert "(sumIf(orders.amount, orders.status = 'paid') * 1.0) / SUM(orders.amount)" in plan(
        model, "paid_share"
    )
    assert "dateDiff('day', orders.created_at, now('UTC'), 'UTC')" in plan(model, "age_days")


def test_the_clock_is_the_models_time_zone(tmp_path):
    model = build(tmp_path, time_zone="Europe/Berlin")
    assert "now('Europe/Berlin'), 'Europe/Berlin')" in plan(model, "age_days")


def test_fields_and_metrics_of_the_two_dialects_combine(model):
    # A field in CLICKHOUSE, if() printed as CASE, under a metric in sql.
    assert (
        "COUNT(*) FILTER(WHERE CASE WHEN orders.amount >= 100 "
        "THEN 'large' ELSE 'small' END = 'large')" in plan(model, "orders.large_orders")
    )
    # A field in sql, read by the spec, under a metric in CLICKHOUSE.
    assert "MAX(CHAR_LENGTH(orders.status))" in plan(model, "orders.longest_status")


def test_the_same_text_as_sql_is_read_as_before(tmp_path):
    for text, sent in [
        ("length(status)", "CHAR_LENGTH(orders.status)"),
        ("position(status, 'a')", "POSITION('a', orders.status)"),
    ]:
        model = with_field(tmp_path, sql(text))
        assert untranslatable(model) == ({}, {})
        assert f"COUNT(DISTINCT {sent})" in plan(model, "orders.m")


# --- what is refused, and what is not -------------------------------------------

REFUSED = [
    ("tags[1]", "an array index"),
    ("position(status, 'a')", "`position(haystack, needle)`"),
    ("length(status)", "`length` counts characters"),
    ("round(amount)", "`round`"),
    ("round(amount, 2)", "`round`"),
    ("amount % 7", "`%`"),
    ("median(amount)", "`median`"),
    ("sum(amount) OVER (ORDER BY created_at DESC)", "a window's `ORDER BY ... DESC`"),
    ("replaceRegexpAll(status, '\\\\d+', '')", "a backslash in a string"),
    ("CAST('2026-01-01 00:00:00' AS DateTime)", "a string cast to a time"),
    ("`order id`", "a name in backticks"),
    ("CAST(status AS LowCardinality(String))", "a type of ClickHouse's own"),
    ("dayofyear(created_at)", "would send toDayOfYear(created_at) instead"),
    # Ossie SQL refuses these, and the refusal says so.
    ("quantile(0.9)(amount)", "read as Ossie SQL: ClickHouse's parametric call"),
    ("dateDiff('day', created_at, now(), 'UTC')", "read as Ossie SQL: cannot parse"),
]


@pytest.mark.parametrize(("text", "construct"), REFUSED)
def test_a_text_clickhouse_reads_otherwise_is_refused_by_name(tmp_path, text, construct):
    model = with_field(tmp_path, ch(text))
    fields, metrics = untranslatable(model)
    assert construct in fields["orders.f"]
    assert fields["orders.f"].endswith(f": {text!r}")
    assert metrics == {}  # m reads the field; the executor hides it with the field
    assert fields["orders.f"].startswith("written for ClickHouse")


def test_a_refused_field_is_hidden_and_named_by_the_executor(tmp_path):
    model = with_field(tmp_path, ch("length(status)"))
    columns = {"id", "customer_id", "status", "amount", "created_at"}
    tables = {("ossie_ch_dialect", "orders"): ("MergeTree", "MergeTree", ("", ""), columns)}
    ex = Executor(FakeClickHouse(tables), model)
    assert "f" not in {f.name for f in ex.model.datasets[0].fields}
    assert "m" not in {m.name for m in ex.model.datasets[0].metrics}
    assert "`length` counts characters" in ex.untranslatable_fields["orders.f"]


def test_a_refusal_says_what_to_write_instead():
    for text, instead in [
        ("length(s)", "lengthUTF8(s)"),
        ("position(s, 'a')", "position('a' IN s)"),
        ("tags[1]", "arrayElement(tags, 1)"),
        ("round(x, 2)", "roundBankers(x, 2)"),
        ("CAST(s AS LowCardinality(String))", "toLowCardinality(s)"),
        ("dayofyear(d)", "toDayOfYear(d)"),
        ("a % b", "modulo(a, b)"),
        ("sum(x) OVER (ORDER BY d DESC)", "sum(x) OVER (ORDER BY d DESC NULLS LAST)"),
        ("`order id`", '"order id"'),
    ]:
        assert clickhouse_problem(text) is not None
        assert clickhouse_problem(instead) is None, instead


@pytest.mark.parametrize(
    "text",
    [
        "position(status IN 'a') > 0",
        "round(amount, digits) > 0",
        "match(status, '\\d+')",
    ],
)
def test_a_refusal_names_only_a_construct_read_otherwise(text):
    # Each is accepted alone; refused for dayofyear, the refusal does not name it.
    assert clickhouse_problem(text) is None
    problem = clickhouse_problem(f"{text} AND dayofyear(created_at) = 1")
    assert "would send" in problem and "toDayOfYear(created_at)" in problem


def test_a_backtick_in_a_string_is_not_a_name_in_backticks():
    problem = clickhouse_problem("status = '`' AND amount // 2 = 1")
    assert "read as Ossie SQL: cannot parse" in problem


ACCEPTED = [
    # Sent as written.
    "countIf(status = 'paid')",
    "sumIf(amount, status = 'paid')",
    "uniqExact(customer_id)",
    "toStartOfMonth(created_at)",
    "multiIf(amount > 100, 'large', amount > 10, 'medium', 'small')",
    "dictGet('customers', 'region', customer_id)",
    "date_diff('day', created_at, shipped_at)",
    "toDateTime('2026-01-01 00:00:00')",
    "arrayMap(x -> x * 2, tags)",
    "status IN ('paid', 'refunded') AND amount > 0",
    "coalesce(discount, 0)",
    "lower(status)",
    # The model's time zone.
    "now()",
    "today()",
    "yesterday()",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "dateDiff('day', created_at, today())",
    # Spellings that change nothing.
    "if(amount > 0, amount, 0)",
    "discount IS NOT NULL",
    "created_at - INTERVAL 1 DAY",
    "CAST(amount AS Float64)",
    "CAST(tags AS Array(String))",
    "amount::Float64",
    "(amount > 0 AND (status = 'paid'))",
    "SUM(amount)",
    "sum(amount)",
    # A division made Float64.
    "sum(amount) / count()",
    "sumIf(amount, status = 'paid') / nullIf(sum(amount), 0) * 100",
]


@pytest.mark.parametrize("text", ACCEPTED)
def test_an_allowed_difference_is_not_refused(text):
    assert clickhouse_problem(text) is None


# --- values, against ClickHouse -------------------------------------------------

SETUP = [
    "DROP DATABASE IF EXISTS ossie_ch_dialect",
    "CREATE DATABASE ossie_ch_dialect",
    "CREATE TABLE ossie_ch_dialect.orders (id Int32, customer_id Int32, status String, "
    "amount Decimal(10, 2), created_at DateTime('UTC')) ENGINE = MergeTree ORDER BY id",
    "INSERT INTO ossie_ch_dialect.orders VALUES "
    "(1, 1, 'paid', 100.00, '2026-01-05 10:00:00'), "
    "(2, 1, 'paid', 50.00, '2026-01-20 10:00:00'), "
    "(3, 2, 'refunded', 200.00, '2026-02-03 10:00:00'), "
    "(4, 3, 'paid', 20.00, '2026-02-10 10:00:00'), "
    "(5, 3, 'pending', 30.00, '2026-02-11 10:00:00'), "
    "(6, 3, 'pending', 30.00, '2026-03-02 10:00:00')",
]


@pytest.fixture
def ex(clickhouse, model):
    for stmt in SETUP:
        clickhouse.command(stmt)
    yield Executor(clickhouse, model)
    clickhouse.command("DROP DATABASE ossie_ch_dialect")


def test_values_of_a_model_written_for_clickhouse(ex):
    assert ex.check() == []

    def total(name: str):
        return ex.execute(Query((name,))).rows[0][0]

    def by_month(name: str) -> dict:
        rows = ex.execute(Query((name,), ("orders.month",))).rows
        return {month.isoformat(): value for month, value in rows}

    # Hand-computed from the six orders.
    assert total("orders.paid") == 3
    assert total("orders.paid_amount") == 170
    assert total("orders.buyers") == 3
    assert total("orders.large_orders") == 2
    assert total("orders.longest_status") == 8  # refunded
    assert total("paid_share") == pytest.approx(170 / 430)
    jan, feb, mar = "2026-01-01", "2026-02-01", "2026-03-01"
    assert by_month("orders.paid") == {jan: 2, feb: 1, mar: 0}
    assert by_month("orders.buyers") == {jan: 1, feb: 2, mar: 1}
    # No paid order in March: sumIf over no matching rows is NULL, as the spec's SUM
    # with FILTER is, where ClickHouse alone gives 0; countIf and uniqExact stay 0.
    assert by_month("orders.paid_amount") == {jan: 150, feb: 20, mar: None}
    shares = by_month("paid_share")
    assert shares[mar] is None
    assert {jan: shares[jan], feb: shares[feb]} == pytest.approx({jan: 1.0, feb: 20 / 250})

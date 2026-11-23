"""A field's expression is scalar: no aggregate outside a window.

Spec, spec.md "Fields" and "Expression Object"; apache/ossie#792 checks it in
validate_field_aggregates, whose bounds these tests follow: a windowed aggregate is
allowed, one in a window's PARTITION BY or ORDER BY or inside the windowed call is
not. Sent as written, a filter on such a field became a condition on groups
(docs/decisions/field-aggregates.md)."""

import copy

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from dactopus_ossie_clickhouse.translate import FIELD_AGGREGATE_RULE, untranslatable

BASE = {
    "version": "0.3.0.dev0",
    "name": "fields",
    "datasets": [
        {
            "name": "orders",
            "source": "t.orders",
            "primary_key": ["id"],
            "fields": [
                {"name": "id", "expression": {"sql": "id"}},
                {"name": "country", "expression": {"sql": "country"}},
                {"name": "amount", "expression": {"sql": "amount"}},
            ],
        }
    ],
    "metrics": [{"name": "revenue", "expression": {"sql": "SUM(orders.amount)"}}],
}


def build(tmp_path, *fields: tuple[str, dict]):
    data = copy.deepcopy(BASE)
    data["datasets"][0]["fields"] += [{"name": n, "expression": e} for n, e in fields]
    path = tmp_path / "m.ossie.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_model(path)


AGGREGATES = [
    "SUM(amount)",
    "amount - AVG(amount)",
    "COUNT(*) FILTER (WHERE amount > 1)",
    "APPROX_PERCENTILE(amount, 0.5)",
    "APPROX_COUNT_DISTINCT(id)",
    "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount)",
    "SUM(SUM(amount)) OVER ()",
    "SUM(amount) OVER (PARTITION BY MAX(country))",
    "RANK() OVER (ORDER BY SUM(amount))",
]
WINDOWS = [
    "amount / SUM(amount) OVER ()",
    "SUM(amount) OVER (PARTITION BY country)",
    "SUM(amount) FILTER (WHERE amount > 1) OVER ()",
    "LAG(amount) OVER (ORDER BY id)",
    "RANK() OVER (ORDER BY amount DESC)",
    "SUM(amount) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
]


@pytest.mark.parametrize("text", AGGREGATES)
def test_a_field_that_aggregates_does_not_translate(tmp_path, text):
    fields, metrics = untranslatable(build(tmp_path, ("f", {"sql": text})))
    assert fields == {"orders.f": f"{FIELD_AGGREGATE_RULE}: {text!r}"}
    assert metrics == {}


@pytest.mark.parametrize("text", WINDOWS)
def test_a_window_in_a_field_translates(tmp_path, text):
    assert untranslatable(build(tmp_path, ("f", {"sql": text}))) == ({}, {})


@pytest.mark.parametrize(
    "dialects",
    [
        [{"dialect": "OSSIE_SQL_2026", "expression": "SUM(amount)"}],
        [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}],
        [{"dialect": "CLICKHOUSE", "expression": "sum(amount)"}],
    ],
)
def test_every_dialect_is_checked(tmp_path, dialects):
    # The rule is about the SQL sent, which any dialect's text becomes.
    fields, _ = untranslatable(build(tmp_path, ("f", {"dialects": dialects})))
    assert list(fields) == ["orders.f"]


# --- the planner, without an executor to hide the field -------------------------


@pytest.mark.parametrize("text", ["SUM(amount)", "countIf(amount > 1)"])
def test_the_planner_refuses_a_field_that_aggregates(tmp_path, text):
    # countIf is an aggregate by SQLGlot's list of ClickHouse aggregates, which the
    # planner reads without a server.
    planner = Planner(build(tmp_path, ("f", {"sql": text})))
    message = f"field orders.f: {FIELD_AGGREGATE_RULE}: {text!r}"
    for q in (
        Query(("revenue",), ("orders.f",)),
        # was HAVING SUM(orders.amount) > 15: a condition on each country's total
        Query(("revenue",), ("orders.country",), ("orders.f > 15",)),
    ):
        with pytest.raises(PlanError) as e:
            planner.sql(q)
        assert str(e.value) == message


def test_a_metric_over_a_field_that_aggregates_is_unanswerable(tmp_path):
    model = build(tmp_path, ("total", {"sql": "SUM(amount)"}))
    data = model.model_dump(by_alias=True, exclude_none=True)
    data["metrics"].append({"name": "m", "expression": {"sql": "SUM(orders.total)"}})
    model = type(model).model_validate(data)
    assert "m" in unanswerable_metrics(model)


@pytest.mark.parametrize(
    "q",
    [
        Query(("revenue",), ("orders.f",)),
        Query(("revenue",), ("orders.country",), ("orders.f > 0.1",)),
    ],
)
def test_the_planner_refuses_a_window_field_by_name(tmp_path, q):
    # ClickHouse refuses a window in GROUP BY or WHERE (ILLEGAL_AGGREGATION).
    planner = Planner(build(tmp_path, ("f", {"sql": "amount / SUM(amount) OVER ()"})))
    with pytest.raises(PlanError) as e:
        planner.sql(q)
    assert str(e.value) == (
        "field orders.f is a window function, which this version cannot group, filter or "
        "aggregate by: 'amount / SUM(amount) OVER ()'"
    )


def test_a_scalar_field_plans_as_before(tmp_path):
    planner = Planner(build(tmp_path, ("big", {"sql": "amount > 15"})))
    sql = planner.sql(Query(("revenue",), ("orders.country",), ("orders.big",)))
    assert "WHERE orders.amount > 15" in sql and "HAVING" not in sql

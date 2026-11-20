"""Expressions over metrics in a question: ``100 * returns / sales AS return_pct``.

docs/decisions/expressions-over-metrics.md. The value is the one a model's metric would
have whose expression is this one, with each dataset's metric written ``AGG(dataset.metric)``
and each model's metric replaced by its own expression. So every model metric below has a
twin written out, and the two must answer alike; the values are also computed by hand from
the data of tests/test_stitch.py: two facts (orders, returns) over one dimension (customers),
with an orphan of each fact and a customer with returns and no orders.

    region  orders         returns
    EAST    350 (3 rows)   10
    WEST     75 (1 row)     5
    NORTH   none           15
    NULL     30 (orphan)    7
"""

import copy
import json

import pytest
import yaml
from sqlglot import exp

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.access import Hidden, restrict
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.mcp_server import build_server, instructions
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query
from tests.conftest import server_url
from tests.test_model import FIXTURE as TPCDS
from tests.test_stitch import COLUMNS, ROWS


def expr(text: str) -> dict:
    return {"sql": text}


def field(name: str) -> dict:
    return {"name": name, "expression": expr(name)}


def metric(name: str, text: str) -> dict:
    return {"name": name, "expression": expr(text)}


BASE = {
    "version": "0.3.0.dev0",
    "name": "expressions",
    "datasets": [
        {
            "name": "customers",
            "source": "ossie_expr.customers",
            "primary_key": ["id"],
            "fields": [field("id"), field("region"), field("segment")],
        },
        {
            "name": "orders",
            "source": "ossie_expr.orders",
            "primary_key": ["id"],
            "fields": [
                field("id"),
                field("customer_id"),
                field("amount"),
                field("status"),
                {**field("placed_at"), "datatype": "DateTime"},
            ],
            "metrics": [
                metric("revenue", "SUM(amount)"),
                metric("n", "COUNT(*)"),
                metric("avg_order", "SUM(amount) / NULLIF(COUNT(*), 0)"),
            ],
        },
        {
            "name": "returns",
            "source": "ossie_expr.returns",
            "primary_key": ["id"],
            "fields": [field("id"), field("customer_id"), field("amount")],
            "metrics": [metric("refunds", "SUM(amount)")],
        },
    ],
    "relationships": [
        {
            "name": "orders_to_customers",
            "from": "orders",
            "to": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
        },
        {
            "name": "returns_to_customers",
            "from": "returns",
            "to": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
        },
    ],
    "metrics": [
        metric("total_revenue", "SUM(orders.amount)"),
        metric("total_returns", "SUM(returns.amount)"),
        metric("order_count", "COUNT(orders.id)"),
        metric("customer_count", "COUNT(DISTINCT customers.id)"),
        # Split across the two facts, as a model's metric on its own is.
        metric("return_rate", "SUM(returns.amount) / SUM(orders.amount)"),
        metric("net_revenue", "SUM(orders.amount) - SUM(returns.amount)"),
        metric("rank_revenue", "RANK() OVER (ORDER BY SUM(orders.amount) DESC)"),
        metric(
            "completed_revenue", "SUM(orders.amount) FILTER (WHERE orders.status = 'completed')"
        ),
        metric("avg_per_order", "SUM(orders.amount) / COUNT(orders.id)"),
        metric("clock", "CURRENT_TIMESTAMP"),  # no aggregate
        # Twins: the expressions of the questions below, written as a model's metric.
        metric("twin_ratio", "SUM(returns.amount) / SUM(orders.amount)"),
        metric("twin_pct", "100 * SUM(returns.amount) / SUM(orders.amount)"),
        metric("twin_gap", "SUM(orders.amount) - SUM(returns.amount)"),
        metric("twin_agg_ratio", "AGG(returns.refunds) / AGG(orders.revenue)"),
        metric("twin_avg", "AGG(orders.revenue) / AGG(orders.n)"),
        metric("twin_plus", "SUM(returns.amount) + COUNT(orders.id)"),
        metric("twin_double_net", "2 * (SUM(orders.amount) - SUM(returns.amount))"),
        metric("twin_inverse_net", "1 / (SUM(orders.amount) - SUM(returns.amount))"),
        metric("twin_net_share", "(SUM(orders.amount) - SUM(returns.amount)) / SUM(orders.amount)"),
        metric("twin_rate_pct", "100 * (SUM(returns.amount) / SUM(orders.amount))"),
        metric(
            "twin_completed_share",
            "SUM(orders.amount) FILTER (WHERE orders.status = 'completed') / SUM(orders.amount)",
        ),
        metric(
            "twin_completed_big",
            "SUM(orders.amount) FILTER (WHERE orders.status = 'completed' AND orders.amount > 60)"
            " / SUM(orders.amount)",
        ),
        metric(
            "twin_avg_completed",
            "(SUM(orders.amount) FILTER (WHERE orders.status = 'completed') / "
            "COUNT(orders.id) FILTER (WHERE orders.status = 'completed')) / "
            "(SUM(orders.amount) / COUNT(orders.id))",
        ),
        metric(
            "twin_retail_share",
            "SUM(orders.amount) FILTER (WHERE customers.segment = 'retail') / SUM(orders.amount)",
        ),
        metric(
            "twin_retail_rate",
            "(SUM(returns.amount) FILTER (WHERE customers.segment = 'retail') / "
            "SUM(orders.amount) FILTER (WHERE customers.segment = 'retail')) / "
            "(SUM(returns.amount) / SUM(orders.amount))",
        ),
        metric("twin_per_customer", "SUM(orders.amount) / COUNT(DISTINCT customers.id)"),
    ],
}

# The rows are those of tests/test_stitch.py. This file adds one column, when each order
# was placed, for a condition on a time field.
PLACED_AT = {
    101: "2026-01-10 09:00:00",
    102: "2026-02-15 10:00:00",
    103: "2026-02-20 11:00:00",
    104: "2026-03-05 12:00:00",
    105: "2026-03-10 13:00:00",
}


def setup() -> list[str]:
    statements = ["DROP DATABASE IF EXISTS ossie_expr", "CREATE DATABASE ossie_expr"]
    for table, (columns, _) in COLUMNS.items():
        placed = ", placed_at DateTime('UTC')" if table == "orders" else ""
        statements.append(
            f"CREATE TABLE ossie_expr.{table} ({columns}{placed}) ENGINE = MergeTree ORDER BY id"
        )
        if table != "orders":
            statements.append(f"INSERT INTO ossie_expr.{table} VALUES {ROWS[table]}")
    times = ", ".join(f"({order}, '{at}')" for order, at in PLACED_AT.items())
    statements.append(
        "INSERT INTO ossie_expr.orders SELECT o.*, toDateTime(p.at, 'UTC') "
        f"FROM values('{COLUMNS['orders'][0]}', {ROWS['orders']}) AS o "
        f"JOIN values('id Int32, at String', {times}) AS p ON o.id = p.id"
    )
    return statements


@pytest.fixture(scope="module")
def model_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("expressions") / "model.yaml"
    path.write_text(yaml.safe_dump(copy.deepcopy(BASE), sort_keys=False))
    return path


@pytest.fixture(scope="module")
def model(model_path):
    return load_model(model_path)


@pytest.fixture(scope="module")
def planner(model):
    return Planner(model)


@pytest.fixture(scope="module")
def ex(clickhouse, model):
    for stmt in setup():
        clickhouse.command(stmt)
    yield Executor(clickhouse, model)
    clickhouse.command("DROP DATABASE ossie_expr")


def rows(ex, *metrics: str, **kw) -> dict:
    """The rows of a question by their dimensions (one dimension, or none)."""
    got = ex.execute(Query(metrics, **kw)).rows
    return {(r[0] if len(r) > 1 else None): r[-1] for r in got}


def region(ex, *metrics: str, **kw) -> dict:
    return rows(ex, *metrics, dimensions=("customers.region",), **kw)


def total(ex, *metrics: str, **kw):
    return ex.execute(Query(metrics, **kw)).rows[0][-1]


def refusal(planner, *metrics: str, **kw) -> str:
    with pytest.raises(PlanError) as e:
        planner.sql(Query(metrics, **kw))
    return str(e.value)


# --- values -------------------------------------------------------------------------------


def test_a_ratio_of_two_metrics_of_one_dataset(ex):
    got = region(ex, "orders.revenue / orders.n AS avg_amount")
    assert got == pytest.approx({"EAST": 350 / 3, "WEST": 75, None: 30})
    assert region(ex, "twin_avg") == pytest.approx(got)
    assert total(ex, "total_revenue / order_count AS avg_amount") == pytest.approx(91)


def test_a_ratio_of_metrics_of_two_datasets(ex):
    # NORTH has returns and no orders: the denominator over no rows is NULL, not 0.
    want = {"EAST": 10 / 350, "WEST": 5 / 75, "NORTH": None, None: 7 / 30}
    assert region(ex, "total_returns / total_revenue AS r") == pytest.approx(want)
    assert region(ex, "twin_ratio") == pytest.approx(want)
    assert total(ex, "total_returns / total_revenue AS r") == pytest.approx(37 / 455)


def test_dataset_metrics_of_two_datasets_are_the_model_metric_with_agg(ex):
    for dimensions in [(), ("customers.region",), ("customers.segment",)]:
        got = rows(ex, "returns.refunds / orders.revenue AS r", dimensions=dimensions)
        want = rows(ex, "twin_agg_ratio", dimensions=dimensions)
        assert got == pytest.approx(want), dimensions
    assert total(ex, "returns.refunds / orders.revenue AS r") == pytest.approx(37 / 455)


def test_the_sql_of_a_question_expression_is_that_of_the_model_metric(planner):
    for item, twin in [
        ("total_returns / total_revenue AS twin_ratio_q", "twin_ratio"),
        ("returns.refunds / orders.revenue AS twin_agg_ratio_q", "twin_agg_ratio"),
        ("100 * total_returns / total_revenue AS twin_pct_q", "twin_pct"),
    ]:
        name = item.rpartition(" AS ")[2]
        for dimensions in [(), ("customers.region",)]:
            got = planner.sql(Query((item,), dimensions)).replace(name, twin)
            assert got == planner.sql(Query((twin,), dimensions)), item


def test_a_model_metric_that_is_split_across_datasets_is_an_operand(ex):
    # return_rate divides the returns' sum by the orders' on their own rows: the expression
    # is evaluated over the values of its components.
    pct = region(ex, "return_rate * 100 AS pct")
    assert pct == pytest.approx(
        {"EAST": 100 * 10 / 350, "WEST": 100 * 5 / 75, "NORTH": None, None: 100 * 7 / 30}
    )
    assert pct == pytest.approx(region(ex, "twin_rate_pct"))
    # Beside a metric of one of its datasets.
    assert total(ex, "return_rate / total_revenue AS x") == pytest.approx(37 / 455 / 455)


def test_a_difference_a_percentage_and_a_minus(ex):
    assert region(ex, "total_revenue - total_returns AS gap") == pytest.approx(
        {"EAST": 340, "WEST": 70, "NORTH": None, None: 23}
    )
    assert region(ex, "total_revenue - total_returns AS gap") == pytest.approx(
        region(ex, "twin_gap")
    )
    assert total(ex, "100 * total_returns / total_revenue AS pct") == pytest.approx(100 * 37 / 455)
    assert region(ex, "100 * total_returns / total_revenue AS pct") == pytest.approx(
        region(ex, "twin_pct")
    )
    assert total(ex, "-total_revenue AS neg") == -455
    assert total(ex, "-total_revenue + total_returns AS neg") == -418
    assert total(ex, "-(total_revenue - total_returns) AS neg") == -418
    assert total(ex, "total_revenue - -total_returns AS plus") == 492
    assert total(ex, "1.5 * order_count AS n") == pytest.approx(7.5)


def test_a_sum_or_a_difference_under_a_product_or_a_quotient_keeps_its_parentheses(ex):
    # net_revenue is a difference, split across two datasets: 455 - 37 = 418. Regrouped, the
    # three would be 2 * 455 - 37 = 873, 1 / 455 - 37 and 455 / 455 - 37 = -36.
    assert total(ex, "2 * net_revenue AS d") == 836 == total(ex, "twin_double_net")
    assert total(ex, "1 / net_revenue AS d") == pytest.approx(1 / 418)
    assert total(ex, "1 / net_revenue AS d") == pytest.approx(total(ex, "twin_inverse_net"))
    assert total(ex, "net_revenue / total_revenue AS d") == pytest.approx(418 / 455)
    assert total(ex, "net_revenue / total_revenue AS d") == pytest.approx(
        total(ex, "twin_net_share")
    )
    # The same written in the expression, and a quotient under a product.
    assert total(ex, "2 * (total_revenue - total_returns) AS d") == 836
    assert total(ex, "100 * (total_returns / total_revenue) AS d") == pytest.approx(100 * 37 / 455)
    assert total(ex, "total_revenue / (total_returns + 1) AS d") == pytest.approx(455 / 38)


def test_the_inlined_operand_is_written_in_parentheses(planner):
    sql = planner.sql(Query(("2 * net_revenue AS d",)))
    assert "2 * (any(__m0) - any(__m1))" in sql


def test_row_filters_apply_to_every_part(ex):
    # Retail customers 1, 2 and 4: their orders 100 + 50 + 200, their returns 10 + 15.
    q = ("customers.segment = 'retail'",)
    assert total(ex, "total_returns / total_revenue AS r", filters=q) == pytest.approx(25 / 350)
    assert total(ex, "total_returns / total_revenue AS r", filters=q) == total(
        ex, "twin_ratio", filters=q
    )
    assert region(ex, "total_returns / total_revenue AS r", filters=q) == pytest.approx(
        {"EAST": 10 / 350, "NORTH": None}
    )


def test_a_row_filter_on_the_many_side_is_a_semi_join_in_each_part(ex):
    # Revenue of the orders over 60: 100 + 200 + 75. The customers with such an order:
    # 1, 2 and 3. Customer 4 has no order, so the filter drops it from the count too.
    q = ("orders.amount > 60",)
    assert total(ex, "total_revenue / customer_count AS per_customer", filters=q) == 125
    assert total(ex, "total_revenue / customer_count AS per_customer") == pytest.approx(455 / 4)
    assert total(ex, "twin_per_customer", filters=q) == 125
    got = region(ex, "total_revenue / customer_count AS per_customer", filters=q)
    assert got == pytest.approx({"EAST": 300 / 2, "WEST": 75})


# --- the name -----------------------------------------------------------------------------


def test_the_result_column_is_the_name_as_written(ex):
    r = ex.execute(Query(("total_returns / total_revenue AS ReturnShare", "order_count")))
    assert r.columns == ["ReturnShare", "order_count"]
    r = ex.execute(Query(("total_returns / total_revenue as ReturnShare",), ("customers.region",)))
    assert r.columns == ["region", "ReturnShare"]


def test_the_name_filters_the_grouped_rows_and_orders_them(ex):
    item = "total_returns / total_revenue AS r"
    # WEST 5/75 and the orphans 7/30 pass; EAST 10/350 and NORTH (NULL) do not.
    kept = region(ex, item, filters=("r > 0.05",))
    assert kept == pytest.approx({"WEST": 5 / 75, None: 7 / 30})
    assert region(ex, item, filters=("R > 0.05",)) == kept
    # Beside a filter on a dimension.
    only = region(ex, item, filters=("r > 0.05 AND customers.region <> 'EAST'",))
    assert only == pytest.approx({"WEST": 5 / 75})  # a NULL region is not <> 'EAST'
    # One dataset, planned as a single SELECT.
    one = region(ex, "orders.revenue / orders.n AS avg_amount", filters=("avg_amount > 60",))
    assert one == pytest.approx({"EAST": 350 / 3, "WEST": 75})


def test_the_name_is_the_default_order_and_works_in_order_by_and_limit(ex):
    def ordered(*metrics: str, **kw) -> list:
        return [r[0] for r in ex.execute(Query(metrics, ("customers.region",), **kw)).rows]

    item = "total_returns / total_revenue AS r"
    # First metric descending, NULLs last: NULL region 0.23, WEST 0.067, EAST 0.029, NORTH NULL.
    assert ordered(item) == [None, "WEST", "EAST", "NORTH"]
    assert ordered(item, limit=2) == [None, "WEST"]
    assert ordered(item, order_by=("r",)) == ["EAST", "WEST", None, "NORTH"]
    assert ordered(item, order_by=("R desc",)) == [None, "WEST", "EAST", "NORTH"]
    # Not the first: order_by names the second column.
    assert ordered("order_count", item, order_by=("r desc",), limit=1) == [None]
    one = "orders.revenue / orders.n AS avg_amount"
    assert ordered(one, order_by=("avg_amount",)) == [None, "WEST", "EAST"]


# --- values over no rows and division by zero -------------------------------------------


def test_division_by_zero_shows_null(ex):
    assert total(ex, "total_revenue / (total_returns - total_returns) AS z") is None
    assert total(ex, "1 / (order_count - order_count) AS z") is None
    assert total(ex, "total_returns / total_revenue AS r", filters=("customers.id = 0",)) is None


def test_a_part_with_no_rows_in_a_group_takes_its_value_over_no_rows(ex):
    # NORTH has no orders: a count of them is 0 there, and a sum is NULL.
    got = region(ex, "total_returns + order_count AS x")
    assert got == pytest.approx({"EAST": 13, "WEST": 6, "NORTH": 15, None: 8})
    assert got == pytest.approx(region(ex, "twin_plus"))
    assert region(ex, "total_returns + total_revenue AS y")["NORTH"] is None


# --- what is refused ----------------------------------------------------------------------


REFUSED = [
    # No name, or a name that is no identifier.
    (
        "total_revenue / total_returns",
        ["needs a name", "write total_revenue / total_returns AS <name>"],
    ),
    ("total_revenue - 1 AS 1x", ["'1x' in 'total_revenue - 1 AS 1x' is not an identifier"]),
    ('total_revenue - 1 AS "r q"', ["is not an identifier", "total_revenue - 1 AS <name>"]),
    ("total_revenue - 1 AS r q", ["is not an identifier"]),
    ("total_revenue - 1 AS", ["is not an identifier"]),
    # An AS inside parentheses is no name.
    (
        "total_revenue FILTER (WHERE CAST(orders.amount AS INT) > 1) / total_revenue",
        ["needs a name", "/ total_revenue AS <name>"],
    ),
    # The planner's own columns start with two underscores: a stitched question would
    # read one of them for the name.
    (
        "total_revenue * 2 AS __c0",
        ["'__c0'", "starts with two underscores", "total_revenue * 2 AS <name>"],
    ),
    # A name a metric of the model has, whatever the case.
    ("total_returns / 2 AS total_revenue", ["'total_revenue'", "is a metric of the model"]),
    ("total_returns / 2 AS TOTAL_REVENUE", ["'TOTAL_REVENUE'", "is a metric of the model"]),
    # An unknown name: what a plain metric name says, with the nearest names.
    (
        "total_revnue / 2 AS r",
        ["unknown metric 'total_revnue' (did you mean: total_revenue"],
    ),
    ("amount / 2 AS r", ["unknown metric 'amount'", "it names the fields orders.amount"]),
    # Not its own operand, and not another expression's.
    (
        "r + 1 AS r",
        ["'r' in 'r + 1 AS r' is the name of an expression of this question", "write that"],
    ),
    ("customers.region_x / 2 AS r", ["unknown metric 'customers.region_x'"]),
    # A field.
    (
        "orders.amount / 2 AS r",
        [
            "'orders.amount' in 'orders.amount / 2 AS r' is a field, and an expression reads "
            "metrics only",
            "metrics over it: ",
            "total_revenue",
        ],
    ),
    # A written aggregate, and AGG.
    (
        "SUM(orders.amount) / 2 AS r",
        ["SUM(orders.amount) in", "is an aggregate", "name a metric of the model"],
    ),
    (
        "COUNT(DISTINCT customers.id) / total_revenue AS r",
        ["COUNT(DISTINCT customers.id)", "is an aggregate"],
    ),
    (
        "AGG(orders.revenue) / 2 AS r",
        ["AGG(orders.revenue) in", "names a dataset's metric as orders.revenue, without AGG"],
    ),
    # Anything else.
    (
        "COALESCE(total_revenue, 0) AS r",
        ["COALESCE(total_revenue, 0) in", "is not allowed in an expression over metrics"],
    ),
    ("ROUND(total_revenue) AS r", ["ROUND(total_revenue)", "is not allowed"]),
    ("NULLIF(total_revenue, 0) AS r", ["NULLIF(total_revenue, 0)", "is not allowed"]),
    ("foo(total_revenue) AS r", ["FOO(total_revenue)", "is not allowed"]),
    ("total_revenue + 'a' AS r", ["'a' in", "is not allowed"]),
    ("total_revenue + DATE '2026-01-01' AS r", ["2026-01-01", "is not allowed"]),
    ("total_revenue + TRUE AS r", ["TRUE in", "is not allowed"]),
    ("total_revenue + NULL AS r", ["NULL in", "is not allowed"]),
    ("total_revenue > 1 AS r", ["total_revenue > 1 in", "is not allowed"]),
    (
        "CASE WHEN total_revenue > 1 THEN 1 ELSE 0 END AS r",
        ["CASE WHEN total_revenue > 1", "is not allowed"],
    ),
    ("total_revenue % 2 AS r", ["total_revenue % 2", "is not allowed"]),
    ("(SELECT 1) AS r", ["Subquery is not allowed", "an expression over metrics reads"]),
    ("SUM(total_revenue) OVER () AS r", ["is a window", "an expression over metrics reads"]),
    # No metric at all.
    ("1 + 1 AS two", ["'1 + 1 AS two' names no metric"]),
    # A window metric: select it by its own name.
    (
        "rank_revenue + 1 AS r",
        ["'rank_revenue' in 'rank_revenue + 1 AS r' is a window metric", "select rank_revenue"],
    ),
]


@pytest.mark.parametrize(("item", "fragments"), REFUSED)
def test_what_an_expression_may_not_hold_is_refused_with_the_remedy(planner, item, fragments):
    message = refusal(planner, item, dimensions=("customers.region",))
    for fragment in fragments:
        assert fragment in message, message


def test_a_name_that_clashes_with_another_column_is_the_duplicate_error(planner):
    item = "total_returns / total_revenue AS region"
    assert "two selected columns named 'region'" in refusal(
        planner, item, dimensions=("customers.region",)
    )
    twice = "two selected columns named 'r'"
    assert twice in refusal(planner, "total_returns / 2 AS r", "total_revenue / 2 AS r")
    assert "two selected columns named 'R'" in refusal(
        planner, "total_returns / 2 AS r", "total_revenue / 2 AS R"
    )


def test_an_item_that_is_a_metric_name_is_answered_as_before(planner, model):
    names = ["total_revenue", "TOTAL_REVENUE", "orders.revenue", "no_such_metric", "orders.nope"]
    for name in names:
        try:
            before = planner.sql(Query((name,)))
        except PlanError as e:
            before = str(e)
        # The same planner after a question with an expression, and a fresh one.
        planner.sql(Query(("total_returns / total_revenue AS r",)))
        try:
            after = Planner(model).sql(Query((name,)))
        except PlanError as e:
            after = str(e)
        assert before == after, name
    assert "unknown metric 'no_such_metric'" in refusal(planner, "no_such_metric")


# --- no shared state ------------------------------------------------------------------


def test_planning_a_question_leaves_the_planner_as_it_was(model):
    planner = Planner(model)
    metrics, scopes = dict(planner._metrics), dict(planner._scopes)
    first = planner.sql(Query(("total_returns / total_revenue AS r",), ("customers.region",)))
    assert planner._metrics == metrics and planner._scopes == scopes
    assert planner._expressions == {}
    # The name is no metric afterwards, in a question, a filter or order_by.
    assert "unknown metric 'r'" in refusal(planner, "r")
    assert "unqualified column 'r'" in refusal(planner, "order_count", filters=("r > 1",))
    with pytest.raises(PlanError, match="not a selected metric or dimension"):
        planner.sql(Query(("order_count",), order_by=("r",), dimensions=("customers.region",)))
    # The same question gives the same SQL, from this planner and a new one.
    assert (
        planner.sql(Query(("total_returns / total_revenue AS r",), ("customers.region",))) == first
    )
    assert (
        Planner(model).sql(Query(("total_returns / total_revenue AS r",), ("customers.region",)))
        == first
    )


def test_a_failed_question_leaves_the_planner_as_it_was(model):
    planner = Planner(model)
    metrics = dict(planner._metrics)
    refusal(planner, "total_revnue / 2 AS r")
    refusal(planner, "1 + 1 AS r")
    assert planner._metrics == metrics and planner._expressions == {}


def test_a_name_in_one_question_does_not_clash_in_the_next(planner):
    planner.sql(Query(("total_returns / 2 AS r",)))
    assert "AS r" in planner.sql(Query(("total_revenue / 2 AS r",)))


def test_a_hidden_metric_is_an_unknown_name_with_no_trace(model):
    # returns.amount hidden: total_returns, return_rate, net_revenue, returns.refunds and
    # the twins that read it go with it.
    hidden = restrict(model, Hidden(fields=frozenset({"returns.amount"})))
    planner = Planner(hidden)
    gone = ["return_rate", "net_revenue", "returns.refunds", "twin_ratio", "twin_agg_ratio"]
    for name in ("total_returns", "returns.refunds"):
        as_a_name = refusal(planner, name)
        as_an_operand = refusal(planner, f"{name} / total_revenue AS r")
        assert as_an_operand == as_a_name
        left = as_an_operand.replace(f"'{name}'", "")  # the name the question wrote
        assert not [g for g in gone if g in left], as_an_operand
    for item in ("return_rate * 2 AS r", "net_revenue + 1 AS r", "2 * twin_ratio AS r"):
        name = item.replace("2 * ", "").split(" ")[0]
        suggested = refusal(planner, item).partition("(did you mean")[2]
        assert refusal(planner, item) == refusal(planner, name), item
        assert not [g for g in gone if g in suggested], item


# --- a condition on a part ------------------------------------------------------------------

COMPLETED = "total_revenue FILTER (WHERE orders.status = 'completed') / total_revenue AS s"


def test_a_share_of_a_subset_in_one_dataset(ex):
    # Completed orders: 100 + 50 + 75 + 30 of 455.
    assert total(ex, COMPLETED) == pytest.approx(255 / 455)
    assert total(ex, COMPLETED) == pytest.approx(total(ex, "twin_completed_share"))
    got = region(ex, COMPLETED)
    assert got == pytest.approx({"EAST": 150 / 350, "WEST": 1.0, None: 1.0})
    assert got == pytest.approx(region(ex, "twin_completed_share"))


def test_a_condition_on_a_part_is_one_filter_of_the_model_metric(planner):
    sql = planner.sql(Query((COMPLETED,)))
    assert sql.count("FILTER") == 1 and "FILTER(WHERE FILTER" not in sql
    twin = planner.sql(Query(("twin_completed_share",)))
    assert sql.replace(" AS s", " AS twin_completed_share") == twin


def test_a_condition_on_the_one_side_beside_the_metric_without_it(ex):
    # Retail customers 1, 2 and 4 have orders of 100 + 50 + 200. The orphan order and
    # the wholesale customer's are not theirs; a group without retail orders has NULL.
    item = "total_revenue FILTER (WHERE customers.segment = 'retail') / total_revenue AS s"
    assert total(ex, item) == pytest.approx(350 / 455)
    assert total(ex, item) == pytest.approx(total(ex, "twin_retail_share"))
    by_region = region(ex, item)
    assert by_region == pytest.approx({"EAST": 1.0, "WEST": None, None: None})
    assert by_region == pytest.approx(region(ex, "twin_retail_share"))


def test_a_metric_that_has_a_filter_takes_one_filter_with_both_conditions(ex, planner):
    item = "completed_revenue FILTER (WHERE orders.amount > 60) / total_revenue AS s"
    assert total(ex, item) == pytest.approx(175 / 455)  # 100 and 75
    assert total(ex, item) == pytest.approx(total(ex, "twin_completed_big"))
    assert planner.sql(Query((item,))).count("FILTER") == 1


def test_a_metric_with_several_aggregates_takes_the_condition_in_each(ex):
    # The mean of the completed orders, 255 / 4, over the mean of all, 455 / 5.
    item = "avg_per_order FILTER (WHERE orders.status = 'completed') / avg_per_order AS s"
    assert total(ex, item) == pytest.approx(63.75 / 91)
    assert total(ex, item) == pytest.approx(total(ex, "twin_avg_completed"))


def test_a_condition_on_the_parts_own_dataset_beside_a_part_of_another(ex):
    # Returns over 6: 10 + 15 + 7, over the 455 of all orders. A condition on a dataset the
    # part's aggregates read is a FILTER of a model's metric that the planner answers.
    item = "total_returns FILTER (WHERE returns.amount > 6) / total_revenue AS s"
    assert total(ex, item) == pytest.approx(32 / 455)
    assert region(ex, item) == pytest.approx(
        {"EAST": 10 / 350, "WEST": None, "NORTH": None, None: 7 / 30}
    )


def test_a_condition_on_another_dataset_beside_a_part_of_a_different_dataset_is_the_mixed_refusal(
    planner,
):
    # As for a model's metric (docs/decisions/components.md): the part is computed over one
    # join of its dataset and the one side, and a part of another dataset would count that
    # dataset's rows as the join repeats them. The conditions below read customers.
    for item in (
        "return_rate FILTER (WHERE customers.segment = 'retail') / return_rate AS s",
        "total_returns FILTER (WHERE customers.segment = 'retail') / total_revenue AS s",
    ):
        message = refusal(planner, item)
        assert message.startswith("metric 's' applies SUM(")
        assert "which reads several datasets or filters on another" in message


def test_a_metric_that_has_a_filter_can_be_filtered_on(ex):
    # The condition of its FILTER is part of the aggregate, not a column outside it.
    got = region(ex, "completed_revenue", filters=("completed_revenue > 100",))
    assert got == pytest.approx({"EAST": 150})


def test_a_dataset_metric_with_a_condition(ex):
    item = "orders.revenue FILTER (WHERE orders.status = 'completed') / orders.revenue AS s"
    assert total(ex, item) == pytest.approx(255 / 455)


def test_one_metric_over_two_periods(ex):
    march = "orders.placed_at >= '2026-03-01'"
    february = "orders.placed_at >= '2026-02-01' AND orders.placed_at < '2026-03-01'"
    item = f"total_revenue FILTER (WHERE {march}) / total_revenue FILTER (WHERE {february}) AS g"
    assert total(ex, item) == pytest.approx(105 / 250)  # 75 + 30 over 50 + 200


def test_a_date_string_against_a_time_field_and_the_current_date(ex):
    # A string against a DateTime field is a TIMESTAMP, read in the model's zone.
    since = "total_revenue FILTER (WHERE orders.placed_at >= '2026-02-01 10:00:00') / total_revenue"
    assert total(ex, f"{since} AS s") == pytest.approx(355 / 455)  # from order 102 on, not 101
    past = "total_revenue FILTER (WHERE orders.placed_at < CURRENT_DATE) / total_revenue AS s"
    assert total(ex, past) == pytest.approx(1.0)
    future = "total_revenue FILTER (WHERE orders.placed_at > CURRENT_DATE) / total_revenue AS s"
    assert total(ex, future) is None  # a sum over no rows


def test_a_row_filter_of_the_question_applies_beside_a_condition_on_a_part(ex):
    # Retail customers' orders: 100 + 50 + 200, completed 100 + 50.
    q = ("customers.segment = 'retail'",)
    assert total(ex, COMPLETED, filters=q) == pytest.approx(150 / 350)
    assert region(ex, COMPLETED, filters=q) == pytest.approx({"EAST": 150 / 350})


def test_the_name_of_a_share_works_in_filters_and_order_by(ex):
    kept = region(ex, COMPLETED, filters=("s > 0.5",))
    assert kept == pytest.approx({"WEST": 1.0, None: 1.0})
    rows_ = ex.execute(Query((COMPLETED,), ("customers.region",), order_by=("s",))).rows
    assert [r[0] for r in rows_] == ["EAST", "WEST", None]
    assert ex.execute(Query((COMPLETED,), ("customers.region",), limit=1)).rows[0][1] == 1.0


def test_a_division_by_zero_in_a_share_shows_null(ex):
    item = "total_revenue / (total_revenue FILTER (WHERE orders.id < 0)) AS s"
    assert total(ex, item) is None


CONDITION_REFUSED = [
    # What a condition may not hold.
    (
        "total_revenue FILTER (WHERE total_returns > 1) / total_revenue AS s",
        [
            "the condition 'total_returns > 1' on 'total_revenue'",
            "names the metric 'total_returns'",
        ],
    ),
    (
        "total_revenue FILTER (WHERE orders.revenue > 1) / total_revenue AS s",
        ["names the metric 'orders.revenue'", "dataset.field"],
    ),
    (
        "total_revenue FILTER (WHERE SUM(orders.amount) > 1) / total_revenue AS s",
        ["SUM(orders.amount) in the condition", "no aggregate, window or metric"],
    ),
    (
        "total_revenue FILTER (WHERE AGG(orders.revenue) > 1) / total_revenue AS s",
        ["AGG(orders.revenue) in the condition", "is not allowed"],
    ),
    (
        "total_revenue FILTER (WHERE RANK() OVER (ORDER BY orders.amount) = 1) / total_revenue "
        "AS s",
        ["in the condition", "no aggregate, window or metric"],
    ),
    (
        "total_revenue FILTER (WHERE orders.id IN (SELECT 1)) / total_revenue AS s",
        ["is not allowed in an Ossie expression"],
    ),
    # Fields: the errors of a filter of the question.
    (
        "total_revenue FILTER (WHERE orders.nope = 1) / total_revenue AS s",
        ["unknown field 'orders.nope'"],
    ),
    (
        "total_revenue FILTER (WHERE status = 'x') / total_revenue AS s",
        ["unqualified column 'status'"],
    ),
    # Where FILTER may stand.
    (
        "(total_revenue) FILTER (WHERE orders.id > 1) / 2 AS s",
        ["cannot parse", "FILTER (WHERE condition) goes directly after a metric name"],
    ),
    (
        "1 FILTER (WHERE orders.id > 1) AS s",
        ["cannot parse", "goes directly after a metric name"],
    ),
    (
        "SUM(orders.amount) FILTER (WHERE orders.id > 1) / total_revenue AS s",
        ["is an aggregate", "name a metric of the model"],
    ),
    (
        "total_revenue FILTER (WHERE orders.id > 1)",
        ["needs a name", "total_revenue FILTER (WHERE orders.id > 1) AS <name>"],
    ),
    # A metric without an aggregate, a window metric.
    (
        "clock FILTER (WHERE orders.id > 1) / 2 AS s",
        ["'clock' in", "has no aggregate", "put the condition in a filter of the question"],
    ),
    (
        "rank_revenue FILTER (WHERE orders.id > 1) + 1 AS s",
        ["is a window metric", "select rank_revenue by its own name"],
    ),
    # Reach: the condition's dataset must be reached from every dataset the part aggregates.
    (
        "total_returns FILTER (WHERE orders.status = 'completed') / total_returns AS s",
        [
            "the condition \"orders.status = 'completed'\" on 'total_returns'",
            "reads orders, which returns, where total_returns aggregates rows, does not reach",
            "in a filter of the question, which applies to every part",
            "declare a metric of the model",
        ],
    ),
    (
        "return_rate FILTER (WHERE orders.status = 'completed') / return_rate AS s",
        ["on 'return_rate'", "reads orders, which returns", "does not reach"],
    ),
    (
        "customer_count FILTER (WHERE orders.status = 'completed') / customer_count AS s",
        [
            "reads orders, which customers",
            "customers is the one side of ['orders']",
            "rows that reference what a metric counts is not supported in an expression",
        ],
    ),
    # A division of a tree by itself.
    (
        "total_revenue / total_revenue AS s",
        ["divides total_revenue by itself", "total_revenue FILTER (WHERE <condition>) / "],
    ),
    (
        "(total_revenue + 1) / (total_revenue + 1) AS s",
        ["by itself, which is always 1"],
    ),
    (
        "total_revenue FILTER (WHERE orders.id > 1) / total_revenue FILTER (WHERE orders.id > 1) "
        "AS s",
        ["divides total_revenue by itself"],
    ),
    # Another expression of the question is no operand.
    (
        "total_revenue / 2 AS a",
        [],  # a valid first item; the second names it
    ),
]


@pytest.mark.parametrize(("item", "fragments"), CONDITION_REFUSED[:-1])
def test_what_a_condition_on_a_part_may_not_be_is_refused(planner, item, fragments):
    message = refusal(planner, item)
    for fragment in fragments:
        assert fragment in message, message


def test_an_expression_that_names_another_one_of_the_question_is_refused(planner):
    message = refusal(planner, "total_revenue / 2 AS a", "a + 1 AS b")
    assert "'a' in 'a + 1 AS b' is the name of an expression of this question" in message
    assert "write that expression out where the name stands" in message


def test_the_form_is_the_question_only(model, tmp_path):
    # A model's expression, a question's filter and a field refuse it as they did.
    from dactopus_ossie_clickhouse.translate import parse, untranslatable

    with pytest.raises(ValueError, match="cannot parse"):
        parse("total_revenue FILTER (WHERE orders.id > 1)")
    assert parse("total_revenue FILTER (WHERE orders.id > 1)", question=True).find(exp.Filter)
    data = copy.deepcopy(BASE)
    data["metrics"].append(metric("bad", "total_revenue FILTER (WHERE orders.id > 1)"))
    data["datasets"][0]["fields"].append(
        {"name": "bad_field", "expression": expr("id FILTER (WHERE id > 1)")}
    )
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    fields, metrics = untranslatable(load_model(path))
    assert "cannot parse" in metrics["bad"] and "cannot parse" in fields["customers.bad_field"]
    planner = Planner(model)
    for f in (
        "total_revenue FILTER (WHERE orders.id > 1) > 1",
        "orders.id FILTER (WHERE 1 = 1) > 1",
    ):
        assert "cannot parse" in refusal(planner, "order_count", filters=(f,))


def test_a_hidden_field_in_a_condition_is_an_unknown_field_with_no_trace(model):
    planner = Planner(restrict(model, Hidden(fields=frozenset({"customers.segment"}))))
    item = "total_revenue FILTER (WHERE customers.segment = 'retail') / total_revenue AS s"
    message = refusal(planner, item)
    assert message == refusal(planner, "total_revenue", filters=("customers.segment = 'retail'",))
    assert message.startswith("unknown field 'customers.segment'")
    assert "wholesale" not in message and "twin_retail" not in message


def test_a_question_with_a_condition_gives_stable_sql_and_leaves_the_planner(model):
    planner = Planner(model)
    metrics = dict(planner._metrics)
    item = "total_revenue FILTER (WHERE customers.segment = 'retail') / total_revenue AS s"
    first = planner.sql(Query((item,), ("customers.region",)))
    assert planner.sql(Query((item,), ("customers.region",))) == first
    assert Planner(model).sql(Query((item,), ("customers.region",))) == first
    assert planner._metrics == metrics and planner._expressions == {}
    assert planner.sql(Query(("total_revenue",))) == Planner(model).sql(Query(("total_revenue",)))


# --- the surfaces ------------------------------------------------------------------------


def test_the_cli_builds_and_runs_a_question_expression(ex, model_path, capsys):
    item = "100 * total_returns / total_revenue AS return_pct"
    assert main(["sql", str(model_path), "-m", item, "-d", "customers.region"]) == 0
    out = capsys.readouterr().out
    assert "AS return_pct" in out and "UNION ALL" in out
    assert main(["sql", str(model_path), "-m", "total_revenue"]) == 0
    assert "AS return_pct" not in capsys.readouterr().out
    url = server_url()
    args = ["query", str(model_path), "-m", item, "-d", "customers.region", "--url", url]
    assert main([*args, "-f", "return_pct > 10", "-o", "return_pct", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got == [{"region": None, "return_pct": pytest.approx(100 * 7 / 30)}]
    # A refusal is the error line, not a traceback.
    assert main(["sql", str(model_path), "-m", "total_revenue / 2"]) == 1
    assert "needs a name" in capsys.readouterr().err


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_the_mcp_query_tool_returns_the_column_under_its_name(clickhouse, ex, model):
    from mcp import Client

    async with Client(build_server(model, lambda: clickhouse), raise_exceptions=True) as c:
        r = await c.call_tool(
            "query",
            {
                "metrics": ["100 * total_returns / total_revenue AS return_pct"],
                "dimensions": ["customers.region"],
                "filters": ["return_pct > 10"],
                "order_by": ["return_pct desc"],
            },
        )
        out = r.structured_content
        assert out["columns"] == ["region", "return_pct"]
        assert out["rows"][0][0] is None
        assert out["rows"][0][1] == pytest.approx(100 * 7 / 30)
        assert [row[0] for row in out["rows"]] == [None]
        assert "AS return_pct" in out["sql"]
        # A refusal is a result with an error field, as for any metric.
        r = await c.call_tool("query", {"metrics": ["total_returns / total_revenue"]})
        assert "needs a name" in r.structured_content["error"]
        r = await c.call_tool("query", {"metrics": ["SUM(orders.amount) / 2 AS r"]})
        assert "is an aggregate" in r.structured_content["error"]
        # The tool tells the agent about expressions.
        tools = {t.name: t for t in (await c.list_tools()).tools}
        text = " ".join(tools["query"].description.split())
        assert "total_sales / total_profit AS ratio" in text
        assert "A filter of the question applies to every part" in text
        assert "customer_count FILTER (WHERE customer.c_birth_year > 1980)" in text


# --- the instructions -------------------------------------------------------------------


# The text before the model's own instructions was 1731 characters for the TPC-DS example
# when expressions came, 1748 with the condition on a part, and may not grow: Claude Code
# cuts instructions at 4,096 and models use up to 2,300.
INSTRUCTIONS_BEFORE_THE_MODEL = 1748


def test_the_instructions_say_where_a_share_comes_from_and_stay_as_short():
    text = instructions(load_model(TPCDS))
    own = text.index("\n\nInstructions of this model:\n")
    assert own <= INSTRUCTIONS_BEFORE_THE_MODEL
    assert (
        "Every number you state must come from a query result; rounding is fine. For a share, "
        'ratio or difference, put an expression in metrics: "buyers FILTER (WHERE c.orders > '
        '1) / buyers AS share".'
    ) in text
    assert "such as the counts behind a rate" not in text

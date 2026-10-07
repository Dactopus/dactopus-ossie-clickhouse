"""docs/index.html quotes what the planner writes; these keep the quotes true."""

import html
import json
import re
from pathlib import Path

import clickhouse_connect
import pytest
from sqlglot import exp

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Query
from dactopus_ossie_clickhouse.translate import parse, pick_expression
from tests.conftest import FakeClickHouse

ROOT = Path(__file__).parents[1]
PAGE = (ROOT / "docs" / "index.html").read_text()
SQL = html.unescape(re.search(r'<pre id="sql">(.*?)</pre>', PAGE, re.S)[1])
STEPS = json.loads(re.search(r"const STEPS = (\[.*?\]);\n", PAGE, re.S)[1])
MODEL = load_model(str(ROOT / "tests" / "fixtures" / "web_analytics.yaml"))
QUESTION = Query(
    measures=("revenue", "purchases"),
    dimensions=("purchases.purchase_month", "sessions.source_medium", "purchases.currency"),
    where=("sessions.country = 'United States'",),
)


def fake_clickhouse(without: tuple[str, str] | None = None) -> FakeClickHouse:
    """A user of the database dactopus, where dactopus-data-models builds every
    table as plain MergeTree (dbt-clickhouse's default for incremental
    delete+insert); ``without`` hides one column."""
    tables = {}
    for ds in MODEL.datasets:
        cols = {
            c.name
            for f in ds.fields
            for c in parse(pick_expression(f.expression)).find_all(exp.Column)
        }
        if without and without[0] == ds.source:
            cols -= {without[1]}
        tables["dactopus", ds.source] = ("MergeTree", "MergeTree", ("", ""), cols)
    return FakeClickHouse(tables, user="analyst", database="dactopus")


def refusal(ex: Executor, q: Query) -> str:
    with pytest.raises(PlanError) as e:
        ex.planner.sql(q)
    return str(e.value)


def test_page_shows_the_sql_the_planner_writes():
    assert STEPS[0][2] == QUESTION.where[0]
    assert SQL == Executor(fake_clickhouse(), MODEL).planner.sql(QUESTION, pretty=True)


def test_page_marks_every_sql_line_and_only_lines_that_exist():
    lines = SQL.split("\n")
    starts = [s for step in STEPS for s in step[3]]
    assert [s for s in starts if not any(line.startswith(s) for line in lines)] == []
    assert [line for line in lines if not any(line.startswith(s) for s in starts)] == []


def test_page_quotes_the_fan_out_refusal():
    q = Query(measures=("session_conversion_rate",), dimensions=("purchases.currency",))
    assert STEPS[6][2] == refusal(Executor(fake_clickhouse(), MODEL), q)


def test_page_quotes_what_a_user_without_revenue_gets():
    ex = Executor(fake_clickhouse(without=("purchases", "revenue")), MODEL)
    metrics = {m.name for m in ex.model.metrics}
    assert {"revenue", "average_order_value"}.isdisjoint(metrics)
    assert "revenue_usd" in metrics
    assert STEPS[7][2] == refusal(ex, QUESTION)


def test_page_figures_come_from_the_ga4_sample(clickhouse):
    if not clickhouse.query("EXISTS TABLE dactopus.purchases").result_rows[0][0]:
        pytest.skip("GA4 sample not loaded into the database dactopus (see dactopus-data-models)")
    ex = Executor(clickhouse_connect.get_client(dsn=clickhouse.url, database="dactopus"), MODEL)
    month, source_medium, currency, revenue, purchases = ex.execute(QUESTION).rows[0]
    row = f"{month:%B %Y}, {source_medium}, {currency}: revenue {revenue:,.0f}"
    assert f"{row} from {purchases} purchases" in STEPS[5][1]

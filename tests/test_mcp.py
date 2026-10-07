"""MCP server: tools are thin wrappers, checked against the library directly."""

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Result
from dactopus_ossie_clickhouse.mcp_server import (
    answer,
    describe,
    instructions,
    refusal,
    search,
    summary,
)
from dactopus_ossie_clickhouse.planner import LAYER3_REVISION, QUERY_SCHEMA, Code, Suggestion
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)

# --- pure views, no server ---------------------------------------------------


def test_summary_is_compact():
    s = summary(MODEL)
    assert [d["name"] for d in s["datasets"]] == [
        "store_sales",
        "date_dim",
        "customer",
        "item",
        "store",
    ]
    assert s["datasets"][0] == {
        "name": "store_sales",
        "description": "Fact table containing all store sales transactions",
        "synonyms": ["sales transactions", "store purchases", "retail sales", "POS data"],
        "fields": 8,
    }
    total_sales = next(m for m in s["metrics"] if m["name"] == "total_sales")
    assert total_sales["synonyms"] == ["total revenue", "gross sales", "sales amount"]
    assert "store_sales -> item" in s["relationships"]
    assert "instructions" in s


def test_search_by_synonym_and_name():
    names = {h["name"] for h in search(MODEL, "revenue")}
    assert {"total_sales", "customer_lifetime_value", "store_productivity"} <= names
    assert search(MODEL, "brand")[0]["kind"] == "field"
    assert search(MODEL, "zzz") == [] and search(MODEL, "") == []
    # words may be apart and in any order: "customer value" hits the CLV description
    assert [h["name"] for h in search(MODEL, "value customer")] == ["customer_lifetime_value"]
    assert search(MODEL, "return discount") == []


def test_describe_dataset_metric_field_and_unknown():
    d = describe(MODEL, "item")
    assert d["kind"] == "dataset" and {f["name"] for f in d["fields"]} >= {"i_brand", "i_category"}
    assert d["relationships"] == ["store_sales(ss_item_sk) -> item(i_item_sk)"]
    m = describe(MODEL, "CUMULATIVE_SALES")
    assert m["kind"] == "metric" and m["expression"].startswith("SUM(SUM(")
    assert "requires grouping by date_dim.d_date" in m["description"]
    f = describe(MODEL, "date_dim.d_date")
    assert f["kind"] == "field" and f.get("is_time")
    assert "similar" in describe(MODEL, "sales")["error"]


def test_ai_context_forms():
    from ossie import OssieDocument

    data = MODEL.model_dump(by_alias=True)
    data["ai_context"] = "plain text instructions"
    metric = next(m for m in data["metrics"] if m["name"] == "total_sales")
    metric["ai_context"] = {"instructions": "sum, never average", "examples": ["sales by brand"]}
    m = OssieDocument.model_validate(data)
    assert summary(m)["instructions"] == "plain text instructions"
    d = describe(m, "total_sales")
    assert d["instructions"] == "sum, never average" and d["examples"] == ["sales by brand"]


def test_instructions_carry_the_model_instructions():
    # An agent that starts with search_model never sees list_model's copy.
    text = instructions(MODEL)
    assert text.startswith("Semantic model 'tpcds_retail_model' over ClickHouse.")
    assert "Every number you state must come from a query result" in text
    assert text.endswith(f"Instructions of this model:\n{summary(MODEL)['instructions']}")
    bare = MODEL.model_copy(update={"ai_context": None})
    assert "Instructions of this model" not in instructions(bare)


def test_describe_shows_the_expression_the_planner_runs():
    data = MODEL.model_dump(by_alias=True)
    metric = next(m for m in data["metrics"] if m["name"] == "total_sales")
    metric["expression"]["dialects"].insert(
        0, {"dialect": "SNOWFLAKE", "expression": "SUM(store_sales.ss_ext_sales_price):snow"}
    )
    from ossie import OssieDocument

    m = describe(OssieDocument.model_validate(data), "total_sales")
    assert m["expression"] == "SUM(store_sales.ss_ext_sales_price)"


def test_answer_names_the_layer3_revision():
    from datetime import date
    from decimal import Decimal

    r = Result(["d", "v"], [(date(1998, 1, 2), Decimal("1.50"))], "SELECT 1", ["Date", "Decimal"])
    assert answer(r) == {
        "language": LAYER3_REVISION,
        "columns": ["d", "v"],
        "rows": [["1998-01-02", "1.50"]],
        "row_count": 1,
        "sql": "SELECT 1",
    }
    assert LAYER3_REVISION == "apache/ossie#246@cc0d070"


def test_refusal_is_the_ossie_529_error_envelope():
    out = refusal(Code.E_NAME_NOT_FOUND, "unknown metric 'x'", (Suggestion("name", "y"),))
    assert out == {
        "status": "error",
        "language": LAYER3_REVISION,
        "error": {"code": "E_NAME_NOT_FOUND", "message": "unknown metric 'x'", "retryable": False},
        "suggestions": [{"kind": "name", "message": "y"}],
    }


# --- through the protocol, against ClickHouse ---------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(tpcds):
    from mcp import Client

    from dactopus_ossie_clickhouse.mcp_server import build_server

    async with Client(build_server(MODEL, lambda: tpcds), raise_exceptions=True) as c:
        yield c


def test_server_reports_the_package_version(tpcds):
    from importlib.metadata import version

    from dactopus_ossie_clickhouse.mcp_server import build_server

    assert build_server(MODEL, lambda: tpcds).version == version("dactopus-ossie-clickhouse")


def test_serve_names_the_database_on_stderr(tpcds, monkeypatch, capsys):
    from mcp.server import MCPServer

    from dactopus_ossie_clickhouse.cli import main

    monkeypatch.setattr(MCPServer, "run", lambda self: None)
    assert main(["serve", str(FIXTURE), "--url", tpcds.url]) == 0
    out = capsys.readouterr()
    assert out.out == ""  # stdout carries the protocol
    assert out.err.endswith(f"on {tpcds.url} (database tpcds)\n")


@pytest.mark.anyio
async def test_tools_listed(client):
    tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {
        "list_model",
        "search_model",
        "describe_object",
        "query",
    }
    q = next(t for t in tools.tools if t.name == "query")
    assert q.input_schema["required"] == ["query"]
    assert q.input_schema["properties"]["query"] == QUERY_SCHEMA | {"title": "Query"}
    assert set(QUERY_SCHEMA["properties"]) == {
        "dimensions",
        "measures",
        "where",
        "having",
        "order_by",
        "limit",
        "fields",
    }
    assert LAYER3_REVISION in q.description


@pytest.mark.anyio
async def test_tools_match_library(client):
    r = await client.call_tool("list_model", {})
    # store_productivity is refused in every question, so agents never see it.
    seen = MODEL.model_copy(
        update={"metrics": [m for m in MODEL.metrics if m.name != "store_productivity"]}
    )
    assert r.structured_content == summary(seen)
    r = await client.call_tool("describe_object", {"name": "total_sales"})
    assert r.structured_content == describe(MODEL, "total_sales")
    r = await client.call_tool("search_model", {"text": "profit"})
    assert r.structured_content["result"] == search(MODEL, "profit")


@pytest.mark.anyio
async def test_query(client):
    r = await client.call_tool(
        "query",
        {"query": {"measures": ["total_sales"], "dimensions": ["item.i_category"],
                   "where": "date_dim.d_year = 1998", "limit": 3}},
    )  # fmt: skip
    out = r.structured_content
    assert not r.is_error and out["language"] == LAYER3_REVISION
    assert out["columns"] == ["i_category", "total_sales"] and out["row_count"] == 3
    assert "LEFT JOIN tpcds.item" in out["sql"] and "ORDER BY total_sales DESC" in out["sql"]
    values = [float(row[1]) for row in out["rows"]]
    assert values == sorted(values, reverse=True)
    # The #246 example shape: having, order_by objects, a list of where predicates.
    r = await client.call_tool(
        "query",
        {"query": {"dimensions": ["item.i_category"], "measures": ["total_sales"],
                   "where": ["date_dim.d_year = 1998", "item.i_category IS NOT NULL"],
                   "having": "total_sales > 100000000",
                   "order_by": [{"field": "item.i_category", "direction": "ASC"}]}},
    )  # fmt: skip
    out = r.structured_content
    assert [row[0] for row in out["rows"]] == sorted(row[0] for row in out["rows"])
    assert "HAVING" in out["sql"] and "LIMIT 100" in out["sql"]  # the default limit


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "code", "message"),
    [
        (
            {"measures": ["total_revenue"]},
            "E_NAME_NOT_FOUND",
            "unknown metric 'total_revenue' (did you mean: total_sales",
        ),
        (
            {"metrics": ["total_sales"]},
            "QUERY_INVALID",
            "unknown query clause 'metrics'; use measures",
        ),
        ({"measures": ["total_sales"], "limit": -1}, "QUERY_INVALID", "limit must be"),
        (
            {"measures": ["total_sales"], "order_by": ["total_sales desc"]},
            "QUERY_INVALID",
            "use a list of objects such as",
        ),
        (
            {"measures": ["total_sales"], "order_by": [{"field": "total_sales", "direction": 1}]},
            "QUERY_INVALID",
            "all strings",
        ),
        (
            {"measures": ["total_sales"], "order_by": "total_sales"},
            "QUERY_INVALID",
            "use a list of objects",
        ),
        ({"measures": "total_sales"}, "QUERY_INVALID", "must be a list of strings"),
        ({"measures": ["total_sales"], "where": "total_sales > 1"}, "E_AGGREGATE_IN_WHERE", ""),
        ({}, "E_EMPTY_AGGREGATION_QUERY", ""),
        ({"fields": ["item.i_brand"]}, "UNSUPPORTED_QUERY", "scalar queries"),
        # Plans, but ClickHouse fails it: a backend error, on one line.
        (
            {"measures": ["total_sales"], "where": "date_dim.d_year = 'abc'"},
            "BACKEND_ERROR",
            "ClickHouse: ",
        ),
    ],
)
async def test_refusals_are_tool_errors(client, query, code, message):
    r = await client.call_tool("query", {"query": query})
    out = r.structured_content
    assert r.is_error and out["status"] == "error" and out["language"] == LAYER3_REVISION
    assert out["error"]["code"] == code and message in out["error"]["message"]
    assert out["error"]["retryable"] is False and "\n" not in out["error"]["message"]
    assert r.content[0].text == f"{code}: {out['error']['message']}"


@pytest.mark.anyio
async def test_name_suggestions_are_structured(client):
    r = await client.call_tool("query", {"query": {"measures": ["total_sale"]}})
    assert r.structured_content["suggestions"][0] == {"kind": "name", "message": "total_sales"}
    r = await client.call_tool("query", {"query": {"filters": ["x"]}})
    assert r.structured_content["suggestions"] == [
        {"kind": "name", "message": "where"},
        {"kind": "name", "message": "having"},
    ]


@pytest.mark.anyio
async def test_concurrent_queries_share_one_process(tpcds):
    """Hosts issue tool calls in parallel and the SDK runs sync tools in threads:
    one server must serve them all without ClickHouse session clashes."""
    import asyncio

    from mcp import Client

    from dactopus_ossie_clickhouse.executor import connect
    from dactopus_ossie_clickhouse.mcp_server import build_server

    async with Client(build_server(MODEL, connect), raise_exceptions=True) as c:
        q = {"where": "sleep(0.3) = 0", "measures": ["total_sales"]}
        results = await asyncio.gather(*[c.call_tool("query", {"query": q}) for _ in range(3)])
    assert [r.is_error for r in results] == [False, False, False]

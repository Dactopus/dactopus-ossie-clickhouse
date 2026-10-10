"""MCP server: tools are thin wrappers, checked against the library directly."""

import re

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.execute_query import (
    CSV_META,
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
    Binding,
)
from dactopus_ossie_clickhouse.mcp_server import describe, instructions, search, summary
from dactopus_ossie_clickhouse.planner import LAYER3_REVISION
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)
BINDING = Binding.of(MODEL)
SOURCE = BINDING.data_source_id

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
    assert "binding" not in s
    assert summary(MODEL, BINDING)["binding"] == BINDING.describe()


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
    assert f'execute_query with data_source_id "{SOURCE}"' in instructions(MODEL, BINDING)


def test_describe_shows_the_expression_the_planner_runs():
    data = MODEL.model_dump(by_alias=True)
    metric = next(m for m in data["metrics"] if m["name"] == "total_sales")
    metric["expression"]["dialects"].insert(
        0, {"dialect": "SNOWFLAKE", "expression": "SUM(store_sales.ss_ext_sales_price):snow"}
    )
    from ossie import OssieDocument

    m = describe(OssieDocument.model_validate(data), "total_sales")
    assert m["expression"] == "SUM(store_sales.ss_ext_sales_price)"


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
        "execute_query",
    }
    q = next(t for t in tools.tools if t.name == "execute_query")
    # The profile's schemas as published (apache/ossie#529), not ones the SDK derives.
    assert q.input_schema == INPUT_SCHEMA and q.output_schema == OUTPUT_SCHEMA
    assert q.annotations.read_only_hint and not q.annotations.destructive_hint
    assert LAYER3_REVISION in q.description
    # The same for every model: no names an agent could take for its own model's.
    names = {m.name for m in MODEL.metrics} | {d.name for d in MODEL.datasets}
    assert not {n for n in names if re.search(rf"\b{n}\b", q.description)}


@pytest.mark.anyio
async def test_tools_match_library(client):
    r = await client.call_tool("list_model", {})
    # store_productivity is refused in every question, so agents never see it.
    seen = MODEL.model_copy(
        update={"metrics": [m for m in MODEL.metrics if m.name != "store_productivity"]}
    )
    assert r.structured_content == summary(seen, BINDING)
    r = await client.call_tool("describe_object", {"name": "total_sales"})
    assert r.structured_content == describe(MODEL, "total_sales")
    r = await client.call_tool("search_model", {"text": "profit"})
    assert r.structured_content["result"] == search(MODEL, "profit")


async def _query(client, query):
    # The client checks structuredContent against the tool's outputSchema.
    return await client.call_tool("execute_query", {"data_source_id": SOURCE, "query": query})


@pytest.mark.anyio
async def test_query(client):
    r = await _query(
        client,
        {"measures": ["total_sales"], "dimensions": ["item.i_category"],
         "where": "date_dim.d_year = 1998", "limit": 3},
    )  # fmt: skip
    out = r.structured_content
    assert not r.is_error and out["status"] == "success"
    assert out["data_source_id"] == SOURCE
    assert out["model"] == {"id": "tpcds_retail_model", "revision": BINDING.revision}
    assert out["preview"]["columns"] == [
        {"name": "i_category", "datatype": "String"},
        {"name": "total_sales", "datatype": "Decimal"},
    ]
    assert out["result"]["row_count"] == 3 and out["result"]["completeness"] == "complete"
    sql = out["result"]["extensions"]["io.github.dactopus/clickhouse"]["sql"]
    assert "LEFT JOIN tpcds.item" in sql and "ORDER BY total_sales DESC" in sql
    values = [float(row[1]) for row in out["preview"]["rows"]]
    assert values == sorted(values, reverse=True)
    assert r.content[0].text == "3 rows; complete. Data: result.csv."
    csv = r.content[1].resource
    assert csv.uri == "file:///result.csv" and csv.mime_type == "text/csv"
    assert csv.text.split("\r\n")[:2] == [
        "i_category,total_sales",
        ",".join(out["preview"]["rows"][0]),
    ]
    assert csv.meta[CSV_META]["columns"] == out["preview"]["columns"]
    # The #246 example shape: having, order_by objects, a list of where predicates.
    r = await _query(
        client,
        {"dimensions": ["item.i_category"], "measures": ["total_sales"],
         "where": ["date_dim.d_year = 1998", "item.i_category IS NOT NULL"],
         "having": "total_sales > 100000000",
         "order_by": [{"field": "item.i_category", "direction": "ASC"}]},
    )  # fmt: skip
    rows = r.structured_content["preview"]["rows"]
    assert [row[0] for row in rows] == sorted(row[0] for row in rows)


@pytest.mark.anyio
async def test_rows_past_the_ceiling_are_reported_truncated(client):
    r = await _query(client, {"measures": ["total_sales"], "dimensions": ["item.i_brand"]})
    out = r.structured_content
    assert out["result"]["completeness"] == "truncated" and out["result"]["row_count"] == 100
    assert "LIMIT 101" in out["result"]["extensions"]["io.github.dactopus/clickhouse"]["sql"]
    assert r.content[0].text.startswith("100 rows; truncated: there are more than 100")
    # The query's own limit is its meaning, not a cut: the answer is complete.
    r = await _query(
        client, {"measures": ["total_sales"], "dimensions": ["item.i_brand"], "limit": 200}
    )
    out = r.structured_content
    assert out["result"]["completeness"] == "complete" and out["result"]["row_count"] == 200
    assert out["preview"]["row_count"] == 100 and out["preview"]["has_more"]


@pytest.mark.anyio
async def test_no_rows_still_name_their_columns(client):
    # ClickHouse returns no column names for an empty grouped result: asked separately.
    r = await _query(
        client,
        {
            "measures": ["total_sales"],
            "dimensions": ["item.i_brand"],
            "where": "item.i_brand = 'x'",
        },
    )
    out = r.structured_content
    assert out["result"]["row_count"] == 0 and out["preview"]["rows"] == []
    assert [c["name"] for c in out["preview"]["columns"]] == ["i_brand", "total_sales"]
    assert r.content[1].resource.text == "i_brand,total_sales\r\n"
    assert out["diagnostics"]["state"] == "unavailable"


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
            "INVALID_ARGUMENT",
            "unknown query clause 'metrics'; use measures",
        ),
        ({"measures": ["total_sales"], "limit": -1}, "INVALID_ARGUMENT", "limit must be"),
        (
            {"measures": ["total_sales"], "order_by": ["total_sales desc"]},
            "INVALID_ARGUMENT",
            "keys are field",
        ),
        (
            {"measures": ["total_sales"], "order_by": [{"field": "total_sales", "direction": 1}]},
            "INVALID_ARGUMENT",
            'direction ("ASC" or "DESC")',
        ),
        (
            {"measures": ["total_sales"], "order_by": "total_sales"},
            "INVALID_ARGUMENT",
            "order_by must be a non-empty list",
        ),
        ({"measures": "total_sales"}, "INVALID_ARGUMENT", "must be a list of non-blank strings"),
        ({"measures": ["total_sales"], "where": "total_sales > 1"}, "E_AGGREGATE_IN_WHERE", ""),
        ({}, "E_EMPTY_AGGREGATION_QUERY", ""),
        ({"fields": []}, "E_EMPTY_SCALAR_QUERY", ""),
        ({"fields": ["item.i_brand"], "dimensions": []}, "E_MIXED_QUERY_SHAPE", "dimensions"),
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
    r = await _query(client, query)
    out = r.structured_content
    assert r.is_error and out["status"] == "error" and out["contract_version"] == "0.4-draft"
    assert out["data_source_id"] == SOURCE and out["model"]["revision"] == BINDING.revision
    assert out["error"]["code"] == code and message in out["error"]["message"]
    assert out["error"]["retryable"] is False and "\n" not in out["error"]["message"]
    assert r.content[0].text == f"{code}: {out['error']['message']}"
    assert "preview" not in out and "result" not in out


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"query": {"measures": ["total_sales"]}}, "INVALID_ARGUMENT"),
        ({"data_source_id": "other", "query": {"measures": ["total_sales"]}}, "SOURCE_UNAVAILABLE"),
        ({"data_source_id": SOURCE}, "INVALID_ARGUMENT"),
        # The SDK would drop the extra key and decode the string: the arguments as sent count.
        ({"data_source_id": SOURCE, "query": {"measures": ["total_sales"]}, "x": 1},
         "INVALID_ARGUMENT"),
        ({"data_source_id": SOURCE, "query": '{"measures": ["total_sales"]}'}, "INVALID_ARGUMENT"),
    ],
)  # fmt: skip
async def test_arguments_outside_the_profile_are_refused(client, arguments, code):
    r = await client.call_tool("execute_query", arguments)
    out = r.structured_content
    assert r.is_error and out["error"]["code"] == code
    if isinstance(arguments.get("data_source_id"), str):
        assert out["data_source_id"] == arguments["data_source_id"]
    if code == "SOURCE_UNAVAILABLE":
        assert "model" not in out
        assert out["suggestions"] == [{"kind": "name", "message": SOURCE}]


@pytest.mark.anyio
async def test_name_suggestions_are_structured(client):
    r = await _query(client, {"measures": ["total_sale"]})
    assert r.structured_content["suggestions"][0] == {"kind": "name", "message": "total_sales"}
    r = await _query(client, {"filters": ["x"]})
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
        results = await asyncio.gather(*[_query(c, q) for _ in range(3)])
    assert [r.is_error for r in results] == [False, False, False]

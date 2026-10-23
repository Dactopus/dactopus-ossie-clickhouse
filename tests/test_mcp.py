"""MCP server: tools are thin wrappers, checked against the library directly."""

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.mcp_server import describe, instructions, search, summary
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
        "fields": 9,
    }
    total_sales = next(m for m in s["metrics"] if m["name"] == "total_sales")
    assert total_sales["synonyms"] == ["total revenue", "gross sales", "sales amount"]
    assert "store_sales -> item" in s["relationships"]
    assert "instructions" in s
    assert s["time_zone"] == "UTC"


def test_summary_carries_the_clock_after_the_zone():
    clock = {"today": "2026-10-21", "now": "2026-10-21 22:16:00-10:00"}
    s = summary(MODEL, clock)
    keys = list(s)
    assert keys[keys.index("time_zone") + 1 :][:2] == ["today", "now"]
    assert s["today"] == "2026-10-21" and s["now"] == "2026-10-21 22:16:00-10:00"
    assert "today" not in summary(MODEL)


def test_zone_given_at_load_time_is_named():
    model = load_model(FIXTURE, time_zone="America/New_York")
    assert summary(model)["time_zone"] == "America/New_York"
    assert "Today and the current time are those of America/New_York" in instructions(model)


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


def test_a_datasets_metric_is_named_as_a_question_asks_for_it():
    name = "store_sales.total_quantity"
    assert {"name": name, "description": "Total quantity sold across all store sales"} in (
        summary(MODEL)["metrics"]
    )
    assert describe(MODEL, "store_sales")["metrics"] == [
        {"name": name, "description": "Total quantity sold across all store sales"}
    ]
    m = describe(MODEL, name.upper())
    assert (m["kind"], m["name"], m["expression"]) == ("metric", name, "SUM(ss_quantity)")
    assert name in [h["name"] for h in search(MODEL, "total_quantity")]


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
    # A filter on the many side is a semi-join: it cannot find rows without a match.
    assert "it cannot find rows without a match" in text
    assert "Today and the current time are those of UTC" in text
    # Filters are where SQL goes; relative dates are CURRENT_DATE, never a known date (#56).
    assert "never write a query; SQL goes only in filters" in text
    assert "never with a date you know" in text
    assert "d.day = CURRENT_DATE for today" in text
    # A plain string compared with a time reads as TIMESTAMP '...' does (#58).
    assert "or '2026-10-21 16:00:00' compared with a DateTime or DateTimeTz field" in text
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
    assert set(q.input_schema["properties"]) == {
        "metrics",
        "dimensions",
        "filters",
        "order_by",
        "limit",
    }


@pytest.mark.anyio
async def test_tools_match_library(client):
    r = await client.call_tool("list_model", {})
    # store_productivity is refused in every question, so agents never see it.
    seen = MODEL.model_copy(
        update={"metrics": [m for m in MODEL.metrics if m.name != "store_productivity"]}
    )
    out = r.structured_content
    clock = {k: out[k] for k in ("today", "now")}  # Executor.clock(), tested there
    assert out == summary(seen, clock)
    assert out["now"].startswith(out["today"]) and out["now"].endswith("+00:00")
    r = await client.call_tool("describe_object", {"name": "total_sales"})
    assert r.structured_content == describe(MODEL, "total_sales")
    r = await client.call_tool("search_model", {"text": "profit"})
    assert r.structured_content["result"] == search(MODEL, "profit")


@pytest.mark.anyio
async def test_query_and_error(client):
    r = await client.call_tool(
        "query",
        {"metrics": ["total_sales"], "dimensions": ["item.i_category"],
         "filters": ["date_dim.d_year = 1998"], "limit": 3},
    )  # fmt: skip
    out = r.structured_content
    assert out["columns"] == ["i_category", "total_sales"] and out["row_count"] == 3
    assert "LEFT JOIN tpcds.item" in out["sql"] and "ORDER BY total_sales DESC" in out["sql"]
    assert out["truncated"] and "LIMIT 3 " in out["sql"]  # the question's limit, not 4
    values = [float(row[1]) for row in out["rows"]]
    assert values == sorted(values, reverse=True)
    r = await client.call_tool("query", {"metrics": ["total_sales"]})
    assert r.structured_content["truncated"] is False
    r = await client.call_tool("query", {"metrics": ["total_revenue"]})
    assert not r.is_error and "did you mean: total_sales" in r.structured_content["error"]
    # A query that plans but fails in ClickHouse is a result too, never an exception.
    r = await client.call_tool(
        "query", {"metrics": ["total_sales"], "filters": ["date_dim.d_year = 'abc'"]}
    )
    assert not r.is_error and r.structured_content["error"].startswith("ClickHouse: ")
    assert "\n" not in r.structured_content["error"]


@pytest.mark.anyio
async def test_list_model_reports_an_unreadable_clock(tpcds):
    """The model still comes back when the clock cannot be read, with the reason."""
    from mcp import Client

    from dactopus_ossie_clickhouse.mcp_server import build_server

    class NoClock:
        url = tpcds.url

        def query(self, sql, parameters=None):
            if sql.startswith("SELECT toDate(now("):
                raise RuntimeError("Code: 159. Timeout exceeded\nstack")
            return tpcds.query(sql, parameters=parameters)

    async with Client(build_server(MODEL, NoClock), raise_exceptions=True) as c:
        out = (await c.call_tool("list_model", {})).structured_content
    assert out["error"] == "ClickHouse: Code: 159. Timeout exceeded" and "today" not in out
    assert out["name"] == "tpcds_retail_model" and out["time_zone"] == "UTC"


@pytest.mark.anyio
async def test_concurrent_queries_share_one_process(tpcds):
    """Hosts issue tool calls in parallel and the SDK runs sync tools in threads:
    one server must serve them all without ClickHouse session clashes."""
    import asyncio

    from mcp import Client

    from dactopus_ossie_clickhouse.executor import connect
    from dactopus_ossie_clickhouse.mcp_server import build_server

    async with Client(build_server(MODEL, connect), raise_exceptions=True) as c:
        calls = [c.call_tool("query", {"filters": ["sleep(0.3) = 0"], "metrics": ["total_sales"]})
                 for _ in range(3)]  # fmt: skip
        results = await asyncio.gather(*calls)
    assert [r.structured_content.get("error") for r in results] == [None, None, None]

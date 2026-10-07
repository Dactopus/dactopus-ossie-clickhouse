"""MCP server: a thin adapter over the library for AI agents.

Four tools, no logic of their own: everything they do is a library call.
``query`` takes the aggregation query of Ossie Layer 3 (apache/ossie#246
§5.1.1) as a JSON object. A refusal comes back as a tool error in the
envelope of apache/ossie#529 (``isError``, ``structuredContent.error`` with
a #246 code where one applies, ``suggestions``), never as an exception, so
the planner's "did you mean" hints reach the agent verbatim.

Runs over stdio with ClickHouse credentials from the environment, so one
process serves one ClickHouse user and access control applies unchanged.
``client_factory`` is the seam for a remote server that maps a caller's
identity to a ClickHouse connection.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from importlib.metadata import version
from typing import Annotated, Any

import pydantic_core
from clickhouse_connect.driver.exceptions import OperationalError
from ossie import OssieDocument
from pydantic import WithJsonSchema

from dactopus_ossie_clickhouse.access import Policy
from dactopus_ossie_clickhouse.executor import Executor, Result, connect
from dactopus_ossie_clickhouse.planner import (
    LAYER3_REVISION,
    QUERY_SCHEMA,
    Code,
    PlanError,
    Query,
    Suggestion,
)
from dactopus_ossie_clickhouse.translate import pick_expression

try:  # module level: the SDK resolves the query tool's return annotation here
    from mcp.types import CallToolResult, TextContent
except ImportError:  # pragma: no cover  (build_server says which extra to install)
    pass

DEFAULT_LIMIT = 100  # rows when a query sets no limit: an agent reads every one
QUERY_TOOL = f"""Run an aggregation query and return its rows and SQL. The query follows Ossie
Layer 3, {LAYER3_REVISION} §5.1.1: measures are metric names, dimensions are
dataset.field; where filters rows before aggregation (dataset.field conditions,
e.g. "date_dim.d_year = 1998"); having filters aggregated rows (metric names and
the query's dimensions, e.g. "total_sales > 1000000"); each is a string or a list
(AND). A window metric such as a rank cannot be filtered: select it instead.
order_by: [{{"field": "total_sales", "direction": "DESC"}}], default the first
measure descending, ties broken by the dimensions; NULLs sort as the highest
value, so first in DESC unless "nulls": "LAST" (a key of this server; #246 names
NULLS FIRST / LAST but gives the object no key for them), and a limit may cut
them off. limit defaults to {DEFAULT_LIMIT}.
Example: {{"measures": ["total_sales"], "dimensions": ["item.i_category"],
"where": "date_dim.d_year = 1998", "limit": 5}}. A refusal carries error.code
and suggestions to repair the query by."""

# --- pure views of a model, testable without a server ------------------------


def _ai(obj) -> dict[str, Any]:
    ctx = obj.ai_context
    if ctx is None:
        return {}
    if isinstance(ctx, str):
        return {"instructions": ctx}
    return {k: v for k, v in ctx.model_dump().items() if v}


def _brief(obj, **extra) -> dict[str, Any]:
    d = {"name": obj.name}
    if getattr(obj, "description", None):
        d["description"] = obj.description
    if synonyms := _ai(obj).get("synonyms"):
        d["synonyms"] = list(synonyms)
    return d | extra


def summary(model: OssieDocument) -> dict[str, Any]:
    """The whole model in one screen: names, descriptions, synonyms, no fields."""
    return {
        "name": model.name,
        "description": model.description,
        **_ai(model),
        "datasets": [_brief(d, fields=len(d.fields or [])) for d in model.datasets],
        "metrics": [_brief(m) for m in model.metrics or []],
        "relationships": [f"{r.from_dataset} -> {r.to}" for r in model.relationships or []],
        "usage": (
            "Call describe_object(name) for a dataset's fields or a metric's definition, "
            "search_model(text) to find objects by name or synonym, then query(...)."
        ),
    }


def instructions(model: OssieDocument) -> str:
    """The server's instructions: how to ask, then the model's own instructions.

    They reach the agent whichever tool it calls first; list_model repeats
    the model's part for clients that ignore server instructions."""
    text = (
        f"Semantic model '{model.name}' over ClickHouse. Ask in business terms: pick metrics "
        "(as measures) and dimensions by name, never write SQL. Start with list_model. "
        "When a query is refused, repair it from the error's message and suggestions. "
        "Never add up or rank rows yourself: for a total, run query without dimensions; for "
        "a comparison, query the exact dimensions you need. If the model has no metric or "
        "field for what is asked, say so; do not approximate it with filters on other fields. "
        "Every number you state must come from a query result: rounding and formatting are "
        "fine, but do not derive, estimate or recall other numbers, such as the counts "
        "behind a rate."
    )
    if model_text := _ai(model).get("instructions"):
        text += f"\n\nInstructions of this model:\n{model_text}"
    return text


def _objects(model: OssieDocument):
    for d in model.datasets:
        yield "dataset", d.name, d
        for f in d.fields or []:
            yield "field", f"{d.name}.{f.name}", f
    for m in model.metrics or []:
        yield "metric", m.name, m


def search(model: OssieDocument, text: str, limit: int = 20) -> list[dict[str, Any]]:
    """Objects whose name, description or synonyms contain every word of ``text``."""
    words = text.lower().split()
    hits = []
    for kind, name, obj in _objects(model):
        hay = " ".join([name, obj.description or "", *_ai(obj).get("synonyms", [])]).lower()
        if words and all(w in hay for w in words):
            hits.append({"kind": kind} | _brief(obj) | {"name": name})
    return hits[:limit]


def _edge(r) -> str:
    return f"{r.from_dataset}({', '.join(r.from_columns)}) -> {r.to}({', '.join(r.to_columns)})"


def describe(model: OssieDocument, name: str) -> dict[str, Any]:
    """Everything about one dataset, field (``dataset.field``) or metric."""
    for kind, obj_name, obj in _objects(model):
        if obj_name.upper() != name.upper():
            continue
        out: dict[str, Any] = {"kind": kind} | _brief(obj) | {"name": obj_name}
        ai = _ai(obj)
        if ai.get("instructions"):
            out["instructions"] = ai["instructions"]
        if ai.get("examples"):
            out["examples"] = list(ai["examples"])
        if kind == "dataset":
            out["fields"] = [
                _brief(f, datatype=f.datatype.value if f.datatype else None)
                | ({"is_time": True} if f.is_time_dimension() else {})
                for f in obj.fields or []
            ]
            out["relationships"] = [
                _edge(r) for r in model.relationships or [] if obj_name in (r.from_dataset, r.to)
            ]
        else:
            out["expression"] = pick_expression(obj.expression)
            if obj.datatype:
                out["datatype"] = obj.datatype.value
            if kind == "field" and obj.is_time_dimension():
                out["is_time"] = True
        return out
    close = [n for _, n, _ in _objects(model) if name.lower() in n.lower()][:5]
    return {"error": f"unknown object {name!r}" + (f"; similar: {close}" if close else "")}


def answer(r: Result) -> dict[str, Any]:
    """A query's rows, JSON-ready, with the Layer 3 revision the query followed."""
    return pydantic_core.to_jsonable_python(
        {
            "language": LAYER3_REVISION,
            "columns": r.columns,
            "rows": r.shown_rows(),
            "row_count": len(r.rows),
            "sql": r.sql,
        }
    )


def refusal(
    code: Code, message: str, suggestions: tuple[Suggestion, ...] = (), retryable: bool = False
) -> dict[str, Any]:
    """A refused or failed query in the error envelope of apache/ossie#529."""
    return {
        "status": "error",
        "language": LAYER3_REVISION,
        "error": {"code": str(code), "message": message, "retryable": retryable},
        "suggestions": [{"kind": s.kind, "message": s.message} for s in suggestions],
    }


# --- the server ----------------------------------------------------------------


def build_server(
    model: OssieDocument,
    client_factory: Callable[[], Any] = connect,
    policy: Policy | None = None,
):
    try:
        from mcp.server import MCPServer
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "MCP support needs the extra: pip install 'dactopus-ossie-clickhouse[mcp]'"
        ) from e

    executor = Executor(client_factory(), model, policy)
    m = executor.model  # trimmed to what the connected user may see
    server = MCPServer(
        "dactopus-ossie-clickhouse",
        version=version("dactopus-ossie-clickhouse"),
        instructions=instructions(m),
    )
    server.location = executor.location  # for `serve` to print

    @server.tool()
    def list_model() -> dict[str, Any]:
        """The semantic model: datasets, metrics, relationships, with descriptions and synonyms."""
        return summary(m)

    @server.tool()
    def search_model(text: str) -> list[dict[str, Any]]:
        """Find datasets, fields and metrics whose name, description or synonyms contain
        every word of the text. Try single words too: "returns", then "discount"."""
        return search(m, text)

    @server.tool()
    def describe_object(name: str) -> dict[str, Any]:
        """Full detail for one dataset, field (dataset.field) or metric, including AI hints."""
        return describe(m, name)

    @server.tool(description=QUERY_TOOL)
    def query(query: Annotated[dict[str, Any], WithJsonSchema(QUERY_SCHEMA)]) -> CallToolResult:
        try:
            q = Query.from_dict(query)
            r = executor.execute(q if q.limit is not None else replace(q, limit=DEFAULT_LIMIT))
        except PlanError as e:
            return _error(refusal(e.code, str(e), e.suggestions))
        except Exception as e:  # ClickHouse errors are useful to the agent too
            message = f"ClickHouse: {str(e).splitlines()[0][:400]}"
            return _error(refusal(Code.BACKEND_ERROR, message, (), isinstance(e, OperationalError)))
        out = answer(r)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(out))], structured_content=out
        )

    def _error(out: dict[str, Any]) -> CallToolResult:
        text = f"{out['error']['code']}: {out['error']['message']}"
        return CallToolResult(
            content=[TextContent(type="text", text=text)], structured_content=out, is_error=True
        )

    return server

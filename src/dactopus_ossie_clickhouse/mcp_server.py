"""MCP server: a thin adapter over the library for AI agents.

Four tools, no logic of their own: everything they do is a library call.
``execute_query`` is the tool of the execute_query profile (apache/ossie#529,
module ``execute_query``): it takes a Layer 3 query object (apache/ossie#246)
and returns the rows as embedded CSV with ``structuredContent``. A refusal
comes back as a tool error with its code in ``structuredContent.error`` and
``suggestions``, never as an exception, so the planner's "did you mean"
hints reach the agent verbatim.

Runs over stdio with ClickHouse credentials from the environment, so one
process serves one ClickHouse user and access control applies unchanged.
``client_factory`` is the seam for a remote server that maps a caller's
identity to a ClickHouse connection.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import version
from typing import Any

from ossie import OssieDocument

from dactopus_ossie_clickhouse.access import Policy
from dactopus_ossie_clickhouse.execute_query import (
    CONTRACT_VERSION,
    CSV_META,
    INPUT_SCHEMA,
    MAX_ROWS,
    OUTPUT_SCHEMA,
    Binding,
    Reply,
    call,
)
from dactopus_ossie_clickhouse.executor import Executor, connect
from dactopus_ossie_clickhouse.planner import LAYER3_REVISION
from dactopus_ossie_clickhouse.translate import pick_expression

try:  # module level: the SDK resolves the tool's annotations here
    from mcp.server.mcpserver import Context
    from mcp.types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents
except ImportError:  # pragma: no cover  (build_server says which extra to install)
    pass

# Placeholders, not names: the description is the same for every model, and the
# agent takes names from list_model rather than from an example.
QUERY_TOOL = f"""Run an aggregation query and return its rows as CSV. Arguments: data_source_id,
given in list_model and the server instructions, and query, an Ossie Layer 3 object
(execute_query profile {CONTRACT_VERSION}, {LAYER3_REVISION} §5): measures are
metric names, dimensions are dataset.field; where filters rows before aggregation
(dataset.field conditions, e.g. "<dataset>.<field> = 'value'"); having filters
aggregated rows (metric names and the query's dimensions, e.g. "<metric> > 1000");
each is a string or a non-empty list (AND). A window metric such as a rank cannot be
filtered: select it instead. order_by: [{{"field": "<metric>", "direction": "DESC"}}],
default the first measure descending, ties broken by the dimensions; NULLs sort as
the highest value, so first in DESC unless "nulls": "LAST", and a limit may cut them
off. Without a limit at most {MAX_ROWS} rows come back, marked truncated if there
were more. Example: {{"measures": ["<metric>"], "dimensions": ["<dataset>.<field>"], "where":
"<dataset>.<field> = 'value'", "order_by": [{{"field": "<metric>", "direction":
"DESC"}}], "limit": 5}}. Names come from list_model. A refusal carries error.code
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


def summary(model: OssieDocument, binding: Binding | None = None) -> dict[str, Any]:
    """The whole model in one screen: names, descriptions, synonyms, no fields;
    with a binding, what execute_query resolves against and supports."""
    out = {
        "name": model.name,
        "description": model.description,
        **_ai(model),
        "datasets": [_brief(d, fields=len(d.fields or [])) for d in model.datasets],
        "metrics": [_brief(m) for m in model.metrics or []],
        "relationships": [f"{r.from_dataset} -> {r.to}" for r in model.relationships or []],
        "usage": (
            "Call describe_object(name) for a dataset's fields or a metric's definition, "
            "search_model(text) to find objects by name or synonym, then execute_query(...)."
        ),
    }
    if binding:
        out["binding"] = binding.describe()
    return out


def instructions(model: OssieDocument, binding: Binding | None = None) -> str:
    """The server's instructions: how to ask, then the model's own instructions.

    They reach the agent whichever tool it calls first; list_model repeats
    the model's part for clients that ignore server instructions."""
    text = (
        f"Semantic model '{model.name}' over ClickHouse. Ask in business terms: pick metrics "
        "(as measures) and dimensions by name, never write SQL. Start with list_model. "
    )
    if binding:
        text += f'Call execute_query with data_source_id "{binding.data_source_id}". '
    text += (
        "When a query is refused, repair it from the error's message and suggestions. "
        "Never add up or rank rows yourself: for a total, run a query without dimensions; for "
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


# --- the server ----------------------------------------------------------------


def build_server(
    model: OssieDocument,
    client_factory: Callable[[], Any] = connect,
    policy: Policy | None = None,
):
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.tools import Tool
        from mcp.types import ToolAnnotations
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "MCP support needs the extra: pip install 'dactopus-ossie-clickhouse[mcp]'"
        ) from e

    executor = Executor(client_factory(), model, policy)
    m = executor.model  # trimmed to what the connected user may see
    binding = Binding.of(model)  # the whole model's revision, not the trimmed one's

    def execute_query(ctx: Context, data_source_id: Any = None, query: Any = None):
        # The arguments as sent: the SDK drops unknown keys and decodes a query
        # given as a JSON string, and the profile refuses both.
        return _result(call(executor, binding, ctx.request_context.params.get("arguments")))

    tool = Tool.from_function(
        execute_query,
        description=QUERY_TOOL,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    # The profile's schemas as published, so a client recognizes the tool.
    tool.parameters = INPUT_SCHEMA
    tool.fn_metadata.output_schema = OUTPUT_SCHEMA

    server = MCPServer(
        "dactopus-ossie-clickhouse",
        version=version("dactopus-ossie-clickhouse"),
        instructions=instructions(m, binding),
        tools=[tool],
    )
    server.location = executor.location  # for `serve` to print

    @server.tool()
    def list_model() -> dict[str, Any]:
        """The semantic model: datasets, metrics, relationships, with descriptions and
        synonyms, and the binding execute_query takes."""
        return summary(m, binding)

    @server.tool()
    def search_model(text: str) -> list[dict[str, Any]]:
        """Find datasets, fields and metrics whose name, description or synonyms contain
        every word of the text. Try single words too: "returns", then "discount"."""
        return search(m, text)

    @server.tool()
    def describe_object(name: str) -> dict[str, Any]:
        """Full detail for one dataset, field (dataset.field) or metric, including AI hints."""
        return describe(m, name)

    return server


def _result(reply: Reply) -> CallToolResult:
    content: list[Any] = [TextContent(type="text", text=reply.text)]
    if reply.csv is not None:
        content.append(
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=reply.structured["result"]["resources"][0]["uri"],
                    mime_type="text/csv",
                    text=reply.csv,
                    _meta={CSV_META: reply.csv_meta},
                ),
            )
        )
    return CallToolResult(
        content=content, structured_content=reply.structured, is_error=reply.is_error
    )

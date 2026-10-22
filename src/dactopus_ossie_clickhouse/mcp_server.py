"""MCP server: a thin adapter over the library for AI agents.

Four tools, no logic of their own: everything they do is a library call.
Errors come back as results with an ``error`` field so the planner's
"did you mean" hints reach the agent verbatim.

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
from dactopus_ossie_clickhouse.executor import Executor, connect
from dactopus_ossie_clickhouse.model import time_zone
from dactopus_ossie_clickhouse.planner import PlanError, Query
from dactopus_ossie_clickhouse.translate import pick_expression

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


def summary(model: OssieDocument, clock: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole model in one screen: names, descriptions, synonyms, no fields.
    ``clock`` is ``Executor.clock()``: today and now in the model's zone."""
    return {
        "name": model.name,
        "description": model.description,
        **_ai(model),
        "datasets": [_brief(d, fields=len(d.fields or [])) for d in model.datasets],
        "metrics": [_brief(m) for m in model.metrics or []],
        "relationships": [f"{r.from_dataset} -> {r.to}" for r in model.relationships or []],
        "time_zone": time_zone(model),
        **(clock or {}),
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
        "and dimensions by name and never write a query; SQL goes only in filters, as "
        "conditions on fields and metrics. Start with list_model. Never add up or "
        "rank rows yourself: for a total, run query without dimensions; for a comparison, "
        "query the exact dimensions you need. If the model has no metric or field for what "
        "is asked, say so; do not approximate it with filters on other fields. A filter on "
        "rows that reference what you count (purchases for sessions) keeps each counted row "
        "that has at least one matching row, once; it cannot find rows without a match, and "
        "NOT inside it means a row with a non-matching one, so ask for absence through a "
        "field or metric of the model, such as a last activity date. Every number "
        "you state must come from a query result: rounding and formatting are fine, but do "
        "not derive, estimate or recall other numbers, such as the counts behind a rate. "
        f"Today and the current time are those of {time_zone(model)}, which may not be "
        "your user's: write today, yesterday, this week and any other period counted from "
        "now with CURRENT_DATE or CURRENT_TIMESTAMP, never with a date you know: d.day = "
        "CURRENT_DATE for today, d.day >= CURRENT_DATE - INTERVAL '6' DAY for the last 7 "
        "days, d.day >= DATE_TRUNC('MONTH', CURRENT_DATE) for this month. list_model gives "
        "today and now there; name the date in your answer. A time you write without an "
        "offset "
        "(TIMESTAMP '2026-10-21 16:00:00') is there too; times in results "
        "carry their offset from UTC, and a field's description may say whose dates it holds."
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
        """The semantic model: datasets, metrics, relationships, with descriptions and
        synonyms; today and now in the model's time zone, where CURRENT_DATE is."""
        try:
            clock = executor.clock()  # on every call: a session may outlast midnight
        except Exception as e:
            clock = {"error": _clickhouse_error(e)}
        return summary(m, clock)

    @server.tool()
    def search_model(text: str) -> list[dict[str, Any]]:
        """Find datasets, fields and metrics whose name, description or synonyms contain
        every word of the text. Try single words too: "returns", then "discount"."""
        return search(m, text)

    @server.tool()
    def describe_object(name: str) -> dict[str, Any]:
        """Full detail for one dataset, field (dataset.field) or metric, including AI hints."""
        return describe(m, name)

    @server.tool()
    def query(
        metrics: list[str] = [],  # noqa: B006
        dimensions: list[str] = [],  # noqa: B006
        filters: list[str] = [],  # noqa: B006
        order_by: list[str] = [],  # noqa: B006
        limit: int = 100,
    ) -> dict[str, Any]:
        """Run a semantic query. metrics and dimensions are names from the model
        (dimensions as dataset.field); filters are SQL-like conditions over
        dataset.field, e.g. "date_dim.d_year = 1998", today and periods counted
        from it with CURRENT_DATE, e.g. "date_dim.d_date = CURRENT_DATE", never a
        date you know, or over a metric name,
        e.g. "total_sales > 1000000", which filters the aggregated rows
        (a window metric such as a rank cannot be filtered: select it instead);
        order_by lists metric or dimension names, "name desc" for descending,
        default: first metric descending, ties broken by the dimensions; rows
        with a NULL come last either way, so limit may cut them off. Returns
        rows and the SQL."""
        try:
            r = executor.execute(
                Query(tuple(metrics), tuple(dimensions), tuple(filters), tuple(order_by), limit)
            )
        except PlanError as e:
            return {"error": str(e)}
        except Exception as e:  # ClickHouse errors are useful to the agent too
            return {"error": _clickhouse_error(e)}
        return {
            "columns": r.columns,
            "rows": r.shown_rows(),
            "row_count": len(r.rows),
            "sql": r.sql,
        }

    return server


def _clickhouse_error(e: Exception) -> str:
    return f"ClickHouse: {str(e).splitlines()[0][:400]}"

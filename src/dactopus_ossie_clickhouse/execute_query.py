"""The execute_query profile of apache/ossie#529 (0.4-draft): one call, one reply.

The profile binds the Layer 3 query object (apache/ossie#246) to an MCP tool:
the arguments are ``{data_source_id, query}``, a success embeds the rows as
CSV and describes them in ``structuredContent``, a refusal carries its code in
``error.code``. ``call`` makes the whole reply; the MCP adapter only wraps it in
a ``CallToolResult``, so nothing here needs the MCP SDK. The schemas in
``schemas/`` are the profile's own, copied unchanged: a client that knows them
recognizes the tool.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from typing import Any

from clickhouse_connect.driver.exceptions import OperationalError
from ossie import OssieDocument

from dactopus_ossie_clickhouse.executor import Executor, Result
from dactopus_ossie_clickhouse.planner import LAYER3_REVISION, Code, PlanError, Query, Suggestion

CONTRACT_VERSION = "0.4-draft"
# The commit of apache/ossie#529 whose text and schemas this module follows.
PROFILE_REVISION = "apache/ossie#529@b5418ee"
MAX_ROWS = 100  # rows when a query sets no limit: an agent reads every one
EXTENSION = "io.github.dactopus/clickhouse"  # our key in the profile's `extensions`
CSV_META = "org.apache.ossie/execute_query"  # the profile's key in a CSV resource's _meta
CSV_URI = "file:///result.csv"
NULL, ESCAPE = "\\N", "\\"


def _schema(name: str) -> dict[str, Any]:
    path = files("dactopus_ossie_clickhouse").joinpath(f"schemas/execute-query-{name}.schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


INPUT_SCHEMA = _schema("input")
OUTPUT_SCHEMA = _schema("output")


@dataclass(frozen=True)
class Binding:
    """What a call resolves against: one model at one revision.

    The revision is a hash of the whole model, before access trims it, so a
    changed model gets a new ``data_source_id`` and an agent holding the old
    one is refused rather than answered from a model it has not seen."""

    data_source_id: str
    model: str
    revision: str

    @classmethod
    def of(cls, model: OssieDocument) -> Binding:
        doc = json.dumps(model.model_dump(mode="json", by_alias=True), sort_keys=True)
        revision = hashlib.sha256(doc.encode()).hexdigest()[:12]
        return cls(f"{model.name}@{revision}", model.name, revision)

    def describe(self) -> dict[str, Any]:
        """What the profile says a binding must expose, for list_model."""
        return {
            "data_source_id": self.data_source_id,
            "model": {"id": self.model, "revision": self.revision},
            "profile": f"execute_query {CONTRACT_VERSION} ({PROFILE_REVISION})",
            "foundation": LAYER3_REVISION,
            "query_shapes": ["aggregation"],
            "refused": [
                "scalar queries (fields)",
                "measures other than metric names: no ad-hoc aggregates or windows",
                "dimensions other than dataset.field: no expressions or aliases",
                "joins other than many-to-one paths from one root dataset: no stitching of facts",
                "window functions in having",
            ],
            "output_formats": ["csv"],
            "diagnostics": "suggestions with refusals; no filter value alternatives",
            "max_rows": f"{MAX_ROWS} when the query sets no limit; more are reported as truncated",
        }


@dataclass(frozen=True)
class Reply:
    """A final tool result: text for the agent, structured content, and the CSV."""

    structured: dict[str, Any]
    text: str
    csv: str | None = None
    csv_meta: dict[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return self.structured["status"] == "error"


def call(executor: Executor, binding: Binding, arguments: Any) -> Reply:
    """One execute_query call: check the arguments, plan, run, encode."""
    source = arguments.get("data_source_id") if isinstance(arguments, dict) else None
    try:
        q = _query(binding, arguments)
        r = executor.execute(q, MAX_ROWS)
    except PlanError as e:
        return refusal(binding, source, e.code, str(e), e.suggestions)
    except Exception as e:  # ClickHouse errors are useful to the agent too
        message = f"ClickHouse: {str(e).splitlines()[0][:400]}"
        retryable = isinstance(e, OperationalError)  # a connection or timeout, not the query
        return refusal(binding, source, Code.BACKEND_ERROR, message, retryable=retryable)
    return answer(binding, r, q)


def _query(binding: Binding, arguments: Any) -> Query:
    if not isinstance(arguments, dict) or set(arguments) - {"data_source_id", "query"}:
        raise PlanError(
            "the arguments are data_source_id and query, nothing else", Code.INVALID_ARGUMENT
        )
    source = arguments.get("data_source_id")
    if not isinstance(source, str) or not source.strip():
        raise PlanError(
            f"data_source_id is required: {binding.data_source_id!r} for this model",
            Code.INVALID_ARGUMENT,
            (Suggestion("name", binding.data_source_id),),
        )
    if source != binding.data_source_id:
        raise PlanError(
            f"no data source {source!r}; this server answers {binding.data_source_id!r}",
            Code.SOURCE_UNAVAILABLE,
            (Suggestion("name", binding.data_source_id),),
        )
    if "query" not in arguments:
        raise PlanError("query is required", Code.INVALID_ARGUMENT)
    return Query.from_dict(arguments["query"])


def refusal(
    binding: Binding,
    source: Any,
    code: Code,
    message: str,
    suggestions: tuple[Suggestion, ...] = (),
    retryable: bool = False,
) -> Reply:
    """A refused or failed query. It echoes the data_source_id the caller sent,
    and names the model only when that was this binding's."""
    out: dict[str, Any] = {"contract_version": CONTRACT_VERSION, "status": "error"}
    if isinstance(source, str) and source:
        out["data_source_id"] = source
        if source == binding.data_source_id:
            out["model"] = {"id": binding.model, "revision": binding.revision}
    out |= {
        "error": {"code": str(code), "message": message, "retryable": retryable},
        # The planner's refusals come with what it found; ClickHouse's do not.
        "diagnostics": {"state": "not_applicable" if code == Code.BACKEND_ERROR else "completed"},
        "suggestions": [{"kind": s.kind, "message": s.message} for s in suggestions],
        "filter_value_alternatives": [],
    }
    return Reply(out, f"{code}: {message}")


def answer(binding: Binding, r: Result, q: Query) -> Reply:
    """A query's rows: all of them as CSV, the first MAX_ROWS also as a preview."""
    types = [datatype(t) for t in r.types] if r.types else [None] * len(r.columns)
    columns = [
        {"name": c} | ({"datatype": t} if t else {}) for c, t in zip(r.columns, types, strict=True)
    ]
    # A value of no profile type (Time among them) goes out as ClickHouse prints it.
    rows = [
        [value(v if t else p, t) for v, p, t in zip(row, printed, types, strict=True)]
        for row, printed in zip(r.rows, r.shown_rows(), strict=True)
    ]
    completeness = "complete" if r.complete else "truncated"
    n = len(rows)
    meta = {
        "columns": columns,
        "row_count": n,
        "completeness": completeness,
        "csv_null_value": NULL,
        "csv_escape_prefix": ESCAPE,
    }
    if n == 0 and q.where:
        # Zero rows after a filter is often a value spelled differently in the data.
        diagnostics = {
            "state": "unavailable",
            "message": "This server does not look up filter values; to see the values a "
            "field takes, query it as a dimension.",
        }
    else:
        diagnostics = {"state": "not_applicable"}
    out = {
        "contract_version": CONTRACT_VERSION,
        "status": "success",
        "data_source_id": binding.data_source_id,
        "model": {"id": binding.model, "revision": binding.revision},
        "preview": {
            "columns": columns,
            "rows": rows[:MAX_ROWS],
            "row_count": min(n, MAX_ROWS),
            "has_more": n > MAX_ROWS,
            "selection": "first_rows",
        },
        "result": {
            "row_count": n,
            "completeness": completeness,
            "resources": [
                {
                    "uri": CSV_URI,
                    "name": CSV_URI.rsplit("/", 1)[1],
                    "format": "csv",
                    "mime_type": "text/csv",
                    "csv_null_value": NULL,
                    "csv_escape_prefix": ESCAPE,
                }
            ],
            "extensions": {EXTENSION: {"sql": r.sql}},
        },
        "diagnostics": diagnostics,
        "suggestions": [],
        "filter_value_alternatives": [],
    }
    status = (
        "complete"
        if r.complete
        else f"truncated: there are more than {MAX_ROWS}; set a limit or narrow the query"
    )
    text = f"{n} row{'' if n == 1 else 's'}; {status}. Data: {CSV_URI.rsplit('/', 1)[1]}."
    if diagnostics.get("message"):
        text += " " + diagnostics["message"]
    return Reply(out, text, to_csv(r.columns, rows, types), meta)


# --- values -------------------------------------------------------------------

_WRAPPER = re.compile(r"(?:Nullable|LowCardinality)\((.*)\)")
_TYPES = [
    (re.compile(r"U?Int\d+"), "Integer"),
    (re.compile(r"Decimal(?:\d+)?\(.*\)"), "Decimal"),
    (re.compile(r"B?Float\d+"), "Float"),
    (re.compile(r"Bool"), "Boolean"),
    (re.compile(r"Date(?:32)?"), "Date"),
    # A ClickHouse DateTime is an instant; its timezone only changes how it reads.
    (re.compile(r"DateTime(?:64)?(?:\(.*\))?"), "DateTimeTz"),
    (re.compile(r"String|FixedString\(\d+\)|Enum(?:8|16)\(.*\)|UUID|IPv[46]"), "String"),
]


def datatype(clickhouse_type: str) -> str | None:
    """The profile's logical type for a ClickHouse column type; None when it has none
    (arrays, maps, Time and the like), and the value goes out as text."""
    t = clickhouse_type
    while m := _WRAPPER.fullmatch(t):
        t = m[1]
    return next((name for pattern, name in _TYPES if pattern.fullmatch(t)), None)


def value(v: Any, datatype: str | None) -> Any:
    """A cell as the profile's JSON encodes it: exact numbers as decimal strings,
    floats as numbers, dates and instants as ISO 8601."""
    if v is None:
        return None
    if datatype == "Integer":
        return str(int(v))
    if datatype == "Decimal":
        return format(v if isinstance(v, Decimal) else Decimal(str(v)), "f")
    if datatype == "Float":
        return float(v)  # nan and inf are None already (executor)
    if datatype == "Boolean":
        return bool(v)
    if datatype == "Date":
        return v.isoformat()
    if datatype == "DateTimeTz":
        # clickhouse-connect leaves a DateTime('UTC') naive: it is UTC.
        return (v if v.tzinfo else v.replace(tzinfo=dt.UTC)).isoformat()
    if isinstance(v, bytes):  # FixedString
        return v.decode("utf-8", "replace")
    return str(v)


def to_csv(names: list[str], rows: list[list[Any]], types: list[str | None]) -> str:
    """RFC 4180 CSV with the profile's encoding: NULL is an unquoted \\N, and a
    string that starts with a backslash gets one more, so \\N the string is \\\\N."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(names)
    for row in rows:
        w.writerow(_csv_cell(v, t) for v, t in zip(row, types, strict=True))
    return out.getvalue()


def _csv_cell(v: Any, datatype: str | None) -> str:
    if v is None:
        return NULL
    if datatype == "Boolean":
        return "true" if v else "false"
    if datatype == "Float":
        return repr(v)
    return ESCAPE + v if v.startswith(ESCAPE) else v

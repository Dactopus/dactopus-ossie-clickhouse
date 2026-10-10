"""The execute_query profile (apache/ossie#529): arguments, replies, values, CSV."""

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import jsonschema
import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.execute_query import (
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
    Binding,
    answer,
    call,
    datatype,
    to_csv,
    value,
)
from dactopus_ossie_clickhouse.executor import Result
from dactopus_ossie_clickhouse.planner import Code, PlanError, Query
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)
BINDING = Binding.of(MODEL)
SOURCE = BINDING.data_source_id


def conforms(structured):
    jsonschema.validate(structured, OUTPUT_SCHEMA)
    return True


class Stub:
    """An executor that answers every query with one result, or fails."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def execute(self, q, max_rows=None):
        self.calls.append((q, max_rows))
        if self.error:
            raise self.error
        return self.result


def test_schemas_are_the_profiles():
    assert INPUT_SCHEMA["$id"] == "urn:apache:ossie:execute-query:input:0.4-draft"
    assert OUTPUT_SCHEMA["$id"] == "urn:apache:ossie:execute-query:output:0.4-draft"
    assert OUTPUT_SCHEMA["properties"]["contract_version"] == {"const": "0.4-draft"}


def test_binding_changes_with_the_model():
    assert Binding.of(MODEL) == BINDING
    assert SOURCE == f"tpcds_retail_model@{BINDING.revision}" and len(BINDING.revision) == 12
    changed = MODEL.model_copy(update={"description": "another"})
    assert Binding.of(changed).revision != BINDING.revision
    d = BINDING.describe()
    assert d["data_source_id"] == SOURCE and d["query_shapes"] == ["aggregation"]
    assert d["foundation"] == "apache/ossie#246@cc0d070"


# Query objects on both sides of the profile's query schema. from_dict must
# accept exactly the ones the schema accepts; the shape codes are #246's.
QUERIES = [
    ({"measures": ["m"]}, None),
    ({"dimensions": ["d.f"], "measures": []}, None),
    ({"measures": ["m"], "where": "d.f = 1", "having": ["m > 1", "m < 9"]}, None),
    ({"measures": ["m"], "order_by": [{"field": "m", "direction": "DESC", "nulls": "LAST"}]}, None),
    ({"measures": ["m"], "limit": 0}, None),
    ({"fields": ["d.f"], "where": "d.f > 1", "order_by": [{"field": "d.f"}]}, None),
    ({}, Code.E_EMPTY_AGGREGATION_QUERY),
    ({"dimensions": [], "measures": []}, Code.E_EMPTY_AGGREGATION_QUERY),
    ({"fields": []}, Code.E_EMPTY_SCALAR_QUERY),
    ({"fields": ["d.f"], "dimensions": []}, Code.E_MIXED_QUERY_SHAPE),
    ({"fields": ["d.f"], "having": "x > 1"}, Code.E_MIXED_QUERY_SHAPE),
    ({"measures": ["m"], "where": []}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "where": " "}, Code.INVALID_ARGUMENT),
    ({"measures": ["m", ""]}, Code.INVALID_ARGUMENT),
    ({"measures": None}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "order_by": []}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "order_by": [{"field": "m", "direction": "desc"}]}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "order_by": [{"field": "m", "nulls": None}]}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "order_by": [{"direction": "ASC"}]}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "limit": True}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "limit": 1.0}, None),  # JSON Schema's integer: 1.0 is one
    ({"measures": ["m"], "limit": 1.5}, Code.INVALID_ARGUMENT),
    ({"measures": ["m"], "query": {}}, Code.INVALID_ARGUMENT),
    ("{}", Code.INVALID_ARGUMENT),
]


@pytest.mark.parametrize(("query", "code"), QUERIES)
def test_from_dict_accepts_what_the_query_schema_accepts(query, code):
    valid = jsonschema.Draft202012Validator(INPUT_SCHEMA).is_valid(
        {"data_source_id": "x", "query": query}
    )
    assert valid == (code is None)
    if code is None:
        Query.from_dict(query)
    else:
        with pytest.raises(PlanError) as e:
            Query.from_dict(query)
        assert e.value.code == code


@pytest.mark.parametrize(
    ("arguments", "code", "echo"),
    [
        (None, Code.INVALID_ARGUMENT, False),
        ({"query": {"measures": ["m"]}}, Code.INVALID_ARGUMENT, False),
        ({"data_source_id": " ", "query": {}}, Code.INVALID_ARGUMENT, True),
        ({"data_source_id": 7, "query": {}}, Code.INVALID_ARGUMENT, False),
        ({"data_source_id": SOURCE}, Code.INVALID_ARGUMENT, True),
        ({"data_source_id": SOURCE, "query": {}, "limit": 5}, Code.INVALID_ARGUMENT, True),
        ({"data_source_id": "tpcds_retail_model", "query": {}}, Code.SOURCE_UNAVAILABLE, True),
    ],
)
def test_arguments_are_checked_before_the_query(arguments, code, echo):
    ex = Stub()
    reply = call(ex, BINDING, arguments)
    out = reply.structured
    assert reply.is_error and out["error"]["code"] == code and conforms(out)
    assert ("data_source_id" in out) == echo and not ex.calls
    if echo:
        assert out["data_source_id"] == arguments["data_source_id"]
    # Only the binding's own id names the model.
    assert ("model" in out) == (echo and arguments["data_source_id"] == SOURCE)
    if code == Code.SOURCE_UNAVAILABLE:
        assert out["suggestions"] == [{"kind": "name", "message": SOURCE}]


def test_refusals_and_backend_errors():
    from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

    args = {"data_source_id": SOURCE, "query": {"measures": ["m"]}}
    reply = call(Stub(error=PlanError("unknown metric 'm'", Code.E_NAME_NOT_FOUND)), BINDING, args)
    assert reply.text == "E_NAME_NOT_FOUND: unknown metric 'm'" and conforms(reply.structured)
    assert reply.structured["diagnostics"] == {"state": "completed"}
    reply = call(Stub(error=DatabaseError("Code: 43.\nstack")), BINDING, args)
    err = reply.structured["error"]
    assert err == {"code": "BACKEND_ERROR", "message": "ClickHouse: Code: 43.", "retryable": False}
    assert reply.structured["diagnostics"] == {"state": "not_applicable"}
    reply = call(Stub(error=OperationalError("timed out")), BINDING, args)
    assert reply.structured["error"]["retryable"] is True


def test_the_ceiling_applies_only_without_a_limit():
    ex = Stub(Result(["m"], [], "SELECT", ["UInt64"]))
    call(ex, BINDING, {"data_source_id": SOURCE, "query": {"measures": ["m"], "limit": 500}})
    call(ex, BINDING, {"data_source_id": SOURCE, "query": {"measures": ["m"]}})
    assert [(q.limit, max_rows) for q, max_rows in ex.calls] == [(500, 100), (None, 100)]


@pytest.mark.parametrize(
    ("clickhouse", "ossie"),
    [
        ("UInt64", "Integer"),
        ("Nullable(Int128)", "Integer"),
        ("Decimal(38, 4)", "Decimal"),
        ("Nullable(Decimal(18, 2))", "Decimal"),
        ("Float32", "Float"),
        ("Bool", "Boolean"),
        ("LowCardinality(Nullable(String))", "String"),
        ("FixedString(3)", "String"),
        ("Enum8('a' = 1)", "String"),
        ("UUID", "String"),
        ("Date32", "Date"),
        ("DateTime", "DateTimeTz"),
        ("DateTime64(3, 'Europe/Berlin')", "DateTimeTz"),
        ("Array(UInt8)", None),
        ("Time", None),
    ],
)
def test_datatypes(clickhouse, ossie):
    assert datatype(clickhouse) == ossie


def test_values_and_csv():
    berlin = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=ZoneInfo("Europe/Berlin"))
    cells = [
        (None, "String", None, r"\N"),
        ("\\N", "String", "\\N", r"\\N"),
        ("\\x", "String", "\\x", r"\\x"),
        ("", "String", "", ""),
        ('a,"b"', "String", 'a,"b"', '"a,""b"""'),
        ("two\nlines", "String", "two\nlines", '"two\nlines"'),
        (18446744073709551615, "Integer", "18446744073709551615", "18446744073709551615"),
        (Decimal("1.50"), "Decimal", "1.50", "1.50"),
        (Decimal("1E+3"), "Decimal", "1000", "1000"),
        (0.1, "Float", 0.1, "0.1"),
        (1e20, "Float", 1e20, "1e+20"),
        (True, "Boolean", True, "true"),
        (dt.date(2026, 1, 2), "Date", "2026-01-02", "2026-01-02"),
        (berlin, "DateTimeTz", "2026-01-02T03:04:05+01:00", "2026-01-02T03:04:05+01:00"),
        # clickhouse-connect leaves DateTime('UTC') naive.
        (dt.datetime(2026, 1, 2), "DateTimeTz", "2026-01-02T00:00:00+00:00", None),
        ([1, 2], None, "[1, 2]", '"[1, 2]"'),
    ]
    for v, t, json_value, _ in cells:
        assert value(v, t) == json_value
    names = [f"c{i}" for i in range(len(cells))]
    types = [t for _, t, _, _ in cells]
    text = to_csv(names, [[json_value for _, _, json_value, _ in cells]], types)
    header, row = text.split("\r\n", 1)
    assert header == ",".join(names) and row.endswith("\r\n")
    expected = [c if c is not None else j for _, _, j, c in cells]
    assert row == ",".join(expected) + "\r\n"


def test_answer():
    r = Result(
        ["country", "revenue"],
        [("FR", Decimal("1.5")), (None, None)],
        "SELECT ...",
        ["LowCardinality(String)", "Nullable(Decimal(18, 2))"],
    )
    reply = answer(BINDING, r, Query(("revenue",), ("c.country",)))
    out = reply.structured
    assert conforms(out) and not reply.is_error
    assert out["preview"]["rows"] == [["FR", "1.5"], [None, None]]
    assert out["result"]["extensions"]["io.github.dactopus/clickhouse"] == {"sql": "SELECT ..."}
    assert reply.csv == "country,revenue\r\nFR,1.5\r\n\\N,\\N\r\n"
    assert reply.csv_meta["columns"] == out["preview"]["columns"]
    assert reply.text == "2 rows; complete. Data: result.csv."
    cut = Result(["n"], [(1,)], "SELECT", ["UInt8"], complete=False)
    reply = answer(BINDING, cut, Query(("n",)))
    assert reply.structured["result"]["completeness"] == "truncated"
    assert reply.text.startswith("1 row; truncated: there are more than 100")
    # Nothing found after a filter: say that values are not looked up.
    empty = Result(["n"], [], "SELECT", ["UInt8"])
    reply = answer(BINDING, empty, Query(("n",), where=("c.country = 'Frnace'",)))
    assert reply.structured["diagnostics"]["state"] == "unavailable" and conforms(reply.structured)
    assert reply.csv == "n\r\n" and "does not look up filter values" in reply.text

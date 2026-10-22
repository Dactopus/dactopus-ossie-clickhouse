"""Values as the CLI and the MCP server show them, compared with ClickHouse's own text.

clickhouse-connect reads Time as a timedelta and DateTime as a datetime; each
surface used to print them its own way (#40)."""

import json

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import Query

COLUMNS = {
    "t": "Nullable(Time)",
    "t3": "Time64(3)",
    "tokyo": "DateTime('Asia/Tokyo')",
    "plain": "DateTime",
    "micro": "DateTime64(6)",
    "day": "Date",
}
ROWS = (
    "(1, '10:30:00', '10:30:00.125', '2024-03-15 10:30:45', '2024-03-15 10:30:45', "
    "'2024-03-15 10:30:45.123456', '2024-03-15'), "
    # A negative Time, and one under a second: str() of either is "-1 day, ...".
    "(2, '-01:02:03', '-00:00:00.5', '1999-12-31 23:59:59', '2000-01-01 00:00:00', "
    "'2000-01-01 00:00:00.000001', '1999-12-31'), "
    # Over 24 hours: str() counts days.
    "(3, '100:00:00', '999:59:59.999', '2024-03-15 00:00:00', '2024-03-15 00:00:00', "
    "'2024-03-15 00:00:00', '2024-03-15'), "
    "(4, NULL, '00:00:00', '2024-03-15 00:00:00', '2024-03-15 00:00:00', "
    "'2024-03-15 00:00:00', '2024-03-15')"
)
EXPRESSION = "{{dialects: [{{dialect: ANSI_SQL, expression: {}}}]}}"
FIELDS = "".join(
    f"      - name: {c}\n        expression: {EXPRESSION.format(c)}\n" for c in ("id", *COLUMNS)
)
MODEL = f"""version: "0.2.0.dev0"
name: values_model
datasets:
  - name: v
    source: ossie_values.v
    primary_key: [id]
    fields:
{FIELDS}metrics:
  - name: rows
    expression: {{dialects: [{{dialect: ANSI_SQL, expression: COUNT(v.id)}}]}}
"""
DIMENSIONS = tuple(f"v.{c}" for c in ("id", *COLUMNS))


@pytest.fixture(scope="module")
def model(clickhouse, tmp_path_factory):
    columns = ", ".join(f"{c} {t}" for c, t in COLUMNS.items())
    clickhouse.command("CREATE DATABASE IF NOT EXISTS ossie_values")
    clickhouse.command("DROP TABLE IF EXISTS ossie_values.v")
    clickhouse.command(
        f"CREATE TABLE ossie_values.v (id Int32, {columns}) ENGINE = MergeTree ORDER BY id"
    )
    clickhouse.command(f"INSERT INTO ossie_values.v VALUES {ROWS}")
    path = tmp_path_factory.mktemp("values") / "values.yaml"
    path.write_text(MODEL)
    yield path
    clickhouse.command("DROP DATABASE ossie_values")


@pytest.fixture(scope="module")
def shown(clickhouse, model):
    return Executor(clickhouse, load_model(model)).execute(Query((), DIMENSIONS)).shown_rows()


def test_values_read_as_clickhouse_prints_them(clickhouse, shown):
    # A DateTime gets its offset from UTC, which ClickHouse's text leaves out.
    text = ", ".join(
        f"concat(toString({c}), {_offset_sql(c)})" if t.startswith("DateTime") else f"toString({c})"
        for c, t in {"id": "Int32", **COLUMNS}.items()
    )
    printed = clickhouse.query(f"SELECT {text} FROM ossie_values.v ORDER BY id").result_rows
    # A Date stays a date, whose str() is ClickHouse's text already.
    assert [tuple(None if v is None else str(v) for v in r) for r in shown] == printed
    assert shown[1][1:3] == ["-01:02:03", "-00:00:00.500"] and shown[2][1] == "100:00:00"
    assert shown[0][3] == "2024-03-15 10:30:45+09:00"


def _offset_sql(c: str) -> str:
    """+09:00 from ClickHouse's own offset of the value, in seconds."""
    s = f"timeZoneOffset({c})"
    return (
        f"concat(if({s} < 0, '-', '+'), leftPad(toString(intDiv(abs({s}), 3600)), 2, '0'), "
        f"':', leftPad(toString(intDiv(abs({s}), 60) % 60), 2, '0'))"
    )


def test_shown_values_filter_back_to_their_row(clickhouse, model, shown):
    ex = Executor(clickhouse, load_model(model))
    for row in shown:
        for column, value in zip(COLUMNS, row[1:], strict=True):
            if value is None:
                continue
            literals = [f"'{value}'"]
            if COLUMNS[column].startswith("DateTime"):
                literals.append(f"TIMESTAMP '{value}'")  # read in the model's zone, but
            for literal in literals:  # the offset names the instant
                q = Query(("rows",), ("v.id",), (f"v.{column} = {literal}", f"v.id = {row[0]}"))
                assert ex.execute(q).rows == [(row[0], 1)], (column, literal)


@pytest.mark.anyio
async def test_cli_json_and_mcp_show_the_same_values(clickhouse, model, shown, capsys):
    from mcp import Client

    from dactopus_ossie_clickhouse.mcp_server import build_server

    args = ["query", str(model), "--url", clickhouse.url, "--json"]
    assert main([*args, *(a for d in DIMENSIONS for a in ("-d", d))]) == 0
    cli = [list(r.values()) for r in json.loads(capsys.readouterr().out)]
    async with Client(build_server(load_model(model), lambda: clickhouse)) as c:
        mcp = (await c.call_tool("query", {"dimensions": list(DIMENSIONS)})).structured_content
    assert cli == mcp["rows"] == [[str(v) if k == 6 else v for k, v in enumerate(r)] for r in shown]


@pytest.fixture
def anyio_backend():
    return "asyncio"

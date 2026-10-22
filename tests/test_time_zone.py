"""The model's time zone: the current date and wall-clock time there, and timestamp
literals read there, whatever the server's zone and the column's (#52).

Columns are in Asia/Makassar and models in zones far from it and from UTC, so a
value taken in the wrong zone shows; a server in UTC would hide most of it.
Expected values come from Python's zoneinfo, not from ClickHouse."""

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import Query

EXPRESSION = "{{dialects: [{{dialect: ANSI_SQL, expression: {}}}]}}"
METRICS = {
    "events": "COUNT(e.id)",
    "now_here": "CURRENT_TIMESTAMP",
    "today_here": "CURRENT_DATE",
    # 7 hours after the reading: 23:00 on the 21st in New York, the 22nd in UTC.
    "hours_after": "DATEDIFF(hour, MAX(e.at), TIMESTAMP '2026-10-22 03:00:00+00:00')",
    "days_after": "DATEDIFF(day, MAX(e.at), TIMESTAMP '2026-10-22 03:00:00+00:00')",
}
# 2026-10-21 20:00 UTC: 04:00 on the 22nd in Makassar, 16:00 on the 21st in New York.
INSTANT = dt.datetime(2026, 10, 21, 20, tzinfo=dt.UTC)


def model_text(zone: str | None) -> str:
    extension = (
        f"custom_extensions:\n  - vendor_name: CLICKHOUSE\n"
        f"    data: '{json.dumps({'time_zone': zone})}'\n"
        if zone
        else ""
    )
    metrics = "".join(
        f"  - name: {n}\n    expression: {EXPRESSION.format(json.dumps(e))}\n"
        for n, e in METRICS.items()
    )
    return f"""version: "0.2.0.dev0"
name: zone_model
{extension}datasets:
  - name: e
    source: ossie_zone.e
    primary_key: [id]
    fields:
      - name: id
        expression: {EXPRESSION.format("id")}
      - name: at
        expression: {EXPRESSION.format("at")}
metrics:
{metrics}"""


@pytest.fixture(scope="module")
def table(clickhouse):
    clickhouse.command("CREATE DATABASE IF NOT EXISTS ossie_zone")
    clickhouse.command("DROP TABLE IF EXISTS ossie_zone.e")
    clickhouse.command(
        "CREATE TABLE ossie_zone.e (id Int32, at DateTime('Asia/Makassar')) "
        "ENGINE = MergeTree ORDER BY id"
    )
    clickhouse.command(f"INSERT INTO ossie_zone.e VALUES (1, {int(INSTANT.timestamp())})")
    yield
    clickhouse.command("DROP DATABASE ossie_zone")


def executor(clickhouse, tmp_path, zone: str | None) -> Executor:
    path = tmp_path / "zone.yaml"
    path.write_text(model_text(zone))
    return Executor(clickhouse, load_model(path))


@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago", None])
def test_current_date_and_time_are_the_model_zone(clickhouse, table, tmp_path, zone):
    # UTC+14 and UTC-11 are on different dates at every hour.
    tz = ZoneInfo(zone or "UTC")
    ex = executor(clickhouse, tmp_path, zone)
    before = dt.datetime.now(tz)
    res = ex.execute(Query(("now_here", "today_here")))
    after = dt.datetime.now(tz)
    assert "FROM" not in res.sql  # reads no dataset
    [(now, today)] = res.shown_rows()
    assert today in {before.date(), after.date()}
    shown = dt.datetime.fromisoformat(now)
    assert shown.utcoffset() == before.utcoffset()  # the zone's wall clock, with its offset
    assert before - dt.timedelta(seconds=1) <= shown <= after + dt.timedelta(seconds=1)


def test_current_date_is_on_every_row(clickhouse, table, tmp_path):
    ex = executor(clickhouse, tmp_path, "Pacific/Kiritimati")
    # With dimensions the current date is the same on every row.
    before = dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    [(_, events, today)] = ex.execute(Query(("events", "today_here"), ("e.id",))).rows
    after = dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    assert events == 1 and today in {before, after}


@pytest.mark.parametrize(
    ("zone", "literal", "matches"),
    [
        # Without a zone, UTC: the column's zone and the server's do not count.
        (None, "2026-10-21 20:00:00", True),
        (None, "2026-10-22 04:00:00", False),
        # The model's zone.
        ("America/New_York", "2026-10-21 16:00:00", True),
        ("America/New_York", "2026-10-21 20:00:00", False),
        # An offset names the instant in any zone: values are shown with one.
        ("America/New_York", "2026-10-22 04:00:00+08:00", True),
        (None, "2026-10-21 20:00:00+00:00", True),
    ],
)
def test_timestamp_literal_is_read_in_the_model_zone(
    clickhouse, table, tmp_path, zone, literal, matches
):
    ex = executor(clickhouse, tmp_path, zone)
    res = ex.execute(Query(("events",), filters=(f"e.at = TIMESTAMP '{literal}'",)))
    assert res.rows == [(1 if matches else 0,)]


def test_shown_time_carries_the_column_zone(clickhouse, table, tmp_path):
    ex = executor(clickhouse, tmp_path, "America/New_York")
    [(_, at)] = ex.execute(Query((), ("e.id", "e.at"))).shown_rows()
    assert at == "2026-10-22 04:00:00+08:00"
    assert dt.datetime.fromisoformat(at) == INSTANT


@pytest.mark.parametrize(("zone", "days"), [("America/New_York", 0), (None, 1)])
def test_datediff_counts_in_the_model_zone(clickhouse, table, tmp_path, zone, days):
    """The column is in Asia/Makassar and the literal in the model's zone: ClickHouse
    miscounts hours across two zones unless dateDiff is given one."""
    ex = executor(clickhouse, tmp_path, zone)
    assert ex.execute(Query(("hours_after", "days_after"))).rows == [(7, days)]

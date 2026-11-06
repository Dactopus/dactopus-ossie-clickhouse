"""Aggregates that need other values from a key stored twice (docs/design.md, "A key
stored twice on the one side").

A value carried in the derived table of the one side for one aggregate (the condition of a
FILTER on that dataset, its column in an argument) keeps apart the copies of a key, so any
other aggregate of the same SELECT that counts repeated rows would count a root row once
for each. Layer 2 takes the DISTINCT of each aggregate on its own, so such aggregates are
answered in SELECTs of their own and merged on the dimensions, as the aggregates of
different datasets are (Layer 3, apache/ossie#653 section 5.4). DuckDB runs Layer 2 for
the expected values, on the data of ``tests/test_dedup.py`` and on the same data with each
key stored once, where the answers are the plain join's.
"""

import copy

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Query
from tests.test_dedup import DATA, DUCK, MODEL, ORDERS_JOIN, layer2

UNIQUE = {  # the keys of C1, C3 and the channel stored once
    "customers": (
        DATA["customers"][0],
        "(1, 'EAST', 1, []), (2, 'EAST', 1, []), (3, 'WEST', 1, []), (4, 'NORTH', 1, ['vip'])",
    ),
    "channels": (DATA["channels"][0], "(1, 'web'), (2, 'shop')"),
}
ROWS = {
    "repeated": {
        "customers": "(1, 'EAST', 1), (1, 'EAST', -1), (2, 'EAST', 1), (3, 'WEST', 1), "
        "(3, 'SOUTH', 1), (4, 'NORTH', 1)",
        "channels": DATA["channels"][1],
    },
    "unique": {
        "customers": "(1, 'EAST', 1), (2, 'EAST', 1), (3, 'WEST', 1), (4, 'NORTH', 1)",
        "channels": UNIQUE["channels"][1],
    },
}


@pytest.fixture(scope="module", params=["repeated", "unique"])
def world(request, clickhouse, tmp_path_factory):
    """An executor over the data and DuckDB over the same rows."""
    import duckdb

    kind = request.param
    db = f"ossie_dedup_carry_{kind}"
    tables = {t: UNIQUE.get(t, DATA[t]) if kind == "unique" else DATA[t] for t in DATA}
    clickhouse.command(f"CREATE DATABASE IF NOT EXISTS {db}")
    for table, (columns, rows) in tables.items():
        clickhouse.command(f"DROP TABLE IF EXISTS {db}.{table}")
        clickhouse.command(f"CREATE TABLE {db}.{table} ({columns}) ENGINE = MergeTree ORDER BY id")
        clickhouse.command(f"INSERT INTO {db}.{table} VALUES {rows}")
    model = copy.deepcopy(MODEL)
    for ds in model["datasets"]:
        ds["source"] = ds["source"].replace("ossie_dedup.", f"{db}.")
    path = tmp_path_factory.mktemp(db) / "m.yaml"
    path.write_text(yaml.safe_dump(model, sort_keys=False))
    con = duckdb.connect()
    for table, columns in DUCK.items():
        con.execute(f"CREATE TABLE {table} ({columns})")
        con.execute(f"INSERT INTO {table} VALUES {ROWS[kind].get(table, DATA[table][1])}")
    yield kind, Executor(clickhouse, load_model(path)), con
    con.close()
    clickhouse.command(f"DROP DATABASE {db}")


def number(v):
    return None if v is None else float(v)


def by_region(con, args="o.amount", where="TRUE"):
    return dict(layer2(con, "o", args, {"region": "c.region"}, ORDERS_JOIN, where))


def total(con, args="o.amount", where="TRUE"):
    return number(layer2(con, "o", args, {}, ORDERS_JOIN, where)[0][0])


def rows_by_region(got):
    return {r[0]: tuple(number(v) for v in r[1:]) for r in got.rows}


def test_a_filter_on_the_one_side_beside_a_plain_sum_in_total(world):
    kind, ex, con = world
    got = ex.execute(Query(("west_revenue", "total_revenue")))
    west, whole = total(con, where="c.region = 'WEST'"), total(con)
    assert [number(v) for v in got.rows[0]] == [west, whole]
    if kind == "repeated":
        assert (west, whole) == (300, 690)  # C3 is WEST and SOUTH: O4 once in the total
    assert " UNION ALL " in got.sql


def test_a_filter_on_the_one_side_beside_a_plain_sum_by_region(world):
    kind, ex, con = world
    got = rows_by_region(
        ex.execute(Query(("west_revenue", "total_revenue"), ("customers.region",)))
    )
    sums = by_region(con)
    west = by_region(con, where="c.region = 'WEST'")
    assert got == {r: (number(west.get(r)), number(v)) for r, v in sums.items()}
    if kind == "repeated":
        assert got["WEST"] == (300, 300) and got["SOUTH"] == (None, 300) and got[None] == (None, 40)


def test_a_column_of_the_one_side_in_an_argument_beside_a_plain_sum(world):
    kind, ex, con = world
    q = Query(("total_revenue", "weighted_by_customer"))
    got = ex.execute(q)
    assert [number(v) for v in got.rows[0]] == [
        total(con),
        total(con, "o.amount * c.weight"),
    ]
    if kind == "repeated":
        assert [number(v) for v in got.rows[0]] == [690, 350]  # O1 and O2 cancel: 1 and -1
    got = rows_by_region(
        ex.execute(Query(("total_revenue", "weighted_by_customer"), ("customers.region",)))
    )
    sums, weighted = by_region(con), by_region(con, "o.amount * c.weight")
    assert got == {r: (number(v), number(weighted[r])) for r, v in sums.items()}


def test_one_metric_with_aggregates_that_need_other_values(world):
    kind, ex, con = world
    got = ex.execute(Query(("west_share",)))
    expected = total(con, where="c.region = 'WEST'") / total(con)
    assert got.rows[0][0] == pytest.approx(expected)
    if kind == "repeated":
        assert expected == pytest.approx(300 / 690)
    by = rows_by_region(ex.execute(Query(("west_share",), ("customers.region",))))
    sums, west = by_region(con), by_region(con, where="c.region = 'WEST'")
    for region, value in sums.items():
        want = None if west.get(region) is None else float(west[region]) / float(value)
        assert (by[region][0] is None) if want is None else by[region][0] == pytest.approx(want)


def test_the_same_values_in_every_aggregate_stay_in_one_select(world):
    _, ex, con = world
    got = ex.execute(Query(("west_revenue", "west_orders"), ("customers.region",)))
    assert " UNION ALL " not in got.sql
    sums = by_region(con, where="c.region = 'WEST'")
    counts = dict(
        layer2(con, "o", "o.id", {"region": "c.region"}, ORDERS_JOIN, "c.region = 'WEST'", "COUNT")
    )
    assert rows_by_region(got) == {
        r: (number(sums.get(r)), number(counts.get(r, 0))) for r in rows_by_region(got)
    }
    assert rows_by_region(got)["WEST"] == (300, 1)


def test_an_aggregate_written_in_a_filter_joins_the_select_of_the_plain_sum(world):
    """COUNT(orders.id) needs no value from the one side: it is computed with total_revenue."""
    _, ex, con = world
    q = Query(("west_revenue", "total_revenue"), ("channels.name",), ("COUNT(orders.id) > 2",))
    got = ex.execute(q)
    dims = {"name": "h.name"}
    count = dict(layer2(con, "o", "o.id", dims, ORDERS_JOIN, fn="COUNT"))
    west = dict(layer2(con, "o", "o.amount", dims, ORDERS_JOIN, "c.region = 'WEST'"))
    whole = dict(layer2(con, "o", "o.amount", dims, ORDERS_JOIN))
    kept = {n for n, c in count.items() if c > 2}
    assert kept == {"web"}
    assert {r[0]: (number(r[1]), number(r[2])) for r in got.rows} == {
        n: (number(west.get(n)), number(whole[n])) for n in kept
    }


def test_an_aggregate_written_in_a_filter_joins_the_select_with_its_values(world):
    """SUM(orders.amount * customers.weight) needs what weighted_by_customer does."""
    _, ex, con = world
    argument = "o.amount * c.weight"
    q = Query(
        ("weighted_by_customer", "total_revenue"),
        ("channels.name",),
        ("SUM(orders.amount * customers.weight) > 100",),
    )
    got = ex.execute(q)
    dims = {"name": "h.name"}
    weighted = dict(layer2(con, "o", argument, dims, ORDERS_JOIN))
    whole = dict(layer2(con, "o", "o.amount", dims, ORDERS_JOIN))
    kept = {n for n, v in weighted.items() if v is not None and v > 100}
    assert {r[0]: (number(r[1]), number(r[2])) for r in got.rows} == {
        n: (number(weighted[n]), number(whole[n])) for n in kept
    }
    assert " UNION ALL " in got.sql


def test_a_window_beside_metrics_answered_apart_says_why(world):
    _, ex, _ = world
    with pytest.raises(PlanError) as e:
        ex.execute(Query(("running", "west_revenue"), ("customers.region",)))
    message = str(e.value)
    assert (
        "metric 'running' is a window over the merged rows of metrics that need other values"
        in message
    )
    assert "different datasets" not in message


def test_a_filter_of_the_question_is_shared(world):
    """A whole condition is carried for every aggregate, so it separates no copies."""
    kind, ex, con = world
    q = Query(("west_revenue", "total_revenue"), (), ("customers.weight <> 0",))
    got = ex.execute(q)
    assert [number(v) for v in got.rows[0]] == [
        total(con, where="c.region = 'WEST' AND c.weight <> 0"),
        total(con, where="c.weight <> 0"),
    ]
    assert got.sql.count("SELECT DISTINCT") == 2  # one derived table for each SELECT


def test_a_conjunct_over_the_root_and_the_one_side_is_the_documented_limit(world):
    """docs/design.md, "A key stored twice on the one side": the part of the condition on
    customers is carried, and O2 passes it for both copies of C1 (weights 1 and -1)."""
    kind, ex, con = world
    q = Query(
        ("total_revenue",), ("customers.region",), ("customers.weight > 0 OR orders.amount > 150",)
    )
    got = rows_by_region(ex.execute(q))
    layer = by_region(con, where="c.weight > 0 OR o.amount > 150")
    if kind == "unique":
        assert got == {r: (number(v),) for r, v in layer.items()}
    else:
        assert layer["EAST"] == 350 and got["EAST"] == (550,)  # Layer 2 counts O2 once

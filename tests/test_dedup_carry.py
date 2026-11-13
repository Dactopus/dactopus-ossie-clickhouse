"""Aggregates that need other values from a key stored twice (docs/decisions/key-stored-twice.md).

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
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from tests.test_dedup import DATA, DUCK, MODEL, ORDERS_JOIN, layer2, metric

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


BLENDS = {  # metrics that read orders and customers in one expression
    "west_or_large": "SUM(orders.amount) FILTER (WHERE customers.region = 'WEST' "
    "OR orders.amount > 250)",
    "west_and_large": "SUM(orders.amount) FILTER (WHERE customers.region = 'WEST' "
    "AND orders.amount > 250)",
    "greatest_weight": "SUM(GREATEST(orders.amount, customers.weight))",
    "weighted_count": "COUNT(orders.amount * customers.weight)",
    "two_parts": "SUM(orders.amount * customers.weight + customers.weight)",
    "divided_twice": "SUM(orders.amount / customers.weight * customers.weight)",
    "west_else_one": "COUNT(CASE WHEN customers.region = 'WEST' THEN orders.id ELSE 0 END)",
    "west_case": "SUM(CASE WHEN customers.region = 'WEST' THEN orders.amount END)",
    "west_if": "SUM(IF(customers.region = 'WEST', orders.amount, NULL))",
    "west_else_zero": "SUM(CASE WHEN customers.region = 'WEST' THEN orders.amount ELSE 0 END)",
    "west_case_count": "COUNT(CASE WHEN customers.region = 'WEST' AND orders.amount > 1 "
    "THEN orders.id END)",
    "largest_blend": "MAX(GREATEST(orders.amount, customers.weight))",
    "shifted_average": "AVG(orders.amount + customers.weight)",
    "squared_weight": "SUM(orders.amount * customers.weight * customers.weight)",
    "discounted": "SUM(orders.amount * customers.weight * (1 - customers.weight))",
    "net_of_weights": "AVG(customers.weight - orders.amount - customers.weight * 2)",
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
    model["metrics"] += [metric(name, e) for name, e in BLENDS.items()]
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


# --- expressions that read the root and the one side together --------------------------
# The part on customers is carried as a value of the derived table, so two copies of a key
# that differ in it are two rows. Where the expression around the part gives both the same
# result, a root row would count twice, so the question is refused, even on data that
# stores each key once: the planner never reads the data.

REFUSED = [
    (
        Query(
            ("total_revenue",),
            ("customers.region",),
            ("customers.weight > 0 OR orders.amount > 150",),
        ),
        "filter 'customers.weight > 0 OR orders.amount > 150' reads customers and another "
        "dataset in one condition (customers.weight > 0) OR (orders.amount > 150)",
        "Join the condition on customers to the rest with AND",
    ),
    (
        Query(("west_or_large",)),
        "metric 'west_or_large' reads customers and another dataset in one condition "
        "(customers.region = 'WEST') OR (orders.amount > 250)",
        "copy that passes it",
    ),
    (
        Query(("greatest_weight",)),
        "metric 'greatest_weight' reads customers and another dataset in the argument of "
        "SUM(GREATEST(orders.amount, customers.weight))",
        "copy that gives the same argument",
    ),
    (
        Query(("weighted_count",)),
        "the argument of COUNT(orders.amount * customers.weight)",
        "(or * and / in SUM)",
    ),
    (
        Query(("total_revenue",), (), ("SUM(GREATEST(orders.amount, customers.weight)) > 0",)),
        "filter 'SUM(GREATEST(orders.amount, customers.weight)) > 0' reads customers",
        "put a condition on customers in a FILTER",
    ),
    (
        Query(("two_parts",)),
        "the argument of SUM((orders.amount * customers.weight) + customers.weight)",
        "",
    ),
    (Query(("west_else_one",)), "the argument of COUNT(CASE WHEN", ""),
    (Query(("divided_twice",)), "the argument of SUM", ""),
]


@pytest.fixture(scope="module")
def blended(tmp_path_factory):
    """The model with BLENDS, as written: the executor hides the metrics refused in every
    question, so the planner sees them only here."""
    model = copy.deepcopy(MODEL)
    model["metrics"] += [metric(name, e) for name, e in BLENDS.items()]
    path = tmp_path_factory.mktemp("blended") / "m.yaml"
    path.write_text(yaml.safe_dump(model, sort_keys=False))
    return load_model(path)


@pytest.mark.parametrize(("q", "says", "advice"), REFUSED)
def test_an_expression_that_could_count_a_copy_twice_is_refused(blended, q, says, advice):
    """With the data of tests/test_dedup.py, O2 passes customers.weight > 0 OR
    orders.amount > 150 for both copies of C1 (weights 1 and -1), and GREATEST(200, 1)
    and GREATEST(200, -1) are both 200: Layer 2 counts O2 once, EAST 350, where carrying
    the part on customers gives 550."""
    with pytest.raises(PlanError) as e:
        Planner(blended).plan(q)
    message = str(e.value)
    assert says in message and advice in message
    assert "ClickHouse does not enforce the key of customers" in message


def test_the_refused_metrics_are_reported_and_hidden(world, blended):
    _, ex, _ = world
    found = unanswerable_metrics(blended)
    refused = {
        "west_or_large",
        "greatest_weight",
        "weighted_count",
        "two_parts",
        "west_else_one",
        "divided_twice",
    }
    assert refused <= set(found)
    assert "copy that passes it" in found["west_or_large"]
    answered = set(BLENDS) - refused
    assert answered.isdisjoint(found)
    with pytest.raises(PlanError, match="unknown metric 'greatest_weight'"):
        ex.execute(Query(("greatest_weight",)))
    with pytest.raises(PlanError, match="in one condition"):
        ex.execute(REFUSED[0][0])


@pytest.mark.parametrize(
    ("name", "argument"),
    [
        ("west_and_large", "CASE WHEN c.region = 'WEST' AND o.amount > 250 THEN o.amount END"),
        ("west_case", "CASE WHEN c.region = 'WEST' THEN o.amount END"),
        ("west_if", "CASE WHEN c.region = 'WEST' THEN o.amount END"),
        ("west_else_zero", "CASE WHEN c.region = 'WEST' THEN o.amount ELSE 0 END"),
        ("west_case_count", "CASE WHEN c.region = 'WEST' AND o.amount > 1 THEN o.id END"),
        ("shifted_average", "o.amount + c.weight"),
        ("weighted_by_customer", "o.amount * c.weight"),
        # Two parts on customers, gathered into one value (_regroup): C1's copies give
        # 1 * 1 and -1 * -1, the same product, and collapse as in Layer 2.
        ("squared_weight", "o.amount * c.weight * c.weight"),
        ("discounted", "o.amount * c.weight * (1 - c.weight)"),
        ("net_of_weights", "c.weight - o.amount - c.weight * 2"),
    ],
)
def test_an_expression_that_tells_the_copies_apart_is_answered(world, name, argument):
    """A conjunct, a CASE that is a FILTER, and an argument that gives each value of the
    part its own result answer as Layer 2 does, by region and in total."""
    kind, ex, con = world
    fn = {"west_case_count": "COUNT", "shifted_average": "AVG", "net_of_weights": "AVG"}.get(
        name, "SUM"
    )
    gold = dict(layer2(con, "o", argument, {"region": "c.region"}, ORDERS_JOIN, fn=fn))
    got = rows_by_region(ex.execute(Query((name,), ("customers.region",))))
    assert got.keys() == gold.keys()
    for region, value in gold.items():
        assert got[region][0] == pytest.approx(number(value))
    whole = number(layer2(con, "o", argument, {}, ORDERS_JOIN, fn=fn)[0][0])
    assert number(ex.execute(Query((name,))).rows[0][0]) == pytest.approx(whole)


def test_a_filter_joined_with_and_is_answered(world):
    kind, ex, con = world
    q = Query(
        ("total_revenue",), ("customers.region",), ("customers.weight > 0 AND orders.amount > 150",)
    )
    got = rows_by_region(ex.execute(q))
    assert got == {
        r: (number(v),) for r, v in by_region(con, where="c.weight > 0 AND o.amount > 150").items()
    }


def test_an_aggregate_that_repeats_do_not_change_reads_both_sides_as_before(world):
    """MAX takes no derived table, so nothing is carried and nothing refused; beside a SUM
    it does, and a copy still changes nothing MAX returns."""
    _, ex, con = world
    q = Query(
        ("max_order",), ("customers.region",), ("customers.weight > 0 OR orders.amount > 150",)
    )
    got = rows_by_region(ex.execute(q))
    gold = layer2(
        con,
        "o",
        "o.amount",
        {"region": "c.region"},
        ORDERS_JOIN,
        "c.weight > 0 OR o.amount > 150",
        "MAX",
    )
    assert got == {r: (number(v),) for r, v in gold}
    got = ex.execute(Query(("largest_blend", "total_revenue"), ("customers.region",)))
    assert "SELECT DISTINCT" in got.sql
    largest = dict(
        layer2(con, "o", "GREATEST(o.amount, c.weight)", {"r": "c.region"}, ORDERS_JOIN, fn="MAX")
    )
    sums = by_region(con)
    assert rows_by_region(got) == {r: (number(largest[r]), number(v)) for r, v in sums.items()}

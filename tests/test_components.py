"""A model's metric whose aggregates read different datasets: each aggregate is a
component, evaluated over the rows of its own dataset (Layer 3, apache/ossie#653,
sections 5.2 to 5.5).

Values against ClickHouse are compared with DuckDB running the plan of section
5.5 written out as SQL (``reference``): a branch for each home dataset, the keys
as the UNION of the branches' keys, NULL-safe equality, a missing branch row giving
the component's empty-input value, and the metric's expression over the values.
Where a component is 0 the expression divides by zero: DuckDB and ClickHouse
differ on that, so the reference writes the value by hand as NULL, which is what
the executor shows (docs/design.md, "Division to `Float64`").

``B1`` is the data of the spec's Appendix B.1. ``EXT`` adds an order and a return
without a customer (a NULL group in two branches and in none of the third) and
leaves NORTH without orders, WEST without returns."""

from pathlib import Path

import pytest
import yaml

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from tests.test_model import FIXTURE as TPCDS
from tests.test_planner import LISTED, _with_metrics
from tests.test_stitch import COLUMNS as STITCH_COLUMNS
from tests.test_stitch import FIXTURE as STITCH

COLUMNS = {
    **STITCH_COLUMNS,
    "customers": (
        "id Int32, region Nullable(String), segment String",
        STITCH_COLUMNS["customers"][1],
    ),
}

# Appendix B.1: customers C1..C4 as 1..4, orders O1..O4, returns R1 and R2.
CUSTOMERS = (
    "(1, 'EAST', 'retail'), (2, 'EAST', 'retail'), (3, 'WEST', 'retail'), (4, 'NORTH', 'retail')"
)
ORDERS = (
    "(1, 1, 100.00, 'done'), (2, 1, 200.00, 'done'), (3, 2, 50.00, 'done'), (4, 3, 300.00, 'done')"
)
RETURNS = "(1, 1, 25.00), (2, 4, 10.00)"
B1 = {"customers": CUSTOMERS, "orders": ORDERS, "returns": RETURNS}
EXT = {
    "customers": CUSTOMERS,
    "orders": ORDERS + ", (5, 99, 30.00, 'done')",  # customer 99 does not exist
    "returns": RETURNS + ", (3, 98, 7.00)",  # nor does 98
}


# B1 and a customer whose region is NULL, with an order and a return: the NULL group
# comes from the dimension's own dataset, and every branch has it.
NUL = {
    "customers": CUSTOMERS + ", (5, NULL, 'retail')",
    "orders": ORDERS + ", (5, 5, 40.00, 'done')",
    "returns": RETURNS + ", (3, 5, 7.00)",
}


def expression(sql: str) -> dict:
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": sql}]}


def model_data(database: str) -> dict:
    data = yaml.safe_load(STITCH.read_text().replace("ossie_stitch.", f"{database}."))
    data["metrics"] += [
        {
            "name": "revenue_per_customer",
            "expression": expression("SUM(orders.amount) / COUNT(DISTINCT customers.id)"),
        },
        {
            "name": "returns_per_order",
            "expression": expression("SUM(returns.amount) / COUNT(orders.id)"),
        },
        {
            "name": "per_return_and_one",
            "expression": expression("SUM(orders.amount) / (COUNT(returns.id) + 1)"),
        },
    ]
    return data


def load_data(tmp_path: Path, data: dict):
    p = tmp_path / f"{data['name']}.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_model(p)


def load(tmp_path: Path, database: str):
    return load_data(tmp_path, model_data(database))


@pytest.fixture(scope="module")
def models(clickhouse, tmp_path_factory):
    """Executors over B1 and EXT, each in a database of its own."""
    tmp = tmp_path_factory.mktemp("components")
    out = {}
    for name, rows in (("b1", B1), ("ext", EXT), ("nul", NUL)):
        database = f"ossie_components_{name}"
        clickhouse.command(f"CREATE DATABASE IF NOT EXISTS {database}")
        for table, (columns, _) in COLUMNS.items():
            clickhouse.command(f"DROP TABLE IF EXISTS {database}.{table}")
            clickhouse.command(
                f"CREATE TABLE {database}.{table} ({columns}) ENGINE = MergeTree ORDER BY id"
            )
            clickhouse.command(f"INSERT INTO {database}.{table} VALUES {rows[table]}")
        out[name] = Executor(clickhouse, load(tmp, database))
    yield out
    for name in out:
        clickhouse.command(f"DROP DATABASE ossie_components_{name}")


@pytest.fixture(scope="module")
def duck_for():
    import duckdb

    def make(rows: dict[str, str]):
        con = duckdb.connect()
        for table, (_, columns) in COLUMNS.items():
            con.execute(f"CREATE TABLE {table} ({columns})")
            con.execute(f"INSERT INTO {table} VALUES {rows[table]}")
        return con

    return make


def reference(branches: dict[str, tuple[str, dict[str, str]]], select: str) -> str:
    """Section 5.5 as SQL, by region. ``branches``: for each home dataset, its branch (a
    query of ``region`` and its components) and the empty-input value of each component,
    as a column name -> SQL. ``select`` is the metrics' expressions over the component
    columns."""
    ctes = ", ".join(
        f"b_{n} AS (SELECT *, 1 AS present FROM ({q}))" for n, (q, _) in branches.items()
    )
    keys = " UNION ".join(f"SELECT region FROM b_{n}" for n in branches)
    joins = " ".join(
        f"LEFT JOIN b_{n} ON b_{n}.region IS NOT DISTINCT FROM k.region" for n in branches
    )
    cols = ", ".join(
        f"CASE WHEN b_{n}.present IS NULL THEN {empty} ELSE b_{n}.{c} END AS {c}"
        for n, (_, comps) in branches.items()
        for c, empty in comps.items()
    )
    return (
        f"WITH {ctes}, keys AS ({keys}), c AS (SELECT k.region, {cols} FROM keys k {joins}) "
        f"SELECT region, {select} FROM c"
    )


# Branches of the spec's examples: one query for each home dataset, by region.
ORDERS_BRANCH = (
    "SELECT c.region, SUM(o.amount) AS orders_sum, COUNT(o.id) AS orders_count "
    "FROM orders o LEFT JOIN customers c ON o.customer_id = c.id GROUP BY c.region",
    {"orders_sum": "NULL", "orders_count": "0"},
)
CUSTOMERS_BRANCH = (
    "SELECT c.region, COUNT(DISTINCT c.id) AS customers_count FROM customers c GROUP BY c.region",
    {"customers_count": "0"},
)
RETURNS_BRANCH = (
    "SELECT c.region, SUM(x.amount) AS returns_sum FROM returns x "
    "LEFT JOIN customers c ON x.customer_id = c.id GROUP BY c.region",
    {"returns_sum": "NULL"},
)
# A metric's expression over components; a zero divisor is NULL, written by hand.
PER_CUSTOMER = "CASE WHEN customers_count = 0 THEN NULL ELSE orders_sum * 1.0 / customers_count END"
PER_ORDER = "CASE WHEN orders_count = 0 THEN NULL ELSE returns_sum * 1.0 / orders_count END"


def _key(row: tuple) -> tuple:
    return tuple((v is None, str(v)) for v in row[:1])


def same(got: list[tuple], gold: list[tuple]) -> None:
    got, gold = sorted(got, key=_key), sorted(gold, key=_key)
    assert len(got) == len(gold), (got, gold)
    for g, e in zip(got, gold, strict=True):
        assert g[0] == e[0]
        for a, b in zip(g[1:], e[1:], strict=True):
            assert (a is None and b is None) or a == pytest.approx(float(b), rel=1e-9), (g, e)


# --- the spec's example, Appendix B.1 ------------------------------------------


def test_the_spec_example_in_total(models, duck_for):
    """Section 5.2: 650 / 4 = 162.5, where one query over a join gives 650 / 3."""
    got = models["b1"].execute(Query(("revenue_per_customer",)))
    assert got.rows == [(162.5,)]
    assert " UNION ALL " in got.sql
    gold = (  # the section's own illustration: a branch for each home, a CROSS JOIN
        "SELECT b1.c1 / b2.c2 FROM (SELECT SUM(amount) AS c1 FROM orders) b1 "
        "CROSS JOIN (SELECT COUNT(DISTINCT id) * 1.0 AS c2 FROM customers) b2"
    )
    assert duck_for(B1).sql(gold).fetchall() == [(162.5,)]


def test_the_spec_example_by_region(models, duck_for):
    """EAST 350 / 2, WEST 300 / 1, and NORTH, a customer without an order: the sum over
    no orders is NULL (section 5.5), so NULL / 1 is NULL."""
    got = models["b1"].execute(Query(("revenue_per_customer",), ("customers.region",))).rows
    assert sorted(got, key=_key) == [("EAST", 175.0), ("NORTH", None), ("WEST", 300.0)]
    gold = reference({"orders": ORDERS_BRANCH, "customers": CUSTOMERS_BRANCH}, PER_CUSTOMER)
    same(got, duck_for(B1).sql(gold).fetchall())


def test_one_query_over_a_join_gives_650_over_3(duck_for):
    """The data tells the plans apart: a LEFT JOIN from orders counts three customers."""
    wrong = (
        duck_for(B1)
        .sql(
            "SELECT SUM(o.amount) * 1.0 / COUNT(DISTINCT c.id) FROM orders o "
            "LEFT JOIN customers c ON o.customer_id = c.id"
        )
        .fetchall()
    )
    assert wrong[0][0] == pytest.approx(650 / 3)


# --- a dimension value in one branch only, a NULL group, an empty-input COUNT ----


def test_null_group_and_empty_input_count(models, duck_for):
    """The NULL group is an order without a customer: the customers branch has no
    NULL key, so its COUNT takes 0, and 30 / 0 is NULL in the executor. NORTH has no
    orders, WEST no returns."""
    ex = models["ext"]
    q = Query(("revenue_per_customer", "returns_per_order"), ("customers.region",))
    got = ex.execute(q).rows
    rows = {r[0]: r[1:] for r in got}
    assert rows == {
        "EAST": (175.0, pytest.approx(25 / 3)),  # 350 / 2 customers; 25 returned of 3 orders
        "WEST": (300.0, None),  # no returns: the sum over no rows is NULL
        "NORTH": (None, None),  # no orders (NULL sum), and 10 / 0 orders
        None: (None, 7.0),  # 30 / 0 customers; 7 returned of 1 order
    }
    gold = reference(
        {"orders": ORDERS_BRANCH, "customers": CUSTOMERS_BRANCH, "returns": RETURNS_BRANCH},
        f"{PER_CUSTOMER} AS per_customer, {PER_ORDER} AS per_order",
    )
    same(got, duck_for(EXT).sql(gold).fetchall())
    assert ex.execute(q).sql.count(" UNION ALL ") == 2  # three branches, one merge


def test_a_component_beside_a_metric_of_its_dataset_shares_the_branch(models, duck_for):
    """Section 5.4 groups components by home across metrics: total_revenue is the
    orders branch's sum again, and customers get their own."""
    ex = models["ext"]
    q = Query(("revenue_per_customer", "total_revenue"), ("customers.region",))
    got = ex.execute(q)
    assert got.sql.count(" UNION ALL ") == 1
    gold = reference(
        {"orders": ORDERS_BRANCH, "customers": CUSTOMERS_BRANCH},
        f"{PER_CUSTOMER} AS per_customer, orders_sum AS total_revenue",
    )
    same(got.rows, duck_for(EXT).sql(gold).fetchall())


def test_return_rate_reads_two_facts_and_is_answered(models, duck_for):
    """SUM(returns.amount) / SUM(orders.amount): each side over its own rows."""
    ex = models["ext"]
    assert "return_rate" not in unanswerable_metrics(ex.model)
    got = ex.execute(Query(("return_rate",), ("customers.region",))).rows
    gold = reference(
        {"orders": ORDERS_BRANCH, "returns": RETURNS_BRANCH},
        "returns_sum * 1.0 / orders_sum AS return_rate",
    )
    same(got, duck_for(EXT).sql(gold).fetchall())
    total = ex.execute(Query(("return_rate",))).rows
    assert total[0][0] == pytest.approx(42 / 680)


def test_a_server_aggregate_takes_its_empty_input_value_from_the_server(models, tmp_path):
    """countIf is 0 over no rows and sumIf NULL, and the server's function list does
    not say which: the component is planned over WHERE FALSE and ClickHouse answers."""
    data = model_data("ossie_components_ext")
    data["metrics"].append(
        {
            "name": "per_late_customer",
            "expression": expression("SUM(orders.amount) / countIf(customers.id > 1)"),
        }
    )
    ex = Executor(models["ext"].client, load_data(tmp_path, data))
    got = {
        r[0]: r[1] for r in ex.execute(Query(("per_late_customer",), ("customers.region",))).rows
    }
    # EAST: customer 2 only; WEST: 3; NORTH: 4 but no orders; NULL: 30 over no customers (0).
    assert got == {"EAST": 350.0, "WEST": 300.0, "NORTH": None, None: None}


def test_no_dimension_and_a_filter_that_matches_nothing(models):
    """A filter applies to every branch; over no rows a sum is NULL and a count 0, so
    NULL / 0 is NULL, not an error."""
    q = Query(("revenue_per_customer",), (), ("customers.region = 'SOUTH'",))
    assert models["ext"].execute(q).rows == [(None,)]


def test_a_null_dimension_value_of_its_own_dataset_is_not_a_missing_branch_row(models, duck_for):
    """Customer 5 has no region. Every branch has a NULL group but orders and returns
    per customer: the returns branch has it too (a return of customer 5), so its COUNT
    is 1 there, where a missing row would give 0 (section 5.5: a NULL group is not a
    missing branch row)."""
    ex = models["nul"]
    q = Query(("per_return_and_one", "revenue_per_customer"), ("customers.region",))
    got = ex.execute(q).rows
    rows = {r[0]: r[1:] for r in got}
    assert rows == {
        "EAST": (175.0, 175.0),  # 350 / (1 return + 1); 350 / 2 customers
        "WEST": (300.0, 300.0),  # no returns row: COUNT 0, 300 / 1
        "NORTH": (None, None),  # a return and a customer, no orders
        None: (20.0, 40.0),  # 40 / (1 return + 1); 40 / 1 customer
    }
    returns_count = (
        "SELECT c.region, COUNT(x.id) AS returns_count FROM returns x "
        "LEFT JOIN customers c ON x.customer_id = c.id GROUP BY c.region",
        {"returns_count": "0"},
    )
    gold = reference(
        {"orders": ORDERS_BRANCH, "returns": returns_count, "customers": CUSTOMERS_BRANCH},
        "orders_sum * 1.0 / (returns_count + 1) AS per_return_and_one, "
        f"{PER_CUSTOMER} AS per_customer",
    )
    same(got, duck_for(NUL).sql(gold).fetchall())


# --- a filter on the many side is a semi-join in the branch ----------------------


def test_a_filter_on_the_many_side_counts_customers_with_a_matching_order(models, duck_for):
    """Orders over 100 sum to 500 and belong to C1 and C3: 500 / 2. #653 refuses the
    filter (E_UNSUPPORTED_FILTER_PATH); here it is a semi-join, as for any metric of
    customers (docs/design.md)."""
    ex = models["ext"]
    got = ex.execute(Query(("revenue_per_customer",), (), ("orders.amount > 100",)))
    assert got.rows == [(250.0,)]
    assert " IN (SELECT orders.customer_id" in got.sql
    by_region = ex.execute(
        Query(("revenue_per_customer",), ("customers.region",), ("orders.amount > 100",))
    ).rows
    assert sorted(by_region, key=_key) == [("EAST", 200.0), ("WEST", 300.0)]  # no NORTH key
    gold = reference(
        {
            "orders": (
                "SELECT c.region, SUM(o.amount) AS orders_sum, COUNT(o.id) AS orders_count "
                "FROM orders o LEFT JOIN customers c ON o.customer_id = c.id "
                "WHERE o.amount > 100 GROUP BY c.region",
                {"orders_sum": "NULL", "orders_count": "0"},
            ),
            "customers": (
                "SELECT c.region, COUNT(DISTINCT c.id) AS customers_count FROM customers c "
                "WHERE c.id IN (SELECT customer_id FROM orders WHERE amount > 100) "
                "GROUP BY c.region",
                {"customers_count": "0"},
            ),
        },
        PER_CUSTOMER,
    )
    same(by_region, duck_for(EXT).sql(gold).fetchall())


def test_a_row_filter_on_the_dimension_dataset_applies_to_each_branch(models):
    """customers.region = 'EAST': orders of EAST customers over EAST customers."""
    ex = models["ext"]
    got = ex.execute(Query(("revenue_per_customer",), (), ("customers.region = 'EAST'",)))
    assert got.rows == [(175.0,)]


# --- reachability (section 5.3) ----------------------------------------------------


def refusal(planner: Planner, q: Query) -> str:
    with pytest.raises(PlanError) as e:
        planner.sql(q)
    return str(e.value)


def test_a_dimension_a_component_cannot_reach_is_refused_naming_both(tmp_path):
    planner = Planner(load(tmp_path, "ossie_components_x"))
    message = refusal(planner, Query(("revenue_per_customer",), ("orders.status",)))
    assert message.startswith("dimension 'orders.status' cannot be with metric")
    assert "'revenue_per_customer'" in message and "reads customers rows" in message
    assert "customers does not reach orders" in message
    # what would answer it: a metric of the dataset that reaches the dimension
    assert "define a metric of dataset orders" in message
    assert "such as COUNT(DISTINCT customer_id)" in message


def test_a_filter_no_component_reaches_is_refused_naming_both(tmp_path):
    """returns meets orders only at customers: customers does not reach returns, and
    no returns row is what a customer's orders are about."""
    planner = Planner(load(tmp_path, "ossie_components_x"))
    message = refusal(planner, Query(("revenue_per_customer",), (), ("returns.amount > 5",)))
    assert "'revenue_per_customer'" in message and "returns" in message


TPCDS_MODEL = load_model(TPCDS)


@pytest.mark.parametrize(
    ("dimension", "dataset"), [("store.s_state", "store"), ("date_dim.d_year", "date_dim")]
)
def test_customer_lifetime_value_by_a_dimension_customer_does_not_reach(dimension, dataset):
    """Answered before, over the buyers only; customer reaches neither store nor date_dim."""
    message = refusal(Planner(TPCDS_MODEL), Query(("customer_lifetime_value",), (dimension,)))
    assert f"dimension '{dimension}' cannot be with metric 'customer_lifetime_value'" in message
    assert f"customer does not reach {dataset}" in message
    assert (
        "define a metric of dataset store_sales, such as COUNT(DISTINCT ss_customer_sk)" in message
    )


def test_customer_lifetime_value_filtered_on_a_dataset_customer_does_not_reach():
    message = refusal(
        Planner(TPCDS_MODEL),
        Query(("customer_lifetime_value",), (), ("date_dim.d_year = 2000",)),
    )
    assert "row filter 'date_dim.d_year = 2000' cannot be with metric" in message
    assert "'customer_lifetime_value'" in message and "date_dim" in message


def test_a_dimension_one_component_reaches_and_another_does_not_names_the_second():
    # store_productivity: both reach store; the customer count does not reach item.
    planner = Planner(TPCDS_MODEL)
    assert "UNION ALL" in planner.sql(Query(("store_productivity",), ("store.s_state",)))
    message = refusal(planner, Query(("store_productivity",), ("item.i_brand",)))
    assert "dimension 'item.i_brand' cannot be with metric 'store_productivity'" in message
    assert "reads store rows" in message


def test_no_hint_when_the_relationship_to_the_counted_dataset_is_hidden():
    """With ss_customer_sk hidden there is no relationship from store_sales to customer,
    and no metric of store_sales counts customers."""
    from dactopus_ossie_clickhouse.access import Hidden, restrict

    model = restrict(TPCDS_MODEL, Hidden(fields=frozenset({"STORE_SALES.SS_CUSTOMER_SK"})))
    message = refusal(Planner(model), Query(("customer_lifetime_value",), ("date_dim.d_year",)))
    assert "customer does not reach date_dim" in message
    assert "define a metric" not in message and "ss_customer_sk" not in message


def test_no_hint_when_no_dataset_metric_can_read_the_counted_rows():
    """store_productivity sums store rows; a metric of store_sales cannot read them."""
    message = refusal(Planner(TPCDS_MODEL), Query(("store_productivity",), ("item.i_brand",)))
    assert "store does not reach item" in message
    assert "define a metric" not in message


def test_a_filter_on_a_metric_beside_dimensions_it_cannot_reach_is_refused():
    planner = Planner(TPCDS_MODEL)
    message = refusal(
        planner,
        Query(("total_sales",), ("item.i_brand",), ("customer_lifetime_value > 1",)),
    )
    assert "dimension 'item.i_brand' cannot be with metric 'customer_lifetime_value'" in message
    # by a field customer reaches, the filter is applied to the merged rows
    sql = planner.sql(
        Query(("total_sales",), ("customer.c_first_name",), ("customer_lifetime_value > 1",))
    )
    assert " UNION ALL " in sql
    assert (
        "HAVING (any(__m2) * 1.0) / CASE WHEN COUNT(__b1) > 0 THEN any(__m1) ELSE 0 END > 1" in sql
    )


def test_a_written_single_dataset_aggregate_filter_beside_a_split_metric_is_refused():
    message = refusal(
        Planner(TPCDS_MODEL),
        Query(("customer_lifetime_value",), (), ("SUM(store_sales.ss_quantity) > 5",)),
    )
    assert "applies an aggregate function to metrics answered from different datasets" in message


# --- mixed metrics and the home of a FILTER (apache/ossie#653, section 5.2) ----------

CLV_PARTS = "SUM(store_sales.ss_ext_sales_price)"
MIXED = {
    # an aggregate over two datasets, and one over a dataset it does not read
    "joined_per_customer": f"{LISTED} / COUNT(DISTINCT customer.c_customer_sk)",
    "joined_per_staff": f"{LISTED} / MAX(store.s_number_employees)",
    # a FILTER on another dataset does not make that dataset the home: the count is
    # over customer rows, and one join would count the buyers only
    "retail_per_customer": f"{CLV_PARTS} FILTER (WHERE customer.c_first_name = 'James') / "
    "COUNT(DISTINCT customer.c_customer_sk)",
    # an aggregate that reads no dataset has no home either
    "per_filtered_count": f"{CLV_PARTS} / COUNT(*) FILTER (WHERE customer.c_first_name = 'James')",
    "filtered_per_customer": "COUNT(*) FILTER (WHERE store_sales.ss_quantity > 1) / "
    "COUNT(DISTINCT customer.c_customer_sk)",
}
# Aggregates over one root that need other values from the one side than their neighbours do:
# one SELECT each, since a value carried for one keeps apart copies of a key for the others
# (docs/design.md, "A key stored twice on the one side").
APART = {
    "joined_per_sale": f"{LISTED} / SUM(store_sales.ss_quantity)",
    "filtered_per_sale": f"{CLV_PARTS} FILTER (WHERE item.i_brand = 'x') / "
    "SUM(store_sales.ss_quantity)",
}
ONE_ROOT = {
    "joined_per_item": f"{LISTED} / MAX(item.i_current_price)",
    "count_beside_sum": f"{CLV_PARTS} / COUNT(*)",
    "count_filtered_beside_sum": f"{CLV_PARTS} / "
    "COUNT(*) FILTER (WHERE store_sales.ss_quantity > 1)",
    "count_filtered_alone": "COUNT(*) FILTER (WHERE store_sales.ss_quantity > 1)",
}


@pytest.mark.parametrize("name", MIXED)
def test_a_mixed_metric_is_refused_in_every_question_and_reported(name):
    planner = _with_metrics(**{name: MIXED[name]})
    assert name in unanswerable_metrics(planner.model)
    for dimensions in ((), ("item.i_brand",)):
        message = refusal(planner, Query((name,), dimensions))
        assert f"metric '{name}'" in message
    assert ("reads a dataset the first does not" in unanswerable_metrics(planner.model)[name]) == (
        name not in ("per_filtered_count", "filtered_per_customer")
    )


@pytest.mark.parametrize("name", APART)
def test_aggregates_that_need_other_values_of_the_one_side_are_answered_apart(name):
    planner = _with_metrics(**{name: APART[name]})
    assert name not in unanswerable_metrics(planner.model)
    sql = planner.sql(Query((name,), ("item.i_brand",)))
    assert sql.count(" UNION ALL ") == 1 and sql.count("FROM tpcds.store_sales ") == 2
    # a window over them cannot be split, and is refused
    window = _with_metrics(w=f"RANK() OVER (ORDER BY {APART[name]})")
    with pytest.raises(PlanError, match="must be answered apart"):
        window.sql(Query(("w",), ("item.i_brand",)))


@pytest.mark.parametrize("name", ONE_ROOT)
def test_a_metric_whose_aggregates_stay_within_one_root_is_planned_as_before(name):
    planner = _with_metrics(**{name: ONE_ROOT[name]})
    assert name not in unanswerable_metrics(planner.model)
    sql = planner.sql(Query((name,), ("item.i_brand",)))
    assert " UNION ALL " not in sql and sql.count("FROM tpcds.store_sales ") == 1


def test_a_written_filter_that_would_split_or_mix_is_refused_whatever_else_it_holds():
    planner = _with_metrics()
    parts = "SUM(store_sales.ss_ext_sales_price) / COUNT(DISTINCT customer.c_customer_sk) > 5"
    mixed = f"{LISTED} / COUNT(DISTINCT customer.c_customer_sk) > 5"
    for condition in (parts, f"{parts} AND COUNT(*) > 0", mixed, f"{mixed} AND COUNT(*) > 0"):
        message = refusal(planner, Query(("total_sales",), ("item.i_brand",), (condition,)))
        assert f"filter {condition!r}" in message
    # one dataset, with an aggregate that names none, is answered as before
    sql = planner.sql(
        Query(
            ("total_sales",),
            ("item.i_brand",),
            ("SUM(store_sales.ss_quantity) > 5 AND COUNT(*) > 0",),
        )
    )
    assert "HAVING" in sql and "UNION ALL" not in sql


def test_a_named_metric_beside_a_written_aggregate_of_another_dataset_is_refused():
    """The filter reads store_sales through total_sales, so as a whole it reads the
    root; one join would count the stores with a sale."""
    condition = "total_sales > COUNT(DISTINCT store.s_store_sk)"
    message = refusal(_with_metrics(), Query(("total_sales",), ("store.s_state",), (condition,)))
    assert f"filter {condition!r} applies aggregates of store and store_sales rows" in message


def test_an_aggregate_over_a_metric_that_reads_no_dataset_names_none():
    """A bare metric name in a written aggregate reads no dataset, like COUNT(*)."""
    planner = _with_metrics(now_m="CURRENT_TIMESTAMP")
    condition = "MAX(now_m) > TIMESTAMP '2020-01-01 00:00:00'"
    sql = planner.sql(Query(("total_sales",), ("item.i_brand",), (condition,)))
    assert "HAVING" in sql and "UNION ALL" not in sql


def test_metrics_of_different_datasets_named_in_one_filter_are_each_answered_on_their_own():
    planner = _with_metrics(store_count="COUNT(DISTINCT store.s_store_sk)")
    q = Query(("total_sales",), ("store.s_state",), ("total_sales > store_count",))
    sql = planner.sql(q)
    # the stores are counted over store rows, not over the sales that reference them
    assert " UNION ALL " in sql
    assert "COUNT(DISTINCT store.s_store_sk) AS __m1, 1 AS __b1 FROM tpcds.store AS store " in sql
    # by a dimension store does not reach, refused as a selected store_count is
    message = refusal(planner, Query(("total_sales",), ("item.i_brand",), q.filters))
    assert "'store_count' cannot be with this question's dimensions" in message


# --- unchanged (point 3) ------------------------------------------------------------


def test_metrics_of_one_dataset_are_one_select_as_before():
    planner = _with_metrics(
        average_sale="SUM(store_sales.ss_ext_sales_price) / COUNT(store_sales.ss_item_sk)",
        # an aggregate over two datasets is deferred (section 5.2): one join, as before
        revenue_at_list_price=LISTED,
        named_sales="SUM(store_sales.ss_quantity) FILTER (WHERE item.i_brand = 'x')",
    )
    for name in (
        "average_sale",
        "revenue_at_list_price",
        "named_sales",
        "store_sales.total_quantity",
    ):
        sql = planner.sql(Query((name,), ("item.i_brand",)))
        assert "UNION ALL" not in sql and sql.count("FROM tpcds.store_sales ") == 1
    assert planner.sql(Query(("average_sale",), ("item.i_brand",))).startswith(
        "SELECT item.__e1 AS i_brand, (SUM(store_sales.ss_ext_sales_price) * 1.0) / "
        "COUNT(store_sales.ss_item_sk) AS average_sale FROM tpcds.store_sales AS store_sales "
        "LEFT JOIN (SELECT DISTINCT item.i_item_sk AS __k0, item.i_brand AS __e1 "
        "FROM tpcds.item AS item) AS item"
    )


def test_a_dataset_metric_with_several_aggregates_is_one_component(tmp_path):
    data = yaml.safe_load(TPCDS.read_text())
    store_sales = next(d for d in data["datasets"] if d["name"] == "store_sales")
    store_sales["metrics"].append(
        {"name": "per_sale", "expression": expression("SUM(ss_ext_sales_price) / COUNT(*)")}
    )
    sql = Planner(load_data(tmp_path, data)).sql(
        Query(("store_sales.per_sale",), ("item.i_brand",))
    )
    assert "UNION ALL" not in sql and "COUNT(*)" in sql


def test_a_metric_over_a_filter_of_its_own_dataset_is_not_split():
    planner = _with_metrics(
        bulk="SUM(store_sales.ss_ext_sales_price) FILTER (WHERE store_sales.ss_quantity > 50) / "
        "COUNT(store_sales.ss_item_sk)"
    )
    assert "UNION ALL" not in planner.sql(Query(("bulk",), ("item.i_brand",)))


# --- refused (point 4) ----------------------------------------------------------------


def test_a_window_over_aggregates_of_different_datasets_is_refused():
    planner = _with_metrics(
        windowed="SUM(SUM(store_sales.ss_ext_sales_price)) OVER () / SUM(store.s_number_employees)"
    )
    message = refusal(planner, Query(("windowed",), ("store.s_state",)))
    assert (
        "metric 'windowed' is a window function over aggregates that read different datasets"
        in message
    )
    assert "windowed" in unanswerable_metrics(planner.model)


def test_a_column_outside_every_aggregate_is_refused():
    planner = _with_metrics(
        bare="SUM(store_sales.ss_ext_sales_price) / SUM(store.s_number_employees) + store.s_state"
    )
    message = refusal(planner, Query(("bare",), ("store.s_state",)))
    assert "metric 'bare' reads store.s_state outside every aggregate" in message
    assert "bare" in unanswerable_metrics(planner.model)


def _b1_planner(tmp_path, model_metrics=(), orders_metrics=()):
    """The spec's Appendix B.1 model with extra metrics, no server needed."""
    data = model_data("ossie_components_x")
    data["metrics"] += [{"name": n, "expression": expression(e)} for n, e in model_metrics]
    orders = next(d for d in data["datasets"] if d["name"] == "orders")
    orders.setdefault("metrics", []).extend(
        {"name": n, "expression": expression(e)} for n, e in orders_metrics
    )
    return Planner(load_data(tmp_path, data))


NO_AGGREGATE_WINDOWS = [
    "SUM(orders.amount) OVER (ORDER BY orders.id)",
    "ROW_NUMBER() OVER (ORDER BY orders.id)",
    "LAG(orders.amount, 1) OVER (ORDER BY orders.id)",
]


@pytest.mark.parametrize("expr", NO_AGGREGATE_WINDOWS)
def test_a_window_in_a_question_that_groups_nothing_is_refused(tmp_path, expr):
    planner = _b1_planner(tmp_path, model_metrics=[("running", expr)])
    message = refusal(planner, Query(("running",), ()))
    assert message.startswith("metric 'running' is a window function, and this question has no ")
    assert "ask for it with a dimension" in message
    # refused in this question only: with a dimension it plans (a column the window
    # reads that is not a dimension is ClickHouse's to refuse), and validate keeps it
    assert "OVER" in planner.sql(Query(("running",), ("customers.region",)))
    assert "running" not in unanswerable_metrics(planner.model)


def test_a_window_with_a_filter_on_rows_still_groups_nothing(tmp_path):
    planner = _b1_planner(
        tmp_path, model_metrics=[("running", "SUM(orders.amount) OVER (ORDER BY orders.id)")]
    )
    message = refusal(planner, Query(("running",), (), ("orders.amount > 10",)))
    assert message.startswith("metric 'running' is a window function")


def test_a_dataset_metric_window_in_a_question_that_groups_nothing_is_refused(tmp_path):
    planner = _b1_planner(tmp_path, orders_metrics=[("running", "SUM(amount) OVER (ORDER BY id)")])
    message = refusal(planner, Query(("orders.running",), ()))
    assert message.startswith("metric 'orders.running' is a window function")
    assert "orders.running" not in unanswerable_metrics(planner.model)


def test_a_window_beside_an_aggregate_is_not_refused_here(tmp_path):
    """A SELECT with an aggregate outside a window is one group; what a window there
    may read is ClickHouse's to say."""
    planner = _b1_planner(
        tmp_path, model_metrics=[("running", "SUM(orders.amount) OVER (ORDER BY orders.id)")]
    )
    assert "OVER" in planner.sql(Query(("running", "total_revenue")))


def test_a_window_in_a_stitched_question_is_refused_as_a_window(tmp_path):
    planner = _b1_planner(
        tmp_path, model_metrics=[("running", "SUM(orders.amount) OVER (ORDER BY orders.id)")]
    )
    for dimensions in ((), ("customers.region",)):
        message = refusal(planner, Query(("running", "total_revenue", "total_returns"), dimensions))
        assert message.startswith("metric 'running' is a window over the merged rows")


def test_a_window_over_a_dimension_ranks_the_groups(tmp_path):
    """A window over the grouped rows needs no aggregate when it reads dimensions."""
    planner = _b1_planner(
        tmp_path,
        model_metrics=[("region_rank", "RANK() OVER (ORDER BY customers.region DESC)")],
    )
    sql = planner.sql(Query(("region_rank",), ("customers.region",)))
    assert "OVER" in sql and "GROUP BY" in sql
    assert "region_rank" not in unanswerable_metrics(planner.model)


@pytest.mark.parametrize(
    "expr",
    [
        "SUM(SUM(orders.amount)) OVER ()",
        "RANK() OVER (ORDER BY SUM(orders.amount) DESC)",
        "SUM(orders.amount) - LAG(SUM(orders.amount), 1) OVER (ORDER BY customers.region)",
    ],
)
def test_a_window_over_grouped_rows_is_answered(tmp_path, expr):
    planner = _b1_planner(tmp_path, model_metrics=[("ranked", expr)])
    assert "OVER" in planner.sql(Query(("ranked",), ("customers.region",)))
    assert "ranked" not in unanswerable_metrics(planner.model)


def test_the_tpcds_window_metrics_are_answered():
    planner = _with_metrics()
    for metric, dimensions in (
        ("cumulative_sales", ("date_dim.d_date",)),
        ("brand_rank_in_store", ("store.s_store_sk", "item.i_brand")),
        ("monthly_sales_change", ("date_dim.d_year", "date_dim.d_moy")),
    ):
        assert "OVER" in planner.sql(Query((metric,), dimensions))
    assert unanswerable_metrics(planner.model) == {}


@pytest.mark.parametrize("dimensions", [(), ("customers.region",), ("customers.segment",)])
def test_a_column_outside_every_aggregate_of_one_dataset_is_refused(tmp_path, dimensions):
    planner = _b1_planner(
        tmp_path, model_metrics=[("weighted", "SUM(orders.amount) * customers.segment")]
    )
    message = refusal(planner, Query(("weighted",), dimensions))
    assert message.startswith("metric 'weighted' reads customers.segment outside every aggregate")
    assert "different datasets" not in message  # SUM(orders.amount) alone is one dataset
    assert "weighted" in unanswerable_metrics(planner.model)


def test_a_column_outside_every_aggregate_is_refused_when_it_is_also_a_dimension(tmp_path):
    planner = _b1_planner(
        tmp_path, model_metrics=[("weighted", "SUM(orders.amount) * customers.segment")]
    )
    message = refusal(planner, Query(("weighted",), ("customers.segment",)))
    assert "metric 'weighted' reads customers.segment outside every aggregate" in message


def test_a_column_in_a_window_clause_is_not_loose(tmp_path):
    planner = _b1_planner(
        tmp_path,
        model_metrics=[
            (
                "share",
                "SUM(orders.amount) / SUM(SUM(orders.amount)) OVER (PARTITION BY customers.region)",
            )
        ],
    )
    assert "OVER" in planner.sql(Query(("share",), ("customers.region",)))


def test_a_window_over_aggregates_of_one_dataset_is_unchanged():
    planner = _with_metrics(
        share="SUM(store_sales.ss_ext_sales_price) / "
        "SUM(SUM(store_sales.ss_ext_sales_price)) OVER ()"
    )
    sql = planner.sql(Query(("share",), ("item.i_brand",)))
    assert "UNION ALL" not in sql and "OVER" in sql


def test_a_written_aggregate_over_two_datasets_is_refused_in_a_filter():
    planner = _with_metrics()
    message = refusal(
        planner,
        Query(
            ("total_sales",),
            ("store.s_state",),
            ("SUM(store_sales.ss_ext_sales_price) / SUM(store.s_number_employees) > 5",),
        ),
    )
    assert (
        "filter 'SUM(store_sales.ss_ext_sales_price) / SUM(store.s_number_employees) > 5'"
        in message
    )
    assert "store and store_sales" in message and "name a metric" in message
    # a metric of the model that does the same is answered, by components
    sql = planner.sql(Query(("total_sales",), ("store.s_state",), ("store_productivity > 5",)))
    assert "UNION ALL" in sql


# --- the reference model, against ClickHouse -----------------------------------------


def test_customer_lifetime_value_is_total_sales_over_the_customer_count(tpcds, tmp_path):
    """52,624.71 where the denominator was the 90,858 customers who bought."""
    data = yaml.safe_load(TPCDS.read_text())
    data["metrics"].append(
        {
            "name": "customer_count",
            "expression": expression("COUNT(DISTINCT customer.c_customer_sk)"),
        }
    )
    ex = Executor(tpcds, load_data(tmp_path, data))
    (clv,) = ex.execute(Query(("customer_lifetime_value",))).rows[0]
    (sales,) = ex.execute(Query(("total_sales",))).rows[0]
    (count,) = ex.execute(Query(("customer_count",))).rows[0]
    assert float(sales) / count == pytest.approx(clv, rel=1e-9)


def _expected(sales: dict, counts: dict) -> dict:
    """Every key of either branch: sales over its count (NULL without a count or without
    sales), and the sales."""
    out = {}
    for key in set(sales) | set(counts):
        total, n = sales.get(key), counts.get(key, 0)
        ratio = None if total is None or n == 0 else float(total) / n
        out[key] = (ratio, None if total is None else float(total))
    return out


def _compare(got: list[tuple], expected: dict) -> None:
    assert len(got) == len(expected) and len({r[0] for r in got}) == len(got)
    for key, ratio, total in got:
        want_ratio, want_total = expected[key]
        for have, want in ((ratio, want_ratio), (total, want_total)):
            if want is None:
                assert have is None, (key, have)
            else:
                assert float(have) == pytest.approx(want, rel=1e-9), key


def test_customer_lifetime_value_by_first_name_is_each_sum_over_its_own_count(tpcds):
    """Each branch on its own, merged here by key; a NULL name is a key, and so is a name
    only one branch has."""
    ex = Executor(tpcds, TPCDS_MODEL)
    got = ex.execute(Query(("customer_lifetime_value", "total_sales"), ("customer.c_first_name",)))
    sales = dict(
        tpcds.query(
            "SELECT c.c_first_name, SUM(ss_ext_sales_price) FROM tpcds.store_sales "
            "LEFT JOIN tpcds.customer c ON ss_customer_sk = c.c_customer_sk "
            "GROUP BY c.c_first_name SETTINGS join_use_nulls = 1"
        ).result_rows
    )
    counts = dict(
        tpcds.query(
            "SELECT c_first_name, COUNT(DISTINCT c_customer_sk) FROM tpcds.customer "
            "GROUP BY c_first_name"
        ).result_rows
    )
    assert len(set(counts) - set(sales)) > 0  # names of customers who bought nothing
    _compare(got.rows, _expected(sales, counts))


def test_customer_lifetime_value_filtered_on_the_many_side_divides_by_customers_with_such_a_sale(
    tpcds,
):
    ex = Executor(tpcds, TPCDS_MODEL)
    (got,) = ex.execute(
        Query(("customer_lifetime_value",), (), ("store_sales.ss_quantity > 50",))
    ).rows[0]
    sales = tpcds.query(
        "SELECT SUM(ss_ext_sales_price) FROM tpcds.store_sales WHERE ss_quantity > 50"
    ).result_rows[0][0]
    customers = tpcds.query(
        "SELECT COUNT(DISTINCT c_customer_sk) FROM tpcds.customer WHERE c_customer_sk IN "
        "(SELECT ss_customer_sk FROM tpcds.store_sales WHERE ss_quantity > 50)"
    ).result_rows[0][0]
    assert float(sales) / customers == pytest.approx(got, rel=1e-9)


def test_store_productivity_is_answered_by_state(tpcds):
    """Sales per employee: each state's sales over its stores' staff, where the
    employees were once summed per sale. Every key of either branch, the NULL group
    of sales without a store included."""
    ex = Executor(tpcds, TPCDS_MODEL)
    got = ex.execute(Query(("store_productivity", "total_sales"), ("store.s_state",))).rows
    sales = dict(
        tpcds.query(
            "SELECT st.s_state, SUM(ss.ss_ext_sales_price) FROM tpcds.store_sales ss "
            "LEFT JOIN tpcds.store st ON ss.ss_store_sk = st.s_store_sk "
            "GROUP BY st.s_state SETTINGS join_use_nulls = 1"
        ).result_rows
    )
    staff = dict(
        tpcds.query(
            "SELECT s_state, SUM(s_number_employees) FROM tpcds.store GROUP BY s_state"
        ).result_rows
    )
    assert None in sales and None not in staff  # the NULL group is the sales branch's alone
    _compare(got, _expected(sales, staff))

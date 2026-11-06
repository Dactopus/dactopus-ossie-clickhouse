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
from tests.test_stitch import COLUMNS
from tests.test_stitch import FIXTURE as STITCH

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
    for name, rows in (("b1", B1), ("ext", EXT)):
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
    assert "define a metric of dataset store_sales, which reaches " in message
    assert "COUNT(DISTINCT ss_customer_sk)" in message


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
        assert "UNION ALL" not in sql and sql.count("FROM") == 1
    assert planner.sql(Query(("average_sale",), ("item.i_brand",))).startswith(
        "SELECT item.i_brand AS i_brand, (SUM(store_sales.ss_ext_sales_price) * 1.0) / "
        "COUNT(store_sales.ss_item_sk) AS average_sale FROM tpcds.store_sales AS store_sales "
        "LEFT JOIN tpcds.item AS item"
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


def test_customer_lifetime_value_by_first_name_is_each_sum_over_its_own_count(tpcds):
    """Each branch on its own, merged here by key (a NULL name is a key)."""
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
    keys = set(sales) | set(counts)
    assert len(got.rows) == len(keys) and len(got.rows) > 1
    for name, ratio, total in got.rows:
        assert (total is None) == (name not in sales)
        if total is not None:
            assert float(sales[name]) == pytest.approx(float(total), rel=1e-9)
            assert float(sales[name]) / counts[name] == pytest.approx(float(ratio), rel=1e-9)
        else:
            assert ratio is None


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
    employees were once summed per sale."""
    ex = Executor(tpcds, TPCDS_MODEL)
    got = {r[0]: r[1] for r in ex.execute(Query(("store_productivity",), ("store.s_state",))).rows}
    gold = tpcds.query(
        """SELECT sales.s_state, toFloat64(sales.total) / staff.n FROM
             (SELECT st.s_state AS s_state, SUM(ss.ss_ext_sales_price) AS total
              FROM tpcds.store_sales ss LEFT JOIN tpcds.store st ON ss.ss_store_sk = st.s_store_sk
              GROUP BY st.s_state) sales
             JOIN (SELECT s_state, SUM(s_number_employees) AS n FROM tpcds.store
                   GROUP BY s_state) staff ON sales.s_state = staff.s_state"""
    ).result_rows
    assert gold and all(float(v) == pytest.approx(got[k], rel=1e-9) for k, v in gold)

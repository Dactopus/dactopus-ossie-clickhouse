import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.planner import (
    Catalog,
    PlanError,
    Planner,
    Query,
    TableInfo,
    source_name,
    source_table,
    unanswerable_metrics,
)
from tests.test_model import FIXTURE

MODEL = load_model(FIXTURE)
P = Planner(MODEL)
SETTINGS = "SETTINGS join_use_nulls = 1, aggregate_functions_null_for_empty = 1"


def test_source_table():
    assert source_table("tpcds.public.store_sales").sql() == "tpcds.store_sales"
    assert source_table("db.t").sql() == "db.t"
    assert source_table("t").sql() == "t"
    assert source_table("db.select").sql(dialect="clickhouse") == "db.select"
    assert source_table("db.x-y").sql(dialect="clickhouse") == 'db."x-y"'
    # Unquoted text is the name it spells, as in 0.2.3, next to quoted parts too.
    assert source_table("db.t$x").sql(dialect="clickhouse") == 'db."t$x"'
    assert source_table("db.t#1").sql(dialect="clickhouse") == 'db."t#1"'
    assert source_table('"my db".t$x').sql(dialect="clickhouse") == '"my db"."t$x"'
    assert source_table("`shop`.t#1").sql(dialect="clickhouse") == '"shop"."t#1"'
    assert source_table("  db.t ").sql() == "db.t"


@pytest.mark.parametrize(
    "source",
    [
        "SELECT * FROM t",
        "db.t x",
        "db . t",
        "numbers(10)",
        "s3('x')",
        "db.t(1)",
        # what refuses a table name must not hide that a query is one
        "SELECT 1;",
        "SELECT * FROM t -- x",
        "SELECT replaceRegexpAll(s, '\\d', '') FROM t",
        "SELECT * FROM t WHERE s = 'say \"hi'",
    ],
)
def test_query_source_refused(source):
    # Table functions included: one such dataset must not break introspection.
    with pytest.raises(PlanError, match="query sources"):
        source_table(source)


@pytest.mark.parametrize(
    "source",
    [
        "db.",
        ".t",
        "db..t",
        "a.b.c.d",
        "`db`.``",
        "`db`.``.t",
        "db.x`y`",
        "db.t;",
        "db.t -- legacy",
        "/* x */ db.t",
        "db.t--legacy",
        "db.t/*x*/",
        "db.o'brien",
        '"my db.t',
        "db.a\\",
        "`db`.`a\\x41`",  # ClickHouse reads `aA`; emitted unescaped, it would differ
        'db."t',
    ],
)
def test_garbled_source_refused(source):
    # Refused, not read as a neighbouring table with the rest dropped.
    with pytest.raises(PlanError, match="cannot map"):
        source_table(source)


@pytest.mark.parametrize(
    "source, db, name",
    [
        ("`shop`.`orders`", "shop", "orders"),  # as dbt-clickhouse writes it
        ("`my.db`.`orders`", "my.db", "orders"),
        ('"Sales DB".orders', "Sales DB", "orders"),
        ('`a`."b.c".`t`', "a", "t"),
        ('"a""b".`c``d`', 'a"b', "c`d"),
        ("`x (1)`.`t--1;`", "x (1)", "t--1;"),
    ],
)
def test_source_table_quoted(source, db, name):
    t = source_table(source)
    assert (t.db, t.name) == (db, name)
    assert source_table(t.sql(dialect="clickhouse")) == t  # quotes kept in SQL
    assert source_name(source) == f"{db}.{name}"


def test_backslash_in_a_column_name(tmp_path):
    # ClickHouse reads `\` in a quoted name as an escape: emitted as written,
    # "a\x41" would read column aA. The server-side check is in test_executor.
    p = tmp_path / "esc.yaml"
    p.write_text(
        'version: "0.2.0.dev0"\nname: esc\ndatasets:\n'
        "  - name: esc\n    source: db.esc\n    fields:\n"
        "      - name: v\n"
        "        expression: {dialects: [{dialect: ANSI_SQL, expression: '\"a\\x41\"'}]}\n"
        "metrics:\n"
        "  - name: total\n"
        "    expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(esc.v)}]}\n"
    )
    sql = Planner(load_model(p)).sql(Query(metrics=("total",), dimensions=("esc.v",)))
    assert sql.startswith('SELECT esc."a\\\\x41" AS v, SUM(esc."a\\\\x41") AS total FROM')


def test_single_dataset_no_join():
    sql = P.sql(Query(metrics=("total_sales",)))
    assert sql == (
        "SELECT SUM(store_sales.ss_ext_sales_price) AS total_sales "
        "FROM tpcds.store_sales AS store_sales " + SETTINGS
    )


def test_dimension_filter_join_snapshot():
    sql = P.sql(
        Query(
            metrics=("total_sales", "total_profit"),
            dimensions=("item.i_brand",),
            filters=("date_dim.d_year = 1998",),
            limit=10,
        )
    )
    assert sql == (
        "SELECT item.i_brand AS i_brand, "
        "SUM(store_sales.ss_ext_sales_price) AS total_sales, "
        "SUM(store_sales.ss_net_profit) AS total_profit "
        "FROM tpcds.store_sales AS store_sales "
        "LEFT JOIN tpcds.date_dim AS date_dim ON store_sales.ss_sold_date_sk = date_dim.d_date_sk "
        "LEFT JOIN tpcds.item AS item ON store_sales.ss_item_sk = item.i_item_sk "
        "WHERE date_dim.d_year = 1998 "
        "GROUP BY item.i_brand ORDER BY total_sales DESC, i_brand ASC LIMIT 10 " + SETTINGS
    )


def test_field_expression_is_inlined_and_qualified():
    sql = P.sql(Query(dimensions=("customer.customer_full_name",), metrics=("total_sales",)))
    expr = "customer.c_first_name || ' ' || customer.c_last_name"
    assert f"{expr} AS customer_full_name" in sql and f"GROUP BY {expr}" in sql


def test_case_insensitive_resolution_keeps_physical_case():
    sql = P.sql(Query(metrics=("TOTAL_SALES",), dimensions=("ITEM.I_BRAND",)))
    assert "item.i_brand AS i_brand" in sql and "SUM(store_sales.ss_ext_sales_price)" in sql


def test_dimension_only_query():
    sql = P.sql(Query(dimensions=("store.s_state",)))
    assert sql.startswith("SELECT store.s_state AS s_state FROM tpcds.store AS store GROUP BY")


def test_window_metric_passes_through():
    sql = P.sql(Query(metrics=("cumulative_sales",), dimensions=("date_dim.d_date",)))
    assert "SUM(SUM(store_sales.ss_ext_sales_price)) OVER (ORDER BY date_dim.d_date" in sql


def test_window_metric_keeps_nulls_clause():
    planner = _with_metrics(
        headcount_rank="RANK() OVER (ORDER BY SUM(store.s_number_employees) ASC NULLS FIRST)"
    )
    sql = planner.sql(Query(metrics=("headcount_rank",), dimensions=("store.s_state",)))
    assert "RANK() OVER (ORDER BY SUM(store.s_number_employees) ASC NULLS FIRST)" in sql


def test_order_by():
    # Default: first metric descending, only when there are rows to rank.
    q = Query(metrics=("total_profit", "total_sales"), dimensions=("item.i_brand",))
    assert "ORDER BY total_profit DESC" in P.sql(q)
    assert "ORDER BY" not in P.sql(Query(metrics=("total_sales",)))
    # Then each dimension ascending, so ties come in one order and a limit keeps
    # the same rows on every run.
    assert "ORDER BY i_brand ASC SETTINGS" in P.sql(Query(dimensions=("item.i_brand",)))
    q = Query(
        metrics=("total_sales",), dimensions=("item.i_brand",), order_by=("total_sales desc",)
    )
    assert P.sql(q).endswith(
        "GROUP BY item.i_brand ORDER BY total_sales DESC, i_brand ASC " + SETTINGS
    )
    q = Query(
        metrics=("total_sales",),
        dimensions=("item.i_brand",),
        order_by=("item.i_brand", "TOTAL_SALES"),
    )
    # NULLs last both ways: ClickHouse's default, so no NULLS clause (docs/design.md).
    assert "ORDER BY i_brand ASC, total_sales ASC SETTINGS" in P.sql(q)
    # The caller's spelling of dataset.field never leaks into ORDER BY: aliases are the model's.
    q = Query(dimensions=("date_dim.D_YEAR",), order_by=("DATE_DIM.D_YEAR",))
    assert "AS d_year" in P.sql(q) and "ORDER BY d_year ASC" in P.sql(q)
    with pytest.raises(
        PlanError, match="not a selected metric or dimension \\(did you mean: total_sales"
    ):
        P.sql(Query(metrics=("total_sales",), order_by=("total_sale",)))
    with pytest.raises(PlanError, match="use 'name'"):
        P.sql(Query(metrics=("total_sales",), order_by=("total_sales down",)))
    # i_brand is selectable as "i_brand" and "item.i_brand"; the hint names it once.
    with pytest.raises(PlanError, match=r"\(did you mean: i_brand\?\)$"):
        P.sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",), order_by=("brand",)))


def test_deterministic():
    q = Query(metrics=("total_sales",), dimensions=("store.s_state", "item.i_category"))
    assert P.sql(q) == P.sql(q)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (Query(metrics=("total_sale",)), "unknown metric 'total_sale' (did you mean: total_sales"),
        (
            Query(dimensions=("item.brand",)),
            "unknown field 'item.brand' (did you mean: item.i_brand",
        ),
        (Query(dimensions=("items.i_brand",)), "unknown dataset 'items' (did you mean: item"),
        (Query(dimensions=("i_brand",)), "must be dataset.field"),
        (Query(), "at least one metric or dimension"),
        (
            Query(metrics=("total_sales",), filters=("total_sale > 1",)),
            "unqualified column 'total_sale' in 'total_sale > 1'; use dataset.field or a "
            "metric name (did you mean: total_sales",
        ),
        (
            Query(metrics=("total_sales",), filters=("ss_quantity > 1",)),
            "use dataset.field or a metric name (did you mean: store_sales.ss_quantity",
        ),
        (
            Query(
                metrics=("total_sales",),
                dimensions=("item.i_brand", "store.s_store_sk"),
                filters=("brand_rank_in_store <= 3",),
            ),
            "'brand_rank_in_store <= 3' uses a window function",
        ),
        (
            Query(metrics=("total_sales",), filters=("RANK() OVER (ORDER BY COUNT(*)) < 3",)),
            "uses a window function",
        ),
        (
            Query(
                metrics=("total_sales",),
                dimensions=("item.i_brand",),
                filters=("total_sales > store_sales.ss_quantity",),
            ),
            "mixes an aggregate with store_sales.ss_quantity, which is not a dimension",
        ),
        (
            Query(
                metrics=("total_sales",),
                dimensions=("customer.customer_full_name",),
                filters=("total_sales > 1 AND customer.c_first_name <> ''",),
            ),
            "mixes an aggregate with customer.c_first_name",
        ),
        (
            Query(metrics=("total_sales",), filters=("store_sales.ss_quantity IN (SELECT 1)",)),
            "not allowed",
        ),
        (
            Query(dimensions=("item.i_brand", "store.s_state")),
            "not joined through many-to-one relationships",
        ),
        (Query(metrics=("total_sales",), filters=("date_dim.d_year = ",)), "cannot parse"),
    ],
)
def test_errors(query, message):
    with pytest.raises(PlanError, match=message.replace("(", r"\(").replace("?", r"\?")):
        P.sql(query)


@pytest.mark.parametrize(
    "f",
    [
        "1 = 1; DROP TABLE tpcds.item",
        "date_dim.d_year = 1 UNION ALL SELECT 1",
        "date_dim.d_year = 1 SETTINGS max_threads = 1",
        "date_dim.d_year = {y:UInt16}",
        "date_dim.d_year IN (SELECT 1)",
        "EXISTS (SELECT 1)",
    ],
)
def test_filters_cannot_smuggle_statements(f):
    """Filters come from an agent as free text: expressions only, never statements."""
    with pytest.raises(PlanError):
        P.sql(Query(metrics=("total_sales",), filters=(f,)))


def test_filters_pass_functions_through():
    # Any function goes to ClickHouse as written; grants and quotas there are the boundary.
    sql = P.sql(Query(metrics=("total_sales",), filters=("toString(date_dim.d_year) = '1998'",)))
    assert "WHERE toString(date_dim.d_year) = '1998'" in sql


def test_aggregate_filters_go_to_having():
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("item.i_brand",),
            filters=(
                "date_dim.d_year = 1998",
                "total_sales > 1000000",  # metric by name
                "COUNT(*) > 10",  # raw aggregate
            ),
        )
    )
    assert sql.endswith(
        "WHERE date_dim.d_year = 1998 GROUP BY item.i_brand "
        "HAVING SUM(store_sales.ss_ext_sales_price) > 1000000 AND COUNT(*) > 10 "
        "ORDER BY total_sales DESC, i_brand ASC " + SETTINGS
    )
    # No dimensions: HAVING without GROUP BY is still one SELECT.
    assert P.sql(Query(metrics=("total_sales",), filters=("total_sales > 1",))).endswith(
        "HAVING SUM(store_sales.ss_ext_sales_price) > 1 " + SETTINGS
    )
    # A HAVING filter may use a dimension of the query, even one with an expression.
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("customer.customer_full_name",),
            filters=("total_sales > 1 AND customer.customer_full_name <> ''",),
        )
    )
    assert "HAVING (SUM(store_sales.ss_ext_sales_price) > 1) AND ((customer.c_first_name" in sql


def test_filter_on_unselected_metric_adds_its_join():
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("item.i_brand",),
            filters=("customer_lifetime_value > 1",),
        )
    )
    assert (
        "LEFT JOIN tpcds.customer AS customer "
        "ON store_sales.ss_customer_sk = customer.c_customer_sk" in sql
    )
    assert sql.endswith(
        "GROUP BY item.i_brand HAVING ((SUM(store_sales.ss_ext_sales_price) * 1.0) / "
        "COUNT(DISTINCT customer.c_customer_sk)) > 1 ORDER BY total_sales DESC, i_brand ASC "
        "SETTINGS join_use_nulls = 1, aggregate_functions_null_for_empty = 1"
    )


def _variant(mutate) -> Planner:
    """A planner over a copy of the fixture model changed by ``mutate(data)``."""
    import copy

    from ossie import OssieDocument

    m = copy.deepcopy(MODEL.model_dump(by_alias=True))
    mutate(m)
    return Planner(OssieDocument.model_validate(m))


def test_join_requires_unique_key_on_target():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        item["primary_key"] = ["i_item_id"]
        item["unique_keys"] = None

    with pytest.raises(PlanError, match="do not cover a primary or unique key"):
        _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))


def test_join_requires_declared_key_on_target():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        item["primary_key"] = None
        item["unique_keys"] = None

    with pytest.raises(PlanError, match="'item' declares no primary_key or unique_keys"):
        _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))


def _superset_of_item_key(m):
    """Join store_sales to item on (i_item_sk, i_manufact_id): wider than the key."""
    r = next(r for r in m["relationships"] if r["name"] == "store_sales_to_item")
    r["from_columns"] = ["ss_item_sk", "ss_store_sk"]
    r["to_columns"] = ["I_ITEM_SK", "i_manufact_id"]


def test_join_on_superset_of_key():
    """A superset of a unique key is unique too (apache/ossie#330 reads it the same way)."""
    sql = _variant(_superset_of_item_key).sql(
        Query(metrics=("total_sales",), dimensions=("item.i_brand",))
    )
    assert (
        "LEFT JOIN tpcds.item AS item ON store_sales.ss_item_sk = item.I_ITEM_SK "
        "AND store_sales.ss_store_sk = item.i_manufact_id"
    ) in sql


def test_join_on_superset_of_unique_key():
    def mutate(m):
        _superset_of_item_key(m)
        item = next(d for d in m["datasets"] if d["name"] == "item")
        item["primary_key"] = ["i_item_id"]  # not covered; the unique key is
        item["unique_keys"] = [["i_item_sk"]]

    sql = _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))
    assert "LEFT JOIN tpcds.item AS item ON" in sql


def test_dictionary_joined_on_superset_of_its_key_is_a_join():
    """dictGet by the key alone would ignore the extra column and match rows the join would not."""
    planner = _variant(_superset_of_item_key)
    planner.catalog = Catalog(
        {"item": TableInfo("Dictionary", frozenset(), dictionary_key="i_item_sk")}
    )
    sql = planner.sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))
    assert "LEFT JOIN tpcds.item AS item" in sql and "dictGet" not in sql


def test_anonymous_aggregate_filter_goes_to_having():
    # APPROX_PERCENTILE has no SQLGlot node; it must still count as an aggregate.
    sql = P.sql(
        Query(
            metrics=("total_sales",),
            dimensions=("item.i_brand",),
            filters=("APPROX_PERCENTILE(store_sales.ss_net_profit, 0.95) > 10",),
        )
    )
    assert "WHERE" not in sql
    assert "HAVING quantileTDigest(0.95)(store_sales.ss_net_profit) > 10" in sql


def test_selected_names_must_differ():
    with pytest.raises(PlanError, match="two selected columns named 'total_sales'"):
        P.sql(Query(metrics=("total_sales", "TOTAL_SALES")))

    def mutate(m):
        next(x for x in m["metrics"] if x["name"] == "total_sales")["name"] = "I_BRAND"

    with pytest.raises(PlanError, match="two selected columns named 'I_BRAND'"):
        _variant(mutate).sql(Query(metrics=("i_brand",), dimensions=("item.i_brand",)))

    def same_field_name(m):
        next(d for d in m["datasets"] if d["name"] == "store")["fields"][0]["name"] = "i_brand"

    with pytest.raises(PlanError, match="fields of two datasets, cannot share a name"):
        _variant(same_field_name).sql(Query(dimensions=("item.i_brand", "store.i_brand")))


def test_two_roots_are_ambiguous():
    def mutate(m):
        m["relationships"] += [
            {"name": "i2s", "from": "item", "to": "store",
             "from_columns": ["i_item_sk"], "to_columns": ["s_store_sk"]},
            {"name": "s2i", "from": "store", "to": "item",
             "from_columns": ["s_store_sk"], "to_columns": ["i_item_sk"]},
        ]  # fmt: skip

    with pytest.raises(PlanError, match="ambiguous root dataset among \\['item', 'store'\\]"):
        _variant(mutate).sql(Query(dimensions=("item.i_brand", "store.s_state")))


def test_field_may_only_use_its_own_columns():
    def mutate(m):
        item = next(d for d in m["datasets"] if d["name"] == "item")
        brand = next(f for f in item["fields"] if f["name"] == "i_brand")
        brand["expression"]["dialects"][0]["expression"] = "store.s_state"

    with pytest.raises(PlanError, match="field item.i_brand references another dataset"):
        _variant(mutate).sql(Query(dimensions=("item.i_brand",)))


def test_catalog_final_and_dictionary():
    """The executor's catalog changes the SQL shape; pinned here without a server."""
    catalog = Catalog(
        {
            "store_sales": TableInfo("ReplacingMergeTree", frozenset(), dedup=True),
            "item": TableInfo("Dictionary", frozenset(), dictionary_key="i_item_sk"),
        }
    )
    sql = Planner(MODEL, catalog).sql(
        Query(metrics=("total_sales",), dimensions=("item.i_brand", "item.i_item_sk"))
    )
    assert sql == (
        "SELECT dictGetOrNull('tpcds.item', 'i_brand', store_sales.ss_item_sk) AS i_brand, "
        "CASE WHEN dictHas('tpcds.item', store_sales.ss_item_sk) THEN store_sales.ss_item_sk "
        "ELSE NULL END AS i_item_sk, SUM(store_sales.ss_ext_sales_price) AS total_sales "
        "FROM tpcds.store_sales AS store_sales FINAL "
        "GROUP BY dictGetOrNull('tpcds.item', 'i_brand', store_sales.ss_item_sk), "
        "CASE WHEN dictHas('tpcds.item', store_sales.ss_item_sk) THEN store_sales.ss_item_sk "
        "ELSE NULL END ORDER BY total_sales DESC, i_brand ASC, i_item_sk ASC " + SETTINGS
    )
    # A dictionary joined on something other than its key is a plain LEFT JOIN.
    catalog["item"] = TableInfo("Dictionary", frozenset(), dictionary_key="i_item_id")
    sql = Planner(MODEL, catalog).sql(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))
    assert "LEFT JOIN tpcds.item AS item" in sql and "dictGet" not in sql


def test_two_relationships_to_the_same_dataset_are_ambiguous():
    def mutate(m):
        sold = next(r for r in m["relationships"] if r["name"] == "store_sales_to_date")
        m["relationships"].append(sold | {"name": "store_sales_to_ship_date"})

    with pytest.raises(PlanError, match=r"ambiguous join .* 'store_sales_to_ship_date'"):
        _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("date_dim.d_year",)))


def test_cli(capsys):
    assert main(["sql", str(FIXTURE), "-m", "total_sales", "-d", "item.i_brand", "-l", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SELECT\n") and "LEFT JOIN tpcds.item" in out
    assert main(["sql", str(FIXTURE), "-m", "nope"]) == 1
    assert "unknown metric" in capsys.readouterr().err


def test_cli_reports_connection_and_policy_errors_without_traceback(capsys):
    nowhere = "http://127.0.0.1:9"  # nothing listens; a refused connection is an error line
    assert main(["sql", str(FIXTURE), "-m", "total_sales", "--url", nowhere]) == 1
    assert capsys.readouterr().err.startswith("error: ")
    args = ["sql", str(FIXTURE), "-m", "total_sales", "--url", nowhere, "--policy", "nope.yaml"]
    assert main(args) == 1
    assert "nope.yaml" in capsys.readouterr().err


# --- execution against TPC-DS (fixture ``tpcds`` in conftest) -----------------


@pytest.mark.parametrize(
    "ref",
    [f"{d.name}.{f.name}" for d in MODEL.datasets for f in d.fields],
)
def test_every_field_is_a_dimension(ref, tpcds):
    assert tpcds.query(P.sql(Query(dimensions=(ref,), limit=1))).result_rows


def test_no_fan_out_and_no_loss(tpcds):
    """Grouping by a joined dimension must neither multiply nor drop fact rows."""
    total = tpcds.query("SELECT SUM(ss_ext_sales_price) FROM tpcds.store_sales").result_rows[0][0]
    grouped = tpcds.query(
        P.sql(Query(metrics=("total_sales",), dimensions=("item.i_brand", "customer.c_last_name")))
    ).result_rows
    assert sum(r[-1] for r in grouped if r[-1] is not None) == total


def test_filter_and_values(tpcds):
    direct = tpcds.query(
        "SELECT SUM(ss.ss_ext_sales_price) FROM tpcds.store_sales ss "
        "JOIN tpcds.date_dim d ON ss.ss_sold_date_sk = d.d_date_sk WHERE d.d_year = 1998"
    ).result_rows[0][0]
    planned = tpcds.query(
        P.sql(Query(metrics=("total_sales",), filters=("date_dim.d_year = 1998",)))
    ).result_rows[0][0]
    assert planned == direct


def test_having_values(tpcds):
    direct = tpcds.query(
        "SELECT i.i_brand, SUM(ss.ss_ext_sales_price) AS s FROM tpcds.store_sales ss "
        "LEFT JOIN tpcds.item i ON ss.ss_item_sk = i.i_item_sk GROUP BY i.i_brand "
        "HAVING s > 100000 ORDER BY i.i_brand NULLS LAST " + SETTINGS
    ).result_rows
    planned = tpcds.query(
        P.sql(
            Query(
                metrics=("total_sales",),
                dimensions=("item.i_brand",),
                filters=("total_sales > 100000",),
                order_by=("item.i_brand",),
            )
        )
    ).result_rows
    assert planned and planned == direct


ANSWERABLE = [m for m in MODEL.metrics if m.name not in unanswerable_metrics(MODEL)]


@pytest.mark.parametrize("metric", ANSWERABLE, ids=[m.name for m in ANSWERABLE])
def test_every_metric_executes(metric, tpcds):
    dims = {
        "cumulative_sales": ("date_dim.d_date",),
        "brand_rank_in_store": ("store.s_store_sk", "item.i_brand"),
        "monthly_sales_change": ("date_dim.d_year", "date_dim.d_moy"),
    }.get(metric.name, ("store.s_state",))
    rows = tpcds.query(P.sql(Query(metrics=(metric.name,), dimensions=dims, limit=5))).result_rows
    assert rows


def _with_metrics(**expressions: str) -> Planner:
    """The fixture with extra metrics and store_extra, a one-to-one extension of store."""

    def mutate(m):
        store = next(d for d in m["datasets"] if d["name"] == "store")
        m["datasets"].append({**store, "name": "store_extra"})
        m["relationships"].append(
            {
                "name": "store_to_extra",
                "from": "store",
                "to": "store_extra",
                "from_columns": ["s_store_sk"],
                "to_columns": ["s_store_sk"],
            }
        )
        m["relationships"].append(
            {
                "name": "sales_to_extra",
                "from": "store_sales",
                "to": "store_extra",
                "from_columns": ["ss_store_sk"],
                "to_columns": ["s_store_sk"],
            }
        )
        for name, e in expressions.items():
            m["metrics"].append(
                {
                    "name": name,
                    "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": e}]},
                }
            )

    return _variant(mutate)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        # A joined dataset's rows repeat once per root row: store_productivity sums
        # each store's employees once per sale.
        (
            Query(metrics=("store_productivity",), dimensions=("store.s_store_name",)),
            "metric 'store_productivity' applies SUM(store.s_number_employees) to store "
            "columns alone over store_sales rows",
        ),
        (
            Query(metrics=("total_sales",), filters=("store_productivity > 1",)),
            "filter 'store_productivity > 1' applies SUM(store.s_number_employees)",
        ),
        # Even a repeat-safe aggregate is about other rows: only customers who bought.
        (
            Query(metrics=("customers",), filters=("store_sales.ss_quantity > 1",)),
            "(because of filter 'store_sales.ss_quantity > 1')",
        ),
        (
            Query(metrics=("customers",), dimensions=("store_sales.ss_store_sk",)),
            "(because of dimension 'store_sales.ss_store_sk')",
        ),
        # Checked after rewrite: APPROX_PERCENTILE is an aggregate only then.
        # Nested in another rewrite: the check sees the inner one rewritten too.
        (
            Query(metrics=("sales_per_nonzero_median_staff",)),
            "applies quantileTDigest(0.5)(store.s_number_employees)",
        ),
        # COUNT(*) alone counts whatever rows the question is about: stores here,
        # sales next to total_sales.
        (
            Query(metrics=("cnt",), dimensions=("store.s_state",)),
            "metric 'cnt' uses COUNT(*), which reads no column",
        ),
        (
            Query(metrics=("total_sales",), filters=("cnt > 1",)),
            "metric 'cnt' uses COUNT(*), which reads no column",
        ),
        # A FILTER column counts for its aggregate: this counts sales once per store.
        (
            Query(metrics=("tn_stores",), dimensions=("store_sales.ss_quantity",)),
            "metric 'tn_stores' aggregates store rows",
        ),
        # Only the root's key makes a join one-to-one: sales repeat per store_extra row.
        (
            Query(metrics=("extra_staff",), dimensions=("store.s_state",)),
            "metric 'extra_staff' applies SUM(store_extra.s_number_employees)",
        ),
        (
            Query(metrics=("sales_per_median_staff",)),
            "metric 'sales_per_median_staff' applies "
            "quantileTDigest(0.5)(store.s_number_employees)",
        ),
    ],
)
def test_refuses_aggregates_over_other_rows_than_the_root(query, message):
    planner = _with_metrics(
        headcount="SUM(store.s_number_employees)",
        customers="COUNT(DISTINCT customer.c_customer_sk)",
        sales_per_median_staff="SUM(store_sales.ss_ext_sales_price) / "
        "APPROX_PERCENTILE(store.s_number_employees, 0.5)",
        sales_per_nonzero_median_staff="SUM(store_sales.ss_ext_sales_price) / "
        "NULLIFZERO(APPROX_PERCENTILE(store.s_number_employees, 0.5))",
        cnt="COUNT(*)",
        tn_stores="COUNT(*) FILTER (WHERE store.s_state = 'TN')",
        extra_staff="SUM(store_sales.ss_ext_sales_price) / SUM(store_extra.s_number_employees)",
    )
    with pytest.raises(PlanError) as e:
        planner.sql(query)
    assert message in str(e.value)


@pytest.mark.parametrize(
    "catalog",
    [
        None,  # no server: SQLGlot's list of ClickHouse aggregates
        {},  # a plain dict says nothing either
        Catalog(),  # system.functions could not be read
        Catalog(aggregate=lambda name: name == "countIf"),  # the server's word
    ],
)
def test_server_aggregates_outside_the_spec_are_checked_as_aggregates(catalog):
    """countIf is an aggregate to ClickHouse, not to the spec: the planner refuses
    it over repeated rows and filters on it in HAVING, with or without a server."""
    planner = _with_metrics(
        big_store_sales="SUM(store_sales.ss_ext_sales_price) / "
        "countIf(store.s_number_employees > 10)",
        quoted='SUM(store_sales.ss_ext_sales_price) / "countIf"(store.s_number_employees > 10)',
    )
    planner.catalog = catalog
    having = Query(
        metrics=("total_sales",),
        dimensions=("store_sales.ss_store_sk",),
        filters=("countIf(store_sales.ss_quantity > 1) > 5",),
    )
    # ClickHouse runs a quoted name as the same aggregate.
    for name in ("big_store_sales", "quoted"):
        with pytest.raises(PlanError, match=r"applies countIf\(store.s_number_employees > 10\)"):
            planner.sql(Query(metrics=(name,)))
    sql = planner.sql(having)
    assert "HAVING countIf(store_sales.ss_quantity > 1) > 5" in sql and "WHERE" not in sql


def test_the_server_names_aggregates_sqlglot_does_not_know():
    """A user-defined or newer aggregate is one only on the server's word."""
    planner = _with_metrics(
        per_store="SUM(store_sales.ss_ext_sales_price) / myAgg(store.s_store_sk)"
    )
    query = Query(metrics=("per_store",))
    assert "myAgg(store.s_store_sk)" in planner.sql(query)
    planner.catalog = Catalog(aggregate=lambda name: name == "myAgg")
    with pytest.raises(PlanError, match=r"applies myAgg\(store.s_store_sk\)"):
        planner.sql(query)


def test_a_plain_dict_catalog_still_plans():
    catalog = {"store": TableInfo("ReplacingMergeTree", frozenset(), dedup=True)}
    sql = Planner(MODEL, catalog).sql(
        Query(metrics=("total_sales",), dimensions=("store.s_state",))
    )
    assert "tpcds.store AS store FINAL" in sql


@pytest.mark.parametrize(
    ("aggregate", "safe"),
    [
        ("uniqExact(store.s_store_sk)", True),
        ("anyLast(store.s_state)", True),
        ("ANY_VALUE(store.s_state)", True),
        ("argMax(store.s_state, store.s_number_employees)", True),
        ("MAXIf(store.s_number_employees, store.s_state = 'TN')", True),
        ("uniqExactIfOrNull(store.s_store_sk, store.s_state = 'TN')", True),
        ("singleValueOrNull(store.s_state)", True),
        # Distinct makes any aggregate repeat-safe, as the keyword does.
        ("sumDistinct(store.s_number_employees)", True),
        ("sumIf(DISTINCT store.s_number_employees, store.s_state = 'TN')", True),
        # Repeat-sensitive, or a combinator that may not keep it: refused.
        ("countIf(store.s_number_employees > 10)", False),
        ("sumIf(store.s_number_employees, store.s_state = 'TN')", False),
        ("uniqArray([store.s_store_sk])", False),
        ("anyHeavy(store.s_state)", False),
        ("UNIQEXACT(store.s_store_sk)", False),
    ],
)
def test_clickhouse_aggregates_that_repeats_do_not_change(aggregate, safe):
    """Over sales rows, each store repeats once per sale: only an aggregate repeats
    cannot change may read store columns alone."""
    planner = _with_metrics(per_store=f"SUM(store_sales.ss_ext_sales_price) / {aggregate}")
    planner.catalog = Catalog(aggregate=lambda name: True)
    query = Query(metrics=("per_store",))
    if safe:
        assert "LEFT JOIN tpcds.store AS store" in planner.sql(query)
    else:
        with pytest.raises(PlanError, match="may read a joined dataset on their own"):
            planner.sql(query)


@pytest.mark.parametrize(
    "query",
    [
        # Each metric from its own root, merged on the dimension.
        Query(metrics=("total_sales", "headcount"), dimensions=("store.s_state",)),
        Query(metrics=("total_sales", "headcount"), dimensions=("store.s_state", "store.s_city")),
        # No dimension: one row of totals.
        Query(metrics=("headcount", "items")),
        # A FILTER column counts for its aggregate: stores, not sales once per store.
        Query(metrics=("total_sales", "tn_stores")),
        # A filter on a metric of the other dataset, selected or not.
        Query(metrics=("total_sales",), dimensions=("store.s_state",), filters=("headcount > 10",)),
    ],
)
def test_stitches_metrics_that_no_one_root_answers(query):
    planner = _with_metrics(
        headcount="SUM(store.s_number_employees)",
        items="COUNT(item.i_item_sk)",
        tn_stores="COUNT(*) FILTER (WHERE store.s_state = 'TN')",
    )
    assert " UNION ALL " in planner.sql(query)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        # headcount cannot be grouped by item: no root answers it, alone or not.
        (
            Query(metrics=("headcount", "items"), dimensions=("item.i_brand",)),
            "datasets ['item', 'store'] are not joined through many-to-one relationships",
        ),
        # Both read store: the dimension, not the mix of metrics, is the cause.
        (
            Query(metrics=("headcount", "stores"), dimensions=("store_sales.ss_quantity",)),
            "metric 'headcount' aggregates store rows",
        ),
        # A window over merged rows would rank each side's rows apart.
        (
            Query(metrics=("total_sales", "headcount_rank"), dimensions=("store.s_state",)),
            "metric 'headcount_rank' is a window over the merged rows",
        ),
        (
            Query(
                metrics=("total_sales", "headcount"),
                dimensions=("store.s_state",),
                filters=("SUM(store.s_number_employees) > 10",),
            ),
            "applies an aggregate function to metrics answered from different datasets",
        ),
    ],
)
def test_stitching_refuses(query, message):
    planner = _with_metrics(
        headcount="SUM(store.s_number_employees)",
        headcount_rank="RANK() OVER (ORDER BY SUM(store.s_number_employees) DESC)",
        stores="COUNT(store.s_store_sk)",
        items="COUNT(item.i_item_sk)",
    )
    with pytest.raises(PlanError) as e:
        planner.sql(query)
    assert message in str(e.value)


def _area(m):
    """store_area, a copy of store joined from store only."""
    store = next(d for d in m["datasets"] if d["name"] == "store")
    m["datasets"].append({**store, "name": "store_area"})
    m["relationships"].append(
        {"name": "store_to_area", "from": "store", "to": "store_area",
         "from_columns": ["s_store_sk"], "to_columns": ["s_store_sk"]}
    )  # fmt: skip


def test_path_of_two_relationships():
    """store_area is reached through store, which the query does not name
    (apache/ossie#246 §6.9); values are compared in test_paths.py."""
    sql = _variant(_area).sql(Query(metrics=("total_sales",), dimensions=("store_area.s_state",)))
    assert (
        "FROM tpcds.store_sales AS store_sales "
        "LEFT JOIN tpcds.store AS store ON store_sales.ss_store_sk = store.s_store_sk "
        "LEFT JOIN tpcds.store AS store_area ON store.s_store_sk = store_area.s_store_sk "
    ) in sql


def test_a_direct_and_a_longer_path_are_ambiguous():
    def mutate(m):
        _area(m)
        m["relationships"].append(
            {"name": "sales_to_area", "from": "store_sales", "to": "store_area",
             "from_columns": ["ss_store_sk"], "to_columns": ["s_store_sk"]}
        )  # fmt: skip

    with pytest.raises(PlanError) as e:
        _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("store_area.s_state",)))
    assert "paths 'sales_to_area' and 'store_sales_to_store' -> 'store_to_area'" in str(e.value)
    # Two paths to a dataset the question does not touch do not matter.
    _variant(mutate).sql(Query(metrics=("total_sales",), dimensions=("store.s_state",)))


def test_relationships_in_a_cycle():
    """store and store_area join each other one to one: a path never visits a
    dataset twice (apache/ossie#246 §6.9 rule 4), and either could be the root."""

    def mutate(m):
        _area(m)
        m["relationships"].append(
            {"name": "area_to_store", "from": "store_area", "to": "store",
             "from_columns": ["s_store_sk"], "to_columns": ["s_store_sk"]}
        )  # fmt: skip

    planner = _variant(mutate)
    assert "LEFT JOIN tpcds.store AS store_area" in planner.sql(
        Query(metrics=("total_sales",), dimensions=("store_area.s_state",))
    )
    with pytest.raises(PlanError, match="ambiguous root dataset"):
        planner.sql(Query(dimensions=("store.s_state", "store_area.s_city")))


def test_one_to_one_through_a_path():
    """store -> store_area -> area_copy, each on a key of its from side: no row of
    area_copy repeats over store's, so a store metric may sum it."""

    def mutate(m):
        _area(m)
        store = next(d for d in m["datasets"] if d["name"] == "store")
        m["datasets"].append({**store, "name": "area_copy"})
        m["relationships"].append(
            {"name": "area_to_copy", "from": "store_area", "to": "area_copy",
             "from_columns": ["s_store_sk"], "to_columns": ["s_store_sk"]}
        )  # fmt: skip
        e = "SUM(store.s_number_employees) - SUM(area_copy.s_number_employees)"
        m["metrics"].append(
            {"name": "gap", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": e}]}}
        )

    planner = _variant(mutate)
    assert planner._one_to_one("store") == {"store_area", "area_copy"}
    assert "LEFT JOIN tpcds.store AS area_copy" in planner.sql(
        Query(metrics=("gap",), dimensions=("store.s_state",))
    )


def test_dictionary_on_a_path():
    """A dictionary at the end of a path is read with dictGetOrNull on the key of
    the dataset before it; one in the middle is joined, since the next join reads it."""
    planner = _variant(_area)
    catalog = Catalog(
        {"store_area": TableInfo("Dictionary", frozenset(), dictionary_key="s_store_sk")}
    )
    query = Query(metrics=("total_sales",), dimensions=("store_area.s_state",))
    sql = Planner(planner.model, catalog).sql(query)
    assert "dictGetOrNull('tpcds.store', 's_state', store.s_store_sk)" in sql
    assert "LEFT JOIN tpcds.store AS store ON" in sql and "AS store_area" not in sql
    catalog = Catalog({"store": TableInfo("Dictionary", frozenset(), dictionary_key="s_store_sk")})
    sql = Planner(planner.model, catalog).sql(query)
    assert "dictGet" not in sql and "LEFT JOIN tpcds.store AS store ON" in sql


@pytest.mark.parametrize(
    ("metrics", "dimensions"),
    [
        (("customer_lifetime_value",), ("store.s_state",)),  # customer only under DISTINCT
        (("cumulative_sales",), ("date_dim.d_date",)),  # date_dim only in OVER
        (("brand_rank_in_store",), ("store.s_store_sk", "item.i_brand")),
        (("monthly_sales_change",), ("date_dim.d_year", "date_dim.d_moy")),
        (("revenue_at_list_price",), ("store.s_state",)),  # one value per root row
        (("sales_per_largest_staff",), ("item.i_brand",)),  # MAX ignores repeats
        (("sales_per_median_distinct_staff",), ("item.i_brand",)),  # so does DISTINCT
        # COUNT(*) next to an aggregate that names its dataset counts that dataset's rows.
        (("average_sale",), ("store.s_state",)),
        (("bulk_sales",), ("store.s_state",)),  # FILTER reads the root
        (("bulk_median_quantity",), ("store.s_state",)),  # FILTER under a rewrite too
        (("staff_per_extra",), ("store.s_state",)),  # store_extra rows do not repeat
    ],
)
def test_allows_aggregates_that_read_root_rows(metrics, dimensions):
    planner = _with_metrics(
        revenue_at_list_price="SUM(store_sales.ss_quantity * item.i_current_price)",
        sales_per_largest_staff="SUM(store_sales.ss_ext_sales_price) / "
        "MAX(store.s_number_employees)",
        sales_per_median_distinct_staff="SUM(store_sales.ss_ext_sales_price) / "
        "MEDIAN(DISTINCT store.s_number_employees)",
        average_sale="SUM(store_sales.ss_ext_sales_price) / COUNT(*)",
        bulk_sales="COUNT(*) FILTER (WHERE store_sales.ss_quantity > 50)",
        bulk_median_quantity="PERCENTILE_DISC(0.5) WITHIN GROUP "
        "(ORDER BY store_sales.ss_quantity) FILTER (WHERE store_sales.ss_quantity > 50)",
        staff_per_extra="SUM(store.s_number_employees) / SUM(store_extra.s_number_employees)",
    )
    planner.sql(Query(metrics=metrics, dimensions=dimensions))


def test_unanswerable_metrics():
    assert list(unanswerable_metrics(MODEL)) == ["store_productivity"]
    # Dataset names are case-insensitive: one dataset, spelled two ways.
    planner = _with_metrics(
        margin="SUM(Store_Sales.ss_ext_sales_price) - SUM(store_sales.ss_net_profit)"
    )
    assert "margin" not in unanswerable_metrics(planner.model)
    assert "SUM(store.s_number_employees)" in unanswerable_metrics(MODEL)["store_productivity"]
    planner = _with_metrics(
        typo="SUM(store.s_number_employes)",
        cnt="COUNT(*)",
        one_to_one="SUM(store.s_number_employees) / SUM(store_extra.s_number_employees)",
    )
    found = unanswerable_metrics(planner.model)
    assert "unknown field 'store.s_number_employes'" in found["typo"]
    assert "uses COUNT(*), which reads no column" in found["cnt"]
    assert "one_to_one" not in found


def test_unanswerable_metrics_are_hidden(tpcds):
    from dactopus_ossie_clickhouse.executor import Executor

    names = [m.name for m in Executor(tpcds, MODEL).model.metrics]
    assert "store_productivity" not in names and "total_sales" in names


def test_nested_rewrite_in_a_metric():
    planner = _with_metrics(
        median_or_null="NULLIFZERO(APPROX_PERCENTILE(store_sales.ss_quantity, 0.5))"
    )
    sql = planner.sql(Query(metrics=("median_or_null",)))
    assert "nullIf(quantileTDigest(0.5)(store_sales.ss_quantity), 0) AS median_or_null" in sql

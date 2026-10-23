"""Access control: ClickHouse grants decide, the model follows.

Needs a ClickHouse where the connected user may create users (SQL access
management); skips otherwise."""

from pathlib import Path

import clickhouse_connect
import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.access import Hidden, Policy, restrict
from dactopus_ossie_clickhouse.executor import Executor
from dactopus_ossie_clickhouse.planner import PlanError, Query
from tests.conftest import unavailable
from tests.test_executor import FIXTURE, SETUP

USERS = """
DROP USER IF EXISTS ossie_analyst, ossie_clerk, ossie_local;
DROP ROLE IF EXISTS ossie_sales;
DROP ROW POLICY IF EXISTS de_only ON ossie_test.orders;
CREATE ROLE ossie_sales;
CREATE USER ossie_analyst IDENTIFIED WITH no_password;
GRANT SELECT ON ossie_test.orders TO ossie_analyst;
GRANT ossie_sales TO ossie_analyst;
CREATE ROW POLICY de_only ON ossie_test.orders FOR SELECT
  USING country_code = 'DE' TO ossie_analyst;
CREATE USER ossie_clerk IDENTIFIED WITH no_password;
GRANT SELECT(order_id, amount) ON ossie_test.orders TO ossie_clerk;
GRANT SELECT ON ossie_test.country TO ossie_clerk;
CREATE USER ossie_local IDENTIFIED WITH no_password DEFAULT DATABASE ossie_test;
GRANT SELECT ON ossie_test.* TO ossie_local;
"""
POLICY = Path(__file__).parent / "fixtures" / "policy.yaml"

# --- pure trimming, no server -------------------------------------------------

MODEL = load_model(Path(__file__).parent / "fixtures" / "tpcds.yaml")


def test_restrict_cascades():
    m = restrict(MODEL, Hidden(datasets=frozenset({"customer"})))
    assert [d.name for d in m.datasets] == ["store_sales", "date_dim", "item", "store"]
    assert all(r.to != "customer" for r in m.relationships)
    assert "customer_lifetime_value" not in {x.name for x in m.metrics}  # references customer
    assert "total_sales" in {x.name for x in m.metrics}


def test_restrict_field_and_metric():
    m = restrict(
        MODEL,
        Hidden(fields=frozenset({"STORE.S_NUMBER_EMPLOYEES"}), metrics=frozenset({"total_profit"})),
    )
    store = next(d for d in m.datasets if d.name == "store")
    assert "s_number_employees" not in {f.name for f in store.fields}
    names = {x.name for x in m.metrics}
    assert "store_productivity" not in names and "total_profit" not in names


def test_a_datasets_metric_goes_with_its_fields_or_its_name():
    def scoped(m):
        return [x.name for d in m.datasets for x in d.metrics or []]

    assert scoped(restrict(MODEL, Hidden())) == ["total_quantity"]
    # Its bare names are fields of its own dataset.
    assert scoped(restrict(MODEL, Hidden(fields=frozenset({"store_sales.ss_quantity"})))) == []
    assert scoped(restrict(MODEL, Hidden(fields=frozenset({"item.ss_quantity"})))) == [
        "total_quantity"
    ]
    assert scoped(restrict(MODEL, Hidden(metrics=frozenset({"STORE_SALES.total_quantity"})))) == []
    assert scoped(restrict(MODEL, Hidden(metrics=frozenset({"total_quantity"})))) == [
        "total_quantity"
    ]


def test_policy_matches_user_and_roles():
    p = Policy.load(POLICY)
    assert p.hidden_for("ossie_analyst", ["ossie_sales"]).metrics == {"spread"}
    assert p.hidden_for("someone", []) == Hidden()


def test_policy_accepts_empty_rule_and_rejects_wrong_shape(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("analyst:\nclerk:\n  hidden_metrics: [spread]\n")
    assert Policy.load(p).hidden_for("analyst", []) == Hidden()
    assert Policy.load(p).hidden_for("clerk", []).metrics == {"spread"}
    p.write_text("- analyst\n")
    with pytest.raises(ValueError, match="expected a mapping"):
        Policy.load(p)
    p.write_text("analyst: [spread]\n")
    with pytest.raises(ValueError, match="expected a mapping"):
        Policy.load(p)


def test_restrict_with_nothing_left_is_a_readable_error():
    with pytest.raises(PlanError, match="none of the model's datasets is readable"):
        restrict(MODEL, Hidden(datasets=frozenset(d.name for d in MODEL.datasets)))


@pytest.mark.parametrize("hidden", ["STORE_SALES.SS_ITEM_SK", "item.i_item_sk"])
def test_restrict_drops_a_relationship_with_a_hidden_field(hidden):
    # It joins on fields; one hidden must not reach SQL through the join.
    m = restrict(MODEL, Hidden(fields=frozenset({hidden})))
    assert "store_sales_to_item" not in {r.name for r in m.relationships}
    assert len(m.relationships) == len(MODEL.relationships) - 1
    assert hidden.split(".")[1].upper() not in m.model_dump_json().upper()


def test_restrict_drops_the_keys_a_hidden_field_is_in():
    m = restrict(MODEL, Hidden(fields=frozenset({"customer.c_customer_id"})))
    customer = next(d for d in m.datasets if d.name == "customer")
    assert customer.primary_key == ["c_customer_sk"]
    assert customer.unique_keys == [["c_customer_sk"]]  # [c_customer_id] dropped
    m = restrict(MODEL, Hidden(fields=frozenset({"customer.c_customer_sk"})))
    customer = next(d for d in m.datasets if d.name == "customer")
    assert customer.primary_key is None and customer.unique_keys == [["c_customer_id"]]


# --- against ClickHouse -------------------------------------------------------


@pytest.fixture(scope="module")
def admin(clickhouse):
    for stmt in filter(None, (s.strip() for s in SETUP.split(";"))):
        clickhouse.command(stmt)
    try:
        for stmt in filter(None, (s.strip() for s in USERS.split(";"))):
            clickhouse.command(stmt)
    except Exception as e:
        unavailable(f"cannot create users here: {str(e)[:80]}")
    yield clickhouse
    clickhouse.command("DROP ROW POLICY IF EXISTS de_only ON ossie_test.orders")
    clickhouse.command("DROP DATABASE ossie_test")
    clickhouse.command("DROP USER IF EXISTS ossie_analyst, ossie_clerk, ossie_local")
    clickhouse.command("DROP ROLE IF EXISTS ossie_sales")


def as_user(admin, user):
    return clickhouse_connect.get_client(dsn=admin.url, username=user, password="")


def test_hidden_dataset_is_invisible(admin):
    ex = Executor(as_user(admin, "ossie_analyst"), load_model(FIXTURE))
    assert ex.user == "ossie_analyst" and "ossie_sales" in ex.roles
    assert [d.name for d in ex.model.datasets] == ["orders", "orders_raw"]  # no country
    with pytest.raises(PlanError) as e:
        ex.execute(Query(metrics=("revenue",), dimensions=("country.name",)))
    msg = str(e.value)
    assert "unknown dataset 'country'" in msg
    assert "did you mean" not in msg or "country" not in msg.split("did you mean")[1]


def test_row_policy_applies(admin):
    ex = Executor(as_user(admin, "ossie_analyst"), load_model(FIXTURE))
    assert ex.execute(Query(metrics=("revenue",))).rows == [(15.0,)]  # DE only, deduplicated


def test_column_grant_hides_field_and_dependents(admin):
    ex = Executor(as_user(admin, "ossie_clerk"), load_model(FIXTURE))
    orders = next(d for d in ex.model.datasets if d.name == "orders")
    assert {f.name for f in orders.fields} == {"order_id", "amount"}
    assert "country" in {d.name for d in ex.model.datasets}  # readable on its own
    assert ex.model.relationships == []  # the join needs country_code, which the clerk cannot read
    assert ex.execute(Query(metrics=("revenue",))).rows == [(65.0,)]
    with pytest.raises(PlanError) as e:
        ex.execute(Query(metrics=("revenue",), dimensions=("country.name",)))
    assert "country_code" not in str(e.value)


def test_policy_file_hides_metric_for_role(admin):
    ex = Executor(as_user(admin, "ossie_analyst"), load_model(FIXTURE), Policy.load(POLICY))
    assert "spread" not in {m.name for m in ex.model.metrics}
    with pytest.raises(PlanError, match="unknown metric 'spread'"):
        ex.execute(Query(metrics=("spread",)))
    unrestricted = Executor(admin, load_model(FIXTURE), Policy.load(POLICY))
    assert "spread" in {m.name for m in unrestricted.model.metrics}


def test_bare_source_reads_the_users_default_database(admin, tmp_path):
    # No database in the URL: ClickHouse reads the user's DEFAULT DATABASE, so
    # introspection must too, or it misses the ReplacingMergeTree and its FINAL.
    p = tmp_path / "bare.yaml"
    p.write_text(FIXTURE.read_text().replace("source: ossie_test.", "source: "))
    ex = Executor(as_user(admin, "ossie_local"), load_model(p))
    assert ex.database == "ossie_test"
    r = ex.execute(Query(metrics=("revenue",)))
    assert "FINAL" in r.sql and r.rows == [(65.0,)]

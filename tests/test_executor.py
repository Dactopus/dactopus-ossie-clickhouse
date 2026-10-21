"""Executor against a ClickHouse test database with a ReplacingMergeTree fact
table and a dictionary dimension, created here."""

import re
from pathlib import Path

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.executor import Executor, ServerFunctions
from dactopus_ossie_clickhouse.model import overrides
from dactopus_ossie_clickhouse.planner import (
    _CLICKHOUSE_REPEAT_SAFE,
    _CLICKHOUSE_REPEAT_SAFE_ANY_CASE,
    PlanError,
    Query,
)
from dactopus_ossie_clickhouse.translate import parse
from tests.conftest import FakeClickHouse

FIXTURE = Path(__file__).parent / "fixtures" / "history.yaml"
SETUP = """
CREATE DATABASE IF NOT EXISTS ossie_test;
DROP DICTIONARY IF EXISTS ossie_test.country;
DROP DICTIONARY IF EXISTS ossie_test.`country names`;
DROP DICTIONARY IF EXISTS ossie_test.`country.v2`;
DROP TABLE IF EXISTS ossie_test.orders;
DROP TABLE IF EXISTS ossie_test.esc;
DROP TABLE IF EXISTS ossie_test.country_src;
CREATE TABLE ossie_test.orders (order_id UInt32, amount Float64, country_code String, ver UInt32)
  ENGINE = ReplacingMergeTree(ver) ORDER BY order_id;
INSERT INTO ossie_test.orders VALUES (1, 10, 'DE', 1), (2, 20, 'FR', 1), (3, 30, 'XX', 1);
INSERT INTO ossie_test.orders VALUES (1, 15, 'DE', 2);
CREATE TABLE ossie_test.country_src (code String, name String) ENGINE = Memory;
INSERT INTO ossie_test.country_src VALUES ('DE', 'Germany'), ('FR', 'France');
CREATE DICTIONARY ossie_test.country (code String, name String) PRIMARY KEY code
  SOURCE(CLICKHOUSE(TABLE 'country_src' DB 'ossie_test')) LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(0);
CREATE DICTIONARY ossie_test.`country names` (code String, name String) PRIMARY KEY code
  SOURCE(CLICKHOUSE(TABLE 'country_src' DB 'ossie_test')) LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(0);
CREATE DICTIONARY ossie_test.`country.v2` (code String, name String) PRIMARY KEY code
  SOURCE(CLICKHOUSE(TABLE 'country_src' DB 'ossie_test')) LAYOUT(COMPLEX_KEY_HASHED()) LIFETIME(0);
"""


@pytest.fixture(scope="module")
def ex(clickhouse):
    for stmt in filter(None, (s.strip() for s in SETUP.split(";"))):
        clickhouse.command(stmt)
    yield Executor(clickhouse, load_model(FIXTURE))
    clickhouse.command("DROP DATABASE ossie_test")


def test_introspection(ex):
    assert ex.catalog["orders"].engine == "ReplacingMergeTree" and ex.catalog["orders"].dedup
    assert not ex.catalog["orders_raw"].dedup  # overridden
    assert ex.catalog["country"].engine == "Dictionary"
    assert ex.catalog["country"].dictionary_key == "code"
    assert overrides(ex.model.datasets[1]) == {"dedup": "none"}


ORDERS = {"order_id", "amount", "country_code"}
REPLICATED = ("ReplicatedReplacingMergeTree", "", ("", ""), ORDERS)
PLAIN = ("MergeTree", "", ("", ""), ORDERS)


def distributed(table: str) -> tuple:
    return ("Distributed", f"Distributed('c', 'ossie_test', '{table}', rand())", ("", ""), ORDERS)


def view(target: str) -> tuple:
    return ("MaterializedView", "", ("ossie_test", target), ORDERS)


@pytest.mark.parametrize(
    "orders, under, final",
    [
        (("ReplacingMergeTree", "", ("", ""), ORDERS), None, True),
        (REPLICATED, None, True),  # a cluster with replication
        (("SharedReplacingMergeTree", "", ("", ""), ORDERS), None, True),  # ClickHouse Cloud
        (PLAIN, None, False),
        (("ReplicatedMergeTree", "", ("", ""), ORDERS), None, False),
        # A cluster's Distributed table over the replicated table on each shard.
        (distributed("orders_local"), REPLICATED, True),
        (distributed("orders_local"), PLAIN, False),  # FINAL would be an error
        (view("orders_local"), REPLICATED, True),
        (view("orders_local"), PLAIN, False),
        (distributed("orders_local"), view("orders_store"), True),  # store: Replacing
        (distributed("orders_local"), None, False),  # local table hidden: undecided
    ],
)
def test_final_follows_the_engine_of_the_stored_rows(orders, under, final):
    """Engines as system.tables reports them; no server needed."""
    tables = {
        ("ossie_test", "orders"): orders,
        ("ossie_test", "orders_store"): REPLICATED,
        ("ossie_test", "country"): ("Memory", "", ("", ""), {"code", "name"}),
    }
    if under:
        tables["ossie_test", "orders_local"] = under
    ex = Executor(FakeClickHouse(tables), load_model(FIXTURE))
    assert ex.catalog["orders"].dedup is final
    assert ("FINAL" in ex.planner.sql(Query(metrics=("revenue",)))) is final
    assert not ex.catalog["orders_raw"].dedup  # overridden
    undecided = [p for p in ex.check() if "cannot see" in p]
    if orders[0] == "Distributed" and not under:
        # orders_raw says "none": only orders is undecided, and the hidden
        # table's name stays out of the message.
        assert len(undecided) == 1 and "'orders'" in undecided[0]
        assert "orders_local" not in undecided[0]
    else:
        assert undecided == []


def test_view_target_unknown_before_clickhouse_26_6():
    class Older(FakeClickHouse):
        def query(self, sql, parameters=None):
            if "target_table" in sql:
                raise RuntimeError("Unknown expression identifier `target_database`")
            return super().query(sql, parameters)

    tables = {
        ("ossie_test", "orders"): view("orders_local"),
        ("ossie_test", "orders_local"): REPLICATED,
    }
    ex = Executor(Older(tables), load_model(FIXTURE))
    assert not ex.catalog["orders"].dedup and ex.undecided == {"orders"}


def test_dedup_final_forces_final(tmp_path):
    # For a source whose stored rows this user cannot see.
    p = tmp_path / "forced.yaml"
    p.write_text(FIXTURE.read_text().replace('{"dedup": "none"}', '{"dedup": "final"}'))
    tables = {("ossie_test", "orders"): distributed("orders_local")}
    ex = Executor(FakeClickHouse(tables), load_model(p))
    assert ex.catalog["orders_raw"].dedup and not ex.catalog["orders"].dedup


def test_dedup_with_final(ex):
    r = ex.execute(Query(metrics=("revenue",)))
    assert "FROM ossie_test.orders AS orders FINAL" in r.sql
    assert r.rows == [(65.0,)]  # 15 + 20 + 30, not 10 + 15 + 20 + 30
    raw = ex.execute(Query(metrics=("raw_revenue",)))
    assert "FINAL" not in raw.sql and raw.rows == [(75.0,)]


VIEW = "MATERIALIZED VIEW ossie_test.wrapped TO ossie_test.{} AS SELECT * FROM ossie_test.feed"
MERGE = "TABLE ossie_test.wrapped AS ossie_test.orders ENGINE = Merge({}, '^{}$')"


@pytest.mark.parametrize(
    "wrapped, revenue",
    [
        (VIEW.format("orders"), 65.0),
        (MERGE.format("ossie_test", "orders"), 65.0),
        (MERGE.format("REGEXP('^ossie_t')", "orders"), 65.0),
        # FINAL over a plain MergeTree would be an error on the view.
        (VIEW.format("plain"), 75.0),
        (MERGE.format("ossie_test", "plain"), 75.0),
    ],
)
def test_final_through_a_view_or_merge_table(ex, tmp_path, wrapped, revenue):
    ex.client.command("DROP TABLE IF EXISTS ossie_test.wrapped")
    ex.client.command(
        "CREATE TABLE IF NOT EXISTS ossie_test.feed AS ossie_test.orders ENGINE = Null"
    )
    ex.client.command(
        "CREATE TABLE IF NOT EXISTS ossie_test.plain ENGINE = MergeTree ORDER BY order_id"
        " AS SELECT * FROM ossie_test.orders"  # every version: 75
    )
    ex.client.command(f"CREATE {wrapped}")
    r = _with_sources(ex, tmp_path, orders="ossie_test.wrapped").execute(
        Query(metrics=("revenue",))
    )
    assert r.rows == [(revenue,)] and ("FINAL" in r.sql) is (revenue == 65.0)


def test_dictionary_read_with_dictget(ex):
    r = ex.execute(Query(metrics=("revenue",), dimensions=("country.name",)))
    assert "dictGetOrNull('ossie_test.country', 'name', orders.country_code)" in r.sql
    assert "JOIN" not in r.sql
    assert set(r.rows) == {(None, 30.0), ("France", 20.0), ("Germany", 15.0)}


def test_dictionary_key_column_is_the_join_key(ex):
    # dictGet* cannot read the key; unknown keys are NULL as with a LEFT JOIN.
    r = ex.execute(Query(metrics=("revenue",), dimensions=("country.code",)))
    assert "dictGetOrNull('ossie_test.country', 'code'" not in r.sql
    assert set(r.rows) == {(None, 30.0), ("FR", 20.0), ("DE", 15.0)}


def _with_sources(ex, tmp_path, **sources):
    """Executor over the fixture with datasets' sources replaced."""
    text = FIXTURE.read_text()
    for old, new in sources.items():
        text = text.replace(f"source: ossie_test.{old}\n", f"source: '{new}'\n")
    p = tmp_path / "quoted.yaml"
    p.write_text(text)
    return Executor(ex.client, load_model(p))


def test_quoted_source(ex, tmp_path):
    # dbt-clickhouse writes every source quoted.
    q = _with_sources(ex, tmp_path, orders="`ossie_test`.`orders`", country='"ossie_test".country')
    r = q.execute(Query(metrics=("revenue",), dimensions=("country.name",)))
    assert 'FROM "ossie_test"."orders" AS orders FINAL' in r.sql
    assert "dictGetOrNull('ossie_test.country', 'name'" in r.sql
    assert set(r.rows) == {(None, 30.0), ("France", 20.0), ("Germany", 15.0)}


@pytest.mark.parametrize("name", ["country names", "country.v2"])
def test_dictionary_with_special_name(ex, tmp_path, name):
    # dictGet* takes the name unquoted and cannot address one with a dot: join it.
    q = _with_sources(ex, tmp_path, country=f"ossie_test.`{name}`")
    r = q.execute(Query(metrics=("revenue",), dimensions=("country.name",)))
    if "." in name:
        assert f'LEFT JOIN ossie_test."{name}" AS country' in r.sql and "dictGet" not in r.sql
    else:
        assert f"dictGetOrNull('ossie_test.{name}', 'name'" in r.sql
    assert set(r.rows) == {(None, 30.0), ("France", 20.0), ("Germany", 15.0)}


def test_dictionary_joined_on_superset_of_its_key(ex, tmp_path):
    # The extra column narrows the match: no order's code equals a country name,
    # so every row is unmatched. dictGet by the key alone would find DE and FR.
    p = tmp_path / "superset.yaml"
    text = FIXTURE.read_text().replace(
        "from_columns: [country_code]", "from_columns: [country_code, country_code]"
    )
    p.write_text(text.replace("to_columns: [code]", "to_columns: [code, name]"))
    r = Executor(ex.client, load_model(p)).execute(
        Query(metrics=("revenue",), dimensions=("country.name",))
    )
    assert "LEFT JOIN ossie_test.country AS country" in r.sql and "dictGet" not in r.sql
    assert r.rows == [(None, 65.0)]


def test_inlined_expressions_keep_their_precedence(ex, tmp_path):
    # A field or metric named inside an operator is inlined as one operand.
    p = tmp_path / "inline.yaml"
    text = FIXTURE.read_text().replace(
        "      - name: country_code\n",
        "      - name: net\n"
        "        expression: {dialects: [{dialect: ANSI_SQL, expression: amount - 5}]}\n"
        "      - name: country_code\n",
        1,
    )
    text += (
        "  - name: avg_order\n"
        "    expression: {dialects: [{dialect: ANSI_SQL, "
        "expression: SUM(orders.amount) / COUNT(orders.order_id)}]}\n"
    )
    p.write_text(text)
    e = Executor(ex.client, load_model(p))
    # Amounts 15, 20, 30: (amount - 5) * 2 > 25 keeps 20 and 30, not amount - 5 * 2.
    assert e.execute(Query(metrics=("revenue",), filters=("orders.net * 2 > 25",))).rows == [
        (50.0,)
    ]
    # 100 / (65 / 3) is 4.6, not 100 / 65 / 3.
    r = e.execute(Query(metrics=("revenue",), filters=("100 / avg_order > 4",)))
    assert r.rows == [(65.0,)]


def test_nan_and_inf_become_none(ex):
    r = ex.execute(Query(metrics=("spread",), dimensions=("orders.order_id",), limit=1))
    assert r.rows[0][1] is None
    assert ex.execute(Query(metrics=("zero_ratio",))).rows == [(None,)]


def test_aggregate_over_no_rows_is_null(ex):
    """SQL's SUM over no rows is NULL; ClickHouse's is 0 unless told otherwise."""
    q = Query(metrics=("revenue",), filters=("orders.amount > 1000",))
    assert ex.execute(q).rows == [(None,)]


@pytest.mark.parametrize(
    "source, problem",
    [
        ("ossie_test.`o'brien x`", "not found in ClickHouse"),
        ("ossie_test.o'brien\\x", "cannot map source"),
        ("numbers(10)", "query sources are not supported yet: 'numbers(10)'"),
    ],
)
def test_odd_sources_do_not_break_introspection(ex, tmp_path, source, problem):
    p = tmp_path / "odd.yaml"
    text = FIXTURE.read_text().replace("source: ossie_test.country\n", f"source: {source}\n")
    p.write_text(text)
    odd = Executor(ex.client, load_model(p))
    assert any(problem in x for x in odd.check())
    assert odd.execute(Query(metrics=("revenue",))).rows == [(65.0,)]


def test_check_reports_database_level_problems(ex, tmp_path):
    assert ex.check() == []
    text = FIXTURE.read_text().replace("expression: amount}", "expression: amnt}", 1)
    text = text.replace("source: ossie_test.country", "source: ossie_test.countries")
    text = text.replace("from_columns: [country_code]", "from_columns: [country_cod]")
    p = tmp_path / "bad.yaml"
    p.write_text(text)
    problems = Executor(ex.client, load_model(p)).check()
    assert any("column 'amnt' (field 'amount') not in ossie_test.orders" in x for x in problems)
    assert any("source 'ossie_test.countries' not found" in x for x in problems)
    assert any(
        "relationship 'orders_to_country': column 'country_cod' not in dataset 'orders'" in x
        for x in problems
    )
    assert main(["validate", str(p), "--url", ex.client.url]) == 1


def test_bare_source_reads_the_url_database(ex, tmp_path, capsys):
    p = tmp_path / "bare.yaml"
    p.write_text(FIXTURE.read_text().replace("source: ossie_test.", "source: "))
    url = f"{ex.client.url}/ossie_test"
    assert main(["validate", str(p), "--url", url]) == 0
    assert capsys.readouterr().out.endswith(f" against {ex.client.url} (database ossie_test)\n")
    assert main(["query", str(p), "-m", "revenue", "-d", "country.name", "--url", url]) == 0
    assert "Germany\t15.0" in capsys.readouterr().out  # deduplicated, dictionary found
    # A database without the tables: the refusal names it.
    ex.client.command("CREATE DATABASE IF NOT EXISTS ossie_empty")
    try:
        assert (
            main(["query", str(p), "-m", "revenue", "--url", f"{ex.client.url}/ossie_empty"]) == 1
        )
    finally:
        ex.client.command("DROP DATABASE ossie_empty")
    assert "(database ossie_empty); check" in capsys.readouterr().err


def test_backslash_in_a_column_name(ex, tmp_path):
    # ClickHouse reads `\` in a quoted name as an escape: emitted as written,
    # "a\x41" would read column aA.
    ex.client.command(r"CREATE TABLE ossie_test.esc (aA Float64, `a\\x41` Float64) ENGINE = Memory")
    ex.client.command("INSERT INTO ossie_test.esc VALUES (1, 2)")
    p = tmp_path / "esc.yaml"
    p.write_text(
        'version: "0.2.0.dev0"\nname: esc\ndatasets:\n'
        "  - name: esc\n    source: ossie_test.esc\n    fields:\n"
        "      - name: v\n"
        "        expression: {dialects: [{dialect: ANSI_SQL, expression: '\"a\\x41\"'}]}\n"
        "metrics:\n"
        "  - name: total\n"
        "    expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(esc.v)}]}\n"
    )
    r = Executor(ex.client, load_model(p)).execute(Query(metrics=("total",)))
    assert r.rows == [(2.0,)]
    assert 'SUM(esc."a\\\\x41")' in r.sql


def test_query_sources_are_not_introspected(ex, tmp_path):
    p = tmp_path / "query.yaml"
    p.write_text(re.sub(r"source: ossie_test\.\w+", "source: SELECT 1", FIXTURE.read_text()))
    with pytest.raises(PlanError, match="none of the model's datasets is readable"):
        Executor(ex.client, load_model(p))


def test_cli_query(ex, capsys):
    assert (
        main(
            [
                "query",
                str(FIXTURE),
                "-m",
                "revenue",
                "-d",
                "country.name",
                "--url",
                ex.client.url,
                "--json",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert '"revenue": 15.0' in out and '"name": null' in out
    assert main(["query", str(FIXTURE), "-m", "revenue", "--url", ex.client.url]) == 0
    assert capsys.readouterr().out == "revenue\n65.0\n"
    assert main(["sql", str(FIXTURE), "-m", "revenue", "--url", ex.client.url]) == 0
    assert "FINAL" in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(_CLICKHOUSE_REPEAT_SAFE | _CLICKHOUSE_REPEAT_SAFE_ANY_CASE))
def test_listed_clickhouse_aggregates_ignore_repeated_rows(clickhouse, name):
    """The planner's list, checked on the server: the same value over rows
    repeated up to five times as over the rows once."""
    args = "x, x % 7" if name.lower() in ("argmin", "argmax", "min_by", "max_by") else "x"
    agg = f"arraySort({name}({args}))" if name == "groupUniqArray" else f"{name}({args})"
    x = "5" if name == "singleValueOrNull" else "number % 13"
    once = f"SELECT {agg} FROM (SELECT {x} AS x FROM numbers(100))"
    repeated = (
        f"SELECT {agg} FROM (SELECT {x} AS x FROM numbers(100) "
        "ARRAY JOIN range(1 + number % 5) AS r)"
    )
    sql = f"SELECT toString(({once})) = toString(({repeated})), toString(({once}))"
    same, value = clickhouse.query(sql).result_rows[0]
    assert same and value not in ("", "\\N")


def test_server_functions_match_as_clickhouse_does():
    rows = [("toString", 0, 0), ("DAYOFWEEK", 1, 0), ("sum", 1, 1), ("uniqExact", 0, 1)]
    fns = ServerFunctions(rows, ["If", "OrNull", "Array"])
    found = ["toString", "DAYOFWEEK", "dayofweek", "DayOfWeek", "sum", "SUM", "sumIf"]
    found += ["sumIfOrNull", "SUMIf", "uniqExact", "uniqExactArray"]
    assert all(fns.has(n) for n in found)
    missing = ["tostring", "SUMIF", "sumif", "uniqexact", "If", "DAY_OF_WEEK", "toStringIf"]
    assert not any(fns.has(n) for n in missing)
    assert fns.problem(parse("SUM(x) + sumIf(y, z) + DAYOFWEEK(d)")) is None
    problem = fns.problem(parse("DAYOFWEEK(d) + NO_SUCH(x) + DAY_OF_WEEK(d)"))
    assert problem.startswith(
        "calls DAY_OF_WEEK(), a function this ClickHouse server does not have"
    )
    assert "did you mean: DAYOFWEEK" in problem


def test_functions_outside_the_spec_run_as_written_or_are_hidden(ex, tmp_path, capsys):
    # ClickHouse's own DAYOFWEEK, an aggregate with a combinator, and two the
    # server lacks: a misspelling and a combinator in the wrong case.
    text = FIXTURE.read_text().replace(
        "      - name: country_code\n",
        "      - name: weekday\n        expression: {dialects: [{dialect: ANSI_SQL, "
        "expression: DAYOFWEEK(toDate('2024-03-15'))}]}\n"
        "      - name: label\n        expression: {dialects: [{dialect: ANSI_SQL, "
        "expression: NO_SUCH_FUNCTION(order_id)}]}\n"
        "      - name: country_code\n",
        1,
    )
    text = text.replace(
        "metrics:\n",
        "metrics:\n  - name: big\n    expression: {dialects: [{dialect: ANSI_SQL, "
        'expression: "sumIf(orders.amount, orders.amount > 15)"}]}\n'
        "  - name: big_upper\n    expression: {dialects: [{dialect: ANSI_SQL, "
        'expression: "SUMIF(orders.amount, orders.amount > 15)"}]}\n',
        1,
    )
    p = tmp_path / "passthrough.yaml"
    p.write_text(text)
    other = Executor(ex.client, load_model(p))
    # as written: 2024-03-15 is a Friday, ISO 5; amounts above 15 are 20 and 30
    assert other.execute(Query(("big",), ("orders.weekday",))).rows == [(5, 50.0)]
    # the server says sumIf aggregates, so a filter on it goes to HAVING
    assert other.catalog.aggregate("sumIf") and not other.catalog.aggregate("toString")
    assert other.execute(Query(("big",), ("orders.weekday",), ("big > 40",))).rows == [(5, 50.0)]
    assert other.execute(Query(("big",), ("orders.weekday",), ("big > 60",))).rows == []
    assert other.check() == []
    # the others are hidden like untranslatable ones
    with pytest.raises(PlanError, match="unknown field"):
        other.planner.sql(Query(("revenue",), ("orders.label",)))
    with pytest.raises(PlanError, match="unknown metric"):
        other.planner.sql(Query(("big_upper",)))
    # only a server can tell: validate reports them with --url, and nothing without
    assert main(["validate", str(p)]) == 0
    assert main(["validate", str(p), "--url", ex.client.url]) == 1
    err = capsys.readouterr().err
    assert (
        "field orders.label: calls NO_SUCH_FUNCTION(), a function this ClickHouse server "
        "does not have" in err
    )
    assert "metric 'big_upper': calls SUMIF(), a function this ClickHouse server" in err
    assert "weekday" not in err and "'big'" not in err


def test_a_server_without_a_function_list_is_reported(ex, monkeypatch):
    """Without system.functions nothing checks functions outside the spec or
    types their aggregates: validate says so instead of passing."""
    monkeypatch.setattr(Executor, "_server_functions", lambda self: None)
    other = Executor(ex.client, load_model(FIXTURE))
    assert other.catalog.aggregate is None
    assert other.check() == [
        "system.functions cannot be read: functions outside the spec are not checked, "
        "and an aggregate among them (countIf, uniq) is planned as a plain function, "
        "so nothing refuses it over joined rows that repeat"
    ]


def test_untranslatable_expressions_are_hidden_and_reported(ex, tmp_path, capsys):
    # Snowflake's four-argument REGEXP_COUNT is outside the spec signature.
    bad = "REGEXP_COUNT(country_code, 'e', 1, 'i')"
    text = FIXTURE.read_text().replace(
        "      - name: country_code\n",
        f"      - name: e_count\n        expression: {{dialects: [{{dialect: ANSI_SQL, "
        f'expression: "{bad}"}}]}}\n      - name: country_code\n',
        1,
    )
    text = text.replace(
        "metrics:\n",
        "metrics:\n  - name: e_total\n    expression: {dialects: [{dialect: ANSI_SQL, "
        "expression: SUM(orders.e_count)}]}\n  - name: bad_total\n    expression: "
        f'{{dialects: [{{dialect: ANSI_SQL, expression: "SUM({bad})"}}]}}\n',
        1,
    )
    p = tmp_path / "untranslatable.yaml"
    p.write_text(text)
    other = Executor(ex.client, load_model(p))
    # the rest of the model still answers
    assert other.execute(Query(("revenue",))).rows == [(65.0,)]
    assert other.check() == []
    # the field, a metric over it and the bad metric are gone, like hidden objects
    with pytest.raises(PlanError, match="unknown field"):
        other.planner.sql(Query(("revenue",), ("orders.e_count",)))
    for name in ("e_total", "bad_total"):
        with pytest.raises(PlanError, match="unknown metric"):
            other.planner.sql(Query((name,)))
    # validate names each one, with or without a server
    for args in (["validate", str(p)], ["validate", str(p), "--url", ex.client.url]):
        assert main(args) == 1
        err = capsys.readouterr().err
        assert "field orders.e_count: cannot parse" in err
        assert "metric 'bad_total': cannot parse" in err
        assert err.count("expected REGEXP_COUNT(str, pattern), got 4") == 2

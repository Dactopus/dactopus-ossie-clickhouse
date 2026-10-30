"""Shared fixtures.

Unit tests run anywhere. Tests marked ``integration`` need a ClickHouse
reachable over HTTP and skip cleanly when there is none. Set
``OSSIE_CLICKHOUSE_URL`` (default ``http://127.0.0.1:8123``) to point
elsewhere.
"""

import os
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

# CI sets this: a missing server, database or privilege is then a failure, not a skip.
REQUIRED = bool(os.environ.get("OSSIE_CLICKHOUSE_REQUIRED"))


def unavailable(reason: str):
    (pytest.fail if REQUIRED else pytest.skip)(reason)


class FakeClickHouse:
    """Answers the Executor's introspection without a server. ``tables`` maps
    (database, name) to (engine, engine_full, (target_database, target_table),
    columns), as system.tables and system.columns would report them."""

    url = "http://fake:8123"

    def __init__(self, tables: dict, user: str = "u", database: str = "default"):
        self.tables, self.user, self.database = tables, user, database

    def query(self, sql, parameters=None):
        p = parameters or {}
        known = [k for k in p.get("pairs") or [p.get("t")] if k in self.tables]
        if "currentUser()" in sql:
            rows = [(self.user, [], self.database)]
        elif "system.columns" in sql:
            rows = [(*k, sorted(self.tables[k][3])) for k in known]
        elif "target_table" in sql:
            rows = [self.tables[k][2] for k in known]
        elif "system.tables" in sql:
            rows = [(*k, *self.tables[k][:2]) for k in known]
        elif "system.time_zones" in sql:
            rows = [(1,)]  # every zone is known
        else:
            rows = []
        return SimpleNamespace(result_rows=rows)


@pytest.fixture(scope="session")
def duck():
    """In-memory DuckDB connection: default expected values, not ground truth."""
    import duckdb

    con = duckdb.connect()
    yield con
    con.close()


URL = os.environ.get("OSSIE_CLICKHOUSE_URL", "http://127.0.0.1:8123")


def server_url(path: str = "", user: str | None = None) -> str:
    """The test server's URL with its user and password, on database ``path``.
    A client's ``url`` has neither, so a CLI or a client given it connects as
    ``default`` without a password. ``user`` (``name:password``) replaces them."""
    parts = urlsplit(URL)
    netloc = parts.netloc if user is None else f"{user}@{parts.netloc.rpartition('@')[2]}"
    return f"{parts.scheme}://{netloc}/{path or parts.path.lstrip('/')}"


def _literal(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def dictionary_credentials() -> str:
    """USER and PASSWORD for a dictionary's CLICKHOUSE source: the server loads it
    as ``default`` without a password unless told, whoever created it."""
    parts = urlsplit(URL)
    user, password = unquote(parts.username or "default"), unquote(parts.password or "")
    return f"USER {_literal(user)} PASSWORD {_literal(password)}"


def connect(database: str | None = None):
    """A client of the test server as its user; the scheme decides TLS, as in
    `executor.connect` (#60)."""
    import clickhouse_connect

    extra = {"database": database} if database else {}
    return clickhouse_connect.get_client(
        dsn=URL, interface=URL.split("://", 1)[0], connect_timeout=2, **extra
    )


@pytest.fixture(scope="session")
def clickhouse():
    """clickhouse-connect client, or skip the test when no server answers."""
    try:
        client = connect()
        client.command("SELECT 1")
    except Exception as e:
        unavailable(f"no ClickHouse at {URL}: {e}")
    yield client
    client.close()


@pytest.fixture(scope="session")
def dactopus(clickhouse):
    """A client on the database dactopus, once dactopus-data-models has loaded its
    GA4 sample there."""
    if not clickhouse.query("EXISTS TABLE dactopus.purchases").result_rows[0][0]:
        pytest.skip("GA4 sample not loaded into the database dactopus (see dactopus-data-models)")
    client = connect("dactopus")
    yield client
    client.close()


@pytest.fixture(scope="session")
def tpcds(clickhouse):
    """The ClickHouse client, once the TPC-DS reference database is loaded."""
    if not clickhouse.query("EXISTS DATABASE tpcds").result_rows[0][0]:
        unavailable("no tpcds database loaded (see CONTRIBUTING.md)")
    return clickhouse


def pytest_collection_modifyitems(items):
    """Every test using the ``clickhouse`` fixture is an integration test."""
    for item in items:
        if "clickhouse" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)

"""Run semantic queries against ClickHouse.

Learns what it needs from the server (`system.tables`, `system.columns`,
`system.dictionaries`) rather than from the user, and lets a dataset's
`custom_extensions` entry with `vendor_name: CLICKHOUSE` override it.
"""

from __future__ import annotations

import datetime as dt
import difflib
import math
import os
import re
from dataclasses import dataclass
from functools import partial
from urllib.parse import urlsplit

import clickhouse_connect
from ossie import OssieDataset, OssieDocument
from sqlglot import exp

from dactopus_ossie_clickhouse.access import Hidden, Policy, restrict
from dactopus_ossie_clickhouse.model import overrides, time_zone
from dactopus_ossie_clickhouse.planner import (
    Catalog,
    PlanError,
    Planner,
    Query,
    TableInfo,
    source_table,
    unanswerable_metrics,
)
from dactopus_ossie_clickhouse.translate import (
    parse,
    passthrough_functions,
    pick_expression,
    to_clickhouse,
    untranslatable,
)

DEFAULT_URL = "http://127.0.0.1:8123"
TABLES = "SELECT database, name, engine, engine_full FROM system.tables"
# engine_full as ClickHouse writes it back: arguments as quoted strings.
_STRING = r"'((?:[^'\\]|\\.)*)'"
_DISTRIBUTED = re.compile(rf"Distributed\({_STRING}, {_STRING}, {_STRING}")
_MERGE = re.compile(rf"Merge\((REGEXP\()?{_STRING}\)?, {_STRING}")


def connect(url: str | None = None):
    url = url or os.environ.get("OSSIE_CLICKHOUSE_URL", DEFAULT_URL)
    # clickhouse-connect ignores a DSN's scheme and turns TLS on by the port (443
    # or 8443), so https on any other port went out in the clear (#60). The scheme
    # passed as `interface` decides it on every port, both ways. A URL without a host
    # would connect to localhost. The message quotes nothing: `localhost:8123` parses
    # with `localhost` as its scheme, and `user:pw@h` with the user name.
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(
            "ClickHouse URL must be http://host or https://host, with an optional port"
        )
    # No session: nothing here needs one, and a session serializes queries, so
    # concurrent MCP tool calls on one client would fail.
    return clickhouse_connect.get_client(dsn=url, interface=scheme, autogenerate_session_id=False)


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    sql: str
    types: list[str]  # ClickHouse type of each column

    def shown_rows(self) -> list[list]:
        """Rows as the CLI and the MCP server show them: a Time or DateTime as
        ClickHouse prints it, other values unchanged."""
        return [
            [_as_printed(v, t) for v, t in zip(row, self.types, strict=True)] for row in self.rows
        ]


class ServerFunctions:
    """What system.functions says the server has, matched as ClickHouse matches
    a call: the name as written, or in any case when the server marks it
    case-insensitive (DAYOFWEEK, any_value), and for an aggregate with any
    combinators after it, each as written: sumIf and uniqExactOrNull, not SUMIF.
    User-defined functions are listed too."""

    def __init__(self, rows: list[tuple], combinators: list[str]):
        self.names = {name for name, _, _ in rows}
        self.folded = {name.lower() for name, insensitive, _ in rows if insensitive}
        self.aggregates = {name for name, _, aggregate in rows if aggregate}
        self.folded_aggregates = {
            name.lower() for name, insensitive, aggregate in rows if insensitive and aggregate
        }
        self.combinators = sorted(combinators, key=len, reverse=True)
        self.by_lower = {n.lower(): n for n in sorted(self.names)}  # for suggestions

    def has(self, name: str, aggregate: bool = False) -> bool:
        names, folded = (
            (self.aggregates, self.folded_aggregates) if aggregate else (self.names, self.folded)
        )
        if name in names or name.lower() in folded:
            return True
        return any(
            len(name) > len(c) and name.endswith(c) and self.has(name[: -len(c)], aggregate=True)
            for c in self.combinators
        )

    def problem(self, tree: exp.Expression) -> str | None:
        """Why a parsed expression cannot run here: the first function it calls
        that the server lacks, with the nearest names it has."""
        for name in sorted(passthrough_functions(tree)):
            if not self.has(name):
                close = difflib.get_close_matches(name.lower(), self.by_lower, n=3, cutoff=0.6)
                hint = (
                    f" (did you mean: {', '.join(self.by_lower[c] for c in close)}?)"
                    if close
                    else ""
                )
                return f"calls {name}(), a function this ClickHouse server does not have{hint}"
        return None


class Executor:
    """Runs as whoever the client is connected as; sees only what that user may read."""

    def __init__(self, client, model: OssieDocument, policy: Policy | None = None):
        self.client = client
        self.full_model = model
        self.untranslatable_fields, self.untranslatable_metrics = untranslatable(model)
        # A source without a database reads currentDatabase(): the URL's database,
        # else the user's DEFAULT DATABASE. Introspection must look in the same place.
        self.user, self.roles, self.database = self.client.query(
            "SELECT currentUser(), enabledRoles(), currentDatabase()"
        ).result_rows[0]
        self.functions = self._server_functions()
        self.catalog = self.introspect()
        self._hide_unknown_functions()
        # The databases the model reads, not the connection's: no credentials, and
        # not joined to the URL, whose path may be a proxy's.
        self.location = f"{self.client.url} (database {', '.join(self.databases) or self.database})"
        # Metrics no question can answer are hidden too: an agent would only
        # spend a turn on each. validate reports them.
        hidden = self._hidden_by_grants() | Hidden(
            fields=frozenset(self.untranslatable_fields),
            metrics=frozenset(self.untranslatable_metrics)
            | frozenset(unanswerable_metrics(model, self.catalog)),
        )
        if policy:
            hidden |= policy.hidden_for(self.user, list(self.roles))
        self.model = restrict(model, hidden, reader=f"by {self.user!r} on {self.location}")
        self.planner = Planner(self.model, self.catalog)

    def _hidden_by_grants(self) -> Hidden:
        """ClickHouse lists in system.tables and system.columns only what the
        connected user may read, so anything the model names and the catalog
        lacks is invisible to this user."""
        datasets, fields = set(), set()
        for ds in self.full_model.datasets:
            info = self.catalog.get(ds.name)
            if info is None:
                datasets.add(ds.name)
                continue
            for f, tree in self._parsed_fields(ds):
                cols = {c.name for c in tree.find_all(exp.Column)}
                if not cols <= info.columns:
                    fields.add(f"{ds.name}.{f.name}")
        # A relationship goes with the fields it joins on (restrict).
        return Hidden(frozenset(datasets), frozenset(fields))

    def _parsed_fields(self, ds: OssieDataset):
        """Each field of ``ds`` that translates, with its parsed expression; the
        others are hidden and reported by untranslatable()."""
        for f in ds.fields or []:
            if f"{ds.name}.{f.name}" not in self.untranslatable_fields:
                yield f, parse(pick_expression(f.expression))

    def _server_functions(self) -> ServerFunctions | None:
        """What system.functions lists; None when it cannot be read or is empty."""
        try:
            rows = self.client.query(
                "SELECT name, case_insensitive, is_aggregate FROM system.functions"
            ).result_rows
            combinators = self.client.query(
                "SELECT name FROM system.aggregate_function_combinators"
            ).result_rows
        except Exception:
            return None
        return ServerFunctions(rows, [c for (c,) in combinators]) if rows else None

    def _hide_unknown_functions(self) -> None:
        """A field or metric calling a function this server does not have is
        hidden like one that does not translate, and reported the same way.

        Functions outside the spec reach ClickHouse as written (translate.py),
        so only the server can say whether they exist. Without system.functions
        nothing is checked."""
        if self.functions is None:
            return
        for ds in self.full_model.datasets:
            for f, tree in list(self._parsed_fields(ds)):
                if problem := self.functions.problem(tree):
                    self.untranslatable_fields[f"{ds.name}.{f.name}"] = problem
        for m in self.full_model.metrics or []:
            if m.name not in self.untranslatable_metrics:
                if problem := self.functions.problem(parse(pick_expression(m.expression))):
                    self.untranslatable_metrics[m.name] = problem

    # --- introspection ------------------------------------------------------

    def introspect(self) -> Catalog:
        tables: dict[
            tuple[str, str], list[OssieDataset]
        ] = {}  # several datasets may share a source
        for ds in self.full_model.datasets:
            try:
                t = source_table(ds.source)
            except PlanError:
                continue
            tables.setdefault((t.db or self.database, t.name), []).append(ds)
        self.databases = sorted({db for db, _ in tables})
        # Which functions outside the spec are aggregates, for the planner's checks.
        aggregate = partial(self.functions.has, aggregate=True) if self.functions else None
        if not tables:
            return Catalog(aggregate=aggregate)
        params = {"pairs": list(tables)}  # bound server-side: names never enter SQL text
        pairs = "{pairs:Array(Tuple(String, String))}"
        rows = self.client.query(
            f"{TABLES} WHERE (database, name) IN {pairs}", parameters=params
        ).result_rows
        cols = self.client.query(
            "SELECT database, table, groupArray(name) FROM system.columns "
            f"WHERE (database, table) IN {pairs} GROUP BY database, table",
            parameters=params,
        ).result_rows
        columns = {(db, t): frozenset(c) for db, t, c in cols}
        try:
            dicts = self.client.query(
                "SELECT database, name, attribute.names FROM system.dictionaries "
                f"WHERE (database, name) IN {pairs}",
                parameters=params,
            ).result_rows
        except Exception:  # needs an explicit grant; without it dictionaries are joined as tables
            dicts = []
        attributes = {(db, n): set(a) for db, n, a in dicts}

        catalog = Catalog(aggregate=aggregate)
        self.undecided: set[str] = set()  # datasets whose need for FINAL this user cannot see
        for db, name, engine, full in rows:
            key = None
            # dictGet* splits its name on dots and ignores quoting (string and
            # identifier forms, 26.9): a dotted name is joined as a table.
            if engine == "Dictionary" and "." not in db + name:
                keys = columns[(db, name)] - attributes.get((db, name), set())
                key = next(iter(keys)) if len(keys) == 1 else None
            versions = self._keeps_versions((db, name), engine, full)
            for ds in tables[(db, name)]:
                mode = overrides(ds).get("dedup")
                if versions is None and mode is None:
                    self.undecided.add(ds.name)
                catalog[ds.name] = TableInfo(
                    engine=engine,
                    columns=columns.get((db, name), frozenset()),
                    dedup=mode == "final" or (mode != "none" and bool(versions)),
                    dictionary_key=key,
                )
        return catalog

    def _keeps_versions(self, table, engine, full, depth=0) -> bool | None:
        """Whether reading a table needs FINAL: it is a ReplacingMergeTree
        (Replicated* on clusters, Shared* on ClickHouse Cloud), or reads one as
        a Distributed table, a materialized view or a Merge table. FINAL on any
        of them reaches the stored rows; on one over a plain MergeTree it is an
        error. None when the table read is hidden from this user or unknown."""
        if engine.endswith("ReplacingMergeTree"):
            return True
        if depth == 3:  # ponytail: wrappers nest this deep at most, and a loop stops here
            return None
        if engine == "Distributed":
            if not (m := _DISTRIBUTED.match(full)):
                return None
            target = tuple(_unquote(p) for p in m.groups()[1:])
        elif engine == "Merge" and (m := _MERGE.match(full)):
            # FINAL on a Merge table reaches every table it reads that supports
            # it and skips the rest; a table hidden from this user is not read.
            regex, db, name = m.groups()
            where = "match(database, {db:String})" if regex else "database = {db:String}"
            return any(
                self._keeps_versions(row[:2], *row[2:], depth + 1)
                for row in self.client.query(
                    f"{TABLES} WHERE {where} AND match(name, {{name:String}}) "
                    "AND (database, name) != {table:Tuple(String, String)}",
                    parameters={"db": _unquote(db), "name": _unquote(name), "table": table},
                ).result_rows
            )
        elif engine == "MaterializedView":
            try:
                target = self.client.query(
                    "SELECT target_database, target_table FROM system.tables "
                    "WHERE (database, name) = {t:Tuple(String, String)}",
                    parameters={"t": table},
                ).result_rows[0]
            except Exception:  # added in ClickHouse 26.6: undecided before it
                return None
        else:
            return False
        rows = self.client.query(
            TABLES + " WHERE (database, name) = {t:Tuple(String, String)}", parameters={"t": target}
        ).result_rows
        if not rows:
            return None
        return self._keeps_versions(rows[0][:2], *rows[0][2:], depth + 1)

    # --- checks -------------------------------------------------------------

    def check(self) -> list[str]:
        """Problems that only the database can reveal: missing sources and columns,
        a time zone the server lacks, and a server whose functions cannot be listed.

        Run as a user who can read everything; for a restricted user, missing
        means hidden, not broken."""
        problems = []
        for ds in self.full_model.datasets:
            info = self.catalog.get(ds.name)
            if info is None:
                try:
                    source_table(ds.source)
                    why = f"source {ds.source!r} not found in ClickHouse"
                except PlanError as e:
                    why = str(e)
                problems.append(f"dataset {ds.name!r}: {why}")
                continue
            if ds.name in self.undecided:
                problems.append(
                    f"dataset {ds.name!r}: {ds.source} reads a table this user cannot see "
                    "(or, before ClickHouse 26.6, a view's target), so whether it keeps row "
                    "versions is unknown and it is read without "
                    'FINAL; set "dedup" to "final" or "none" (see docs/model-authoring.md)'
                )
            # Keys and relationships name fields (load_model checks), so their
            # columns are the fields' own.
            needed: dict[str, str] = {}
            for f, tree in self._parsed_fields(ds):
                for c in tree.find_all(exp.Column):
                    if not c.table:
                        needed[c.name] = f"field {f.name!r}"
            for col, where in needed.items():
                if col not in info.columns:
                    problems.append(
                        f"dataset {ds.name!r}: column {col!r} ({where}) not in {ds.source}"
                    )
        zone = time_zone(self.full_model)
        try:
            known = self.client.query(
                "SELECT count() FROM system.time_zones WHERE time_zone = {zone:String}",
                parameters={"zone": zone},
            ).result_rows[0][0]
        except Exception:  # unreadable: a question using a zone it lacks fails naming it
            known = True
        if not known:  # Python's zone database and the server's may differ
            problems.append(f"model: time zone {zone!r} is not in system.time_zones")
        if self.functions is None:
            problems.append(
                "system.functions cannot be read: functions outside the spec are not "
                "checked, and an aggregate among them that SQLGlot's list of ClickHouse "
                "aggregates lacks (medianExact, a user-defined one) is planned as a plain "
                "function, so nothing refuses it over joined rows that repeat"
            )
        return problems

    # --- execution ----------------------------------------------------------

    def clock(self) -> dict[str, str]:
        """Today and the current time in the model's zone by the server's clock: what
        CURRENT_DATE and CURRENT_TIMESTAMP are in a question asked now. The time is
        shown as in results, with its offset from UTC."""
        zone = time_zone(self.model)
        today, now = (to_clickhouse(e, zone) for e in ("CURRENT_DATE", "CURRENT_TIMESTAMP"))
        res = self.client.query(f"SELECT {today}, {now}")  # now() is one instant per query
        day, at = res.result_rows[0]
        return {"today": day.isoformat(), "now": _as_printed(at, res.column_types[1].name)}

    def execute(self, q: Query) -> Result:
        sql = self.planner.sql(q)
        res = self.client.query(sql)
        rows = [tuple(_non_finite_to_none(v) for v in row) for row in res.result_rows]
        return Result(list(res.column_names), rows, sql, [t.name for t in res.column_types])


def _unquote(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _non_finite_to_none(v):
    """ClickHouse yields nan and inf where SQL yields NULL; neither is JSON."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _as_printed(v, type_name: str):
    """A Time, Time64, DateTime or DateTime64 value as ClickHouse's toString prints it,
    a DateTime with its offset from UTC after it.

    clickhouse-connect reads Time as a timedelta, whose str() of -01:02:03 is
    "-1 day, 22:57:57" and pydantic's "-PT1H2M3S"; datetimes get a T over MCP and
    a space in the CLI. ClickHouse's own text reads the same everywhere and goes
    back into a filter, the offset included. Fractions show the column's digits;
    clickhouse-connect keeps microseconds, so digits 7 to 9 are 0."""
    while type_name.startswith(("Nullable(", "LowCardinality(")):
        type_name = type_name[type_name.index("(") + 1 : -1]
    kind, _, args = type_name.partition("(")
    digits = int(args.split(",")[0].rstrip(")")) if kind in ("Time64", "DateTime64") else 0
    offset = ""
    if isinstance(v, dt.timedelta) and kind in ("Time", "Time64"):
        us = v // dt.timedelta(microseconds=1)
        s, fraction = divmod(abs(us), 1_000_000)
        text = f"{'-' if us < 0 else ''}{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
    elif isinstance(v, dt.datetime) and kind in ("DateTime", "DateTime64"):
        # Already the wall time of the column's time zone, which toString prints.
        # Without the offset a reader cannot tell which zone that is: a column
        # without one shows the server's. clickhouse-connect leaves a value naive
        # only in UTC.
        text, fraction = v.strftime("%Y-%m-%d %H:%M:%S"), v.microsecond
        offset = _offset(v.utcoffset() or dt.timedelta(0))
    else:
        return v
    text = f"{text}.{fraction:06d}000"[: len(text) + 1 + digits] if digits else text
    return text + offset


def _offset(delta: dt.timedelta) -> str:
    """+05:30 for 5.5 hours east of UTC; seconds only when there are some (local mean
    time before a zone adopted standard time: +07:07:12)."""
    s = int(delta.total_seconds())
    sign, s = "-" if s < 0 else "+", abs(s)
    text = f"{sign}{s // 3600:02d}:{s // 60 % 60:02d}"
    return text + (f":{s % 60:02d}" if s % 60 else "")

# dactopus-ossie-clickhouse

[![CI](https://github.com/Dactopus/dactopus-ossie-clickhouse/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Dactopus/dactopus-ossie-clickhouse/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/Dactopus/dactopus-ossie-clickhouse)](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/dactopus-ossie-clickhouse)](https://pypi.org/project/dactopus-ossie-clickhouse/)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FDactopus%2Fdactopus-ossie-clickhouse%2Fmain%2Fpyproject.toml)](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/pyproject.toml)

An implementation of the [Apache Ossie](https://github.com/apache/ossie)
semantic model standard for [ClickHouse](https://clickhouse.com/docs). It reads an Ossie description of
your data, answers questions asked in business terms (metrics, dimensions,
filters) with one ClickHouse query, and serves the description and the
queries to AI agents over [MCP](https://modelcontextprotocol.io), the
Model Context Protocol.

## Why

A company connects an AI assistant to its database and the assistant gets
things wrong: hundreds of tables with no explanations, three columns with
the same name, a different "revenue" in every department. Apache Ossie
fixes this at the level of description: one open, portable definition of
what tables and fields mean, how they relate and how metrics are computed.
dactopus-ossie-clickhouse executes those descriptions on ClickHouse, which the
standard does not cover itself, and handles the ClickHouse specifics
(tables that keep change history, reference data in dictionaries, join
resolution) so that neither the model author nor the agent has to.

## Install

Python 3.11 or later:

```bash
pip install "dactopus-ossie-clickhouse[mcp]"
```

Leave out `[mcp]` to skip the MCP server and its SDK; the library and the
CLI work without them.

## Quick start

Point it at a model and a ClickHouse. The
[TPC-DS reference model](https://github.com/apache/ossie/blob/main/examples/tpcds_semantic_model.yaml)
from the Ossie repository is in [tests/fixtures/tpcds.yaml](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/tests/fixtures/tpcds.yaml).
For a store or a GA4 property,
[dactopus-data-models](https://github.com/Dactopus/dactopus-data-models)
ships Ossie models for orders, products, customers and sessions, with the
dbt packages that build their tables from Shopify, WooCommerce and GA4.
ClickHouse credentials go in the URL or in `OSSIE_CLICKHOUSE_URL`
(default `http://127.0.0.1:8123`). `https://` connects with TLS on any
port, `http://` without it; no other scheme is accepted. A database in the URL path
(`.../analytics`) is where a model's sources without a database are read.
`--time-zone` or `OSSIE_CLICKHOUSE_TIME_ZONE` names the zone of today and
of times without an offset when the deployment's differs from the model's
([Time zone](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/model-authoring.md#time-zone)).

```bash
dactopus-ossie-clickhouse validate model.yaml --url http://user:password@host:8123
dactopus-ossie-clickhouse sql model.yaml -m total_sales -d item.i_brand -f "date_dim.d_year = 1998"
dactopus-ossie-clickhouse query model.yaml -m total_sales -d item.i_brand -f "total_sales > 1000000" --json
dactopus-ossie-clickhouse serve model.yaml        # MCP over stdio, see docs/mcp-setup.md
```

From Python:

```python
from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.executor import Executor, connect
from dactopus_ossie_clickhouse.planner import Query

ex = Executor(connect("http://user:password@host:8123"), load_model("model.yaml"))
r = ex.execute(Query(metrics=("total_sales",), dimensions=("item.i_brand",)))
print(r.sql, r.columns, r.rows[:3])
```

That is the Python API: `load_model` (its `time_zone` argument is the
CLI's `--time-zone`), `executor.connect`,
`executor.Executor` and `planner.Query`; a `PlanError` names the nearest
known object. `Executor` takes the document `load_model` returns, or one
built with `dactopus_ossie_clickhouse._vendor.ossie`, never with an
installed `ossie`. The other modules are internal.

## How a question becomes SQL

<a href="https://dactopus.github.io/dactopus-ossie-clickhouse/"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Dactopus/dactopus-ossie-clickhouse/main/docs/architecture.svg#dark"><img src="https://raw.githubusercontent.com/Dactopus/dactopus-ossie-clickhouse/main/docs/architecture.svg#light" width="680" alt="How dactopus-ossie-clickhouse answers a question: an AI agent asks through the MCP server or the CLI; the Ossie model is cut down to what the connected ClickHouse user may read, as system tables show, and a policy file may hide more; the planner and translator write one SELECT or refuse; the executor runs it in ClickHouse as that user."></picture></a>

Click the diagram for the [interactive version](https://dactopus.github.io/dactopus-ossie-clickhouse/):
one question from an agent followed to its SQL, line by line, and two
questions it refuses.

- Datasets come from `source`, joins from `relationships`. One root
  dataset, `LEFT JOIN` to the dimensions the question touches, only along
  relationships whose `to_columns` cover a primary or unique key of the
  target (many-to-one). ClickHouse does not enforce that a key is unique,
  so when an aggregate counts rows, each joined dataset is read as a
  derived table of the distinct values the question asks of it, and a row
  of the root counts once in each group it matches. `SETTINGS`
  [`join_use_nulls = 1`](https://clickhouse.com/docs/operations/settings/settings#join_use_nulls)
  so unmatched rows get NULL, not ClickHouse defaults, and
  [`aggregate_functions_null_for_empty = 1`](https://clickhouse.com/docs/operations/settings/settings#aggregate_functions_null_for_empty)
  so a sum over no rows is NULL, as in SQL, not 0, and
  [`transform_null_in = 0`](https://clickhouse.com/docs/operations/settings/settings#transform_null_in)
  so a NULL matches nothing in `IN`.
- A filter on rows that reference the root, such as sessions with a
  purchase of some value, is a semi-join: each session with at least one
  such purchase counts once. So is a filter on rows that reference what
  the root references, such as order lines whose product is in a
  category, and, with the order lines named in a filter, orders with a
  line in a category
  ([semi-joins](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/design.md#semi-joins-for-filters-on-the-many-side)).
- Metrics of different facts, such as revenue and returns by region, are
  each computed from their own root and merged on the dimensions
  ([stitching](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/design.md#stitch-metrics-of-several-facts)): every
  region of either fact appears, and a fact without rows in a region
  gives NULL for a sum and 0 for a count.
- A model's metric over several datasets, such as sales per customer,
  `SUM(orders.amount) / COUNT(DISTINCT customers.id)`, computes each
  aggregate over its own dataset's rows, as the Layer 3 spec requires, and
  divides the results: the customers are all customers, with an order or
  without ([components](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/design.md#evaluate-each-aggregate-over-its-own-dataset)).
- A metric declared on a dataset (`datasets[].metrics`) is asked for as
  `dataset.metric` and computed over that dataset's rows, `COUNT(*)`
  included ([model authoring](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/model-authoring.md#metrics-on-a-dataset)).
- Filters over fields go to `WHERE`; filters that name a metric or contain
  an aggregate (`total_sales > 1000000`) go to `HAVING`, and may only use
  the question's own dimensions besides aggregates. A window metric
  (`RANK() OVER ...`) cannot be filtered: select it and filter the rows.
  A filter is one expression, never a statement, and its functions reach
  ClickHouse as written: what a caller may run there is decided by
  ClickHouse grants and quotas, not by this library.
- Tables with a [`ReplacingMergeTree`](https://clickhouse.com/docs/engines/table-engines/mergetree-family/replacingmergetree)
  engine, replicated or on ClickHouse Cloud, and `Distributed` tables,
  materialized views and `Merge` tables over them, are read with [`FINAL`](https://clickhouse.com/docs/sql-reference/statements/select/from#final-modifier);
  single-key [dictionaries](https://clickhouse.com/docs/sql-reference/dictionaries) are read with
  `dictGetOrNull` instead of a join. Both are learned from `system.tables`
  and `system.dictionaries`, not configured.
- Expressions in `sql` (OSSIE_SQL_2026), or in the `OSSIE_SQL_2026` or
  `ANSI_SQL` dialect, are translated on the
  [SQLGlot](https://github.com/tobymao/sqlglot) AST; the spec's function
  catalog is mapped to ClickHouse equivalents and checked by value against
  [DuckDB](https://duckdb.org) and the spec text. `AGG`, which the spec
  reserves, is refused. Any other function
  reaches ClickHouse as written (`toString`, `sumIf`, `DAYOFWEEK`, a
  user-defined function), and `validate --url` checks the server has it.
- Rows come sorted by the first metric, descending, unless `order_by` says
  otherwise, so the top rows come first and nobody has to rank them by hand.
  Rows without a value (NULL) come last in either direction. Ties are
  broken by the dimensions, ascending, so a `limit` keeps the same rows on
  every run.
- Names resolve case-insensitively; SQL uses the physical names as the
  model writes them. The same question and model always give the same SQL.
- Errors name the nearest known metric, field or dataset, so an agent can
  recover from a typo without help.

## CLI

| Command | What it does |
| --- | --- |
| `validate <model>... [--url]` | Check the model; with `--url`, also that every source, column and function outside the spec exists in ClickHouse. |
| `sql <model>... ...` | Print the SQL for a question without running it. |
| `query <model>... ... [--json]` | Run it; TSV or JSON rows. |
| `serve <model>...` | MCP server over stdio (needs the `[mcp]` extra). |

Question options for `sql` and `query`: `-m metric` (repeatable;
`dataset.metric` for one declared on a dataset),
`-d dataset.field` (repeatable), `-f condition` (repeatable; a metric
name or an aggregate means `HAVING`), `-o "name [desc]"`, `-l limit`.
`--url` and `--policy` apply to anything that talks to ClickHouse.
`<model>...` is one Ossie file or several read as one model, such as one
file per subject area ([docs/model-authoring.md](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/model-authoring.md#several-files)).

## Documentation

- [Model authoring for ClickHouse](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/model-authoring.md): sources,
  keys, expressions, deduplication, dictionaries, validation.
- [Access control](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/access-control.md): ClickHouse users, roles and
  row policies decide; the policy file hides more.
- [MCP setup](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/mcp-setup.md): Claude Desktop, Claude Code and other
  stdio MCP clients.
- [Design decisions](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/design.md): what was measured or evaluated, and
  the remote server design that waits for demand.
- [Changelog](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/CHANGELOG.md) and [contributing](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/CONTRIBUTING.md).

## What it is not

No chat, no charts, no user interface: it provides access and
understanding, the interface is someone else's job. No data loading: the
data must already be in ClickHouse.

## Limits

- One root dataset per group of metrics, which reaches every dataset it
  touches through many-to-one relationships, along one path to each. Two
  paths to the same dataset are refused as ambiguous. Metrics of
  different facts are stitched, so each dimension and row filter must be
  reachable from each fact, and from the dataset of each aggregate of a
  metric over several datasets. A metric of one dataset by a field of a
  dataset that reaches it counts each of its rows once per group.
- Metrics are computed over the root's rows. A question whose metric
  would count a joined dataset's rows repeatedly is answered over that
  dataset's own rows, each once, or refused with a message; see [model
  authoring](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/model-authoring.md#keys-decide-which-joins-are-allowed).
- Joins are many-to-one only, inferred from primary and unique keys.
- No time grain and no derived-dimension syntax: declare a field per
  grain or band (see [model authoring](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/model-authoring.md)). The
  spec has no such attributes yet, and this project does not invent them.
- A metric cannot reference another metric by name; repeat the expression.

## Status

Version 0.x. The supported Ossie schema version is pinned (`0.3.0.dev0`)
and checked on load, because the standard is still moving. Until the
major version is 1, a minor release may change the Python API; the CLI
and the MCP tools are the stable surface. Tested against the TPC-DS
reference model and the spec's query compliance cases on ClickHouse
26.x, and weekly on the newest SQLGlot and on Ossie's `main`.

What is designed and waits for a request, such as a remote multi-user
MCP server, is in the
[issue tracker](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues?q=label%3Aneeds-demand)
under `needs-demand`; ask there.

[`research/`](https://github.com/Dactopus/dactopus-ossie-clickhouse/tree/main/research) holds the experiments behind some decisions in this
code and the data we share with the Apache Ossie community; nothing there is
part of the library.

## License

Apache License 2.0.

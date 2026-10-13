# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project

dactopus-ossie-clickhouse is an open-source implementation of the
[Apache Ossie](https://github.com/apache/ossie) semantic model standard for
ClickHouse. It reads an Ossie data description, translates metric definitions
into ClickHouse SQL, answers questions posed in business terms (metrics,
dimensions, filters), and serves the description and queries to AI agents over
MCP. See [README.md](README.md) for the full product description.

Licensed under Apache 2.0.

## Language

All text in this repository is in English only: code identifiers, comments,
docstrings, commit messages, documentation, issues, and test names. Do not
introduce text in any other language, even in examples or fixtures.

## Scope

In scope:

- Reading and validating Ossie descriptions.
- Translating Ossie expressions into ClickHouse SQL, including ClickHouse
  specifics the user must not have to think about: deduplication of tables that
  keep change history, reference data stored in dictionaries, join resolution.
- Executing queries against any ClickHouse (self-hosted or cloud).
- Serving the model and accepting questions over MCP, with access controlled
  by a local configuration.

Out of scope, do not build:

- Chat, charts, or any user interface.
- Data loading, ETL, or warehouse features. Data is already in ClickHouse.
- Dependencies beyond ClickHouse and the Ossie standard where avoidable.

## Design principles

- The Ossie description is the single source of truth. Never hardcode metric
  or table semantics that belong in the model.
- Follow the standard's semantics exactly. When ClickHouse behaviour differs
  from the standard's assumptions, handle it in the translator, not by
  changing what the standard means.
- Every answer must be reproducible: the same question and the same model
  yield the same SQL.
- Keep the public surface small. Prefer one obvious way to do each thing.

## Stack

Python library with a CLI entry point. The parts that constrain code:

- **SQLGlot** for expression translation, parsing with its default dialect
  until the upstream Ossie dialect (apache/ossie PR #222) lands. Models today
  carry `ANSI_SQL` expressions only; prefer `OSSIE_SQL_2026` when present.
- **clickhouse-connect** over HTTP for execution.
- **MCP SDK** only behind the `[mcp]` extra. The core library must import
  without it.
- **apache-ossie** package (from the Ossie repository, git dependency until
  published) for loading and validating models. Do not write a model loader:
  a file is read with `OssieDocument.from_ossie_yaml` or `from_ossie_json`,
  which also refuse a null property and a missing `version`;
  `model_validate` is for a document built in Python, such as the trimmed
  model in `access.py`.
- Supported Ossie schema version is pinned (`0.2.0.dev0`) and checked
  explicitly.

## Architecture rules

- Layering: model loader -> translator -> planner -> executor -> MCP adapter.
  Lower layers never import higher ones.
- The MCP server (`mcp_server.py`, `[mcp]` extra) is a thin adapter over
  the library. No logic lives only in the MCP layer; each tool is one
  library call, and errors go back as results with an `error` field, never
  as exceptions, so suggestions reach the agent. Keep `client_factory` as
  the only place that decides which ClickHouse connection a caller gets.
- Access control is ClickHouse's. Queries run as the connected user; the
  model is trimmed (`access.py`) to the sources and columns that user can
  read, as revealed by `system.tables` and `system.columns`. A hidden object
  must never appear in SQL, errors or suggestions. The optional policy file
  only hides more; it never grants. Nothing in this project authenticates.
  A field or metric whose expression does not translate is hidden the same
  way (nothing proves it reads only visible columns) and reported by
  `validate`; so is a metric the planner refuses in every question.
- ClickHouse specifics come from introspecting `system.tables`,
  `system.columns` and `system.dictionaries`, not from asking the user.
  `ReplacingMergeTree` engines, `Replicated` and Cloud's `Shared` too, are
  read with `FINAL` (measured faster than `argMax`), and so is a
  `Distributed` table, materialized view or `Merge` table that reads one;
  `FINAL` on one that reads a plain `MergeTree` is an error. When the table
  read is hidden from the user, `validate` reports it and no `FINAL` is
  added. Single-key dictionaries with `dictGetOrNull` instead of a join.
  Overrides go in the model's `custom_extensions` under
  `vendor_name: CLICKHOUSE` as JSON, currently `{"dedup": "none"}` or
  `{"dedup": "final"}`.
- The planner never talks to the database; it takes an optional `Catalog`
  the executor built. Keep that boundary.
- Query planning is deterministic: datasets from `source`, joins from
  `relationships`, one `SELECT` per question. Joins are `LEFT JOIN` from
  one root dataset, along the one path of relationships to each dataset
  the question touches (two paths are ambiguous, #246 §6.9), only along
  relationships whose `to_columns` cover a primary or unique key of the
  target. Generated SQL carries
  `SETTINGS join_use_nulls = 1` so unmatched rows get NULL, not defaults,
  and `aggregate_functions_null_for_empty = 1` so an aggregate over no
  rows is NULL, not 0 (counts stay 0).
  Metrics run over the root's rows, so the planner refuses an aggregate
  that reads only joined datasets unless repeats cannot change it or the
  join is on a key of the root, an aggregate expression that reads no root
  column at all, and a metric whose `COUNT(*)` no other aggregate ties to
  one dataset (`docs/design.md`). Metrics no one root answers together
  are stitched: each group of metrics planned from its own root, merged
  with `UNION ALL` and `GROUP BY` on the dimensions (#246 section 6.6); a
  metric missing from a group is its value over no rows.
- Name resolution is case-insensitive (spec rule); emitted SQL uses physical
  names exactly as the model writes them (ClickHouse is case-sensitive).
- Planner errors name the nearest known object; agents recover from that.

## Testing

Translation tests compare values, not only that ClickHouse accepts the SQL:
a translation that runs and returns the wrong value is the failure mode to
guard against. Expected values come from DuckDB by default, as a second
opinion close to the spec's Postgres-like semantics, not as ground truth.
Where DuckDB lacks a function or disagrees with the spec, the expected value
is written by hand in the test with a reference to the spec section. The
spec text wins over any engine. Replace hand-written expectations with the
Ossie compliance suite (apache/ossie PR #237) once it exists. Integration tests run
against the TPC-DS reference model from the Ossie repository, loaded into a
local ClickHouse. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup.

## Known ClickHouse traits to handle

- Float division by zero and single-row `STDDEV`/`VARIANCE` yield `inf` or
  `nan`, not `NULL`; the executor maps both to `None`.
- Table names are `database.table`; Ossie `source` may have three parts,
  or one: a bare table reads `currentDatabase()` (the URL's database, else
  the user's default), and introspection asks the server for it. Parts
  may be quoted (`` ` `` or `"`, a doubled quote inside); unquoted text
  between dots is the name as written (`t$x`, `t#1`). `_parts` splits it
  with regexes, not SQLGlot's tokenizer. A space or parenthesis outside
  quotes, strings and comments marks a query, checked first so a query
  is reported as one. Refused: `--`, `/*`, `;` or a stray quote outside
  quotes, and `\` anywhere: ClickHouse reads it as an escape in quoted
  names, and `_parts` does not decode escapes.
- Spec functions ClickHouse lacks or answers differently are rewritten in
  `translate.py`, on the SQLGlot AST, never on SQL text. Why each one
  differs is in a comment at its rewrite; the larger decisions, with
  evidence, are also in `docs/design.md`.
- ClickHouse's short aggregate names are approximate: `quantile`,
  `median`, `uniq`. A spec function that is exact translates to an exact
  one; only `APPROX_*` may use them.
- NULLs sort last in both directions, ClickHouse's default (the spec
  leaves it open, apache/ossie#498): `NULL_ORDERING` on the parser
  dialect, `nulls_first=False` in the planner. Keep it when parsing
  switches to the Ossie dialect. An explicit `NULLS FIRST | LAST` passes
  through in a window's `ORDER BY` and is rejected elsewhere: the spec's
  syntax has it only there.
- SQLGlot adds no parentheses for precedence, and its Python operators
  (`1 - p`) skip them when `p` is the same operator. `rewrite_tree`
  parenthesizes every operator under another operator, so a rewrite builds
  plain nodes and an inlined field or metric needs nothing. `CONTAINS`
  counts as an operator there: SQLGlot prints it as `POSITION(...) > 0`
  and parenthesizes that only under a binary operator. Code that puts
  an operator under another after `rewrite_tree` parenthesizes it itself
  (`exp.and_` does for `WHERE`, `HAVING` and `ON`).
- A rewritten function, ours or SQLGlot's, gets its spec signature in
  `_SIGNATURES`; `parse()` rejects other argument counts, so a rewrite
  never drops an argument.

## Development

Python 3.11+, managed with uv. Layout: `src/dactopus_ossie_clickhouse/` (library and
CLI), `tests/` (pytest, fixtures in `tests/fixtures/`), `docs/`, `research/`
(studies behind decisions and data for Apache Ossie; never imported by the
library or tests, linted with the rest).

```bash
uv sync                       # install with dev tools
uv run pytest                 # tests; ClickHouse-dependent ones skip when no server
uv run pytest -m "not integration"   # unit tests only
uv run ruff check src tests research   # lint
uv run ruff format src tests research  # format
uv run dactopus-ossie-clickhouse validate tests/fixtures/tpcds.yaml [--url http://127.0.0.1:8123]
uv run dactopus-ossie-clickhouse sql tests/fixtures/tpcds.yaml -m total_sales -d item.i_brand
uv run dactopus-ossie-clickhouse query tests/fixtures/tpcds.yaml -m total_sales -d item.i_brand --json
uv run dactopus-ossie-clickhouse query model.yaml -m revenue --url http://analyst:secret@host:8123 --policy policy.yaml
uv run dactopus-ossie-clickhouse serve tests/fixtures/tpcds.yaml   # MCP over stdio
```

Access-control tests create users and need SQL access management on the
server (`access_management` for the connecting user; in the official Docker
image `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`). They skip otherwise.

Tests that use the `clickhouse` fixture are integration tests: they need a
server at `OSSIE_CLICKHOUSE_URL` (default `http://127.0.0.1:8123`) and skip
otherwise. DuckDB supplies default expected values for comparisons and needs
no server. Every change to library code comes with a test. Keep modules flat; add a
package level only when a module outgrows one file.

## Repository status

Released versions are in [CHANGELOG.md](CHANGELOG.md); the package is
installed from git until `apache-ossie` reaches PyPI. Decisions with their
evidence are in [docs/design.md](docs/design.md).
Pull requests are merged with rebase; releases follow
[CONTRIBUTING.md](CONTRIBUTING.md#releasing).

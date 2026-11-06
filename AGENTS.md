# AGENTS.md

Guidance for AI coding agents working in this repository. It holds what the
code does not show. The rest is where it belongs: `README.md` describes
the product, `docs/design.md` records each decision with its evidence,
`docs/model-authoring.md` what a model author sees, `CONTRIBUTING.md` the
setup, the layout and the release steps.

## Project

dactopus-ossie-clickhouse implements the
[Apache Ossie](https://github.com/apache/ossie) semantic model standard
for ClickHouse: it reads an Ossie description, translates metric
definitions into ClickHouse SQL, answers questions posed in business
terms (metrics, dimensions, filters), and serves the description and the
queries to AI agents over MCP. Licensed under Apache 2.0.

## Language

All text in this repository is in English only: code identifiers,
comments, docstrings, commit messages, documentation, issues and test
names. Do not introduce text in any other language, even in examples or
fixtures.

## Scope

Do not build: chat, charts or any user interface; data loading, ETL or
warehouse features (the data is already in ClickHouse); dependencies
beyond ClickHouse and the Ossie standard where avoidable.

## Design principles

- The Ossie description is the single source of truth. Never hardcode
  metric or table semantics that belong in the model.
- Follow the standard's semantics exactly. When ClickHouse behaviour
  differs from the standard's assumptions, handle it in the translator;
  what the standard means stays. The spec text wins over any engine,
  DuckDB and ClickHouse included.
- Every answer is reproducible: the same question and the same model
  yield the same SQL.
- Keep the public surface small. Prefer one obvious way to do each thing.
- A decision that rests on a measurement or an evaluation goes into
  `docs/design.md` with its evidence, so it is not relitigated without
  new evidence. Why a rewrite in `translate.py` exists is in a comment at
  the rewrite.

## Rules the code does not show

Layers and boundaries:

- Layers, lowest first: model loader, translator, planner, executor, MCP
  adapter. A lower layer never imports a higher one.
- The planner never talks to the database. It takes an optional `Catalog`
  the executor built.
- The MCP server (`mcp_server.py`, the `[mcp]` extra) is a thin adapter:
  each tool is one library call, errors go back as results with an
  `error` field, never as exceptions, and `client_factory` is the only
  place that decides which ClickHouse connection a caller gets. The core
  library must import without the MCP SDK.

Access control:

- Access control is ClickHouse's. Queries run as the connected user, and
  the model is trimmed (`access.py`) to what `system.tables` and
  `system.columns` reveal. The policy file only hides more; it never
  grants. Nothing in this project authenticates.
- A hidden object must never appear in SQL, errors or suggestions. A field
  or metric whose expression does not translate, or that calls a function
  the server lacks, is hidden the same way and reported by `validate`.

The vendored Ossie package:

- `apache-ossie` is carried in `src/dactopus_ossie_clickhouse/_vendor/ossie`
  until it is on PyPI. Import it as
  `dactopus_ossie_clickhouse._vendor.ossie`, never `ossie`. The copy is
  upstream's at the commit in `_vendor/vendor.txt`, changed only by
  `scripts/vendor_ossie.py`; never edit it by hand (CI checks).
- Do not write a model loader. A file is read with
  `OssieDocument.from_ossie_yaml` or `from_ossie_json`; `model_validate`
  is for a document built in Python. `load_model` combines several files
  as a union; a name in two files is an error, never a merge.
- The supported Ossie schema version is pinned (`0.2.0.dev0`) and checked
  explicitly.

Translation:

- Expressions are parsed with SQLGlot's default dialect until the upstream
  Ossie dialect lands. `model.expression_text` is the one place that picks
  which of an expression's texts is read; a dialect name is any string.
- `parse()` types only the spec's function catalog (`_CATALOG`). Any other
  function is `exp.Anonymous` and reaches ClickHouse as written, so
  SQLGlot never renames it. A rewritten function carries its spec
  signature in `_SIGNATURES`, so a rewrite never drops an argument.
- Rewrites work on the SQLGlot AST, never on SQL text. Emitted SQL uses
  physical names as the model writes them; name resolution is
  case-insensitive.
- `rewrite_tree` parenthesizes every operator under another operator.
  Code that puts an operator under another after `rewrite_tree`
  parenthesizes it itself (`exp.and_` does).
- ClickHouse specifics (`FINAL`, dictionaries, the server's functions)
  come from introspecting `system.tables`, `system.columns`,
  `system.dictionaries` and `system.functions`; the user is never asked.
  Overrides live in `custom_extensions` under `vendor_name: CLICKHOUSE`,
  a dataset's (`dedup`) or the model's (`time_zone`); `model._extension`
  is the one parser.
- `CURRENT_*`, ClickHouse's `now()` and `today()`, timestamp literals and
  `DATEDIFF` use the model's zone (`model.time_zone`, UTC by default),
  never the server's zone or `session_timezone`.
- A window's `ORDER BY` item without `NULLS FIRST | LAST` sorts NULL as the
  largest value (spec, "Window ORDER BY NULL Placement"): `parse()` makes
  `DESC` say `NULLS FIRST`. Every other `ORDER BY`, the planner's rows and
  `WITHIN GROUP` included, sorts NULLs last both ways. Keep both when
  parsing switches to the Ossie dialect.

Planning:

- Deterministic: datasets from `source`, joins from `relationships`, one
  `SELECT` per question. `LEFT JOIN` from one root along the one path to
  each dataset, only along relationships whose `to_columns` cover a key
  of the target. Keys and relationship columns name fields, never source
  columns. A row filter the root does not reach is a semi-join; metrics
  no one root answers together are stitched. The rules and the refusals
  are in `docs/design.md`; the code is `planner.py`.
- The planner refuses a question it cannot answer with the right number:
  an aggregate that repeats could change, an aggregate over aggregates,
  two paths to one dataset. Errors name the nearest known object; agents
  recover from that.
- Metrics come at two levels, the model's and a dataset's, the latter
  named `dataset.metric`. Every layer lists them through
  `model.all_metrics`.

## Testing

- Every change to library code comes with a test.
- Translation tests compare values, not only that ClickHouse accepts the
  SQL: a translation that runs and returns the wrong value is the failure
  mode to guard against. DuckDB gives the default expected value as a
  second opinion. Where it lacks a function or disagrees with the spec,
  the expected value is written by hand with a reference to the spec
  section.
- A rule in `translate.py` that exists only because of a gap in SQLGlot
  comes with a canary in `tests/test_canaries.py`: SQLGlot's output
  without the rule, pinned, and the rule named in the message. So does a
  fact of Ossie a rule depends on. Canaries pass on the lock;
  `.github/workflows/latest.yml` runs the suite weekly on the newest
  SQLGlot and on apache-ossie from `main`, so a red canary means a rule
  to revisit, not a broken answer.
- Tests with the `clickhouse` fixture need a server at
  `OSSIE_CLICKHOUSE_URL` (default `http://127.0.0.1:8123`) and skip
  otherwise; access-control tests also need SQL access management on
  the server. `tests/test_compliance.py` and the fixture canary need a
  checkout of apache/ossie at `OSSIE_REPO`. `CONTRIBUTING.md` has the
  setup.
- `tests/fixtures/tpcds.yaml` is an exact copy of Ossie's
  `examples/tpcds_semantic_model.yaml`; a canary checks it.

## Development

Python 3.11+, managed with uv. `research/` holds studies behind decisions
and is never imported by the library or tests; `scripts/` is maintenance,
outside the package. Keep modules flat; add a package level only when a
module outgrows one file.

```bash
uv sync                                        # install with dev tools
uv run pytest                                  # ClickHouse-dependent tests skip without a server
uv run pytest -m "not integration"             # unit tests only
uv run ruff check src tests research scripts
uv run ruff format src tests research scripts
uv run python scripts/vendor_ossie.py <full commit>   # move the apache-ossie copy
uv run dactopus-ossie-clickhouse validate tests/fixtures/tpcds.yaml [--url http://127.0.0.1:8123]
uv run dactopus-ossie-clickhouse sql tests/fixtures/tpcds.yaml -m total_sales -d item.i_brand
uv run dactopus-ossie-clickhouse query tests/fixtures/tpcds.yaml -m total_sales -d item.i_brand --json
uv run dactopus-ossie-clickhouse serve tests/fixtures/tpcds.yaml   # MCP over stdio
```

## Repository status

Released versions are in `CHANGELOG.md` and on PyPI, where a release on
GitHub publishes them. `README.md` is the PyPI page, so its links are
absolute. Pull requests are merged with rebase; releases follow
`CONTRIBUTING.md`.

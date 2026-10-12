# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). While the major version is 0,
a minor release may change the Python API.

## [Unreleased]

### Added

- A metric of aggregates over two datasets, such as sales per employee,
  `SUM(store_sales.ss_ext_sales_price) /
  NULLIF(SUM(store.s_number_employees), 0)`: each aggregate is computed
  over its own dataset's rows and the results are merged on the
  dimensions, as #246 §4.5 says of a metric's operands. Such a metric was
  refused in every question, hidden from agents and reported by
  `validate`; now it is answered wherever each dataset reaches the
  question's dimensions.

### Changed

- An aggregate over a dataset other than the question's root counts all
  of that dataset's rows when the dataset reaches the dimensions on its
  own, not only those the root's rows reference. In the TPC-DS model,
  `customer_lifetime_value` in total divides sales by all 100,000
  customers (52,624.71), where it divided by the 90,858 who bought
  (57,919.73); by a customer field it counts that field's customers
  who did not buy too, and by a store field it still divides by the
  buyers. A `MAX` of store staff next to sales by state is of every store
  in the state. Such a question is now stitched, so like other stitched
  questions it refuses an aggregate function written into `having` and a
  window metric.
- `validate` reports, and agents do not see, a metric that is refused
  when asked alone: that question is the one most likely to be
  answered. Newly reported: one metric over two facts that share no
  dimension, and a metric every question of which needs a join the
  model's keys do not allow.
- Breaking: the MCP tool `query` is `execute_query`, the tool of the
  `execute_query` profile draft, apache/ossie#529 `0.4-draft` at
  `b5418ee`, and passes that profile's conformance checker
  (`validation/validate_mcp.py`). Its arguments are `data_source_id`
  (named by `list_model` and the server instructions; it changes when the
  model does) and `query`, a Layer 3 query object, apache/ossie#246 §5 at
  `cc0d070`: `{"measures": [...], "dimensions": [...], "where": ...,
  "having": ..., "order_by": [{"field": ..., "direction": "DESC"}],
  "limit": ...}`, in place of the arguments `metrics`, `filters` and
  `order_by` strings. `inputSchema` and `outputSchema` are the profile's.
  `Query` has `measures`, `where`, `having` and `order_by` of `Order`; the
  CLI has `-w` and `--having` in place of `-f`, and `-m` is spelled
  `--measure`. A condition is no longer moved between `WHERE` and
  `HAVING`: one in the wrong clause is refused.
- Breaking: an answer embeds its rows as CSV (`text/csv`, NULL as `\N`)
  and its `structuredContent` is the profile's: `model` with a revision,
  `preview` of the first 10 rows with logical types, `result` with
  `row_count`, `completeness` and the SQL under `extensions`. Without a
  `limit`, 100 rows come back and the answer says `truncated` when there
  were more; before, the limit was silent. A refused query is a tool error
  (`isError`): `error` with `code`, `message` and `retryable`, and
  `suggestions`; before, it was a result with an `error` string. Every
  `PlanError` has a `code` (#246's where one applies, such as
  `E_AMBIGUOUS_PATH` for two paths to a dataset, otherwise
  `INVALID_ARGUMENT` for a value the query schema rejects,
  `QUERY_INVALID`, `UNSUPPORTED_QUERY`) and `suggestions`; the CLI prints
  the code. `list_model` describes the binding: model revision, profile
  and Layer 3 revisions, what is refused.
- Breaking: over MCP a `DateTime` value is the profile's `DateTimeTz`, ISO
  8601 with its offset (`2024-03-15T10:30:45+09:00`), where the CLI and
  0.5.0's MCP rows print ClickHouse's text. A `Time` and other values of
  no profile type stay ClickHouse's text.
- The `mcp` extra needs mcp 2.2 or later.
- An explicit `order_by` sorts NULL as the highest value, first
  descending, as #246 says, unless the entry's `nulls` says `LAST`. The
  default order, first measure descending, still puts NULLs last.

## [0.6.0] - 2026-10-13

### Added

- Joins along a path of several relationships, such as order lines to
  orders to customers: a question about line revenue by customer region
  is answered, where it was refused. The dataset in the middle joins like
  any other, so a row filter may read it. Two paths to a dataset a
  question touches are refused as ambiguous, as apache/ossie#246 §6.9
  says.

### Changed

- Breaking: a direct relationship next to a longer path to the same
  dataset is refused as ambiguous (apache/ossie#246 §6.9), where 0.5.0
  joined along the direct one. None of the models in this repository or
  in dactopus-data-models has such a pair.

### Fixed

- Division of a `Decimal` lost digits: ClickHouse keeps the dividend's
  scale, so `SUM(refunds.amount) / SUM(orders.amount)` gave 0.02 where
  Postgres and DuckDB give 0.0286. The dividend of `/` is now `Float64`,
  as DuckDB divides, so a ratio of `Decimal` values is a float
  (`customer_lifetime_value` of the TPC-DS model is 57919.727998855364
  in the CLI, where it was 57919.72). Division by zero no longer fails
  the query: the value is shown as NULL, but inside the query it is
  infinite (NaN for 0 / 0), so it sorts before or after every number and
  a positive one passes a filter such as `ratio > 1`, as division of
  integers already did. Division of integers
  is unchanged: it was already `Float64`.

## [0.5.0] - 2026-10-12

### Added

- Metrics of several facts in one question, such as revenue and returns
  by region: each fact is aggregated on its own and the results are
  merged on the dimensions, as apache/ossie#246 describes. Every region
  of either fact appears, a NULL region from both makes one row, and a
  fact without rows in a region gives what its metric gives over no rows
  (NULL for a sum, 0 for a count). Without dimensions the answer is one
  row of totals. Such questions were refused, and so was a metric of a
  dimension table next to a fact's, such as staff next to sales by state.

### Fixed

- An aggregate over no rows is NULL, as the spec and SQL say, not 0: a
  total whose filter matches nothing, and `SUM(x) FILTER (WHERE ...)` in a
  group where nothing matches. ClickHouse answered 0 for `SUM`, `MIN`,
  `MAX` and `ANY_VALUE`; generated SQL now sets
  `aggregate_functions_null_for_empty = 1`. Counts stay 0.

- Rows that tie on the requested or default order, such as the regions a
  stitched question gives no revenue, came back in whatever order
  ClickHouse produced them, so a `limit` could keep different rows on each
  run, and a question without metrics was not ordered at all. Ties are now
  broken by the dimensions, ascending (#42).

- A `Time` value read differently in the CLI and over MCP, and neither
  form was ClickHouse's: `-01:02:03` came out as `-1 day, 22:57:57` in
  the CLI and `-PT1H2M3S` over MCP, `100:00:00` as `4 days, 4:00:00` and
  `P4DT4H` (#40).

### Changed

- `Time`, `Time64`, `DateTime` and `DateTime64` values in the CLI's output
  and the MCP server's rows are ClickHouse's text, the same in both and
  usable as a filter literal: `2024-03-15 10:30:45.123` with the column's
  fraction digits, without the time zone offset the CLI added and the `T`
  MCP used. `Result.rows` keeps Python values; `Result.shown_rows()` gives
  the text, and `Result.types` the ClickHouse type of each column.

### Removed

- The advice to ask for each metric in its own question: it was given only
  when each metric could be answered alone, and such questions are now
  answered together.

- The `ossie-clickhouse` command, the name before 0.4.0: run
  `dactopus-ossie-clickhouse`, and point an MCP client's configuration at
  it (`docs/mcp-setup.md`).

## [0.4.0] - 2026-10-10

### Changed

- Renamed from `ossie-clickhouse` to `dactopus-ossie-clickhouse`: the
  package, the command, the repository (`Dactopus/dactopus-ossie-clickhouse`;
  GitHub redirects the old URLs) and the MCP server's name. Apache Ossie's
  own converters are named `ossie-<vendor>` with a module `ossie_<vendor>`,
  and one for ClickHouse is proposed under the old name; the two could not
  be installed together. To migrate:
  - uninstall `ossie-clickhouse` (`pip uninstall`, `uv tool uninstall`),
    then install `dactopus-ossie-clickhouse`. In this order: both packages
    install an `ossie-clickhouse` command, so `uv tool` refuses the new one
    while the old one is installed, and uninstalling the old one
    afterwards removes the command from the new one too;
  - import `dactopus_ossie_clickhouse` instead of `ossie_clickhouse`;
  - run `dactopus-ossie-clickhouse` instead of `ossie-clickhouse`. The old
    command still works in 0.4.x, with a note on stderr, and is removed in
    0.5.0;
  - in an MCP client's configuration, rename the server and point it at the
    new command (`docs/mcp-setup.md`).

  `OSSIE_CLICKHOUSE_URL` keeps its name. The diagram's interactive page
  moves to <https://dactopus.github.io/dactopus-ossie-clickhouse/>; the old
  address does not redirect.

## [0.3.1] - 2026-10-10

### Fixed

- `PERCENTILE_CONT` and `MEDIAN` are exact, as the spec requires. They
  were translated to ClickHouse's `quantile` and `median`, which keep a
  sample of 8,192 values: on larger groups the answer was approximate and
  could change between runs (the median of 100,000 values came out 49,505
  instead of 49,999.5). They now read every value, so memory grows with
  the group, as it already did for `PERCENTILE_DISC`; `APPROX_PERCENTILE`
  remains the fast approximate choice. Both return `Float64` (`Decimal`
  before, for a `Decimal` column) and NULL for an empty set. Over a
  `Date`, `DateTime` or `String` column ClickHouse now refuses the
  query: the spec, like Postgres, defines them for numbers only. For a
  median date, use `PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ...)`.
- `PERCENTILE_DISC(...) WITHIN GROUP (...) FILTER (WHERE ...)` runs.
  The condition went to the expression `PERCENTILE_DISC` is rewritten to,
  and ClickHouse refused it (`ifIf` does not exist); it now applies to
  each aggregate inside.
- `PERCENTILE_DISC(...) WITHIN GROUP (...) OVER (...)` is rejected when
  the model is read, with a clear error; it produced SQL ClickHouse
  refused. `PERCENTILE_CONT` and `MEDIAN` with `OVER` work as before.
- `FILTER (WHERE ...)` on an expression that is not an aggregate, such as
  `COALESCE(SUM(x), 0) FILTER (...)`, is rejected when the model is read,
  as the spec requires; ClickHouse refused it.

## [0.3.0] - 2026-10-10

### Changed

- Requires SQLGlot 30.22 or later. Its ClickHouse generator now doubles
  `\` in quoted names and translates `CONTAINS` and `DAYOFYEAR`, so the
  rewrites this project carried for them are gone. Values are unchanged;
  the SQL spells `POSITION` in capitals.

### Fixed

- `DAYOFMONTH(d)` runs: SQLGlot 30.22 translates it to `toDayOfMonth`
  instead of `DAY_OF_MONTH`, which ClickHouse does not have.

### Removed

- `translate.CLICKHOUSE`, the dialect that doubled `\` in quoted names.
  Generate SQL with `dialect="clickhouse"`.

## [0.2.5] - 2026-10-06

### Fixed

- Tables with `ReplicatedReplacingMergeTree`, on a cluster, and
  `SharedReplacingMergeTree`, which ClickHouse Cloud makes of every
  `ReplacingMergeTree`, are read with `FINAL`. Before, only plain
  `ReplacingMergeTree` was, and metrics over these tables counted every
  stored version of a row (#29).
- A `Distributed` table, materialized view or `Merge` table over a
  `ReplacingMergeTree` table is read with `FINAL`. Before, it was read
  raw: on a cluster, where models point at the `Distributed` table,
  metrics counted every stored version of a row. When the table it reads
  is hidden from the connected user, `validate --url` reports the
  dataset, which is read without `FINAL` as before.

### Changed

- `{"dedup": "final"}` in a dataset's `custom_extensions` adds `FINAL`
  whatever the engine, for a source whose stored rows the connected user
  cannot see. Before, it meant the default.

## [0.2.4] - 2026-10-05

### Fixed

- A dataset `source` with quoted parts (`` `shop`.`orders` ``,
  `"Sales DB".orders`), as dbt-clickhouse writes it, is read as the table
  it names. Before, the quotes stayed in the name, the table was not
  found, and the dataset disappeared from the model (#26).
- A dataset `source` with a backslash is refused and reported by
  `validate --url`. Before, the generated query read another table than
  the one checked in ClickHouse, or broke: ClickHouse reads `\` in a
  quoted name as an escape.
- A column whose name has a backslash is read as named. Before, the
  backslash went into the query undoubled, and a field over column
  `a\x41` read column `aA`.

### Changed

- `--` and `/*` outside quotes in a dataset `source`, likely a pasted
  comment, refuse it; 0.2.3 read them as part of the table name. Quote
  the part if the table is really named so.

### Added

- `docs/model-authoring.md`: tables replicated by ClickPipes or PeerDB
  keep deleted rows under `FINAL`; read them through a view that filters
  `_peerdb_is_deleted`.

## [0.2.3] - 2026-10-05

### Added

- A diagram of how a question becomes SQL in the README, linked to an
  interactive page (`docs/index.html`, GitHub Pages) that follows one
  question on the `web_analytics` model of dactopus-data-models.
  `tests/test_docs.py` checks that the SQL, the lines each step marks and
  the refusals it quotes are what the planner writes for a ClickHouse user
  who can or cannot read a column; the README diagram follows GitHub's
  theme, not the system's.

### Fixed

- A compound expression placed under an operator keeps its precedence.
  SQLGlot prints no parentheses of its own, so a metric named in a filter
  (`1000 / customer_lifetime_value > 1`), a computed field inside
  arithmetic (`orders.net * 2`, `net` being `amount - 5`), `CONTAINS`
  under another operator and a percentile whose fraction is an expression
  (`PERCENTILE_CONT(0.5 - 0.2) ... DESC` took the 0.3 quantile, not 0.7)
  were read with the operators regrouped and returned wrong values
  without an error. Every operator under another operator is now
  parenthesized, except the left operand of the same operator
  (`a - b - c`, `a AND b AND c`), also where precedence alone would do:
  generated SQL prints new parentheses (`(x * 2) > 25`,
  `(a > 1) AND (b < 2)`), with the same values.

## [0.2.2] - 2026-10-04

### Fixed

- A source without a database is introspected where ClickHouse reads it:
  the URL's database, else the user's `DEFAULT DATABASE`. Introspection
  used to look in `default`, so such a user with no database in the URL
  got a refusal, or, with a copy of the table in `default`, the wrong
  engine: a ReplacingMergeTree read without `FINAL` counted its
  duplicates.

### Changed

- A source may be a bare table name, read in the connection's database;
  documented in `docs/model-authoring.md` and tested. One model then
  serves any database a deployment chooses.
- `validate --url` and `serve` (on stderr) name the host and database
  they read, and so does the refusal when no dataset is readable.

## [0.2.1] - 2026-10-04

### Changed

- The MCP server's instructions include the model's own
  `ai_context.instructions`. They used to come only from `list_model`, so
  an agent that started with `search_model` never saw them.
- The MCP server's instructions tell the agent to state only numbers a
  query returned. Agents named the value of a metric they had not queried
  and made up the counts behind a rate.
- A refused question with several metrics, each of which can be asked on
  its own, says so. With one dimension it also says how to keep the rows
  of the first answer: filter the other questions with
  `sessions.source IN (...)` over the values that answer returned. The
  refusal for datasets not joined from one root used to give no advice
  at all.

## [0.2.0] - 2026-10-03

### Changed

- Requires SQLGlot 30.21 or later, which generates `varPop` for
  `VAR_POP` itself (tobymao/sqlglot#8469); the translator's own rewrite
  is gone.
- A relationship is joined when its `to_columns` cover a primary or unique
  key of the target, not only when they equal one, as the reference
  validator reads the spec (apache/ossie#330). A join on
  `(tenant_id, customer_id)` to customers keyed by `customer_id` used to
  fail with "not many-to-one". A target with no declared key gets its own
  error that says so. `validate` now fails on both kinds of relationship,
  so a model that passed it before may not.

- NULLs sort last in both directions, in window `ORDER BY` inside model
  expressions and in a query's `order_by`. Ascending orderings used to put
  NULLs first, a default inherited from SQLGlot rather than chosen; a
  running total over a key with NULLs, such as sales without a matching
  date, started from their sum. The spec does not define NULL ordering;
  this is ClickHouse's and DuckDB's default, so the generated SQL carries
  no `NULLS` clause.
- Expressions with `NULLS FIRST | LAST`, or with a window frame the spec
  does not list, are rejected; `validate` reports them. Accepted frames:
  `ROWS` frames that hold the current row, and `RANGE BETWEEN UNBOUNDED
  PRECEDING AND CURRENT ROW`. ClickHouse answered an empty frame with 0
  instead of NULL, and a `RANGE` offset counted NULL keys as within it.

### Fixed

- A question no longer returns a wrong number when a metric aggregates
  another dataset's rows over the root's: each joined row repeats once per
  root row that references it, and only referenced rows are present. The
  planner now refuses such a metric with a message that names what made
  the root. The reference TPC-DS `store_productivity` summed each store's
  staff once per sale; it is now refused in every question, and
  `validate` reports it, so a model that passed `validate` before may not.
  Repeat-safe aggregates (`MIN`, `MAX`, `DISTINCT` and similar) over a
  joined dataset still pass when the metric also reads the root, and so
  does any aggregate over a dataset joined on a key of the root. A metric
  that is only `COUNT(*)` is refused too: it counted stores or sales
  depending on what else the question asked. Metrics refused in every
  question are hidden from agents and reported by `validate`.
- `validate` reports a metric that names an unknown field; it passed and
  then failed every question.
- A rewritten function inside another one is translated in queries too,
  not only by `translate`: `NULLIFZERO(APPROX_PERCENTILE(x, 0.5))` in a
  metric reached ClickHouse as `APPROX_PERCENTILE`, which it does not
  have.

- `LAG` and `LEAD` without a default, and `NTH_VALUE` past the end of the
  frame, return NULL as the spec (ANSI SQL) says, not 0. ClickHouse falls
  back to the type's default when the argument is not Nullable, so a
  month-over-month change on a non-Nullable column showed the first
  month's full value as its change. `FIRST_VALUE` and `LAST_VALUE`
  respect NULLs; ClickHouse skips them by default. `IGNORE NULLS` on
  `LAG`, `LEAD` and `NTH_VALUE` is rejected: ClickHouse ignored it.
- A rewritten function inside another rewritten one is translated too:
  `DAYOFYEAR(TO_DATE(s))` used to leave `TO_DATE` for ClickHouse, which
  has no such function.

## [0.1.1] - 2026-10-01

### Fixed

- Functions the translator rewrites are checked against the spec's
  signature. Longer forms from other engines, such as Snowflake's
  `REGEXP_COUNT(str, pattern, position, flags)`, used to lose their extra
  arguments and return a different value; they are now rejected with the
  expected signature. Too few arguments give the same error instead of an
  `IndexError`, and `EXTRACT` and `DATE_PART` accept only the spec's date
  parts.
- One field or metric whose expression does not translate no longer stops
  the whole model. It is hidden like an object the user may not read,
  everything that depends on it goes with it, and `validate` names each
  one, with or without `--url`.

## [0.1.0] - 2026-09-30

First release. Ossie schema `0.2.0.dev0`, tested on ClickHouse 26.x with
the TPC-DS reference model.

- Model loading on `apache-ossie` with checks upstream lacks: unique names,
  relationships that resolve, expressions in a translatable dialect.
- Expression translation on the SQLGlot AST: the spec's function catalog
  mapped to ClickHouse, `OSSIE_SQL_2026` preferred, `ANSI_SQL` fallback,
  every catalog function checked by value against DuckDB or the spec text.
- Planner: one `SELECT` per question, `LEFT JOIN` along many-to-one
  relationships from one root dataset, `HAVING` for filters over metrics
  or aggregates, window metrics passed through, errors that name the
  nearest known object.
- Executor: `FINAL` for `Replacing*` engines, `dictGetOrNull` for
  single-key dictionaries, both learned from system tables; `inf` and
  `nan` returned as NULL; `{"dedup": "none"}` override in
  `custom_extensions`.
- Access control through ClickHouse: the model is trimmed to what the
  connected user may read; an optional policy file hides more per user or
  role.
- MCP server over stdio with `list_model`, `search_model`,
  `describe_object` and `query`.
- CLI: `validate`, `sql`, `query`, `serve`.

# Changelog

What changes for you from one version to the next. **Breaking:** marks a
change that needs something from you: a model that no longer loads, a
question or metric refused that was answered, a renamed or removed
command or API, with what to do. "Answers change" marks a change after
which a question returns another number. Why each change was made is in
[docs/decisions/](https://github.com/Dactopus/dactopus-ossie-clickhouse/tree/main/docs/decisions).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). While the major
version is 0, a minor release may change the Python API.

## [Unreleased]

### Changed

- **Breaking:** `x IN ()`, which ClickHouse answered as false, and
  `SUM(DISTINCT)` without an argument are refused, as the spec's
  fixtures require; `validate` reports a field or metric that writes
  them. Write `FALSE` for `x IN ()`.
- `NULLS FIRST | LAST` is accepted in `WITHIN GROUP (ORDER BY ...)`,
  where it changes nothing.

### Fixed

- `ROUND` rounds a value exactly halfway away from zero, as the spec
  requires (apache/ossie#687). Answers change for a `Float64`:
  `ROUND(2.5)` is 3, was 2; `ROUND(0.125, 2)` is 0.13, was 0.12. A `d`
  read from a column keeps ClickHouse's `round`, which rounds a `Float64`
  tie to even
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/round-and-mod.md)).
- `MOD(a, b)` and `a % b` of a `Decimal` align the scales. Answers change:
  `MOD(125.50, 7)` on a `Decimal(18, 2)` is 6.5, was 0.06.

## [0.16.0] - 2026-11-11

### Changed

- **Breaking:** the `CLICKHOUSE` entry of a model or a dataset (`time_zone`,
  `dedup`) is read from `nonportable_extensions`, with `data` an object, as
  Ossie renamed `custom_extensions` (apache/ossie#522). A model in the old
  form no longer loads, and the message says what to rename. Write
  `nonportable_extensions: [{vendor_name: CLICKHOUSE, data: {dedup: none}}]`.
- A row filter on a dataset the root does not reach, with no fact named in
  a filter, is read through the one fact every linking fact passes through:
  `orders.order_count -f "products.status = 'archived'"` counts the orders
  with a line of an archived product. It was refused
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md#through-a-fact-that-links-them)).

## [0.15.0] - 2026-11-11

### Changed

- **Breaking:** the supported Ossie schema version is `0.3.0.dev0`, and a
  model that declares `0.2.0.dev0` is refused. Change its `version` line;
  nothing else in a model changes.
- `sqlglot>=30.31`.

### Added

- A metric grouped by a field of rows that reference its dataset counts
  each of its rows once per group (#10): `orders.order_count` by
  `order_lines.product_id`, `customers.customer_count` by
  `orders.shipping_country`. It was refused. The dataset that links them
  must be one the question names
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/one-side-by-many-side.md)).
- `serve` says on stderr how much of the model the connected user is not
  served, and why: missing in the database, not granted, does not
  translate, or hidden by the policy. `Executor.withheld()` gives the counts.

## [0.14.0] - 2026-11-07

### Changed

- **Breaking:** a metric over several datasets is refused for a dimension
  or filter one of its datasets cannot reach; it was answered over the
  buyers only. On TPC-DS, ask `customer_lifetime_value` by a field of
  `store`, `date_dim` or `item` with a metric of `store_sales` instead,
  such as `COUNT(DISTINCT ss_customer_sk)`
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/components.md#reachability)).
- **Breaking:** refused in every question and reported by `validate`: a
  model's metric with an aggregate over several datasets (or a `FILTER` on
  another) next to one over a dataset the first does not read, and a
  metric over several datasets with a window function or a column outside
  every aggregate. A `COUNT(*) FILTER (WHERE ...)` names no dataset and is
  refused next to an aggregate of another. Declare the ratio on one
  dataset, or write two metrics.
- **Breaking:** refused in a question: a window metric or an aggregate
  written in a filter next to a metric over several datasets, and a
  written filter that mixes aggregates of different datasets
  (`SUM(a.x) / COUNT(DISTINCT b.k) > 5`,
  `total_sales > COUNT(DISTINCT store.s_store_sk)`); name a metric instead.
- **Breaking:** a call of `AGG` in any capitalization is refused, as the
  spec reserves the name (apache/ossie#662); a field, metric or column
  named `agg` is unaffected.
- Each aggregate of a model's metric is computed over its own dataset's
  rows, as Layer 3 requires (apache/ossie#653). Answers change: on TPC-DS,
  `customer_lifetime_value` divides by all 100,000 customers and is
  52,624.71, was 57,919.73; `store_productivity`, refused before, is
  answered. A filter comparing metrics of different datasets
  (`total_sales > store_count`) is answered over each one's own rows
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/components.md)).
- A window's `ORDER BY` without `NULLS FIRST | LAST` sorts NULL as the
  largest value, as the spec says (apache/ossie#587). Answers change for
  `DESC` over a key with NULLs: `RANK() OVER (ORDER BY d_year DESC)` ranks
  the rows without a year 1, not last
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/null-placement.md)).
- `sqlglot>=30.30`.

### Added

- A dataset's metric may read a column of its `source` as `source.column`
  (apache/ossie#633), checked and hidden like a field's column
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/source-columns.md)).

### Fixed

- **Breaking:** a key stored twice on the one side of a relationship no
  longer counts the root's rows twice. Answers change on such data: with
  customer C1 written twice, `orders.total_revenue` by region gives EAST
  350, was 650. An expression where two copies of a key could give one
  result (`customers.weight > 0 OR orders.amount > 150`) is refused on any
  data, and so is a window metric over aggregates that need other values
  of the repeated dataset, or next to metrics answered apart. A large table on the one side costs more: 3.0 s against 0.85 s on
  10 million orders
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/key-stored-twice.md)).
- **Breaking:** a question with a window metric and nothing that groups rows
  (`SUM(orders.amount) OVER (ORDER BY orders.order_id)` alone) is refused;
  it returned one row per order.
- **Breaking:** a model's metric that reads a field outside every
  aggregate (`SUM(orders.amount) * customers.weight`) is refused and
  hidden; ClickHouse rejected it unless the field was a dimension. Every
  field of a model's metric goes inside an aggregate.

## [0.13.0] - 2026-11-01

### Changed

- **Breaking:** the package carries its own copy of apache-ossie, at
  `fab4f63`, so that it can be on PyPI. `load_model` returns the copy's
  `OssieDocument`: import types from `dactopus_ossie_clickhouse._vendor.ossie`,
  not `ossie`. `Executor` refuses a document built with an installed
  apache-ossie
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/vendored-ossie.md)).
- `sqlglot>=30.27`.

### Added

- On PyPI: `pip install "dactopus-ossie-clickhouse[mcp]"` (#3).
- The query compliance cases of apache/ossie run against ClickHouse, each
  as the question its `AGG()` query asks
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/questions-only.md)).

## [0.12.1] - 2026-10-30

### Added

- A filter on a dataset that meets the root nowhere is answered through a
  fact another filter names (#69): `-m orders.order_count -f
  "order_lines.order_id IS NOT NULL" -f "product_categories.category =
  'Clothing'"` counts the orders with a line in Clothing. Without the named
  fact, the refusal lists the facts to name
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md#through-a-fact-that-links-them)).

## [0.12.0] - 2026-10-27

### Changed

- `sqlglot>=30.25`. The SQL changes, the values do not: `NOT CONTAINS(s,
  'a')` prints as `NOT POSITION(s, 'a') > 0`, and `EXTRACT(MILLISECOND
  FROM ts)` is sent as written.

## [0.11.0] - 2026-10-27

### Changed

- **Breaking:** a model file with a repeated key (two `name:` lines in one
  dataset), or with an empty `sql`, `expression`, `dialect` or
  `vendor_name`, no longer loads (apache/ossie#618). The last key used to
  win without a word.

### Added

- A row filter on a dataset that meets the root at a dataset both reach is
  a semi-join through it (#69): `-m order_lines.line_revenue -f
  "product_categories.category = 'Clothing'"` adds up the lines whose
  product is in Clothing, each once. A filter dataset that does not list
  properties of the shared one is refused, with what to declare
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md#through-a-dataset-both-reach)).
- An expression may be written in `sql`, the spec's portable form
  (apache/ossie#617). `sql` is read first, then an `OSSIE_SQL_2026` entry,
  then `ANSI_SQL`; a dialect name is any string.

### Fixed

- `SPLIT_PART` of a NULL string, or with a NULL delimiter, is NULL, as in
  DuckDB. Answers change: it was `''`, so a count of the parts counted rows
  without one.

## [0.10.1] - 2026-10-25

### Changed

- **Breaking:** `LOG(x)`, outside the spec, is refused: ClickHouse reads it
  as the natural logarithm, Postgres and DuckDB as base 10. A field or
  metric that writes it is hidden from agents and reported by `validate`:
  write `LN(x)` or `LOG(10, x)`.

### Fixed

- **Breaking:** fourteen catalog functions dropped arguments without an
  error: `REGEXP_LIKE(s, 'iphone', 'i')` ran case-sensitive, `MOD(x, y, z)`
  as `x % y`. Each now refuses the arguments it would lose, and `validate`
  reports them; longer forms that mean the same in ClickHouse
  (`FLOOR(x, 1)`, `LTRIM(s, 'ab')`) keep working
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/catalog-signatures.md)).
- `REGEXP_EXTRACT(s, 'a(b.)')` returns the whole match, `abc`, as the spec
  says. Answers change: it returned the capture group, `bc`.
- `LOG(base, x)` runs for any base. Answers change: `LOG(-2, x)` and
  `LOG(-10, x)` are NULL, were `log2(x)` and `log10(x)`.
- A field asked for as a metric gets an error that says it is a field and
  lists the metrics that read it (#66).
- `search_model` returns up to 20 datasets and fields, then up to 20
  metrics; a broad word could leave every metric out.

## [0.10.0] - 2026-10-23

### Changed

- **Breaking:** `primary_key`, `unique_keys`, `from_columns` and
  `to_columns` name fields of their dataset, not columns of its source
  (apache/ossie#486). A model whose key or relationship names anything
  else no longer loads; declare the field. A key or relationship is hidden
  from a user who cannot read one of its fields
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/keys-name-fields.md)).
- **Breaking:** a negative `limit` is refused; ClickHouse read it as the
  last rows.
- A failure to connect is one line naming the step, the location without
  credentials and what to check (#62); the error is `executor.ConnectError`.

### Added

- Metrics declared on a dataset (`datasets[].metrics`, apache/ossie#591),
  asked for as `dataset.metric`. A field and a metric of one dataset with
  the same name fail to load.
- The MCP `query` tool returns `truncated` when `limit` cut off rows; the
  CLI notes it on stderr, and `Result.truncated` carries it.

### Fixed

- A metric with an aggregate over aggregates outside a window
  (`AVG(COUNT(x))`) is refused, reported by `validate` and hidden; every
  question with it failed in ClickHouse.

## [0.9.5] - 2026-10-22

### Fixed

- **Breaking:** an `https://` URL connected without TLS unless its port was
  443 or 8443, sending the password in the clear (#60). The scheme now
  decides on every port, so an `http://` URL on 443 or 8443 goes without
  TLS: write `https://`. A URL with another scheme, without one (such as
  `host:8123`) or without a host is refused.

## [0.9.4] - 2026-10-22

### Added

- `list_model` returns `today` and `now` in the zone in effect, by the
  server's clock (#56).

### Fixed

- **Breaking:** a string compared with a `DateTime` or `DateTimeTz` field is
  read as a `TIMESTAMP` literal in the model's zone, not the column's
  (#58). Answers change: in a New York store, the orders before
  `'2026-09-01 00:00:00'` are 3, were 2. A field declared `DateTime` over a
  `String` column fails in such a comparison: parse the column in the
  field's expression
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/time-zone.md)).
- ClickHouse's `today()`, `yesterday()`, `now()` and `now64()` without a
  zone are read in the model's zone (#56). Answers change on a server in
  another zone.
- Agents asked about today wrote the date they knew and answered for the
  wrong day (#56); the server's instructions now ask for `CURRENT_DATE` or
  `CURRENT_TIMESTAMP` in filters.

## [0.9.3] - 2026-10-22

### Added

- `--time-zone`, with `OSSIE_CLICKHOUSE_TIME_ZONE` as the default, and
  `load_model(time_zone=...)` replace the model's zone, so one model serves
  stores in several zones (#54).

## [0.9.2] - 2026-10-22

### Changed

- `CURRENT_DATE`, `CURRENT_TIMESTAMP`, `CURRENT_TIME`, timestamp literals
  and string casts to a timestamp without an offset, and `DATEDIFF`, use
  the model's zone, UTC by default, not the server's. Answers change on a
  server outside UTC
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/time-zone.md)).
- The CLI and the MCP server show a `DateTime` with its offset from UTC,
  `2024-03-15 10:30:45+00:00`, which still works as a filter literal.

### Added

- A model names the zone of its dates in its `CLICKHOUSE` entry
  (`time_zone`), UTC without one (#52).
- A metric that reads no dataset, such as `CURRENT_TIMESTAMP`, is answered
  without `FROM` (#52).

### Fixed

- A `DateTime` value filtered back as `TIMESTAMP '...'` matched no row when
  the column's zone differed from the server's; a fraction of a second was
  dropped.
- `DATEDIFF` in hours or longer between values in two zones gave a wrong
  count: 20 hours for 7.
- `COUNT(*)` in a model of one dataset failed in every question.

## [0.9.1] - 2026-10-21

### Changed

- The SQL sets `transform_null_in = 0`, so a NULL key matches nothing in
  `IN` even when the user's profile sets 1.

### Added

- A row filter on a dataset whose rows reference the root's is a
  semi-join (#10): `-m sessions -f "purchases.revenue > 0"` counts each
  session with such a purchase once. It was refused
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md)).

## [0.9.0] - 2026-10-21

### Changed

- **Breaking:** a function outside the spec's catalog reaches ClickHouse as
  written (#35). Other engines' spellings SQLGlot used to translate
  (`ARG_MAX`, `COUNT_IF`, `LEN`) no longer become `argMax`, `countIf`,
  `CHAR_LENGTH`: write ClickHouse's name or the spec's
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/functions-pass-through.md)).
- `ANY_VALUE`, `BOOL_AND` and `BOOL_OR` no longer read a joined dataset on
  their own; `ANY_VALUE` still does when a server lists it as `any`.
- `sqlglot>=30.24`.

### Added

- `validate --url` reports a field or metric that calls a function the
  server lacks, with the nearest names, and the executor hides it. It also
  reports a server whose `system.functions` cannot be read.
- An aggregate the server lists outside the spec (`countIf`, `uniq`) is
  planned as an aggregate: a filter on it goes to `HAVING`, and over
  repeated joined rows it is refused unless repeats cannot change it.

### Fixed

- An expression with an alias (`SUM(x) AS total`) or ClickHouse's
  parametric form (`quantile(0.9)(x)`) is hidden and reported by
  `validate`; every question with it failed.
- `DAYOFWEEK(d)` and ClickHouse's `any(x)` run; SQLGlot printed them as
  `DAY_OF_WEEK(d)` and `ANY(x)`, which ClickHouse does not have.

## [0.8.0] - 2026-10-15

### Changed

- `sqlglot>=30.23`; the SQL and the values are unchanged.

### Added

- Every command takes several model files, read as one model (#32); a
  name defined in two files is an error.

### Fixed

- **Breaking:** the `CLICKHOUSE` entry is checked when the model loads
  (#30). A typo in its key or value, text that is not a JSON object, a
  repeated key, a second entry on one dataset or an entry where nothing
  reads it is a model problem; a typo used to change silently whether a
  dataset was read with `FINAL`.
- **Breaking:** a name repeated in another case (`Revenue` and `revenue`)
  is a duplicate, and the model does not load: rename one. One of the two
  was unreachable.

## [0.7.1] - 2026-10-14

### Changed

- **Breaking:** `primary_key: []` and `unique_keys: [[]]` no longer load
  (apache/ossie#574).

### Added

- A window's `ORDER BY` accepts `NULLS FIRST` and `NULLS LAST`
  (apache/ossie#577).

## [0.7.0] - 2026-10-13

### Changed

- **Breaking:** a model fails to load with an unknown property (a typo
  such as `descriptionn`), a property set to null (omit it), a `version`
  missing or other than `0.2.0.dev0`, or a NaN or infinite number
  (apache/ossie#530). These used to be dropped silently.

## [0.6.0] - 2026-10-13

### Changed

- **Breaking:** a direct relationship next to a longer path to the same
  dataset is refused as ambiguous, where 0.5.0 joined along the direct one
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/paths.md)).

### Added

- Joins along a path of several relationships, such as order lines to
  orders to customers.

### Fixed

- Division of a `Decimal` is in `Float64`. Answers change:
  `SUM(refunds.amount) / SUM(orders.amount)` gives 0.0286, was 0.02. A
  division by zero shows NULL instead of failing the query, but inside it
  is infinite, so a positive one passes a filter such as `ratio > 1`
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/division-to-float64.md)).

## [0.5.0] - 2026-10-12

### Changed

- `Time`, `Time64`, `DateTime` and `DateTime64` values read the same in the
  CLI and over MCP, as ClickHouse's text, usable as a filter literal (#40).
  `Result.shown_rows()` gives the text, `Result.types` the types.

### Added

- Metrics of several facts in one question, such as revenue and returns by
  region, are each aggregated on their own and merged on the dimensions.
  They were refused
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/stitching.md)).

### Removed

- **Breaking:** the `ossie-clickhouse` command. Run
  `dactopus-ossie-clickhouse`, and point an MCP client's configuration at
  it.

### Fixed

- An aggregate over no rows is NULL, as the spec says. Answers change: a
  `SUM`, `MIN`, `MAX` or `ANY_VALUE` whose filter matches nothing gave 0;
  counts stay 0.
- Rows that tie on the order are ordered by the dimensions, so a `limit`
  keeps the same rows on every run (#42).

## [0.4.0] - 2026-10-10

Renamed from `ossie-clickhouse` to `dactopus-ossie-clickhouse`.

### Changed

- **Breaking:** the package, the command, the module, the repository and
  the MCP server's name change. To migrate:
  - uninstall `ossie-clickhouse` first, then install
    `dactopus-ossie-clickhouse`: both install an `ossie-clickhouse`
    command, and the other order breaks the new one;
  - import `dactopus_ossie_clickhouse`;
  - run `dactopus-ossie-clickhouse`; the old command works in 0.4.x;
  - in an MCP client's configuration, rename the server and point it at
    the new command.

  `OSSIE_CLICKHOUSE_URL` keeps its name. The interactive page moves to
  <https://dactopus.github.io/dactopus-ossie-clickhouse/>.

## [0.3.1] - 2026-10-10

### Fixed

- **Breaking:** `PERCENTILE_CONT` and `MEDIAN` are exact, as the spec
  requires; ClickHouse's `quantile` samples 8,192 values. Answers change:
  the median of 100,000 values is 49,999.5, was 49,505. They return
  `Float64`, where a `Decimal` column gave a `Decimal`. A `Date`,
  `DateTime` or `String` column is refused; for a median date, use
  `PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ...)`
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/exact-percentiles.md)).
- `PERCENTILE_DISC(...) WITHIN GROUP (...) FILTER (WHERE ...)` runs.
- `PERCENTILE_DISC` with `OVER`, and `FILTER` on an expression that is not
  an aggregate, are rejected when the model is read; ClickHouse refused
  them.

## [0.3.0] - 2026-10-10

### Changed

- `sqlglot>=30.22`; the values are unchanged.

### Removed

- **Breaking:** `translate.CLICKHOUSE`. Generate SQL with
  `dialect="clickhouse"`.

### Fixed

- `DAYOFMONTH(d)` runs.

## [0.2.5] - 2026-10-06

### Changed

- `{"dedup": "final"}` adds `FINAL` whatever the engine; it meant the
  default.

### Fixed

- Replicated and ClickHouse Cloud `ReplacingMergeTree` tables, and
  `Distributed` tables, materialized views and `Merge` tables over one, are
  read with `FINAL` (#29). Answers change: metrics over them counted every
  stored version of a row
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/final.md)).

## [0.2.4] - 2026-10-05

### Changed

- **Breaking:** `--` and `/*` outside quotes in a dataset `source` refuse
  it; quote the part if the table is really named so.

### Added

- A note in model authoring on tables replicated by ClickPipes or PeerDB,
  which keep deleted rows under `FINAL`.

### Fixed

- A `source` with quoted parts (`` `shop`.`orders` ``), as dbt-clickhouse
  writes it, is read as the table it names; the dataset disappeared (#26).
- A `source` with a backslash is refused; a column name with a backslash
  is read as named.

## [0.2.3] - 2026-10-05

### Added

- A diagram of how a question becomes SQL, with an interactive page.

### Fixed

- A compound expression under an operator keeps its precedence. Answers
  change: a metric named in a filter, a computed field inside arithmetic
  and a percentile with an expression as its fraction were read with the
  operators regrouped.

## [0.2.2] - 2026-10-04

### Changed

- A source may be a bare table name, read in the connection's database,
  so one model serves any database.
- `validate --url` and `serve` name the host and database they read.

### Fixed

- A source without a database is introspected where ClickHouse reads it,
  not in `default`, where a copy of the table could be read without
  `FINAL`.

## [0.2.1] - 2026-10-04

### Changed

- The MCP server's instructions include the model's
  `ai_context.instructions`, and tell the agent to state only numbers a
  query returned.

## [0.2.0] - 2026-10-03

### Changed

- **Breaking:** a relationship is joined when its `to_columns` cover a key
  of the target, and `validate` fails on one that covers none or whose
  target declares no key
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/many-to-one.md)).
- **Breaking:** `NULLS FIRST | LAST`, window frames the spec does not
  list, and `IGNORE NULLS` on `LAG`, `LEAD` and `NTH_VALUE`, which
  ClickHouse ignored, are rejected; ClickHouse answered an empty frame
  with 0
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/window-frames.md)).
- NULLs sort last in both directions. Answers change for a running total
  over a key with NULLs, which ascending order put first.
- `sqlglot>=30.21`.

### Fixed

- **Breaking:** a metric that aggregates another dataset's rows over the
  root's, such as the TPC-DS `store_productivity`, which summed each
  store's staff once per sale, is refused and hidden, and so is a bare
  `COUNT(*)` in a model of several datasets
  ([decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/metric-over-other-rows.md)).
- `LAG` and `LEAD` without a default, and `NTH_VALUE` past the frame, return
  NULL, not 0; `FIRST_VALUE` and `LAST_VALUE` respect NULLs. Answers change
  on columns that are not Nullable.
- A rewritten function inside another one is translated in queries too.
- `validate` reports a metric that names an unknown field.

## [0.1.1] - 2026-10-01

### Fixed

- **Breaking:** rewritten functions are checked against the spec's
  signature, and `EXTRACT` and `DATE_PART` accept only the spec's date
  parts; longer forms from other engines lost arguments and returned other
  values.
- A field or metric that does not translate is hidden and named by
  `validate`, instead of stopping the whole model.

## [0.1.0] - 2026-09-30

First release: Ossie schema `0.2.0.dev0`, tested on ClickHouse 26.x with the
TPC-DS reference model.

- Model loading on `apache-ossie`, with checks for unique names,
  relationships that resolve and expressions in a dialect it reads.
- Expressions translated on the SQLGlot AST, the spec's function catalog
  mapped to ClickHouse and checked by value against DuckDB or the spec.
- One `SELECT` per question along many-to-one relationships, `HAVING` for
  filters over metrics, errors that name the nearest known object.
- `FINAL` for `Replacing*` engines and `dictGetOrNull` for single-key
  dictionaries, learned from system tables.
- Access control through ClickHouse, and a policy file that hides more.
- An MCP server over stdio and a CLI: `validate`, `sql`, `query`, `serve`.

[Unreleased]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.9.5...v0.10.0
[0.9.5]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.2.5...v0.3.0
[0.2.5]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.1.0

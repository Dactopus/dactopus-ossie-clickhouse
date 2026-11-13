# Upgrading

To move to a later version, go through the sections of every release in
between, oldest first. A step says what to change in a model, a command
or code that imports the package; an item that starts with "Expect"
names a question that returns another number, so a report built on it
moves. A release not listed here needs only the new version. What each
release changed:
[CHANGELOG.md](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/CHANGELOG.md).

`validate` with `--url` lists every field and metric of a model that the
new version hides, with the reason; run it on each model after an
upgrade.

## 0.17.0

- Write `FALSE` for `x IN ()`, and give `SUM(DISTINCT ...)` its
  argument: a field or metric that writes either is hidden.
- Expect `ROUND` of a `Float64` to round a value exactly halfway away
  from zero: `ROUND(2.5)` is 3, was 2; `ROUND(0.125, 2)` is 0.13, was
  0.12. A `d` read from a column keeps ClickHouse's `round`, which rounds
  a `Float64` tie to even.
- Expect `MOD(a, b)` and `a % b` of a `Decimal` to align the scales:
  `MOD(125.50, 7)` on a `Decimal(18, 2)` is 6.5, was 0.06.

## 0.16.0

- Rename `custom_extensions` to `nonportable_extensions` in a model and
  its datasets, and write the entry's `data` as an object:
  `nonportable_extensions: [{vendor_name: CLICKHOUSE, data: {dedup: none}}]`.
  A model in the old form does not load, and the message says what to
  rename.

## 0.15.0

- Change a model's `version` line to `0.3.0.dev0`; a model that declares
  `0.2.0.dev0` is refused. Nothing else in a model changes.

## 0.14.0

- Ask a metric over several datasets, such as TPC-DS
  `customer_lifetime_value`, only by dimensions and filters every one of
  its datasets reaches. By a field of `store`, `date_dim` or `item`, ask
  a metric of `store_sales` instead, such as
  `COUNT(DISTINCT ss_customer_sk)`.
- Rewrite a model's metric refused as mixed: one with an aggregate over
  several datasets (or a `FILTER` on another) next to one over a dataset
  the first does not read, or a metric over several datasets with a
  window function or a column outside every aggregate. Declare the ratio
  on one dataset, or write two metrics. A `COUNT(*) FILTER (WHERE ...)`
  names no dataset and is refused next to an aggregate of another.
- Name a metric in a question's filter in place of a window metric or an
  aggregate written next to a metric over several datasets, and in place
  of a written filter that mixes aggregates of different datasets
  (`SUM(a.x) / COUNT(DISTINCT b.k) > 5`).
- Rename a function called `AGG` in any capitalization; a field, metric
  or column named `agg` is unaffected.
- Put every field of a model's metric inside an aggregate:
  `SUM(orders.amount) * customers.weight` is refused and hidden.
- Give a question with a window metric a dimension: one with nothing
  that groups rows is refused.
- Expect a model's metric whose aggregates read different datasets to
  compute each over its own dataset's rows: on TPC-DS,
  `customer_lifetime_value` divides by all 100,000 customers and is
  52,624.71, was 57,919.73. A filter comparing metrics of different
  datasets is answered over each one's own rows.
- Expect a window's `ORDER BY ... DESC` without `NULLS FIRST | LAST` to
  sort NULL first: `RANK() OVER (ORDER BY d_year DESC)` ranks the rows
  without a year 1, not last.
- Expect a key stored twice on the one side of a relationship to count
  the root's rows once: with customer C1 written twice,
  `orders.total_revenue` by region gives EAST 350, was 650. An
  expression where two copies of a key could give one result
  (`customers.weight > 0 OR orders.amount > 150`) is refused on any data.
  A large table on the one side costs more: 3.0 s against 0.85 s on 10
  million orders.

## 0.13.0

- Import types from `dactopus_ossie_clickhouse._vendor.ossie`, not
  `ossie`: `load_model` returns the package's own copy of apache-ossie,
  and `Executor` refuses a document built with an installed one.

## 0.11.0

- Remove repeated keys (two `name:` lines in one dataset) and empty
  `sql`, `expression`, `dialect` or `vendor_name` values from a model:
  it no longer loads. The last key used to win without a word.
- Expect `SPLIT_PART` of a NULL string, or with a NULL delimiter, to be
  NULL, as in DuckDB; it was `''`, so a count of the parts counted rows
  without one.

## 0.10.1

- Write `LN(x)` or `LOG(10, x)` for `LOG(x)`, which is refused:
  ClickHouse reads it as the natural logarithm, Postgres and DuckDB as
  base 10.
- Remove the arguments a catalog function used to drop without an error,
  or write the function so it keeps them: `REGEXP_LIKE(s, 'iphone', 'i')`
  ran case-sensitive, `MOD(x, y, z)` as `x % y`, and both are refused
  now. Longer forms that mean the same in ClickHouse (`FLOOR(x, 1)`,
  `LTRIM(s, 'ab')`) keep working.
- Expect `REGEXP_EXTRACT(s, 'a(b.)')` to return the whole match, `abc`;
  it returned the capture group, `bc`.
- Expect `LOG(-2, x)` and `LOG(-10, x)` to be NULL; they were `log2(x)`
  and `log10(x)`.

## 0.10.0

- Make `primary_key`, `unique_keys`, `from_columns` and `to_columns` name
  fields of their dataset, not columns of its source: declare a field
  for each. A model whose key or relationship names anything else no
  longer loads. A key or relationship is hidden from a user who cannot
  read one of its fields.
- Pass a `limit` of zero or more; a negative one is refused.

## 0.9.5

- Write `https://` for a server that takes TLS: the scheme decides on
  every port, and an `http://` URL on 443 or 8443 goes without TLS. A URL
  with another scheme, without one (such as `host:8123`) or without a
  host is refused.

## 0.9.4

- Parse a `String` column in the expression of a field declared
  `DateTime`: a string compared with such a field is a `TIMESTAMP`
  literal and fails against a `String`.
- Expect a string compared with a `DateTime` or `DateTimeTz` field to be
  read in the model's zone, not the column's: in a New York store, the
  orders before `'2026-09-01 00:00:00'` are 3, were 2.
- Expect ClickHouse's `today()`, `yesterday()`, `now()` and `now64()`
  without a zone to be read in the model's zone, on a server in another
  zone.

## 0.9.2

- Expect `CURRENT_DATE`, `CURRENT_TIMESTAMP`, `CURRENT_TIME`, timestamp
  literals and string casts to a timestamp without an offset, and
  `DATEDIFF`, to use the model's zone, UTC by default, on a server
  outside UTC. Set the zone in the model's `CLICKHOUSE` entry
  (`time_zone`) to keep the server's.

## 0.9.1

- Expect a NULL key to match nothing in `IN` when the user's profile
  sets `transform_null_in = 1`.

## 0.9.0

- Write ClickHouse's name or the spec's for a function outside the
  spec's catalog: it reaches ClickHouse as written, so `ARG_MAX`,
  `COUNT_IF` and `LEN` no longer become `argMax`, `countIf` and
  `CHAR_LENGTH`.

## 0.8.0

- Fix the `CLICKHOUSE` entry the loader reports: a typo in its key or
  value, text that is not a JSON object, a repeated key, a second entry
  on one dataset or an entry where nothing reads it stops the model. A
  typo used to change silently whether a dataset was read with `FINAL`.
- Rename one of two names that differ only in case (`Revenue` and
  `revenue`): the model does not load. One of the two was unreachable.

## 0.7.1

- Remove `primary_key: []` and `unique_keys: [[]]`: they no longer load.

## 0.7.0

- Fix what the loader reports: an unknown property (a typo such as
  `descriptionn`), a property set to null (omit it), a `version` missing
  or other than `0.2.0.dev0`, or a NaN or infinite number. These used to
  be dropped silently.

## 0.6.0

- Remove the direct relationship or the longer path to a dataset reached
  both ways: such a question is refused as ambiguous, where 0.5.0 joined
  along the direct one.
- Expect a division of a `Decimal` in `Float64`:
  `SUM(refunds.amount) / SUM(orders.amount)` gives 0.0286, was 0.02. A
  division by zero shows NULL instead of failing the query, but inside it
  is infinite, so a positive one passes a filter such as `ratio > 1`.

## 0.5.0

- Run `dactopus-ossie-clickhouse` in place of `ossie-clickhouse`, and
  point an MCP client's configuration at it: the old command is removed.
- Expect a `SUM`, `MIN`, `MAX` or `ANY_VALUE` whose filter matches
  nothing to be NULL; it gave 0. Counts stay 0.

## 0.4.0

The package was renamed from `ossie-clickhouse` to
`dactopus-ossie-clickhouse`.

- Uninstall `ossie-clickhouse` first, then install
  `dactopus-ossie-clickhouse`: both install an `ossie-clickhouse`
  command, and the other order breaks the new one.
- Import `dactopus_ossie_clickhouse`.
- Run `dactopus-ossie-clickhouse`; the old command works in 0.4.x.
- Rename the server in an MCP client's configuration and point it at the
  new command. `OSSIE_CLICKHOUSE_URL` keeps its name.

## 0.3.1

- Write `PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ...)` for a median
  of a `Date`, `DateTime` or `String` column: `PERCENTILE_CONT` and
  `MEDIAN` refuse them.
- Expect `PERCENTILE_CONT` and `MEDIAN` to be exact and to return
  `Float64`: the median of 100,000 values is 49,999.5, was 49,505, and a
  `Decimal` column no longer gives a `Decimal`.

## 0.3.0

- Generate SQL with `dialect="clickhouse"`: `translate.CLICKHOUSE` is
  removed.

## 0.2.5

- Expect replicated and ClickHouse Cloud `ReplacingMergeTree` tables, and
  `Distributed` tables, materialized views and `Merge` tables over one,
  to be read with `FINAL`: metrics over them counted every stored
  version of a row.

## 0.2.4

- Quote the part of a dataset `source` that holds `--` or `/*` outside
  quotes, if the table is really named so: such a source is refused.

## 0.2.3

- Expect a compound expression under an operator to keep its precedence:
  a metric named in a filter, a computed field inside arithmetic and a
  percentile with an expression as its fraction were read with the
  operators regrouped.

## 0.2.0

- Give each relationship's target a key its `to_columns` cover:
  `validate` fails on a relationship that covers none or whose target
  declares no key.
- Remove `NULLS FIRST | LAST`, window frames the spec does not list, and
  `IGNORE NULLS` on `LAG`, `LEAD` and `NTH_VALUE`: they are rejected.
- Rewrite a metric that aggregates another dataset's rows over the
  root's, such as TPC-DS `store_productivity`, which summed each store's
  staff once per sale, and a bare `COUNT(*)` in a model of several
  datasets: both are refused and hidden.
- Expect NULLs to sort last in both directions: a running total over a
  key with NULLs changes, as ascending order put them first.
- Expect `LAG` and `LEAD` without a default, and `NTH_VALUE` past the
  frame, to return NULL, not 0, and `FIRST_VALUE` and `LAST_VALUE` to
  respect NULLs, on columns that are not Nullable.

## 0.1.1

- Write rewritten functions with the spec's signature, and `EXTRACT` and
  `DATE_PART` with the spec's date parts: longer forms from other
  engines lost arguments, returned other values, and are refused now.

# Changelog

The format follows [Common Changelog](https://common-changelog.org); versions follow [Semantic Versioning](https://semver.org), and while the major version is 0, a minor release may change the Python API. What to change and which answers move when you upgrade: [UPGRADING.md](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md). Why each change was made: [docs/decisions/](https://github.com/Dactopus/dactopus-ossie-clickhouse/tree/main/docs/decisions).

## [0.17.0] - 2026-11-13

_A field or metric that writes `x IN ()` is hidden, and `ROUND` and `MOD` answers change ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0170))._

### Changed

- **Breaking:** Refuse `x IN ()` and `SUM(DISTINCT)` without an argument, as the spec's fixtures require ([#99](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/99))
- Accept `NULLS FIRST | LAST` in `WITHIN GROUP (ORDER BY ...)`, where it changes nothing ([#99](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/99))

### Fixed

- Round a value exactly halfway away from zero in `ROUND`, as the spec requires; answers change ([#99](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/99), [apache/ossie#687](https://github.com/apache/ossie/issues/687), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/round-and-mod.md))
- Align the scales in `MOD(a, b)` and `a % b` of a `Decimal`; answers change ([#99](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/99))

## [0.16.0] - 2026-11-11

_A model with `custom_extensions` no longer loads ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0160))._

### Changed

- **Breaking:** Read the `CLICKHOUSE` entry from `nonportable_extensions`, with `data` an object, as Ossie renamed `custom_extensions` ([#97](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/97), [apache/ossie#522](https://github.com/apache/ossie/issues/522))
- Read a row filter on a dataset the root does not reach through the one fact every linking fact passes through; it was refused ([#98](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/98), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md#through-a-fact-that-links-them))

## [0.15.0] - 2026-11-11

_A model must declare Ossie schema `0.3.0.dev0` ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0150))._

### Changed

- **Breaking:** Support Ossie schema `0.3.0.dev0` and refuse `0.2.0.dev0` ([#92](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/92))
- Require `sqlglot>=30.31` ([#96](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/96))

### Added

- Answer a metric by a field of rows that reference its dataset, each of its rows once per group; it was refused ([#96](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/96), [#10](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/10), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/one-side-by-many-side.md))
- Say on stderr at `serve` how much of the model the connected user is not served, and why; `Executor.withheld()` gives the counts ([#95](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/95))

## [0.14.0] - 2026-11-07

_Some metrics and questions over several datasets are refused, and their answers change ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0140))._

### Changed

- **Breaking:** Refuse a metric over several datasets by a dimension or filter one of its datasets cannot reach; it was answered over the buyers only ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/components.md#reachability))
- **Breaking:** Refuse a model's metric that mixes an aggregate over several datasets with one over a dataset the first does not read, and a metric over several datasets with a window function or a column outside every aggregate ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89))
- **Breaking:** Refuse a window metric or an aggregate in a question's filter next to a metric over several datasets, and a filter that mixes aggregates of different datasets ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89))
- **Breaking:** Refuse a call of `AGG` in any capitalization, as the spec reserves the name ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89), [apache/ossie#662](https://github.com/apache/ossie/issues/662))
- Compute each aggregate of a model's metric over its own dataset's rows, as Layer 3 requires; answers change ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89), [apache/ossie#653](https://github.com/apache/ossie/issues/653), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/components.md))
- Sort NULL as the largest value in a window's `ORDER BY` without `NULLS FIRST | LAST`, as the spec says; answers change ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89), [apache/ossie#587](https://github.com/apache/ossie/issues/587), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/null-placement.md))
- Require `sqlglot>=30.30` ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89))

### Added

- Add `source.column` in a dataset's metric, checked and hidden like a field's column ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89), [apache/ossie#633](https://github.com/apache/ossie/issues/633), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/source-columns.md))

### Fixed

- **Breaking:** Count the root's rows once when a key is stored twice on the one side of a relationship, and refuse an expression where two copies of a key could give one result; answers change ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/key-stored-twice.md))
- **Breaking:** Refuse a question with a window metric and nothing that groups rows; it returned one row per root row ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89))
- **Breaking:** Refuse and hide a model's metric that reads a field outside every aggregate; ClickHouse rejected it unless the field was a dimension ([#89](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/89))

## [0.13.0] - 2026-11-01

_Types come from the package's own copy of apache-ossie ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0130))._

### Changed

- **Breaking:** Carry a copy of apache-ossie at `fab4f63` in the package, so that it can be on PyPI ([#75](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/75), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/vendored-ossie.md))
- Require `sqlglot>=30.27` ([#75](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/75))

### Added

- Publish the package on PyPI: `pip install "dactopus-ossie-clickhouse[mcp]"` ([#76](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/76), [#3](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/3))
- Run the query compliance cases of apache/ossie against ClickHouse, each as the question its `AGG()` query asks ([#74](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/74), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/questions-only.md))

## [0.12.1] - 2026-10-30

### Added

- Answer a filter on a dataset that meets the root nowhere through a fact another filter names; without one, the refusal lists the facts to name ([#73](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/73), [#69](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/69), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md#through-a-fact-that-links-them))

## [0.12.0] - 2026-10-27

### Changed

- Require `sqlglot>=30.25`; the SQL changes and the values do not ([#71](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/71))

## [0.11.0] - 2026-10-27

_A model file with a repeated key or an empty expression no longer loads, and `SPLIT_PART` answers change ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0110))._

### Changed

- **Breaking:** Refuse a model file with a repeated key or an empty `sql`, `expression`, `dialect` or `vendor_name` ([#70](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/70), [apache/ossie#618](https://github.com/apache/ossie/issues/618))

### Added

- Answer a row filter on a dataset that meets the root at a dataset both reach as a semi-join through it ([#70](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/70), [#69](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/69), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md#through-a-dataset-both-reach))
- Read an expression's `sql`, the spec's portable form, before an `OSSIE_SQL_2026` entry and `ANSI_SQL` ([#70](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/70), [apache/ossie#617](https://github.com/apache/ossie/issues/617))

### Fixed

- Return NULL from `SPLIT_PART` of a NULL string or with a NULL delimiter, as DuckDB does; answers change ([#70](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/70))

## [0.10.1] - 2026-10-25

_`LOG(x)` and catalog calls that lost arguments are refused, and `REGEXP_EXTRACT` and `LOG` answers change ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0101))._

### Changed

- **Breaking:** Refuse `LOG(x)`, which engines read in different bases ([#68](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/68))

### Fixed

- **Breaking:** Refuse the arguments fourteen catalog functions dropped without an error ([#68](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/68), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/catalog-signatures.md))
- Return the whole match from `REGEXP_EXTRACT`, as the spec says; answers change ([#68](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/68))
- Run `LOG(base, x)` for any base; answers change ([#68](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/68))
- Say that a field asked for as a metric is a field, and list the metrics that read it ([#67](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/67), [#66](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/66))
- Return up to 20 metrics from `search_model` apart from datasets and fields, so a broad word keeps them ([#67](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/67))

## [0.10.0] - 2026-10-23

_Keys and relationships name fields, and a negative `limit` is refused ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#0100))._

### Changed

- **Breaking:** Name fields in `primary_key`, `unique_keys`, `from_columns` and `to_columns`, not columns of the source ([#63](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/63), [apache/ossie#486](https://github.com/apache/ossie/issues/486), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/keys-name-fields.md))
- **Breaking:** Refuse a negative `limit`, which ClickHouse read as the last rows ([#65](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/65))
- Report a failure to connect in one line naming the step, the location without credentials and what to check, as `executor.ConnectError` ([#65](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/65), [#62](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/62))

### Added

- Add metrics declared on a dataset, asked for as `dataset.metric` ([#64](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/64), [apache/ossie#591](https://github.com/apache/ossie/issues/591))
- Return `truncated` from the MCP `query` tool and `Result.truncated` when `limit` cut off rows; the CLI notes it on stderr ([#65](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/65))

### Fixed

- Refuse, report and hide a metric with an aggregate over aggregates outside a window; every question with it failed ([#65](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/65))

## [0.9.5] - 2026-10-22

_The URL's scheme alone decides whether to connect with TLS ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#095))._

### Fixed

- **Breaking:** Connect with TLS whenever the URL says `https://`, on any port; an `https://` URL on another port than 443 or 8443 sent the password in the clear ([#61](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/61), [#60](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/60))

## [0.9.4] - 2026-10-22

_Times compared with a string, and `today()` and `now()`, are read in the model's zone ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#094))._

### Added

- Return `today` and `now` from `list_model`, in the zone in effect, by the server's clock ([#57](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/57), [#56](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/56))

### Fixed

- **Breaking:** Read a string compared with a `DateTime` or `DateTimeTz` field as a `TIMESTAMP` literal in the model's zone, not the column's; answers change ([#59](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/59), [#58](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/58), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/time-zone.md))
- Read ClickHouse's `today()`, `yesterday()`, `now()` and `now64()` without a zone in the model's zone; answers change on a server in another zone ([#57](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/57), [#56](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/56))
- Ask agents in the server's instructions to write today as `CURRENT_DATE` or `CURRENT_TIMESTAMP`; they wrote the date they knew ([#57](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/57), [#56](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/56))

## [0.9.3] - 2026-10-22

### Added

- Add `--time-zone`, `OSSIE_CLICKHOUSE_TIME_ZONE` and `load_model(time_zone=...)` to replace the model's zone, so one model serves stores in several zones ([#55](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/55), [#54](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/54))

## [0.9.2] - 2026-10-22

_The current time and timestamp literals are in the model's zone, UTC by default ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#092))._

### Changed

- Read `CURRENT_*`, timestamp literals, string casts to a timestamp without an offset and `DATEDIFF` in the model's zone, not the server's; answers change on a server outside UTC ([#53](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/53), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/time-zone.md))
- Show a `DateTime` with its offset from UTC in the CLI and the MCP server ([#53](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/53))

### Added

- Add `time_zone` to the model's `CLICKHOUSE` entry, the zone of its dates ([#53](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/53), [#52](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/52))
- Answer a metric that reads no dataset, such as `CURRENT_TIMESTAMP`, without `FROM` ([#53](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/53), [#52](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/52))

### Fixed

- Match a `DateTime` value filtered back as `TIMESTAMP '...'` when the column's zone differs from the server's, and keep its fraction of a second ([#53](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/53))
- Count `DATEDIFF` in hours or longer right between values in two zones ([#53](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/53))
- Answer `COUNT(*)` in a model of one dataset ([#53](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/53))

## [0.9.1] - 2026-10-21

_A NULL key matches nothing in `IN` whatever the user's profile sets ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#091))._

### Changed

- Set `transform_null_in = 0` in the SQL ([#51](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/51))

### Added

- Answer a row filter on a dataset whose rows reference the root's as a semi-join, each root row once; it was refused ([#51](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/51), [#10](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/10), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/semi-joins.md))

## [0.9.0] - 2026-10-21

_Functions outside the spec reach ClickHouse as written ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#090))._

### Changed

- **Breaking:** Send a function outside the spec's catalog to ClickHouse as written, so other engines' spellings are no longer translated ([#50](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/50), [#35](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/35), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/functions-pass-through.md))
- Stop `ANY_VALUE`, `BOOL_AND` and `BOOL_OR` reading a joined dataset on their own; `ANY_VALUE` still does when a server lists it as `any` ([#50](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/50))
- Require `sqlglot>=30.24` ([#50](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/50))

### Added

- Report in `validate --url` a field or metric that calls a function the server lacks, with the nearest names, and hide it ([#50](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/50))
- Plan an aggregate the server lists outside the spec (`countIf`, `uniq`) as an aggregate ([#50](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/50))

### Fixed

- Hide and report an expression with an alias or ClickHouse's parametric form; every question with it failed ([#50](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/50))
- Run `DAYOFWEEK(d)` and ClickHouse's `any(x)` ([#50](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/50))

## [0.8.0] - 2026-10-15

_The `CLICKHOUSE` entry is checked, and names that differ only in case are duplicates ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#080))._

### Changed

- Require `sqlglot>=30.23`; the SQL and the values are unchanged ([#46](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/46))

### Added

- Read several model files in every command as one model; a name defined in two files is an error ([#48](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/48), [#32](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/32))

### Fixed

- **Breaking:** Check the `CLICKHOUSE` entry when the model loads; a typo changed silently whether a dataset was read with `FINAL` ([#47](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/47), [#30](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/30))
- **Breaking:** Refuse a name repeated in another case, of which one was unreachable ([#48](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/48))

## [0.7.1] - 2026-10-14

_`primary_key: []` and `unique_keys: [[]]` no longer load ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#071))._

### Changed

- **Breaking:** Refuse an empty `primary_key` or unique key ([#45](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/45), [apache/ossie#574](https://github.com/apache/ossie/issues/574))

### Added

- Accept `NULLS FIRST` and `NULLS LAST` in a window's `ORDER BY` ([#45](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/45), [apache/ossie#577](https://github.com/apache/ossie/issues/577))

## [0.7.0] - 2026-10-13

_A model with an unknown or null property no longer loads ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#070))._

### Changed

- **Breaking:** Load models with apache-ossie's document loaders, which refuse an unknown property, a null one, a missing or other `version` and a NaN or infinite number ([#44](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/44), [apache/ossie#530](https://github.com/apache/ossie/issues/530))

## [0.6.0] - 2026-10-13

_Two paths to one dataset are refused, and divisions of a `Decimal` change ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#060))._

### Changed

- **Breaking:** Refuse a direct relationship next to a longer path to the same dataset as ambiguous ([#43](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/43), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/paths.md))

### Added

- Join along a path of several relationships, such as order lines to orders to customers ([#43](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/43))

### Fixed

- Divide a `Decimal` in `Float64`; answers change ([#43](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/43), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/division-to-float64.md))

## [0.5.0] - 2026-10-12

_The `ossie-clickhouse` command is removed, and aggregates over no rows are NULL ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#050))._

### Changed

- Show `Time`, `Time64`, `DateTime` and `DateTime64` values as ClickHouse's text in the CLI and over MCP, usable as a filter literal ([#41](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/41), [#40](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/40))

### Added

- Answer metrics of several facts in one question, each aggregated on its own and merged on the dimensions; they were refused ([#41](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/41), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/stitching.md))

### Removed

- **Breaking:** Remove the `ossie-clickhouse` command ([#41](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/41))

### Fixed

- Return NULL from an aggregate over no rows, as the spec says; answers change ([#41](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/41))
- Order rows that tie on the order by the dimensions, so a `limit` keeps the same rows on every run ([#41](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/41), [#42](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/42))

## [0.4.0] - 2026-10-10

_The package is renamed to `dactopus-ossie-clickhouse` ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#040))._

### Changed

- **Breaking:** Rename the package, the command, the module, the repository and the MCP server to `dactopus-ossie-clickhouse` ([#37](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/37))

## [0.3.1] - 2026-10-10

_`PERCENTILE_CONT` and `MEDIAN` are exact and refuse dates and strings ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#031))._

### Fixed

- **Breaking:** Compute `PERCENTILE_CONT` and `MEDIAN` exactly, as the spec requires, over numbers only; answers change ([#36](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/36), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/exact-percentiles.md))
- Run `PERCENTILE_DISC(...) WITHIN GROUP (...) FILTER (WHERE ...)` ([#36](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/36))
- Reject `PERCENTILE_DISC` with `OVER`, and `FILTER` on an expression that is not an aggregate, when the model is read ([#36](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/36))

## [0.3.0] - 2026-10-10

_`translate.CLICKHOUSE` is removed ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#030))._

### Changed

- Require `sqlglot>=30.22`; the values are unchanged ([#34](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/34))

### Removed

- **Breaking:** Remove `translate.CLICKHOUSE` ([#34](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/34))

### Fixed

- Run `DAYOFMONTH(d)` ([#34](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/34))

## [0.2.5] - 2026-10-06

_Replicated and Cloud `ReplacingMergeTree` tables are read with `FINAL` ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#025))._

### Changed

- Add `FINAL` with `{"dedup": "final"}` whatever the engine ([#31](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/31))

### Fixed

- Read replicated and ClickHouse Cloud `ReplacingMergeTree` tables, and tables and views over one, with `FINAL`; answers change ([#31](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/31), [#29](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/29), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/final.md))

## [0.2.4] - 2026-10-05

_A `source` with `--` or `/*` outside quotes is refused ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#024))._

### Changed

- **Breaking:** Refuse `--` and `/*` outside quotes in a dataset `source` ([#28](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/28))

### Added

- Document reading tables replicated by ClickPipes or PeerDB, which keep deleted rows under `FINAL` ([#25](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/25))

### Fixed

- Read a `source` with quoted parts, as dbt-clickhouse writes it, as the table it names ([#27](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/27), [#26](https://github.com/Dactopus/dactopus-ossie-clickhouse/issues/26))
- Refuse a `source` with a backslash, and read a column name with one as named ([#28](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/28))

## [0.2.3] - 2026-10-05

_Expressions under an operator keep their precedence ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#023))._

### Added

- Add a diagram of how a question becomes SQL, with an interactive page ([#21](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/21))

### Fixed

- Keep the precedence of a compound expression under an operator; answers change ([#23](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/23))

## [0.2.2] - 2026-10-04

### Changed

- Read a bare table name as a source in the connection's database, so one model serves any database ([#20](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/20))
- Name the host and database that `validate --url` and `serve` read ([#20](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/20))

### Fixed

- Introspect a source without a database where ClickHouse reads it, not in `default` ([#20](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/20))

## [0.2.1] - 2026-10-04

### Changed

- Include the model's `ai_context.instructions` in the MCP server's instructions, and tell the agent to state only numbers a query returned ([#19](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/19))

## [0.2.0] - 2026-10-03

_Relationships need a key, window clauses outside the spec are rejected, and window answers change ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#020))._

### Changed

- **Breaking:** Join a relationship when its `to_columns` cover a key of the target, and fail `validate` on one that covers none ([#16](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/16), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/many-to-one.md))
- **Breaking:** Reject `NULLS FIRST | LAST`, window frames the spec does not list, and `IGNORE NULLS` on `LAG`, `LEAD` and `NTH_VALUE` ([#14](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/14), [#15](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/15), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/window-frames.md))
- Sort NULLs last in both directions; answers change ([#14](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/14))
- Require `sqlglot>=30.21` ([#18](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/18))

### Fixed

- **Breaking:** Refuse and hide a metric that aggregates another dataset's rows over the root's, and a bare `COUNT(*)` in a model of several datasets ([#17](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/17), [decision](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/docs/decisions/metric-over-other-rows.md))
- Return NULL from `LAG` and `LEAD` without a default and `NTH_VALUE` past the frame, and respect NULLs in `FIRST_VALUE` and `LAST_VALUE`; answers change ([#15](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/15))
- Translate a rewritten function inside another one in queries too ([#15](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/15))
- Report in `validate` a metric that names an unknown field ([#17](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/17))

## [0.1.1] - 2026-10-01

_Rewritten functions take only the spec's signature ([Upgrading](https://github.com/Dactopus/dactopus-ossie-clickhouse/blob/main/UPGRADING.md#011))._

### Fixed

- **Breaking:** Check rewritten functions against the spec's signature, and accept only the spec's date parts in `EXTRACT` and `DATE_PART` ([#12](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/12))
- Hide a field or metric that does not translate and name it in `validate`, instead of stopping the whole model ([#12](https://github.com/Dactopus/dactopus-ossie-clickhouse/pull/12))

## [0.1.0] - 2026-09-30

_First release: Ossie schema `0.2.0.dev0`, tested on ClickHouse 26.x with the TPC-DS reference model._

[0.17.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.17.0
[0.16.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.16.0
[0.15.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.15.0
[0.14.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.14.0
[0.13.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.13.0
[0.12.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.12.1
[0.12.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.12.0
[0.11.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.11.0
[0.10.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.10.1
[0.10.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.10.0
[0.9.5]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.9.5
[0.9.4]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.9.4
[0.9.3]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.9.3
[0.9.2]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.9.2
[0.9.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.9.1
[0.9.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.9.0
[0.8.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.8.0
[0.7.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.7.1
[0.7.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.7.0
[0.6.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.6.0
[0.5.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.5.0
[0.4.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.4.0
[0.3.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.3.1
[0.3.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.3.0
[0.2.5]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.2.5
[0.2.4]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.2.4
[0.2.3]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.2.3
[0.2.2]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.2.2
[0.2.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.2.1
[0.2.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.2.0
[0.1.1]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.1.1
[0.1.0]: https://github.com/Dactopus/dactopus-ossie-clickhouse/releases/tag/v0.1.0

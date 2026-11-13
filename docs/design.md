# Design decisions

Decisions that rest on a measurement or an evaluation, each in its own
file under [decisions/](decisions/) with its evidence, so it is not
relitigated without new evidence. A new decision gets a file there and a
line here. What a model author sees of them, with examples, is in
[model-authoring.md](model-authoring.md); the release history is in
[CHANGELOG.md](../CHANGELOG.md).

## Scope and process

- [Questions are the only query input](decisions/questions-only.md): the
  executor compiles questions (metrics, dimensions and filters by name)
  and does not read the spec's relational `AGG()` SQL; the spec's query
  compliance cases are asked as questions.
- [Spec over engine where they disagree](decisions/spec-over-engine.md):
  where DuckDB and the spec differ, a test pins the spec's value with a
  section reference.
- [SQLGlot bounds move only in minor releases](decisions/sqlglot-bounds.md):
  a patch release never changes the `sqlglot` range; a minor release
  raises the lower bound and drops the rewrites it made redundant.
- [apache-ossie is vendored until it reaches PyPI](decisions/vendored-ossie.md):
  PyPI refuses a git dependency, so the package carries an unmodified
  copy.

## Model

- [Keys and relationships name fields](decisions/keys-name-fields.md):
  keys and relationship columns name fields of their dataset, never
  columns of its source (apache/ossie#486).
- [Source columns in a dataset's metric](decisions/source-columns.md):
  a dataset's metric may read its source's columns as `source.column`,
  checked and hidden like a field's column.

## Translator

- [Parsing with SQLGlot's default dialect](decisions/parsing.md): until
  an upstream Ossie parser is on PyPI; the spec's grammar fixtures run
  against it.
- [Functions outside the spec pass through as written](decisions/functions-pass-through.md):
  only the spec's catalog is typed; the server's `system.functions` says
  which other functions exist and which of them aggregate.
- [Catalog calls keep their spec signature](decisions/catalog-signatures.md):
  a rewritten function never drops an argument silently.
- [AGG is reserved](decisions/agg-reserved.md): a call of `AGG` is
  refused in any capitalization.
- [Exact percentiles](decisions/exact-percentiles.md): the spec's
  percentiles are exact, so they do not use ClickHouse's sampling
  `quantile`.
- [Division to `Float64`](decisions/division-to-float64.md): every
  division is in `Float64`, as DuckDB divides, since ClickHouse keeps a
  `Decimal` dividend's scale.
- [ROUND ties away from zero, MOD through intDiv](decisions/round-and-mod.md):
  as the spec fixes them, where ClickHouse rounds a `Float64` tie to even
  and misaligns `Decimal` scales in `%`.
- [NULL placement in ORDER BY](decisions/null-placement.md): a window
  sorts NULL as the largest value, as the spec requires; every other
  `ORDER BY` keeps NULLs last; tied rows are ordered by the dimensions.
- [Window frames limited to the spec's](decisions/window-frames.md): the
  other frames give wrong values in ClickHouse 26.9.

## Planner

- [Many-to-one inferred from keys](decisions/many-to-one.md): a
  relationship is joined only when its `to_columns` cover a key of the
  target, since the spec has no cardinality attribute.
- [A key stored twice on the one side](decisions/key-stored-twice.md):
  ClickHouse does not enforce keys, so a joined subtree is read as
  distinct values where a repeat would change an aggregate; always on, at
  a measured cost.
- [Paths of several relationships](decisions/paths.md): one path to each
  dataset; two are ambiguous (apache/ossie#653 section 4).
- [Refuse a metric over another dataset's rows](decisions/metric-over-other-rows.md):
  an aggregate that would count a joined dataset's rows once per
  reference, or only the referenced ones, is refused.
- [Aggregates over aggregates are refused](decisions/nested-aggregates.md):
  ClickHouse rejects them outside a window, and the spec does not define
  them.
- [Evaluate each aggregate over its own dataset](decisions/components.md):
  a model's metric over several datasets is computed by components, as
  Layer 3 requires.
- [Stitch metrics of several facts](decisions/stitching.md): metrics no
  one root answers together come from their own roots, merged on the
  dimensions.
- [Semi-joins for filters on the many side](decisions/semi-joins.md): a
  row filter on rows that reference the counted ones keeps each counted
  row once, also through a shared dataset or a linking fact.
- [A metric of the one side by the many side](decisions/one-side-by-many-side.md):
  each row of the metric's dataset counts once per group, over a spine of
  distinct keys.
- [A metric that reads no dataset](decisions/metric-without-dataset.md):
  answered without `FROM`.
- [Answers beyond Layer 3](decisions/beyond-layer-3.md): the questions
  apache/ossie#653 refuses that the executor answers, each with one
  meaning.

## ClickHouse

- [`FINAL` instead of `argMax` for `ReplacingMergeTree` tables](decisions/final.md):
  measured faster, and one keyword to generate.
- [The model's time zone, UTC by default](decisions/time-zone.md): today,
  now and timestamp literals are read in the model's zone, which a
  deployment may replace, never in the server's.

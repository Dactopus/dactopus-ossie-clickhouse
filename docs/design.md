# Design decisions

Decisions that rest on a measurement or an evaluation, recorded so they are
not relitigated without new evidence. [AGENTS.md](../AGENTS.md) holds the
rules an agent needs in every session; the detailed rules of the planner
and the translator, with their reasoning, are here.

## `FINAL` instead of `argMax` for `ReplacingMergeTree` tables

Measured on 3.5 million rows with 20% duplicate versions: `FINAL` took
94 ms against 150 ms for an `argMax` rewrite alone, 90 against 125 with a
join. `FINAL` is also one keyword to generate where `argMax` is a subquery
per dataset. `argMax` was not built.

A `Distributed` table, materialized view or `Merge` table is read with
`FINAL` when the table it reads keeps versions. Checked on ClickHouse
26.9: `FINAL` on the first two over a plain `MergeTree` fails with
`ILLEGAL_FINAL`, so it cannot be added blindly; a `Merge` table applies it
to the tables that support it and skips the rest. When the table read is
hidden from the connected user, the dataset is read without `FINAL` and
`validate` reports it: guessing `FINAL` would break queries over plain
tables that work today, and `{"dedup": "final"}` settles it per dataset.
`engine_full` names a `Distributed` or `Merge` table's tables as quoted
strings on 26.1 through 26.9; `system.tables` names a view's target only
from 26.6 on, so the introspection query does not select it.

## Parsing with SQLGlot's default dialect, not the upstream Ossie dialect

apache/ossie PR #222 adds an Ossie dialect to SQLGlot. Evaluated and not
depended on: it parses and validates the spec's SQL, but the mapping to
ClickHouse stays this project's work either way. Switch parsing to it once
it merges; the rewrite layer is unchanged.

## Functions outside the spec pass through as written

`parse()` gives SQLGlot's typed nodes to the spec's function catalog only
(`_CATALOG` in `translate.py`, the tables of `expression_language.md`);
any other function name parses as `exp.Anonymous` and prints as written.
SQLGlot's default dialect knows the functions of many engines, and its
ClickHouse generator renames 135 of them (counted on SQLGlot 30.23):
`DAYOFWEEK(d)` became `DAY_OF_WEEK(d)` and `BOOL_AND(x)` became
`LOGICAL_AND(x)`, neither of which ClickHouse 26.9 has, though it accepts
`DAYOFWEEK` itself as a case-insensitive alias of `toDayOfWeek`. The
query failed and `validate` said nothing (#35). The spec's own rule for
what it does not define is to pass it through ("Dialect Extensions"),
and a name sent as written is what the author can look up in
ClickHouse's documentation.

The cost is SQLGlot's translation of other engines' spellings: `ARG_MAX`,
`COUNT_IF`, `LEN` no longer become `argMax`, `countIf`, `CHAR_LENGTH`.
The spec's aliases (`NVL`, `IFNULL`, `CEILING`, `TRUNCATE`, `CHARINDEX`)
are in the catalog and still translate. `ANY_VALUE`, `BOOL_AND` and
`BOOL_OR` left the planner's list of aggregates that may read a joined
dataset on their own: they are not the spec's, `BOOL_AND` and `BOOL_OR`
never ran, and `ANY_VALUE` runs as ClickHouse's alias of `any`, which
the planner knows as repeat-safe when connected to a server (below).

Whether a pass-through function exists only the server can say.
`Executor` reads `system.functions`, which lists user-defined functions
too, and matches a name as ClickHouse does: as written, or in any case
when the server marks it case-insensitive, and for an aggregate with
combinators after it, each as written (`sumIf` and `uniqExactOrNull`
resolve, `SUMIF` does not; `countIf` is not listed, `count` and the `If`
combinator are). A field or metric calling a function the server lacks
is hidden like one that does not translate and reported by `validate
--url` with the nearest names. Without a server nothing checks it.

The same list tells the planner which pass-through calls aggregate.
`Catalog.aggregate` answers it with the combinator rule above, and the
planner types such a call as `exp.AnonymousAggFunc`, so every check on
aggregates covers it, and a filter on it goes to `HAVING`. Before,
SQLGlot typed a few of them (`countIf`, `argMax`, `corr`) and left the
rest (`sumIf`, `uniq`) unchecked; a metric like `countIf(store.x > 10)`
next to a sales dimension counted each store once per sale. Without the
server's word (the `sql` command without `--url`, or a server whose
`system.functions` cannot be read) the planner asks SQLGlot's list of
ClickHouse aggregates, with the same combinator rule
(`_known_clickhouse_aggregate`), so `countIf` is still an aggregate
there. One the list lacks stays a plain call, and that is about half
of them: 131 of the 259 aggregates ClickHouse 26.9 lists are missing
from SQLGlot 30.23's list, among them `medianExact`, `group_concat`,
`regr_count`, `any_value` and every user-defined aggregate.

One over a joined dataset alone is refused unless repeats cannot change
it. `system.functions` does not say which do, so the planner lists them
(`_CLICKHOUSE_REPEAT_SAFE`), each checked on ClickHouse 26.9 against its
input with rows repeated: the `uniq` family, `any`, `anyLast`, `argMin`,
`argMax`, `groupUniqArray`, `groupBitAnd`, `groupBitOr`, `groupBitmap`,
`singleValueOrNull`, and `min`, `max`, `any_value`, `min_by`, `max_by` in
any case. A combinator keeps that only if it is `If`, `OrNull`,
`OrDefault`, or `Distinct`, which makes any aggregate repeat-safe; names
are split as ClickHouse splits them, longest combinator first. Any other
is repeat-sensitive, as the spec's are.

## SQLGlot bounds move only in minor releases

Some rewrites in `translate.py` cover gaps in SQLGlot's ClickHouse
generator, and fixes for them go upstream. A rewrite left in place after
SQLGlot is fixed does no harm: it runs before generation and yields the
same SQL. Removing it means raising the lower bound on `sqlglot`, and every
raise makes the package harder to install next to tools that pin SQLGlot
narrowly. So patch releases never change the bounds. Each minor release
raises the lower bound to the current SQLGlot release and drops the
rewrites it made redundant; the value tests stay. The upper bound moves to
a new SQLGlot major only after the tests pass on it.

## Spec over engine where they disagree

DuckDB supplies expected values in tests as a second opinion close to the
spec's Postgres-like semantics, not as ground truth. Where DuckDB and the
spec differ, the test pins the spec's value with a section reference:
`REGEXP_REPLACE` replaces every match, `DAYOFWEEK` is ISO (Monday is 1),
`MILLISECOND` is the component, `PERCENTILE_DISC` is the first value whose
cumulative share reaches `p` (ClickHouse's `quantileExact` picks one too
high whenever `p * n` is whole).

## Questions are the only query input

The spec has two query surfaces. The Semantic Query Interface takes a
question: metrics, dimensions and filters by name, what the CLI and the
`query` MCP tool accept. The Relational Query Interface (apache/ossie#354)
takes SQL over the model's datasets as relations, with `AGG(metric)` for
a measure. Decision of 2026-11-01: this executor compiles questions
straight into ClickHouse SQL and does not read `AGG()` SQL. One input
grammar keeps one planner, the one place where every ClickHouse trait
(`FINAL`, dictionaries, the model's zone, semi-joins) is applied; a
second grammar would need the same rules a second time or a rewriter of
layer-2 SQL into questions, which the spec's own reference rewriter
(apache/ossie#502) is meant to be.

Compliance is shown on results. The spec's query compliance cases
(`compliance/query` in apache/ossie, since apache/ossie#632) ship the
SCOTT data, a model with dataset metrics and, per case, an `AGG()` query
with its expected result as standard SQL over literals.
`tests/test_compliance.py` loads the data through DuckDB into ClickHouse,
reads the model as it is, runs each case as the question it asks and
compares by the suite's README: columns by position, rows as multisets,
exact numerics equal, approximate ones within 1e-9. The weekly workflow
runs it on Ossie's `main`, so a case added upstream fails there until its
question is written here. The suite demands standard SQL semantics, which
every generated query already sets: `SUM` over no rows is NULL
(`aggregate_functions_null_for_empty = 1`) and `COUNT` is 0.

## Exact percentiles

The spec's percentile functions are exact, and `APPROX_PERCENTILE` is the
approximate one. ClickHouse names it the other way round: `quantile` and
`median` keep a sample of 8,192 values, exact below that and approximate
above. On ClickHouse 26.9, the median of 2,000,000 values of
`rand64() % 1000000` came out 503,113.5 from `quantile` and 499,986.5
from the exact function.

`PERCENTILE_CONT` and `MEDIAN` are translated to
`quantileExactInclusiveOrNull(p)(x * 1.0)`. `quantileExactInclusive`
interpolates between the two nearest values as Postgres does (2.5 for the
median of 1, 2, 3, 4; `quantileExact` gives 3). It rejects `Decimal`, so
the argument is made `Float64`, which is also what Postgres computes in.
Alone it answers an empty set with `nan`; `OrNull` makes that NULL
while keeping one aggregate, so `FILTER (WHERE ...)` still applies to it.
Exact percentiles hold every value of the group in memory.

Postgres defines `PERCENTILE_CONT` for numbers only. `quantileExactInclusive`
takes a `Date` and answers with a day count (20,455.5 for a median date in
2026), and `toFloat64` would also parse a `String`. Multiplying by `1.0`
makes ClickHouse refuse both, so the query fails instead of returning a
number that is not a date. `PERCENTILE_DISC` takes any sortable type and
returns a date for dates. The translator does not know column types, so
the refusal comes when the query runs, not from `validate`.

## Division to `Float64`

ClickHouse keeps the dividend's scale when it divides a `Decimal`: 10.00 /
350.00 is 0.02, and `SUM(price) / 3` drops digits the same way. Postgres
(`numeric`) and DuckDB (`double`) give 0.0285714. The spec does not say
what type a division returns, so the translator follows the engines the
project compares with: the dividend of every `/` becomes `Float64`, as
DuckDB divides. `AVG` of a `Decimal` is already `Float64` in ClickHouse,
and division of integers too. Division by zero then gives `inf` or `nan`,
which the executor shows as NULL, where a `Decimal` division by zero
failed the whole query. Inside the query the value stays infinite, or
NaN for 0 / 0: `inf` sorts above every number and passes `ratio > 1` in
a filter, `-inf` sorts below, and NaN sorts last in both directions and
fails every comparison, as division of integers always did. DuckDB
gives `inf` too; Postgres refuses the division. The dividend is
`x * 1.0`, as for percentiles: ClickHouse refuses it for a date or a
string, as Postgres does, where `toFloat64` would divide a day count or a
parsed number.

Two alternatives were rejected:

- `Decimal` with a fixed scale, `toDecimal128(a, 10) / b`: the scale has
  no basis in the spec, `int / int` would become a `Decimal`, and ratios
  would differ in type from `AVG`.
- Casting to the metric's declared `datatype`: a rule of this project's
  own on top of the spec, which applies only to a whole metric, not to a
  division inside one or in a filter.

Known mismatch: the spec's own example of a ratio, `avg_orders`
(`SUM(orders.amount) / COUNT(DISTINCT customers.id)`, `core-spec/spec.md`
on apache/ossie `main`), declares `datatype: Decimal`, and the answer is
a float. `datatype` is not checked against results.

## NULL placement in ORDER BY

A window's `ORDER BY` without `NULLS FIRST | LAST` sorts NULL as the
largest value: last for `ASC`, first for `DESC`. Everything else sorts
NULLs last in both directions.

The spec defines the first rule (apache/ossie#587, merged as `30e1e50`;
`expression_language.md`, "Window Functions" and "Window ORDER BY NULL
Placement"): "Without `NULLS FIRST` or `NULLS LAST`, NULL sorts as the
largest value: last for `ASC`, first for `DESC`. This applies to
`OSSIE_SQL_2026` expressions; other dialects keep their own semantics.
Implementations MUST produce this order whatever the native default of
the engine". Before it the spec was silent (apache/ossie#498, and the
closed proposal apache/ossie#246); this executor had chosen NULLs last
both ways, ClickHouse's and DuckDB's default. The window syntax has the
`NULLS` clause since `7496811` (apache/ossie#577).

Engines differ. ClickHouse 26.9 and DuckDB 2.0.0 put NULLs last both
ways; Postgres treats NULL as larger than any value, as the spec does;
SQLGlot's default dialect, which the parser derives from, treats it as
smaller, first ascending. The translator once inherited that last one,
and the planner matched it, so a running total ordered by a key with
NULLs started from their sum: on TPC-DS, with `LEFT JOIN`s, the 129,850
sales without a matching date came first in `cumulative_sales`. DuckDB
disagrees with the spec for `DESC`, so the tests of that case pin their
expected values by hand.

Which expressions it covers. `sql` is the `OSSIE_SQL_2026` form
(`spec.md`: "the top-level `sql` is authoritative and consumers MUST use
it as the OSSIE_SQL_2026 form"), and so is an `OSSIE_SQL_2026` entry in
`dialects`. An `ANSI_SQL` entry is, for the spec, an opaque dialect that
"keeps its own semantics". This project reads it with the same rule all
the same: the SQL standard leaves the placement of NULLs to the
implementation, so the spec's default is a conforming choice, and one
rule keeps the answer of a metric the same when its author moves it
between `sql` and `ANSI_SQL`. An agent's filter is read the same way.

How. ClickHouse supports the clause. Its default, NULLs last both ways
(spec's table "Native defaults"), is the spec's for `ASC`, so `parse()`
sets `nulls_first` on each `DESC` item of a window's `ORDER BY` that has
no clause, and the SQL says `DESC NULLS FIRST`; `ASC` carries nothing.
The spec lets an implementation omit a default the engine already gives
"only if the session cannot change that default, or the implementation
sets it". No ClickHouse 26.9 setting changes the order of NULLs
(`system.settings` has none whose name concerns NULLs and ordering, and
the server has no counterpart of DuckDB's `default_null_order`), and the
planner's `SETTINGS` set none. An explicit `NULLS FIRST | LAST` is
kept as written; SQLGlot 30.27 prints `NULLS FIRST` and drops
`NULLS LAST`, which is ClickHouse's default in both directions. A
canary pins that, and the spec sentence.

The setting is not `NULL_ORDERING` of the parser dialect, which stays
`nulls_are_last`: `nulls_are_large` would also turn every `DESC` the
parser reads into `NULLS FIRST`, where the spec gives no rule. Switching
to the upstream Ossie dialect must keep that: the window rule stays in
`parse()`, and anything else the dialect parses keeps NULLs last.

Out of scope, kept as it was. The rows of a query: the planner builds
the `ORDER BY`, NULLs last in both directions with no clause, which
keeps rows without a value at the end of a top-N list. The merged Layer 3
spec (apache/ossie#653) has no query `ORDER BY` to follow. And
`WITHIN GROUP (ORDER BY ...)`, where the spec's syntax has no clause: it
stays rejected there, and the rewrite of the percentile functions
reads the direction only. Tests pin both.

What changes in an answer: a window metric ordered `DESC` over a key
with NULLs. On TPC-DS, with `LEFT JOIN`s, the sales without a matching
date form a group with a NULL `d_year`, and
`RANK() OVER (ORDER BY date_dim.d_year DESC)` now ranks it 1, where it
ranked it last. `ASC`, such as the running total in `cumulative_sales`,
and an explicit clause give what they gave.

NaN is not NULL and engines disagree on it too: ClickHouse puts NaN after
the values and before NULLs in both directions; DuckDB and Postgres treat
it as larger than any value, so first descending. The executor returns NaN
as `None`, so ClickHouse's order keeps every row without a value at the
end. A test ordering by a key that can be NaN pins its expected value by
hand.

Rows that tie on the ordered keys are then ordered by each dimension not
ordered yet, ascending. ClickHouse returns tied rows in whatever order its
threads produce them, so without this a `limit` could keep different rows
on each run of the same question. The rows are grouped by the dimensions,
so no two of them tie on all of them. Ties are common in stitched answers:
every dimension value missing from the first metric's fact has NULL there
(#42).

## Window frames limited to the spec's

The spec lists three frames: `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT
ROW`, `ROWS BETWEEN n PRECEDING AND n FOLLOWING` and `RANGE BETWEEN
UNBOUNDED PRECEDING AND CURRENT ROW`; its examples also use `n PRECEDING
AND CURRENT ROW`. Accepted: `ROWS` frames that hold the current row, and
that one `RANGE` frame. The others give wrong values in ClickHouse 26.9,
compared with DuckDB on keys with NULLs:

- An empty frame, such as `ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING` on
  the last row, sums to 0 (`AVG` to NaN), not NULL.
- `RANGE` with an offset counts NULL keys as within any offset: on keys
  (1, NULL, 2, NULL, 2, 4), `SUM(v) OVER (ORDER BY k RANGE BETWEEN 1
  PRECEDING AND 1 FOLLOWING)` adds the NULL rows to the row with key 4.

`parse()` rejects other frames and `GROUPS`, so such a field or metric is
hidden and reported by `validate`. Reopen when the translator can rewrite
them, or ClickHouse fixes them.

## Catalog calls keep their spec signature

SQLGlot's translation of fourteen catalog functions dropped the arguments
beyond the spec's signature without an error: `REGEXP_LIKE(s, 'iphone',
'i')` ran as `match(s, 'iphone')`, case-sensitive; `MOD(x, y, z)` as
`x % y`; `LOG10()` as `LOG(10)`; `LENGTH`, `LTRIM`, `RTRIM`, `FLOOR`,
`CEIL`, `CEILING`, `DATE_TRUNC`, `CURRENT_DATE`, `CURRENT_TIMESTAMP`,
`POSITION` and `CHARINDEX` likewise. So a rewritten function, ours or
SQLGlot's, carries its spec signature in `_SIGNATURES`, and `parse()`
refuses other argument counts; `validate` reports a field or metric
written so. A longer form that reaches ClickHouse whole and means there
what it means in DuckDB, Snowflake or Postgres stays, with a value test:
`FLOOR(x, 1)`, `CEIL(x, 1)`, `LTRIM(s, 'ab')`, `RTRIM`,
`CHARINDEX(substr, str, start)`, `POSITION` likewise and `DATE_TRUNC`
with a zone. A name in `_SIGNATURES` parses as a plain call, never
through SQLGlot's special syntax (`CEIL(x TO DAY)`), which skips the
check; `POSITION(substr IN str)` keeps its own, checked in
`_parse_position`. `LOG(x)`, outside the spec, is refused: ClickHouse
reads it as the natural logarithm, Postgres and DuckDB as base 10.

## Many-to-one inferred from keys

The spec has no cardinality attribute. A relationship is used only when its
`to_columns` cover the primary key or a unique key of the target, which is
what makes a `LEFT JOIN` safe against fan-out. A row filter on the many
side of a relationship is a semi-join, and metrics of several facts are
stitched (both below). A metric that reads both sides of a one-to-many
relationship, which symmetric aggregates would answer, waits for demand
(#10).

The spec calls `to_columns` "Primary/unique key columns"; covering, a
superset of a key, is how the reference validator reads it
(apache/ossie#330), and a superset of a unique key is unique too. It
matters for multi-tenant schemas joined on `(tenant_id, customer_id)`
with the key declared as `customer_id`. Such a join to a single-key
dictionary stays a `LEFT JOIN`: `dictGetOrNull` by the key alone would
ignore the extra columns. The cost is memory: ClickHouse (checked on
26.9) runs a join on more than the dictionary key as a hash join that
loads the whole dictionary, not as a direct key lookup.

Two points are stricter than that validator. A relationship whose
`to_columns` cover no key is refused, where the validator only warns. A
target with no declared key is refused too, where the validator skips the
check: nothing then rules out fan-out. The model still loads; questions
that need such a join fail, and `validate` reports the relationship.

## Keys and relationships name fields

apache/ossie#486 (`7205e15`) settled what `primary_key`, `unique_keys`,
`from_columns` and `to_columns` name: fields of their dataset, never
columns of its source. Before it this project read them as columns, which
gave the same SQL only while each key field was named after its column
and read it unchanged. They are now checked against the declared fields
when a model loads, and the planner joins on the fields' expressions, so
a key field may read a column of another name or an expression. A
relationship goes with its fields when the model is trimmed for a user:
a field hidden by grants or a policy file must not reach SQL through a
join.

The pin that brings this also brings dataset-scoped metrics
(apache/ossie#591, `fae8f41`), which the SDK now reads. Until the planner
answers them, a model that declares one is refused: loading it and
answering without its metrics would drop them silently.

## Source columns in a dataset's metric

apache/ossie#633 (`21e849d`; `spec.md`, "Metric Scoping" and "Name
Resolution") lets a dataset's metric read a column of its dataset's
`source` as `source.column`: "`source.quantity` and `SOURCE.quantity` name
the same column. `source.amount` refers to the source column even when the
dataset declares a field named `amount`." An unqualified name in such a
metric "MUST be a declared field of the dataset". On the name `source`:
"`source` is not reserved: a field or dataset may be named `source`. If a
dataset declares a field named `source`, the name `source` refers to that
field, so the dataset's metrics cannot refer to source columns." A model's
metric "cannot reference source columns", and keys and relationship
columns name fields. For a consumer that generates SQL against the source
tables the spec says to replace "each `source.column` reference with
`column`".

How. `Planner._own_field` reads the qualifier case-insensitively. When the
dataset declares no field named `source`, it rewrites `source.column` on
the AST to the column under the dataset's alias, `orders.column`, and
substitutes no field's expression, even for a field of the same name. Only
the qualifier folds case: the column keeps the spelling the expression
gives it, as a field's column does, since ClickHouse's column names are
case-sensitive. When the dataset declares a field named `source`, any
`source.column` is refused with a message that says the field takes
precedence. Every other qualified name in a dataset's metric stays an
error. A `source.column` in a model's metric or in an agent's filter, where
no dataset named `source` exists, is refused with a message that says
source columns belong to a dataset's own metrics; a dataset that is itself
named `source` keeps `source.field` in model metrics and filters. A key or
relationship column `source.x` is not a field name, so loading refuses it.

Access. `validate --url` checks a metric's source columns against
`system.columns` like the columns of fields. A metric over a source column
that the connected user cannot see is hidden from that user (`Executor`
adds it to the hidden metrics), and `restrict` does not read `source.x` as
a field named `x` of a dataset named `source`. The column therefore appears
in no SQL, error or suggestion. Under a policy file, `source.x` counts as
reading every field of the dataset whose expression reads the column `x`, so
hiding any of them hides the metric, while the column's data stays protected
by ClickHouse column grants.

## AGG is reserved

apache/ossie#662 (`9b8f16c`; `expression_language.md`, "Measure
Evaluation: `AGG` (RESERVED)"): "In `OSSIE_SQL_2026` expressions, an
implementation MUST NOT give `AGG` any other meaning. Expressions in other
dialects are not affected. Because function names should be case
insensitive, the reservation covers every capitalization". And:
"`OSSIE_SQL_2026` expressions MUST NOT call `AGG` until a future version
defines where model declarations may use it." A field, metric or column
named `agg` stays valid ("Reserving `AGG` as a function name does not
prevent a field, metric, or column from being named `agg`").

Before this a call of `AGG(x)` parsed as an unknown function and went to
ClickHouse as written, where it fails or, if the server has a function of
that name, means something else. `parse()` now refuses a call of `AGG` in
any capitalization, an `OVER` call too, so the refusal reaches a field and
a metric (hidden and reported by `validate`, as any expression that does
not translate) and an agent's filter, before any SQL is built. Only a
function call is refused: a column, field or metric named `agg` is a name,
not a call.

Which expressions it covers. The same as in "NULL placement in ORDER BY":
`sql`, an `OSSIE_SQL_2026` entry and, because this project reads it by the
same rules, an `ANSI_SQL` entry. The spec's "Expressions in other dialects
are not affected" says the reservation does not bind them, not that
refusing is wrong; the SQL standard has no function `AGG`, so no portable
`ANSI_SQL` expression loses a meaning, and one rule keeps a metric valid
when its author moves it between `sql` and `ANSI_SQL`. The cost is a
ClickHouse user-defined function named `agg` called from an `ANSI_SQL`
entry; the spec's own advice for an older expression is to "write the
expression in a vendor dialect", which this executor does not read, so such
a call cannot be written in a model. Nothing else the spec says gives a
reason to accept it.

## Paths of several relationships

A question may touch a dataset the root reaches only through others, such
as order lines to orders to customers. apache/ossie#653 section 4
resolves it: the one path of many-to-one relationships is used, two or
more are ambiguous (`E_AMBIGUOUS_PATH`), and a path visits no dataset
twice. A dataset no path reaches is `E_NO_PATH` (section 5.3). The
datasets in the middle are joined like the others (the join tree of
section 5.4), so a row filter may read them, and each relationship on the
path must cover a key of its target.

Any second path is ambiguous, a direct relationship next to a longer
path included: section 4 says "even if one path is shorter". The paths
can mean different things: `store_sales` to `date_dim` is the sale's
date, `store_sales` to `store` to `date_dim` could be the store's closing
date. Only paths to datasets the question touches count: two
paths elsewhere in the model do not refuse a question that does not need
them.

The root is a dataset the question names; a dataset it does not name is
never chosen as the root, so dimensions of two dimension tables stay
refused even when a fact reaches both. A dictionary at the end of a path
is read with `dictGetOrNull` on the key column of the dataset before it;
one in the middle is joined, since the next join reads its columns. A
dataset reached through relationships that each cover a key of their from
side is one to one with the root, so its rows do not repeat (next
section).

`tests/test_paths.py` compares answers with DuckDB on data with an
orphan at each step of the path: both land in one NULL group, inner joins
lose them, and planning from the dimension outward shows a region no fact
reaches.

## Refuse a metric over another dataset's rows

A question is one `SELECT` over the root's rows with many-to-one joins,
so a joined row repeats once per root row that references it, and rows
no root row references are absent. Two failures follow, and until this
change both returned a plausible wrong number. The reference TPC-DS model's
`store_productivity`, `SUM(store_sales.ss_ext_sales_price) /
NULLIF(SUM(store.s_number_employees), 0)`, summed each store's staff once
per sale and came out about 458,000 times too small. In a GA4 model,
`sessions` asked next to `revenue` was answered over purchases: 1,800
sessions from Google instead of 102,920.

The planner refuses both after it picks the root, on the rewritten tree
(`APPROX_PERCENTILE` is an aggregate only after rewrite). Since 0.14.0 a
model's metric whose aggregates each read one dataset is not checked this
way: it is split into components ("Evaluate each aggregate over its own
dataset", below), and `store_productivity` is answered. The checks
remain for a metric of one dataset asked over another root, and for an
aggregate whose arguments read several datasets:

- An aggregate whose argument reads joined datasets and no root column is
  refused unless repeats cannot change it (`MIN`, `MAX`, `DISTINCT`,
  `APPROX_COUNT_DISTINCT`), or every dataset it reads is joined on a key
  of the root: a one-to-one extension table, whose rows a root row
  references at most once.
  Everything else, percentiles included, counts as repeat-sensitive. A
  mixed argument such as `SUM(qty * price)` is one value per root row and
  passes; `SUM(CASE WHEN root.x THEN joined.y END)` passes too, though it
  repeats `joined.y`: the per-row expression is the author's.
- An aggregate expression that reads no root column at all is refused even
  when repeat-safe: it would be about the joined rows the root references.
  A one-to-one join does not lift this: the target's unreferenced rows are
  still missing.
- A metric with an aggregate that reads no column (`COUNT(*)`, `COUNT(1)`)
  is refused unless its other aggregates read exactly one dataset. Alone,
  it counts the rows of whatever the question is about: stores when asked
  by `store.s_state`, sales next to `total_sales`. In `SUM(sales.amount) /
  COUNT(*)` it counts sales, the only dataset the root can be. A model
  with one dataset is exempt, and so is a filter written in the question.
  A metric declared on a dataset is exempt too: every aggregate in it,
  `COUNT(*)` included, aggregates that dataset's rows (the planner marks
  each one with it), so it is checked as an aggregate reading that
  dataset.

Only columns inside an aggregate's argument or its `FILTER (WHERE ...)`
count. A window function reads
grouped rows, and its `PARTITION BY` and `ORDER BY` columns are grouping
keys. The check covers selected metrics and aggregate filters, including
metrics named in a filter. The error names what made the root (a metric,
a dimension or a filter) so an agent can split the question.

The cost: a correct question is refused when its intended population is
the root's references, such as distinct users per purchased item through
`sessions.user_id`. The planner cannot tell that from users next to
revenue, where it would be wrong. Layer 3 (apache/ossie#653 section 5.2)
now says which rows a model's metric is computed over: each aggregate
call, the rows of the one dataset its arguments read. A metric declared
on a dataset (apache/ossie#591) has that dataset as its home whatever its
expression.
A question whose metrics no one root answers together is stitched instead
(next sections).

A metric that refuses in every question is hidden from agents like an
untranslatable one, since every attempt would cost a turn, and is
reported by `validate`: a bare `COUNT(*)`, an unknown field, an aggregate
that repeats rows of a dataset next to an aggregate over several
datasets, and a metric of components that has a window function or a
column outside every aggregate.

## Aggregates over aggregates are refused

An aggregate inside another outside a window, `AVG(COUNT(x))`,
`AVG(SUM(x) OVER ())` or one in another's `FILTER`, is
`ILLEGAL_AGGREGATION` in ClickHouse, and the merged specification does not
define it (the closed proposal apache/ossie#246 deferred it). Such a
metric passed `validate` and failed in every question, so the planner
refuses it
(`_nested_aggregate`), `validate` reports it and the metric is hidden, as
is such a filter. `SUM(SUM(x)) OVER ()` is a window over grouped rows
and passes.

## Evaluate each aggregate over its own dataset

A model's metric whose aggregates read different datasets was planned as
one `SELECT` from one root. The reference model's
`customer_lifetime_value`, `SUM(store_sales.ss_ext_sales_price) /
COUNT(DISTINCT customer.c_customer_sk)`, ran over `store_sales LEFT JOIN
customer`, so the denominator counted the customers with a sale. The Layer
3 spec (apache/ossie#653, `core-spec/wide_table_semantics.md`, merged
2026-11-04) names that plan as wrong in section 5.2: on its data
(Appendix B.1) it gives 650 / 3 where the answer is 650 / 4. Each
aggregate call of a model's metric is a component, evaluated over the rows
of its home dataset, the one dataset its arguments read. Components
with the same home form a branch with the question's dimensions and row
filters (sections 5.3 and 5.4); the branches are merged on the dimensions
(next section), and the metric's expression is evaluated over the
component values. The expression is the translator's output as before,
`Division to Float64` included: the aggregates in the rewritten tree are
replaced by the merged values.

On the TPC-DS data in ClickHouse, `customer_lifetime_value` is 52,624.71
(5,262,470,646.52 over 100,000 customers) where 0.13.0 gave 57,919.73
(over the 90,858 customers with a sale). A group of the merge that only the sales
branch has, such as sales with no matching customer, has no customer
count: its component takes the empty-input value 0, the division gives
`inf`, and the executor shows NULL, as for any division by zero.

Cases, against #653:

| Case | Here | #653 |
|---|---|---|
| A model's metric whose aggregates read different single datasets | Components, one branch per home | Section 5.2 |
| A model's metric whose aggregates read one dataset | One branch, planned as a whole: the SQL of 0.13.0 | Same, one component per aggregate in one branch |
| A dataset's metric | One component whatever its expression | Same |
| An aggregate whose arguments read several datasets, such as `SUM(lines.qty * products.price)`, or a `FILTER (WHERE ...)` on another dataset, in a metric whose other aggregates read only datasets it reads | Not split. The metric is planned as a whole, over one join, as in 0.13.0 | Refused, deferred (`E_AMBIGUOUS_MEASURE_GRAIN`) |
| The same, next to an aggregate whose arguments read a dataset it does not read: `SUM(lines.qty * products.price) / COUNT(DISTINCT customers.id)`, `SUM(orders.amount) FILTER (WHERE customers.segment = 'retail') / COUNT(DISTINCT customers.id)` | Refused in every question and reported by `validate` | Refused (`E_AMBIGUOUS_MEASURE_GRAIN`) |
| An aggregate whose arguments read no dataset, `COUNT(*)`, with or without a `FILTER` | Counts the rows of the one dataset that every aggregate of the metric, `FILTER`s included, reads, as before; refused when they read several | Refused (`E_AMBIGUOUS_MEASURE_GRAIN`) |
| A column outside every aggregate, in a metric of components | Refused | Refused (`E_UNAGGREGATED_FINER_GRAIN_REFERENCE`) |
| A column outside every aggregate, in a metric of one dataset | Sent as written, which ClickHouse accepts only for a dimension of the question | Refused |
| A window function in a metric of components | Refused | Refused (`E_DEFERRED_WINDOWED_METRIC`) |
| A window function in a metric of one dataset | Answered, in the same `SELECT` | Refused |
| Aggregates written into a question's filter that would be split or mixed as in a metric, `SUM(a.x) / COUNT(DISTINCT b.k) > 5`, with or without a `COUNT(*)` beside them, or a written aggregate beside a metric of another dataset, `total_sales > COUNT(DISTINCT b.k)` | Refused; name a metric | No such filter |
| Metrics of different datasets compared in one filter, `total_sales > store_count` | Each checked against the root as a selected metric, so stitched or refused as one would be | No such filter |

The rows with a departure share a reason: `query` is not a Layer 3
binding. Its questions carry several filters, aggregate filters, sorting
and limits, which #653 leaves out (sections 3.2 and 3.4, Appendix A), so
the executor answers some questions the spec refuses, where the spec
defers a case or rejects it as unsupported and one join gives the right
value. Where one join would give a wrong value, the metric is refused.
The home of a component comes from its arguments only (section 5.2: the
`FILTER` "does not determine the home dataset"), so a `FILTER` on another
dataset never makes that dataset a home.

A metric with an aggregate over several datasets (or a `FILTER` on another)
and an aggregate over a dataset the first does not read is refused. Section
5.2 rejects the first aggregate as deferred, and the plan 0.13.0 used, one
join of the first's datasets, would count the second's dataset over the
first's rows, the plan section 5.2 names wrong. For example,
`SUM(orders.amount) FILTER (WHERE customers.segment = 'retail') /
COUNT(DISTINCT customers.id)` divided by the customers with an order
(650 / 3 on the data of Appendix B.1). The message suggests a metric
declared on the dataset, or two metrics. "Another aggregate reads a dataset
the first does not" means a dataset outside the datasets the first one's
arguments read: `SUM(lines.qty * products.price) / SUM(lines.qty)` or
`/ MAX(products.price)` stay within them and are planned as before.

### Reachability

Section 5.3: every dimension's dataset, and the filter's, must be
reachable through many-to-one relationships from every component's home.
`customer_lifetime_value` by `store.s_state` or by `date_dim.d_year` was
answered (over the buyers only) and is refused now: `customer` reaches
neither `store` nor `date_dim`. The message names the dimension (or the
filter), the metric, the aggregate and its home, and what would answer the
question: a metric of a dataset that reaches the dimension and that the
metric's other aggregates read, such as `COUNT(DISTINCT ss_customer_sk)`
on `store_sales`. That counts the customers a sale references, which is
the meaning of the question once a sale is the row.

A row filter is planned in each branch as a question of its own, as
stitching does:

- A filter on a dataset the home reaches is a `WHERE` over the branch's
  joins.
- A filter on a dataset whose rows reference the home, the many side, is
  the semi-join of "Semi-joins for filters on the many side".
  `customer_lifetime_value` filtered on `store_sales.ss_quantity > 50`
  divides the sales with that quantity by the customers with at least one
  such sale. #653 refuses this filter with `E_UNSUPPORTED_FILTER_PATH`
  (section 5.3) unless the query or the model asks for the behavior
  (section 9); the executor answers it for every metric, as it did before.
- A filter no branch home reaches in either way is refused with that
  branch's error, which names the filter, the metric and its home.

The errors use no codes. Section 8 asks for the dimension or filter and the
metric that does not reach it, and for a remedy where one exists; the
message has both.

### Components in the merged query

The units of a stitched question are its metrics, and the components of
its split metrics. Units are grouped by the datasets they aggregate across
all metrics (section 5.4), so `customer_lifetime_value` next to
`total_sales` by `customer.c_first_name` is one `SELECT` with two
branches: `store_sales`, which computes both sums, and `customer`, which
counts. A unit's value over no rows (the empty-input value) is computed as
before. A branch is planned without stitching again.

Tests: `tests/test_components.py` runs the plan of section 5.5 written out
as SQL in DuckDB on the data of Appendix B.1 and on a set with a
dimension value in one branch only, a NULL group in two branches and an
empty-input `COUNT`, and compares ClickHouse with it. Where a component is
0 the metric divides by zero, which the two engines answer differently:
the expected value there is written by hand as NULL, what the executor shows.

## Stitch metrics of several facts

When no one root answers a question's metrics together, each group of
metrics is answered from its own root and the results are merged on the
dimensions, as apache/ossie#653 requires in sections 5.4 and 5.5 for the
branches of a Layer 3 query. A group is the metrics whose aggregates read
the same datasets, and the components of a split metric (previous
section) count as metrics of their home. It is planned as a question of
its own with the question's dimensions and row filters: a row filter
applies to every branch (section 5.4). Revenue next to returns by region
was refused; an agent had to ask twice and combine the rows, which the MCP
server's instructions forbid.

The merge is `UNION ALL` of the groups' rows and `GROUP BY` on the
dimensions, where #653 describes a `UNION` of the keys with a `LEFT JOIN`
of each branch on NULL-safe equality (`IS NOT DISTINCT FROM`) and warns
against joining each branch to only one earlier branch. The rows are the
same: every dimension tuple of either side appears once (guarantee S3),
and a NULL dimension value on two sides makes one row. `GROUP BY` gives that
for any number of groups; chained joins would need the keys coalesced
after each one. With no dimensions it leaves one row of totals.

A metric missing from a group shows its value over no rows (section 5.2,
the empty-input value; `core-spec/relational_semantics.md`, section 8.1):
NULL for `SUM`, 0 for `COUNT`, 0 for `COALESCE(SUM(x), 0)`. The planner
computes it by replacing each aggregate with its value over no rows, 0 for
counts and NULL otherwise, and each group marks its rows so the merge can
tell a missing group from a NULL value, the `present` marker of section
5.5. A ClickHouse aggregate outside the spec has no such rule: under
`aggregate_functions_null_for_empty` it gives 0 for `countIf`, `uniq`
and `deltaSum`, `[]` for `groupArray` and `topK`, NULL for most others,
and `system.functions` does not say which. ClickHouse computes it
instead: the metric, or the component, planned on its own as a scalar
subquery over `WHERE FALSE`, which reads no rows and runs under the outer
`SETTINGS`.

Aggregate filters, which #653 does not have (it has no predicates on
metric values), run on the merged rows and may name metrics, selected or
not, and the question's dimensions. Refused: an aggregate function written
into such a filter, and a window metric, which a group would evaluate over
its own rows. A question whose dimension or row filter one group cannot
reach, such as an `orders` field next to a `returns` metric, is refused
with that group's own error, which names the datasets it cannot join; the
single-root error would only say the facts do not join.

`tests/test_stitch.py` compares answers with DuckDB running the `FULL OUTER
JOIN` plan, on the F_PRELUDE data of the Ossie compliance suite
(apache/ossie#237) with one more orphan return. On that data each
plausible wrong plan gives another answer: the dimension's values with
`LEFT JOIN` loses the NULL group, a `FULL OUTER JOIN` by plain equality
splits it in two, and one `SELECT` from orders loses a region and counts
returns once per order.

Refusals no longer advise asking for each metric in its own question: the
advice was given only when each metric planned alone, and such a question
is now stitched.

## Semi-joins for filters on the many side

A row filter on a dataset whose rows reference the root's, such as
`-m sessions -f "purchases.revenue > 0"` (sessions with a purchase of a
value) or `-m device_count -f "readings.reading_date = '2026-10-19'"`
(devices with a reading that day), made the referencing dataset the root,
and the check of the previous sections refused the metric: it would count
each session once per purchase. Models worked around it with a field on
the root (`sessions.has_purchase`), which cannot take the filter's
condition. Two models of dactopus-data-models asked for it (#10).

Such a filter is now a semi-join: `sessions.session_key IN (SELECT
purchases.session_key FROM purchases WHERE purchases.revenue > 0)`. A root
row stays when at least one row of the filtered dataset references it and
passes the filter, and counts once, so a root row never repeats. Layer 3
(apache/ossie#653) does not define it: a filter whose dataset the metric
does not reach is `E_UNSUPPORTED_FILTER_PATH` (section 5.3), and section
9 lets an engine answer it only when the query or the model asks for that
explicitly. Appendix A.1 lists "existence filters" among the future
extensions. This executor answers it for every question, so it departs
from #653 here, as it did before #653 existed. The relational spec (`relational_semantics.md`, section 8.3)
keeps a source row once per group however many rows derive from it, which
is the same count. Whether a later version defines an anti-semi-join, and
how it treats NULL, is open.

The planner tries it only when the question is refused as before. It then
takes the root of the metrics, dimensions and aggregate filters alone; a
row filter on datasets that root reaches stays a `WHERE` over its joins,
and a row filter on datasets it does not reach, one of which reaches it
through many-to-one relationships, becomes the semi-join. A question
planned before plans the same: `-d devices.country -f "readings..."` keeps
readings as its root and the NULL group of readings without a device.
When the root of the metrics does not answer them either, the question is
stitched or refused with the error it got before, which names the root
and why.

- All filters on one dataset M and on datasets M reaches go into one
  subquery joined by `AND`, so one row of M passes all of them, as one
  joined row would: `-f "lines.qty > 1" -f "orders.status = 'paid'"`
  keeps a customer with a paid order that has such a line. M is the
  dataset among those filters that reaches the others. It must be named,
  as the root must (above): a filter on `items` alone, which reaches no
  customer, is refused with the dataset that links them (`lines`) in the
  message, since which fact links two datasets is a choice of meaning.
- The subquery is planned as a question over M: its own `LEFT JOIN`s,
  `FINAL` and dictionaries. M reaches the root along one path of
  relationships (two are ambiguous, section 6.9), with one `IN` per step,
  and a key of several columns is a tuple.
- `NOT` stays inside the subquery: `NOT readings.reading_date = '...'`
  keeps devices that have a reading on another day. A filter that holds on
  a missing row, such as `readings.value IS NULL`, is checked against rows
  that exist. Absence is asked through a field on the root, such as
  `devices.last_reading_at`. A person at the command line can also
  subtract two answers (`device_count` with and without the filter); the
  MCP server's instructions do not suggest it, since they forbid an agent
  to derive numbers.
- A NULL key references nothing: ClickHouse answers `NULL IN (...)` with
  NULL and a tuple with a NULL in it with 0, as SQL does, when
  `transform_null_in` is 0 (checked on 26.9). It is 0 by default, and
  every generated `SELECT` now sets it, since a user's profile may set 1,
  under which a NULL matches a NULL.

Refused: a filter that mixes the root's datasets and M's in one
expression (split it into two filters; an `OR` across the sides cannot be
split), a dataset that both reaches the root and is reached by it
(ambiguous, as before), and two paths from M to the root. A dimension of
M and a metric that reads M, `FILTER (WHERE ...)` included, make M one of
the question's datasets as before: the dimension is refused, and the
metric is answered over M's rows (its per-row expression is the author's,
as in the previous section). Inside a stitched question each group plans
its own semi-joins: `-m device_count -m reading_count -f "readings..."`
filters readings with a `WHERE` and devices with an `IN`.

`tests/test_semi_join.py` compares answers with DuckDB running `EXISTS`, on
data where a customer has two matching orders, one has none, an order and
a line have NULL keys, two filters pass on different orders of one
customer, and an order was updated in a `ReplacingMergeTree`, so a
subquery without `FINAL` counts it. On the GA4 sample loaded with
dactopus-data-models, 4,446 sessions have a purchase with revenue above
0, as a join with `uniqExact` counts them; `sessions.has_purchase` gives
4,848, since 450 purchases carry no revenue. On a Sensor.Community load,
35 of 36 devices have a reading on 2026-10-19.

### Through a dataset both reach

A WooCommerce product is in several categories, modelled as
`product_categories (product_id, category) -> products`; order lines
reach `products` too. "Revenue of the lines in Clothing" has one meaning,
the lines whose product has a Clothing row, each once, but M (the
categories) does not reach the root (the lines), and 0.10.1 refused it
(#69). A model could answer it only through a dataset at the grain of
line and category, which repeats each line once per category and
rewrites past lines whenever the catalog changes.

The filter is now a semi-join through S, the first dataset on M's
relationships that the root reaches: M's subquery keeps S rows, and the
root's path to S adds one `IN` per relationship, as M's path does.

```sql
order_lines.product_id IN (SELECT products.product_id FROM products
  WHERE products.product_id IN (SELECT product_categories.product_id
    FROM product_categories WHERE product_categories.category = 'Clothing'))
```

- S and every dataset on the root's path to it are read, with `FINAL`
  where the table needs it, though `order_lines.product_id IN (SELECT
  product_categories.product_id ...)` would be shorter. A line whose
  product does not exist has no category, as a join from the line would
  find; an order updated in a `ReplacingMergeTree` counts at its latest
  version; and the relationship from the root may name another key of S
  than M's does.
- S is found by walking M's relationships and stopping each path at the
  first dataset the root reaches. Two such datasets are two meanings (a
  line kept through its item or through its customer) and are refused
  with both named, even when one reaches the other: M's row then names
  the second one twice, directly and through the first. So are two paths
  from the root to S (apache/ossie#653 section 4).
- Only a table of S's properties qualifies: on M's path to S, a key of
  each dataset holds its columns to the next (`Planner._not_a_property`).
  `product_categories (product_id, category)` lists properties of a
  product. With `returns (return_id) -> customers` and `orders ->
  customers`, `-m order_count -f "returns.reason = 'damaged'"` would
  count the orders of customers with a damaged return. Someone asking
  that question means the orders returned, and an agent would pass the
  other number on without a word, so it is refused with what to do:
  relate returns to orders, or declare a key of returns that includes
  `customer_id` if each row does describe its customer. A dataset that
  declares no key is refused the same way, since nothing says what its
  rows are. Refused questions can be answered later without breaking an
  answer; the other way round, a wider rule narrowed later would.
- A dimension of M stays refused: by category, a line would count once
  per category.

The data in `tests/test_semi_join.py` has an item with two matching tags,
two tag filters passed by different rows, a tag of an item that does not
exist, a line on the order of a missing customer and NULL keys: a join, a
subquery that skips the shared dataset, and one that skips the orders on
the way each give another answer.

### Through a fact the question names

"Orders with a line in Clothing" has a root two steps from M: orders do
not reach products, and the categories meet nothing orders reach. A fact
reaches both, the order lines, but the fact is a choice of meaning. With
`refund_lines -> orders` and `refund_lines -> products` next to the order
lines, the same words through refunds count the orders where Clothing was
refunded, a smaller number. A planner that took the only fact a model
has would answer the refunds one without a word in a model that relates
only refunds, and adding the order lines later would turn an answer into
a refusal. So the question names the fact with a filter on it, as a
filter on `items` for customers already must (above):

```
-m orders.order_count -f "order_lines.order_id IS NOT NULL" -f "product_categories.category = 'Clothing'"
```

The filter on the lines makes them M of a semi-join to orders, and the
filter on categories joins that subquery as a filter of the question over
the lines, through `products` as in the previous section:

```sql
orders.order_id IN (SELECT order_lines.order_id FROM order_lines
  WHERE order_lines.order_id IS NOT NULL AND order_lines.product_id IN
    (SELECT products.product_id FROM products WHERE products.product_id IN
      (SELECT product_categories.product_id FROM product_categories
        WHERE product_categories.category = 'Clothing')))
```

- One line passes every filter on the lines and has a product in the
  category, as one joined row would: `-f "order_lines.quantity > 1"`
  keeps orders with a line of more than one item in Clothing, not orders
  with a large line and another line in Clothing.
- `order_lines.order_id IS NOT NULL` drops no line that could keep an
  order, since a line without an order keeps none. It does bind: every
  filter on a dataset the lines reach, `orders.status = 'paid'` too, then
  holds for the same line, as a filter on the lines always makes it, and
  an order without lines no longer passes it. A question without the
  fact is refused with each dataset a filter could name to link them, as
  the planner would plan it, with such a filter on each as an example.
  A fact that meets the root at a shared dataset counts too
  (`favorites (customer_id, item_id)` for orders through customers), and
  one with two paths to the root, or one that M meets at two datasets,
  is left out, since naming it would be refused.
- A filter dataset that meets two named facts (`order_lines` and
  `refund_lines` both filtered) is refused with both named, since an
  order could be kept through either. The rules of the previous section
  hold inside the fact's subquery: one meeting dataset, a dataset of
  properties.
- Customers by the categories of their orders' lines work the same way,
  one `IN` more.

`tests/test_semi_join.py` compares these with DuckDB running `EXISTS` on
the data above, which adds an order with a large line of an untagged item
and a small line of a tagged one: a join, a subquery that skips the
products, and one subquery per filter each give another answer.

## The model's time zone, UTC by default

A ClickHouse `DateTime` is an instant; its zone decides how it prints and
which calendar day or hour it falls on. A column without a zone, `now()`,
`today()` and `CAST('...' AS DateTime)` use the server's zone, so the same
question gave different answers on two servers (#52). Measured on
ClickHouse 26.9.5.2 with a server in `Asia/Makassar` (UTC+8), 2026-10-22:

- `CURRENT_DATE` was the server's date: at 20:00 UTC, already tomorrow.
- A value the CLI showed for a `DateTime('UTC')` column, filtered back as
  `TIMESTAMP '2026-10-21 20:00:00'`, matched no row: the cast read it at
  UTC+8.
- `CURRENT_TIMESTAMP` printed the server's wall clock without a zone.

The zone belongs to the model's data: dactopus-data-models computes order
dates in the store's zone and keeps times in UTC. So the model names it
(`time_zone` in its `CLICKHOUSE` entry), UTC when it does not, as Cube,
Malloy, Power BI and Superset default to UTC. `CURRENT_DATE` is
`toDate(now(zone))`, `CURRENT_TIMESTAMP` is `now(zone)`, and a timestamp
literal is `toDateTime('...', zone)` (`toDateTime64` with its fraction,
which the cast dropped). `DATEDIFF` is `dateDiff(unit, a, b, zone)`:
without a zone argument ClickHouse counts in its arguments' zone, and when
they have two it miscounts hours and longer. A reading at 20:00 UTC in a
`DateTime('Asia/Makassar')` column and `03:00+00:00` the next day came out
20 hours and -1 day apart; with the zone, 7 hours and 0 days in New York.
`CURRENT_TIMESTAMP` against a column in UTC on a server outside UTC was
such a pair before this change too. Ossie has no such field;
apache/ossie may get one, and this entry would then read it.

A model shared by deployments cannot name the zone: dactopus-data-models
has one `commerce` model for every store and builds each store's dates
in that store's zone (#54). So the deployment may replace it, as it
names the database in its URL: `--time-zone`, else
`OSSIE_CLICKHOUSE_TIME_ZONE`, else the model's entry, else UTC. The
deployment wins over the model because it knows which zone its data was
built in, and a model can then carry a default. `load_model` writes the
zone into the loaded model's `CLICKHOUSE` entry, so the planner, the
model trimmed to a user's grants, `validate --url` and `list_model` read
it where they read the model's own. A deployment's file holding only
the entry was not possible: every file holds a dataset, and the entry in
two files is refused, since one would be edited and not the other. One
zone per deployment is still one zone for every store a deployment
holds.

Agents still answered "today" for the wrong day (#56). Asked how many
orders were placed today, with the store in `Pacific/Honolulu` and the
agent's clock in `Pacific/Kiritimati` (always a day apart), fresh
`claude -p` sessions on 0.9.3 wrote the date they knew as a literal or
ClickHouse's `today()`: none of three Haiku 5.5 and three Sonnet 5.5
sessions wrote `CURRENT_DATE`, and one answered right, by converting
the date itself. The instructions said only whose today it was, and
"never write SQL" read as a ban on `CURRENT_DATE` in a filter. They now
say to write periods counted from now with `CURRENT_DATE` or
`CURRENT_TIMESTAMP`, never a known date, and that SQL goes in filters;
the `query` tool's description, which an agent reads as it writes the
filter, has a `CURRENT_DATE` example. After that all six sessions wrote
`CURRENT_DATE` and answered 1 order, 137. `list_model` also returns
`today` and `now` in the zone, so an agent names the day it answered
for. They come from the server's clock, as `CURRENT_DATE` does, not from
Python's, which may differ from it, and are read on every call, since a
session may outlast midnight. `today()`, `yesterday()`, `now()` and
`now64()` without a zone, and the aliases `curdate()` and
`localtimestamp()`, are rewritten into the zone too: they are
ClickHouse's own functions, but an answer in the server's zone is the
failure this entry is about, and a zone written in the call is kept.
A metric of the current time in each model was not chosen: every model
would have to carry it, and agents would still have to be told to use
it.

A plain string compared with a time field was still read in the
column's zone (#58), and agents write it as often as `TIMESTAMP`. On
dactopus-data-models' commerce model over an earlier build of its
Shopify fixture (without `sales_events`, so `all_orders` stands in for
`orders`), built in `America/New_York` with every time column in UTC,
orders before midnight of September 1 (order 1003 was placed at 22:00
on August 31 in New York):

| Filter on `orders.order_time` | New York, before | New York, after | UTC, both |
| --- | --- | --- | --- |
| `< TIMESTAMP '2026-09-01 00:00:00'` | 3 | 3 | 2 |
| `< '2026-09-01 00:00:00'` | 2 | 3 | 2 |
| `< '2026-09-01'` | 2 | 3 | 2 |
| `< '2026-09-01 00:00:00+00:00'`, with or without `TIMESTAMP` | 2 | 2 | 2 |
| `< '2026-09-01 00:00:00-04:00'`, with or without `TIMESTAMP` | 3 | 3 | 3 |

A string compared directly with a field or metric whose `datatype` is
`DateTime` or `DateTimeTz` (`=`, `<>`, `<`, `BETWEEN`, `IN` and the
like) is now made a `TIMESTAMP` literal before the field is inlined, so
`rewrite_tree` reads it as one: in the model's zone, an offset kept, a
fraction as `DateTime64`. The model's `datatype` says which fields hold
times, so the SQL is the same without a server, as `sql` shows it. A
field without `datatype` keeps ClickHouse's reading, and so does a
string compared with an expression of a field
(`toDate(orders.order_time) = '2026-09-01'`), whose type the planner
does not know. `Date` and `Time` fields are left alone: a date has no
zone, and the string compared with one is a date already.

Not chosen:

- The column's type from `system.columns` to tell a time field. The SQL
  would differ with and without a server, `TableInfo` would carry types
  it does not need elsewhere, and a field computed from a column would
  still be unknown.
- `session_timezone` on every query. A column without a zone then printed
  in the session's zone while `toDate`, `toHour` and `dateTrunc` of it
  still used the server's (`timeZoneOf` stays the server's), so one row
  showed 12:00 with an hour of 20. ClickHouse marks the setting beta and
  documents that some parsing paths ignore it.
- A literal read in the zone of the column it is compared with.
  ClickHouse does that for a string compared directly with a column, but
  a literal inside `DATEDIFF`, `GREATEST` or on its own has no column.
  Values are shown with their offset instead (`2026-10-21 20:00:00+00:00`),
  which ClickHouse reads back as that instant in a comparison and a cast.
- Other calendar functions of columns in the model's zone. A `Date` column
  refuses a zone argument in `dateTrunc('month', ...)` and `toStartOfMonth`,
  and the planner does not know column types. It can be done without
  them (`toStartOfMonth(toDate(x, zone))`), when the standard's field asks
  for it.

A metric that reads no dataset, such as `CURRENT_TIMESTAMP`, is answered
without `FROM`: one row, or the same value on every row of a question's
dataset; in a stitched question it joins a group of the others, where a
group of its own would add a row for every dimension value. An aggregate
of it (`MAX(CURRENT_TIMESTAMP)`) is still refused in a model of several
datasets: it would aggregate the rows of whichever dataset the question
is answered over.

## Remote MCP server, designed, not built

Streamable HTTP with OAuth 2.1 is built only when a real deployment asks
for it. The design is fixed so the local server does not preclude it:

- The server validates bearer tokens from the organization's identity
  provider through the SDK's token verifier; the SDK serves the RFC 9728
  metadata. The MCP token is never passed to ClickHouse.
- Caller identity maps to a ClickHouse identity behind `client_factory`,
  the one seam that decides which connection a caller gets. Two
  strategies: token exchange for a ClickHouse-audience JWT (ClickHouse
  Cloud JWT authentication, or self-hosted builds that verify JWTs), or
  role switching, where a service user holds every role and each request
  enables only the roles mapped from the token's groups. Role switching
  works with any ClickHouse; grants, row policies and model trimming all
  follow the active roles.

## apache-ossie is vendored until it reaches PyPI

PyPI refuses an upload whose dependencies include a direct reference
(`name @ git+https://...`), and apache-ossie was one: it is not on PyPI
(checked 2026-11-01), and Ossie's first release has not been voted yet.
Even then, an ASF release is a source release, and publishing to PyPI is
optional. Until apache-ossie is on PyPI, this package carries a copy, so
that it, and the runner that depends on it, can go to PyPI.

- **What is copied.** `python/src/ossie` at one commit (three modules, 593
  lines at `fab4f63`) with the repository's `LICENSE` and `NOTICE`.
  `_vendor/vendor.txt` names the commit. Its dependencies, pydantic and
  PyYAML, are direct dependencies here.
- **The one change.** `scripts/vendor_ossie.py` makes the package's
  imports of itself relative, so no code of an `ossie` installed
  next to it is imported (`tests/test_model.py` checks that). CI runs the script with
  `--check`, which fails on any other difference from upstream.
- **What users see.** `load_model` returns
  `dactopus_ossie_clickhouse._vendor.ossie.OssieDocument`, a class distinct
  from an installed apache-ossie's. `Executor` refuses a document of that
  other class with a `TypeError`: `model.py` tells datasets from the model
  with `isinstance`, so it would drop a dataset's `dedup` override and
  report a false error.
- **The weekly run.** `latest.yml` vendors Ossie's `main` before its tests
  and canaries, where it used to install it.

Not chosen:

- **A package of our own on PyPI with the copy.** One more package to
  release, and "Ossie" in its name is the ASF's mark.
- **Our own model classes.** They would drift from the spec's.
- **Relative imports upstream.** apache/ossie has no relative import
  anywhere, and the change would serve only those who vendor; here it is
  two lines of the script.

When apache-ossie is on PyPI, the dependency returns and `_vendor` goes.

# Design decisions

Decisions that rest on a measurement or an evaluation, recorded so they are
not relitigated without new evidence. The rules themselves are in
[AGENTS.md](../AGENTS.md); the reasoning is here.

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
there; only one the list lacks, such as a user-defined aggregate, stays
a plain call.

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

## NULLs sort last in both directions

The spec does not define where NULLs go in `ORDER BY` when the author
does not say (apache/ossie#498). Its window syntax has
`ORDER BY order_expr [ASC|DESC] [NULLS {FIRST|LAST}]` since apache/ossie
`7496811` (apache/ossie#577); before that it had no `NULLS` clause at all
(checked in `expression_language.md`, `spec.md` and `spec.yaml` at
apache/ossie b6c702e). Engines differ:
ClickHouse 26.9 and DuckDB 1.5.6 put NULLs last both ways; Postgres treats
NULL as larger than any value, last ascending and first descending;
SQLGlot's default dialect, which the parser derives from, treats it as
smaller, first ascending. The translator used to inherit that last one,
and the planner matched it, so a running total ordered by a key with NULLs
started from their sum: on TPC-DS, with `LEFT JOIN`s, the 129,850 sales
without a matching date came first in `cumulative_sales`.

Chosen: NULLs last both ways when the clause is absent. It is
ClickHouse's default, so the generated SQL carries no `NULLS` clause, and
it agrees with DuckDB, the reference engine in tests, so no expected value
needs pinning. For a query's `order_by` it also keeps rows without a value
at the end of a top-N list in either direction. The parser dialect sets
`NULL_ORDERING`; the Ossie dialect from apache/ossie PR #222 inherits
SQLGlot's default, so switching to it must keep the setting. Revisit when
apache/ossie#498 settles the default.

An explicit `NULLS FIRST | LAST` in a window's `ORDER BY` is passed
through as written; the spec places NULLs by it regardless of direction,
as ClickHouse does. SQLGlot 30.22.0 keeps `NULLS FIRST` and drops
`NULLS LAST`, ClickHouse's default in both directions. The clause stays
rejected in `WITHIN GROUP (ORDER BY ...)`, where the spec's syntax has
none. A query's `order_by` has no clause either.

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

## Many-to-one inferred from keys

The spec has no cardinality attribute. A relationship is used only when its
`to_columns` cover the primary key or a unique key of the target, which is
what makes a `LEFT JOIN` safe against fan-out. Star schemas need nothing
more; one-to-many questions wait for demand. Metrics of several facts are
stitched (below).

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

## Paths of several relationships

A question may touch a dataset the root reaches only through others, such
as order lines to orders to customers. apache/ossie#246 (`cc0d070`)
section 6.9 resolves it: the one path of many-to-one relationships is
used, two or more are ambiguous (`E_AMBIGUOUS_PATH`), none is
`E_NO_PATH`, and a path visits no dataset twice. The datasets in the
middle are joined like the others (section 6.2, datasets involved in a
query), so a row filter may read them, and each relationship on the path
must cover a key of its target.

Any second path is ambiguous, a direct relationship next to a longer
path included, as rule 2 says ("two or more paths"). The table of error
codes under Semantic 5 (section 6.1) says "equally-valid", which could
be read as preferring the shortest path; the planner does not, since the
paths can mean different things: `store_sales` to `date_dim` is the
sale's date, `store_sales` to `store` to `date_dim` could be the store's
closing date. Only paths to datasets the question touches count: two
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
(`APPROX_PERCENTILE` is an aggregate only after rewrite):

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

Only columns inside an aggregate's argument or its `FILTER (WHERE ...)`
count. A window function reads
grouped rows, and its `PARTITION BY` and `ORDER BY` columns are grouping
keys. The check covers selected metrics and aggregate filters, including
metrics named in a filter. The error names what made the root (a metric,
a dimension or a filter) so an agent can split the question.

The cost: a correct question is refused when its intended population is
the root's references, such as distinct users per purchased item through
`sessions.user_id`. The planner cannot tell that from users next to
revenue, where it would be wrong. The spec does not say which rows a
metric spanning datasets is computed over; apache/ossie#354 (open) asks
every metric to be grain-safe, and #343 (open) gives metrics a home
dataset, which would replace guessing the home from the expression.
A question whose metrics no one root answers together is stitched instead
(next section); a single metric that reads two datasets is still refused.

A metric that refuses in every question (a repeat-sensitive aggregate over
each of two datasets, a bare `COUNT(*)`, an unknown field) is hidden from
agents like an untranslatable one, since every attempt would cost a turn,
and is reported by `validate`.

## Stitch metrics of several facts

When no one root answers a question's metrics together, each group of
metrics is answered from its own root and the results are merged on the
dimensions, as apache/ossie#246 (`cc0d070`) requires in section 6.2,
step A.7, and section 6.6. A group is the metrics whose aggregates read
the same datasets. It is planned as a question of its own with the
question's dimensions and row filters: a row filter applies to every
metric (step A.6.1). Revenue next to returns by region was refused; an
agent had to ask twice and combine the rows, which the MCP server's
instructions forbid.

The merge is `UNION ALL` of the groups' rows and `GROUP BY` on the
dimensions, where #246 describes a `FULL OUTER JOIN`. The rows are the
same: every group of either side appears (section 6.6, row 3), and a NULL
dimension value on two sides makes one row, as the NULL-safe equality
(`IS NOT DISTINCT FROM`) #246 requires would. `GROUP BY` gives that for
any number of groups; chained joins would need the keys coalesced after
each one. With no dimensions it leaves one row of totals (row 4).

A metric missing from a group shows its value over no rows (section
6.11.5): NULL for `SUM`, 0 for `COUNT`, 0 for `COALESCE(SUM(x), 0)`. The
planner computes it by replacing each aggregate with its value over no
rows, 0 for counts and NULL otherwise, and each group marks its rows so
the merge can tell a missing group from a NULL value. A ClickHouse
aggregate outside the spec has no such rule: under
`aggregate_functions_null_for_empty` it gives 0 for `countIf`, `uniq`
and `deltaSum`, `[]` for `groupArray` and `topK`, NULL for most others,
and `system.functions` does not say which. ClickHouse computes it
instead: the metric planned on its own, as a scalar subquery over
`WHERE FALSE`, which reads no rows and runs under the outer `SETTINGS`.

Aggregate filters run on the merged rows (step A.9) and may name metrics,
selected or not, and the question's dimensions. Refused: an aggregate
function written into such a filter, and a window metric, which #246
evaluates over the merged rows (step A.10) and a group would evaluate
over its own. A question whose dimension or row filter one group cannot
reach, such as an `orders` field next to a `returns` metric, is refused
with that group's own error, which names the datasets it cannot join;
the single-root error would only say the facts do not join.

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

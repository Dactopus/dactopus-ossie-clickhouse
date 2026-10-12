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

## NULLs sort last in both directions

The spec does not define where NULLs go in `ORDER BY`, and its window
syntax (`ORDER BY order_expr [ASC|DESC]`) has no `NULLS FIRST | LAST` for
the model author to say it (checked in `expression_language.md`,
`spec.md` and `spec.yaml` at apache/ossie b6c702e). Engines differ:
ClickHouse 26.9 and DuckDB 1.5.6 put NULLs last both ways; Postgres treats
NULL as larger than any value, last ascending and first descending;
SQLGlot's default dialect, which the parser derives from, treats it as
smaller, first ascending. The translator used to inherit that last one,
and the planner matched it, so a running total ordered by a key with NULLs
started from their sum: on TPC-DS, with `LEFT JOIN`s, the 129,850 sales
without a matching date came first in `cumulative_sales`.

Chosen: NULLs last both ways. It is ClickHouse's default, so the generated
SQL carries no `NULLS` clause, and it agrees with DuckDB, the reference
engine in tests, so no expected value needs pinning. For a query's
`order_by` it also keeps rows without a value at the end of a top-N list
in either direction. The parser dialect sets `NULL_ORDERING`; the Ossie
dialect from apache/ossie PR #222 inherits SQLGlot's default, so switching
to it must keep the setting. An explicit `NULLS FIRST | LAST` is rejected,
as the spec's syntax has none. Reopen if the spec defines NULL ordering.

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
  refused unless repeats cannot change it (`MIN`, `MAX`, `ANY_VALUE`,
  `BOOL_AND`, `BOOL_OR`, `DISTINCT`, `APPROX_COUNT_DISTINCT`), or every
  dataset it reads is joined on a key of the root: a one-to-one extension
  table, whose rows a root row references at most once.
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
the merge can tell a missing group from a NULL value.

Aggregate filters run on the merged rows (step A.9) and may name metrics,
selected or not, and the question's dimensions. Refused: an aggregate
function written into such a filter, and a window metric, which #246
evaluates over the merged rows (step A.10) and a group would evaluate
over its own. A question whose dimension or row filter one group cannot
reach, such as an `orders` field next to a `returns` metric, is refused
with the error of the single-root plan.

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

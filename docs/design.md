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

## NULLs sort last in both directions, except in an explicit `order_by`

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

Chosen for expressions (window `ORDER BY` in fields and metrics): NULLs
last both ways. It is ClickHouse's default, so the generated SQL carries
no `NULLS` clause, and it agrees with DuckDB, the reference engine in
tests, so no expected value needs pinning. The parser dialect sets
`NULL_ORDERING`; the Ossie dialect from apache/ossie PR #222 inherits
SQLGlot's default, so switching to it must keep the setting. An explicit
`NULLS FIRST | LAST` in an expression is rejected, as the spec's syntax
has none.

A query's own `order_by` follows the Layer 3 draft, apache/ossie#246 §5.1
(`cc0d070`), which answers apache/ossie#498 for queries: NULL sorts as the
highest value, last ascending and first descending, Postgres's and
Snowflake's default, and an entry may say `nulls` explicitly. The planner
emits `NULLS FIRST` where ClickHouse's default differs, so the row order
is the same on any engine. Without an `order_by` the planner still sorts
by the first measure descending with NULLs last: #246 leaves the order of
such a query to the engine, and an explicit `NULLS LAST` keeps rows
without a value at the end of a top-N list. #246 applies its rule inside
`OVER (...)` too; windows keep NULLs last until the spec settles it, so
the two disagree there. Reopen when #246 or the expression language
merges.

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
query), so `where` may filter them, and each relationship on the path
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

Each aggregate of a metric resolves on its own (apache/ossie#246,
`cc0d070`, section 4.5 form 2, L353: "each operand resolves independently
at the query's grain"). An aggregate whose dataset reaches the question's
dimensions and `where` on its own is computed over that dataset's rows
(section 6.2 step A.6.3), in a group of its own if another aggregate needs
another root, and stitched (next sections). The rules below apply to the
rest: aggregates computed over the question's root because their dataset
cannot reach a dimension or condition, such as
`MAX(store.s_number_employees)` by `item.i_brand`, which store reaches
only through sales.

A `SELECT` over the root's rows with many-to-one joins repeats a joined
row once per root row that references it, and rows no root row references
are absent. Two failures follow, and before these rules both returned a
plausible wrong number. The reference TPC-DS model's
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
keys. The check covers selected metrics and `having` conditions,
including metrics named in one. The error names what made the root (a
metric, a dimension, a `where` or `having` condition) so an agent can
split the question.

The cost: a correct question is refused when its intended population is
the root's references, such as distinct users per purchased item through
`sessions.user_id`. The planner cannot tell that from users next to
revenue, where it would be wrong. #246 is not clear here either: section
6.2 step A.6.4 asks every row of the aggregate's dataset to appear, under
a NULL group when it reaches no group value, and the contract written for
it (L351) is for many-to-many edges; whether a coarser dataset's
aggregate by a finer dataset's dimension follows it is a question for the
spec. apache/ossie#354 (open) asks every metric to be grain-safe, and
#343 (open) gives metrics a home dataset.

The population of an aggregate therefore depends on the question. In
`customer_lifetime_value`, `SUM(store_sales.ss_ext_sales_price) /
COUNT(DISTINCT customer.c_customer_sk)`, the count is of every customer
in a total or by a customer field (TPC-DS: 100,000 customers, 52,624.71
per customer), and of the customers who bought by a store field, which
customer does not reach on its own. Before this change it was the buyers
in every question (90,858, 57,919.73). `MAX(store.x)` next to sales by
`store.s_state` is now the largest of every store in the state, not of
those with sales.

A metric refused in every question is hidden from agents like an
untranslatable one, since every attempt would cost a turn, and is
reported by `validate`. A question with the metric alone is the one most
likely to be answered, since dimensions and `where` only add datasets to
reach, so the check plans that question: an unknown field, a bare
`COUNT(*)` in a model of several datasets, facts that share no dimension
in one metric (`E3013_NO_STITCHING_DIMENSION`), a join every question
needs that the model's keys do not allow. A metric that reads no dataset,
such as `COUNT(*)` in a model of one dataset, needs a dimension, so it is
not checked.

## Stitch metrics of several facts

When no one root answers a question's aggregates together, each group of
aggregates is answered from its own root and the results are merged on
the dimensions, as apache/ossie#246 (`cc0d070`) requires in section 6.2,
step A.7, and section 6.6. A group is the aggregates with the same root:
the dataset an aggregate reads when it reaches the dimensions and `where`
on its own, otherwise the question's root (previous section). It is
planned as a question of its own with the question's dimensions and
`where`, which applies to every aggregate (step A.6.1), and each metric
is computed from its merged aggregates, as section 4.5 form 2 applies the
arithmetic "after the operand aggregates have resolved". Revenue next to
returns by region was refused; an agent had to ask twice and combine the
rows, which the MCP server's instructions forbid. So was sales per
employee, `SUM(store_sales.ss_ext_sales_price) /
NULLIF(SUM(store.s_number_employees), 0)`: by `store.s_state` it is now
the state's sales over the staff of all its stores (TPC-DS: 6 of 12
stores have sales).

The merge is `UNION ALL` of the groups' rows and `GROUP BY` on the
dimensions, where #246 describes a `FULL OUTER JOIN`. The rows are the
same: every group of either side appears (section 6.6, row 3), and a NULL
dimension value on two sides makes one row, as the NULL-safe equality
(`IS NOT DISTINCT FROM`) #246 requires would. `GROUP BY` gives that for
any number of groups; chained joins would need the keys coalesced after
each one. With no dimensions it leaves one row of totals (row 4).

An aggregate missing from a group shows its value over no rows (section
6.11.5): NULL for `SUM`, 0 for `COUNT`, so `COALESCE(SUM(x), 0)` gives 0
and `COUNT(x) / NULLIF(COUNT(y), 0)` gives 0 where only `y` has rows.
Each group marks its rows so the merge can tell a missing group from a
NULL value.

A holistic aggregate (`MEDIAN`, a percentile) in a group is computed once,
over its group's rows at the question's grain: no partial result is
aggregated again, so section 6.1 Semantic 4 (`E_UNSAFE_REAGGREGATION`,
for plans that decompose an aggregate) does not apply.

`having` runs on the merged rows (step A.9) and may name metrics, selected
or not, and the question's dimensions. Refused: an aggregate function
written into `having`, and a window metric, which #246 evaluates over the
merged rows (step A.10) and a group would evaluate over its own. A
question whose dimension or `where` one group cannot reach, such as an
`orders` field next to a `returns` metric, is refused with that group's
own error and code, which name the datasets it cannot join; the
single-root error would only say the facts do not join.

Facts that share no dimension at all are stitched only without
dimensions and only as separate metrics: each one's total in one row, as
§6.6 row 4 reads. With a dimension, one of them cannot reach it, and the
question is refused with `E3013_NO_STITCHING_DIMENSION` (next section);
so is one metric that reads both, as the table of section 6.2 words it
("in the same measure").

`tests/test_stitch.py` compares answers with DuckDB running the `FULL OUTER
JOIN` plan, on the F_PRELUDE data of the Ossie compliance suite
(apache/ossie#237) with one more orphan return and a customer with
neither orders nor returns. On that data each plausible wrong plan gives
another answer: the dimension's values with `LEFT JOIN` loses the NULL
group, a `FULL OUTER JOIN` by plain equality splits it in two, and one
`SELECT` from orders loses a region and counts returns once per order;
for one metric over orders and customers it counts customers once per
order and misses the customer without orders.

Refusals no longer advise asking for each metric in its own question: the
advice was given only when each metric planned alone, and such a question
is now stitched.

## Query shape and refusal codes from the Layer 3 drafts

The query is the aggregation query of apache/ossie#246 §5.1.1 at
`cc0d070`, an open draft of Ossie's Layer 3, and the MCP tool takes it as
one JSON object. Taken before it merges so the shape is tried on a working
executor and the findings go back to the draft (dev@ossie, thread
"Proposal: an MCP server for Ossie", 2026-10-07). The tool is that of the
`execute_query` profile draft, apache/ossie#529 `0.4-draft` at `b5418ee`,
which binds the #246 object to MCP; its schemas are published unchanged
and its offline checker passes on captured replies. The binding, named by
`list_model`, carries both revisions. If a draft changes, the shape
follows it.

Refusals carry #246's code where its trigger matches the planner's
reason, and a common code of the `execute_query` draft (apache/ossie#529)
otherwise: `INVALID_ARGUMENT` for a value its query schema rejects,
`QUERY_INVALID`, `UNSUPPORTED_QUERY`, `SOURCE_UNAVAILABLE`,
`BACKEND_ERROR`. No codes of this project's own: an agent repairs a query
the same way on any engine only if the codes are shared. The planner's
"no join from one root" splits by #246's cases. Facts that share
no stitching dimension (no dataset reachable from each through many-to-one
relationships, §6.8.2), read by one metric or by two measures with a
dimension, are `E3013_NO_STITCHING_DIMENSION`, as Appendix A words it
("two unrelated facts referenced together"): the question pairs every row
of one with every row of the other on any engine, so the agent should ask
it differently. Two measures of such facts without dimensions are
answered with each one's total (previous section). Datasets of a measure and its dimensions or conditions with
no relationship path between them are `E_NO_PATH`. A path the planner
cannot follow (relationships pointing away from every dataset it could
start from) is `UNSUPPORTED_QUERY`, not `E_NO_PATH` or E3013, which would
tell the agent the question is wrong when only this engine cannot answer
it.
`E_PRIMARY_KEY_REQUIRED` stays for a join to a dataset with no declared
key: #246 §4.2 lets an engine require primary keys, so it is not a
planner gap passed off as an invalid question.

The profile's reply is followed whole. Rows go out as embedded CSV, the
profile's own encoding (NULL is an unquoted `\N`, a string starting with
a backslash gets one more), not ClickHouse's CSV output, which quotes the
string `\N` instead and so differs from the profile. Types come from
the ClickHouse column type; a `DateTime` is an instant, so it is
`DateTimeTz`, in ISO 8601 with its offset, where the CLI prints ClickHouse's
text. A value of no profile type, such as a `Time`, goes out as text as
ClickHouse prints it (`-01:02:03`), as in the CLI. `data_source_id` is the model name and a hash of the whole
model: the profile forbids reusing an id for another revision, and an
agent holding an old one is refused with `SOURCE_UNAVAILABLE` and the
current id as a suggestion. Without a `limit`, 100 rows return and the
reply says `truncated` if there were more (one more row is asked for to
tell); a query's own `limit` is its meaning, so that answer is
`complete`.
The preview in `structuredContent` holds the first 10 rows, the profile's
economical default: Claude Code shows an agent both the CSV and
`structuredContent` (measured 2026-10-10), so a full preview would make
it read every row twice.

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

# Evaluate each aggregate over its own dataset

Planned as one `SELECT` from one root, the reference model's
`customer_lifetime_value`, `SUM(store_sales.ss_ext_sales_price) /
COUNT(DISTINCT customer.c_customer_sk)`, runs over `store_sales LEFT JOIN
customer`, so the denominator counts the customers with a sale. The Layer
3 spec (apache/ossie#653, `core-spec/wide_table_semantics.md`, merged
2026-11-04) names that plan as wrong in section 5.2: on its data
(Appendix B.1) it gives 650 / 3 where the answer is 650 / 4. Each
aggregate call of a model's metric is a component, evaluated over the rows
of its home dataset, the one dataset its arguments read. Components
with the same home form a branch with the question's dimensions and row
filters (sections 5.3 and 5.4); the branches are merged on the dimensions
([Components in the merged query](#components-in-the-merged-query)), and the metric's expression is evaluated over the
component values. The expression is the translator's output,
[Division to `Float64`](division-to-float64.md) included: the aggregates in the rewritten tree are
replaced by the merged values.

On the TPC-DS data in ClickHouse, `customer_lifetime_value` is 52,624.71
(5,262,470,646.52 over 100,000 customers) where one join gives 57,919.73
(over the 90,858 customers with a sale). A group of the merge that only the sales
branch has, such as sales with no matching customer, has no customer
count: its component takes the empty-input value 0, the division gives
`inf`, and the executor shows NULL, as for any division by zero.

Cases, against #653:

| Case | Here | #653 |
|---|---|---|
| A model's metric whose aggregates read different single datasets | Components, one branch per home | Section 5.2 |
| A model's metric whose aggregates read one dataset | One branch, planned as a whole | Same, one component per aggregate in one branch |
| A dataset's metric | One component whatever its expression | Same |
| An aggregate whose arguments read several datasets, such as `SUM(lines.qty * products.price)`, or a `FILTER (WHERE ...)` on another dataset, in a metric whose other aggregates read only datasets it reads | Not split. The metric is planned as a whole, over one join | Refused, deferred (`E_AMBIGUOUS_MEASURE_GRAIN`) |
| The same, next to an aggregate whose arguments read a dataset it does not read: `SUM(lines.qty * products.price) / COUNT(DISTINCT customers.id)`, `SUM(orders.amount) FILTER (WHERE customers.segment = 'retail') / COUNT(DISTINCT customers.id)` | Refused in every question and reported by `validate` | Refused (`E_AMBIGUOUS_MEASURE_GRAIN`) |
| An aggregate whose arguments read no dataset, `COUNT(*)`, with or without a `FILTER` | Counts the rows of the one dataset that every aggregate of the metric, `FILTER`s included, reads; refused when they read several | Refused (`E_AMBIGUOUS_MEASURE_GRAIN`) |
| A column outside every aggregate, in a metric of components | Refused | Refused (`E_UNAGGREGATED_FINER_GRAIN_REFERENCE`) |
| A column outside every aggregate, in a model's metric of one dataset | Refused (`validate` reports it), whether or not the question groups by the column | Refused |
| A window function in a metric of components | Refused | Refused (`E_DEFERRED_WINDOWED_METRIC`) |
| A window function in a metric of one dataset | Answered, in the same `SELECT`, over the grouped rows (`SUM(SUM(x)) OVER ()`, `RANK() OVER (ORDER BY SUM(x))`, `RANK() OVER (ORDER BY dimension)`) | Refused |
| The same, in a question with no dimension and no aggregate outside a window (`SUM(x) OVER (ORDER BY k)` alone) | Refused for that question: nothing groups the rows, so the window returns one row per row. The metric stays listed, since a question with a dimension is planned | Refused |
| Aggregates written into a question's filter that would be split or mixed as in a metric, `SUM(a.x) / COUNT(DISTINCT b.k) > 5`, with or without a `COUNT(*)` beside them, or a written aggregate beside a metric of another dataset, `total_sales > COUNT(DISTINCT b.k)` | Refused; name a metric | No such filter |
| Metrics of different datasets compared in one filter, `total_sales > store_count` | Each checked against the root as a selected metric, so stitched or refused as one would be | No such filter |

Why the executor answers where #653 refuses is in [Answers beyond Layer 3](beyond-layer-3.md). The home of a component comes from its arguments only (section 5.2:
the `FILTER` "does not determine the home dataset"), so a `FILTER` on
another dataset never makes that dataset a home.

A metric with an aggregate over several datasets (or a `FILTER` on another)
and an aggregate over a dataset the first does not read is refused. Section
5.2 rejects the first aggregate as deferred, and one join of the first's
datasets would count the second's dataset over the first's rows, the plan
section 5.2 names wrong. For example,
`SUM(orders.amount) FILTER (WHERE customers.segment = 'retail') /
COUNT(DISTINCT customers.id)` divided by the customers with an order
(650 / 3 on the data of Appendix B.1). The message suggests a metric
declared on the dataset, or two metrics. "Another aggregate reads a dataset
the first does not" means a dataset outside the datasets the first one's
arguments read: `SUM(lines.qty * products.price) / SUM(lines.qty)` or
`/ MAX(products.price)` stay within them and are planned over one join.

## Reachability

Section 5.3: every dimension's dataset, and the filter's, must be
reachable through many-to-one relationships from every component's home.
`customer_lifetime_value` by `store.s_state` or by `date_dim.d_year` was
answered (over the buyers only) and is refused now: `customer` reaches
neither `store` nor `date_dim`. The message names the dimension (or the
filter), the metric, the aggregate and its home, and what would answer the
question: a metric of a dataset that reaches the dimension and that the
metric's other aggregates read, such as `COUNT(DISTINCT ss_customer_sk)`
on `store_sales`. That counts the customers a sale references, which is
the meaning of the question once a sale is the row. A whole metric of
`customer`, such as `customer_count`, by a field of `store_sales` is
answered ([A metric of the one side by the many side](one-side-by-many-side.md)); a component is
not, since section 5.3 asks every home of a metric to reach the
dimension.

A row filter is planned in each branch as a question of its own, as
stitching does:

- A filter on a dataset the home reaches is a `WHERE` over the branch's
  joins.
- A filter on a dataset whose rows reference the home, the many side, is
  the semi-join of [Semi-joins for filters on the many side](semi-joins.md).
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

## Components in the merged query

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

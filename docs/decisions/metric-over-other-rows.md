# Refuse a metric over another dataset's rows

A question is one `SELECT` over the root's rows with many-to-one joins,
so a joined row repeats once per root row that references it, and rows
no root row references are absent. Both failures return a plausible
wrong number. Planned over one join, the reference TPC-DS model's
`store_productivity`, `SUM(store_sales.ss_ext_sales_price) /
NULLIF(SUM(store.s_number_employees), 0)`, summed each store's staff once
per sale and came out about 458,000 times too small. In a GA4 model,
`sessions` asked next to `revenue` was answered over purchases: 1,800
sessions from Google instead of 102,920.

A model's metric whose aggregates each read one dataset is split into
components ([Evaluate each aggregate over its own dataset](components.md)), and a metric
of one dataset by a dimension it does not reach is joined to a spine ([A metric of the one side by the many side](one-side-by-many-side.md)). The planner refuses the rest
after it picks the root, on the rewritten tree (`APPROX_PERCENTILE` is an
aggregate only after rewrite): a metric of one dataset asked over another
root, and an aggregate whose arguments read several datasets:

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

Layer 3 (apache/ossie#653 section 5.2) says which rows a model's metric
is computed over: for each aggregate call, the rows of the one dataset
its arguments read. A metric declared on a dataset (apache/ossie#591) has
that dataset as its home whatever its expression. A question whose
metrics no one root answers together is stitched instead.

A metric that refuses in every question is hidden from agents like an
untranslatable one, since every attempt would cost a turn, and is
reported by `validate`: a bare `COUNT(*)`, an unknown field, an aggregate
that repeats rows of a dataset next to an aggregate over several
datasets, a model's metric with a column outside every aggregate, and a
metric of components that has a window function.

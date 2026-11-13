# Answers beyond Layer 3

apache/ossie#653 section 9 lets an engine answer a query the version
rejects "only when the query or the model requests that behavior
explicitly", for example through an engine-defined query property or a
`nonportable_extensions` entry. Otherwise the engine raises the error of
section 8. The rule binds an engine that claims conformance through a
binding. `query` (the API, the CLI and the MCP tool) is not one: it takes
several filters, aggregate filters, sorting and limits, which #653 leaves
out (sections 3.2 and 3.4, Appendix A), and the `execute_query` profile
(apache/ossie#529) is not out. The answers below are therefore extensions
beyond #653, kept as they are because a refusal would gain a user nothing
today: each has one meaning, and the SQL returns it. Decision of
2026-11-06: no behavior changes for #653 where the answer is right, and
this section keeps the choice from being reopened without new evidence.
An answer that could be wrong is refused instead, as #653 refuses it (the
last row).

A future Layer 3 binding (the `execute_query` profile) refuses each of
them unless a query property or a `nonportable_extensions` entry asks for it.
It also carries the section 8 error codes and their order (the table in
section 8: the first row whose condition holds wins). The planner's
messages have no codes today, and nothing orders them by that table. The
examples use the model of Appendix B.1 (`orders` and `returns` relate to
`customers`).

| Question | This executor | #653 |
|---|---|---|
| Dimensions of the one side, a filter on the many side, no metric: `customers.region` with `orders.amount > 100` (Appendix B.5) | The filter's dataset is the root, joined to `customers`, and each region is listed once: EAST and WEST. The rows are the regions with a matching order, plus a NULL group for matching orders that have no customer. A join, not a semi-join: no row repeats, since only the dimension is selected | `E_UNSUPPORTED_FILTER_PATH`: the anchor is `customers`, which cannot reach `orders` (section 5.6) |
| A metric of a dataset, a filter on a dataset it does not reach: `customers.customer_count` with `orders.amount > 100` (section 5.3) | A semi-join: the customers with at least one such order, counted once each (C1 and C3, so 2). See [Semi-joins for filters on the many side](semi-joins.md) and, for a metric of components, [Reachability](components.md#reachability) | `E_UNSUPPORTED_FILTER_PATH` |
| A filter that reads two datasets, `orders.status = 'completed' AND customers.region = 'EAST'`, or two entries of `filters`, one on each dataset (section 3.4) | One `WHERE` over the joins of the metric's root, when that root joins every dataset the filter reads. Refused when one of them could only be searched with a semi-join: the message asks for one filter per side | `E_CROSS_DATASET_FILTER` for the expression. #653 has a single filter, so several entries are a part a binding does not accept (`E_INVALID_QUERY`, sections 3.4 and 3.5) |
| An aggregate or a metric in a filter: `SUM(orders.amount) > 100`, `orders.revenue > 100` (sections 3.2 and 3.4) | `HAVING` over the groups. A written aggregate that would be split or mixed as in a metric is refused; see the table of [Evaluate each aggregate over its own dataset](components.md) | `E_AGGREGATE_IN_WHERE`; predicates on metric values are future (Appendix A.1) |
| Two metrics with the same final name in different datasets: `orders.revenue` and `returns.revenue` by `customers.region` (section 3.3) | Answered, stitched ([Stitch metrics of several facts](stitching.md)). A dataset's metric is named `dataset.metric`, so the columns are `orders.revenue` and `returns.revenue` and never collide. The question has no alias | `E_NAME_COLLISION` unless an entry has an alias |
| A model's metric with an aggregate that reads several datasets, or none, or whose `FILTER` reads another dataset: `SUM(orders.amount * customers.weight)`, `SUM(orders.amount) FILTER (WHERE customers.region = 'WEST')`, `COUNT(*) FILTER (WHERE orders.amount > 100)`, `SUM(orders.amount) / COUNT(*)` (section 5.2) | One `SELECT` over one join rooted at the dataset the metric reads (`orders`); `COUNT(*)` counts the rows of that dataset. Aggregates that need other values of a key stored twice are answered in SELECTs of their own, merged on the dimensions ([A key stored twice on the one side](key-stored-twice.md)). Refused when another aggregate of the metric reads a dataset the first does not, as in `SUM(orders.amount) FILTER (WHERE customers.region = 'WEST') / COUNT(DISTINCT customers.customer_id)`: see the table of [Evaluate each aggregate over its own dataset](components.md) | `E_AMBIGUOUS_MEASURE_GRAIN` |
| A window function over the grouped rows in a model's metric: `RANK() OVER (ORDER BY SUM(orders.amount) DESC)`, `SUM(SUM(orders.amount)) OVER ()` (section 5.2) | Answered in the same `SELECT`, over the grouped rows. The three window metrics of `tests/fixtures/tpcds.yaml`, Ossie's own example, rely on it. Refused when the metric's aggregates read several datasets, in a question that groups nothing, and in a stitched question; see the same table | `E_DEFERRED_WINDOWED_METRIC` |
| An expression that reads two datasets next to a key stored twice: a filter on the root and the one side joined with `AND`, an aggregate over two datasets (`SUM(orders.amount * customers.weight)`) or with a `FILTER` on the one side | Answered as Layer 2 does, in a SELECT of its own when its neighbours need other values. An expression where two copies of a key could give the same result (`customers.weight > 0 OR orders.amount > 150`, `SUM(GREATEST(orders.amount, customers.weight))`) is refused, as #653 refuses it. See [A key stored twice on the one side](key-stored-twice.md) | `E_CROSS_DATASET_FILTER` for the filter; `E_AMBIGUOUS_MEASURE_GRAIN` for the aggregate |
| A metric of a dataset by a field of a dataset whose rows reference it: `customers.customer_count` by a field of `orders` (section 5.3) | Each customer once in each group its orders make, over the distinct pairs of the field's value and customer key; see [A metric of the one side by the many side](one-side-by-many-side.md) | `E_NO_PATH`, or `E_PARTIAL_DIMENSION_REACHABILITY` next to a metric that reaches the field; section 7 sends it to Layer 2 |

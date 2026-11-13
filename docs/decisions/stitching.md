# Stitch metrics of several facts

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

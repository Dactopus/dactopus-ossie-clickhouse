# Aggregates over aggregates are refused

An aggregate inside another outside a window, `AVG(COUNT(x))`,
`AVG(SUM(x) OVER ())` or one in another's `FILTER`, is
`ILLEGAL_AGGREGATION` in ClickHouse, and the merged specification does not
define it (the closed proposal apache/ossie#246 deferred it). Such a
metric passed `validate` and failed in every question, so the planner
refuses it
(`_nested_aggregate`), `validate` reports it and the metric is hidden, as
is such a filter. `SUM(SUM(x)) OVER ()` is a window over grouped rows
and passes.

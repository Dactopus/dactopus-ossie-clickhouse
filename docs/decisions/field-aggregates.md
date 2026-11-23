# Aggregates and windows in a field

`spec.md`, "Fields" and "Expression Object": a field is a row-level
attribute and its expression is scalar, with no aggregation; an
aggregation is a metric. A window function still gives one value for
each row, so the spec allows it in a field (`compliance/portability`
rungs M7 and M8). apache/ossie#792 (`d4dca4e`) made Ossie's validator
check it: `validate_field_aggregates` reports an aggregate without
`OVER` in an `OSSIE_SQL_2026` field, including one in a window's
`PARTITION BY` or `ORDER BY` and one inside the windowed call
(`SUM(SUM(x)) OVER ()`).

## What a field with an aggregate did

Measured on 2026-11-23 against ClickHouse 26.9 with the field
`orders.total_amount: SUM(amount)` and the metric `revenue:
SUM(orders.amount)`, on three orders (DE 10, DE 30, FR 20):

| Question | Before |
|---|---|
| `revenue` by `orders.total_amount` | ClickHouse refuses: `ILLEGAL_AGGREGATION`, aggregate found in `GROUP BY` |
| `revenue` by `orders.country`, filter `orders.total_amount > 15` | DE 40, FR 20: the filter became `HAVING SUM(orders.amount) > 15`, a condition on each group |

The filter is the case that matters: a row filter silently turned into a
condition on groups and answered with another number.

## What is refused, and where

A field whose expression calls an aggregate outside a window is hidden
and reported by `validate`, like a field that does not translate: the
model loads, and every metric and question over the field goes with it.
Refusing the whole model in `check_model` would take every other field
and metric down with one field, where hiding keeps the rest answerable.

- `untranslatable()` refuses the spec's aggregates, with no server.
- The executor adds a function outside the spec that `system.functions`
  says aggregates (`countIf`), as it does for a function the server
  lacks.
- `Planner._field_expr` refuses both for a planner without an executor
  (`sql` without `--url`), since its `_parse` has typed the server's
  aggregates.

The rule reads the expression the executor sends, in any dialect: a
`CLICKHOUSE` text with `sumIf` in a field reaches the same `HAVING`.

## Windows

A field with a window passes `validate`, as the spec allows. A question
cannot use it yet: one `SELECT` computes a window after `WHERE` and
`GROUP BY`, so ClickHouse refuses it in either and inside an aggregate.
`Planner._field_expr` refuses such a field with a message that names it,
where the server's `ILLEGAL_AGGREGATION` came back before. Answering it
needs the window computed over the dataset's rows in a derived table
before the question groups them.

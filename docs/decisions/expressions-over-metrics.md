# Expressions over metrics in a question

An item of a question's `metrics` is a metric name, or an expression over
metric names with the name of its column: `100 * returns / sales AS
return_pct`. A metric name in it may carry a condition: `buyers FILTER
(WHERE customers.number_of_orders > 1) / buyers AS share`. The executor
computes it, so the number of a share, a ratio or a difference comes from
the query. Decision of 2026-11-20.

## Evidence

Until then a question's metrics were names only, and the MCP
instructions told the agent not to derive numbers from results. A share
is a number no single metric of a model holds, so the agent asked for the
parts and divided them itself. Measured on 2026-11-19 on the public
evaluation set `evals/commerce.yaml` of Dactopus/dactopus-data-models, 8 of
whose questions ask for a share:

- With Haiku 5.5 and with Gemma 4 31B, 6 of the 8 failed in every repeat.
- With Qwen3.8-27B, 5 to 6 of the 8 failed in every repeat.
- On 2026-11-11, Haiku 5.5 and Sonnet 5.5 answered 2 of the 8.
- On the question for the share of repeat customers, all seven open
  models of the pilot divided the numbers themselves or substituted
  another metric.
- The wrong numbers of the experiment were the ones an agent computed: a
  sum of 42 rows given as 2,847.46 instead of 2,987.12, and a mean given as
  37.35 instead of 37.34.

Acceptance on the stand of the first version, which divided one metric by
another, showed what the evaluation set mostly asks. Of its share
questions that no metric of the model answers, one divides two different
metrics, four are shares of a subset (repeat customers, 69 of 228;
products ever sold, 16 of 17; revenue from repeat customers; orders with a
discount code), one compares one metric over two periods, and one compares
a group with the total. A question's filter applies to every part, so
`customers.customer_count / customers.customer_count AS share` with the
filter `customers.number_of_orders > 1` returned 1.0 without an error. A
condition on one part answers the four shares of a subset and the two
periods; the division of two metrics needs none; the group against the
total stays out (see Left out).

A rule in the instructions did not close the gap: an agent that is not
allowed to divide has no other way to answer a share, and one that is
allowed divides wrongly.

## Spec basis

Checked on apache/ossie `main` at `344259a`.

- `core-spec/wide_table_semantics.md`, section 3.2, "Query parts":
  "Expressions over metrics (such as Layer 2's `AGG(revenue) - AGG(cost)`),
  row listings, sorting, limits, and predicates on metric values are not
  supported (Appendix A.1)." Appendix A.1 lists "expressions over metrics"
  under "More expressions", as a future extension.
- `core-spec/spec_layers.md`, "Layer 3": "a question that Layer 3 cannot
  express can be answered in Layer 2 without changing the model."

So the question is Layer 2's. This executor's `query` is already beyond
Layer 3 ([Answers beyond Layer 3](beyond-layer-3.md)): it sorts, limits,
takes several filters and filters on metric values. The last row of that table
says what is answered where Layer 3 refuses.
[Questions are the only query input](questions-only.md) keeps its
decision: the executor still reads no `AGG()` SQL, and the question is the
one grammar.

## Decision

Syntax:

- An item is `<expression> AS <name>`, with `AS` in any case and `<name>`
  an unquoted identifier, `[A-Za-z_][A-Za-z0-9_]*`. Any other item is a
  metric name, as before: its SQL and its errors do not change.
- The item is a string in `Query.metrics`, in the MCP `query` tool and in
  `-m` of the CLI `sql` and `query`. `Query` stays a tuple of strings.

Expression:

- Operands are names of the model's metrics, written as a filter names
  them (a model's metric bare, a dataset's as `dataset.metric`, in any
  case), and numeric literals.
- Operators are `+`, `-`, `*`, `/`, unary minus and parentheses. Nothing
  else.

Meaning:

- The value is the one a model's metric would have whose expression is
  this one, with each dataset's metric written `AGG(dataset.metric)` and
  each model's metric replaced by its own expression in parentheses.
- No planning rule is new. Components by home dataset
  ([Evaluate each aggregate over its own dataset](components.md)),
  [stitching](stitching.md), the value over no rows,
  [division to `Float64`](division-to-float64.md), the reachability
  refusals and the row checks all apply as to a model's metric. A
  question's row filters apply to every part of the expression, each part
  in its own branch, so `total_returns / total_revenue` for retail
  customers divides the returns of retail customers by their sales.
- A part with no rows in a group takes its value over no rows: NULL for a
  sum, 0 for a count. A part that is NULL makes the result NULL, as SQL
  arithmetic does.
- Division by zero shows NULL.

A condition on a part, decision of 2026-11-20 (the second version):

- A metric name may be followed by `FILTER (WHERE <condition>)`, directly
  after the name and nowhere else: `customers.customer_count FILTER (WHERE
  customers.number_of_orders > 1) / customers.customer_count AS
  repeat_share`. One filtered metric alone is an expression and needs
  `AS name`.
- The condition is what a row filter of a question may say, an Ossie
  boolean expression over `dataset.field`, resolved as a row filter is:
  fields inlined, a string compared with a time field read as a timestamp
  in the model's zone, `CURRENT_DATE` in the model's zone. It holds no
  metric name, aggregate, window, `AGG` or subquery.
- The part's value is over the rows where the condition holds: every
  aggregate of the metric takes it as its `FILTER`, joined with `AND` to a
  `FILTER` it has already. The result has one `FILTER` on each aggregate,
  never a `FILTER` over a `FILTER`, and it is a model's metric whose
  aggregates have that `FILTER`: no planning rule is new, and an aggregate
  with a `FILTER` on its own dataset or on a dataset of the one side is
  planned as [a key stored twice](key-stored-twice.md) says. The question's
  row filters still apply to every part.
- Every dataset the condition reads must be reached through many-to-one
  relationships from every dataset the part's aggregates read (itself
  included). Otherwise the question is refused, naming the condition, its
  dataset and the metric, and saying to put the condition in a filter of the
  question or to declare a metric of the model. A condition on rows that
  reference the metric's dataset, the many side, is left out of this
  version, and the message says so.
- A model's metric whose aggregates read different datasets takes no
  condition on a third dataset: it is refused as a model's metric with that
  `FILTER` is, by the rule of [components](components.md) that refuses an
  aggregate with a `FILTER` on another dataset next to an aggregate over a
  dataset it does not read. A condition on the dataset the part's aggregates
  read works beside parts of other datasets.
- A division whose two sides are the same tree after the operands are
  inlined is refused: it is always 1. The message says to put the condition
  on one part, `m FILTER (WHERE <condition>) / m`.
- The syntax is parsed through SQLGlot (`QuestionSQL`, a parser that takes
  `FILTER` after a column). A model's expression, a field and a question's
  filter parse as before, and `name FILTER (...)` is an error there.

The name:

- It is the result column, spelled as written.
- In the same question it works wherever a metric name does: in
  `order_by`, as the default order (the first metric, descending) and in
  filters, where `return_pct > 10` filters the grouped rows.
- Refused: a name equal, in any case, to a metric of the model, and a name
  that another selected column has (the duplicate-column error).
- An expression cannot name another expression of the question, or its
  own name; the message says it is this question's expression and must be
  written out where the name stands.

Refused, each as a `PlanError` that names the construct and what to write
instead:

| Item | Message says |
|---|---|
| An expression without `AS name`, or a name that is no identifier | The item with ` AS <name>` appended |
| A name the model does not have | The message and suggestions of an unknown metric, the hint for a field included |
| A field, `dataset.field` | An expression reads metrics only, and the metrics that read the field |
| A written aggregate, `SUM(orders.total)` | Name a metric of the model; a question does not define one |
| A call of `AGG` | A question names a dataset's metric as `dataset.metric` |
| Any other function, a string, date, boolean or NULL literal, a comparison, `CASE`, a subquery, a window | What an expression over metrics reads |
| An expression that names no metric, `1 + 1 AS two` | At least one metric is needed |
| A window metric as an operand | Select it by its own name |
| A metric name in a condition, an aggregate, a window, `AGG`, a subquery | A condition reads `dataset.field` |
| `FILTER` after anything but a metric name | It goes directly after a metric name |
| A condition on a metric with no aggregate | It has no rows to restrict |
| A condition on a dataset the part does not reach | A filter of the question, or a metric of the model |
| A division of a tree by itself | The condition goes on one part |

A metric hidden by access control is an unknown name in an expression, as
it is in a question, and no error mentions it.

Planning a question leaves the `Planner` unchanged. A copy of the planner
plans the question, with each expression added to its metrics under its
name, so the planner that serves the next question never holds a name. The
same question gives the same SQL.

## Left out

| Left out | Why |
|---|---|
| Functions: `COALESCE`, `NULLIF`, `ROUND` | A part without a value makes the result NULL, as SQL arithmetic does. A function would give the question a way to say what a missing part is worth, and an agent would pick a different one each time. A model's metric can say it, `COALESCE(SUM(x), 0)`, and then every question has the same answer |
| Written aggregates | A question would define a metric the model does not have, with its own grain. The model defines metrics; the question combines them |
| Window metrics as operands | A window runs over the grouped rows of its own metric; inside an expression it would run over rows the expression changes |
| A change by period as a dimension, with each period's row next to the previous | Needs a window over a time dimension. One metric under two date conditions answers a comparison of two named periods, which is all a part's condition can say |
| A condition on rows that reference the part's dataset (the many side) | The part's rows would be selected by rows it does not read; [Semi-joins for filters on the many side](semi-joins.md) say it for a question's filter, which applies to every part |
| A group against the total | A part over the grouped rows and a part over all of them need two groupings in one row, which a question has not |
| Expressions in filters on fields and in dimensions | A different feature: this one adds columns to the result |
| Reading `AGG()` SQL | [Questions are the only query input](questions-only.md) |

## Tests

`tests/test_metric_expressions.py` compares each expression with its twin,
a model's metric written out, and with values computed by hand on the data
of `tests/test_stitch.py`. It covers ratios inside one dataset and across
two, a model's metric that is split, parentheses of inlined operands, row
filters including a semi-join, the name in filters and `order_by`, a
condition on a part (a subset share, the one side, a metric that has a
`FILTER`, several aggregates, dates, two periods) and every refusal,
division by zero, a part with no rows, the unchanged planner, a hidden
metric or field, the CLI and the MCP tool, and the length of the
instructions. `tests/test_compliance.py` asks `b14`, `b15` and `b16` of the
Ossie compliance suite as questions.

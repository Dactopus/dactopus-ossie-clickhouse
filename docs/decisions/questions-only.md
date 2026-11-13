# Questions are the only query input

The spec has two query surfaces. The Semantic Query Interface takes a
question: metrics, dimensions and filters by name, what the CLI and the
`query` MCP tool accept. The Relational Query Interface (apache/ossie#354)
takes SQL over the model's datasets as relations, with `AGG(metric)` for
a measure. Decision of 2026-11-01: this executor compiles questions
straight into ClickHouse SQL and does not read `AGG()` SQL. One input
grammar keeps one planner, the one place where every ClickHouse trait
(`FINAL`, dictionaries, the model's zone, semi-joins) is applied; a
second grammar would need the same rules a second time or a rewriter of
layer-2 SQL into questions, which the spec's own reference rewriter
(apache/ossie#502) is meant to be.

Compliance is shown on results. The spec's query compliance cases
(`compliance/query` in apache/ossie, since apache/ossie#632) ship the
SCOTT data, a model with dataset metrics and, per case, an `AGG()` query
with its expected result as standard SQL over literals.
`tests/test_compliance.py` loads the data through DuckDB into ClickHouse,
reads the model as it is, runs each case as the question it asks and
compares by the suite's README: columns by position, rows as multisets,
exact numerics equal, approximate ones within 1e-9. The weekly workflow
runs it on Ossie's `main`, so a case added upstream fails there until it
is listed here.

On 2026-11-10, at apache/ossie `d700cde`, the suite has 32 cases
(apache/ossie#628 added 31):

| Cases | Count | Outcome |
|-------|-------|---------|
| `a01`-`a04`, `a06`, `a07`, `b01`, `b02`, `b04`, `b18`, `b20` | 11 | asked as a question, the result equals the expected one |
| the rest | 21 | no question has the query's form |

`b04`, `b18` and `b20` ask for a metric of the one side (`dept_count`)
grouped or filtered by the many side (`emp.job`); see [A metric of the one side by the many side](one-side-by-many-side.md). The 21 other queries use a form a question
does not have: an
expression over measures in the `SELECT` list, grouping by an expression
that is no field, `ROLLUP`, a CTE, a subquery, a dataset read twice, a
join on a range or on columns that are no key, `RIGHT`, `FULL` and
`CROSS JOIN`, and a `LEFT JOIN` from the one side, which keeps a
department without employees where a question keeps `emp` rows. Each is
named with its form in `NOT_ASKED`.

The suite demands standard SQL semantics, which every generated query
already sets: `SUM` over no rows is NULL
(`aggregate_functions_null_for_empty = 1`) and `COUNT` is 0.

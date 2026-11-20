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

On 2026-11-20, at apache/ossie `344259a`, the suite has 32 cases:

| Cases | Count | Outcome |
|-------|-------|---------|
| `a01`-`a04`, `a06`, `a07`, `b01`, `b02`, `b04`, `b14`, `b15`, `b18`, `b20` | 13 | asked as a question, the result equals the expected one |
| the rest | 19 | no question has the query's form |

`b04`, `b18` and `b20` ask for a metric of the one side (`dept_count`)
grouped or filtered by the many side; see [A metric of the one side by the many side](one-side-by-many-side.md).
`b14` and `b15` divide the payroll by `dept_count`, a ratio of measures of
two datasets, which a question asks as an expression over metrics
(`emp.sum_sal / dept.dept_count AS sal_per_dept`; see
[Expressions over metrics in a question](expressions-over-metrics.md)). `b14` joins
inner, so its departments are those with employees: the question
filters on the employees, which keeps each department that has one. `b15`
joins `LEFT` from `dept`, which is the question without a filter.

The 19 other queries use a form a question does not have: `COALESCE`
over a measure value (`a05`; an expression over metrics takes no
function), grouping by an expression that is no field, `ROLLUP`, a CTE, a
subquery, a dataset read twice, a join on a range or on columns that are
no key, `RIGHT`, `FULL` and `CROSS JOIN`, a `LEFT JOIN` from the one side,
which keeps a department without employees where a question keeps `emp`
rows, and a filter in the `ON` clause (`b16`), which `dept_count` does not
see and a question's row filter, applied to every metric, would. Each is
named with its form in `NOT_ASKED`.

The suite demands standard SQL semantics, which every generated query
already sets: `SUM` over no rows is NULL
(`aggregate_functions_null_for_empty = 1`) and `COUNT` is 0.

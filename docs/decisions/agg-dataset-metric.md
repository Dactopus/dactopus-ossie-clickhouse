# AGG of a dataset's metric

apache/ossie#703 (`5eb0523`; `spec.md`, "Metric Scoping" rule 6 and "Name
Resolution"; `expression_language.md`, "Measure Evaluation: `AGG`") lets a
model's metric evaluate a dataset's metric: "A model-scoped metric MAY
evaluate a dataset-scoped metric with `AGG(dataset.metric)`". The argument
"MUST be a single reference to a dataset-scoped metric, written as
`dataset.metric`, and the call MUST NOT have a `FILTER (WHERE ...)`
clause"; "a bare `orders.total_revenue`, `AGG` of a field, and `AGG` of a
model-scoped metric are invalid". Anywhere else, "`OSSIE_SQL_2026`
expressions MUST NOT call `AGG` ... until a future version defines it",
in every capitalization. `wide_table_semantics.md` section 5.2: "An
`AGG(D.m)` call is one component with home dataset `D`, evaluated as the
dataset-scoped metric `D.m`." A field, metric or column named `agg` stays
valid. Before #703, apache/ossie#662 (`9b8f16c`) reserved the name with no
call allowed, and this executor refused every call.

## Where each check runs

- `parse()` checks the shape wherever a call stands: one argument, a
  column written `dataset.metric`, no `FILTER`. It also refuses `OVER` on
  the call, which the spec does not mention: expanded, the window would run
  over the dataset metric's input rows, not over its values.
  `SUM(AGG(d.m)) OVER ()` is a window over the values, and works. For the
  same reason it refuses `IGNORE NULLS`, `RESPECT NULLS` and `WITHIN GROUP`
  on the call, which would land on the dataset metric's whole expression.
- `untranslatable()` refuses a call in a field or a dataset's metric, so
  `validate` reports it and the executor hides that field or metric.
- `Planner._agg` expands `AGG(D.m)` in a model's metric into `D.m` as the
  planner resolves it when a question names `D.m`, and refuses a call in a
  filter, naming `D.m` as what a filter writes. An unknown dataset, a field
  or a name that is no metric of `D` (a model's metric included) is refused
  with the nearest names: `validate` reports the model's metric and the
  executor hides it.
- `outside_spec()` leaves `AGG` out, so no server's function list hides a
  model's metric that calls it. No call reaches ClickHouse: the planner
  expands or refuses every one before SQL is built.

## What the expansion keeps

Each aggregate of the expanded `D.m` carries `D` as its dataset, as when a
question names `D.m`; `_aggregate_reads` reads it too, so `COUNT(*)` in
`D.m` reads `D`. The model's metric `AGG(D.m)` alone is then planned like
`D.m`, its spine through a dimension `D` does not reach included.
Combined with an aggregate of another dataset, the aggregates of `D.m` go
to separate components, all with home `D`, and `D.m`'s expression is
evaluated over their values. Its value is that of one component: each part
is computed over the same rows of `D`, and over no rows each part has its
own empty value, so `SUM(x) / NULLIF(COUNT(*), 0)` is NULL, as `D.m` is.
Such a component keeps the reachability rule of a split metric
([One side by many side](one-side-by-many-side.md)); `D.m` asked alone by
a dimension `D` does not reach is bridged instead.

## Access

A model's metric goes with every dataset's metric it evaluates (`restrict`
in `access.py`): hidden by the grants, the policy file, a column the user
cannot read or a function the server lacks, `D.m` takes the model's metric
with it, and neither name appears in an error.

## Dialects

As in [NULL placement in ORDER BY](null-placement.md): `sql`, an
`OSSIE_SQL_2026` entry and, read by the same rules, an `ANSI_SQL` entry.
The spec defines `AGG` only for `OSSIE_SQL_2026`; the SQL standard has no
function `AGG`, so no portable `ANSI_SQL` expression loses a meaning, and
one rule keeps a metric valid when its author moves it between `sql` and
`ANSI_SQL`. A ClickHouse user-defined function named `agg` cannot be
called from a model.

## Next

apache/ossie#726 (open on 2026-11-16) would let a dataset's metric call
`AGG(source.measure)` for a source that declares measures. A ClickHouse
table declares none, so the refusal in a dataset's metric says what this
executor allows rather than what the spec reserves.

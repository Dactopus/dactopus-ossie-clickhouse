# NULL placement in ORDER BY

A window's `ORDER BY` without `NULLS FIRST | LAST` sorts NULL as the
largest value: last for `ASC`, first for `DESC`. Everything else sorts
NULLs last in both directions.

The spec defines the first rule (apache/ossie#587, merged as `30e1e50`;
`expression_language.md`, "Window Functions" and "Window ORDER BY NULL
Placement"): "Without `NULLS FIRST` or `NULLS LAST`, NULL sorts as the
largest value: last for `ASC`, first for `DESC`. This applies to
`OSSIE_SQL_2026` expressions; other dialects keep their own semantics.
Implementations MUST produce this order whatever the native default of
the engine". Before it the spec was silent (apache/ossie#498, and the
closed proposal apache/ossie#246); this executor had chosen NULLs last
both ways, ClickHouse's and DuckDB's default. The window syntax has the
`NULLS` clause since `7496811` (apache/ossie#577).

Engines differ. ClickHouse 26.9 and DuckDB 2.0.0 put NULLs last both
ways; Postgres treats NULL as larger than any value, as the spec does;
SQLGlot's default dialect, which the parser derives from, treats it as
smaller, first ascending. The translator once inherited that last one,
and the planner matched it, so a running total ordered by a key with
NULLs started from their sum: on TPC-DS, with `LEFT JOIN`s, the 129,850
sales without a matching date came first in `cumulative_sales`. DuckDB
disagrees with the spec for `DESC`, so the tests of that case pin their
expected values by hand.

Which expressions it covers. `sql` is the `OSSIE_SQL_2026` form
(`spec.md`: "the top-level `sql` is authoritative and consumers MUST use
it as the OSSIE_SQL_2026 form"), and so is an `OSSIE_SQL_2026` entry in
`dialects`. An `ANSI_SQL` entry is, for the spec, an opaque dialect that
"keeps its own semantics". This project reads it with the same rule all
the same: the SQL standard leaves the placement of NULLs to the
implementation, so the spec's default is a conforming choice, and one
rule keeps the answer of a metric the same when its author moves it
between `sql` and `ANSI_SQL`. An agent's filter is read the same way.

How. ClickHouse supports the clause. Its default, NULLs last both ways
(spec's table "Native defaults"), is the spec's for `ASC`, so `parse()`
sets `nulls_first` on each `DESC` item of a window's `ORDER BY` that has
no clause, and the SQL says `DESC NULLS FIRST`; `ASC` carries nothing.
The spec lets an implementation omit a default the engine already gives
"only if the session cannot change that default, or the implementation
sets it". No ClickHouse 26.9 setting changes the order of NULLs
(`system.settings` has none whose name concerns NULLs and ordering, and
the server has no counterpart of DuckDB's `default_null_order`), and the
planner's `SETTINGS` set none. An explicit `NULLS FIRST | LAST` is
kept as written; SQLGlot 30.27 prints `NULLS FIRST` and drops
`NULLS LAST`, which is ClickHouse's default in both directions. A
canary pins that, and the spec sentence.

The setting is not `NULL_ORDERING` of the parser dialect, which stays
`nulls_are_last`: `nulls_are_large` would also turn every `DESC` the
parser reads into `NULLS FIRST`, where the spec gives no rule. Switching
to an upstream parser (#4) must keep that: the window rule stays in
`parse()`, and anything else the parser reads keeps NULLs last.

Out of scope, kept as it was. The rows of a query: the planner builds
the `ORDER BY`, NULLs last in both directions with no clause, which
keeps rows without a value at the end of a top-N list. The merged Layer 3
spec (apache/ossie#653) has no query `ORDER BY` to follow. And
`WITHIN GROUP (ORDER BY ...)`: the spec's syntax shows no clause there,
but the reference grammar (apache/ossie#657) shares its sort item with
windows, and `compliance/expressions/valid.txt` has
`PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x DESC NULLS LAST)`. A
percentile skips NULLs, so the clause changes nothing: `parse()` accepts
it there and the rewrite of the percentile functions reads the direction
only. Anywhere else outside a window it is refused. Tests pin both.

What changes in an answer: a window metric ordered `DESC` over a key
with NULLs. On TPC-DS, with `LEFT JOIN`s, the sales without a matching
date form a group with a NULL `d_year`, and
`RANK() OVER (ORDER BY date_dim.d_year DESC)` ranks it 1, where NULLs
last in both directions rank it last. `ASC`, such as the running total in `cumulative_sales`,
and an explicit clause give what they gave.

NaN is not NULL and engines disagree on it too: ClickHouse puts NaN after
the values and before NULLs in both directions; DuckDB and Postgres treat
it as larger than any value, so first descending. The executor returns NaN
as `None`, so ClickHouse's order keeps every row without a value at the
end. A test ordering by a key that can be NaN pins its expected value by
hand.

Rows that tie on the ordered keys are then ordered by each dimension not
ordered yet, ascending. ClickHouse returns tied rows in whatever order its
threads produce them, so without this a `limit` could keep different rows
on each run of the same question. The rows are grouped by the dimensions,
so no two of them tie on all of them. Ties are common in stitched answers:
every dimension value missing from the first metric's fact has NULL there
(#42).

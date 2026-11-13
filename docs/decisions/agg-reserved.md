# AGG is reserved

apache/ossie#662 (`9b8f16c`; `expression_language.md`, "Measure
Evaluation: `AGG` (RESERVED)"): "In `OSSIE_SQL_2026` expressions, an
implementation MUST NOT give `AGG` any other meaning. Expressions in other
dialects are not affected. Because function names should be case
insensitive, the reservation covers every capitalization". And:
"`OSSIE_SQL_2026` expressions MUST NOT call `AGG` until a future version
defines where model declarations may use it." A field, metric or column
named `agg` stays valid ("Reserving `AGG` as a function name does not
prevent a field, metric, or column from being named `agg`").

Passed through as an unknown function, `AGG(x)` would fail in ClickHouse
or, if the server has a function of that name, mean something else.
`parse()` refuses a call of `AGG` in any capitalization, an `OVER` call
too, so the refusal reaches a field and
a metric (hidden and reported by `validate`, as any expression that does
not translate) and an agent's filter, before any SQL is built. Only a
function call is refused: a column, field or metric named `agg` is a name,
not a call.

Which expressions it covers. The same as in [NULL placement in ORDER BY](null-placement.md):
`sql`, an `OSSIE_SQL_2026` entry and, because this project reads it by the
same rules, an `ANSI_SQL` entry. The spec's "Expressions in other dialects
are not affected" says the reservation does not bind them, not that
refusing is wrong; the SQL standard has no function `AGG`, so no portable
`ANSI_SQL` expression loses a meaning, and one rule keeps a metric valid
when its author moves it between `sql` and `ANSI_SQL`. The cost is a
ClickHouse user-defined function named `agg` called from an `ANSI_SQL`
entry; the spec's own advice for an older expression is to "write the
expression in a vendor dialect", which this executor does not read, so such
a call cannot be written in a model. Nothing else the spec says gives a
reason to accept it.

# Expressions in ClickHouse's dialect

An Ossie expression may carry texts in other dialects next to `sql`, and
`CLICKHOUSE` is one of the spec's well-known names. The Holistics and
OrionBelt converters mark every expression `CLICKHOUSE` when asked
(apache/ossie-converters#202, merged 2026-11-16), so a converted model
may have no other text. Such a text cannot be read as `sql`: the
executor reads a name the spec shares with ClickHouse by the spec, its
author meant ClickHouse's, and the answer comes back with another number
and no error. `length(name)` counts characters in Ossie SQL and bytes in
ClickHouse.

## Decision

A text in `CLICKHOUSE` is read where the SQL the executor sends to
ClickHouse is the text as written, give or take the differences below,
and refused elsewhere with the construct named and what to write
instead. `sql` is read first, then `OSSIE_SQL_2026`, `ANSI_SQL` and
`CLICKHOUSE`.

`translate.clickhouse_problem` reads the text with SQLGlot's ClickHouse
dialect and compares it with the SQL it would send, read the same way.
The check accepts what matches and refuses everything else, so a
difference nobody listed is refused too: the formulas of public projects
had one that 64 hand-written formulas lacked (a backslash in a string).
It runs in `pick_expression`, through which every layer reads an
expression, once per text. A refused field or metric is hidden and
reported by `validate`, as any expression that does not translate.

The check trusts SQLGlot's ClickHouse dialect to read a text as
ClickHouse does. A test of each refusal pins that reading; a canary pins
the one SQLGlot gets wrong, `CURRENT_TIMESTAMP` without parentheses,
which it reads as a column.

## Differences accepted

- The model's time zone for ClickHouse's clocks and `dateDiff`: `now()`,
  `today()`, `yesterday()` and their aliases, `CURRENT_DATE`,
  `CURRENT_TIMESTAMP`, as in `sql` ([time-zone.md](time-zone.md)).
- Spellings that mean the same in ClickHouse: `if(c, a, b)` as `CASE`,
  `x IS NOT NULL` as `NOT (x IS NULL)`, parentheses, `INTERVAL 1 DAY` as
  `INTERVAL '1' DAY`, a cast to `Nullable`, the case of a function name
  ClickHouse reads in any case.
- A division in `Float64` ([division-to-float64.md](division-to-float64.md)).
- An aggregate over no rows is NULL. Every query sets
  `aggregate_functions_null_for_empty`, so `SUM` over no rows, and `SUM`
  with a `FILTER` that matches none, are NULL as the spec says. The
  setting belongs to the query, not to an expression: `sumIf` with no
  matching row is NULL where ClickHouse alone gives 0, and `countIf` and
  `uniqExact` stay 0. A metric in `sql` that calls `sumIf` gets the same.

Leaving a division in a `CLICKHOUSE` text as written was considered. A
field goes into its metric's tree and the whole tree is rewritten at
once, so it would take marking each node with the text it came from.
Integers and `Float64` divide in `Float64` in ClickHouse already, so
their quotient is the same. A `Decimal` quotient keeps all its digits
where ClickHouse alone keeps the dividend's scale: `sumIf(amount, paid)
/ sum(amount)` over `Decimal(10, 2)` amounts is 0.425 here and 0.42 in
ClickHouse. Divisions appear in 22 of the 160 public formulas.

## Refused

| ClickHouse text | The executor would send | Why it differs |
|---|---|---|
| `length(s)` | `CHAR_LENGTH(s)` | characters instead of bytes; an error for an array |
| `position(s, 'x')` | `POSITION('x', s)` | the arguments in the other order |
| `tags[1]` | `tags[2]` | `[i]` is read from 0; the spec has no index |
| `round(x)`, `round(x, 2)` | an expression of `floor` and `sign` | a `Float64` tie away from zero, not to even |
| `a % b` | `a - (b * intDiv(a, b))` | `Decimal` scales aligned |
| `median(x)` | `quantileExactInclusiveOrNull(0.5)(x * 1.0)` | exact instead of approximate |
| `sum(x) OVER (ORDER BY d DESC)` | `... DESC NULLS FIRST` | NULL first ([null-placement.md](null-placement.md)) |
| `replaceRegexpAll(s, '\\d+', '')` | `'\\\\d+'` | the backslash read as a character, not an escape |
| `CAST('2026-01-01 00:00:00' AS DateTime)` | `toDateTime(..., zone)` | the model's zone instead of the server's |
| `CAST(s AS LowCardinality(String))` | `CAST(s AS Nullable(LowCardinality(STRING)))` | a type SQLGlot's default dialect does not know; ClickHouse refuses it |

Each refusal says what to write instead: `lengthUTF8`, `OCTET_LENGTH`,
`position(needle IN haystack)`, `arrayElement`, `roundBankers` for a
`Float64` (ClickHouse's `round` of a `Decimal` already rounds as the
spec's `ROUND`), `modulo`, `NULLS LAST`, `toLowCardinality`, or the
expression as `sql`. A synonym the executor would rename, `dayofyear(d)`
sent as `toDayOfYear(d)`, is refused with the name it would send. A name in backticks
is refused with the advice to quote it in double quotes, which both
readings share; reading backticks would make every place that parses an
expression depend on its dialect. A text Ossie SQL does not parse is
refused with the parser's reason, marked as read as Ossie SQL: a
parametric aggregate, `quantile(0.9)(x)`, or `dateDiff` with a fourth
argument, the zone. `toDateTime('...')` and
`date_diff` reach ClickHouse as written, in the server's zone, as they
do from `sql`.

## Evidence

Measured on 2026-11-17 with SQLGlot 30.31.0. Of 160 formulas from public
ClickHouse projects (the ClickHouse examples of rilldata/rill-examples
at `5f0bf28`, the Superset exports tonindexer/superset-export at
`8b4dab4` and SolDevelo/soldevelo-reporting-stack at `fa068d1`, and
ClickHouse/ClickBench's queries at `98b5d4f`, its 89 sums
`SUM(ResolutionWidth + k)` counted as one), 154 are accepted and 6
refused: `length` 3, a backslash in a string 2, `round` 1. Of the 154,
127 are sent exactly as written; the rest differ by a division, by
parentheses, or by the zone of `today()`. Of 64 hand-written formulas,
9 are refused (`round` twice, `%`, `[1]`, `length`, `position`,
`median`, a window's `DESC` and a name in backticks) and 2 are
parametric aggregates.

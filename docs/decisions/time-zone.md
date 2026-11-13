# The model's time zone, UTC by default

A ClickHouse `DateTime` is an instant; its zone decides how it prints and
which calendar day or hour it falls on. A column without a zone, `now()`,
`today()` and `CAST('...' AS DateTime)` use the server's zone, so the same
question gave different answers on two servers (#52). Measured on
ClickHouse 26.9.5.2 with a server in `Asia/Makassar` (UTC+8), 2026-10-22:

- `CURRENT_DATE` was the server's date: at 20:00 UTC, already tomorrow.
- A value the CLI showed for a `DateTime('UTC')` column, filtered back as
  `TIMESTAMP '2026-10-21 20:00:00'`, matched no row: the cast read it at
  UTC+8.
- `CURRENT_TIMESTAMP` printed the server's wall clock without a zone.

The zone belongs to the model's data: dactopus-data-models computes order
dates in the store's zone and keeps times in UTC. So the model names it
(`time_zone` in its `CLICKHOUSE` entry), UTC when it does not, as Cube,
Malloy, Power BI and Superset default to UTC. `CURRENT_DATE` is
`toDate(now(zone))`, `CURRENT_TIMESTAMP` is `now(zone)`, and a timestamp
literal is `toDateTime('...', zone)` (`toDateTime64` with its fraction,
which the cast dropped). `DATEDIFF` is `dateDiff(unit, a, b, zone)`:
without a zone argument ClickHouse counts in its arguments' zone, and when
they have two it miscounts hours and longer. A reading at 20:00 UTC in a
`DateTime('Asia/Makassar')` column and `03:00+00:00` the next day came out
20 hours and -1 day apart; with the zone, 7 hours and 0 days in New York.
Ossie has no such field;
apache/ossie may get one, and this entry would then read it.

A model shared by deployments cannot name the zone: dactopus-data-models
has one `commerce` model for every store and builds each store's dates
in that store's zone (#54). So the deployment may replace it, as it
names the database in its URL (the order is in
[model authoring](../model-authoring.md#time-zone)). The deployment wins over the model because it knows which zone
its data was built in, and a model can then carry a default. A
deployment's file holding only the entry was not possible: every file holds a dataset, and the entry in
two files is refused, since one would be edited and not the other. One
zone per deployment is still one zone for every store a deployment
holds.

Agents still answered "today" for the wrong day (#56). Asked how many
orders were placed today, with the store in `Pacific/Honolulu` and the
agent's clock in `Pacific/Kiritimati` (always a day apart), none of six
fresh `claude -p` sessions (three Haiku 5.5, three Sonnet 5.5, on 0.9.3)
wrote `CURRENT_DATE`: they wrote the date they knew or ClickHouse's
`today()`, and "never write SQL" read as a ban on `CURRENT_DATE` in a
filter. With the server's instructions and the `query` tool's
description asking for `CURRENT_DATE` or `CURRENT_TIMESTAMP` in filters,
all six wrote it and answered right. `list_model` returns `today` and
`now` in the zone, read from the server's clock on every call, so an
agent names the day it answered for. `today()`, `yesterday()`, `now()` and
`now64()` without a zone, and the aliases `curdate()` and
`localtimestamp()`, are rewritten into the zone too: they are
ClickHouse's own functions, but an answer in the server's zone is the
failure this entry is about, and a zone written in the call is kept.
A metric of the current time in each model was not chosen: every model
would have to carry it, and agents would still have to be told to use
it.

A plain string compared with a time field was still read in the
column's zone (#58), and agents write it as often as `TIMESTAMP`. On
dactopus-data-models' commerce model over an earlier build of its
Shopify fixture (without `sales_events`, so `all_orders` stands in for
`orders`), built in `America/New_York` with every time column in UTC,
orders before midnight of September 1 (order 1003 was placed at 22:00
on August 31 in New York):

| Filter on `orders.order_time` | New York, before | New York, after | UTC, both |
| --- | --- | --- | --- |
| `< TIMESTAMP '2026-09-01 00:00:00'` | 3 | 3 | 2 |
| `< '2026-09-01 00:00:00'` | 2 | 3 | 2 |
| `< '2026-09-01'` | 2 | 3 | 2 |
| `< '2026-09-01 00:00:00+00:00'`, with or without `TIMESTAMP` | 2 | 2 | 2 |
| `< '2026-09-01 00:00:00-04:00'`, with or without `TIMESTAMP` | 3 | 3 | 3 |

A string compared directly with a field or metric whose `datatype` is
`DateTime` or `DateTimeTz` (`=`, `<>`, `<`, `BETWEEN`, `IN` and the
like) is made a `TIMESTAMP` literal before the field is inlined, so
`rewrite_tree` reads it as one: in the model's zone, an offset kept, a
fraction as `DateTime64`. The model's `datatype` says which fields hold
times, so the SQL is the same without a server, as `sql` shows it. A
field without `datatype` keeps ClickHouse's reading, and so does a
string compared with an expression of a field
(`toDate(orders.order_time) = '2026-09-01'`), whose type the planner
does not know. `Date` and `Time` fields are left alone: a date has no
zone, and the string compared with one is a date already.

Not chosen:

- The column's type from `system.columns` to tell a time field. The SQL
  would differ with and without a server, `TableInfo` would carry types
  it does not need elsewhere, and a field computed from a column would
  still be unknown.
- `session_timezone` on every query. A column without a zone then printed
  in the session's zone while `toDate`, `toHour` and `dateTrunc` of it
  still used the server's (`timeZoneOf` stays the server's), so one row
  showed 12:00 with an hour of 20. ClickHouse marks the setting beta and
  documents that some parsing paths ignore it.
- A literal read in the zone of the column it is compared with.
  ClickHouse does that for a string compared directly with a column, but
  a literal inside `DATEDIFF`, `GREATEST` or on its own has no column.
  Values are shown with their offset instead (`2026-10-21 20:00:00+00:00`),
  which ClickHouse reads back as that instant in a comparison and a cast.
- Other calendar functions of columns in the model's zone. A `Date` column
  refuses a zone argument in `dateTrunc('month', ...)` and `toStartOfMonth`,
  and the planner does not know column types. It can be done without
  them (`toStartOfMonth(toDate(x, zone))`), when the standard's field asks
  for it.

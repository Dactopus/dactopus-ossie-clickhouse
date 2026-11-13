# `FINAL` instead of `argMax` for `ReplacingMergeTree` tables

Measured on 3.5 million rows with 20% duplicate versions: `FINAL` took
94 ms against 150 ms for an `argMax` rewrite alone, 90 against 125 with a
join. `FINAL` is also one keyword to generate where `argMax` is a subquery
per dataset. `argMax` was not built.

A `Distributed` table, materialized view or `Merge` table is read with
`FINAL` when the table it reads keeps versions. Checked on ClickHouse
26.9: `FINAL` on the first two over a plain `MergeTree` fails with
`ILLEGAL_FINAL`, so it cannot be added blindly; a `Merge` table applies it
to the tables that support it and skips the rest. When the table read is
hidden from the connected user, the dataset is read without `FINAL` and
`validate` reports it: guessing `FINAL` would break queries over plain
tables that work today, and `dedup: final` settles it per dataset.
`engine_full` names a `Distributed` or `Merge` table's tables as quoted
strings on 26.1 through 26.9; `system.tables` names a view's target only
from 26.6 on, so the introspection query does not select it.

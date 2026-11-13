# Catalog calls keep their spec signature

SQLGlot's translation of fourteen catalog functions dropped the arguments
beyond the spec's signature without an error: `REGEXP_LIKE(s, 'iphone',
'i')` ran as `match(s, 'iphone')`, case-sensitive; `MOD(x, y, z)` as
`x % y`; `LOG10()` as `LOG(10)`; `LENGTH`, `LTRIM`, `RTRIM`, `FLOOR`,
`CEIL`, `CEILING`, `DATE_TRUNC`, `CURRENT_DATE`, `CURRENT_TIMESTAMP`,
`POSITION` and `CHARINDEX` likewise. So a rewritten function, ours or
SQLGlot's, carries its spec signature in `_SIGNATURES`, and `parse()`
refuses other argument counts; `validate` reports a field or metric
written so. A longer form that reaches ClickHouse whole and means there
what it means in DuckDB, Snowflake or Postgres stays, with a value test:
`FLOOR(x, 1)`, `CEIL(x, 1)`, `LTRIM(s, 'ab')`, `RTRIM`,
`CHARINDEX(substr, str, start)`, `POSITION` likewise and `DATE_TRUNC`
with a zone. A name in `_SIGNATURES` parses as a plain call, never
through SQLGlot's special syntax (`CEIL(x TO DAY)`), which skips the
check; `POSITION(substr IN str)` keeps its own, checked in
`_parse_position`. `LOG(x)`, outside the spec, is refused: ClickHouse
reads it as the natural logarithm, Postgres and DuckDB as base 10.

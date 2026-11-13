# Window frames limited to the spec's

The spec lists three frames: `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT
ROW`, `ROWS BETWEEN n PRECEDING AND n FOLLOWING` and `RANGE BETWEEN
UNBOUNDED PRECEDING AND CURRENT ROW`; its examples also use `n PRECEDING
AND CURRENT ROW`. Accepted: `ROWS` frames that hold the current row, and
that one `RANGE` frame. The others give wrong values in ClickHouse 26.9,
compared with DuckDB on keys with NULLs:

- An empty frame, such as `ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING` on
  the last row, sums to 0 (`AVG` to NaN), not NULL.
- `RANGE` with an offset counts NULL keys as within any offset: on keys
  (1, NULL, 2, NULL, 2, 4), `SUM(v) OVER (ORDER BY k RANGE BETWEEN 1
  PRECEDING AND 1 FOLLOWING)` adds the NULL rows to the row with key 4.

`parse()` rejects other frames and `GROUPS`, so such a field or metric is
hidden and reported by `validate`. Reopen when the translator can rewrite
them, or ClickHouse fixes them.

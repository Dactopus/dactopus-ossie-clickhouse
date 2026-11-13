# Exact percentiles

The spec's percentile functions are exact, and `APPROX_PERCENTILE` is the
approximate one. ClickHouse names it the other way round: `quantile` and
`median` keep a sample of 8,192 values, exact below that and approximate
above. On ClickHouse 26.9, the median of 2,000,000 values of
`rand64() % 1000000` came out 503,113.5 from `quantile` and 499,986.5
from the exact function.

`PERCENTILE_CONT` and `MEDIAN` are translated to
`quantileExactInclusiveOrNull(p)(x * 1.0)`. `quantileExactInclusive`
interpolates between the two nearest values as Postgres does (2.5 for the
median of 1, 2, 3, 4; `quantileExact` gives 3). It rejects `Decimal`, so
the argument is made `Float64`, which is also what Postgres computes in.
Alone it answers an empty set with `nan`; `OrNull` makes that NULL
while keeping one aggregate, so `FILTER (WHERE ...)` still applies to it.
Exact percentiles hold every value of the group in memory.

Postgres defines `PERCENTILE_CONT` for numbers only. `quantileExactInclusive`
takes a `Date` and answers with a day count (20,455.5 for a median date in
2026), and `toFloat64` would also parse a `String`. Multiplying by `1.0`
makes ClickHouse refuse both, so the query fails instead of returning a
number that is not a date. `PERCENTILE_DISC` takes any sortable type and
returns a date for dates. The translator does not know column types, so
the refusal comes when the query runs, not from `validate`.

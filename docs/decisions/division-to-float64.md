# Division to `Float64`

ClickHouse keeps the dividend's scale when it divides a `Decimal`: 10.00 /
350.00 is 0.02, and `SUM(price) / 3` drops digits the same way. Postgres
(`numeric`) and DuckDB (`double`) give 0.0285714. The spec does not say
what type a division returns, so the translator follows the engines the
project compares with: the dividend of every `/` becomes `Float64`, as
DuckDB divides. `AVG` of a `Decimal` is already `Float64` in ClickHouse,
and division of integers too. Division by zero then gives `inf` or `nan`,
which the executor shows as NULL, where a `Decimal` division by zero
failed the whole query. Inside the query the value stays infinite, or
NaN for 0 / 0: `inf` sorts above every number and passes `ratio > 1` in
a filter, `-inf` sorts below, and NaN sorts last in both directions and
fails every comparison, as division of integers always did. DuckDB
gives `inf` too; Postgres refuses the division. The dividend is
`x * 1.0`, as for percentiles: ClickHouse refuses it for a date or a
string, as Postgres does, where `toFloat64` would divide a day count or a
parsed number.

Two alternatives were rejected:

- `Decimal` with a fixed scale, `toDecimal128(a, 10) / b`: the scale has
  no basis in the spec, `int / int` would become a `Decimal`, and ratios
  would differ in type from `AVG`.
- Casting to the metric's declared `datatype`: a rule of this project's
  own on top of the spec, which applies only to a whole metric, not to a
  division inside one or in a filter.

Known mismatch: the spec's own example of a ratio, `avg_orders`
(`SUM(orders.amount) / COUNT(DISTINCT customers.id)`, `core-spec/spec.md`
on apache/ossie `main`), declares `datatype: Decimal`, and the answer is
a float. `datatype` is not checked against results.

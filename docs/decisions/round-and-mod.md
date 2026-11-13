# ROUND ties away from zero, MOD through intDiv

The spec fixes two results an engine could otherwise decide
(`expression_language.md`, "Numeric Semantics", apache/ossie#687, merged
2026-11-12): `ROUND(x, d)` rounds a value exactly halfway away from
zero, exact for `DECIMAL` and best effort for `FLOAT`, and `/` is true
division. Division already was (above).

ClickHouse 26.9.27.3, measured on 2026-11-13: `round` rounds integers and
`Decimal` ties away from zero (`round(25, -1)` is 30, `round(-150.00,
-2)` is -200) but a `Float64` tie to even: `round(2.5)` is 2,
`round(-2.5)` is -2, `round(0.125, 2)` is 0.12. The translator does not
know an argument's type, and an `if` on `toTypeName` needs both branches
to type-check for every type, so every `ROUND` becomes the equivalent
expression the spec gives, with `y = abs(x) * 10^d`:
`sign(x) * (floor(y) + if(2 * (y - floor(y)) >= 1, 1, 0)) / 10^d`. The
factors and the literals are integers, so a `Decimal` stays a `Decimal`
at its scale, exact; the comparison is `2 * (y - floor(y)) >= 1` so no
`Decimal` meets a `Float64` literal. A negative `d` rounds to a multiple
of `g = 10^-d` from the rest of `abs(x)` after `floor(abs(x) / g)`
multiples of it, so no factor is a fraction. The quotient is `divide()`,
which the `Float64` rule for `/` leaves alone. Results:

| Type of `x` | Type of `ROUND(x, d)` |
| --- | --- |
| `Decimal(P, S)` | `Decimal(P, S)`, exact |
| `Float64` | `Float64`; a tie in binary is best effort: `ROUND(2.675, 2)` is 2.68, as `2.675 * 100` is 267.5 in `Float64` |
| an integer | `Int64` for `d = 0`, else `Float64`, exact below 2^53 |

The spec writes the factor as an exact literal. A `d` that is not a
whole number literal, a column for one, keeps ClickHouse's `round`,
which takes a column there: right for integers and `Decimal`, and a
`Float64` tie still rounds to even. Refusing it would hide a field that
answers right for most types.

The same measurement found ClickHouse's modulo wrong for `Decimal`: it
does not align scales, so `toDecimal64(125.50, 2) % 7` is 0.06, the rest
of 12550 % 7, and `% toDecimal64(7, 0)` is 0.06 too; with equal scales it
is right (6.5). `intDiv` aligns them and truncates toward zero, so `MOD(a,
b)` and `a % b` become `a - b * intDiv(a, b)`: the rest keeps the
dividend's sign, as `%` and DuckDB do; integers stay integers (`Int64`)
and a `Decimal` a `Decimal`. A
`Float64` beyond the `Int64` range fails in `intDiv` instead of
returning a rest.

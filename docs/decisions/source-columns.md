# Source columns in a dataset's metric

apache/ossie#633 (`21e849d`; `spec.md`, "Metric Scoping" and "Name
Resolution") lets a dataset's metric read a column of its dataset's
`source` as `source.column`: "`source.quantity` and `SOURCE.quantity` name
the same column. `source.amount` refers to the source column even when the
dataset declares a field named `amount`." An unqualified name in such a
metric "MUST be a declared field of the dataset". On the name `source`:
"`source` is not reserved: a field or dataset may be named `source`. If a
dataset declares a field named `source`, the name `source` refers to that
field, so the dataset's metrics cannot refer to source columns." A model's
metric "cannot reference source columns", and keys and relationship
columns name fields. For a consumer that generates SQL against the source
tables the spec says to replace "each `source.column` reference with
`column`".

`Planner._own_field` follows that: `source.column` becomes the column under
the dataset's alias, never a field's expression; only the qualifier folds
case, since ClickHouse's column names are case-sensitive. A source column
is checked and hidden like a field's column: `validate --url` checks it
against `system.columns`, a metric over a column the user cannot see is
hidden from that user, and under a policy file `source.x` counts as
reading every field whose expression reads `x`.

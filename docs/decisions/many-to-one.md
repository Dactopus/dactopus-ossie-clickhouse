# Many-to-one inferred from keys

The spec has no cardinality attribute. A relationship is used only when its
`to_columns` cover the primary key or a unique key of the target, which is
what makes a `LEFT JOIN` safe against fan-out. A row filter on the many
side of a relationship is a semi-join
([Semi-joins for filters on the many side](semi-joins.md)), and metrics
of several facts are stitched
([Stitch metrics of several facts](stitching.md)). A metric of the one
side grouped by a dimension of the many side counts each of its rows
once per group, over a spine of distinct keys ([A metric of the one side by the many side](one-side-by-many-side.md)).

The spec calls `to_columns` "Primary/unique key columns"; covering, a
superset of a key, is how the reference validator reads it
(apache/ossie#330), and a superset of a unique key is unique too. It
matters for multi-tenant schemas joined on `(tenant_id, customer_id)`
with the key declared as `customer_id`. Such a join to a single-key
dictionary stays a `LEFT JOIN`: `dictGetOrNull` by the key alone would
ignore the extra columns. The cost is memory: ClickHouse (checked on
26.9) runs a join on more than the dictionary key as a hash join that
loads the whole dictionary, not as a direct key lookup.

Two points are stricter than that validator. A relationship whose
`to_columns` cover no key is refused, where the validator only warns. A
target with no declared key is refused too, where the validator skips the
check: nothing then rules out fan-out. The model still loads; questions
that need such a join fail, and `validate` reports the relationship.

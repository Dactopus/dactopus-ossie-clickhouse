# A key stored twice on the one side

ClickHouse does not enforce the uniqueness of a declared key, and a
`MergeTree` table keeps every row it is given. Take the spec's Appendix B.1
with customer C1 stored twice, both rows in EAST: a plain `LEFT JOIN` from
`orders` counts C1's orders twice in EAST, 650 for `orders.total_revenue`
where the answer is 350. Layer 3 (`wide_table_semantics.md`, section 4,
on `LEFT JOIN`) answers a model that breaks its declared multiplicity as
Layer 2 does, and Layer 2 (`relational_semantics.md`, section 8.1) gives an
aggregate in a group the home rows that have at least one joined row in it,
each counted once. If C3 is stored with two regions, WEST and SOUTH, an
order of C3 belongs to both groups, once in each.

Rule. In a `SELECT` with an aggregate that repeated rows change, each set of
datasets the root joins through one first relationship (a subtree) is
joined as a derived table, not as tables:

```sql
LEFT JOIN (
  SELECT DISTINCT customers.id AS __k0,
         customers.region AS __e1,
         customers.region = 'EAST' AS __e2,
         1 AS __m
  FROM customers
) AS customers ON orders.customer_id = customers.__k0
```

The derived table holds the key of the first step and each largest
expression of the question that reads only that subtree: a dimension, a
condition of a filter, an argument. A chain (orders, customers, regions)
is one derived table, so two `customers` rows that lead to one region do
not count twice. Conditions are carried as values and applied outside,
since a root row that matched nothing must still see the subtree's
columns as a plain join leaves them (NULL, or the type's default where
ClickHouse cannot make it Nullable; `Catalog.nullable_tuple` probes the
server once). The code is `Planner._dedup`, `_carry`, `_blend`,
`_injective` and `_regroup`.

An expression that reads the subtree and the root together keeps Layer
2's answer only when two copies of a key cannot give it one result: a
condition joined with `AND`, an argument reached through `+`, `-` and, in
`SUM`, `*` and `/`, a one-branch `CASE` or `IF` that acts as a `FILTER`,
and factors of the subtree gathered into one operand
(`SUM(lines.qty * products.price * (1 - products.discount))`). Aggregates
that carry different values of the subtree are answered in SELECTs of
their own and merged on the dimensions, as stitched groups are; a model's
metric with such aggregates is split into components.

Refused, since the planner does not read the data and two copies could
give one result: `customers.weight > 0 OR orders.amount > 150` passes O2
for both copies of C1 (weights 1 and -1) and gives EAST 550 where Layer 2
gives 350; `SUM(GREATEST(orders.amount, customers.weight))` gives 990
where Layer 2 gives 690. #653 rejects them as well
(`E_CROSS_DATASET_FILTER`, `E_AMBIGUOUS_MEASURE_GRAIN`). Answering them
would need Layer 2's own plan, the DISTINCT of the root's key, the whole
argument and the dimensions over the plain join. Decision of 2026-11-06:
refuse until a user needs one, since adding an answer later changes no
number given before. Also refused: a window function over aggregates
that carry different values, and a window metric in a question whose
metrics are answered apart.

Skips, where the plan does not change:

- A `SELECT` with no aggregate, or only repeat-safe ones (`MIN`, `MAX`,
  `DISTINCT` and the list in [Functions outside the spec pass through as written](functions-pass-through.md)). `GROUP BY` already collapses repeats, and section 5.6 of the
  spec does the same for a question without metrics.
- A subtree whose relationships all join a single-key dictionary on its
  key, or a superset of it. A dictionary has one row for each key, and the
  column is read with `dictGetOrNull`. A dictionary with a dot in
  its name, which `dictGet*` cannot address, is joined as a table and is
  not known to be unique.
- Semi-joins (`IN (SELECT ...)`) cannot repeat a root row and are not
  changed.

Stitched branches and the components of a model's metric go through the
same code, so each gets its derived tables.

Not done: a `ReplacingMergeTree` read with `FINAL` on the key of the
relationship is unique too, but proving it needs the sorting key in the
catalog. It is the next skip if the cost below matters.

Cost. Measured on 2026-11-06 on ClickHouse 26.9 on the test server (a
memory limit of 3.26 GiB), best of five runs:

| Case | Plain join | Derived table |
|---|---|---|
| TPC-DS SF1, `store_sales` (2.9 million rows) by `item.i_brand` | 0.107 s | 0.109 s |
| TPC-DS SF1, by customer country and year, filtered on the customer | 0.108 s | 0.119 s |
| 1 million orders, 3 million order lines, by order channel | 0.10 s | 0.18 s |
| the same, filtered on order status | 0.08 s | 0.10 s |
| 10 million orders, 30 million order lines, by order channel | 0.85 s | 3.0 s |
| the same, filtered on order status | 0.69 s | 1.0 s |

Dimension tables cost almost nothing. The cost is a large table on the one
side, here order lines read against orders, which dactopus-data-models
builds as plain `MergeTree`. `GROUP BY` instead of `DISTINCT`,
`optimize_distinct_in_order` and an explicit `parallel_hash` were not
faster. With 10 million orders on this server, parallel queries already hit
the memory limit.

Decision of 2026-11-06: always on, with no switch. The stores this is built
for have up to a million orders, where it adds about 0.1 s, and a wrong
revenue is worse than a slow one. A switch, if a deployment needs one,
belongs in the dataset's `CLICKHOUSE` entry beside `dedup`.

The tests are in `tests/test_dedup.py`: DuckDB runs the Layer 2 plan (the
distinct root key, argument and dimensions after the join and the filter,
then the aggregate) on Appendix B.1 with C1 and C3 stored twice, and an
order for a customer that does not exist.

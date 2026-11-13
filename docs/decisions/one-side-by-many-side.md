# A metric of the one side by the many side

A metric of a dataset H grouped by a field of a dataset whose rows
reference H's, such as `orders.order_count` by `order_lines.product_id`
or `customers.customer_count` by `orders.shipping_country`, was refused
(#10), since one `SELECT` over the many side counts each H row once per
row that references it. On 2026-11-10, 92 questions nobody shaped to the
model (42 from the default reports of Shopify and WooCommerce, 50 written
by an agent in the role of a store owner) were asked of the `commerce`
model of dactopus-data-models over a Shopify and a WooCommerce store, by
Haiku 5.5 and Sonnet 5.5 through the MCP server of 0.14.0: 368 sessions.
The planner refused this shape in 33 sessions on 18 questions, and 10 of
the 18 ask for it: customers per product category, orders per product,
new customers by shipping country. Agents then asked once per category
with a filter, 20 and 25 queries for the categories of the WooCommerce
store. The query compliance suite of apache/ossie asks the same in `b04`,
`b18` and `b20`.

The relational spec defines the answer (`relational_semantics.md`,
section 8.1): the input set of a measure in a group is the set of its
source rows from which at least one row of the group derives, each
counted once, and a group whose input set is empty is kept with the
empty-input value. Section 9 realizes it by reducing each group's rows to
one per row-identity key. The planner writes it in two parts:

- The spine: the question with its dimensions, the first declared key of
  H and its row filters, and no metric. It is planned as any question:
  its root is the dataset that reaches H and the dimensions, with its
  `LEFT JOIN`s, `FINAL`, dictionaries and semi-joins, and its `GROUP BY`
  gives the distinct pairs of dimension values and H key.
- The branch: the metrics of H planned over H, joined to the spine with
  `INNER JOIN` on the key and grouped by the spine's dimensions. An H row
  joins once per pair, so once per group, and a NULL or orphan key joins
  nothing.

The branch is merged with the question's other metrics as a stitched
branch ([Stitch metrics of several facts](stitching.md)), and the spine is merged too,
with no values. A group made only of rows without an H row is kept, and a
metric of H there takes its empty-input value: 0 customers for the line
of an order whose customer does not exist. Aggregate filters run on the
merged rows, as `b20` asks.

- The groups are the root's rows, as everywhere in this planner. H rows
  that no root row references are in no group, as an inner join leaves
  them out in `b04`.
- The root is a dataset the question names. With items reached from
  order lines, `customers.customer_count` by `items.category` is
  refused, and a filter on the lines (`order_lines.order_id IS NOT
  NULL`) names the fact.
  Layer 3 lists one-to-many paths through a bridge dataset, such as
  `movies.total_gross` by `actors.height` through `appearances`, among
  its future extensions (#653, Appendix A.1); a question that names no
  such dataset stays refused.
- Only whole metrics whose aggregates read H and nothing from the one
  side of H (`Planner._carry`). A metric of components keeps the
  reachability rule of #653 section 5.3 ([Reachability](components.md#reachability)).
- The spine is read twice, once in the branch and once in the merge.

Layer 3 refuses these questions (#653 section 5.3: `E_NO_PATH`, or
`E_PARTIAL_DIMENSION_REACHABILITY` next to a metric that reaches the
dimension), and its section 7 sends them to Layer 2, which answers them
as above: a row of [Answers beyond Layer 3](beyond-layer-3.md).

An answer can be literal. `session_conversion_rate` by
`purchases.currency` counts the sessions with a purchase in each
currency, so it is 1 in every group: the spec's answer to the question
asked.
On the GA4 sample it is 1.0 over 4,848 sessions in USD.

`tests/test_bridge.py` compares answers with DuckDB running the section 9
strategy, on the data of the semi-join tests with one more line: an order
with two lines of one item, a customer under two statuses, one without
orders, orders with a NULL and a missing customer, an order updated in a
`ReplacingMergeTree`, and a key of two columns. `b04`, `b18` and `b20`
moved to the answered cases of `tests/test_compliance.py`.

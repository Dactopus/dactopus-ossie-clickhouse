# Semi-joins for filters on the many side

A row filter on a dataset whose rows reference the root's, such as
`-m sessions -f "purchases.revenue > 0"` (sessions with a purchase of a
value) or `-m device_count -f "readings.reading_date = '2026-10-19'"`
(devices with a reading that day), made the referencing dataset the root,
and the check of [Refuse a metric over another dataset's rows](metric-over-other-rows.md) refused
the metric: it would count each session once per purchase. Models worked
around it with a field on the root (`sessions.has_purchase`), which cannot take the filter's
condition. Two models of dactopus-data-models asked for it (#10).

Such a filter is a semi-join: `sessions.session_key IN (SELECT
purchases.session_key FROM purchases WHERE purchases.revenue > 0)`. A root
row stays when at least one row of the filtered dataset references it and
passes the filter, and counts once, so a root row never repeats. Layer 3
(apache/ossie#653) does not define it: a filter whose dataset the metric
does not reach is `E_UNSUPPORTED_FILTER_PATH` (section 5.3), and section
9 lets an engine answer it only when the query or the model asks for that
explicitly. Appendix A.1 lists "existence filters" among the future
extensions. This executor answers it for every question, so it departs
from #653 here, as it did before #653 existed. The relational spec (`relational_semantics.md`, section 8.3)
keeps a source row once per group however many rows derive from it, which
is the same count. Whether a later version defines an anti-semi-join, and
how it treats NULL, is open.

The planner tries it only when one root cannot answer the question. It then
takes the root of the metrics, dimensions and aggregate filters alone; a
row filter on datasets that root reaches stays a `WHERE` over its joins,
and a row filter on datasets it does not reach, one of which reaches it
through many-to-one relationships, becomes the semi-join. A question
planned before plans the same: `-d devices.country -f "readings..."` keeps
readings as its root and the NULL group of readings without a device.
When the root of the metrics does not answer them either, the question is
stitched or refused with the error it got before, which names the root
and why.

- All filters on one dataset M and on datasets M reaches go into one
  subquery joined by `AND`, so one row of M passes all of them, as one
  joined row would: `-f "lines.qty > 1" -f "orders.status = 'paid'"`
  keeps a customer with a paid order that has such a line. M is the
  dataset among those filters that reaches the others. A filter on
  `items` alone, which reaches no customer, is read through the fact
  that links them when there is one (next sections).
- The subquery is planned as a question over M: its own `LEFT JOIN`s,
  `FINAL` and dictionaries. M reaches the root along one path of
  relationships (two are ambiguous, #653 section 4), with one `IN` per step,
  and a key of several columns is a tuple.
- `NOT` stays inside the subquery: `NOT readings.reading_date = '...'`
  keeps devices that have a reading on another day. A filter that holds on
  a missing row, such as `readings.value IS NULL`, is checked against rows
  that exist. Absence is asked through a field on the root, such as
  `devices.last_reading_at`. A person at the command line can also
  subtract two answers (`device_count` with and without the filter); the
  MCP server's instructions do not suggest it, since they forbid an agent
  to derive numbers.
- A NULL key references nothing: ClickHouse answers `NULL IN (...)` with
  NULL and a tuple with a NULL in it with 0, as SQL does, when
  `transform_null_in` is 0 (checked on 26.9). It is 0 by default, and
  every generated `SELECT` sets it, since a user's profile may set 1,
  under which a NULL matches a NULL.

Refused: a filter that mixes the root's datasets and M's in one
expression (split it into two filters; an `OR` across the sides cannot be
split), a dataset that both reaches the root and is reached by it
(ambiguous), and two paths from M to the root. A dimension of
M makes M one of the question's datasets: the metric is answered over its
own rows, joined to a spine of M's rows ([A metric of the one side by the many side](one-side-by-many-side.md)). A metric that reads M, `FILTER (WHERE ...)` included, makes M
one of the question's datasets, and is answered over M's rows
(its per-row expression is the author's, as in [Refuse a metric over another dataset's rows](metric-over-other-rows.md)).
Inside a stitched question each group plans
its own semi-joins: `-m device_count -m reading_count -f "readings..."`
filters readings with a `WHERE` and devices with an `IN`.

`tests/test_semi_join.py` compares answers with DuckDB running `EXISTS`, on
data where a customer has two matching orders, one has none, an order and
a line have NULL keys, two filters pass on different orders of one
customer, and an order was updated in a `ReplacingMergeTree`, so a
subquery without `FINAL` counts it. On the GA4 sample loaded with
dactopus-data-models, 4,446 sessions have a purchase with revenue above
0, as a join with `uniqExact` counts them; `sessions.has_purchase` gives
4,848, since 450 purchases carry no revenue. On a Sensor.Community load,
35 of 36 devices have a reading on 2026-10-19.

## Through a dataset both reach

A WooCommerce product is in several categories, modelled as
`product_categories (product_id, category) -> products`; order lines
reach `products` too. "Revenue of the lines in Clothing" has one meaning,
the lines whose product has a Clothing row, each once, but M (the
categories) does not reach the root (the lines) (#69). A model could answer it only through a dataset at the grain of
line and category, which repeats each line once per category and
rewrites past lines whenever the catalog changes.

The filter is a semi-join through S, the first dataset on M's
relationships that the root reaches: M's subquery keeps S rows, and the
root's path to S adds one `IN` per relationship, as M's path does.

```sql
order_lines.product_id IN (SELECT products.product_id FROM products
  WHERE products.product_id IN (SELECT product_categories.product_id
    FROM product_categories WHERE product_categories.category = 'Clothing'))
```

- S and every dataset on the root's path to it are read, with `FINAL`
  where the table needs it, though `order_lines.product_id IN (SELECT
  product_categories.product_id ...)` would be shorter. A line whose
  product does not exist has no category, as a join from the line would
  find; an order updated in a `ReplacingMergeTree` counts at its latest
  version; and the relationship from the root may name another key of S
  than M's does.
- S is found by walking M's relationships and stopping each path at the
  first dataset the root reaches. Two such datasets are two meanings (a
  line kept through its item or through its customer) and are refused
  with both named, even when one reaches the other: M's row then names
  the second one twice, directly and through the first. So are two paths
  from the root to S (apache/ossie#653 section 4).
- Only a table of S's properties qualifies: on M's path to S, a key of
  each dataset holds its columns to the next (`Planner._not_a_property`).
  `product_categories (product_id, category)` lists properties of a
  product. With `returns (return_id) -> customers` and `orders ->
  customers`, `-m order_count -f "returns.reason = 'damaged'"` would
  count the orders of customers with a damaged return. Someone asking
  that question means the orders returned, and an agent would pass the
  other number on without a word, so it is refused with what to do:
  relate returns to orders, or declare a key of returns that includes
  `customer_id` if each row does describe its customer. A dataset that
  declares no key is refused the same way, since nothing says what its
  rows are. Refused questions can be answered later without breaking an
  answer; the other way round, a wider rule narrowed later would.
- A dimension of M stays refused: by category, a line would count once
  per category.

The data in `tests/test_semi_join.py` has an item with two matching tags,
two tag filters passed by different rows, a tag of an item that does not
exist, a line on the order of a missing customer and NULL keys: a join, a
subquery that skips the shared dataset, and one that skips the orders on
the way each give another answer.

## Through a fact that links them

"Orders with a line in Clothing" has a root two steps from M: orders do
not reach products, and the categories meet nothing orders reach. A fact
reaches both, the order lines. With `refund_lines -> orders` and
`refund_lines -> products` next to the order lines, the same words
through refunds count the orders where Clothing was refunded, a smaller
number: two facts that link them apart are two meanings, and the
question names one with a filter on it:

```
-m orders.order_count -f "order_lines.order_id IS NOT NULL" -f "product_categories.category = 'Clothing'"
```

The filter on the lines makes them M of a semi-join to orders, and the
filter on categories joins that subquery as a filter of the question over
the lines, through `products` as in the previous section:

```sql
orders.order_id IN (SELECT order_lines.order_id FROM order_lines
  WHERE order_lines.order_id IS NOT NULL AND order_lines.product_id IN
    (SELECT products.product_id FROM products WHERE products.product_id IN
      (SELECT product_categories.product_id FROM product_categories
        WHERE product_categories.category = 'Clothing')))
```

- One line passes every filter on the lines and has a product in the
  category, as one joined row would: `-f "order_lines.quantity > 1"`
  keeps orders with a line of more than one item in Clothing, not orders
  with a large line and another line in Clothing.
- `order_lines.order_id IS NOT NULL` drops no line that could keep an
  order, since a line without an order keeps none. It does bind: every
  filter on a dataset the lines reach, `orders.status = 'paid'` too, then
  holds for the same line, as a filter on the lines always makes it, and
  an order without lines no longer passes it.
- A question that names no fact is read through the one fact that every
  fact linking them passes through on each of its ways to the root and
  to M (`Planner._link`), as if a filter named it with no condition.
  `line_brands -> order_lines` and `line_categories -> order_lines`
  link orders to products too, but their rows are order lines, one per
  brand or category, and reading through them drops the lines without
  one: the order lines are the reading. A model with one fact is the
  same case. Filters on two datasets the fact reaches, the dates and the
  items of a sale, go into its one subquery, as when a filter names it:
  one sale passes both.
- Otherwise it is refused with each dataset a filter could name to link
  them, as the planner would plan it, with such a filter on each as an
  example. A fact that meets the root at a shared dataset counts too
  (`favorites (customer_id, item_id)` for orders through customers), and
  one with two paths to the root, or one that M meets at two datasets,
  is left out, since naming it would be refused.
- Refusing a question that names no fact, when one fact links them,
  protects nothing: with one fact the message names only that fact, and
  agents add the filter it suggests as written, so a model that relates
  orders to products only through refund lines still answers "orders
  with Clothing" as refunded ones. With lines by brand next to the
  lines, a weaker agent could name the brands and lose the lines without
  one. The refusal stays where it asks for a choice, between facts that
  link them apart.
- A filter dataset that meets two named facts (`order_lines` and
  `refund_lines` both filtered) is refused with both named, since an
  order could be kept through either. The rules of the previous section
  hold inside the fact's subquery: one meeting dataset, a dataset of
  properties.
- Customers by the categories of their orders' lines work the same way,
  one `IN` more.

`tests/test_semi_join.py` compares these with DuckDB running `EXISTS` on
the data above, which adds an order with a large line of an untagged item
and a small line of a tagged one: a join, a subquery that skips the
products, and one subquery per filter each give another answer.

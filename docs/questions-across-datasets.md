# Questions across datasets

What the executor answers when a question touches more than one dataset,
and what it refuses because the number would be wrong. Which joins exist
is decided by the model's keys and relationships
([model authoring](model-authoring.md#keys-decide-which-joins-are-allowed));
the decision behind each rule is linked from its section.

## One root and its joins

A question is answered from one root dataset (usually the fact table) that
reaches every other dataset it touches through many-to-one relationships,
directly or through datasets in between, such as order lines to orders to
customers. Each dataset must have one path from the root: a second
relationship to it, or a direct one next to a longer path, makes the
question ambiguous, and it is refused. Metrics of
two fact tables in one question are each answered from their own root and
merged on the dimensions, so a dimension both facts reach, such as a
shared `customer`, lets an agent ask for both at once.

Decisions: [Paths of several relationships](decisions/paths.md),
[Stitch metrics of several facts](decisions/stitching.md).

## Rows that would count twice

Metrics are computed over the root's rows, so each row of a joined
dataset appears once per root row that references it, and only if one
does. The planner refuses what that would get wrong:

- An aggregate that reads only joined datasets, unless repeats do not
  change it: `MIN`, `MAX`, `APPROX_COUNT_DISTINCT` or `DISTINCT`, and
  ClickHouse's `uniq` family, `any`, `anyLast`, `argMin`, `argMax`,
  `groupUniqArray`, `groupBitAnd`, `groupBitOr`, `groupBitmap`,
  `singleValueOrNull`, and `any_value`, `min_by` and `max_by` in any case,
  with the `If`, `OrNull`, `OrDefault` or `Distinct`
  combinators (`uniqExactIf`); any aggregate with `Distinct` (`sumDistinct`).
  `SUM(store.s_number_employees)`
  over sales would count a store's staff once per sale. An argument that
  mixes root and joined columns is computed once per root row and is fine:
  `SUM(lines.qty * products.price)`. So is a dataset joined on a key of
  the root (a one-to-one extension such as `order_shipping` joined on the
  order id): its rows do not repeat.
- A metric that aggregates no column of the root at all, even with a
  repeat-safe aggregate. `COUNT(DISTINCT sessions.user_id)` in a question
  whose root is purchases (because of a purchases metric or dimension)
  counts only users with a purchase.
- `COUNT(*)` on its own in a model's metric: it names no dataset, so it
  would count stores when asked by store and sales when asked next to a
  sales metric. Declare the metric on the dataset it counts
  ([Metrics on a dataset](model-authoring.md#metrics-on-a-dataset)),
  count a key, `COUNT(orders.order_id)`, or keep it next to an
  aggregate that names the dataset, as in `SUM(orders.amount) / COUNT(*)`.

A key that is declared but stored twice, such as one customer written two
times, does not multiply the root's rows in a metric or a filter on one
dataset: the planner reads the dataset as the distinct values the
question asks of it. An expression that reads the root and that dataset
together is answered when two copies cannot give it one result: a condition
on the dataset joined to the rest with `AND`, `SUM(lines.qty *
products.price * (1 - products.discount))`, a `FILTER` or a `CASE WHEN` on
the dataset. Others, such as
`customers.weight > 0 OR orders.amount > 150` or `SUM(GREATEST(orders.amount,
customers.weight))`, would count a root row once for each copy, and are
refused; `validate` reports such a metric.

Decisions: [Refuse a metric over another dataset's rows](decisions/metric-over-other-rows.md),
[A key stored twice on the one side](decisions/key-stored-twice.md).

## A metric over two datasets

A model's metric whose aggregates each read one dataset, such as
`SUM(sales.amount) / COUNT(DISTINCT customer.id)` or sales per employee,
`SUM(sales.amount) / SUM(store.employees)`, is computed by parts, as the
Layer 3 spec requires (apache/ossie#653, section 5.2): each aggregate over
the rows of its own dataset, then the expression over the results. The
first ratio divides all sales by all customers, buyers or not, and joins
the customers to the sales nowhere. Without dimensions that is one number from
each dataset. With dimensions, each part is grouped by them and the groups
are merged: a group one dataset lacks gives its part the value over no
rows (NULL for a sum, 0 for a count), so a region with customers and no
sales shows NULL, and the group of sales with no matching customer shows
NULL too, as dividing by 0 does.

Every dimension and every row filter must be reachable from the dataset of
each aggregate through many-to-one relationships. Sales per customer by
`store.state` is refused: `customer` does not reach `store`, and the
message says which dimension, which metric and which dataset. Sales per
buyer, the customers who bought, asks another question, and the dataset
that holds the sales answers it. Declare it on `sales`
([Metrics on a dataset](model-authoring.md#metrics-on-a-dataset)), with
its own fields:

```yaml
datasets:
  - name: sales
    metrics:
      - name: sales_per_buyer
        expression:
          sql: SUM(amount) / COUNT(DISTINCT customer_id)
```

`sales.sales_per_buyer` works with every dimension `sales` reaches,
`store.state` included, and with filters on the many side. A filter on
`sales.quantity > 50` next to the ratio of two datasets divides those
sales by the customers with at least one such sale.

Three things a metric of several datasets cannot have, and it is refused
in every question and reported by `validate`: a window function, such as
`SUM(SUM(sales.amount)) OVER () / COUNT(DISTINCT customer.id)`, a column
outside every aggregate, such as `SUM(sales.amount) * customer.rate`, and
`COUNT(*)` next to aggregates of several datasets, which names no dataset.
An aggregate whose arguments read several datasets, such as
`SUM(lines.qty * products.price)`, or a `FILTER (WHERE ...)` on another
dataset, is computed over one join: one value for each row of the root.
A metric with such an aggregate is not split into parts, and it may not
also have an aggregate over a dataset those arguments do not read:
`SUM(lines.qty * products.price) / COUNT(DISTINCT customers.id)` and
`SUM(lines.qty * products.price) / MAX(store.employees)` are refused in
every question, since one join would count the customers or the stores over
the lines' rows. Declare the ratio on the dataset it means, or write two
metrics. `SUM(lines.qty * products.price) / SUM(lines.qty)` stays within
`lines` and `products` and is computed over one join. A `COUNT(*) FILTER
(WHERE ...)` names no dataset by its filter: it counts the rows of the one
dataset that all of the metric's aggregates read, and is refused next to
aggregates of another.

Decision: [Evaluate each aggregate over its own dataset](decisions/components.md).

## Filters on the many side

A filter on a dataset whose rows reference the metric's, the many side
of a relationship, keeps each row that at least one matching row
references, once. `-m sessions -f "purchases.revenue > 0"` counts
sessions with a purchase of some value; `-m device_count -f
"readings.reading_date = '2026-10-19'"` counts devices with a reading
that day. The planner writes it as `sessions.session_key IN (SELECT
purchases.session_key FROM purchases WHERE ...)`, along a path of
several relationships too (customers through orders through order
lines). Filters on one such dataset, and on the datasets it reaches
(`items` through order lines), must hold for the same row: a customer
with a paid order and a large order line counts only when the large line
is on a paid order.

A filter on a dataset whose rows reference what the metric's rows
reference works the same way, through the dataset both reach. With
`order_lines -> products` and `product_categories -> products`, `-m
order_lines.line_revenue -f "product_categories.category = 'Clothing'"`
adds up the lines whose product has a Clothing row, each line once. The
planner reads `products` too, so a category of a product that does not
exist keeps no line. The filter dataset must list properties of the
shared one: declare a key that holds its columns to it, here
`primary_key: [product_id, category]`. A dataset with a key of its own,
such as `returns (return_id) -> customers` next to `orders ->
customers`, may hold facts of their own, and `-m order_count -f
"returns.reason = 'damaged'"` is refused: through customers it would
count the orders of customers with a damaged return, not the orders
returned. Relate `returns` to `orders` for those. A filter dataset that
meets the metric's at two datasets is refused, since either could be
meant.

The orders with a line in Clothing are read through the fact between
orders and products, the order lines: `-m orders.order_count -f
"product_categories.category = 'Clothing'"`. When several facts link them
apart, such as order lines and refund lines, the question names one with
a filter on it, `-f "order_lines.order_id IS NOT NULL"`; without one it
is refused, and the message names each dataset a filter could name, with
a filter on it that keeps every one of its rows. Every filter on the
lines and on the categories holds for one line, and so does a filter on
orders next to them: `orders.status = 'paid'` then keeps the orders
whose paid status and Clothing line go together.

Such a filter cannot ask for rows without a match: `NOT
readings.reading_date = '2026-10-19'` counts devices that have a reading
on another day. For absence, give the root a field
(`devices.last_reading_at`, `sessions.has_purchase`). Refused, with a message that says why: a
filter that mixes columns of both sides in one expression (write two
filters; an `OR` across the sides cannot be split).

Decision: [Semi-joins for filters on the many side](decisions/semi-joins.md).

## A metric grouped by the many side

A metric of the one side grouped by a field of the many side counts each
of its rows once in each group. `-m orders.order_count -d
order_lines.product_id` counts the orders with a line of each product,
an order with two lines of one product once; `-m customers.customer_count
-d orders.shipping_country` counts each customer once per country its
orders ship to, so a customer can count under two countries. The groups
are those of the many side's rows, as for any question: a customer
without orders is in none, and the line of an order whose customer does
not exist makes a group of 0 customers. Filters on the many side hold for
the rows that make the group. The dataset whose field groups the question,
or one a filter names, must reach the metric's: with items reached from
order lines, `-m customers.customer_count -d items.category` is refused,
and `-f "order_lines.order_id IS NOT NULL"` names the lines that link
them. A metric whose aggregates read several datasets, such as sales per
customer above, is not answered this way.

Decision: [A metric of the one side by the many side](decisions/one-side-by-many-side.md).

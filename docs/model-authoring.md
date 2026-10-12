# Model authoring for ClickHouse

A model for dactopus-ossie-clickhouse is an ordinary Ossie model: the standard's
[specification](https://github.com/apache/ossie) says what goes in it, and
nothing ClickHouse-specific is required. This page lists what the executor
works out on its own, and the few things worth knowing when the target is
[ClickHouse](https://clickhouse.com/docs). The TPC-DS reference model in
[tests/fixtures/tpcds.yaml](../tests/fixtures/tpcds.yaml) is a complete
example.

## Sources

`source` is `table` or `database.table`. A bare `table` is read in the
connection's database: the one in the URL path
(`http://user:password@host:8123/analytics`), else the ClickHouse user's
default database. Leave the database out when the deployment chooses it,
so one model serves a database per customer, or dev and prod side by side.
`validate --url` and `serve` print the database they read.

A name with a dot, a space or another special character is quoted with
backticks or double quotes, as in SQL: `` `my.db`.`orders` `` or
`"Sales DB".orders`. dbt-clickhouse writes every source quoted, and that
works as is. Unquoted text between dots is taken as the name it spells,
so a table replicated from Postgres as `orders$v2` needs no quotes. A
backslash is not accepted in a source, quoted or not, nor `--`, `/*` or
`;` outside quotes; `validate --url` names such a source.

A three-part `database.schema.table` is accepted and the middle part
dropped, since ClickHouse has no schema level; this is what lets a model
written for another warehouse run unchanged. A query as a source is not
supported.

## Keys decide which joins are allowed

`primary_key` and `unique_keys` hold physical column names, as the spec
says. They are not decoration: a relationship is used only when its
`to_columns` cover (include all columns of) the primary key or a unique
key of the target dataset. That is how the planner knows the join is
many-to-one and cannot multiply fact rows; extra columns, such as a
`tenant_id` next to the key, only narrow the match. A dimension dataset
without a declared key is unreachable.

The planner trusts the declared key; ClickHouse does not enforce
uniqueness. In a multi-tenant schema an id is often unique only within a
tenant, and then the key is `[tenant_id, customer_id]`, not
`[customer_id]`. Declared too narrow, it lets a relationship on
`customer_id` alone through, and that join multiplies rows.

```yaml
datasets:
  - name: item
    source: tpcds.item
    primary_key: [i_item_sk]
relationships:
  - name: store_sales_to_item
    from: store_sales
    to: item
    from_columns: [ss_item_sk]
    to_columns: [i_item_sk]
```

A question is answered from one root dataset (usually the fact table) that
reaches every other dataset it touches through many-to-one relationships,
directly or through datasets in between, such as order lines to orders to
customers. Each dataset must have one path from the root: a second
relationship to it, or a direct one next to a longer path, makes the
question ambiguous, and it is refused. Metrics of
two fact tables in one question are each answered from their own root and
merged on the dimensions, so a dimension both facts reach, such as a
shared `customer`, lets an agent ask for both at once.

Each aggregate of a metric is answered on its own the same way. In sales
per employee, `SUM(sales.amount) / SUM(store.employees)`, sales are summed
over sales and staff over stores, by any dimension both reach, such as
`store.state`. An aggregate whose dataset cannot reach a dimension or
condition of the question on its own (staff by `item.brand`: store reaches
items only through sales) is computed over the root's rows, so each row of
a joined dataset appears once per root row that references it, and only if
one does. The planner refuses what that would get wrong:

- An aggregate that reads only joined datasets, unless repeats do not
  change it: `MIN`, `MAX`, `ANY_VALUE`, `BOOL_AND`, `BOOL_OR`,
  `APPROX_COUNT_DISTINCT` or `DISTINCT`. `SUM(store.s_number_employees)`
  over sales would count a store's staff once per sale. An argument that
  mixes root and joined columns is computed once per root row and is fine:
  `SUM(lines.qty * products.price)`. So is a dataset joined on a key of
  the root (a one-to-one extension such as `order_shipping` joined on the
  order id): its rows do not repeat.
- A metric that aggregates no column of the root at all, even with a
  repeat-safe aggregate. `COUNT(DISTINCT sessions.user_id)` in a question
  whose root is purchases (because of a purchases metric, dimension or
  filter) counts only users with a purchase.
- `COUNT(*)` on its own: it names no dataset, so it would count stores when
  asked by store and sales when asked next to a sales metric. Count a key
  instead, `COUNT(orders.order_id)`, or keep it next to an aggregate that
  names the dataset, as in `SUM(orders.amount) / COUNT(*)`.

So the population of an aggregate depends on the question. In
`SUM(sales.amount) / COUNT(DISTINCT customer.id)`, the count is of every
customer in a total or by a customer field, and of the customers who
bought by a store field. For buyers in every question, count a column of
sales: `COUNT(DISTINCT sales.customer_id)`. To filter one dataset's
metric by another dataset, give the first a field for it
(`sessions.has_purchase`).

## Expressions

Each field and metric carries expressions in one or more dialects.
`OSSIE_SQL_2026` is preferred, `ANSI_SQL` is the fallback and what models
carry today. Within a field, bare column names are the dataset's own
columns; a field may not reach into another dataset. Metrics reference
fields as `dataset.field`.

The spec's function catalog is translated to ClickHouse. Functions
ClickHouse lacks (`DATEADD`, `DATEDIFF`, `DATE_PART`, `SPLIT_PART`,
`CONTAINS`, `IFF`, `ZEROIFNULL` and others) are rewritten on the syntax
tree; `EXTRACT(DAYOFWEEK ...)` and `DAYOFYEAR` become `toDayOfWeek` and
`toDayOfYear`. Where ClickHouse disagrees with the spec, the spec wins:
`REGEXP_REPLACE` replaces every match, `DAYOFWEEK` is ISO (Monday is 1),
float division by zero and a one-row `STDDEV` come back as NULL, not
`inf` or `nan`.
Subqueries and statements inside an expression are rejected.

`PERCENTILE_CONT` and `MEDIAN` are exact, as the spec requires, and hold
every value of the group in memory; `APPROX_PERCENTILE` is the fast
approximate choice for large groups. Like the spec, they accept numbers
only: over a `Date`, `DateTime` or `String` column ClickHouse refuses the
query, and `validate` does not catch it. For a median date, write
`PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ...)`, which returns one of
the values. `PERCENTILE_DISC` cannot take `OVER`.

Window functions over aggregates (`RANK() OVER (...)`, `LAG(SUM(x))`)
pass through in the same `SELECT`; their `PARTITION BY` and `ORDER BY`
columns are grouping keys and may come from any joined dataset. The spec cannot say which grain a
window metric needs, so the question has to supply the right dimensions.
A window metric cannot be used in a filter (ClickHouse does not allow a
window function in `HAVING`); the planner rejects it with a message.
Frames are the spec's: `ROWS` frames that hold the current row and
`RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`. Others, and
`NULLS FIRST | LAST`, are rejected; NULLs sort last in both directions.

## Time and derived dimensions

The spec has no time grain and no derived-dimension syntax yet, so declare
what you need as fields:

```yaml
- name: sale_month
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: DATE_TRUNC('month', ss_sold_date)
  dimension: {is_time: true}
- name: price_band
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: CASE WHEN ss_sales_price < 10 THEN 'low' WHEN ss_sales_price < 100 THEN 'mid' ELSE 'high' END
```

A calendar table with year, month and day columns, as in TPC-DS, works
just as well.

## Deduplication

Tables with an engine in the
[`ReplacingMergeTree`](https://clickhouse.com/docs/engines/table-engines/mergetree-family/replacingmergetree)
family (`ReplicatedReplacingMergeTree` on a cluster,
`SharedReplacingMergeTree` on ClickHouse Cloud) keep change history and are read with
[`FINAL`](https://clickhouse.com/docs/sql-reference/statements/select/from#final-modifier), so a
metric sees each row once. This is learned from
[`system.tables`](https://clickhouse.com/docs/operations/system-tables/tables), and it looks
through tables that hold no rows of their own:

- a [`Distributed`](https://clickhouse.com/docs/engines/table-engines/special/distributed)
  table, the usual source on a cluster, is read with `FINAL` when its
  table on each shard is such a table;
- a [materialized view](https://clickhouse.com/docs/sql-reference/statements/create/view#materialized-view),
  when the table it writes to is;
- a [`Merge`](https://clickhouse.com/docs/engines/table-engines/special/merge) table,
  when any table it reads is. ClickHouse applies `FINAL` to those and
  reads the rest as they are.

`FINAL` on a `Distributed` table merges versions within each shard, not
across shards: every version of a row must land on the same shard, so
shard by the table's key (for example `Distributed(cluster, shop,
orders_local, cityHash64(order_id))`), not by `rand()`.

The table on each shard must exist on the server the URL points to, and
the connected user must see it, as must the table a materialized view
writes to (reading the view needs no grant on that table). When it is
hidden, or the server is older than ClickHouse 26.6, which does not name
the table a view writes to, this project cannot tell whether the stored
rows keep versions:
the dataset is read without `FINAL`, and `validate --url` names it, but
only when run with the URL of the user who cannot see it, as the MCP
server or analysts connect. When their rights are narrower than yours,
run it with their URL, or say so in the model:

```yaml
- name: orders
  source: shop.orders
  custom_extensions:
    - vendor_name: CLICKHOUSE
      data: '{"dedup": "final"}'
```

`"final"` adds `FINAL` whatever the engine, an error if the stored rows
are in a plain `MergeTree`. To read a table raw (for example, to count
versions), turn it off per dataset:

```yaml
- name: orders_raw
  source: shop.orders
  custom_extensions:
    - vendor_name: CLICKHOUSE
      data: '{"dedup": "none"}'
```

`custom_extensions`, the spec's escape hatch for vendor settings, with
`vendor_name: CLICKHOUSE` is the namespace this project owns; `data` is
JSON. `dedup` (`"final"` or `"none"`) is the only key today.

`FINAL` drops deleted rows only when the engine declares its delete
column, as in `ReplacingMergeTree(version, is_deleted)`. CDC tools that
write the flag as a plain column leave deleted rows in place:
[ClickPipes and PeerDB](https://clickhouse.com/docs/integrations/clickpipes/postgres/deduplication)
create `ReplacingMergeTree(_peerdb_version)` and mark deletes in
`_peerdb_is_deleted`, so a metric over such a table counts deleted rows.
Point the dataset at a view that filters them out:

```sql
CREATE VIEW shop.orders_current AS
SELECT * FROM shop.orders FINAL WHERE _peerdb_is_deleted = 0
```

A view (not a materialized one) holds no versions of its own, so it is read as is.

## Dictionaries

A dataset whose source is a ClickHouse [dictionary](https://clickhouse.com/docs/sql-reference/dictionaries)
with a single key, joined on that key, is read with
[`dictGetOrNull`](https://clickhouse.com/docs/sql-reference/functions/ext-dict-functions) and the
join disappears. The key column itself is not a dictionary attribute, so
a field over it is read as the joining column, NULL when the dictionary
has no such key, exactly as the join would answer. This is learned from
[`system.dictionaries`](https://clickhouse.com/docs/operations/system-tables/dictionaries), which the connected user needs a grant
to read; without it the dictionary is joined like a table, which is still
correct, only slower. The same happens when the dictionary's or its
database's name has a dot: `dictGetOrNull` cannot address it.

## Names

Questions resolve names case-insensitively, as the spec requires. The SQL
uses names exactly as the model writes them, because ClickHouse is
case-sensitive: write `source`, keys and columns the way the database
spells them.

## Descriptions and AI hints

`description`, `ai_context.synonyms` and `ai_context.instructions` are
what agents see through `list_model`, `search_model` and
`describe_object`. The model's own `ai_context.instructions` also go into
the MCP server's instructions, which agents get before any tool call.
Write there what is true of this model's data; rules for any model, such
as not stating numbers no query returned, are the server's already. An
agent that reads "gross sales" finds `total_sales` only if the synonym is
there. Write them for the reader who does not know the schema.

## Validation

```bash
dactopus-ossie-clickhouse validate model.yaml
dactopus-ossie-clickhouse validate model.yaml --url http://user:password@host:8123
```

Without `--url`: schema, the pinned version (`0.2.0.dev0`), unique names,
relationships that resolve, expressions in a translatable dialect,
joins the planner accepts, and metrics no question can answer (such as
one naming an unknown field or a bare `COUNT(*)`). With
`--url`: every source exists, every column a field, key or relationship
uses exists. Run it as a user who can read everything the model names;
run as a restricted user it reports that user's view, not the model's
correctness.

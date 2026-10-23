# Access control

[ClickHouse](https://clickhouse.com/docs/operations/access-rights) decides who sees what. This
project adds no users, no authentication and no permissions of its own; it makes the model agree
with what ClickHouse already enforces.

## Queries run as the connected user

The CLI and the MCP server connect with the credentials in the URL
(`http://user:password@host:8123`, or `OSSIE_CLICKHOUSE_URL`). Every query
runs as that user, so table grants, column grants and
[row policies](https://clickhouse.com/docs/sql-reference/statements/create/row-policy) apply
exactly as they would to a hand-written query.

## The model is trimmed to match

ClickHouse lists in [`system.tables`](https://clickhouse.com/docs/operations/system-tables/tables)
and `system.columns` only what the connected user may read. Before planning, the model is cut down to that:

- a dataset whose source the user cannot read is removed;
- a field whose column the user cannot read is removed;
- a relationship whose join columns the user cannot read is removed, so
  they never appear in generated SQL or in a ClickHouse error;
- relationships and metrics that rest on anything removed go with them.

A hidden object never appears in SQL, in error messages or in "did you
mean" suggestions, so an agent cannot learn what exists from a typo.

Row policies need no handling at all: the rows are filtered by the server.

## One agent, one ClickHouse user

Give each agent, or each group of people behind an agent, its own
ClickHouse user with the rights they should have:

```sql
CREATE USER analyst IDENTIFIED BY '...';
GRANT SELECT ON shop.orders TO analyst;
GRANT SELECT(code, name) ON shop.country TO analyst;
CREATE ROW POLICY eu ON shop.orders FOR SELECT USING region = 'EU' TO analyst;
```

Then run `dactopus-ossie-clickhouse serve` (or `query`) with that user's URL. The
[MCP](https://modelcontextprotocol.io) server is one process per user; a shared multi-user server with OAuth
is designed but not built (see [design.md](design.md)).

Reading [`system.dictionaries`](https://clickhouse.com/docs/operations/system-tables/dictionaries)
needs an explicit grant. Without it,
dictionaries are joined as tables, which is correct but slower.

## Policy file: hide more than grants do

Some restrictions the database cannot express: hide a metric from a role,
keep a dataset out of an agent's view although the user may read the
table. An optional YAML file, keyed by ClickHouse user or role name,
does that:

```yaml
analyst:                       # user or role, matched case-insensitively
  hidden_datasets: [customer]
  hidden_fields: [customer.c_email_address]
  hidden_metrics: [customer_lifetime_value, store_sales.total_quantity]
```

A metric declared on a dataset is named `dataset.metric`, as a question
names it. Pass the file with `--policy policy.yaml`. Entries for the connected user and for
each of its enabled roles are combined. The file only hides; it can never
grant anything the database refuses. A hidden field takes with it what reads
it: the metrics, the keys and the relationships that name it, so hiding a key
field leaves its dataset unreachable by a join.

## Validation runs with full rights

`dactopus-ossie-clickhouse validate --url` is a model owner's tool: it reports
sources and columns the connected user cannot see as missing. Run it as a
user who can read everything the model names.

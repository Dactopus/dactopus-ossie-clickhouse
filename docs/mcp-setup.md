# MCP setup for desktop agents

The server speaks [MCP](https://modelcontextprotocol.io) over stdio: the
agent launches it as a subprocess.
ClickHouse credentials come from the environment (`OSSIE_CLICKHOUSE_URL`,
default `http://127.0.0.1:8123`) or `--url`; `https://` connects with TLS
on any port. Everything the agent sees and
runs is limited to that ClickHouse user. The zone of today and of times
without an offset comes from `OSSIE_CLICKHOUSE_TIME_ZONE` or
`--time-zone` when the deployment's differs from the model's
([Time zone](model-authoring.md#time-zone)).

## Claude Desktop

Install with the `[mcp]` extra (see the README), then add to
`claude_desktop_config.json`
([Settings, Developer, Edit Config](https://modelcontextprotocol.io/quickstart/user)). Replace
the placeholders: the absolute path of the command (`which
dactopus-ossie-clickhouse`; desktop apps run with a minimal `PATH`), the absolute
path of the model, and the ClickHouse URL with the credentials of the
user the agent should act as. For a store in another zone than the
model's, add `"OSSIE_CLICKHOUSE_TIME_ZONE": "America/New_York"` to `env`.

```json
{
  "mcpServers": {
    "dactopus-ossie-clickhouse": {
      "command": "/ABSOLUTE/PATH/TO/dactopus-ossie-clickhouse",
      "args": ["serve", "/ABSOLUTE/PATH/TO/model.yaml"],
      "env": {"OSSIE_CLICKHOUSE_URL": "http://USER:PASSWORD@127.0.0.1:8123"}
    }
  }
}
```

From a checkout of this repository instead, use `uv` as the command
(absolute path, `which uv`) with the arguments
`["run", "--directory", "/ABSOLUTE/PATH/TO/dactopus-ossie-clickhouse", "dactopus-ossie-clickhouse", "serve", "/ABSOLUTE/PATH/TO/model.yaml"]`.

Restart the app. If it reports "Server disconnected", open
`~/Library/Logs/Claude/mcp-server-dactopus-ossie-clickhouse.log`: a placeholder left
in place shows up as `No such file or directory`. The server appears with
the four tools below. Ask in business terms, for example "sales by item
category in 1998"; the agent should call `list_model`, maybe
`describe_object`, then `query`.

## Claude Code and other clients

Any MCP client that supports stdio works the same way. For
[Claude Code](https://docs.claude.com/en/docs/claude-code/mcp):

```bash
claude mcp add dactopus-ossie-clickhouse -e OSSIE_CLICKHOUSE_URL=http://USER:PASSWORD@127.0.0.1:8123 -- \
  dactopus-ossie-clickhouse serve /path/to/model.yaml
```

## Checking without an agent

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | dactopus-ossie-clickhouse serve model.yaml
```

## Tools

Every tool is one library call; the model it sees is already trimmed to
what the connected user may read. A failure comes back as a result with
an `error` field, never as a protocol error, so the planner's "did you
mean" hint reaches the agent.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_model` | none | `name`, `description`, AI hints, `datasets` (name, description, synonyms, field count), `metrics` (name, description, synonyms; a metric declared on a dataset is named `dataset.metric`), `relationships` (`from -> to`), `time_zone` (the one in effect: `--time-zone`, `OSSIE_CLICKHOUSE_TIME_ZONE`, the model's, UTC; today, the current time and times without an offset are there), `today` and `now` (the date and time there by the server's clock, what `CURRENT_DATE` and `CURRENT_TIMESTAMP` are; `error` instead when the server cannot tell), `usage`. |
| `search_model` | `text` | Datasets, fields and metrics whose name, description or synonyms contain every word of `text`, up to 20 datasets and fields, then up to 20 metrics: `kind` (`dataset`, `field`, `metric`), `name`, `description`, `synonyms`. |
| `describe_object` | `name`: dataset, `dataset.field` or metric, case-insensitive | The object with `instructions` and `examples` from `ai_context`. A dataset lists `fields` (with `datatype`, `is_time`), `relationships` and its own `metrics`; a field or metric carries its `expression` and `datatype`. Unknown name: `error` with similar names. |
| `query` | `metrics`, `dimensions` (`dataset.field`), `filters`, `order_by` (all lists of strings, default empty), `limit` (default 100) | `columns`, `rows`, `row_count`, `truncated` (true when `limit` cut off further rows), `sql` (with the question's `limit`). |

`filters` are Ossie expressions: over `dataset.field` they go to `WHERE`
(`date_dim.d_year = 1998`); over a metric name or an aggregate they go to
`HAVING` (`total_sales > 1000000`). A window metric cannot be filtered:
select it and filter the rows. `order_by` names metrics or dimensions,
`"name desc"` for descending; empty means the first metric descending.
Ties are broken by the dimensions, ascending. Rows without a value come
last either way, so `limit` may cut them off. A `Time` or `DateTime` value
reads as ClickHouse prints it, a `DateTime` with its offset from UTC
(`-01:02:03`, `2024-03-15 10:30:45+00:00`), as in the CLI's output, and
works as a filter literal.
The server's `instructions` tell the agent to start with `list_model`,
to write SQL only in filters, never to add up or rank rows itself, to
state only numbers a query returned, and which time zone today and times
without an offset are in. Today, yesterday and other periods counted from
now go in a filter as `CURRENT_DATE` or `CURRENT_TIMESTAMP`, never as a
date the agent knows: its user's date may differ from the deployment's.
They end with the model's own
`ai_context.instructions`, so an agent that starts with `search_model`
still gets them; `list_model` repeats them for clients that ignore server
instructions.

## Access

The process runs as one ClickHouse user. Give each agent its own user with
the grants and row policies it should have; the model it sees is trimmed
to match. `--policy policy.yaml` hides further objects per user or role.
Details in [access-control.md](access-control.md).
A remote, multi-user server with OAuth is designed but not built; see [design.md](design.md).

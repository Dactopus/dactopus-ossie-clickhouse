# MCP setup for desktop agents

The server speaks [MCP](https://modelcontextprotocol.io) over stdio: the
agent launches it as a subprocess.
ClickHouse credentials come from the environment (`OSSIE_CLICKHOUSE_URL`,
default `http://127.0.0.1:8123`) or `--url`. Everything the agent sees and
runs is limited to that ClickHouse user.

## Claude Desktop

Install with the `[mcp]` extra (see the README), then add to
`claude_desktop_config.json`
([Settings, Developer, Edit Config](https://modelcontextprotocol.io/quickstart/user)). Replace
the placeholders: the absolute path of the command (`which
dactopus-ossie-clickhouse`; desktop apps run with a minimal `PATH`), the absolute
path of the model, and the ClickHouse URL with the credentials of the
user the agent should act as.

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
`describe_object`, then `execute_query`.

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
what the connected user may read. A refused or failed query comes back as
a tool error, never as a protocol error, so the planner's "did you mean"
hint reaches the agent.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_model` | none | `name`, `description`, AI hints, `datasets` (name, description, synonyms, field count), `metrics` (name, description, synonyms), `relationships` (`from -> to`), `usage`. |
| `search_model` | `text` | Up to 20 objects whose name, description or synonyms contain every word of `text`: `kind` (`dataset`, `field`, `metric`), `name`, `description`, `synonyms`. |
| `describe_object` | `name`: dataset, `dataset.field` or metric, case-insensitive | The object with `instructions` and `examples` from `ai_context`. A dataset lists `fields` (with `datatype`, `is_time`) and `relationships`; a field or metric carries its `expression` and `datatype`. Unknown name: `error` with similar names. |
| `execute_query` | `data_source_id` (from `list_model` or the server instructions) and `query`, a Layer 3 query object (below) | The rows as an embedded CSV resource, `result.csv`, and `structuredContent` (below). Refused: a tool error (`isError`) with `error` (`code`, `message`, `retryable`) and `suggestions` (`kind`, `message`). |

`list_model` also returns `binding`: the `data_source_id`, the model's
name and revision (a hash of the model file's content, so it changes when
the model does), the profile and Layer 3 revisions, and what this server
refuses.

`execute_query` is the tool of the `execute_query` profile draft,
[apache/ossie#529](https://github.com/apache/ossie/pull/529) `0.4-draft`
at `b5418ee`; its `inputSchema` and `outputSchema` are the profile's. The
`query` object is Ossie's Layer 3 draft,
[apache/ossie#246](https://github.com/apache/ossie/pull/246) at `cc0d070`:

| Key | Value |
| --- | --- |
| `measures` | Metric names. Ad-hoc aggregates are refused (`UNSUPPORTED_QUERY`). |
| `dimensions` | `dataset.field` names to group by. |
| `where` | A condition or a non-empty list of them (AND) over `dataset.field`, before aggregation: `date_dim.d_year = 1998`. |
| `having` | A condition or a non-empty list over metric names, aggregates and the query's dimensions, after aggregation: `total_sales > 1000000`. A window metric cannot be filtered: select it and filter the rows. |
| `order_by` | `[{"field": "total_sales", "direction": "DESC", "nulls": "LAST"}]`: selected measures or dimensions; `direction` is `ASC` (default) or `DESC`. Without it, the first measure descending, NULLs last. Ties are broken by the dimensions, ascending. With it, NULL sorts as the highest value, first descending, unless `nulls` says otherwise; a `limit` may then cut rows off. |
| `limit` | Rows. Without it at most 100 come back, and the answer says `truncated` if there were more. |
| `fields` | A scalar query; refused (`UNSUPPORTED_QUERY`). |

An answer's `structuredContent` holds `contract_version`, `status`,
`data_source_id`, `model` (`id`, `revision`), `preview` (columns with
their logical type, and up to 100 rows: exact numbers as strings, dates in
ISO 8601, a `Time` as ClickHouse prints it, `-01:02:03`), `result` (`row_count`, `completeness`, the CSV resource, and
the SQL under `extensions["io.github.dactopus/clickhouse"]`),
`diagnostics`, `suggestions` and `filter_value_alternatives` (always
empty: filter values are not looked up). In the CSV, NULL is an unquoted
`\N` and a string that starts with a backslash gets one more.

`error.code` is #246's code where one applies (`E_NAME_NOT_FOUND`,
`E_NO_PATH`, `E_AMBIGUOUS_PATH`, `E3013_NO_STITCHING_DIMENSION`,
`E_EMPTY_AGGREGATION_QUERY`, `E_EMPTY_SCALAR_QUERY`, `E_MIXED_QUERY_SHAPE`,
`E_AGGREGATE_IN_WHERE`, `E_WINDOW_IN_WHERE`, `E_NON_AGGREGATE_IN_HAVING`,
`E_MIXED_PREDICATE_LEVEL`, `E_PRIMARY_KEY_REQUIRED`), otherwise a common
code of the profile: `INVALID_ARGUMENT` for arguments or a clause value
outside its schema, `SOURCE_UNAVAILABLE` for another `data_source_id`,
`QUERY_INVALID` for an expression that does not parse,
`UNSUPPORTED_QUERY` for one this version cannot answer, `BACKEND_ERROR`
when ClickHouse fails it (`retryable` when the connection did). A
suggestion of `kind` `name` is a known name to use instead; `query`
advises how to ask instead.
The server's `instructions` tell the agent to start with `list_model`,
name the `data_source_id` to pass, and tell it never to write SQL, never
to add up or rank rows itself, and to state only numbers a query
returned. They end with the model's own
`ai_context.instructions`, so an agent that starts with `search_model`
still gets them; `list_model` repeats them for clients that ignore server
instructions.

## Access

The process runs as one ClickHouse user. Give each agent its own user with
the grants and row policies it should have; the model it sees is trimmed
to match. `--policy policy.yaml` hides further objects per user or role.
Details in [access-control.md](access-control.md).
A remote, multi-user server with OAuth is designed but not built; see [design.md](design.md).

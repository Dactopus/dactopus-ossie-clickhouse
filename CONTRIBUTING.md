# Contributing

Bug reports, questions about a translation, and pull requests are welcome.
Everything in this repository is in English: code, comments, commit
messages, issues.

## Reporting a wrong answer

The failure mode this project guards against is SQL that runs and returns
the wrong value. If you see one, open an issue with the model (or the part
of it involved), the question (metrics, dimensions, filters), the SQL from
`dactopus-ossie-clickhouse sql`, and what the value should be, with the spec section
or the hand-written query that says so.

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Dactopus/dactopus-ossie-clickhouse
cd dactopus-ossie-clickhouse
uv sync
uv run pytest -m "not integration"     # unit tests, no server needed
```

Integration tests need a [ClickHouse](https://clickhouse.com/docs) server reachable over
[HTTP](https://clickhouse.com/docs/interfaces/http) at `OSSIE_CLICKHOUSE_URL` (default
`http://127.0.0.1:8123`) and skip without one. The URL may carry a user
and password (`http://default:secret@host:8123`); the tests pass them on
to the CLI and to the test dictionaries' source. CI runs ClickHouse 26.9;
older releases are untested. Access-control tests also need
[SQL access management](https://clickhouse.com/docs/operations/access-rights#enabling-access-control)
enabled for the connecting user, and skip otherwise. Without a server at
hand, the [official Docker image](https://clickhouse.com/docs/install/docker) gives one with
access management on:

```bash
docker run -d --name ch -p 8123:8123 -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 clickhouse/clickhouse-server
uv run pytest
```

Tests against the [TPC-DS reference model](https://github.com/apache/ossie/blob/main/examples/tpcds_semantic_model.yaml)
need the `tpcds` database with the five tables the model uses, and skip
when it is absent. CI loads it and sets `OSSIE_CLICKHOUSE_REQUIRED=1`, which
turns every such skip into a failure, so nothing passes there by being
skipped. [DuckDB](https://duckdb.org),
already a dev dependency, generates the data at scale factor 1 with its
[tpcds extension](https://duckdb.org/docs/stable/core_extensions/tpcds):

```bash
mkdir -p /tmp/tpcds && uv run python -c "
import duckdb; c = duckdb.connect(); c.execute('INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf=1)')
for t in ['store_sales', 'date_dim', 'customer', 'item', 'store']:
    c.execute(f\"COPY {t} TO '/tmp/tpcds/{t}.parquet' (FORMAT PARQUET)\")"
```

Copy the Parquet files into the server's
[`user_files_path`](https://clickhouse.com/docs/operations/server-configuration-parameters/settings#user_files_path)
(`SELECT value FROM system.server_settings WHERE name = 'user_files_path'`;
for the Docker container above, `docker cp /tmp/tpcds/. ch:/var/lib/clickhouse/user_files/`),
then load them with the [`file()`](https://clickhouse.com/docs/sql-reference/table-functions/file)
table function:

```bash
URL=http://127.0.0.1:8123
curl -s "$URL" --data-binary "CREATE DATABASE IF NOT EXISTS tpcds"
for t in store_sales date_dim customer item store; do
  curl -s "$URL" --data-binary "CREATE TABLE tpcds.$t ENGINE = MergeTree ORDER BY tuple() AS SELECT * FROM file('$t.parquet', Parquet)"
done
```

Two tests read a checkout of [apache/ossie](https://github.com/apache/ossie)
at `OSSIE_REPO` and skip without one: the canary that keeps
`tests/fixtures/tpcds.yaml` equal to `examples/tpcds_semantic_model.yaml`,
and `tests/test_compliance.py`, which runs the query compliance cases of
`compliance/query` on the server (the SCOTT data goes into a database
`scott`, dropped afterwards). The weekly workflow runs both on Ossie's
`main`.

```bash
OSSIE_REPO=../ossie uv run pytest tests/test_compliance.py tests/test_canaries.py
```

```bash
uv run ruff check src tests research scripts
uv run ruff format src tests research scripts
```

## Rules that shape the code

[AGENTS.md](AGENTS.md) is the full list; the ones that matter most for a
pull request:

- The [Ossie](https://github.com/apache/ossie) description is the single source of truth. Semantics that
  belong in the model are never hardcoded.
- Follow the standard exactly. When ClickHouse differs from the spec,
  handle it in the translator; do not change what the standard means.
- Every change to library code comes with a test. Translation tests
  compare values, not only that ClickHouse accepts the SQL: DuckDB gives
  the default expected value, a hand-written expectation with a spec
  reference where DuckDB lacks the function or disagrees, and the spec
  text wins over any engine.
- Layering: model loader, translator, planner, executor, MCP adapter.
  Lower layers never import higher ones. The planner never talks to the
  database. The MCP server holds no logic of its own.
- Access control is ClickHouse's. Nothing here authenticates or grants.
- Keep the public surface small. One obvious way to do each thing.

## Layout

| Path | What |
| --- | --- |
| `src/dactopus_ossie_clickhouse/model.py` | Load and validate a model on top of `apache-ossie`. |
| `src/dactopus_ossie_clickhouse/_vendor/ossie/` | A copy of [`apache-ossie`](https://github.com/apache/ossie/tree/main/python) until it is on PyPI; never edited by hand. |
| `src/dactopus_ossie_clickhouse/translate.py` | Ossie expression to ClickHouse SQL, on the [SQLGlot](https://github.com/tobymao/sqlglot) AST. |
| `src/dactopus_ossie_clickhouse/planner.py` | Question to one `SELECT`. |
| `src/dactopus_ossie_clickhouse/executor.py` | Introspection, execution, result cleanup. |
| `src/dactopus_ossie_clickhouse/access.py` | Trim the model to the caller's rights; policy file. |
| `src/dactopus_ossie_clickhouse/mcp_server.py` | [MCP](https://modelcontextprotocol.io) tools, behind the `[mcp]` extra. |
| `src/dactopus_ossie_clickhouse/cli.py` | `dactopus-ossie-clickhouse` command. |
| `tests/` | pytest; fixtures in `tests/fixtures/`. |
| `research/` | Studies behind decisions, with data shared with Apache Ossie; not part of the package. |
| `scripts/vendor_ossie.py` | Replace the `apache-ossie` copy with another commit's, or check it (`--check`, run by CI). |

## Pull requests

Small and focused. Say which spec section a semantic decision rests on.
CI runs lint, format check, `scripts/vendor_ossie.py --check` and the
test suite against a ClickHouse service, and builds the package. By
submitting a contribution you agree it is licensed under the Apache
License 2.0, like the rest of the project.

## Releasing

For maintainers. The translator is built on
[SQLGlot](https://github.com/tobymao/sqlglot), and some rewrites in
`translate.py` work around gaps that SQLGlot later fixes. A patch release
never changes the `sqlglot` version range in `pyproject.toml`; while the
major version is 0, it may carry features as well as fixes. A minor
release raises the lower bound to the current
SQLGlot release and drops the rewrites that release made redundant
([design note](docs/design.md#sqlglot-bounds-move-only-in-minor-releases)).

1. On the branch to release, move the `[Unreleased]` entries in
   `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD`, leaving `[Unreleased]`
   empty.
2. Set `version` in `pyproject.toml` and run `uv lock`, so the package and
   the version the MCP server reports match the tag.
3. Push, wait for the four required CI jobs, merge with "Rebase and
   merge", the only method the repository allows.
4. Rehearse on `main`: run the Publish workflow by hand. It uploads the
   build to [TestPyPI](https://test.pypi.org/project/dactopus-ossie-clickhouse/)
   as `X.Y.Z.devN`; install that from there, its dependencies from PyPI
   (anyone may upload to TestPyPI, under any name), and look at the page. PyPI takes
   each version once, so whatever goes wrong is fixed here, before the tag.
5. Tag the commit on `main`, never one on the branch: rebasing changes the
   hashes. Publishing the release on GitHub runs the Publish workflow,
   which checks that the tag is the version in `pyproject.toml` and its
   commit is on `main`, then uploads to PyPI. The release notes are the
   changelog section plus the install line pinned to the version.

```bash
git switch main && git pull --ff-only
gh workflow run publish.yml --ref main   # step 4, TestPyPI
pip install --no-deps --index-url https://test.pypi.org/simple/ dactopus-ossie-clickhouse==X.Y.Z.devN
pip install "dactopus-ossie-clickhouse[mcp]==X.Y.Z.devN"   # dependencies from PyPI
git tag -a vX.Y.Z -m "dactopus-ossie-clickhouse X.Y.Z" && git push origin vX.Y.Z
gh release create vX.Y.Z --title X.Y.Z --notes-file notes.md   # publishes to PyPI
```

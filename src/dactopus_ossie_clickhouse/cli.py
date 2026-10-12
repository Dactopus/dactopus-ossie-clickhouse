"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from decimal import Decimal

import yaml
from clickhouse_connect.driver.exceptions import ClickHouseError

from dactopus_ossie_clickhouse.access import Policy
from dactopus_ossie_clickhouse.executor import Executor, connect
from dactopus_ossie_clickhouse.model import ModelError, join_problems, load_model
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query, unanswerable_metrics
from dactopus_ossie_clickhouse.translate import untranslatable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dactopus-ossie-clickhouse")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check an Ossie model file")
    v.add_argument("model", help="path to a YAML or JSON Ossie model")
    v.add_argument("--url", help="also check the model against this ClickHouse")
    for name, help_ in (("sql", "build the ClickHouse SQL for a semantic query"),
                        ("query", "run a semantic query and print the rows")):  # fmt: skip
        s = sub.add_parser(name, help=help_)
        s.add_argument("model", help="path to a YAML or JSON Ossie model")
        s.add_argument("-m", "--metric", action="append", default=[], help="metric name")
        s.add_argument("-d", "--dimension", action="append", default=[], help="dataset.field")
        s.add_argument(
            "-f",
            "--filter",
            action="append",
            default=[],
            help="condition over dataset.field or a metric name",
        )
        s.add_argument("-o", "--order", action="append", default=[], help="name or 'name desc'")
        s.add_argument("-l", "--limit", type=int)
        s.add_argument("--url", help="ClickHouse HTTP URL (default $OSSIE_CLICKHOUSE_URL)")
        s.add_argument("--policy", help="YAML file hiding objects per ClickHouse user or role")
    sub.choices["query"].add_argument(
        "--json", action="store_true", help="JSON rows instead of TSV"
    )
    sv = sub.add_parser("serve", help="run the MCP server over stdio (needs the [mcp] extra)")
    sv.add_argument("model", help="path to a YAML or JSON Ossie model")
    sv.add_argument("--url", help="ClickHouse HTTP URL (default $OSSIE_CLICKHOUSE_URL)")
    sv.add_argument("--policy", help="YAML file hiding objects per ClickHouse user or role")
    return parser


def main(argv: list[str] | None = None) -> int:
    # clickhouse-connect logs a connection failure before raising it; the error line
    # below is enough. A NullHandler keeps the unconfigured-logging fallback quiet.
    logging.getLogger("clickhouse_connect").addHandler(logging.NullHandler())
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except ModelError as e:
        for p in e.problems:
            print(f"error: {p}", file=sys.stderr)
        print(f"{args.model}: {len(e.problems)} problem(s)", file=sys.stderr)
        return 1
    # PlanError for the question; ClickHouseError for a server that is down, refuses the
    # credentials or the query; the rest for a policy file that is missing or malformed,
    # and for `serve` without the [mcp] extra. None of them deserves a traceback.
    except (PlanError, ClickHouseError, OSError, ValueError, yaml.YAMLError, ImportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy) if getattr(args, "policy", None) else None

    if args.command == "serve":
        from dactopus_ossie_clickhouse.mcp_server import build_server

        model = load_model(args.model)
        server = build_server(model, lambda: connect(args.url), policy)
        # stderr: stdout carries the protocol.
        print(
            f"dactopus-ossie-clickhouse: model {model.name!r} on {server.location}", file=sys.stderr
        )
        server.run()  # stdio
        return 0

    if args.command in ("sql", "query"):
        model = load_model(args.model)
        q = Query(
            tuple(args.metric),
            tuple(args.dimension),
            tuple(args.filter),
            tuple(args.order),
            args.limit,
        )
        if args.command == "sql" and not args.url:
            print(Planner(model).sql(q, pretty=True))
            return 0
        ex = Executor(connect(args.url), model, policy)
        if args.command == "sql":
            print(ex.planner.sql(q, pretty=True))
            return 0
        r = ex.execute(q)
        if args.json:
            rows = [dict(zip(r.columns, row, strict=True)) for row in r.shown_rows()]
            print(
                json.dumps(rows, default=lambda v: float(v) if isinstance(v, Decimal) else str(v))
            )
        else:
            print("\t".join(r.columns))
            for row in r.shown_rows():
                print("\t".join("" if v is None else str(v) for v in row))
        return 0

    # validate
    doc = load_model(args.model)
    ex = Executor(connect(args.url), doc) if args.url else None
    fields, metrics = (
        (ex.untranslatable_fields, ex.untranslatable_metrics) if ex else untranslatable(doc)
    )
    problems = [f"field {k}: {v}" for k, v in fields.items()]
    problems += [f"metric {k!r}: {v}" for k, v in metrics.items()]
    problems += join_problems(doc)
    problems += [f"metric {k!r}: {v}" for k, v in unanswerable_metrics(doc).items()]
    where = ""
    if ex:
        problems += ex.check()
        where = f" against {ex.location}"  # never args.url: it may carry the password
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    if problems:
        print(f"{args.model}: {len(problems)} problem(s){where}", file=sys.stderr)
        return 1
    n_fields = sum(len(d.fields or []) for d in doc.datasets)
    print(
        f"{args.model}: ok ({doc.name}, version {doc.version}, "
        f"{len(doc.datasets)} datasets, {n_fields} fields, "
        f"{len(doc.relationships or [])} relationships, {len(doc.metrics or [])} metrics){where}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Trim a model to what a caller may see.

ClickHouse decides who may read which tables, columns and rows. This module
only makes the model agree with that: datasets, fields, relationships and
metrics that rest on something the caller cannot read are removed before the
planner ever sees them, so they cannot show up in SQL, errors or suggestions.
An optional policy file adds restrictions the database cannot express.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlglot import exp

from dactopus_ossie_clickhouse._vendor.ossie import OssieDocument
from dactopus_ossie_clickhouse.model import SOURCE_QUALIFIER, reads_source
from dactopus_ossie_clickhouse.planner import PlanError
from dactopus_ossie_clickhouse.translate import parse, pick_expression


@dataclass(frozen=True)
class Hidden:
    datasets: frozenset[str] = frozenset()
    fields: frozenset[str] = frozenset()  # "dataset.field"
    metrics: frozenset[str] = frozenset()

    def __or__(self, other: Hidden) -> Hidden:
        return Hidden(
            self.datasets | other.datasets,
            self.fields | other.fields,
            self.metrics | other.metrics,
        )


@dataclass
class Policy:
    """Per ClickHouse user or role: objects to hide beyond what grants already hide.

    File format (YAML)::

        analyst:                     # ClickHouse user or role name
          hidden_datasets: [customer]
          hidden_fields: [customer.c_email_address]
          hidden_metrics: [customer_lifetime_value, store_sales.total_quantity]
    """

    rules: dict[str, Hidden] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        data = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(data, dict) or not all(isinstance(r, dict | None) for r in data.values()):
            raise ValueError(f"policy {path}: expected a mapping of user or role name to rules")
        return cls(
            {
                name.upper(): Hidden(
                    frozenset(r.get("hidden_datasets", [])),
                    frozenset(r.get("hidden_fields", [])),
                    frozenset(r.get("hidden_metrics", [])),
                )
                for name, r in ((n, r or {}) for n, r in data.items())
            }
        )

    def hidden_for(self, user: str, roles: list[str]) -> Hidden:
        h = Hidden()
        for name in [user, *roles]:
            h |= self.rules.get(name.upper(), Hidden())
        return h


def restrict(model: OssieDocument, hidden: Hidden, reader: str = "here") -> OssieDocument:
    """The model without the hidden objects and everything that depends on them."""
    ds_hidden = {d.upper() for d in hidden.datasets}
    f_hidden = {f.upper() for f in hidden.fields}
    m_hidden = {m.upper() for m in hidden.metrics}

    declared = {d.name: d for d in model.datasets}

    # Per dataset, the fields of the full model that read a physical column, by the
    # column's exact name (ClickHouse column names are case sensitive).
    readers: dict[str, dict[str, set[str]]] = {}
    for ds in model.datasets:
        by_column: dict[str, set[str]] = {}
        for f in ds.fields or []:
            try:
                tree = parse(pick_expression(f.expression))
            except ValueError:
                continue  # an untranslatable field reads nothing we can name
            for c in tree.find_all(exp.Column):
                if not c.table:
                    by_column.setdefault(c.name, set()).add(f"{ds.name}.{f.name}".upper())
        readers[ds.name] = by_column

    def refs(expression, scope: str | None = None) -> set[str]:
        """The fields an expression reads; a bare name is a field of ``scope``, the
        dataset whose metric it is. Its ``source.column`` reads every field of that
        dataset whose expression reads the column, so hiding any of them hides the
        metric; a column no field reads adds nothing (a column the caller cannot
        read is hidden by Executor, as a metric). In a dataset that declares a field
        named source the planner refuses the metric, and the qualifier is that field."""
        tree = parse(pick_expression(expression))
        from_source = bool(scope) and reads_source(declared[scope])
        found: set[str] = set()
        for c in tree.find_all(exp.Column):
            if from_source and c.table.upper() == SOURCE_QUALIFIER:
                found |= readers[scope].get(c.name, set())
            elif c.table or scope:
                found.add(f"{c.table or scope}.{c.name}".upper())
        return found

    def kept(name: str, expression, scope: str | None = None) -> bool:
        # By name first: a hidden metric may not parse (untranslatable).
        return name.upper() not in m_hidden and refs(expression, scope) <= kept_fields

    data = model.model_dump(by_alias=True, exclude_none=True)
    data["datasets"] = [d for d in data["datasets"] if d["name"].upper() not in ds_hidden]
    if not data["datasets"]:
        raise PlanError(
            f"none of the model's datasets is readable {reader}; check the ClickHouse URL, "
            "database and the connected user's grants"
        )
    for d in data["datasets"]:
        d["fields"] = [
            f for f in d.get("fields", []) if f"{d['name']}.{f['name']}".upper() not in f_hidden
        ]
        # Keys name fields too; a relationship that needs a dropped key goes below.
        names = {f["name"].upper() for f in d["fields"]}
        keys = [k for k in d.pop("unique_keys", []) if {c.upper() for c in k} <= names]
        if keys:
            d["unique_keys"] = keys
        if not {c.upper() for c in d.get("primary_key", [])} <= names:
            del d["primary_key"]
    kept_fields = {
        f"{d['name']}.{f['name']}".upper() for d in data["datasets"] for f in d.get("fields", [])
    }
    # A relationship joins on fields (spec, "Name Resolution"): it goes with any of them.
    data["relationships"] = [
        r
        for r in data.get("relationships", [])
        if {f"{r['from']}.{c}".upper() for c in r["from_columns"]}
        | {f"{r['to']}.{c}".upper() for c in r["to_columns"]}
        <= kept_fields
    ]
    data["metrics"] = [
        m.model_dump(by_alias=True, exclude_none=True)
        for m in model.metrics or []
        if kept(m.name, m.expression)
    ]
    for d in data["datasets"]:
        metrics = [
            m.model_dump(by_alias=True, exclude_none=True)
            for m in declared[d["name"]].metrics or []
            if kept(f"{d['name']}.{m.name}", m.expression, d["name"])
        ]
        d.pop("metrics", None)
        if metrics:
            d["metrics"] = metrics
    return OssieDocument.model_validate(data)

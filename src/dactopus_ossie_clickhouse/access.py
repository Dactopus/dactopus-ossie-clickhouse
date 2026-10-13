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
from ossie import OssieDocument
from sqlglot import exp

from dactopus_ossie_clickhouse.planner import Code, PlanError
from dactopus_ossie_clickhouse.translate import parse, pick_expression


@dataclass(frozen=True)
class Hidden:
    datasets: frozenset[str] = frozenset()
    fields: frozenset[str] = frozenset()  # "dataset.field"
    metrics: frozenset[str] = frozenset()
    relationships: frozenset[str] = frozenset()  # by name; grants only, not the policy file

    def __or__(self, other: Hidden) -> Hidden:
        return Hidden(
            self.datasets | other.datasets,
            self.fields | other.fields,
            self.metrics | other.metrics,
            self.relationships | other.relationships,
        )


@dataclass
class Policy:
    """Per ClickHouse user or role: objects to hide beyond what grants already hide.

    File format (YAML)::

        analyst:                     # ClickHouse user or role name
          hidden_datasets: [customer]
          hidden_fields: [customer.c_email_address]
          hidden_metrics: [customer_lifetime_value]
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
    r_hidden = {r.upper() for r in hidden.relationships}

    def refs(expression) -> set[str]:
        tree = parse(pick_expression(expression))
        return {f"{c.table}.{c.name}".upper() for c in tree.find_all(exp.Column) if c.table}

    data = model.model_dump(by_alias=True, exclude_none=True)
    data["datasets"] = [d for d in data["datasets"] if d["name"].upper() not in ds_hidden]
    if not data["datasets"]:
        raise PlanError(
            f"none of the model's datasets is readable {reader}; check the ClickHouse URL, "
            "database and the connected user's grants",
            Code.SOURCE_UNAVAILABLE,
        )
    for d in data["datasets"]:
        d["fields"] = [
            f for f in d.get("fields", []) if f"{d['name']}.{f['name']}".upper() not in f_hidden
        ]
    kept_fields = {
        f"{d['name']}.{f['name']}".upper() for d in data["datasets"] for f in d.get("fields", [])
    }
    data["relationships"] = [
        r
        for r in data.get("relationships", [])
        if r["from"].upper() not in ds_hidden
        and r["to"].upper() not in ds_hidden
        and r["name"].upper() not in r_hidden
    ]
    data["metrics"] = [
        m
        for m in model.metrics or []
        if m.name.upper() not in m_hidden and refs(m.expression) <= kept_fields
    ]
    data["metrics"] = [m.model_dump(by_alias=True, exclude_none=True) for m in data["metrics"]]
    return OssieDocument.model_validate(data)

"""Load an Ossie model from one or more files and check what the upstream package does not.

The ``apache-ossie`` package validates the document against the schema: its
loaders refuse an unknown property, a property set to null, a missing or
unsupported ``version``. Everything the later layers rely on beyond that
(names that resolve, expressions in a dialect we can translate) is checked
here, and all problems are reported at once rather than one per run.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from ossie import (
    OssieAIContextObject,
    OssieDataset,
    OssieDialect,
    OssieDocument,
    OssieRelationship,
)
from pydantic import ValidationError

SUPPORTED_VERSION = "0.2.0.dev0"
SUPPORTED_DIALECTS = frozenset({OssieDialect.OSSIE_SQL_2026, OssieDialect.ANSI_SQL})
# The custom_extensions namespace this project owns (docs/model-authoring.md):
# a JSON object on a dataset, with these keys and values.
VENDOR = "CLICKHOUSE"
_EXTENSION_KEYS = {"dedup": ("final", "none")}


class ModelError(ValueError):
    """A model that cannot be used. ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def load_model(*paths: str | Path) -> OssieDocument:
    """Read one or more YAML or JSON Ossie files as one model and validate it.

    Ossie has no way to split a model across files, so several are combined
    here as a union: each file is checked against the schema on its own, and
    names and references on the whole, so a relationship may join datasets of
    two files. A name defined in two files is an error. The root's name and
    description come from the first file; every file's root ``ai_context`` is
    kept: instructions joined in file order, synonyms and examples united.
    Raises ``ModelError``.
    """
    if not paths:
        raise TypeError("load_model() needs at least one path")
    paths = tuple(dict.fromkeys(map(Path, paths)))  # a path given twice is read once
    docs: list[OssieDocument] = []
    problems: list[str] = []
    for path in paths:
        try:
            docs.append(_read(path, f"{path}: " if len(paths) > 1 else ""))
        except ModelError as e:
            problems += e.problems
    problems = problems or _clashes(paths, docs)
    if problems:
        raise ModelError(problems)
    doc = _combine(docs)
    problems = check_model(doc)
    if problems:
        raise ModelError(problems)
    return doc


def _read(path: Path, label: str) -> OssieDocument:
    """One file checked against the schema; ``label`` starts each problem but a read error."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise ModelError([f"cannot read {path}: {e}"]) from e
    # The SDK's document loaders, not model_validate: they also refuse what the
    # schema refuses but the field types cannot express (a null, a missing version).
    parse = (
        OssieDocument.from_ossie_json if path.suffix == ".json" else OssieDocument.from_ossie_yaml
    )
    try:
        return parse(text)
    except ValidationError as e:
        raise ModelError([label + _format(err) for err in e.errors()]) from e
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise ModelError([f"cannot read {path}: {e}"]) from e
    except ValueError as e:  # a document rule, with the paths that break it
        raise ModelError([label + str(e)]) from e


def _clashes(paths: tuple[Path, ...], docs: list[OssieDocument]) -> list[str]:
    """Names defined in two files. Merging same-named objects is not supported."""
    problems: list[str] = []
    for kind, attr in (
        ("dataset", "datasets"),
        ("metric", "metrics"),
        ("relationship", "relationships"),
    ):
        first: dict[str, tuple[str, Path]] = {}
        for path, doc in zip(paths, docs, strict=True):
            # Names resolve case-insensitively; a repeat within one file is check_model's.
            names: dict[str, str] = {}
            for o in getattr(doc, attr) or []:
                names.setdefault(o.name.upper(), o.name)
            for key, name in names.items():
                if key not in first:
                    first[key] = (name, path)
                    continue
                spelled, where = first[key]
                also = f" (as {spelled!r})" if spelled != name else ""
                problems.append(f"{kind} {name!r} is defined in both {where}{also} and {path}")
    return problems


def _combine(docs: list[OssieDocument]) -> OssieDocument:
    if len(docs) == 1:
        return docs[0]

    def union(attr: str) -> list | None:
        return [o for d in docs for o in getattr(d, attr) or []] or None

    return docs[0].model_copy(
        update={
            "datasets": union("datasets"),
            "relationships": union("relationships"),
            "metrics": union("metrics"),
            "custom_extensions": union("custom_extensions"),
            "ai_context": _ai_context(docs),
        }
    )


def _ai_context(docs: list[OssieDocument]) -> str | OssieAIContextObject | None:
    """Every file's root ``ai_context`` as one: instructions joined in file order,
    synonyms and examples united. A string when no file has the object form."""
    objects = [d.ai_context for d in docs if isinstance(d.ai_context, OssieAIContextObject)]
    texts = [t for d in docs if (t := _instructions(d.ai_context))]
    joined = "\n\n".join(texts) or None
    if not objects:
        return joined

    def united(attr: str) -> tuple[str, ...] | None:
        return tuple(dict.fromkeys(v for o in objects for v in getattr(o, attr) or ())) or None

    return OssieAIContextObject(
        instructions=joined, synonyms=united("synonyms"), examples=united("examples")
    )


def _instructions(ai) -> str | None:
    return ai.instructions if isinstance(ai, OssieAIContextObject) else ai


def check_model(doc: OssieDocument) -> list[str]:
    """Semantic checks beyond the schema. Returns human-readable problems."""
    problems: list[str] = []
    # The SDK admits only its schema's version, so this holds unless it widens
    # the field again; test_model.py checks the two constants agree.
    if doc.version != SUPPORTED_VERSION:
        problems.append(
            f"unsupported version {doc.version!r}; this build supports {SUPPORTED_VERSION!r}"
        )

    datasets = {d.name: d for d in doc.datasets}
    problems += _duplicates("dataset", [d.name for d in doc.datasets])
    problems += _duplicates("metric", [m.name for m in doc.metrics or []])
    problems += _duplicates("relationship", [r.name for r in doc.relationships or []])

    # Keys and relationship columns are physical column names per the spec, not
    # field names, so their existence can only be checked against the database.
    for d in doc.datasets:
        problems += _extension(d)[1]
        problems += _duplicates(f"field in dataset {d.name!r}", [f.name for f in d.fields or []])
        for f in d.fields or []:
            problems += _dialect_check(f"field {d.name}.{f.name}", f.expression.dialects)

    for m in doc.metrics or []:
        problems += _dialect_check(f"metric {m.name!r}", m.expression.dialects)

    for r in doc.relationships or []:
        if len(r.from_columns) != len(r.to_columns):
            problems.append(
                f"relationship {r.name!r}: from_columns and to_columns differ in length"
            )
        for side, ds_name in (("from", r.from_dataset), ("to", r.to)):
            if ds_name not in datasets:
                problems.append(
                    f"relationship {r.name!r}: {side} dataset {ds_name!r} does not exist"
                )

    # Nothing reads an entry anywhere else; its author expects an effect that never comes.
    elsewhere = [
        ("model", doc),
        *((f"field {d.name}.{f.name}", f) for d in doc.datasets for f in d.fields or []),
        *((f"metric {m.name!r}", m) for m in doc.metrics or []),
        *((f"relationship {r.name!r}", r) for r in doc.relationships or []),
    ]
    for what, node in elsewhere:
        if _entries(node):
            problems.append(
                f"{what}: a {VENDOR} entry in custom_extensions is read on datasets only"
            )
    return problems


def overrides(ds: OssieDataset) -> dict:
    """The dataset's CLICKHOUSE entry, parsed; {} without one. Keys: dedup ("final" |
    "none"); without it, FINAL where the source keeps row versions. Raises ``ModelError``
    for an entry check_model() refuses: an ``OssieDocument`` built in Python skips it."""
    data, problems = _extension(ds)
    if problems:
        raise ModelError(problems)
    return data


def declared_keys(ds: OssieDataset) -> list[list[str]]:
    """The primary key and unique keys of ``ds``, empty ones dropped."""
    return [k for k in [ds.primary_key, *(ds.unique_keys or [])] if k]


def covers_key(columns: list[str], ds: OssieDataset) -> bool:
    """Whether ``columns`` include a primary or unique key of ``ds``: then no two
    rows share their values (any superset of a unique key is unique too)."""
    cols = {c.upper() for c in columns}
    return any(cols >= {c.upper() for c in k} for k in declared_keys(ds))


def join_problem(r: OssieRelationship, target: OssieDataset) -> str | None:
    """Why ``r`` cannot be joined as many-to-one, or None when it can."""
    keys = declared_keys(target)
    if not keys:
        return (
            f"cannot prove relationship {r.name!r} is many-to-one: {target.name!r} declares "
            "no primary_key or unique_keys; declare the key its to_columns cover"
        )
    if not covers_key(r.to_columns, target):
        return (
            f"relationship {r.name!r} is not many-to-one: to_columns "
            f"{r.to_columns} do not cover a primary or unique key of {target.name!r}"
        )
    return None


def join_problems(doc: OssieDocument) -> list[str]:
    """join_problem() for every relationship; the planner refuses these at query time."""
    datasets = {d.name: d for d in doc.datasets}
    found = (join_problem(r, datasets[r.to]) for r in doc.relationships or [] if r.to in datasets)
    return [p for p in found if p]


def _duplicates(kind: str, names: list[str]) -> list[str]:
    """Names that repeat, compared case-insensitively: that is how they resolve."""
    seen: set[str] = set()
    return [
        f"duplicate {kind} name {n!r}" for n in names if n.upper() in seen or seen.add(n.upper())
    ]


def _entries(node) -> list:
    return [e for e in node.custom_extensions or [] if e.vendor_name.upper() == VENDOR]


def _extension(ds: OssieDataset) -> tuple[dict, list[str]]:
    """The dataset's CLICKHOUSE entry parsed, and the problems with it."""
    where = f"dataset {ds.name!r}: {VENDOR} entry"
    entries = _entries(ds)
    if not entries:
        return {}, []
    if len(entries) > 1:
        return {}, [f"{where} appears {len(entries)} times in custom_extensions; keep one"]
    try:
        data = json.loads(entries[0].data, object_pairs_hook=_pairs)
    except json.JSONDecodeError as e:
        return {}, [f"{where}: data is not JSON ({e.msg})"]
    except ValueError as e:  # a repeated key, from _pairs
        return {}, [f"{where}: {e}"]
    if not isinstance(data, dict):
        return {}, [f"{where}: data must be a JSON object, got {json.dumps(data)}"]
    known = ", ".join(_EXTENSION_KEYS)
    problems = [
        f"{where}: unknown key {k!r}; known: {known}" for k in data if k not in _EXTENSION_KEYS
    ]
    for key, values in _EXTENSION_KEYS.items():
        if key in data and data[key] not in values:
            choices = " or ".join(json.dumps(v) for v in values)
            problems.append(f"{where}: {key!r} must be {choices}, got {json.dumps(data[key])}")
    return ({}, problems) if problems else (data, [])


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    """dict(pairs), refusing a repeated key: json.loads would keep the last one silently."""
    seen: set[str] = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError(f"key {k!r} is repeated; keep one")
        seen.add(k)
    return dict(pairs)


def _dialect_check(what: str, dialects) -> list[str]:
    if any(d.dialect in SUPPORTED_DIALECTS for d in dialects):
        return []
    have = ", ".join(sorted(d.dialect.value for d in dialects))
    return [f"{what}: no expression in a supported dialect (has {have}; need ANSI_SQL)"]


def _format(err: dict) -> str:
    if err["loc"] == ("version",) and err["type"] == "literal_error":
        # pydantic says only what it expected; say what this build supports.
        return f"unsupported version {err['input']!r}; this build supports {SUPPORTED_VERSION!r}"
    if err["loc"] == () and err["type"] == "model_type":
        # An empty file or a list; pydantic's message names the SDK class.
        return "document: top level must be a mapping"
    loc = ".".join(str(p) for p in err["loc"]) or "document"
    return f"{loc}: {err['msg']}"

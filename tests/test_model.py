import copy
from pathlib import Path

import pytest
import yaml
from ossie import OssieDocument

from dactopus_ossie_clickhouse import SUPPORTED_VERSION, ModelError, load_model
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.model import overrides

FIXTURE = Path(__file__).parent / "fixtures" / "tpcds.yaml"


@pytest.fixture
def tpcds() -> dict:
    return yaml.safe_load(FIXTURE.read_text())


def write(tmp_path: Path, data: dict, name: str = "m.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


def test_reference_model_loads():
    doc = load_model(FIXTURE)
    assert doc.name == "tpcds_retail_model"
    assert len(doc.datasets) == 5 and len(doc.metrics) == 8


def test_json_loads(tmp_path, tpcds):
    import json

    p = tmp_path / "m.json"
    p.write_text(json.dumps(tpcds))
    assert load_model(p).name == "tpcds_retail_model"


def problems_of(tmp_path, data) -> list[str]:
    with pytest.raises(ModelError) as e:
        load_model(write(tmp_path, data))
    return e.value.problems


def test_wrong_version(tmp_path, tpcds):
    tpcds["version"] = "9.9.9"
    assert problems_of(tmp_path, tpcds) == [
        "unsupported version '9.9.9'; this build supports '0.2.0.dev0'"
    ]


def test_supported_version_is_the_sdk_version():
    # The SDK admits only its schema's version; this build must name the same one.
    assert OssieDocument.model_fields["version"].default == SUPPORTED_VERSION


def test_missing_version(tmp_path, tpcds):
    del tpcds["version"]
    assert problems_of(tmp_path, tpcds) == ["document is missing the required property: version"]


def test_unknown_key_in_nested_object(tmp_path, tpcds):
    # The SDK once dropped these silently (apache/ossie#530); a typo changed nothing.
    tpcds["datasets"][0]["fields"][0]["descriptionn"] = "typo"
    tpcds["datasets"][1]["custom_extensions"] = [{"vendor_nam": "CLICKHOUSE", "data": "{}"}]
    ps = problems_of(tmp_path, tpcds)
    assert "datasets.0.fields.0.descriptionn: Extra inputs are not permitted" in ps
    assert "datasets.1.custom_extensions.0.vendor_nam: Extra inputs are not permitted" in ps
    assert "datasets.1.custom_extensions.0.vendor_name: Field required" in ps


def test_null_property(tmp_path, tpcds):
    tpcds["datasets"][0]["primary_key"] = None
    tpcds["metrics"][0]["description"] = None
    ps = problems_of(tmp_path, tpcds)
    assert ps == [
        "property set to null, which the schema does not allow; omit it instead: "
        "datasets[0].primary_key, metrics[0].description"
    ]


def test_empty_key(tmp_path, tpcds):
    # An empty key names no column; the SDK refuses it since apache/ossie#574.
    tpcds["datasets"][0]["primary_key"] = []
    tpcds["datasets"][1]["unique_keys"] = [[]]
    ps = problems_of(tmp_path, tpcds)
    assert any(p.startswith("datasets.0.primary_key: List should have at least 1") for p in ps)
    assert any(p.startswith("datasets.1.unique_keys.0: List should have at least 1") for p in ps)


def test_missing_required_field(tmp_path, tpcds):
    del tpcds["datasets"][0]["source"]
    assert any("datasets.0.source" in p for p in problems_of(tmp_path, tpcds))


def test_dangling_relationship(tmp_path, tpcds):
    tpcds["relationships"][0]["to"] = "nowhere"
    tpcds["relationships"][1]["to_columns"] = ["a", "b"]
    ps = problems_of(tmp_path, tpcds)
    assert any("'nowhere' does not exist" in p for p in ps)
    assert any("'store_sales_to_customer': from_columns and to_columns differ" in p for p in ps)


def test_unsupported_dialect_only(tmp_path, tpcds):
    tpcds["metrics"][0]["expression"]["dialects"][0]["dialect"] = "DAX"
    ps = problems_of(tmp_path, tpcds)
    assert any("metric 'total_sales': no expression in a supported dialect" in p for p in ps)


def test_duplicates(tmp_path, tpcds):
    tpcds["datasets"].append(copy.deepcopy(tpcds["datasets"][0]))
    tpcds["metrics"][0]["name"] = tpcds["metrics"][1]["name"]
    ps = problems_of(tmp_path, tpcds)
    assert any("duplicate dataset name 'store_sales'" in p for p in ps)
    assert any("duplicate metric name 'total_profit'" in p for p in ps)


def test_all_problems_reported_at_once(tmp_path, tpcds):
    tpcds["metrics"][0]["expression"]["dialects"][0]["dialect"] = "DAX"
    tpcds["relationships"][0]["to"] = "nowhere"
    assert len(problems_of(tmp_path, tpcds)) == 2


def _clickhouse(data: str, vendor: str = "CLICKHOUSE") -> dict:
    return {"vendor_name": vendor, "data": data}


@pytest.mark.parametrize("data", ['{"dedup": "none"}', '{"dedup": "final"}', "{}"])
def test_clickhouse_extension(tmp_path, tpcds, data):
    tpcds["datasets"][0]["custom_extensions"] = [_clickhouse(data)]
    doc = load_model(write(tmp_path, tpcds))
    assert overrides(doc.datasets[0]) == yaml.safe_load(data)


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        # each of these once kept or dropped FINAL silently, or failed with a bare traceback
        ('{"dedupe": "none"}', "unknown key 'dedupe'; known: dedup"),
        ('{"dedup": "None"}', '\'dedup\' must be "final" or "none", got "None"'),
        ('{"dedup": "Final"}', '\'dedup\' must be "final" or "none", got "Final"'),
        ('{"dedup": null}', '\'dedup\' must be "final" or "none", got null'),
        ("{dedup: none}", "data is not JSON (Expecting property name enclosed in double quotes)"),
        ('"none"', 'data must be a JSON object, got "none"'),
        ("[]", "data must be a JSON object, got []"),
        ("null", "data must be a JSON object, got null"),
    ],
)
def test_clickhouse_extension_refused(tmp_path, tpcds, data, problem):
    ds = tpcds["datasets"][0]
    ds["custom_extensions"] = [_clickhouse(data, vendor="ClickHouse")]  # any case
    assert problems_of(tmp_path, tpcds) == [f"dataset {ds['name']!r}: CLICKHOUSE entry: {problem}"]


def test_clickhouse_extension_twice(tmp_path, tpcds):
    ds = tpcds["datasets"][0]
    ds["custom_extensions"] = [_clickhouse('{"dedup": "none"}'), _clickhouse('{"dedup": "final"}')]
    assert problems_of(tmp_path, tpcds) == [
        f"dataset {ds['name']!r}: CLICKHOUSE entry appears 2 times in custom_extensions; keep one"
    ]


def test_clickhouse_extension_read_on_datasets_only(tmp_path, tpcds):
    entry = _clickhouse('{"dedup": "none"}')
    ds, metric, rel = tpcds["datasets"][0], tpcds["metrics"][0], tpcds["relationships"][0]
    tpcds["custom_extensions"].append(entry)  # beside other vendors' entries, which stay
    ds["fields"][0]["custom_extensions"] = [entry]
    metric["custom_extensions"] = [entry]
    rel["custom_extensions"] = [entry]
    tail = "a CLICKHOUSE entry in custom_extensions is read on datasets only"
    assert problems_of(tmp_path, tpcds) == [
        f"model: {tail}",
        f"field {ds['name']}.{ds['fields'][0]['name']}: {tail}",
        f"metric {metric['name']!r}: {tail}",
        f"relationship {rel['name']!r}: {tail}",
    ]


def test_overrides_checks_a_document_built_in_python(tpcds):
    # Executor accepts an OssieDocument that never went through load_model.
    tpcds["datasets"][0]["custom_extensions"] = [_clickhouse('{"dedup": "None"}')]
    doc = OssieDocument.model_validate(tpcds)
    with pytest.raises(ModelError, match="'dedup' must be"):
        overrides(doc.datasets[0])


def test_unreadable(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("datasets: [\n")
    with pytest.raises(ModelError) as e:
        load_model(p)
    assert "cannot read" in e.value.problems[0]


def test_top_level_must_be_a_mapping(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- datasets\n")
    with pytest.raises(ModelError) as e:
        load_model(p)
    assert e.value.problems == ["document: top level must be a mapping"]


def test_not_utf8(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_bytes(b"\xff\xfe name: x\n")
    with pytest.raises(ModelError) as e:
        load_model(p)
    assert "cannot read" in e.value.problems[0]


def test_non_finite_number(tmp_path, tpcds):
    tpcds["datasets"][0]["ai_context"] = {"weight": float("nan")}
    assert problems_of(tmp_path, tpcds) == [
        "property holds a number JSON cannot represent, so it cannot round-trip: "
        "datasets[0].ai_context.weight"
    ]


def test_cli(tmp_path, tpcds, capsys):
    # The reference model's store_productivity sums two datasets: no question answers it.
    assert main(["validate", str(FIXTURE)]) == 1
    assert "metric 'store_productivity': aggregates columns of store and store_sales" in (
        capsys.readouterr().err
    )
    tpcds["metrics"] = [m for m in tpcds["metrics"] if m["name"] != "store_productivity"]
    assert main(["validate", str(write(tmp_path, tpcds, "ok.yaml"))]) == 0
    assert "ok (tpcds_retail_model" in capsys.readouterr().out
    tpcds["version"] = "0.0.0"
    bad = write(tmp_path, tpcds)
    assert main(["validate", str(bad)]) == 1
    assert "1 problem(s)" in capsys.readouterr().err
    assert main(["serve", str(bad)]) == 1  # fails before touching ClickHouse
    assert "unsupported version" in capsys.readouterr().err


def test_cli_has_only_its_new_name():
    from importlib.metadata import entry_points

    scripts = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert scripts["dactopus-ossie-clickhouse"] == "dactopus_ossie_clickhouse.cli:main"
    assert "ossie-clickhouse" not in scripts


def test_validate_reports_relationships_the_planner_refuses(tmp_path, tpcds, capsys):
    # The model still loads; only questions that join these datasets fail.
    item = next(d for d in tpcds["datasets"] if d["name"] == "item")
    item.pop("primary_key")
    item.pop("unique_keys", None)
    store = next(d for d in tpcds["datasets"] if d["name"] == "store")
    store["primary_key"] = ["s_store_id"]
    store.pop("unique_keys")
    tpcds["metrics"] = [m for m in tpcds["metrics"] if m["name"] != "store_productivity"]
    p = write(tmp_path, tpcds)
    load_model(p)
    assert main(["validate", str(p)]) == 1
    err = capsys.readouterr().err
    assert "'store_sales_to_item' is many-to-one: 'item' declares no primary_key" in err
    assert "'store_sales_to_store' is not many-to-one: to_columns ['s_store_sk']" in err
    assert "2 problem(s)" in err


def test_cli_serve_without_mcp_extra(monkeypatch, capsys):
    import sys

    monkeypatch.setitem(sys.modules, "mcp.server", None)  # what a bare install looks like
    assert main(["serve", str(FIXTURE)]) == 1
    assert "pip install 'dactopus-ossie-clickhouse[mcp]'" in capsys.readouterr().err

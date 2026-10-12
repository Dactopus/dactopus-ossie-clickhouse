import copy
from pathlib import Path

import pytest
import yaml

from dactopus_ossie_clickhouse import ModelError, load_model
from dactopus_ossie_clickhouse.cli import main

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
    assert any("unsupported version '9.9.9'" in p for p in problems_of(tmp_path, tpcds))


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
    tpcds["version"] = "0.0.0"
    tpcds["relationships"][0]["to"] = "nowhere"
    assert len(problems_of(tmp_path, tpcds)) == 2


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
    assert "top level must be a mapping" in e.value.problems[0]


def test_cli(tmp_path, tpcds, capsys):
    assert main(["validate", str(FIXTURE)]) == 0
    assert "ok (tpcds_retail_model" in capsys.readouterr().out
    # A metric of a dataset that reaches nothing else, next to a fact's: refused
    # in every question, since they share no dimension.
    tpcds["datasets"].append({**tpcds["datasets"][1], "name": "lonely"})
    gap = "MAX(store_sales.ss_sales_price) - MAX(lonely.d_year)"
    expression = {"dialects": [{"dialect": "ANSI_SQL", "expression": gap}]}
    tpcds["metrics"].append({"name": "gap", "expression": expression})
    assert main(["validate", str(write(tmp_path, tpcds, "gap.yaml"))]) == 1
    assert "metric 'gap': metric 'gap' reads lonely and store_sales, which share no dimension" in (
        capsys.readouterr().err
    )
    tpcds["metrics"].pop()
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
    item["primary_key"], item["unique_keys"] = None, None
    store = next(d for d in tpcds["datasets"] if d["name"] == "store")
    store["primary_key"], store["unique_keys"] = ["s_store_id"], None
    p = write(tmp_path, tpcds)
    load_model(p)
    assert main(["validate", str(p)]) == 1
    err = capsys.readouterr().err
    assert "'store_sales_to_item' is many-to-one: 'item' declares no primary_key" in err
    assert "'store_sales_to_store' is not many-to-one: to_columns ['s_store_sk']" in err
    # brand_rank_in_store partitions by store.s_store_sk: every question joins store.
    assert "metric 'brand_rank_in_store': relationship 'store_sales_to_store'" in err
    assert "3 problem(s)" in err


def test_cli_serve_without_mcp_extra(monkeypatch, capsys):
    import sys

    monkeypatch.setitem(sys.modules, "mcp.server", None)  # what a bare install looks like
    assert main(["serve", str(FIXTURE)]) == 1
    assert "pip install 'dactopus-ossie-clickhouse[mcp]'" in capsys.readouterr().err

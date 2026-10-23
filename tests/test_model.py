import copy
import json
from pathlib import Path

import pytest
import yaml
from ossie import OssieDocument

from dactopus_ossie_clickhouse import SUPPORTED_VERSION, ModelError, load_model
from dactopus_ossie_clickhouse.cli import main
from dactopus_ossie_clickhouse.model import overrides, time_zone
from dactopus_ossie_clickhouse.planner import PlanError, Planner, Query

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


def test_keys_and_relationship_columns_name_fields(tmp_path, tpcds):
    # Spec, "Name Resolution": fields of their dataset, not columns of its source.
    sales = next(d for d in tpcds["datasets"] if d["name"] == "store_sales")
    sales["primary_key"] = ["ss_item_sk", "ss_ticket_numbr"]
    sales["unique_keys"] = [["ss_wholesale_cost"]]  # a column of the source, not a field
    tpcds["relationships"][0]["from_columns"] = ["SS_SOLD_DATE_SK"]  # case-insensitive
    tpcds["relationships"][0]["to_columns"] = ["d_date_key"]
    ps = problems_of(tmp_path, tpcds)
    assert any(
        p.startswith(
            "dataset 'store_sales': primary_key 'ss_ticket_numbr' is not a field of "
            "'store_sales' (did you mean: ss_ticket_number"
        )
        for p in ps
    )
    assert any("unique_keys 'ss_wholesale_cost' is not a field of 'store_sales'" in p for p in ps)
    rel = tpcds["relationships"][0]["name"]
    assert any(f"relationship {rel!r}: to_columns 'd_date_key' is not a field of" in p for p in ps)
    assert len(ps) == 3


def test_metrics_on_a_dataset_load(tpcds):
    doc = load_model(FIXTURE)
    sales = next(d for d in doc.datasets if d.name == "store_sales")
    assert [m.name for m in sales.metrics] == ["total_quantity"]


def test_a_datasets_fields_and_metrics_share_one_namespace(tmp_path, tpcds):
    sales = next(d for d in tpcds["datasets"] if d["name"] == "store_sales")
    sales["metrics"].append(copy.deepcopy(sales["metrics"][0]))
    sales["metrics"].append(copy.deepcopy(sales["metrics"][0]) | {"name": "SS_QUANTITY"})
    assert problems_of(tmp_path, tpcds) == [
        "duplicate field or metric in dataset 'store_sales' name 'total_quantity'",
        "duplicate field or metric in dataset 'store_sales' name 'SS_QUANTITY'",
    ]


def test_a_datasets_metric_needs_a_supported_dialect(tmp_path, tpcds):
    sales = next(d for d in tpcds["datasets"] if d["name"] == "store_sales")
    sales["metrics"][0]["expression"]["dialects"][0]["dialect"] = "DAX"
    assert problems_of(tmp_path, tpcds) == [
        "metric 'store_sales.total_quantity': no expression in a supported dialect "
        "(has DAX; need ANSI_SQL)"
    ]


def test_a_model_metric_may_not_take_a_datasets_metric_name(tmp_path, tpcds):
    tpcds["metrics"][0]["name"] = "Store_Sales.total_quantity"
    assert problems_of(tmp_path, tpcds) == [
        "metric 'Store_Sales.total_quantity' has the name of a dataset's metric"
    ]


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


def test_duplicate_in_another_case(tmp_path, tpcds):
    tpcds["metrics"].append(copy.deepcopy(tpcds["metrics"][0]) | {"name": "TOTAL_SALES"})
    assert "duplicate metric name 'TOTAL_SALES'" in problems_of(tmp_path, tpcds)


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
        ('{"dedup": "final", "dedup": "none"}', "key 'dedup' is repeated; keep one"),
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


def test_clickhouse_extension_read_on_the_model_and_datasets_only(tmp_path, tpcds):
    entry = _clickhouse('{"dedup": "none"}')
    ds, metric, rel = tpcds["datasets"][0], tpcds["metrics"][0], tpcds["relationships"][0]
    ds["fields"][0]["custom_extensions"] = [entry]
    metric["custom_extensions"] = [entry]
    rel["custom_extensions"] = [entry]
    ds["metrics"][0]["custom_extensions"] = [entry]
    tail = "a CLICKHOUSE entry in custom_extensions is read on the model and datasets only"
    assert problems_of(tmp_path, tpcds) == [
        f"field {ds['name']}.{ds['fields'][0]['name']}: {tail}",
        f"metric {metric['name']!r}: {tail}",
        f"metric '{ds['name']}.{ds['metrics'][0]['name']}': {tail}",
        f"relationship {rel['name']!r}: {tail}",
    ]


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Makassar"])
def test_model_time_zone(tmp_path, tpcds, zone):
    assert time_zone(load_model(write(tmp_path, tpcds))) == "UTC"  # without an entry
    # beside other vendors' entries, which stay
    tpcds["custom_extensions"].append(_clickhouse(json.dumps({"time_zone": zone})))
    assert time_zone(load_model(write(tmp_path, tpcds))) == zone


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ('{"time_zone": "Moscow"}', '"Moscow"'),
        # ClickHouse matches zone names case-sensitively
        ('{"time_zone": "utc"}', '"utc"'),
        ('{"time_zone": "+03:00"}', '"+03:00"'),
        ('{"time_zone": null}', "null"),
        ('{"time_zone": ["UTC"]}', '["UTC"]'),
    ],
)
def test_model_time_zone_refused(tmp_path, tpcds, data, problem):
    tpcds["custom_extensions"] = [_clickhouse(data)]
    assert problems_of(tmp_path, tpcds) == [
        "model: CLICKHOUSE entry: 'time_zone' must be a time zone name such as \"UTC\" "
        f'or "America/New_York", got {problem}'
    ]


def test_model_and_dataset_keys_stay_apart(tmp_path, tpcds):
    ds = tpcds["datasets"][0]
    tpcds["custom_extensions"] = [_clickhouse('{"dedup": "none"}')]
    ds["custom_extensions"] = [_clickhouse('{"time_zone": "UTC"}')]
    assert problems_of(tmp_path, tpcds) == [
        "model: CLICKHOUSE entry: unknown key 'dedup'; known: time_zone",
        f"dataset {ds['name']!r}: CLICKHOUSE entry: unknown key 'time_zone'; known: dedup",
    ]


def test_time_zone_checks_a_document_built_in_python(tpcds):
    tpcds["custom_extensions"] = [_clickhouse('{"time_zone": "Mars/Olympus"}')]
    with pytest.raises(ModelError, match="'time_zone' must be"):
        time_zone(OssieDocument.model_validate(tpcds))


def test_time_zone_given_replaces_the_model_s(tmp_path, tpcds):
    """A deployment's zone, from --time-zone, over the zone a shared model names (#54)."""
    others = list(tpcds["custom_extensions"])
    doc = load_model(write(tmp_path, tpcds), time_zone="America/New_York")
    assert time_zone(doc) == "America/New_York"
    tpcds["custom_extensions"].append(_clickhouse('{"time_zone": "Pacific/Kiritimati"}'))
    doc = load_model(write(tmp_path, tpcds), time_zone="America/New_York")
    assert time_zone(doc) == "America/New_York"  # one entry: time_zone() refuses two
    kept = [e.model_dump() for e in doc.custom_extensions if e.vendor_name != "CLICKHOUSE"]
    assert kept == others  # other vendors' entries stay
    assert time_zone(load_model(write(tmp_path, tpcds))) == "Pacific/Kiritimati"


def test_time_zone_given_replaces_it_in_a_model_of_several_files(tmp_path, tpcds):
    sales, rest = split(tpcds)
    rest["custom_extensions"] = [_clickhouse('{"time_zone": "Pacific/Kiritimati"}')]
    a, b = write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml")
    assert time_zone(load_model(a, b, time_zone="Europe/Berlin")) == "Europe/Berlin"


@pytest.mark.parametrize("zone", ["Moscow", "utc", "+03:00", ""])
def test_time_zone_given_refused(tmp_path, tpcds, zone):
    tpcds["version"] = "0.1"
    with pytest.raises(ModelError) as e:
        load_model(write(tmp_path, tpcds), time_zone=zone)
    # Reported with the model's own problems.
    assert e.value.problems == [
        'time zone must be a time zone name such as "UTC" or "America/New_York", '
        f"got {json.dumps(zone)}",
        "unsupported version '0.1'; this build supports '0.2.0.dev0'",
    ]


def test_cli_time_zone(tmp_path, tpcds, capsys, monkeypatch):
    monkeypatch.delenv("OSSIE_CLICKHOUSE_TIME_ZONE", raising=False)
    tpcds["custom_extensions"].append(_clickhouse('{"time_zone": "Pacific/Kiritimati"}'))
    p = str(write(tmp_path, tpcds))
    q = ["sql", p, "-m", "total_sales", "-f", "date_dim.d_date = CURRENT_DATE"]

    def today_in(*extra: str) -> str:
        assert main([*q, *extra]) == 0
        out = capsys.readouterr().out
        return out.split("toDate(now('")[1].split("'")[0]

    assert today_in() == "Pacific/Kiritimati"
    assert today_in("--time-zone", "America/New_York") == "America/New_York"
    monkeypatch.setenv("OSSIE_CLICKHOUSE_TIME_ZONE", "Europe/Berlin")
    assert today_in() == "Europe/Berlin"
    assert today_in("--time-zone", "America/New_York") == "America/New_York"
    monkeypatch.setenv("OSSIE_CLICKHOUSE_TIME_ZONE", "")
    assert today_in() == "Pacific/Kiritimati"  # empty is unset
    for cmd in (["validate", p], q):
        for zone in ("Moscow", ""):
            assert main([*cmd, "--time-zone", zone]) == 1
            assert f"got {json.dumps(zone)}" in capsys.readouterr().err


def test_overrides_checks_a_document_built_in_python(tpcds):
    # Executor accepts an OssieDocument that never went through load_model.
    tpcds["datasets"][0]["custom_extensions"] = [_clickhouse('{"dedup": "None"}')]
    doc = OssieDocument.model_validate(tpcds)
    with pytest.raises(ModelError, match="'dedup' must be"):
        overrides(doc.datasets[0])


def split(tpcds: dict) -> tuple[dict, dict]:
    """The reference model as two subject areas: sales and dates in one file; customers,
    items, stores and the relationships from sales to them in the other."""
    sales = copy.deepcopy(tpcds)
    sales["datasets"] = tpcds["datasets"][:2]
    sales["metrics"] = tpcds["metrics"][:4]
    sales["relationships"] = tpcds["relationships"][:1]
    rest = {
        "version": tpcds["version"],
        "name": "tpcds_reference",
        "ai_context": {"instructions": "Stores are physical shops."},
        "datasets": tpcds["datasets"][2:],
        "relationships": tpcds["relationships"][1:],
        "metrics": tpcds["metrics"][4:],
    }
    return sales, rest


def test_several_files_answer_as_one(tmp_path, tpcds):
    sales, rest = split(tpcds)
    doc = load_model(write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml"))
    single = load_model(FIXTURE)
    assert [d.name for d in doc.datasets] == [d.name for d in single.datasets]
    assert [m.name for m in doc.metrics] == [m.name for m in single.metrics]

    def answer(model, metric):
        q = Query(metrics=(metric,), dimensions=("store.s_state", "date_dim.d_year"))
        try:
            return Planner(model).sql(q)
        except PlanError as e:
            return str(e)

    for m in single.metrics:
        assert answer(doc, m.name) == answer(single, m.name)


def test_references_resolve_across_files(tmp_path, tpcds):
    # b.yaml's relationships start at store_sales, defined in a.yaml.
    sales, rest = split(tpcds)
    load_model(write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml"))
    ps = problems_of(tmp_path, rest)
    assert "relationship 'store_sales_to_customer': from dataset 'store_sales' does not exist" in ps


def test_root_from_the_first_file_and_instructions_from_all(tmp_path, tpcds):
    sales, rest = split(tpcds)
    doc = load_model(write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml"))
    assert doc.name == tpcds["name"] and doc.description == tpcds["description"]
    first = tpcds["ai_context"]["instructions"]
    assert doc.ai_context.instructions == first + "\n\nStores are physical shops."
    assert doc.ai_context.synonyms == load_model(FIXTURE).ai_context.synonyms

    # A file without instructions adds none; the first file's may be a plain string.
    rest["ai_context"] = "Stores are physical shops."
    del sales["ai_context"]
    doc = load_model(write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml"))
    assert doc.ai_context == "Stores are physical shops."


def test_root_synonyms_and_examples_from_all_files(tmp_path, tpcds):
    # list_model and search_model serve the root's synonyms, so no file's may be lost.
    sales, rest = split(tpcds)
    sales["ai_context"] = {"synonyms": ["retail", "sales"], "examples": ["sales by year"]}
    rest["ai_context"] = {"instructions": "Stores are physical shops.", "synonyms": ["shops"]}
    doc = load_model(write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml"))
    assert doc.ai_context.instructions == "Stores are physical shops."
    assert doc.ai_context.synonyms == ("retail", "sales", "shops")
    assert doc.ai_context.examples == ("sales by year",)


def test_name_defined_in_two_files_in_another_case(tmp_path, tpcds):
    # Names resolve case-insensitively, so 'Total_Sales' is the metric 'total_sales'.
    sales, rest = split(tpcds)
    rest["metrics"].append(copy.deepcopy(sales["metrics"][0]) | {"name": "Total_Sales"})
    a, b = write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml")
    with pytest.raises(ModelError) as e:
        load_model(a, b)
    assert e.value.problems == [
        f"metric 'Total_Sales' is defined in both {a} (as 'total_sales') and {b}"
    ]


def test_same_file_twice_is_read_once(tmp_path, tpcds):
    a = write(tmp_path, tpcds, "a.yaml")
    assert len(load_model(a, a).datasets) == 5


def test_file_problems_reported_with_the_clashes(tmp_path, tpcds):
    # What one file breaks on its own is named with the file and reported in the same run
    # as a clash between files; the checks on the whole wait, since the whole is undefined.
    sales, rest = split(tpcds)
    rest["metrics"].append(copy.deepcopy(rest["metrics"][0]))
    rest["metrics"].append(copy.deepcopy(sales["metrics"][0]))
    rest["custom_extensions"] = [_clickhouse('{"dedup": "none"}')]
    a, b = write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml")
    with pytest.raises(ModelError) as e:
        load_model(a, b)
    assert e.value.problems == [
        f"{b}: duplicate metric name {rest['metrics'][0]['name']!r}",
        f"{b}: model: CLICKHOUSE entry: unknown key 'dedup'; known: time_zone",
        f"metric 'total_sales' is defined in both {a} and {b}",
    ]
    # Without a clash, the whole is checked in the same run as the files.
    rest["metrics"].pop()
    rel = rest["relationships"][0]
    rel["to"] = "nowhere"
    write(tmp_path, rest, "b.yaml")
    with pytest.raises(ModelError) as e:
        load_model(a, b)
    assert e.value.problems == [
        f"{b}: duplicate metric name {rest['metrics'][0]['name']!r}",
        f"{b}: model: CLICKHOUSE entry: unknown key 'dedup'; known: time_zone",
        f"relationship {rel['name']!r}: to dataset 'nowhere' does not exist",
    ]


def test_name_defined_in_two_files(tmp_path, tpcds):
    sales, rest = split(tpcds)
    rest["metrics"].append(copy.deepcopy(sales["metrics"][0]))
    rest["datasets"].append(copy.deepcopy(sales["datasets"][1]))
    a, b = write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml")
    with pytest.raises(ModelError) as e:
        load_model(a, b)
    assert e.value.problems == [
        f"dataset 'date_dim' is defined in both {a} and {b}",
        f"metric 'total_sales' is defined in both {a} and {b}",
    ]


def test_model_time_zone_in_one_file_of_several(tmp_path, tpcds):
    sales, rest = split(tpcds)
    rest["custom_extensions"] = [_clickhouse('{"time_zone": "America/New_York"}')]
    a, b = write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml")
    assert time_zone(load_model(a, b)) == "America/New_York"
    # In two files, even the same zone: one of them would be edited and not the other.
    sales["custom_extensions"] = [_clickhouse('{"time_zone": "America/New_York"}')]
    write(tmp_path, sales, "a.yaml")
    with pytest.raises(ModelError) as e:
        load_model(a, b)
    assert e.value.problems == [f"the model's CLICKHOUSE entry is in both {a} and {b}; keep one"]


def test_each_file_checked_against_the_schema(tmp_path, tpcds):
    sales, rest = split(tpcds)
    rest["version"] = "9.9.9"
    sales["datasets"][0]["primary_key"] = None
    a, b, missing = (tmp_path / n for n in ("a.yaml", "b.yaml", "c.yaml"))
    write(tmp_path, sales, a.name), write(tmp_path, rest, b.name)
    with pytest.raises(ModelError) as e:
        load_model(a, b, missing)
    ps = e.value.problems
    assert ps[0].startswith(f"{a}: property set to null")
    assert ps[1] == f"{b}: unsupported version '9.9.9'; this build supports '0.2.0.dev0'"
    assert ps[2].startswith(f"cannot read {missing}: ")  # names the file once


def test_load_model_needs_a_path():
    with pytest.raises(TypeError):
        load_model()


def test_cli_several_files(tmp_path, tpcds, capsys):
    sales, rest = split(tpcds)
    a, b = write(tmp_path, sales, "a.yaml"), write(tmp_path, rest, "b.yaml")
    # The reference model has a metric validate refuses; two files report the same.
    assert main(["validate", str(FIXTURE)]) == 1
    single = capsys.readouterr().err.splitlines()
    assert main(["validate", str(a), str(b)]) == 1
    pair = capsys.readouterr().err.splitlines()
    assert pair[:-1] == single[:-1]
    assert pair[-1] == f"{a}, {b}: 1 problem(s)"
    assert main(["sql", str(a), str(b), "-m", "total_sales", "-d", "store.s_state"]) == 0
    assert "LEFT JOIN tpcds.store AS store" in capsys.readouterr().out  # joins across files


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

"""Canaries: rules that exist only because of a gap in SQLGlot or a fact of Ossie.

Each test pins what an upstream project does today. It fails when that changes, and
its message names the rule to look at: remove it if the gap is closed, revise it if
the path through it moved. Stable on the versions in uv.lock; the weekly workflow
(.github/workflows/latest.yml) runs them on the newest SQLGlot and on Ossie's main.
"""

import os
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from dactopus_ossie_clickhouse._vendor.ossie import OssieDocument
from dactopus_ossie_clickhouse.model import SUPPORTED_VERSION
from dactopus_ossie_clickhouse.translate import _ANONYMOUS, _ZONED, OssieSQL, parse

pytestmark = pytest.mark.canary

FIXTURES = Path(__file__).parent / "fixtures"

# (expression, SQLGlot's ClickHouse SQL for it without rewrite_tree, the rule).
# Each output fails in ClickHouse or means something else than the spec.
GENERATED = [
    ("SPLIT_PART(s, ',', 2)", "SPLIT_PART(s, ',', 2)", "the exp.SplitPart branch of rewrite"),
    ("EXTRACT(DAYOFWEEK FROM d)", "EXTRACT(DAYOFWEEK FROM d)", "_EXTRACT_PARTS (ISO day)"),
    ("EXTRACT(DAYOFYEAR FROM d)", "EXTRACT(DAYOFYEAR FROM d)", "_EXTRACT_PARTS"),
    ("MEDIAN(x)", "median(x)", "the exp.Median branch of rewrite (exact, NULL if empty)"),
    (
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)",
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)",
        "the exp.WithinGroup branch of rewrite",
    ),
    (
        "PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x)",
        "PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY x)",
        "the exp.WithinGroup branch of rewrite",
    ),
    ("LAG(x) OVER (ORDER BY d)", "lag(x) OVER (ORDER BY d)", "toNullable in rewrite"),
    ("LEAD(x) OVER (ORDER BY d)", "lead(x) OVER (ORDER BY d)", "toNullable in rewrite"),
    (
        "NTH_VALUE(x, 2) OVER (ORDER BY d)",
        "NTH_VALUE(x, 2) OVER (ORDER BY d)",
        "toNullable in rewrite",
    ),
    (
        "FIRST_VALUE(x) OVER (ORDER BY d)",
        "FIRST_VALUE(x) OVER (ORDER BY d)",
        "RESPECT NULLS in rewrite",
    ),
    (
        "LAST_VALUE(x) OVER (ORDER BY d)",
        "LAST_VALUE(x) OVER (ORDER BY d)",
        "RESPECT NULLS in rewrite",
    ),
    ("LOG(3, x)", "LOG(3, x)", "the exp.Log branch of rewrite"),
    ("LOG(2.0, x)", "LOG(2.0, x)", "the exp.Log branch of rewrite"),
    ("LOG(-2, x)", "LOG2(x)", "the exp.Log branch of rewrite"),
    ("REGEXP_EXTRACT(s, 'a(b)')", "REGEXP_EXTRACT(s, 'a(b)')", "the exp.RegexpExtract branch"),
    ("TO_CHAR(d, 'YYYY-MM-DD')", "CAST(d AS Nullable(String))", "the exp.ToChar branch"),
]


@pytest.mark.parametrize(("ossie", "generated", "rule"), GENERATED)
def test_sqlglot_generates(ossie, generated, rule):
    actual = parse(ossie).sql(dialect="clickhouse")
    assert actual == generated, (
        f"SQLGlot {sqlglot.__version__} prints {ossie} as {actual}, no longer as "
        f"{generated}: check whether {rule} in translate.py is still needed"
    )


# Functions the default dialect does not type, rewritten by name in _ANONYMOUS and
# _ZONED. Typed, they would skip those rules.
ANONYMOUS = [
    "APPROX_PERCENTILE(x, 0.9)",
    "IFF(a, 1, 2)",
    "ZEROIFNULL(x)",
    "NULLIFZERO(x)",
    "TO_DATE(s)",
    "TO_TIMESTAMP(s)",
]


def test_anonymous_canaries_cover_every_rule():
    names = {call.split("(")[0] for call in ANONYMOUS}
    assert names == set(_ANONYMOUS) | set(_ZONED), "add a call to ANONYMOUS for each rule"


@pytest.mark.parametrize("ossie", ANONYMOUS)
def test_sqlglot_parses_as_anonymous(ossie):
    tree = parse(ossie)
    assert isinstance(tree, exp.Anonymous), (
        f"SQLGlot {sqlglot.__version__} parses {ossie} as {type(tree).__name__}: "
        "move its rule out of _ANONYMOUS or _ZONED in translate.py"
    )


def test_sqlglot_parses_parametric_call_as_aliases():
    tree = sqlglot.parse_one("quantile(0.5)(x)", read=OssieSQL)
    assert isinstance(tree, exp.Aliases), (
        f"SQLGlot {sqlglot.__version__} parses quantile(0.5)(x) as {type(tree).__name__}: "
        "check the exp.Aliases refusal in _shape_problem in translate.py"
    )


def test_ossie_schema_version():
    version = OssieDocument.model_fields["version"].default
    assert version == SUPPORTED_VERSION, (
        f"apache-ossie declares schema version {version}: accept it in "
        "SUPPORTED_VERSION (model.py) and check what changed in the spec"
    )


# A window's ORDER BY without NULLS FIRST | LAST has the spec's default, NULL as the largest
# value (parse() sets nulls_first on DESC). It relies on ClickHouse's own default, NULLs
# last both ways, which SQLGlot's ClickHouse generator leaves unwritten, and on NULLS FIRST
# surviving, in either direction. Without the clause, ASC needs no rule.
NULL_PLACEMENT = [
    ("k", "k"),
    ("k ASC", "k ASC"),
    ("k NULLS LAST", "k"),
    ("k DESC NULLS LAST", "k DESC"),
    ("k NULLS FIRST", "k NULLS FIRST"),
    ("k DESC NULLS FIRST", "k DESC NULLS FIRST"),
]


@pytest.mark.parametrize(("order", "generated"), NULL_PLACEMENT)
def test_sqlglot_places_nulls_as_clickhouse_does(order, generated):
    tree = sqlglot.parse_one(f"SUM(v) OVER (ORDER BY {order})", read=OssieSQL)
    actual = tree.sql(dialect="clickhouse")
    assert actual == f"SUM(v) OVER (ORDER BY {generated})", (
        f"SQLGlot {sqlglot.__version__} prints ORDER BY {order} as {actual}: check the "
        "window ORDER BY default in parse() (docs/design.md, NULL placement) and the NULLS "
        "tests in tests/test_translate.py"
    )


@pytest.mark.skipif("OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie")
def test_ossie_window_order_default_is_null_largest():
    text = (Path(os.environ["OSSIE_REPO"]) / "core-spec" / "expression_language.md").read_text()
    if "## Window ORDER BY NULL Placement" not in text:
        pytest.skip("this checkout of apache/ossie predates its NULL placement rule (#587)")
    assert (
        "Without `NULLS FIRST` or `NULLS LAST`, NULL sorts as the largest value: "
        "last for `ASC`, first for `DESC`." in text
    ), (
        "apache/ossie's expression_language.md no longer says where NULL sorts in a "
        "window's ORDER BY without a clause: check the default in parse() (translate.py)"
    )


@pytest.mark.parametrize("call", ["AGG(x)", "agg(x)", "Agg(x) OVER ()"])
def test_sqlglot_parses_agg_as_an_anonymous_function(call):
    tree = sqlglot.parse_one(call, read=OssieSQL)
    node = tree.this if isinstance(tree, exp.Window) else tree
    assert isinstance(node, exp.Anonymous) and node.name.upper() == "AGG", (
        f"SQLGlot {sqlglot.__version__} parses {call} as {type(node).__name__}: check the AGG "
        "refusal in _shape_problem in translate.py, which looks for an anonymous function"
    )


@pytest.mark.parametrize("name", ["source.amount", "SOURCE.amount", "Source.amount"])
def test_sqlglot_parses_source_column_as_a_qualified_column(name):
    tree = sqlglot.parse_one(f"SUM({name})", read=OssieSQL).this
    assert isinstance(tree, exp.Column) and (tree.table, tree.name) == (name[:6], "amount"), (
        f"SQLGlot {sqlglot.__version__} parses {name} as {tree!r}: check source_columns "
        "(translate.py) and Planner._own_field, which read the qualifier of a column"
    )


@pytest.mark.skipif("OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie")
def test_ossie_reserves_agg_and_reads_source_columns():
    core = Path(os.environ["OSSIE_REPO"]) / "core-spec"
    language = (core / "expression_language.md").read_text()
    spec = (core / "spec.md").read_text()
    if "Measure Evaluation: `AGG` (RESERVED)" not in language:
        pytest.skip("this checkout of apache/ossie predates the reserved AGG (#662)")
    assert "`OSSIE_SQL_2026` expressions MUST NOT call `AGG`" in language, (
        "apache/ossie's expression_language.md no longer forbids calling AGG: check the AGG "
        "refusal in _shape_problem (translate.py) and docs/design.md"
    )
    assert "source.quantity` and `SOURCE.quantity` name the same column" in spec, (
        "apache/ossie's spec.md no longer lets a dataset's metric read source.column: check "
        "Planner._own_field and docs/design.md"
    )
    assert "the name `source` refers to that field" in spec


@pytest.mark.skipif("OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie")
def test_tpcds_fixture_matches_ossie_example():
    example = Path(os.environ["OSSIE_REPO"]) / "examples" / "tpcds_semantic_model.yaml"
    assert (FIXTURES / "tpcds.yaml").read_text() == example.read_text(), (
        "tests/fixtures/tpcds.yaml differs from apache/ossie's "
        "examples/tpcds_semantic_model.yaml: copy it, and fix what the change breaks"
    )

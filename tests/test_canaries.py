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
from ossie import OssieDocument
from sqlglot import exp

from dactopus_ossie_clickhouse.model import SUPPORTED_VERSION
from dactopus_ossie_clickhouse.translate import OssieSQL, parse

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
@pytest.mark.parametrize(
    "ossie",
    [
        "APPROX_PERCENTILE(x, 0.9)",
        "IFF(a, 1, 2)",
        "ZEROIFNULL(x)",
        "NULLIFZERO(x)",
        "TO_DATE(s)",
        "TO_TIMESTAMP(s)",
    ],
)
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


@pytest.mark.skipif("OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie")
def test_tpcds_fixture_matches_ossie_example():
    example = Path(os.environ["OSSIE_REPO"]) / "examples" / "tpcds_semantic_model.yaml"
    assert (FIXTURES / "tpcds.yaml").read_text() == example.read_text(), (
        "tests/fixtures/tpcds.yaml differs from apache/ossie's "
        "examples/tpcds_semantic_model.yaml: copy it, and fix what the change breaks"
    )

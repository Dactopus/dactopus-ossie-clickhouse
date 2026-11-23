"""The portability ladder of apache/ossie (``compliance/portability``), read by this
executor.

Each rung adds one construct (a metric through a relationship, a metric over
metrics, date functions, windows), written in a portable dialect for converters to
round-trip. The rungs carry no data and no expected values, so nothing here checks a
number: each model loads, has no problem ``check_model`` finds, every field and
metric translates, and every metric plans. The models are read from the checkout, rungs added
upstream included.

Needs ``OSSIE_REPO``, a checkout of apache/ossie.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dactopus_ossie_clickhouse import load_model
from dactopus_ossie_clickhouse.model import check_model
from dactopus_ossie_clickhouse.planner import unanswerable_metrics
from dactopus_ossie_clickhouse.translate import untranslatable

pytestmark = pytest.mark.skipif(
    "OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie"
)


def _models() -> list[Path]:
    if "OSSIE_REPO" not in os.environ:
        return []
    return sorted(
        (Path(os.environ["OSSIE_REPO"]) / "compliance" / "portability").glob("m*/*.ossie.yaml")
    )


def test_the_checkout_has_the_ladder():
    if not _models():
        pytest.skip("this checkout of apache/ossie predates its portability ladder (#631)")


@pytest.mark.parametrize("path", _models(), ids=lambda p: p.name)
def test_a_rung_is_read(path):
    model = load_model(path)  # refuses a model with a problem check_model finds
    assert check_model(model) == []
    assert untranslatable(model) == ({}, {})
    assert unanswerable_metrics(model) == {}

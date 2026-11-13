"""The expression fixtures of apache/ossie (``compliance/expressions``), read by
``parse()``.

``valid.txt`` holds expressions a parser of ``OSSIE_SQL_2026`` must accept and
``invalid.txt`` ones it must reject; the reference parser is the ANTLR grammar
in ``core-spec/grammar``. The fixtures say nothing of values. Each valid line
here parses and translates to ClickHouse SQL, and each invalid line is refused,
unless it is listed below with why. A listed line the checkout no longer has
fails, so the lists do not outlive their reason.

Needs ``OSSIE_REPO``, a checkout of apache/ossie.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dactopus_ossie_clickhouse.translate import to_clickhouse

pytestmark = pytest.mark.skipif(
    "OSSIE_REPO" not in os.environ, reason="OSSIE_REPO: a checkout of apache/ossie"
)

# Valid lines this translator refuses, with why.
REFUSED = {}

# Invalid lines this translator accepts, with why. ClickHouse's INTERVAL arithmetic
# reaches ClickHouse as written; the MCP server's instructions teach it for filters
# on a period counted from now (CURRENT_DATE - INTERVAL '6' DAY).
ACCEPTED = {
    "order_date + INTERVAL '7 days'": "ClickHouse's INTERVAL, taught for filters",
    "DATE_ADD(d, INTERVAL 7 DAY)": "ClickHouse's INTERVAL, taught for filters",
}


def _lines(name: str) -> list[str]:
    path = Path(os.environ["OSSIE_REPO"]) / "compliance" / "expressions" / name
    if not path.exists():
        pytest.skip("this checkout of apache/ossie predates its expression fixtures (#657)")
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _refusal(expression: str) -> str | None:
    try:
        to_clickhouse(expression)
    except ValueError as e:
        return str(e)
    return None


def test_valid_expressions_translate():
    lines = _lines("valid.txt")
    refused = {e: why for e in lines if e not in REFUSED and (why := _refusal(e))}
    assert not refused, refused
    assert set(REFUSED) <= set(lines), set(REFUSED) - set(lines)
    accepted = [e for e in REFUSED if _refusal(e) is None]
    assert not accepted, f"listed as refused, but translates: {accepted}"


def test_invalid_expressions_are_refused():
    lines = _lines("invalid.txt")
    accepted = [e for e in lines if e not in ACCEPTED and _refusal(e) is None]
    assert not accepted, accepted
    assert set(ACCEPTED) <= set(lines), set(ACCEPTED) - set(lines)
    refused = [e for e in ACCEPTED if _refusal(e) is not None]
    assert not refused, f"listed as accepted, but refused: {refused}"

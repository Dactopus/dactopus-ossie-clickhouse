"""Decision records cited from code, tests and docs exist, and the index lists each one.

Code and tests cite a decision by its file under docs/decisions/, so a renamed or
removed file fails here instead of leaving a comment that points nowhere.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "docs" / "decisions"
CITED = re.compile(r"docs/decisions/([\w-]+\.md)")
LINK = re.compile(r"\]\(([^)\s#]+\.md)(?:#([\w-]+))?\)")


def _sources():
    for pattern in ("src/**/*.py", "tests/*.py", "scripts/*.py", "*.md", "docs/**/*.md"):
        for path in ROOT.glob(pattern):
            if "_vendor" not in path.parts:
                yield path


def _anchors(path):
    headings = re.findall(r"(?m)^#+ (.+)$", path.read_text())
    return {re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-") for h in headings}


def test_every_cited_decision_exists():
    missing = {
        f"{path.relative_to(ROOT)}: {name}"
        for path in _sources()
        for name in CITED.findall(path.read_text())
        if not (DECISIONS / name).is_file()
    }
    assert not missing, sorted(missing)


def test_the_index_lists_every_decision():
    index = (ROOT / "docs/design.md").read_text()
    listed = {Path(target).name for target, _ in LINK.findall(index)}
    on_disk = {path.name for path in DECISIONS.glob("*.md")}
    assert on_disk - listed == set(), "not in docs/design.md"
    others = {name for name in listed if name not in on_disk}
    assert others == {"model-authoring.md", "CHANGELOG.md"}


def test_links_between_decisions_resolve():
    broken = []
    for path in DECISIONS.glob("*.md"):
        for target, anchor in LINK.findall(path.read_text()):
            linked = (path.parent / target).resolve()
            if not linked.is_file() or (anchor and anchor not in _anchors(linked)):
                broken.append(f"{path.name}: {target}#{anchor}")
    for path in DECISIONS.glob("*.md"):
        for anchor in re.findall(r"\]\(#([\w-]+)\)", path.read_text()):
            if anchor not in _anchors(path):
                broken.append(f"{path.name}: #{anchor}")
    assert not broken, broken

"""Copy the apache-ossie package into src/dactopus_ossie_clickhouse/_vendor/ossie.

apache-ossie is not on PyPI, and PyPI refuses a package that depends on a git
reference, so this package carries a copy (docs/design.md, "apache-ossie is
vendored until it reaches PyPI"). The copy is upstream's python/src/ossie at one
commit, with LICENSE and NOTICE, and one change: the package's imports of itself
are relative, so no code of an apache-ossie installed next to it is imported.
vendor.txt names the commit.

    python scripts/vendor_ossie.py <commit>  # replace the copy with that commit's
    python scripts/vendor_ossie.py main      # the weekly run, on Ossie's main
    python scripts/vendor_ossie.py --check   # the copy is vendor.txt's commit, unchanged

A commit is given in full: GitHub fetches no abbreviated one.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = "https://github.com/apache/ossie.git"
PACKAGE = "python/src/ossie"
ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "src/dactopus_ossie_clickhouse/_vendor"
TARGET = VENDOR / "ossie"
PIN = VENDOR / "vendor.txt"
SHOWN = TARGET.relative_to(ROOT)

_SELF_IMPORT = re.compile(r"^(\s*)from ossie(?:\.(\w+))? import ", re.MULTILINE)
_LEFT = re.compile(r"^\s*(?:import ossie\b|from ossie\b)", re.MULTILINE)


def fetch(ref: str, into: Path) -> str:
    """Check out the package at ``ref`` into ``into``; return the full commit."""

    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(into), *args], capture_output=True, text=True)
        if done.returncode:
            raise SystemExit(f"git {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout.strip()

    git("init", "-q")
    git("remote", "add", "origin", REPOSITORY)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref)
    git("sparse-checkout", "set", PACKAGE)  # cone mode keeps the top-level files too
    git("checkout", "-q", "FETCH_HEAD")
    return git("rev-parse", "FETCH_HEAD")


def relative(source: str, name: str) -> str:
    out = _SELF_IMPORT.sub(lambda m: f"{m[1]}from .{m[2] or ''} import ", source)
    if _LEFT.search(out):
        raise SystemExit(f"{name}: an import of ossie this script cannot make relative")
    return out


def copy(checkout: Path) -> dict[str, str]:
    package = checkout / PACKAGE
    other = [
        p.name
        for p in package.iterdir()
        if p.name != "__pycache__" and (p.is_dir() or p.suffix != ".py")
    ]
    if other:
        raise SystemExit(f"{PACKAGE} has {other}, not modules: teach this script about them")
    files = {
        p.name: relative(p.read_text(encoding="utf-8"), p.name)
        for p in sorted(package.glob("*.py"))
    }
    for name in ("LICENSE", "NOTICE"):
        files[name] = (checkout / name).read_text(encoding="utf-8")
    return files


def pin_line(commit: str) -> str:
    return f"apache-ossie @ git+{REPOSITORY}@{commit}#subdirectory=python\n"


def pinned() -> str:
    match = re.fullmatch(
        r"apache-ossie @ git\+\S+@([0-9a-f]{40})#\S+\n", PIN.read_text(encoding="utf-8")
    )
    if not match:
        raise SystemExit(f"{PIN.name}: not one line of the form {pin_line('<commit>')!r}")
    return match[1]


def present() -> list[Path]:
    """The copy's files; dotfiles such as .DS_Store, and __pycache__, are not upstream's."""
    return sorted(p for p in TARGET.iterdir() if p.is_file() and not p.name.startswith("."))


def current() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in present()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("ref", nargs="?", help="full commit, branch or tag of apache/ossie")
    group.add_argument("--check", action="store_true", help="compare the copy with vendor.txt")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        commit = fetch(pinned() if args.check else args.ref, Path(tmp))
        files = copy(Path(tmp))

    if args.check:
        have = current()
        differ = sorted(n for n in files.keys() | have.keys() if files.get(n) != have.get(n))
        if differ:
            print(f"{SHOWN} differs from apache/ossie {commit[:7]}: {', '.join(differ)}")
            print(f"Run: python scripts/vendor_ossie.py {commit}")
            sys.exit(1)
        print(f"{SHOWN} is apache/ossie {commit[:7]}")
        return

    TARGET.mkdir(parents=True, exist_ok=True)
    for p in present():
        if p.name not in files:
            p.unlink()
    for name, text in files.items():
        (TARGET / name).write_text(text, encoding="utf-8")
    PIN.write_text(pin_line(commit), encoding="utf-8")
    print(f"{SHOWN} is apache/ossie {commit[:7]}")


if __name__ == "__main__":
    main()

# apache-ossie is vendored until it reaches PyPI

PyPI refuses an upload whose dependencies include a direct reference
(`name @ git+https://...`), and apache-ossie was one: it is not on PyPI
(checked 2026-11-01), and Ossie's first release has not been voted yet.
Even then, an ASF release is a source release, and publishing to PyPI is
optional. Until apache-ossie is on PyPI, this package carries a copy, so
that it, and the runner that depends on it, can go to PyPI.

- **What is copied.** `python/src/ossie` at one commit (three modules, 593
  lines at `fab4f63`) with the repository's `LICENSE` and `NOTICE`.
  `_vendor/vendor.txt` names the commit. Its dependencies, pydantic and
  PyYAML, are direct dependencies here.
- **The one change.** `scripts/vendor_ossie.py` makes the package's
  imports of itself relative, so no code of an `ossie` installed
  next to it is imported (`tests/test_model.py` checks that). CI runs the script with
  `--check`, which fails on any other difference from upstream.
- **What users see.** `load_model` returns
  `dactopus_ossie_clickhouse._vendor.ossie.OssieDocument`, a class distinct
  from an installed apache-ossie's. `Executor` refuses a document of that
  other class with a `TypeError`: `model.py` tells datasets from the model
  with `isinstance`, so it would drop a dataset's `dedup` override and
  report a false error.
- **The weekly run.** `latest.yml` vendors Ossie's `main` before its tests
  and canaries, where it used to install it.

Not chosen:

- **A package of our own on PyPI with the copy.** One more package to
  release, and "Ossie" in its name is the ASF's mark.
- **Our own model classes.** They would drift from the spec's.
- **Relative imports upstream.** apache/ossie has no relative import
  anywhere, and the change would serve only those who vendor; here it is
  two lines of the script.

When apache-ossie is on PyPI, the dependency returns and `_vendor` goes.

# Parsing with SQLGlot's default dialect

apache/ossie PR #222 added an Ossie dialect to SQLGlot. Evaluated and not
depended on: it parsed and validated the spec's SQL, but the mapping to
ClickHouse stays this project's work either way. The PR was closed
unmerged on 2026-11-10, with the ANTLR grammar of apache/ossie#657 as the
route to parsers. That grammar, `core-spec/grammar/OssieExpression.g4`
(merged 2026-11-12), is a reference artifact and ships no parser; its
conformance fixtures, `compliance/expressions/valid.txt` and
`invalid.txt`, run against `parse()` in
`tests/test_expression_fixtures.py`, each line accepted or refused as
the fixture says or listed there with why. Switching to an upstream
parser once one is on PyPI is #4; the rewrite layer is unchanged either
way.

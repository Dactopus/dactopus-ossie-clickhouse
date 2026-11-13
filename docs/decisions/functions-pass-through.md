# Functions outside the spec pass through as written

`parse()` gives SQLGlot's typed nodes to the spec's function catalog only
(`_CATALOG` in `translate.py`, the tables of `expression_language.md`);
any other function name parses as `exp.Anonymous` and prints as written.
SQLGlot's default dialect knows the functions of many engines, and its
ClickHouse generator renames 135 of them (counted on SQLGlot 30.23):
`DAYOFWEEK(d)` became `DAY_OF_WEEK(d)` and `BOOL_AND(x)` became
`LOGICAL_AND(x)`, neither of which ClickHouse 26.9 has, though it accepts
`DAYOFWEEK` itself as a case-insensitive alias of `toDayOfWeek`. The
query failed and `validate` said nothing (#35). The spec's own rule for
what it does not define is to pass it through ("Dialect Extensions"),
and a name sent as written is what the author can look up in
ClickHouse's documentation.

The cost is SQLGlot's translation of other engines' spellings: `ARG_MAX`,
`COUNT_IF`, `LEN` do not become `argMax`, `countIf`, `CHAR_LENGTH`.
The spec's aliases (`NVL`, `IFNULL`, `CEILING`, `TRUNCATE`, `CHARINDEX`)
are in the catalog and still translate. `ANY_VALUE`, `BOOL_AND` and
`BOOL_OR` left the planner's list of aggregates that may read a joined
dataset on their own: they are not the spec's, `BOOL_AND` and `BOOL_OR`
never ran, and `ANY_VALUE` runs as ClickHouse's alias of `any`, which
the planner knows as repeat-safe when connected to a server (below).

Whether a pass-through function exists only the server can say.
`Executor` reads `system.functions`, which lists user-defined functions
too, and matches a name as ClickHouse does: as written, or in any case
when the server marks it case-insensitive, and for an aggregate with
combinators after it, each as written (`sumIf` and `uniqExactOrNull`
resolve, `SUMIF` does not; `countIf` is not listed, `count` and the `If`
combinator are). A field or metric calling a function the server lacks
is hidden like one that does not translate and reported by `validate
--url` with the nearest names. Without a server nothing checks it.

The same list tells the planner which pass-through calls aggregate.
`Catalog.aggregate` answers it with the combinator rule above, and the
planner types such a call as `exp.AnonymousAggFunc`, so every check on
aggregates covers it, and a filter on it goes to `HAVING`. Before,
SQLGlot typed a few of them (`countIf`, `argMax`, `corr`) and left the
rest (`sumIf`, `uniq`) unchecked; a metric like `countIf(store.x > 10)`
next to a sales dimension counted each store once per sale. Without the
server's word (the `sql` command without `--url`, or a server whose
`system.functions` cannot be read) the planner asks SQLGlot's list of
ClickHouse aggregates, with the same combinator rule
(`_known_clickhouse_aggregate`), so `countIf` is still an aggregate
there. One the list lacks stays a plain call, and that is about half
of them: 131 of the 259 aggregates ClickHouse 26.9 lists are missing
from SQLGlot 30.23's list, among them `medianExact`, `group_concat`,
`regr_count`, `any_value` and every user-defined aggregate.

One over a joined dataset alone is refused unless repeats cannot change
it. `system.functions` does not say which do, so the planner lists them
(`_CLICKHOUSE_REPEAT_SAFE`; the list is in
[model authoring](../model-authoring.md#keys-decide-which-joins-are-allowed)),
each checked on ClickHouse 26.9 against
its input with rows repeated. A combinator keeps that only if it is `If`,
`OrNull`, `OrDefault`, or `Distinct`, which makes any aggregate
repeat-safe; names are split as ClickHouse splits them, longest
combinator first. Any other is repeat-sensitive, as the spec's are.

# Paths of several relationships

A question may touch a dataset the root reaches only through others, such
as order lines to orders to customers. apache/ossie#653 section 4
resolves it: the one path of many-to-one relationships is used, two or
more are ambiguous (`E_AMBIGUOUS_PATH`), and a path visits no dataset
twice. A dataset no path reaches is `E_NO_PATH` (section 5.3). The
datasets in the middle are joined like the others (the join tree of
section 5.4), so a row filter may read them, and each relationship on the
path must cover a key of its target.

Any second path is ambiguous, a direct relationship next to a longer
path included: section 4 says "even if one path is shorter". The paths
can mean different things: `store_sales` to `date_dim` is the sale's
date, `store_sales` to `store` to `date_dim` could be the store's closing
date. Only paths to datasets the question touches count: two
paths elsewhere in the model do not refuse a question that does not need
them.

The root is a dataset the question names; a dataset it does not name is
never chosen as the root, so dimensions of two dimension tables stay
refused even when a fact reaches both. A dictionary at the end of a path
is read with `dictGetOrNull` on the key column of the dataset before it;
one in the middle is joined, since the next join reads its columns. A
dataset reached through relationships that each cover a key of their from
side is one to one with the root, so its rows do not repeat (next
section).

`tests/test_paths.py` compares answers with DuckDB on data with an
orphan at each step of the path: both land in one NULL group, inner joins
lose them, and planning from the dimension outward shows a region no fact
reaches.

# Keys and relationships name fields

apache/ossie#486 (`7205e15`) settled what `primary_key`, `unique_keys`,
`from_columns` and `to_columns` name: fields of their dataset, never
columns of its source. They are checked against the declared fields when
a model loads, and the planner joins on the fields' expressions, so a key
field may read a column of another name or an expression. A relationship
goes with its fields when the model is trimmed for a user: a field hidden
by grants or a policy file must not reach SQL through a join.

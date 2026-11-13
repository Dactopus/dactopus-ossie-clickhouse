# A metric that reads no dataset

A metric that reads no dataset, such as `CURRENT_TIMESTAMP`, is answered
without `FROM`: one row, or the same value on every row of a question's
dataset; in a stitched question it joins a group of the others, where a
group of its own would add a row for every dimension value. An aggregate
of it (`MAX(CURRENT_TIMESTAMP)`) is still refused in a model of several
datasets: it would aggregate the rows of whichever dataset the question
is answered over.

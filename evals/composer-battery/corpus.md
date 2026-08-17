# Composer battery corpus

corpus_version: 0

Rules (spec §1): operator voice; task, never implementation; tight enough that one
shape is the reasonable reading; invented data; every prompt must NOT classify
EXPLICIT_MUTATION (tests/unit/evals/composer_battery/test_corpus.py enforces it).
The first unlabelled fenced block under each heading is sent byte-for-byte.

## canary

```
I've got a tiny list of three colours with a name and a hex code — just make it up.
I only want it read in and written back out as JSON, nothing else done to it.
```

## fork_coalesce

```
Make up three products, each with a sku, a name, a price and a long rambling description.
First trim every description down to forty characters. Then I want each product sent
down two parallel paths at the same time and the two copies brought back together
into one record per product, with each path's copy kept under its own key rather
than blended. A final yes/no check on the merged records that always passes should
sit before the output. Write the merged records out as JSON.
```

## transform_pipeline

```
Make up a handful of orders — an order id, a quantity and a unit price, all as
plain text the way a spreadsheet export would give them. First turn the quantity
and unit price into proper numbers, then work out a line total for each order,
and write the finished orders out as CSV.
```

# Batch Error Routing — When One Row Fails A Whole Batch

Demonstrates what an aggregation does when one buffered row cannot be
processed: the **whole batch** fails, `on_error` decides where **every row of
that batch** goes, and the audit trail records why without recording the bad
value.

Twelve orders are summed three at a time by `batch_stats`. Order `A-008`
arrived with its amount as the string `"1,250.00"` instead of a number, so the
third batch (`A-007`, `A-008`, `A-009`) cannot be summed.

Three configs ship here. Run one at a time.

| Config | What happens to the bad order | Output | Exit |
|--------|-------------------------------|--------|------|
| `settings.yaml` | its whole batch fails; all 3 rows go to the `failed_batches` sink with their original values | 3 totals, 3 failed rows | 1 (`PARTIAL`) |
| `settings_discard.yaml` | its whole batch fails; `on_error: discard` records all 3 rows as quarantined and writes them nowhere | 3 totals | 1 (`PARTIAL`) |
| `settings_declared.yaml` | a `value_transform` whose output is declared `int` catches the bad value on its own row, before the batch | 4 totals, 1 failed row | 1 (`PARTIAL`) |

**All three end `PARTIAL` with exit 1, by design.** A run with any failed row is
`PARTIAL`, and a `PARTIAL` run exits 1. Check the row counts below rather than
the exit code alone.

## What This Shows

```
source (12 orders) ─(batch_in)─> [batch_totals: batch_stats, count=3] ─┬─(on_success)─> totals
                                                                       └─(on_error)───> failed_batches
```

**Why the whole batch fails.** A per-row transform can route one bad row and
carry on. A reducer cannot: dropping `A-008` and publishing the total of
`A-007` and `A-009` would put a statistic over two orders into a column that
says it is over three, and nothing in the output row would say so. So the
batch fails as a unit, and the aggregation's `on_error` applies to every row
in it.

**Why the source declares `amount: any`.** An observed source fixes each
field's type from the first row and rejects any later row whose type differs.
With `schema: {mode: observed}` the source itself rejects `A-008`, and the
aggregation never sees it: 11 rows, 4 totals, exit 0. Declaring the column
`any` accepts the feed as it arrives and leaves the type check to the
consumer that needs a number. That is the case this example is about.

## Running

```bash
elspeth run --settings examples/batch_error_routing/settings.yaml --execute           # exit 1
elspeth run --settings examples/batch_error_routing/settings_discard.yaml --execute   # exit 1
elspeth run --settings examples/batch_error_routing/settings_declared.yaml --execute  # exit 1
```

Expected summary lines:

```text
settings.yaml          ⚠ Run PARTIAL: 12 rows processed | ✓3 succeeded | ✗3 failed | ⚠0 quarantined | →3 routed (failed_batches:3)
settings_discard.yaml  ⚠ Run PARTIAL: 12 rows processed | ✓3 succeeded | ✗3 failed | ⚠3 quarantined
settings_declared.yaml ⚠ Run PARTIAL: 12 rows processed | ✓4 succeeded | ✗1 failed | ⚠0 quarantined | →1 routed (failed_rows:1)
```

`succeeded` counts the rows the aggregation **wrote** (one total per batch),
not source rows. The source rows are accounted for in the audit queries
below: in `settings.yaml`, 9 rows were consumed into the three good batches
and 3 were routed, which is all 12.

## Output

`settings.yaml`:

```console
$ cat examples/batch_error_routing/output/totals.csv
batch_size,count,mean,sum
3,3,148.33333333333334,445      # A-001..A-003
3,3,155,465                     # A-004..A-006
3,3,126.66666666666667,380      # A-010..A-012

$ cat examples/batch_error_routing/output/failed_batches.csv
amount,order_id,region
150,A-007,east
"1,250.00",A-008,west
70,A-009,north
```

The third batch has no total. All three of its rows reach `failed_batches`
with the values they arrived with, including the two that were fine: they are
there because their batch failed, and the audit trail says so.

`settings_discard.yaml` writes the same three totals to `totals_discard.csv`
and no failed-rows file.

`settings_declared.yaml`:

```console
$ cat examples/batch_error_routing/output/totals_declared.csv
batch_size,count,mean,sum
3,3,14833.333333333334,44500    # A-001..A-003, in cents
3,3,15500,46500                 # A-004..A-006
3,3,14166.666666666666,42500    # A-007, A-009, A-010
2,2,8750,17500                  # A-011, A-012, flushed at end of source

$ cat examples/batch_error_routing/output/failed_rows_declared.csv
amount,order_id,region
"1,250.00",A-008,west
```

`failed_batches_declared.csv` is not created: no batch fails.

## What The Audit Trail Records

Each config writes its own audit database under `runs/`. Re-running a config
adds another run to the same database, so every query below reads only the
most recent run. Run the queries from the repository root.

### 1. Every row of the failed batch is a routed failure (`settings.yaml`)

```bash
sqlite3 -header -column examples/batch_error_routing/runs/audit.db "
SELECT outcome, path, sink_name, COUNT(*) AS tokens
FROM token_outcomes
WHERE completed = 1
  AND run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)
GROUP BY outcome, path, sink_name
ORDER BY outcome, path;"
```

```text
outcome    path             sink_name       tokens
---------  ---------------  --------------  ------
failure    on_error_routed  failed_batches  3
success    default_flow     totals          3
transient  batch_consumed                   9
```

`(failure, on_error_routed, failed_batches)` is the outcome of each of the
three rows of the failed batch. The nine `batch_consumed` rows went into the
three good batches, and the three `success` tokens are the totals those
batches wrote.

To see which rows those are, join the failed batch to its members:

```bash
sqlite3 -header -column examples/batch_error_routing/runs/audit.db "
SELECT bm.ordinal AS batch_row, r.source_row_index, o.outcome, o.path, o.sink_name
FROM batches b
JOIN batch_members bm  ON bm.batch_id = b.batch_id AND bm.run_id = b.run_id
JOIN tokens t          ON t.token_id = bm.token_id AND t.run_id = b.run_id
JOIN rows r            ON r.row_id = t.row_id AND r.run_id = b.run_id
JOIN token_outcomes o  ON o.token_id = t.token_id AND o.run_id = b.run_id AND o.completed = 1
WHERE b.status = 'failed'
  AND b.run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)
ORDER BY bm.ordinal;"
```

```text
batch_row  source_row_index  outcome  path             sink_name
---------  ----------------  -------  ---------------  --------------
0          6                 failure  on_error_routed  failed_batches
1          7                 failure  on_error_routed  failed_batches
2          8                 failure  on_error_routed  failed_batches
```

Both indexes count from 0: source rows 6, 7 and 8 are `A-007`, `A-008` and
`A-009`.

### 2. One routing decision for the batch, not one per row (`settings.yaml`)

```bash
sqlite3 -header -column examples/batch_error_routing/runs/audit.db "
SELECT re.mode, e.label, e.to_node_id
FROM routing_events re
JOIN edges e ON e.edge_id = re.edge_id AND e.run_id = re.run_id
WHERE re.run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1);"
```

```text
mode    label                   to_node_id
------  ----------------------  --------------------------------
divert  __error_batch_totals__  sink_failed_batches_<hash>
```

The batch failed once, so the aggregation's flush records exactly one
`divert` onto its error edge. `<hash>` stands for the sink node id's suffix,
which stays the same when an unchanged config is re-run.

### 3. The reason names the fault, not the value (`settings.yaml`)

```bash
sqlite3 -header examples/batch_error_routing/runs/audit.db "
SELECT json_extract(row_data_json, '$.order_id') AS order_id, destination, error_details_json
FROM transform_errors
WHERE run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)
ORDER BY order_id;"
```

```text
order_id|destination|error_details_json
A-007|failed_batches|{"actual_type":"str","error":"must be numeric (int or float), got str in row 1","error_type":"wrong_type","expected":"numeric (int or float)","field":"amount","reason":"invalid_input"}
A-008|failed_batches|{"actual_type":"str","error":"must be numeric (int or float), got str in row 1","error_type":"wrong_type","expected":"numeric (int or float)","field":"amount","reason":"invalid_input"}
A-009|failed_batches|{"actual_type":"str","error":"must be numeric (int or float), got str in row 1","error_type":"wrong_type","expected":"numeric (int or float)","field":"amount","reason":"invalid_input"}
```

There is one `transform_errors` row per row of the failed batch, and each
carries the batch's reason:

- `field`: `amount`
- `expected`: `numeric (int or float)`
- `actual_type`: `str`
- `row 1`: the offending row's index **within the batch**, counting from 0
  (`A-008` is the second of the three)

`"1,250.00"` appears nowhere in the reason. The reason names the first row
that could not be used; this batch has only one. `row_data_json` is the
separate per-row evidence column: it holds each routed row as it arrived,
which is how the query above can show the order id.

The same reason is stored once on the failed flush itself:

```bash
sqlite3 -header examples/batch_error_routing/runs/audit.db "
SELECT ns.status, ns.error_json
FROM batches b
JOIN node_states ns ON ns.state_id = b.aggregation_state_id AND ns.run_id = b.run_id
WHERE b.status = 'failed'
  AND b.run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1);"
```

### 4. Discard is recorded as quarantine (`settings_discard.yaml`)

```bash
sqlite3 -header -column examples/batch_error_routing/runs/audit_discard.db "
SELECT outcome, path, sink_name, COUNT(*) AS tokens
FROM token_outcomes
WHERE completed = 1
  AND run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)
GROUP BY outcome, path, sink_name
ORDER BY outcome, path;"
```

```text
outcome    path                   sink_name  tokens
---------  ---------------------  ---------  ------
failure    quarantined_at_source             3
success    default_flow           totals     3
transient  batch_consumed                    9
```

The three rows of the failed batch end `(failure, quarantined_at_source)`, the
same outcome a discarded single-row failure records, which is why the summary
line counts them under `⚠3 quarantined`. `transform_errors` still holds one
row per discarded row, with `destination = 'discard'` and the same reason.
There is **no** routing event in this run: `discard` has no edge to divert
onto, so query 2 returns nothing against `audit_discard.db`.

### 5. The declared type catches the value on its own row (`settings_declared.yaml`)

```bash
sqlite3 -header examples/batch_error_routing/runs/audit_declared.db "
SELECT json_extract(row_data_json, '$.order_id') AS order_id, destination, error_details_json
FROM transform_errors
WHERE run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1);"
```

```text
order_id|destination|error_details_json
A-008|failed_rows|{"actual":"str","expected":"int","field":"amount_cents","message":"Operation target 'amount_cents' computed a value of type str, but this node's schema declares it int. Declare the target 'any' (or the scalar type it computes) to store it.","reason":"type_mismatch"}
```

`row['amount'] * 100` does not fail on a string: it repeats the string 100
times. The node's schema declares `amount_cents: int`, and a transform's
declared output type is checked against every value it emits (ADR-050), so
that row is routed to `failed_rows` by itself with a reason naming the field
and both type names. Without the declaration the repeated string would have
reached `batch_totals` as `amount_cents`, and the batch holding it would have
failed there, taking two good orders with it, as in `settings.yaml`. Running
query 1 against `audit_declared.db` shows 11 `batch_consumed`, 4 `success`
and 1 `(failure, on_error_routed, failed_rows)`, and every batch is
`completed`.

### Every token is terminal

In each database, this returns `0`:

```bash
sqlite3 examples/batch_error_routing/runs/audit.db "
SELECT COUNT(*) FROM tokens t
WHERE t.run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)
  AND NOT EXISTS (SELECT 1 FROM token_outcomes o
                  WHERE o.token_id = t.token_id AND o.completed = 1);"
```

## Configuration Notes

- `on_error` is required on every aggregation and takes a sink name or
  `discard`. The settings loader rejects an aggregation without it, so a
  failed batch always has a declared destination.
- The trigger is `count: 3`. Twelve orders make exactly four batches here,
  so no batch waits for the end-of-source flush in `settings.yaml`. In
  `settings_declared.yaml` eleven orders reach the aggregation and the last
  two are flushed at end of source.
- `batch_stats` treats a missing amount (`null`) and a non-finite float
  differently: it skips that row and reports it in `skipped_missing` or
  `skipped_non_finite`. Only a value of the wrong **type** fails the batch.

## See Also

- `examples/batch_aggregation`: the same `batch_stats` aggregation with clean
  data
- `examples/error_routing`: `on_error` on per-row transforms, where one bad
  row fails alone
- `examples/scope_collector`: a collector, where a failure is settled by the
  group's `policy` instead of `on_error`

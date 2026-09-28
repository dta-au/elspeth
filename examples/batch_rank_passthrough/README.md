# Batch Rank Passthrough — Rank a Batch, Keep the Same Tokens

Demonstrates aggregation `output_mode: passthrough` with `batch_rank`, the one
shipped batch plugin that emits exactly one row per buffered row. Each prompt's
candidate responses are ranked by their judge score within the batch, and
then **the same tokens** continue to a gate that sends the top two per prompt
to a shortlist.

Twelve candidate responses to three prompts (`candidates.jsonl`) arrive four
per prompt. The aggregation flushes every 4 rows, so each batch is one
prompt's candidates. `batch_rank` adds four fields to every row:

| Field | Meaning |
|-------|---------|
| `score_rank` | 1 = highest `judge_score` in the batch (`order: descending`); equal scores share a rank and the next is skipped (`ties: competition`: 1, 2, 2, 4). `null` when the row has no rankable score |
| `score_percentile` | 100 × (ranked rows in the batch with a strictly lower score) / `score_ranked_count`. From 0 up to, never reaching, 100. `null` for an unranked row |
| `score_ranked_count` | how many rows of the batch have a rankable score |
| `score_batch_size` | how many rows the batch holds |

A shortlist gate then sends every candidate with `score_rank <= 2` to
`shortlist.csv` and the rest to `remaining.csv`.

Two configs ship here. Run one at a time.

| Config | Aggregation mode | Output | Tokens in the run | Exit |
|--------|------------------|--------|-------------------|------|
| `settings.yaml` | `passthrough` | 7 shortlisted, 5 remaining | 12 (the source's own) | 0 (`COMPLETED`) |
| `settings_transform.yaml` | `transform` | the same 7 and 5 rows | 24 (12 source tokens consumed, 12 new children) | 0 (`COMPLETED`) |

## What This Shows

```
source (12 candidates) ─(candidates)─> [rank_per_prompt: batch_rank, count=4, passthrough]
    ─(ranked)─> [shortlist_gate: rank <= 2] ─┬─ true  ─> shortlist
                                             └─ false ─> remaining
```

**The same tokens flow on.** Under `passthrough`, each buffered token waits in
the batch (recorded `buffered`), then continues with the row `batch_rank`
returned for it. Nothing is consumed and no token is created, so the token a
sink writes is the token the source created for that row. Under `transform`
the aggregation ends every source token (`transient` / `batch_consumed`) and
each row the flush emits becomes a new child token. The output files are the
same in both modes; the lineage is not. See [Audit Queries](#audit-queries).

**Passthrough needs a plugin that keeps every row.** `passthrough` carries only a
plugin whose flush emits exactly one row per buffered row, in order, and whose
class declares `flush_emits_one_row_per_buffered_row = True`. `batch_rank` does:
it never drops, adds or reorders a row. Every other shipped batch plugin
reduces the batch (`batch_stats`, `report_assemble`, …), replicates rows
(`batch_replicate`) or skips rows (`batch_outlier_annotator`), and
`elspeth validate` refuses it under `passthrough`. See
[A reducer under passthrough is refused](#a-reducer-under-passthrough-is-refused).

**A row without a score is kept, unranked.** `P-102-b` has `"judge_score": null`
(the judge returned no score). It is not dropped: it passes through with
`score_rank` and `score_percentile` empty, and it is not counted in
`score_ranked_count`, which is 3 for prompt `P-102`. The gate tests for a
missing rank before comparing it, so `P-102-b` goes to `remaining`. A missing
field or a non-finite float (`NaN`, `inf`) is treated the same way. A score
that is present but not a number (text, or `true`/`false`) fails the whole
batch, and the aggregation's `on_error` applies to every row of it. This
source declares `judge_score: float?`, so it rejects a text score itself; see
[`batch_error_routing`](../batch_error_routing/README.md) for a whole batch
failing.

**Ties share a rank.** Prompt `P-103` has two candidates scoring `8.0`. Under
the default `ties: competition` the ranks are 1, 2, 2, 4, so three `P-103`
candidates reach the shortlist. `ties: dense` would give 1, 2, 2, 3.

**Ranks restart in every batch.** The trigger decides what "the batch" is.
Here the feed is four rows per prompt, in order, so `count: 4` makes each
batch one prompt. There is no group-by trigger: a feed that is not grouped
that way needs a different trigger or an upstream step that groups it.

## Running

From the repository root:

```bash
elspeth run --settings examples/batch_rank_passthrough/settings.yaml --execute
elspeth run --settings examples/batch_rank_passthrough/settings_transform.yaml --execute
```

Expected summary line, the same for both configs (the timing varies, and so
does the order of the two routed sinks, `shortlist:7` and `remaining:5`):

```
✓ Run COMPLETED: 12 rows processed | ✓12 succeeded | ✗0 failed | ⚠0 quarantined | →12 routed (shortlist:7, remaining:5)
```

Both exit 0.

## Output

`examples/batch_rank_passthrough/output/shortlist.csv` (`settings.yaml`; the
transform config writes the same rows to `shortlist_transform.csv`):

```
candidate_id,judge_score,model,prompt_id,score_batch_size,score_percentile,score_rank,score_ranked_count
P-101-a,8.5,model-a,P-101,4,50,2,4
P-101-c,9,model-c,P-101,4,75,1,4
P-102-a,7,model-a,P-102,4,33.333333333333336,2,3
P-102-c,8,model-c,P-102,4,66.66666666666667,1,3
P-103-b,9.5,model-b,P-103,4,75,1,4
P-103-c,8,model-c,P-103,4,25,2,4
P-103-d,8,model-d,P-103,4,25,2,4
```

`examples/batch_rank_passthrough/output/remaining.csv` (`remaining_transform.csv`
for the transform config):

```
candidate_id,judge_score,model,prompt_id,score_batch_size,score_percentile,score_rank,score_ranked_count
P-101-b,6,model-b,P-101,4,0,4,4
P-101-d,7.5,model-d,P-101,4,25,3,4
P-102-b,,model-b,P-102,4,,,3
P-102-d,5.5,model-d,P-102,4,0,3,3
P-103-a,6.5,model-a,P-103,4,0,4,4
```

The CSV sink writes the columns in name order, and writes a float with no
fractional part without its `.0` (`9`, not `9.0`).

## Audit Queries

Each query reads the latest run in its database. Run the config first.

### Token outcomes: passthrough keeps the tokens, transform replaces them

```bash
sqlite3 -header -column examples/batch_rank_passthrough/runs/audit.db "
SELECT outcome, path, completed, sink_name, COUNT(*) AS tokens
FROM token_outcomes
WHERE run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)
GROUP BY outcome, path, completed, sink_name
ORDER BY outcome, path, sink_name;"
```

`settings.yaml` (`runs/audit.db`):

```
outcome  path         completed  sink_name  tokens
-------  -----------  ---------  ---------  ------
         buffered     0                     12
success  gate_routed  1          remaining  5
success  gate_routed  1          shortlist  7
```

The 12 `buffered` records are not terminal: each is a token waiting in its
batch. The same 12 tokens then reach a sink. Run the query against
`runs/audit_transform.db` for `settings_transform.yaml`:

```
outcome    path            completed  sink_name  tokens
---------  --------------  ---------  ---------  ------
           buffered        0                     12
success    gate_routed     1          remaining  5
success    gate_routed     1          shortlist  7
transient  batch_consumed  1                     12
```

Here the 12 source tokens end at the aggregation (`batch_consumed`), and the 12
tokens that reach a sink are new ones.

### Token count and parent links

```bash
sqlite3 -header -column examples/batch_rank_passthrough/runs/audit.db "
SELECT
  (SELECT COUNT(*) FROM tokens
   WHERE run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)) AS tokens,
  (SELECT COUNT(*) FROM token_parents
   WHERE run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)) AS parent_links;"
```

| Database | `tokens` | `parent_links` |
|----------|----------|----------------|
| `runs/audit.db` (passthrough) | 12 | 0 |
| `runs/audit_transform.db` (transform) | 24 | 12 |

### Is the token at the sink the token that was buffered?

For every token that reached a sink: its source row, whether that same token
was the one buffered into the aggregation, and how many parents it has.

```bash
sqlite3 -header -column examples/batch_rank_passthrough/runs/audit.db "
SELECT r.source_row_index AS source_row, o.sink_name AS sink,
  (SELECT COUNT(*) FROM token_outcomes b
   WHERE b.token_id = o.token_id AND b.path = 'buffered') AS buffered_as_this_token,
  (SELECT COUNT(*) FROM token_parents p WHERE p.token_id = o.token_id) AS parent_links
FROM token_outcomes o
JOIN tokens t ON t.token_id = o.token_id
JOIN rows r ON r.row_id = t.row_id
WHERE o.completed = 1 AND o.sink_name IS NOT NULL
  AND o.run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1)
ORDER BY r.source_row_index, o.sink_name;"
```

Passthrough (`runs/audit.db`): every sink token is the buffered token of its own
source row, with no parent.

```
source_row  sink       buffered_as_this_token  parent_links
----------  ---------  ----------------------  ------------
0           shortlist  1                       0
1           remaining  1                       0
2           shortlist  1                       0
3           remaining  1                       0
4           shortlist  1                       0
5           remaining  1                       0
6           shortlist  1                       0
7           remaining  1                       0
8           remaining  1                       0
9           shortlist  1                       0
10          shortlist  1                       0
11          shortlist  1                       0
```

Transform (`runs/audit_transform.db`): no sink token was buffered, each has one
parent, and all four children of a batch belong to that batch's first source
row (0, 4 or 8), because each one's parent is the batch's first buffered token.

```
source_row  sink       buffered_as_this_token  parent_links
----------  ---------  ----------------------  ------------
0           remaining  0                       1
0           remaining  0                       1
0           shortlist  0                       1
0           shortlist  0                       1
4           remaining  0                       1
4           remaining  0                       1
4           shortlist  0                       1
4           shortlist  0                       1
8           remaining  0                       1
8           shortlist  0                       1
8           shortlist  0                       1
8           shortlist  0                       1
```

This is the practical difference: under `passthrough`, "which source row is
this shortlisted candidate?" is answered by the token itself. Under
`transform` the answer is only in the row data (`candidate_id`), because the
token's lineage points at the batch.

## A reducer under passthrough is refused

Swap `batch_rank` for `batch_stats` in a copy of `settings.yaml` and validate
it. The copy lives outside the example so nothing here changes:

```bash
sed 's/plugin: batch_rank/plugin: batch_stats/' \
  examples/batch_rank_passthrough/settings.yaml > /tmp/batch_stats_passthrough.yaml
elspeth validate --settings /tmp/batch_stats_passthrough.yaml
```

It exits 1, before any row is read or any audit database is written:

```
╭─────────────────────── ❌ Plugin Configuration Error ────────────────────────╮
│ Aggregation 'rank_per_prompt' uses transform 'batch_stats' with output_mode: │
│ passthrough, but 'batch_stats' does not declare that its flush emits exactly │
│ one row per buffered row, which is what passthrough carries. Use             │
│ output_mode: transform, so the rows its flush emits become new downstream    │
│ tokens. A custom batch plugin whose flush does emit one row per buffered row │
│ declares flush_emits_one_row_per_buffered_row = True on its class.           │
│ Hint: Check plugin options match the plugin's requirements.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The refusal is decided from the plugin class before its options are read,
which is why the leftover `output_prefix`, `order` and `ties` options are not
what it reports. The web composer refuses the same node with the same remedy.

## Key Configuration

```yaml
aggregations:
- name: rank_per_prompt
  plugin: batch_rank
  input: candidates
  on_success: ranked
  on_error: discard
  trigger:
    count: 4
  output_mode: passthrough
  options:
    schema:
      mode: observed
    value_field: judge_score
    output_prefix: score
    order: descending
    ties: competition

gates:
- name: shortlist_gate
  input: ranked
  condition: row['score_rank'] is not None and row['score_rank'] <= 2
  routes:
    'true': shortlist
    'false': remaining
```

- `value_field` names the numeric column to rank by. The source declares it
  `judge_score: float?`, so a `null` score is admitted and reaches the batch.
- `output_prefix` names the four added fields (`score_rank`, …). The default is
  `rank`.
- The gate condition must test `score_rank` for `None` first. An unranked row
  carries `None`, and `None <= 2` is not a comparison the expression evaluator
  can make. With the plain condition `row['score_rank'] <= 2`, the run stops
  at `P-102-b` with exit 4 and `ExpressionEvaluationError: type error in
  comparison (LtE): cannot compare NoneType and int` (measured), because this
  gate has no `on_error`.

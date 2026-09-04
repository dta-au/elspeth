# Audit Sweep: Token Outcome Gaps

Current as of 2026-08-31.

Run these read-only checks after a run reaches a terminal run status. The checks
target the ADR-019 two-axis token outcome model.

## Preconditions

- The run is no longer actively processing.
- End-of-source aggregation and coalesce flushes have run.
- Use `completed`, not the retired `is_terminal` column.
- Join `nodes` with both `node_id` and `run_id`.

## 1. Tokens Missing A Decided Or Abandoned Outcome

```sql
SELECT t.token_id, t.row_id
FROM tokens t
LEFT JOIN token_outcomes decided
  ON decided.run_id = t.run_id
 AND decided.token_id = t.token_id
 AND decided.completed = 1
LEFT JOIN token_outcomes abandoned
  ON abandoned.run_id = t.run_id
 AND abandoned.token_id = t.token_id
 AND abandoned.completed = 0
 AND abandoned.outcome IS NULL
 AND abandoned.path = 'abandoned'
WHERE t.run_id = :run_id
  AND decided.token_id IS NULL
  AND abandoned.token_id IS NULL;
```

This should be empty after a run is terminal. A completed run decides every
token; a non-resumable failed or interrupted run may instead explain an
undecided token with `(NULL, abandoned)`.

## 2. Tokens Without Exactly One Final Fate

```sql
SELECT t.run_id, t.token_id, COUNT(o.outcome_id) AS final_fate_count
FROM tokens t
LEFT JOIN token_outcomes o
  ON o.run_id = t.run_id
 AND o.token_id = t.token_id
 AND (
   o.completed = 1
   OR (o.completed = 0 AND o.outcome IS NULL AND o.path = 'abandoned')
 )
WHERE t.run_id = :run_id
GROUP BY t.run_id, t.token_id
HAVING COUNT(o.outcome_id) <> 1;
```

A token's final fate is either one completed outcome or one `(NULL, abandoned)`
record. This query reports missing fates, duplicate abandonment, and the
contradictory decided-plus-abandoned shape. The partial unique index prevents
duplicate completed outcomes under normal writes; this sweep still counts them
if the index was bypassed or the database is corrupt.

## 3. Illegal Non-Terminal Rows

```sql
SELECT outcome_id, token_id, outcome, path, completed
FROM token_outcomes
WHERE run_id = :run_id
  AND completed = 0
  AND NOT (outcome IS NULL AND path IN ('buffered', 'abandoned'));
```

## 4. Illegal Completed Rows

```sql
SELECT outcome_id, token_id, outcome, path, completed
FROM token_outcomes
WHERE run_id = :run_id
  AND completed = 1
  AND (
    outcome IS NULL
    OR (outcome, path) NOT IN (
      ('success', 'default_flow'),
      ('success', 'gate_routed'),
      ('success', 'gate_discarded'),
      ('failure', 'gate_error_discarded'),
      ('failure', 'on_error_routed'),
      ('success', 'filter_dropped'),
      ('success', 'coalesced'),
      ('failure', 'unrouted'),
      ('failure', 'quarantined_at_source'),
      ('transient', 'sink_fallback_to_failsink'),
      ('failure', 'sink_discarded'),
      ('transient', 'fork_parent'),
      ('transient', 'expand_parent'),
      ('transient', 'batch_consumed')
    )
  );
```

## 5. Discriminator Constraint Violations

```sql
SELECT outcome_id, token_id, outcome, path
FROM token_outcomes
WHERE run_id = :run_id
  AND (
    (path = 'default_flow' AND (sink_name IS NULL OR batch_id IS NOT NULL OR error_hash IS NOT NULL))
    OR (path = 'gate_routed' AND (sink_name IS NULL OR batch_id IS NOT NULL OR error_hash IS NOT NULL))
    OR (path = 'gate_discarded' AND (sink_name IS NOT NULL OR batch_id IS NOT NULL OR error_hash IS NOT NULL))
    OR (path = 'gate_error_discarded' AND (sink_name IS NOT NULL OR batch_id IS NOT NULL OR error_hash IS NULL))
    OR (path = 'on_error_routed' AND (sink_name IS NULL OR batch_id IS NOT NULL OR error_hash IS NULL))
    OR (path = 'filter_dropped' AND (sink_name IS NOT NULL OR batch_id IS NOT NULL OR error_hash IS NOT NULL))
    OR (path = 'coalesced' AND (batch_id IS NOT NULL OR error_hash IS NOT NULL))
    OR (path = 'unrouted' AND (sink_name IS NOT NULL OR batch_id IS NOT NULL OR error_hash IS NULL))
    OR (path = 'quarantined_at_source' AND (batch_id IS NOT NULL OR error_hash IS NULL))
    OR (path = 'sink_fallback_to_failsink' AND (sink_name IS NULL OR batch_id IS NOT NULL OR error_hash IS NULL))
    OR (path = 'sink_discarded' AND (sink_name <> '__discard__' OR sink_name IS NULL OR batch_id IS NOT NULL OR error_hash IS NULL))
    OR (path = 'fork_parent' AND (sink_name IS NOT NULL OR batch_id IS NOT NULL OR error_hash IS NOT NULL))
    OR (path = 'expand_parent' AND (sink_name IS NOT NULL OR batch_id IS NOT NULL OR error_hash IS NOT NULL))
    OR (path = 'batch_consumed' AND (sink_name IS NOT NULL OR batch_id IS NULL OR error_hash IS NOT NULL))
    OR (path = 'buffered' AND (sink_name IS NOT NULL OR batch_id IS NULL OR error_hash IS NOT NULL))
    OR (path = 'abandoned' AND (sink_name IS NOT NULL OR batch_id IS NOT NULL OR error_hash IS NOT NULL))
  );
```

## 6. Sink Success Without Completed Sink State

```sql
SELECT o.token_id, o.path, o.sink_name
FROM token_outcomes o
WHERE o.run_id = :run_id
  AND o.completed = 1
  AND o.outcome = 'success'
  AND o.sink_name IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM node_states ns
    JOIN nodes n
      ON n.run_id = ns.run_id
     AND n.node_id = ns.node_id
    WHERE ns.run_id = o.run_id
      AND ns.token_id = o.token_id
      AND n.node_type = 'sink'
      AND ns.status = 'completed'
  );
```

## 7. Completed Sink State Without Success Outcome

```sql
SELECT DISTINCT ns.token_id
FROM node_states ns
JOIN nodes n
  ON n.run_id = ns.run_id
 AND n.node_id = ns.node_id
LEFT JOIN token_outcomes o
  ON o.run_id = ns.run_id
 AND o.token_id = ns.token_id
 AND o.completed = 1
 AND o.outcome = 'success'
 AND o.sink_name IS NOT NULL
WHERE ns.run_id = :run_id
  AND n.node_type = 'sink'
  AND ns.status = 'completed'
  AND o.token_id IS NULL;
```

## 8. Lineage Frames Missing Group Records

```sql
SELECT f.token_id, f.kind, f.group_id, f.member_key
FROM token_lineage_frames f
LEFT JOIN group_records g
  ON g.run_id = f.run_id
 AND g.group_id = f.group_id
 AND g.kind = f.kind
WHERE f.run_id = :run_id
  AND g.group_id IS NULL;
```

## What To Do With Results

1. Group failures by `(outcome, path)`.
2. Use [Outcome Path Map](01-outcome-path-map.md) to find the producer.
3. Reproduce the gap with the smallest pipeline or repository-level test.
4. Add a regression that fails the relevant sweep query.
5. Fix the producer path and re-run the sweep.

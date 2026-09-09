# join_refused — the admission fence, shown returning a negative

`multi_worker` and `multi_worker_showcase` show `elspeth join` **succeeding**.
This example shows it **refusing**, which is the arm that actually protects a
live run: a follower whose resolved settings do not hash to the leader's
`runs.config_hash` is refused atomically, with zero durable mutation, while a
correctly-matched follower admitted against the same run keeps working.

A control that is only ever seen returning a positive is not visibly a control.

## What this shows

Two arms run against **one live run, concurrently**:

| Arm | Settings | Outcome |
|-----|----------|---------|
| **1 — positive control** | `settings.yaml` (identical to the leader's) | admitted, shares work, exits 0 |
| **2 — the refusal** | `settings_mismatched.yaml` | `JoinRefusedError`, exits 1, registers nothing |

`settings_mismatched.yaml` is a **valid** pipeline. It differs from
`settings.yaml` by exactly one scalar:

```diff
-    temperature: 0.0
+    temperature: 0.1
```

That is the teaching point. Admission compares
`stable_hash(resolve_config(settings))` against `runs.config_hash`, so *any*
difference in resolved configuration is a different pipeline — a different
graph, different barrier keys, different downstream contracts. There is no
"close enough". The refusal says so:

```
Cannot join run <run_id>: resolved settings hash '7fdada99…' does not match
the run's config_hash '61c6fc50…'; a joiner must run the identical pipeline
```

## Pipeline shape

```
input.jsonl (1 row, items array of 80 texts)
  └─> [exploded] ─> json_explode ─> [llm_input] ─> llm_0 (ChaosLLM sentiment) ─> output/results.json
                                                                                 ─> output/quarantined.json
```

The same shape as `multi_worker`, sized down to 80 items. The pipeline itself is
incidental — it exists to keep a run alive and claimable while both joins are
attempted.

## Running

```bash
./examples/join_refused/run.sh
```

Self-verifying, ends **COMPLETED / exit 0**, takes ~25 s. The launcher starts its
own ChaosLLM on port 8199 with zero fault injection and tears it down.

## Why the assertion is on the REASON, not the exit code

Exit 1 from `elspeth join` is `JoinRefusedError` *generically*. The admission
path (`engine/orchestrator/join_admission.py::join_run`, design §B.1) has **four**
refusal arms, and all four exit 1:

1. filesystem preflight — the joining process cannot write the DB file, its
   directory, or an existing `-wal`/`-shm` sidecar;
2. the run is not `RUNNING`;
3. the joiner's config hash does not match `runs.config_hash`;
4. no live leader seat (`leader_heartbeat_expires_at < database_now`).

A launcher that checked only `exit == 1` would pass while demonstrating the
wrong thing — most easily arm 2, because a run that finished before the join was
attempted also refuses with exit 1. `run.sh` therefore greps the refusal for
`does not match the run's config_hash`, and separately asserts that the
positive-control follower completed real rows.

## Why `run_workers` is the zero-mutation probe

Admission is one `BEGIN IMMEDIATE` transaction: it verifies status, config hash
and seat liveness, then `INSERT`s the `run_workers` row and the
`worker_register` event, and commits. A refusal must leave nothing behind.

Worker identities are single-use — `departed`/`evicted` rows are never deleted
and never return to `active` — so `COUNT(*) FROM run_workers` only ever grows,
one row per *admitted* worker. That makes it a stable probe even while the
admitted follower is concurrently working and departing: the count is taken
immediately before and after the refused join, and must be unchanged.

## Output

```
ARM 2 (the refusal): joining with settings_mismatched.yaml...
  exit code: 1
  reason:
    Cannot join run …: resolved settings hash '7fdada99…' does not match the
    run's config_hash '61c6fc50…'; a joiner must run the identical pipeline
  run_workers rows: 2 before, 2 after

Per-worker attribution (scheduler_events grouped by from_lease_owner):
worker:…:a3920ac2…|leader|43
worker:…:15935769…|follower|37

✓ PASS: admission fence refused a mismatched joiner without disturbing the live run
```

## Pacing

`chaos_config.yaml` uses `base_ms: 400` — deliberately slower than
`multi_worker`'s profile. At the multi_worker latency these 80 items drain in
about 4 seconds, which leaves no window for two sequential join attempts. The
pacing lives in this example's own ChaosLLM config rather than in a larger input
so the fixture stays small and the reason for the window stays explicit.

If the launcher ever fails with "only 1 worker completed rows", the leader
drained before the positive control attached. Do not add sleeps — raise
`base_ms` or the item count in `input.jsonl`, exactly as `multi_worker`'s README
directs for the same class of race.

## Key concepts

- **Config-hash admission** — the joiner computes
  `stable_hash(resolve_config(settings))` itself and is refused unless it equals
  the leader's stored `runs.config_hash`. This is why the leader and every
  follower must pass the **same** settings file.
- **Atomic admission** — status check, hash check, seat-liveness check, insert
  and event all happen in one `BEGIN IMMEDIATE` transaction, so a refusal is
  durably invisible.
- **`ELSPETH_FINGERPRINT_KEY`** — sourced from `examples/chaosllm_env.sh`, which
  establishes ONE process-scoped key so the leader and both joiners fingerprint
  the fake `api_key` identically. Without a shared key the hashes would differ
  for an unrelated reason and the example would prove nothing.
- **ADR-030 §B.1** — cooperative follower attach; `elspeth join` is not a
  `resume()` variant and takes no `--execute` flag.

## See also

- `examples/multi_worker/` — the same pack shape with the admission arm
  succeeding, plus the per-worker attribution mechanism this example reuses.
- `examples/multi_worker_showcase/` — a 4-way swarm.

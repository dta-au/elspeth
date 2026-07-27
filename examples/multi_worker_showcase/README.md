# multi_worker_showcase

A self-verifying 4-worker swarm demonstrating `elspeth join` at scale. Ten
JSONL batches fan out into 200 durable child work items, and four cooperating
OS processes (1 epoch-fenced leader + 3 claim-only followers) divide them over
a single RUNNING run. The launcher fails unless at least two workers actually
process outcomes.

---

## What This Shows

ELSPETH 0.6.0 ADR-030 ("One-Host WAL Pack") lets independent OS processes share
a single in-flight run over one WAL SQLite audit DB. One process is the epoch-
fenced **leader** (ingests rows, owns barriers, writes sinks, finalises the run);
the others are **followers** (claim READY token work items from the queue, run
the transform, mark dispositions — never write sinks). All coordination state
lives in the DB; followers discover the live `run_id` by reading the `runs` table
and are admitted if they present an identical `config_hash` (same `settings.yaml`).

### Pipeline DAG

```
input.jsonl (10 batches × 20 texts)
  └─> json_explode ─> 200 READY child items ─> (llm: sentiment)
                                                ├─> output/results.json
                                                └─> output/quarantined.json
```

The fan-out is operationally important. Source rows are atomically claimed by
the leader as they are ingested, so many direct source rows do not form a
follower-visible READY backlog. `json_explode` creates new durable child work
items; those committed children are what the followers can claim.

---

## Running

```bash
./examples/multi_worker_showcase/run.sh           # leader + 3 followers (4-way)
WORKERS=1 ./examples/multi_worker_showcase/run.sh  # smaller swarm for quick dev
```

The script:
1. Generates a process-scoped audit fingerprint key when the operator has not
   already configured one, then starts ChaosLLM (`--workers 1`) on port 8199.
2. Backgrounds the **leader** with `elspeth run --settings ... --execute`.
3. Polls the audit DB (read-only) until the run is `RUNNING` with ≥1 claimed
   work item, then launches `WORKERS` **followers** with
   `elspeth join "$RUN_ID" --settings "$PIPELINE_CONFIG"` (no `--execute` —
   `join` executes unconditionally; `--execute` is only a flag on `elspeth run`).
4. Tails live `token_work_items` status counts every ~2s while the leader runs.
5. Reaps all PIDs, renders an ASCII stats card, and asserts that every process
   exited cleanly and at least two workers processed outcomes.

Expected output: exit 0, `✓ PASS`, a stats card showing four registered
workers, 200 completed outcomes (successful plus failed), rows/sec, and
per-worker attribution. An admitted-but-idle swarm now exits non-zero.

The fake inline token is required by the OpenAI-compatible client but is sent
only to `127.0.0.1`. No real OpenRouter credential or service is used.

---

## Join-Window Timing

The follower can only attach while the run is `RUNNING`. The script polls for
`RUNNING ∧ ≥1 leased item` before joining. Each `json_explode` batch creates 20
claimable children and ChaosLLM latency keeps the window wide enough for
followers to attach and share them. If timing or admission prevents sharing,
the final assertion reports the worker counts and exits non-zero.

---

## Exit-Code Semantics for `elspeth join`

| Code | Meaning |
|------|---------|
| `0`  | Clean departure — follower worked until the run ended normally |
| `1`  | JoinRefusedError — admission refused (config-hash mismatch, no live leader, or run not RUNNING) |
| `2`  | FollowerSeatDeadError — leader died mid-drain; run `elspeth resume <run_id>` |
| `3`  | SIGINT / RunWorkerEvictedError |
| `4`  | Framework / Tier-1 error |

*Note: exit 2 (FollowerSeatDeadError) is present in the live CLI but missing
from the 0.6.0 design spec's exit-code list — a spec erratum, intentionally
retained here.*

The `run.sh` cleanup trap kills any still-running followers and the ChaosLLM
server on exit. Any non-zero leader or follower exit fails the launcher.

---

## Output

- `output/results.json` — JSONL of successfully processed rows (sentiment analysis)
- `output/quarantined.json` — JSONL of rows that exhausted retries

Both files plus the audit DB are git-ignored (`examples/**/output/*` +
`examples/**/runs/*`).

---

## Key Concepts

- **ADR-030 One-Host WAL Pack** — epoch-fenced leader + N−1 claim-only followers
  sharing one WAL SQLite audit DB over a single run
- **`elspeth join`** — follower admission: verifies `config_hash` match,
  requires `status='running'` and live leader heartbeat
- **Durable fan-out** — `json_explode` turns leader-preclaimed source batches
  into follower-claimable READY child items
- **Self-verifying swarm** — the launcher requires clean child exits and at
  least two distinct processing owners

---

## CI / Dogfood Note

`multi_worker_showcase` is the heaviest multi-worker example (200 outcomes
across four processes).
Do not gate dogfood completion on this example. Use `examples/multi_worker/`
(leader + 1 follower, 120 rows) for bounded smoke testing.

# Cross-examination — Operations / SRE lens

Tree measured: `release/0.8.1` @ `a375d7f13` (2026-09-28). Other positions cite `6506f6a7f`. I re-read every
line I rely on below at `a375d7f13`. This file is read-only analysis; no code or plan files were changed.

Opening position: **B**. Final position: **B′, refined rather than reversed.** Details are in §5.

## 1. A finding no other position has: the ACA replica probe P1 breaks under 202+poll (every option)

The Azure Container Apps acceptance probe P1 is the instrument an operator uses to prove the session fence on a
live multi-replica deployment. It fires one freeform `POST /messages` body at two replicas at once, then scores
the result (`src/elspeth/web/_acceptance_common/replica_probes.py:625-648`, scoring `:294-317`). It requires:

| Assertion (`replica_probes.py`) | What happens under the async design |
|---|---|
| `:303-305` exactly one success and one fence refusal | Both replicas send the same body, so the same id, so both get `202`: one admits and the other replays. Neither returns an HTTP fence refusal. |
| `:312` fence epoch advanced by exactly one | Admission takes no SOL. The epoch moves only when a worker starts the job, which may be after the observation. |
| `:314` fence owner is the HTTP winner | The fence owner is whichever instance's worker claims the job, not the replica that answered the POST. |
| `:316` exactly 1 `message_ingress_receipts` row | The ingress row is written with the user row by the worker, so an immediate count reads 0. |

The observer runs raw SQL against `message_ingress_receipts` keyed on `client_request_id`
(`_azure_container_apps_acceptance/controller.py:58`, `:296-300`; `replica_probes.py:525,647`). The P1 ingress
assertion was added in `7001600fe` on 2026-09-28. `git log --reverse -S message_ingress_receipt_rows` over both
probe files returns only that commit. The plan does not cover any of this:
`grep -rn "fence_conflict\|replica_probe\|azure_container_apps_acceptance" T*.md contract.md` returns nothing. T13
touches only `azure_container_apps_observations.py` and its tests.

The repair already exists in the same file: `run_start_trial` polls until the durable publication appears
(`replica_probes.py:654-660`). P1 needs the same treatment. It should poll the job to a terminal state, then
assert that exactly one job row exists, that there was one worker start (one epoch advance), that the fence owner
equals the job's `claim_owner_instance_id`, and that exactly one ingress row exists. Its `mechanism` label changes
from `session_operation_fence` to "job-row dedupe + worker claim + fence". This work is the same under every
option, A to D. It belongs in T13 or T15, and it has to land before the first ACA deployment of the async build.
Otherwise the acceptance run goes red and nobody knows why.

It also settles the identity question from an operations angle (§3, leverage).

## 2. Points that change my position (concessions)

1. **Keep the claim owner on terminal rows** (db-architect, audit-integrity). Verified: the completed and failed
   arms force the claim triple to NULL (T03.md:82, :87; CHECK text :842, :845). `session_operation_fences` is keyed
   by `session_id` alone, and the next operation overwrites `owner_instance_id` (`models.py:325-338`). So once a
   row is settled `worker_lost`, nothing records which instance died. My original stuck-turn query cannot answer
   the first post-incident question. Amend T03 so that settlement nulls only `claim_token` and `claim_expires_at`,
   and keeps `claim_owner_instance_id` and `attempt`. The precedent is "Release never nulls forensic authority"
   (`models.py:321-322`). Add a closed `settled_by` column (`owner | reaper_lost | reaper_inactive_session |
   own_lapsed | unstarted`). This is the audit-integrity minimal fallback, and I take it as the ops floor.
   Only settlement keeps the owner. `release_claim` on a queued row still nulls the whole triple, so the queued
   arm's all-or-nothing `CLAIM_IS_NULL` / `CLAIM_IS_SET` (T03.md:837) is unchanged.
   Cost: the completed and failed CHECK arms change, and so do the F-m1 exact-shape tests in T03.
2. **A partial unique index on `(session_id) WHERE status IN ('queued','running')`** (db-architect) replaces T03's
   `one_running_per_session` (T03.md:862-869). Verified: D8 admission already refuses a second nonterminal id
   (contract.md:78). T04.md:3324 calls its own "older queued sibling" arm unreachable. The only producer of two
   nonterminal rows in one session is the deliberately artificial fixture `_clone_claimed_job` (T05.md:1200-1207).
   With the index, the operator's "what is in flight on this session" query returns at most one row by schema, and
   D8 becomes an invariant rather than a lock discipline. Costs, named honestly: the T05 clone proof must be
   reshaped (for example, two authorities racing to start one job), because the fixture insert now fails.
   T03's `test_at_most_one_running_job_per_session` (:592-599) changes. T04's dead sibling arm goes. The index
   **supersedes** my own request for `(session_id, status)`. I also **withdraw** my `(status, deadline_at)`
   request: at most one nonterminal row per session and the admission cap (≤ 10 000) already bound the
   nonterminal set, so the claimable index made partial on nonterminal statuses, as db-architect proposes, is
   enough for `list_expired_queued`.
3. **A transition-guard trigger on both dialects, plus a negative control** (leverage). I accept it, but for a
   reason that corrects leverage's premise; see §3.

## 3. Rebuttals, position by position

**systems-thinker (B).** I agree with the conclusion. I rebut "the invariant becomes structural under B": as the
design is specified, it does not. The `running` arm requires the whole claim triple to be set, including
`claim_expires_at` (T03.md:839). `renew_claim` renews only a queued claim (T04.md:2387, :3310), and the T05 start
CAS moves the row to `running` without touching `claim_expires_at` (T05.md:1006-1013 sets only status, the SOL
triple, `started_at` and `updated_at`). So once `claim_lease_seconds` have passed, a live running row carries an
**expired** `claim_expires_at`. The claimable index `(status, claim_expires_at,
created_at)` (T03.md:852-856) also covers running rows. A later "reclaim expired claims" CAS that leaves out
`status = 'queued'` would therefore match every live turn older than `claim_lease_seconds`. An authority with no
takeover method is still a Level-5 convention. The trigger that forbids `running → queued` and any change to
`claim_token` on a running row is what makes it structural. Your proposed Python negative test cannot catch a
method added later; a trigger negative control can.

**leverage (B′).** I accept the trigger, the writer-manifest pin that lets only T05 set `running`, and the
`status = 'queued'` WHERE clause. Two corrections. (a) "The running row has no lease column" is **false** against
T03.md:839 (see above). The trigger is load-bearing because the column exists. (b) Identity option (1), removing
`message_ingress_receipts` at the cut, breaks the ACA acceptance observer and P1 scoring (§1:
`controller.py:58`, `replica_probes.py:316,525,647`), and it removes the one immutable per-send acceptance record
an operator can query. Pick option (2): `operation_id` **is** `client_request_id`, and ingress stays as the
worker-written immutable record. The positive predicate at `_require_session_operation_context_on_connection`
(`service.py:970-1000`, which today checks only the fence) is plausible, but it belongs to audit, not to my
lens. Its operational side is small: under D12 a lingering SOL blocks fork and revert on that one session until
the lease expires, and the default lease is 30 s (`service.py:566`). That is bounded, but the stuck-turn runbook
should say so.

**solution-architect (B′).** I agree, including the one-identity recommendation and deleting the 409
`message_already_accepted` path at cutover. One addition to your proposed test "a queued compose job does not
block fork/revert". It is true at the table level, but the dependency also runs the other way. `claim_next`
discovery skips any session whose fence is live under any kind (F-M2, T04.md:198), so steady fork and revert
traffic on one session can starve a queued compose job until `deadline_expired`. An operator will see that as
"my message failed with deadline_expired". The runbook needs this row: `queued` + a live foreign-kind fence +
`deadline_expired` means fence contention, not a worker fault. T16 should cover it.

**tech-critic (B′).** I agree. Your "the event log may be pure ceremony" matches my view. On T05 (a head change
between admission and start), the ops need is only that whatever is decided gets recorded on the row, so a
diagnosis can see it. I take no position on refuse versus record.

**db-architect (B).** I accept the partial index and the retention of owner and attempt (§2). The SQLite WAL
figure (about 408 KB per size-changing update against a 393 KB `request_json`, about 2.4 MB per job worst case)
matters to operations for SQLite single-node deployments: checkpoint pressure scales with job churn. I support
T00 measuring the real p50/p99 `request_json` and `result_json` sizes before T03 fixes the bounds. I support
your T16 PostgreSQL archive-delete test (D7).

**python-architect (C).** The table-level reasoning is identical to mine. On the codec, operations has one real
stake: when an operator investigates a 409 "operation id bound to a different request", there should be one
definition of "same request" to reason from. Two copies can drift independently. I adopt codex's fallback rule
as the answer to C: share one neutral normaliser with a schema tag per facility, and fall back to C **only** if
a golden-vector test cannot prove the receipt hashes are byte-identical after the lift.

**audit-integrity (B).** I concede the gap (§2.1). I rebut the full `composer_async_operation_events` table on
retention grounds. The receipts pattern's no-delete trigger raises on DELETE while the parent session exists
(`models.py:1682-1690`). Copied to compose, and given your composite FK from events to the job, it makes every
settled compose row undeletable for the session's lifetime. Those rows carry the heaviest `result_json` in the
session DB. That is a retention ruling made by a trigger rather than by the owner. Event volume is not my
objection: about 5-6 events per job with renewals excluded is fine, even on SQLite's single writer. If the owner
wants the event table, the ruling should state that consequence. Otherwise, owner retention plus `settled_by`
covers the forensic need at far lower schema cost.

**codex.** The panel summary labels it "Forwarded". The file itself is a full position:
`position-codex.md` argues B with a domain-separated shared codec, two public wrappers, golden vectors, and C as
a fallback. I agree with all of it and have adopted its fallback rule above.

## 4. Unchanged from my opening position (still option-independent)

- Failure isolation comes from the shared 16+16 `run_sync_in_worker` pool (`async_workers.py:14,18`) and the
  per-session SOL, not from the table choice. T16 should measure fork and revert p99 under 64 active polls on
  PostgreSQL. The poll must stay a single PK select.
- Under A, `decide_and_soft_archive` (`coordination/repository.py:722-734`) sees compose rows and raises
  `AuditIntegrityError`, which breaks D7. Under B it needs no change.
- The epoch is **72**. The reset is the same stop-the-world recreate for every option.

## 5. Final position

**B′.** A separate `composer_async_operations` table, with:

- one neutral request normaliser with a schema tag per facility, receipt bytes pinned by golden vectors, and C as
  the fallback only if that pin fails;
- one client-minted id per send, where `operation_id` **is** `client_request_id` and `message_ingress_receipts`
  is kept, worker-written, and immutable (this is an owner ruling to take before T02);
- `claim_owner_instance_id` and `attempt` kept on terminal rows, plus a closed `settled_by`;
- a partial unique index on the session's nonterminal row;
- a transition-guard trigger with a negative control;
- the P1 acceptance probe re-scoped in T13/T15.

This refines my opening position rather than reversing it. The table decision stands. The changes are to T03
and T04 deliverables, plus a probe task that no position had named.

### Stuck-turn query (revised for the runbook, T15/T17)

```sql
SELECT j.status, j.kind, j.attempt, j.claim_owner_instance_id, j.settled_by, j.created_at, j.started_at,
       j.deadline_at, j.cancel_requested_at, j.failure_code,
       f.operation_kind, f.owner_instance_id, f.operation_epoch, f.lease_expires_at, f.released_at
FROM composer_async_operations j
LEFT JOIN session_operation_fences f ON f.session_id = j.session_id
WHERE j.session_id = :sid
ORDER BY j.created_at DESC LIMIT 5;
```

Rules to add to the runbook:

- `queued` with an expired claim: any instance may reclaim it.
- `queued` with a live foreign-kind fence: fence contention. It ends in `deadline_expired` if the contention
  persists.
- `running` with a live fence whose `owner_instance_id` equals the claim owner: the turn is in progress.
- `running` with a lapsed or released fence: the reaper settles it `worker_lost`.
- A terminal row with `settled_by` set: tells you who settled the row and which instance held it.
- A live COMPOSE fence left over after the row is terminal: D12, clears within the 30 s lease.

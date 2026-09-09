# Worker Affinity — Path-Scoped Placement for Multi-Process Orchestration (PROPOSAL)

**Status:** DRAFT proposal — captured 2026-08-22 from a maintainer design discussion.
NOT scheduled, NOT part of the unified-lineage campaign. Do not implement anything in
this document until it is promoted to a spec and planned. Sequencing: strictly after
the unified-lineage campaign lands (WS1–WS4 + integration at minimum; ideally after
WS5's resume satisfiability gate, which this design mirrors).

**Companion bug:** `elspeth-258bd49d81` (expand fan-out width is unfenced) — a hard
PRECONDITION for cross-class fan-out, not merely a companion: parking foreign-class
members (§4) leaves a wide expand's children resident in the journal as unclaimed
rows instead of being drained inline by the mint worker, which promotes the width
gap from theoretical to load-bearing. Affinity v1 depends on that fix.

**Review provenance:** rev 2, 2026-08-22 — cost model corrected after review
(claim-at-mint, `run_workers`, `claim_pending_sink` findings verified against
`scheduler/queue.py:175`, `schema.py:965`, `leases.py:291`).

---

## 1. Motivation

Two observations, same session:

1. The unified lineage model makes very wide/deep fan-out *expressible* (depth is
   capped at 5 bound layers fail-closed, config-overridable; width is data-dependent).
   A run that deliberately fans out large is not an abuse case — it is a distributed
   compute shape, *if* the engine can place work deliberately.
2. ELSPETH already has multi-process orchestration: work items are leased from the
   shared Landscape journal (`token_work_items.queue_key` / `lease_owner`, lease
   expiry, takeover). What it lacks is any way to say *which* worker may take *which*
   work.

The capability affinity unlocks is not parallelism (the lease queue already gives
that) — it is **heterogeneous workers**:

- a GPU-holding worker class for embedding/inference nodes;
- a credential-holding worker class for a locked-down sink (credentials never present
  in the other processes);
- a network-isolated worker class for the LLM provider path;
- data-locality placement for large-payload subtrees.

That is an operational-security story as much as a scale story, and it fits the
audit-first posture: placement becomes a declared, recorded fact of the run.

## 2. Core concept

**Worker classes + affinity bindings, routed by lineage-path prefix.**

- Workers declare one or more **classes** at launch (config/CLI), e.g.
  `elspeth worker --class gpu --class default`.
- Pipeline config declares **affinity** either per node or per bound region. A bound
  region binding means: every token whose `lineage_path` carries that region's frame
  (prefix match) inherits the placement — a whole fork branch or expand subtree runs
  on the named class. This is the payoff of the unified model: `lineage_path_json`
  already rides every `token_work_items` row (WS1), so subtree-scoped routing is a
  prefix predicate on data the flip persists anyway. The tri-field could never have
  expressed this.
- At enqueue, the engine stamps the resolved **affinity class** onto the work item
  (a real column — see Q2, closed). Every claim surface filters to classes the
  claiming worker holds.

**Cost model (corrected, rev 2 — this is NOT "just a lease-predicate change"):**
today's execution model is **claim-at-mint**: `enqueue_ready_claimed`
(`scheduler/queue.py:175`) persists children already leased to the minting worker
(`worker_id = lease_owner`, membership-fenced), and the minting process executes
them inline. Affinity therefore forces a second enqueue mode — **park unclaimed** —
for any mint whose resolved class the minting worker does not hold, and the local
execution loop (processor / token-manager / scheduler drain) must skip work it
minted but cannot run. The same applies to resume's inline execution path and to
the coalesce closer's release mint: the leader parks a foreign-class merged token's
continuation rather than running it inline (the barrier itself settles at the mint,
so this is a liveness stall covered by the quorum gate, not a barrier deadlock —
but the park site is mandatory all the same). This is an execution-model change,
priced accordingly.

Resolution precedence (proposed): node-level declaration beats region-level; innermost
region beats outer; absent → `default`.

## 3. Invariants (non-negotiable in any implementation)

1. **Settlement is never affinity-constrained.** Barrier intake, settlement
   propagation, escalation, and leader-drain work run on whatever worker holds
   leadership. If closer work could be pinned, a barrier deadlocks on its own policy.
   Affinity applies to *member execution only*.
2. **Class quorum before class-pinned dispatch (both start and resume).** Worker
   registration is per-run and post-start (`run_workers` rows are written as workers
   join), so a one-shot pre-start gate has nothing to check. The enforceable form is
   a **start-quorum semantic**: class-pinned work is not dispatched (and the run does
   not report itself live) until every declared affinity class has at least one
   active registered worker; the identical rule covers resume, since resuming
   workers re-register the same way. The WS5 lesson still applies: the quorum check
   sits on the enforced dispatch path, never an advisory one (`resume()` never calls
   advisory `can_resume`). Without it, lease expiry cannot save you: reclaim is
   restricted to the same class, so a dead class strands its subtree while the
   enclosing `require_all` roster waits forever.
3. **Affinity is placement, not authorization.** It *supports* credential isolation
   as deployment posture (the credential simply is not present in other processes),
   but nothing may treat "ran on class X" as a trust assertion. Trust-tier rules are
   unchanged.
4. **Placement is audited.** `lease_owner` already records who executed. Add the
   worker's class set to run accounting / worker registration so the audit trail can
   answer "was this token executed by the class its config demanded" — a derivable
   integrity check, same family as the frame-authenticated guards.
5. **No silent fallback.** Work never spills to another class because its class is
   busy or absent. Absent class = gate refusal (start/resume) or stall surfaced as a
   diagnosed condition — never quiet misplacement. (A *soft* preference tier could be
   added later; v1 is hard-only to keep the gate honest.)

## 4. What it needs from the engine (substrate audit, 2026-08-22)

Already present or landing in the campaign:

- Lease-based shared queue: `token_work_items.queue_key`, `lease_owner`, lease
  expiry/takeover (`contracts/scheduler.py`, `scheduler/leases.py`, `scheduler/queue.py`).
- **Worker registration, liveness, and fencing already exist**: `run_workers`
  (`schema.py:965`) carries per-run worker identity, heartbeat expiry, eviction CAS
  with forensics, role/status constraints; `run_coordination` holds the fencing
  token. (Rev 1 of this document wrongly called this "genuinely new".)
- `lineage_path_json` on every work item (WS1a Task 4/5; sole-truth after the WS1b flip).
- Group rosters + settlement seam (`group_records`, `group_losses`, settle-member —
  WS1a/WS3): what invariant 1 must not violate, and what makes stall diagnosis
  ("group G waiting on class X") expressible.

Genuinely new:

- **The park-unclaimed enqueue mode and inline-execution bypass** (§2 cost model) —
  the dominant cost: processor / token-manager / drain learn to mint work they do
  not run, at fork/expand mints, resume's inline path, and the coalesce release mint.
- Class metadata on `run_workers` + the start-quorum semantic (invariant 2).
- The `affinity_class` column on `token_work_items` (Q2, closed as column).
- Class filtering on **every claim surface** — at minimum `claim_ready`,
  `claim_ready_row`, and `claim_pending_sink` (`leases.py:95/:144/:291`). The sink
  redrive surface is not optional: unfiltered, it hands credential-class sink work
  to any worker, which defeats the headline use case exactly.
- Config surface (`workers:` / node `affinity:` / region binding spelling — open, §6).

## 5. Backpressure companion (required for wide fan-out as a *feature*)

Affinity makes placement deliberate; it does not make 10^6-token groups survivable.
That needs admission control: a max-in-flight ceiling per run (and possibly per
group) so a wide expansion streams through workers instead of materializing.

Noted for the record: the roster design almost supports **chunked/streamed minting**
already — settlement counts arrivals against `group_records.member_count`, so a
streamed mint is sound provided `member_count` is sealed exactly when minting closes
(open-until-sealed would need an explicit group state). That is the deepest change in
this neighborhood and is out of scope for v1 of affinity.

**Width interaction (rev 2):** the eager mint transaction is the same size with or
without affinity — what parking changes is *residency*. Today an inline mint drains
its children as it executes them; a foreign-class wide expand instead materializes
fully as parked journal rows awaiting another class (queue residency, lease-scan
pressure, resume workset size). Cross-class fan-out therefore makes
`elspeth-258bd49d81` load-bearing: the width ceiling is a v1 dependency (header
note), not a nice-to-have.

## 6. Open questions (decide at promotion time)

1. Declaration spelling: node `affinity:` key vs a `placement:` block; how a bound
   region names its class (on the opener? on the `scopes:`/`collectors:` entry?).
2. ~~Stamp mechanics~~ **CLOSED (rev 2): a real `token_work_items.affinity_class`
   column.** Encoding into `queue_key` would smuggle a structural fact into a
   string — the carry-the-fact-structurally doctrine forbids it.
3. ~~Worker registration~~ **CLOSED (rev 2): extend `run_workers`** (class metadata
   column(s)) and express the gate as the start-quorum semantic of invariant 2 —
   heartbeat/eviction/fencing machinery is already there.
4. Mid-run worker loss: quorum catches start/resume; a class dying mid-run leaves its
   subtree stalled with leases expiring. Minimum viable: surface as a named diagnosed
   condition ("group G blocked: no live worker for class X"). Later: operator
   `rebind`/drain verbs.
5. Interaction with checkpoint/takeover: leadership transfer must ignore affinity
   (invariant 1) — verify no takeover path re-dispatches member work through a
   non-filtered lease.
6. Does `default` class work require declaration, or is undeclared work leasable by
   every worker? (Leaning: leasable by every worker holding `default`; workers opt
   out of `default` explicitly.)

## 7. Non-goals (v1)

- Sibling cancellation, scheduling optimization, work stealing, load balancing.
- Any change to settlement policy semantics (`require_all`/`best_effort` untouched).
- The width ceiling itself (`elspeth-258bd49d81` — a v1 *dependency*, delivered
  separately, not part of this design).
- Soft/preference affinity (hard-only v1; see invariant 5).

## 8. First prototype (when promoted)

Two-class / two-worker run with one fork branch pinned to a class the minting worker
does not hold, the pinned branch routed to a sink. One topology exercises every new
mechanism: the park-unclaimed mint + inline-execution bypass (the mint worker cannot
run the pinned branch), the cross-class handoff via the filtered claim surfaces —
including `claim_pending_sink` on the pinned branch's sink redrive — and the
start-quorum gate (launch the class worker late and observe pinned dispatch held,
never misplaced). Extend to crash+resume of the class worker before calling the
mechanism proven.

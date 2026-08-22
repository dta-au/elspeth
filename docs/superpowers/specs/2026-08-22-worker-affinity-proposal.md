# Worker Affinity — Path-Scoped Placement for Multi-Process Orchestration (PROPOSAL)

**Status:** DRAFT proposal — captured 2026-08-22 from a maintainer design discussion.
NOT scheduled, NOT part of the unified-lineage campaign. Do not implement anything in
this document until it is promoted to a spec and planned. Sequencing: strictly after
the unified-lineage campaign lands (WS1–WS4 + integration at minimum; ideally after
WS5's resume satisfiability gate, which this design mirrors).

**Companion bug:** `elspeth-258bd49d81` (expand fan-out width is unfenced) — the
admission-control half of "deliberate wide fan-out". Independent of this proposal but
both must exist before wide fan-out is a supported operating mode.

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
- At enqueue, the engine stamps the resolved **affinity class** onto the work item.
  The lease query filters to classes the leasing worker holds. No new scheduler — a
  lease-predicate change.

Resolution precedence (proposed): node-level declaration beats region-level; innermost
region beats outer; absent → `default`.

## 3. Invariants (non-negotiable in any implementation)

1. **Settlement is never affinity-constrained.** Barrier intake, settlement
   propagation, escalation, and leader-drain work run on whatever worker holds
   leadership. If closer work could be pinned, a barrier deadlocks on its own policy.
   Affinity applies to *member execution only*.
2. **Affinity-satisfiability gate, both surfaces.** A run must refuse to start — and
   refuse to *resume* — if any declared affinity class has no live worker. Exact
   analog of the WS5 resume satisfiability gate, and the same lesson applies: the
   gate must sit on the enforced path, not an advisory one (`resume()` never calls
   advisory `can_resume`). Without this, lease expiry cannot save you: reclaim is
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
- `lineage_path_json` on every work item (WS1a Task 4/5; sole-truth after the WS1b flip).
- Group rosters + settlement seam (`group_records`, `group_losses`, settle-member —
  WS1a/WS3): what invariant 1 must not violate, and what makes stall diagnosis
  ("group G waiting on class X") expressible.

Genuinely new:

- Worker identity/registration: a durable worker-class roster with liveness
  (heartbeat), so the satisfiability gate has something real to check.
- The affinity stamp on work items (new column vs `queue_key` encoding — open, §6).
- The lease-predicate filter + gate wiring on both start and resume surfaces.
- Config surface (`workers:` / node `affinity:` / region binding spelling — open, §6).

## 5. Backpressure companion (required for wide fan-out as a *feature*)

Affinity makes placement deliberate; it does not make 10^6-token groups survivable.
That needs admission control: a max-in-flight ceiling per run (and possibly per
group) so a wide expansion streams through workers instead of materializing.

Noted for the record: the roster design almost supports **chunked/streamed minting**
already — settlement counts arrivals against `group_records.member_count`, so a
streamed mint is sound provided `member_count` is sealed exactly when minting closes
(open-until-sealed would need an explicit group state). That is the deepest change in
this neighborhood and is out of scope for v1 of affinity; the eager-transactional
mint concern is tracked as `elspeth-258bd49d81`.

## 6. Open questions (decide at promotion time)

1. Declaration spelling: node `affinity:` key vs a `placement:` block; how a bound
   region names its class (on the opener? on the `scopes:`/`collectors:` entry?).
2. Stamp mechanics: new `token_work_items.affinity_class` column vs encoding into
   `queue_key`. A column is honest schema; `queue_key` encoding avoids an epoch bump
   but smuggles semantics into a string — leaning column.
3. Worker registration: where the class roster + heartbeat lives (Landscape table vs
   process-external), and what "live" means for the gate (heartbeat window).
4. Mid-run worker loss: gate catches start/resume; a class dying mid-run leaves its
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
- The width ceiling itself (`elspeth-258bd49d81`, separate).
- Soft/preference affinity (hard-only v1; see invariant 5).

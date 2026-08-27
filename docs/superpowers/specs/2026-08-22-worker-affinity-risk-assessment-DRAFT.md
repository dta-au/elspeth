# Worker Affinity Proposal — Blast-Radius & Risk Assessment (DRAFT)

**Subject:** `docs/superpowers/specs/2026-08-22-worker-affinity-proposal.md` (6b0eb6afb)
**Assessed:** 2026-08-22, read-only, against the ASSUMED-LANDED unified-lineage substrate
(master plan + WS1a/WS1b/protocols/WS2–WS6 sibling plans, per the maintainer's binding
assumption) plus the already-landed WS1a/WS1b-Phase-A code on `feature/unified-lineage`.
**Companion bug:** `elspeth-258bd49d81` (expand fan-out width unfenced; blocked on WS3).

---

## 0. Headline finding

The proposal's five invariants are the right ones and the lineage-path-prefix routing
idea is genuinely enabled by the campaign (`lineage_path_json` on every
`token_work_items` row is real — WS1a Task 5, sole-truth after the WS1b flip). But the
spec's cost model — *"No new scheduler — a lease-predicate change"* — is materially
optimistic. The engine's execution model is **claim-at-mint**: `ingest_row_with_initial_claim`
and the `enqueue_ready_claimed*` family (`core/landscape/scheduler/queue.py:151/315/417`)
journal children *already leased to the minting worker*, which then executes them inline
through its in-memory scheduler. Affinity means a minting worker that does not hold the
child's class must **park the child unclaimed** (`enqueue_ready`) and its local drain
must *not* pick it up. That is an execution-model change in the processor/token-manager/
drain path, not a WHERE-clause change. The lease predicate is the easy 20%.

Overall risk tier: **medium-high** — implementable without touching settlement semantics,
but three design decisions (park-vs-inline, roster location/start-gate semantics, stamp
mechanics) dominate everything else and must be settled before any code.

---

## 1. Blast radius by invariant

Weighting: **T1** = Tier-1 replay/audit/settlement surface (defects corrupt audit truth
or strand runs); **T2** = engine correctness, recoverable; **T3** = config/UX/docs.

### Invariant: core routing concept (implied by §2, before any numbered invariant)

| Surface | Weight | Change |
|---|---|---|
| `engine/processor.py`, `engine/tokens.py`, `engine/work_items.py`, `engine/scheduler_drain.py` | **T1** | Mint-time affinity resolution (node beats region beats innermost-region precedence) + the park-vs-claim decision at every child mint: fork children, expand children, coalesce/row_union/collector **release mints** (see invariant 1 boundary below), sink emissions. The in-memory scheduler must refuse locally-parked foreign-class items. This is the deepest change in the proposal. |
| `core/landscape/scheduler/leases.py` — `claim_ready` (:95), `claim_ready_row` (:144), **and `claim_pending_sink` (:291)** | **T1** | Class filter on the claim predicate. The spec says "the lease query" (singular); there are **two claim surfaces**. `claim_pending_sink` is the sink-redrive path — and a locked-down credential-holding sink is a headline use case, so missing it is not a corner: it leaks exactly the work the feature exists to isolate. |
| `core/landscape/scheduler/queue.py` + `scheduler_repository.py` wrappers + `scheduler_work_codec.py` | T2 | Thread the resolved class through the enqueue verbs the same way WS1a threaded `lineage_path`. Mechanical, wide (WS1a Task 5 is the exact template, ~10 files). |
| `recover_expired_leases` (leases.py:422) | T2 | Likely **no change** (it resets rows to READY; the class filter re-applies at the next claim), but must be verified — including the `_legacy_unfenced` variant — as part of open question 5. |

### Invariant 1 — settlement never affinity-constrained

| Surface | Weight | Change |
|---|---|---|
| WS3 settle-member seam (`RowProcessor._settle_member_losses`), `group_losses` writers, `BarrierIntakeCoordinator.note_group_failed`, escalation intake, EOF flush loop | **T1** | **Deliberately zero code change** — the invariant is a *non-interference proof obligation*, enforced by tests: BLOCKED barrier/collector rows (addressed by `barrier_key`/`coalesce_name`/`row_union_name`/`collector_name`, adopted via `barrier_adopted_epoch`, not claimed via `claim_ready`) must be shown to bypass the class filter on every path, including WS4 collector holds (`barrier_key = "collector:<name>:<group_id>"`, WS4 Task 6) and the WS5 Task 4 EOF collector drain. |
| **The release-mint boundary** | **T1** | The sharpest edge of invariant 1: settlement runs on the leader, but the token a closer *releases* (merged/collected token continuing downstream) is **member execution of the enclosing region**. Today the leader continues it inline. Under affinity the leader must resolve the released token's class (post-strict-pop path prefix + node) and park it if foreign. If this seam is missed, either placement silently breaks (leader executes gpu-class work) or, if naively filtered, the barrier deadlocks on its own output — the exact failure invariant 1 exists to prevent, one hop downstream of where the spec points. |

### Invariant 2 — satisfiability gate, both surfaces

| Surface | Weight | Change |
|---|---|---|
| `core/checkpoint/recovery.py` | **T1** | New `check_affinity_satisfiability_resumable(...)` mirroring WS5's `check_group_satisfiability_resumable` two-surface pattern: consumed by advisory `RecoveryManager.can_resume` AND an enforcing arm in `ResumeCoordinator.resume()` before any mutation (the WS5 lesson the spec correctly cites). |
| **Fresh-start surface** | **T1** | Has **no existing analog** — and a structural problem the spec does not surface: worker registration is per-run and post-start. `run_workers` rows are minted as `worker:{run_id}:{uuid}` on `orchestrator.join_run` (cli.py:149), and followers **attach to an already-running run** (`cli.py:3155`, ADR-030 §B.1). At the moment `elspeth run` would evaluate the gate, only the starting worker is registered; the gpu-class worker joins *later, keyed by a run_id that does not exist yet*. So "refuse to start if any declared class has no live worker" is unimplementable against the per-run roster as it stands. Resolution routes through open question 3 (see §3). |
| Resume dispatch (`classify_resume_start`, `resume_incomplete_token`, resume filter) | **T1** | The pinned arm order (MERGED → innermost-EXPAND → innermost-FORK, WS1b Task 4) is orthogonal to affinity and should **not** change. But resume today *executes* incomplete tokens inline on the resuming worker (`process_token`/`process_existing_row`). The gate proves a class worker exists somewhere; enforcement requires the resuming worker to **park** foreign-class incomplete specs as READY journal rows instead of processing them — a second execution-model change on the WS5 surface, beyond the gate itself. |

### Invariant 3 — placement, not authorization

| Surface | Weight | Change |
|---|---|---|
| Trust-tier model, ADR set | T3 | Near-zero code. One ADR paragraph + ideally one elspeth-lints rule shape (no consumer of worker-class may feed a trust-tier decision; trust tier belongs to the ROW, set at the source). Cheap; do it anyway — the credential-isolation framing invites exactly this misuse. |

### Invariant 4 — audited placement

| Surface | Weight | Change |
|---|---|---|
| `core/landscape/schema.py` | **T1** | (a) `token_work_items.affinity_class` (if Q2 → column, which it should — see §3): epoch bump (assumed-substrate epoch 35 → 36), `database.py` required-columns, exact-column pin tests (`test_schema_epoch_and_required_columns.py` asserts exact sets), epoch-pin doc fan-out, dev-DB wipe. (b) Worker class set: either a `classes` column on `run_workers` or a `run_worker_classes` child table + registration path. |
| Portable export + manifest | **T1** | The three lineage tables enter the portable export at the WS1b flip; the affinity column and worker-class roster must be adjudicated into (or explicitly out of) the export the same way — `projection_sha256`/`audit_record_counts` manifest rotation with a dated ledger note. Note the oracle-freeze compare suite is deleted at campaign close, so affinity churns no frozen bytes (good), but the corpus manifest ledger discipline survives. |
| Derived integrity check | T2 | "Was this token executed by the class its config demanded" = join `lease_owner` → `run_workers`(classes) vs the stamped `affinity_class`. Same family as WS3's frame-authenticated guards; belongs beside the WS6 MCP group-forensics surfaces (Task 7/8) so the depth-3 reconstruction acceptance pattern extends to placement. |

### Invariant 5 — no silent fallback

| Surface | Weight | Change |
|---|---|---|
| Claim predicates | T1 | Free by construction once the filter is hard (no OR-default arm). The invariant's real cost is the **stall diagnosis**: a dead class produces a run that is neither failing nor progressing. Needs a named diagnosed condition ("group G blocked: no live worker for class X") surfaced from drain-idle diagnostics + WS6's disposition vocabulary/MCP surfaces. Without it the failure mode is an operator staring at a silent hang (see R6). |
| Config validation (WS2 surface) | T2/T3 | Build-time: unknown class names, node-vs-region precedence conflicts → `GraphValidationError` → **runtime-rejection-parity entries** (`config/cicd/runtime_rejection_parity.yaml` + composer Stage-1 mirror) for every new rejection site, plus composer three-pin (serialisation hash pins, redaction snapshot, `guidedDecoder.ts` exactRecord lists) if the config surface touches SourceSpec/NodeSpec/OutputSpec or `scopes:`/`collectors:` entries. |

---

## 2. Risk register

| # | Risk | Traces to | Likelihood | Severity | Notes |
|---|---|---|---|---|---|
| R1 | **Park-vs-inline execution model underestimated.** The "lease-predicate change" framing leads to an implementation that filters claims but leaves claim-at-mint inline execution intact — affinity then only applies to work that happens to be reclaimed after lease expiry, i.e., almost never. | Core concept; inv. 5 | **High** (it is the spec's own stated cost model) | **High** | The single most important correction at promotion time. WS1a Task 5's enqueue-verb threading is the template for the plumbing, but the park decision lives in processor/token-manager, not the repository. |
| R2 | **`claim_pending_sink` left unfiltered** → pending-sink redrive hands credential-class sink work to any worker. Defeats the headline security story while every test of the primary path stays green. | Inv. 3/5 | Medium | **High** | Spec says "the lease query" singular. Enumerate claim surfaces (`claim_ready`, `claim_pending_sink`) explicitly in the promoted spec; memory doctrine "enumerate the paths, don't reason from the mechanism in view" applies verbatim. |
| R3 | **Released/merged token continues inline on the settlement leader**, silently violating placement for the enclosing region — or, if crudely filtered, deadlocking the barrier on its own output. | Inv. 1 boundary | Medium-high | High | The member-execution/settlement boundary must be defined as "settlement ends at the release mint"; the released token re-enters normal (parked, class-filtered) dispatch. Needs its own test family: closer on leader-only class, downstream node on another class. |
| R4 | **Start-surface gate has nothing to check.** Per-run worker registration (workers join an existing run_id) makes "refuse to start if class has no live worker" unimplementable without a roster redesign; the tempting fallback — gate only at first affinity dispatch — is exactly the advisory-path mistake WS5 exists to prevent. | Inv. 2 + Q3 | High (structural) | High | See Q3 in §3. Options: pre-run/process-external class roster; or start-blocks-until-class-quorum semantics (registration barrier with timeout → refusal). Either way the enforced path must be the run entry, not a warning. |
| R5 | **Resume enforcement stops at the gate.** Gate passes (class worker live somewhere), then the resuming leader executes foreign-class incomplete tokens inline anyway. | Inv. 2/5 | High | Medium-high | Same fix shape as R1 applied to `resume_incomplete_token`: park foreign-class specs, don't process them. The satisfiability gate and the placement enforcement are different mechanisms; the spec conflates them slightly. |
| R6 | **Mid-run class death = silent permanent stall.** Leases expire → rows return READY → nobody eligible claims → `require_all` roster waits forever; EOF flush fixpoint never converges toward settlement (it is bounded by `derive_escalation_fixpoint_bound`, so it exits — the run just never completes). Reads as a hang, not a diagnosed condition. | Inv. 5 + Q4 | High (ops reality: workers die) | Medium (run integrity preserved; operator pain high) | Minimum-viable diagnosed condition is genuinely cheap on the assumed substrate: group_records + rosters + stamped class + `run_workers` liveness answer "group G waiting on class X" with one query. Ship it in v1, not "later". |
| R7 | **Stamp/config divergence at resume.** `affinity_class` is stamped durable at enqueue; config is re-read at resume. An edited config (or changed precedence resolution) makes stamp ≠ re-derivation. Which wins is undefined; the invariant-4 integrity check will fire on legitimate resumes or miss real misplacement. | Inv. 4 | Medium | Medium | Decide at promotion: stamp is authoritative for already-journaled rows (audit doctrine: audit captures run config); re-derivation applies only to new mints. Config-hash mismatch at resume should already refuse upstream — verify, don't assume. |
| R8 | **Affinity amplifies elspeth-258bd49d81.** Today a wide expand streams through the minting worker's in-memory loop; children are journal rows but execution is local and payloads flow through. Under affinity, a wide expand bound to a foreign class **materializes entirely as parked READY rows** (full `row_payload_json` each) before any member executes — the eager-mint transaction blowup plus a journal-resident copy of the whole fan-out. Also: the WS5 gate's per-member three-limb query shape is O(members); an affinity gate (and any stall diagnoser) must not copy that shape or a 10^5-member group makes the gate itself the outage. | §5 backpressure note; 258bd49d81 | Medium (theoretical width today, but affinity is *the* feature that invites deliberate wide fan-out) | High | The spec is right that admission control is a required companion — strengthen: cross-class wide fan-out should be considered **unsupported** until the width ceiling lands, not merely "survivability not guaranteed". Sequence: 258bd49d81 (post-WS3) *before* affinity v1 ships, or gate width at the opener when the group's class ≠ minting worker's class. |
| R9 | **Mechanical pin churn missed** — epoch fan-out, exact-column asserts, `comparable_fields`, wire-shape AST gates, composer three-pin, serialisation hash pins. One careless miss reds the whole tree for every sibling. | Inv. 4 + Q1/Q2 | High | Low-medium | Fully mitigated by the existing §S2 checklist discipline; budget for it rather than discovering it. |
| R10 | **`queue_key` encoding chosen for the stamp** → semantics smuggled into a string that is *also* the blocked-item address (`_queue_key_for_blocked_item`), parsed at every consumer. Violates the standing "stop parsing, carry the fact structurally" doctrine; collides with row_union/collector addressing; and `queue_key` is not even in today's claim predicate, so the encoding buys nothing. | Q2 | Low (spec already leans column) | High if taken | Close Q2 as **column** at promotion; treat encoding as rejected, not open. |
| R11 | **Placement-as-authorization creep** — a future change skips a credential/trust check because "only class X runs this". | Inv. 3 | Low | High | One ADR + lint shape now is cheap insurance; the deployment-model discriminator memory applies. |
| R12 | **Takeover/leadership transfer re-dispatch** routes member work through an unfiltered path (barrier adoption replay, journal restore, `recover_expired_leases_legacy_unfenced`). | Q5 / inv. 1 | Low-medium | Medium | Bounded verification job: enumerate every path that flips a row to READY or re-executes buffered members; assert class filter applies to member work and does NOT apply to settlement adoption. Multi-worker corpus scenario (`multi-worker-lease-reclaim-late-completion`) is the extension point. |

---

## 3. The spec's six open questions — what each answer swings

1. **Declaration spelling (node `affinity:` vs `placement:` block; region naming site).**
   Swings the WS2-surface blast radius: composer three-pin, `guidedDecoder.ts`
   exactRecord lists, serialisation hash pins, canonical-hash corpus. The bigger swing
   is *where region bindings live*: attaching affinity to `GroupBinding` couples the
   placement system into a Tier-1 type consumed by WS3's settle-member walk; a
   **parallel affinity registry** keyed the same way (built beside
   `GroupBindingRegistry` in WS2's builder) keeps settlement types untouched and
   honors invariant 1 structurally. Recommend the parallel registry regardless of
   spelling.

2. **Stamp mechanics (column vs `queue_key` encoding).** Column: epoch bump + export
   adjudication + pin churn (R9) — bounded, honest, one-time. Encoding: R10, plus a
   false economy — `queue_key` is a blocked-item *address*, not a claim predicate
   today, so encoding would require adding queue_key parsing to the claim path anyway.
   **This is not really open; close it as column.**

3. **Worker registration/roster location.** The highest-leverage open question. A
   Landscape table reusing `run_workers` inherits heartbeat/eviction/fencing machinery
   that already exists and is battle-tested (heartbeat_expires_at, eviction CAS,
   membership fences — ADR-030) — but `run_workers` is per-run and post-join, which is
   what breaks the start gate (R4). The realistic shapes: (a) extend `run_workers`
   with classes + a **start-quorum semantic** (run start registers declared classes as
   awaited; start blocks with timeout → hard refusal; followers join and satisfy);
   (b) a process-external roster (new liveness infra, new trust surface, duplicated
   heartbeat semantics — expensive and off-posture). "Live" for the gate should be
   the existing heartbeat window, not a new clock. Choice (a) keeps one liveness
   authority; its cost is defining start-time blocking semantics honestly in the CLI.

4. **Mid-run worker loss.** Swings R6 only in *when*, not *whether*: the diagnosed
   condition is cheap on this substrate and should be pulled into v1. The "later:
   operator rebind/drain verbs" carry real audit weight when they come — rebind
   mutates stamped placement on durable rows, so it must be a recorded, fenced
   operation (same family as the eviction CAS), never an UPDATE.

5. **Checkpoint/takeover interaction.** The verification list is longer than the
   spec's one line: barrier adoption replay, `restore_from_journal` buffer restores
   (WS4 Task 7), both `recover_expired_leases` variants, pending-sink redrive, and
   the resume inline-execution path (R5). Each is bounded; the risk is enumeration,
   not depth. Answering "takeover ignores affinity" (per invariant 1) for settlement
   while keeping member work filtered requires the settlement/member boundary of R3
   to be defined first.

6. **Default-class semantics.** The leaning (undeclared work leasable by every worker
   holding `default`; workers opt out explicitly) is the only answer that keeps every
   existing single-process run, test, and example zero-config — the alternative
   forces declarations across the entire corpus (enormous T3 churn, no safety gain).
   Take the leaning; note that a worker opting out of `default` while classes go
   undeclared anywhere in the graph is a satisfiability-gate case, not a special case.

---

## 4. Substrate-audit errata (what §4 of the proposal got wrong or missed)

1. **"Worker identity/registration: genuinely new" — half wrong.** `run_workers`
   (schema.py:965) already provides durable per-run worker registration with
   heartbeat liveness (`heartbeat_expires_at`), leader/follower role, eviction CAS,
   forensic pid/hostname, and membership fence clauses used by the claim verbs. What
   is genuinely new is the **class set** and any **pre-start roster** (R4/Q3). The
   gate has "something real to check" today — but only after workers join a run that
   already exists, which is precisely the gap that matters.

2. **"The lease query filters… no new scheduler — a lease-predicate change" —
   understated twice.** (a) Claim-at-mint inline execution (R1): the shared queue is
   how work *survives* and *fails over*, not how it is normally *dispatched*; normal
   dispatch is local and in-memory, so affinity changes the mint path, not just the
   claim path. (b) Two claim verbs, not one (R2: `claim_pending_sink`).

3. **`queue_key` miscast.** The proposal cites `token_work_items.queue_key` as part
   of the lease-queue mechanism; in the code it is a blocked-item address
   (row_union/collector holds), absent from the `claim_ready` predicate
   (leases.py:104-121: run_id + status=READY + available_at, ordered by
   ingest_sequence). Harmless in §2, but it feeds the Q2 encoding temptation (R10).

4. **"Reclaim is restricted to the same class" (invariant 2's rationale) —
   mechanism wrong, conclusion right.** Reclaim resets rows to READY; the class
   restriction re-applies at the next claim. The stranding conclusion holds.

5. **Missed: resume executes inline.** The proposal mirrors WS5's gate (correctly,
   including the enforced-path lesson) but does not note that resume *placement*
   needs the parking change too (R5). The gate alone leaves the resuming worker
   executing everything it reconstructs.

6. **Missed: the release-mint boundary** (R3). Invariant 1 draws the line at
   "member execution only" without defining where settlement ends; on this substrate
   the precise line is the closer's release mint (fresh EXPAND group for collector
   M-outputs per the decision canon; merged-token continuation for coalesce).

7. **Streamed-minting note — verified accurate.** `group_records.member_count` is
   written in the opener's mint transaction (sealed at mint close; WS1a Tasks 6–7),
   `member_count=0` is legal and the WS5 gate treats empty groups as vacuously
   satisfiable, and settlement counts arrivals against the sealed count — so the
   proposal's claim that chunked minting is *almost* supported but needs an explicit
   open-until-sealed group state is exactly right, and correctly out of scope.

8. **Accurate elsewhere:** `lineage_path_json` on every work item, the prefix-match
   payoff over the tri-field, the rosters/settlement seam citations, and the WS5
   `can_resume`-is-advisory lesson are all consistent with the plan set as ratified.

---

## 5. Verdict

**Risk tier: medium-high.** Nothing here touches settlement *semantics* (the campaign's
irreversible core); the danger is concentrated in the execution-model change the spec
underprices and in gate semantics that per-run registration cannot currently support.
All failure modes found are buildable-around at design time; none require re-opening
campaign rulings.

**Highest-leverage design decisions (settle before any code):**
1. **The park-vs-inline dispatch model** — where mint-time class resolution happens,
   how a parked foreign-class child bypasses the local in-memory scheduler, and the
   settlement/member boundary at the release mint (R1 + R3). This decision *is* the
   feature; everything else is plumbing around it.
2. **Roster location + start-gate semantics** (Q3/R4) — recommend extending
   `run_workers` with classes plus an explicit start-quorum/refusal semantic, keeping
   one liveness authority.
3. **Stamp as an honest column + exhaustive claim-surface enumeration** (Q2/R2/R10)
   — close the encoding option, list `claim_ready` *and* `claim_pending_sink` in the
   promoted spec, and put the invariant-4 integrity check beside the WS6 forensics
   surfaces.

**Prototype first at promotion time:** a two-class, two-worker run — fork with one
branch node-pinned to class B, minting worker holding only A — driving: park at mint,
cross-worker claim, cross-worker barrier arrival, closer settlement on the leader,
released token parked back to B. That one scenario exercises R1, R2 (add a B-class
sink + pending-sink redrive), R3, and the diagnosed-condition path (kill the B worker
mid-run). Second prototype: the satisfiability gate against a synthetic 10^4-member
group to fix the query shape before it inherits WS5's per-member loop (R8).

**Sequencing note:** hold the spec's own line — after WS5 lands, and treat
`elspeth-258bd49d81`'s width ceiling as a *precondition* for advertising cross-class
wide fan-out, not a parallel nicety (R8).

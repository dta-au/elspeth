# Identity and workflow governance

Pluggable SSO, identity substrate, approval, review, shared library, per-person quotas and compartment marking.

One GitHub issue points here. This folder is the detail: the programme's scope as it stood in
the filigree tracker on 2026-09-23, captured verbatim before those rows were closed.

## Why this is a folder and not 17 issues

GitHub has no `milestone` / `phase` / `step` issue type. Operator ruling 2026-09-23 (John):
*"for the epics, a single ticket pointing to a folder with the details"*. So the container rows
below were closed in filigree and their content lives here. The tracker rows are preserved
verbatim in `tracker-rows.json` alongside this file — nothing was summarised away.

## Documents of record

- `docs/specs/2026-09-02-pluggable-sso-design.md` — the design of record
- `docs/plans/2026-09-19-identity-workflow-finalization.md` — finalization plan
- `docs/plans/2026-09-13-kubernetes-and-identity/` — the Kubernetes-and-identity planning set

## Status

**Re-measured 2026-09-23: the programme is essentially delivered — 9 of 12 deliverables done.**
The three that remain are read-surface and labelling work, not enforcement. Full evidence:
`docs/reviews/2026-09-23-identity-sso-completion.md`.

| Deliverable | State |
|---|---|
| Pluggable SSO, identity substrate | Done |
| Send for approval, send for review | Done |
| Shared library | Done |
| Per-person quotas — tokens/day and storage bytes | Done |
| Compartment marking | Done |
| Admin interface | Done |
| Operator cutover runbook and compatibility record | Done — `docs/runbooks/identity-workflow-cutover.md` |
| Approver audit view | **Partial** — the route is registered and tested, no interface reads it |
| Composer completion-bar affordances | **Partial** — reachable, but see the placement question below |
| `Save for review` → `Share inspect link` rename | **Not done** — the old label is still in the served bundle |

### One thing to understand before reading "done"

**`workflow_governance` defaults to `"off"`** (`src/elspeth/web/config.py`). With it off, the run
admission gate admits runs with no approval — that is deliberate and tested, not a gap. Turning
it on is refused unless a `compartment_id` is set and registration is not open
(`src/elspeth/web/readiness.py`), because one person can hold many local identities under open
registration and every author-is-not-approver rule would be defeatable. So the machinery is
built and the enforcement is real; a deployment that has not turned it on is not governed.

### A question this raises, not an answer

Send-for-approval and send-for-review are reached through the Pipeline → Checks sub-tab
(`ApprovalReadinessRow` → `AuditReadinessPanel` → `ChecksView`). A pending approval is a
blocking state, and the standing placement ruling for this project is that a blocking state and
its fix affordance belong at the top level rather than in a sub-tab. Whether that ruling governs
this surface is a decision, not something to infer.

### Why the previous status was wrong, and what it said

This section previously recorded, from **2026-09-13**: Phase 4 governance backend 1/9, Phase 5
frontend 0/3, cutover pending, and "approval, review, library, quotas, compartment marking and
the admin UI are NOT built". That was accurate when written. It was overtaken by
`44cf55a65` (2026-09-20, 218 files, +21,344 lines), which landed Phase 4, Phase 5 and the cutover
runbook in one commit, and it is retained here rather than deleted so that anyone who meets those
figures elsewhere can see they were re-measured rather than quietly dropped.

Two rows in `tracker-rows.json` — per-person quotas and the cutover runbook — were closed as
`skipped` on 2026-09-22 by the migration to GitHub Issues, not because they were abandoned.
**Both are in fact done.** That status is an artefact of how the row was closed; read the table
above, not the row.

This is product governance — approvals and quotas for the people using ELSPETH. It is not the internal signing/tier-model ceremony that the 2026-09-23 ruling kept out of the GitHub migration.

## A note on completeness

Three of the rows below — Phase 3 (lane A'), Phase 5 (lane B') and Phase 6 — carried a
title and nothing else in the tracker: they were pure grouping containers with no
description or notes. Their headings appear with no body for that reason, not because
anything was dropped. Every other row's description and notes are reproduced in full, and
`tracker-rows.json` holds all 17 rows exactly as the tracker returned them.

## A note on the `elspeth-…` identifiers

The identifiers below are rows from **filigree**, the internal tracker ELSPETH used
before moving to GitHub Issues. They are opaque local ids: they name nothing outside that
tracker, and the tracker is being retired.

They are kept here deliberately, because this folder exists to preserve the provenance of
work whose tracker rows were closed — the id is the link between a closed row and the
content that replaced it. They should **not** appear in GitHub issue bodies, where they
would read as a dangling reference to a system a reader cannot see;
`docs/github-issues/check_issues.py` blocks them there for that reason.

## Scope

Closed container rows: `elspeth-07cd19ba73` (milestone) and 16 children.

### 1. Phase 1 — Contracts and the single schema epoch pass

*was `elspeth-18cc7bdf72` (phase, P1, filed 2026-09-02)*

Prepared schema and contract source is currently Sessions epoch 59 and Landscape epoch 42. The old 49/50 and 36/37 baseline and VM in-place rebuild instruction are superseded. The remaining operator task is one archive/export, recreate, re-admit and compatibility-record window after integrated verification; see docs/plans/2026-09-19-identity-workflow-finalization.md and the I11 handoff. Keep this phase pending until its open children are reconciled; prepared tables alone are not delivered workflow behavior.

### 2. Identity cutover runbook, compatibility record and cohort re-admission (single recreate path)

*was `elspeth-5ef01c6ad1` (step, P1, filed 2026-09-02)*

Spec §Two epochs, one window. Pinned sites: CHANGELOG.md; tests/unit/website/test_release_site_contract.py; docs/guides/sharing-pipelines.md + test_release_version_surfaces.py; docs/runbooks/staging-session-db-recreation.md + policy test; web/_aws_ecs_acceptance/receipt_contracts.py + test_receipt_contracts.py + test_cleanup_control_service.py; aws-ecs-deployment.md compat-record example (session_epoch 50, landscape_epoch 37, rollback_permitted:false). State the mid-cutover invalidation of all live SSO sessions in the operator notice.

### 3. Phase 3 — Identity frontend (lane A')

*was `elspeth-f576d114a9` (phase, P1, filed 2026-09-02)*

### 4. Phase 4 — Workflow governance backend (lane B)

*was `elspeth-08baa95f4d` (phase, P1, filed 2026-09-02)*

Parallel with phase 2 once phase 1 lands. Built now on John's ruling; tweak on the fly. All authority mutations audited before response.

### 5. Send for approval (blocking): approval records bound to the binding tuple, approver inbox query, execute gate (409 with distinct error_type, never bare 403), invalidation on edit, separation of duties (author≠approver), quorum per D11

*was `elspeth-c6fd5476d6` (step, P1, filed 2026-09-02)*

Seam: web/execution/routes.py execute_pipeline after verify_session_ownership and before service.execute; readiness panel gets a distinct approval row (authorization fact, visually separate from validation rows). Any new state_id invalidates the outstanding request (content-addressed like ADR-022 D4). R2 refusal: approved binding != compiled binding => refuse to run.

[rev2.8 CORRECTIONS — the two sentences this description used to carry were both reversed by later rulings:]
- APPROVER ELIGIBILITY IS ROLE-BASED (rev2.2), not 'an active manager edge to the author'. Any identity holding an active approver role in this container who is not the author may decide; the author's active approver edge only supplies the DEFAULT SUGGESTION in the picker. That is what gives the lead's own work an approver and gives leave cover without touching the tree.
- THE APPROVER'S READ IS NOT via the existing shareable_reviews transport (D27). That transport is a 30-day unrevocable bearer capability whose resolve route writes no audit row and never reads the caller identity it accepts — a capability, not an authorization. Build a per-request predicate instead: caller holds an active approver (or reviewer) role, caller != requested_by_identity_id, and a live approvals/review_requests row exists for (session_id, state_id). Serve the same frozen projection, mint NO token. Route-layer, no DDL. The read writes an audit_access_log row under a new writer_principal value, which lands in the phase-1 epoch (elspeth-93e7d5ff6a), not here.
- Concurrent decide: conditional write guarded on WHERE decision IS NULL (NOT decided_at IS NULL — superseded/revoked set decision without stamping decided_at, so the timestamp guard would let a superseded request be overwritten to approved against a stale binding). One named error_type carrying current state. approval_decisions insert + approvals update + R4 audit write in ONE transaction.
- Rejection requires a non-blank note (route-layer).

### 6. Send for review (non-blocking): review_requests + review_attestations, reviewer sign-off/changes-requested, captured in the session audit trail; rename existing 'Save for review' → 'Share inspect link'

*was `elspeth-11db5d33ab` (step, P1, filed 2026-09-02)*

Existing shareable_reviews.resolve_token records NO reviewer identity or verdict (reviewer finding). [rev2.8: the title used to say 'two-person'. The spec reserves that phrase for something that REFUSES, and attestation is a ledger — nothing refuses on it. A UI must never say 'two-person rule satisfied' over an unenforced count.] Two independent attestations on the same payload_digest are recorded; never rendered as a boolean gate. The reviewer's read uses the same per-request predicate as the approver's (D27), NOT the bearer-token transport. Requests live in the new review_requests table (D26) so the mailbox Inbox has a row to read; an attestation on the same (session_id, state_id) closes the request. changes_requested requires a non-blank note.

### 7. Shared library + personal lists: publish (frozen, content-addressed), curator accept/reject/deprecate/recall, deployment-wide read surface (first shared-read path; must NOT weaken verify_session_ownership), fork-from-library keeps provenance

*was `elspeth-0585d93372` (step, P1, filed 2026-09-02)*

Curator = identity_roles.role=curator (D9). Personal list = existing per-identity session list (HeaderSessionSwitcher) — no rework. Recall semantics: personal forks keep forked_from provenance and are flagged, never deleted. Separation of duties: curator cannot accept their own publication.

### 8. Per-person quotas (tokens/day R14 + storage bytes R13): quota_policies, accounting (token ledger; SUM(blobs.size_bytes) per identity), enforcement at execute/composer turn and at ALL FOUR byte-admitting sites, audit quota_exceeded with dimension, admin set/revoke

*was `elspeth-7c4b65bed3` (step, P1, filed 2026-09-02)*

Spec D15, D18, D24, D31, R13, R14. [rev2.8: the old 'OPEN D11 / decide with John' first sentence is DELETED — all three questions were ruled: quota is PER PERSON with a container CEILING row (identity_id NULL); accounting unavailable REFUSES, fail-closed.]

TOKENS (R14): SUM over token_usage_ledger for the identity in the current UTC DAY, at execute and at composer turn start. Landscape get_llm_usage_report is NOT the accounting source — the ledger is; calls stores token counts inside the response payload blob where they are not queryable, which is why calls gains the four nullable token columns and runs write a ledger row at finalisation.
STORAGE (R13): SUM(blobs.size_bytes) joined through sessions.identity_id over live rows. ENFORCE AT ALL FOUR BYTE-ADMITTING SITES, not just upload — (1) multipart upload, (2) inline upload/inline custody, (3) run-output finalize, (4) copy_blobs_for_fork. Naming only 'upload' left an identity at its cap able to fork its own session repeatedly, unrefused. Site 4 refuses BEFORE the copy loop starts so a refused fork leaves no half-populated child; the deliberate missing_bytes == 0 idempotent-replay path stays EXEMPT. Blobs in archived sessions count; pending/error rows count.
EXACTNESS (D24): eventually consistent, NOT exact. The existing blob lock is keyed on session_id alone, so two sessions of one identity do not serialise — the earlier 'two concurrent uploads cannot both pass' claim was false on Postgres. Do NOT add an identity-scoped lock: held across the fork copy loop it would serialise every other upload by that identity for the duration of a fork.
POLICY ROW (D31): written on EVERY path that makes an identity active — activate, local registration under open registration, pre-provision, the D20 seed and CLI, and the D21 cutover re-admission. Local exemption binds to R11's predicate (local AND open registration), never to local alone.
Both dimensions: admin set/revoke is one route; quota_exceeded carries dimension, cap, ceiling, usage.

### 9. Compartment marking: compartment_id setting stamped into public YAML metadata, shareable snapshot, library rows, auth_events metadata, signed exports; Tier-1 ingress event on user-pasted composition text

*was `elspeth-e754286e17` (step, P1, filed 2026-09-02)*

Spec rev2.2 §Terminology "What the system can and cannot promise" (all three second-round reviewers): permissions never federate (enforced), but content carried by a person legitimately in two compartments cannot be prevented — YAML export, downloaded outputs, library re-authoring. The honest control is MARKING + RECORDING. (1) WebSettings.compartment_id (operator-set string) stamped into: the public YAML metadata block (composer/yaml_generator.py), the shareable snapshot blob, every library_entries row, every auth_events.metadata_json, every signed Landscape export record. (2) Ingress: when a composition state is created from user-pasted text (the composer knows the turn was a user message), write a Tier-1 event carrying sha256(text) and any foreign compartment marking found in it. This is recording, which the composer invariants permit — it is NOT authoring and must not route around the planner. (3) Egress is already recorded (export_yaml completion event). Do not claim prevention anywhere in UI or docs.

### 10. Approver audit view: runs, approvals, attestations, and auth events for identities the caller has an active approver edge to (relationships × run_attributions × auth_events)

*was `elspeth-fb8ab2f111` (step, P1, filed 2026-09-02)*

Scoped read, not admin. Traversal bounded (cycles permitted in the org chart). [rev2.8: the 'name the auth_events export/retention gap as a follow-up ticket' clause is DELETED — sibling step elspeth-5b01e59986 (P1) already owns it, and this project does not file tickets for work a sibling step holds.]

Raised to P1 (rev2.2, systems review): the audit event types are written before any surface reads them; write-side completeness without a read side is how audit trails stop being read. Sequence in step with the event types, not after. Note the type list grew at rev2.8 (library_accepted, library_rejected, library_deprecated) — the view must render them.

**Notes.** Raised to P1 (rev2.2, systems review): 18 audit event types are written before any surface reads them; write-side completeness without a read side is how audit trails stop being read. Sequence in step with the event types, not after.

### 11. Workflow-governance test suite: fire + mutation test per refusal (R2, R7, R8, R9, R11, R13, R14), R2 through the real compiled binding tuple, full mailbox round trip, both quota dimensions, the separation-rule violations

*was `elspeth-97fa031b35` (step, P1, filed 2026-09-03)*

Spec §Testing → Workflow governance [rev2.8, D30]. Before phase 4 closes, not after: the sprint ships enforcement (R2 refuses a run, R13/R14 refuse spend) and §Testing named ZERO tests for approvals, quota, mailbox, library or attestations.

- Per refusal that ACTUALLY REFUSES (R2, R7, R8, R9, R11, R13, R14): a fire test proving the refusal happens, AND a mutation-derivation test proving the guard derives from its authority rather than from a coincidence.
- R2 driven through an ACTUAL COMPILED binding tuple, not a hand-built fixture. A gate tested against a fixture proves the fixture.
- Full round trip: request with a note, decide with a note, requester sees decision + note, badge clears. Plus the LOSING half of the concurrent-decide guard (two eligible approvers, one named error_type).
- Both quota dimensions: a storage refusal at EACH of R13's four byte-admitting sites, a token refusal, each asserting quota_exceeded carries dimension + cap + ceiling + usage. UTC-midnight rollover case with the clock named explicitly.
- Separation rules as violations: author=approver, curator=publisher, reviewer=author, and R8 in both grant orders including the revoked-workload-role case that must NOT refuse.
- A SEEDED PRE-EXISTING CYCLE in identity_relationships — R7 guards the insert and says nothing about data that predates it.
- The suite runs against a CLOSED local deployment (registration_mode not 'open'), or R11 means it is testing enforcement that was never on.

NOT tested, and why (do not add these): R10 has no reachable path — no service-credential mechanism ships this sprint. Attestation has no refusal to mutate; it is a ledger by design. 'Flex teams' is a property of two live deployments, not of code.

### 12. Phase 5 — Workflow governance frontend (lane B')

*was `elspeth-4bb1f5ecaf` (phase, P1, filed 2026-09-02)*

### 13. Composer completion bar: Send for approval / Send for review / Publish to library as three distinct affordances; approval row in the readiness panel; 409 pending-approval state legible before Run

*was `elspeth-5e1f2b4603` (step, P1, filed 2026-09-02)*

Reviewer tension 5: approval and review are different state machines and must never share one affordance. Blocking must be legible before the user tries to run, not discovered as an error.

### 14. Mailbox: inbox (approvals awaiting my decision + review requests to me, opening the frozen read-only inspect view with request note and decide/attest + note) and sent folder (my requests, decision, decider note, decision_seen_at), nav badge from one summary endpoint; curator queue

*was `elspeth-1c6401a960` (step, P1, filed 2026-09-02)*

Reuse the shared-inspect surface (composition + YAML + readiness snapshot, unmodifiable). Decision is an audit event, not a session mutation.

### 15. Phase 6 — integration and operator cutover handoff

*was `elspeth-3a228dd37b` (phase, P1, filed 2026-09-02)*

### 16. Operator cutover: recreate stores, re-admit cohort, record compatibility and fire judge bundle

*was `elspeth-b03f0aa218` (step, P1, filed 2026-09-02)*

Spec §Rollout 6. Operator-only actions: fire the judge bundle, countersign the compat record.

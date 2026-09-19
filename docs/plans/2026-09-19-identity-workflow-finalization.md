# Identity workflow finalization — current execution map

Date: 2026-09-19. Target: `release/0.8.1`, developed from
`8f4384936aac64e34e8c617a635f092c261ba559` in an isolated worktree.
Tracker: `elspeth-07cd19ba73` and its identity-sprint children.

This document replaces the **execution order and source baselines** for
Workstream I in the [September 13 master plan](2026-09-13-kubernetes-and-identity/2026-09-13-kubernetes-and-identity-master-plan.md).
The I-task files remain detailed implementation references, but their exact
line numbers, epoch numerals, and unimplemented-feature claims must be checked
against the current tree before use. The [SSO and identity spec](../specs/2026-09-02-pluggable-sso-design.md)
defines product behavior. Kubernetes Workstream K is independent delivery.

## Measured starting point

- The branch starts at Sessions schema epoch **59**
  (`src/elspeth/web/sessions/models.py`) and Landscape epoch **42**
  (`src/elspeth/core/landscape/schema.py`). The old I0 instruction to bump
  Sessions 56 to 57 and keep Landscape at 40 is obsolete. The live SQLAlchemy
  metadata has the approvals, review, library, quota, token-ledger, and
  audit-access tables. `token_usage_ledger.prompt_tokens` and
  `completion_tokens` are nullable; no new I0 schema cut is needed.
- Token-ledger settlement, per-identity and container UTC-day aggregation,
  and chargeable admission shipped after the September 13 plan in
  `e3bd9562a` and `e1a389211`. This must be verified against I1's full
  acceptance list before any I1 ticket is closed. The
  [quota repair record](../analysis/2026-09-15-quota-enforcement-repair.md)
  explicitly leaves identity-wide storage admission to I2.
- `WebSettings.workflow_governance` and its R11 readiness refusal are absent.
  Approval, review, and library authorities and workflow routes are absent.
  The admin and mailbox/library frontend components are absent. Their tables
  reserve the shape; they are not delivered behavior.
- The Filigree milestone marks Phase 2 auth core completed. Its other phases
  and open steps need reconciliation with the above code before status changes.
  The Phase 6 title and cutover ticket still mention live checks or an ECS/VM
  split superseded by the operator's no-live-testing and recreate rulings.

## Product rules to carry into implementation

- Preserve the composer's provider and tutorial invariants in `AGENTS.md`.
- A requester cannot approve their own state. The approver's read is an
  identity-authorized, audited read of the exact frozen state; it never mints
  or reuses a shareable bearer token. A decision must be guarded against a
  concurrent decision in the same transaction as its decision and audit rows.
- Every workflow mutation records its `auth_events` row before responding.
  Read authorization and signed export format changes need their own exact
  tests; no release claim is based on a prepared column alone.
- **Operator rulings, 2026-09-19:** any active non-author approver may inspect
  and decide an open request; the addressed approver sorts first in the inbox.
  A later rejection for the same state retires earlier approvals so none can
  still admit a run. Approval supersession emits an audit event, and signed
  Landscape exports carry `compartment_id`. The September 13 I3/I6/I7/I9/I10/I11
  task files contain contrary defaults; reconcile their predicates, tests,
  audit expectations and cutover prose before applying their code excerpts.
- A reviewer attestation requires an open review request for the exact state
  and reviewer; it remains a ledger entry, not a run gate. Compartment ingress
  records chat-pasted text when it creates a composition state, as well as
  explicit YAML paste/import and library fork. These are further operator
  rulings from 2026-09-19.
- The one-time cutover export preserves the administrator-entered `username`
  and `organisation_id` alongside provider, subject and identity ID. The CSV
  stays with the protected database archive so re-admission can restore both
  values (operator ruling 2026-09-20).
- Keep the release code and the deployment cutover distinct. The code may
  prepare a new schema and runbook; the operator controls database recreation,
  countersigning, key-held judge signing, and any live deployment.

## Execution order

1. **Reconcile the substrate and tracker.** Pin the current schema/contract
   metadata and the implemented quota behaviors. Retire I0's obsolete
   instruction, narrow I1 to any measured residual, and update Filigree titles,
   descriptions, and comments without claiming unfinished storage or workflow
   behavior complete. Confirm the 0.8.1 changelog target against the branch.
2. **I8 — governance switch.** Add the `workflow_governance` setting and R11
   readiness refusal, plus a closed local test fixture. Keep enforcement on
   when readiness refuses a misconfiguration.
3. **I2 — identity-wide storage quota.** Account across the identity's live
   blob rows, and enforce at all four byte-admitting sites. Keep the
   eventually-consistent contract and explicit unknown-accounting refusal.
   Reconcile the existing per-session limits and quota error messages.
4. **I3 — approvals and R2.** Implement request, decision, withdrawal,
   supersession, role-based inbox/inspection eligibility, exact compiled
   binding and run-start refusal. A later rejection must retire earlier
   approved rows for that state before the run gate can read them; audit the
   rejection, retirement and supersession outcomes.
5. **I4 and I5 — reviews and library.** Implement request-bound reviewer
   attestations without a run gate, plus frozen library publication,
   curation, browsing and provenance-preserving fork. Share the audited
   request-scoped inspect predicate with approvals.
6. **I6 and I7 — compartment and scoped reads.** Mark the five specified
   outputs, record YAML and state-creating chat ingress, implement workflow
   inspection, the approver audit
   view and delegated curator administration. Version signed export
   derivation if required by its closed contract; do not silently omit it.
7. **I9 and the Phase 3 admin UI — frontend.** Complete identity/role/edge
   administration, mailbox and sent-folder round trip, approval/review/library
   actions, quota status and refusal display. The inbox, inspect and decide
   surfaces must implement one eligibility rule.
8. **I10 — integrated governance proof.** For R2, R7, R8, R9, R11, R13 and
   R14, run a positive fire case and a mutation that makes the test fail.
   Exercise the real compiled binding, full mailbox, both quota dimensions,
   signing/export contract and PostgreSQL contention paths. Run the whole
   default and serial testcontainer gates on a frozen tip.
9. **I11 — code-ready cutover handoff.** Update the single recreate runbook,
   compatibility record, notice, identity/grant/edge export and re-admission
   procedure against the final epochs. Verify it locally. Leave the
   destructive cutover and judge-signature firing to the operator.

## Review and release boundary

The September 15 [plan review](2026-09-13-kubernetes-and-identity/plan.review.json)
was `CHANGES_REQUESTED` against `a522874fe`. Commit `6343e2207` repaired
its B1–B4, B7, W1 and W2 plan findings; B8/B9 remain identity decisions in
the master plan, and B5/B6 concern Kubernetes. The task files have not been
re-reviewed against the later quota/schema commits or the final product
decisions. This execution map is not implementation acceptance.

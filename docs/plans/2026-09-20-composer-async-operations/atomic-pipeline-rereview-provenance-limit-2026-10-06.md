# Atomic pipeline rereview: provenance disagreement and technical NO-GO

The outer Codex CLI requested `gpt-daybreak-blue-latest` using the authorized ChatGPT-authenticated read-only route and completed exit 0, thread `01a11121-302f-7810-9100-b100edf8df5e`. The native report below independently says it is not an actual invocation because it did not launch a nested CLI. Both records are retained unchanged in the consumer workspace. This disagreement is not silently reconciled into model proof: this round does not grant the required actual Daybreak clearance. No access or transmission denial occurred in this round; the earlier archive-payload denial remains separate and unresolved for this streaming lane.

Technical disposition remains NO-GO: R1 requires total deterministic collision priority; R2 requires a concrete underlying SQL Future custody boundary. B1–B5 are described as design closures, not implemented code. The next invocation must distinguish the outer reviewer from a forbidden nested reviewer and retain native requested-model metadata. No substitution is permitted.

The complete raw response follows. Machine-specific links alone are normalized; the native originals, JSONL, stderr, exit and frozen input remain intact.

# Amendment rereview: NO-GO

Important provenance limit: this is a focused read-only review of the supplied snapshot, but it is **not an actual `gpt-daybreak-blue-latest` invocation**. The supplied scope prohibits network/provider/app calls, so no nested Codex CLI or provider call was made. This result must not be represented as Daybreak Blue’s verdict.

The revised amendment concretely closes the original B1–B5 design findings, but two implementation-authorizing details remain insufficiently closed.

## B1–B5 disposition

- **B1 — Closed.** Settlement-specific insertion is explicitly separated from ordinary reuse. Every settlement card is candidate-bound; matching older pending cards are superseded, historical terminal evidence remains immutable, duplicates fail closed, and insertion/supersession/derived-state writes share one rollback domain. This addresses the existing reuse behavior in `create_or_reconcile_pending`.
- **B2 — Closed.** Versioned JSON supplies direct candidate, ordered cohort, derived-state, final-head and transition-assistant identities. Explicit produced-state pointers remove reliance on timestamps, `MAX(version)`, ancestry guessing or the current head. Keeping this in closed event payloads is a valid no-migration design, provided payload size and database JSON limits are measured during implementation.
- **B3 — Closed.** Replay is strict, semantic and read-only. It defines the immutable identity tuple, projects persisted transport IDs, tolerates legitimate later resolution/supersession without changing the frozen initial disposition, rejects ambiguity/corruption, excludes later descendants through `final_state_id`, and forbids assistant insertion. Accepted-v1 refusal and pending-v2 manual approval are deliberate, testable legacy limits.
- **B4 — Closed.** The concrete positive predicate now requires running status, exact fence, no Stop marker and `deadline_at > database_now(conn)` inside the locked writer transaction. Synchronous authorities remain distinct, and the amendment requires before-fence rollback plus after-commit persistence proofs.
- **B5 — Closed.** Creation-v3 records the exact operation/fence/epoch/attempt. The sealed revocation path re-proves job, lease, claim, owner, session, actor, user message, proposal and dispatch authority on connection. Recompose by the same user remains distinguishable through operation ID, epoch and attempt. Legacy/null/ambiguous bindings refuse.

The current source does not yet contain these contracts: creation remains `pipeline_proposal_created.v2`, acceptance remains v1, committed replay can insert an assistant, deadline is absent from the positive mutation predicate, and generic revocation lacks operation binding. Those are expected implementation obligations, not proof that the revised plan failed to describe B1–B5.

## Remaining blockers

### R1 — The collision table is not fully ordered

Priority row 1 combines outcomes with different public projections:

- audit/integrity failure → canonical 500;
- ordinary storage failure → canonical 503.

It does not select the winner when both occur within the joined required-work set. “Retain original cause/group” preserves evidence but does not determine the returned envelope. Row 2 similarly groups accounting and child-settlement failures without defining selection when multiple owned failures collide.

Required resolution:

- Give every distinct projection its own total-order rank, or define a deterministic reducer within each rank.
- Specify the selected public result, primary exception, retained `__cause__`/group and secondary evidence for simultaneous integrity, audit, storage, accounting and child failures.
- Add actual collision tests for every same-rank pair, not only pairs across numbered rows.
- Define deterministic handling of multiple failures of the same category without relying on task completion order or exception-group ordering.

### R2 — “Join actual synchronous Futures” lacks an implementable custody boundary

The amendment lists creation, preparation reads, settlement, dispatch audit, review validation and revocation SQL, but it does not identify:

- which concrete layer owns each underlying executor `Future`;
- how those futures are registered before cancellation can interrupt their awaiters;
- which coordinator drains all registered futures;
- how late future outcomes enter the priority reducer;
- when admissions and lease custody may safely be released;
- what happens if future submission itself fails before a future is returned.

The existing route helper shields an asyncio task, while the service also has worker/SQL custody helpers. Naming operations to join does not yet establish one authoritative future registry or prove that cancellation cannot lose a queued synchronous SQL operation.

Required resolution:

- Name the owning coordinator and concrete future-custody object/API.
- Require future registration atomically with submission.
- Define submission-failure, queued, running, completed and cancelled-before-start behavior.
- State that terminal selection and lease/admission release occur only after every registered required future has an observed outcome.
- Add controls for cancellation immediately before submission, between submission and registration, while queued, while running and after SQL completion but before projection.

## Required tests retained

The amendment’s test matrix is otherwise appropriate and remains mandatory:

- Older identical pending card versus newly inserted candidate-bound card.
- Atomic rollback after candidate insertion, every cohort insertion, supersession, each derived state and assistant insertion.
- Every interpretation kind, legitimate later resolution, unrelated later head, corrupted parent/material/pointer/hash and assistant mismatch.
- Closed-shape mutation tests for creation-v3, acceptance-v2 and revocation payloads.
- Actual PostgreSQL lock contention with Stop/deadline before publication and after real commit.
- Local cancellation kept distinct from durable Stop.
- Same-user send/recompose collision and foreign operation/attempt/claim/fence/owner/kind cases.
- Revocation capability mutation-removal controls that genuinely turn red.
- Actual SQL/storage/audit/corruption collisions with original cause objects preserved.
- No replay DML and no provider/tool call.
- No tutorial-special or server-authored pipeline path.

## Hold and limits

- Earlier plan GO remains historical only.
- Amendment status: **NO-GO pending R1 and R2 amendment and rereview**.
- No implementation approval, frozen evidence, merge, deployment, production action or paid-provider testing is granted.
- No tests were executed and no source was written.
- Final frozen full gate, PostgreSQL testcontainers, frontend/E2E/TLS, historical-obligation reconciliation and independent Astra/actual Daybreak review remain owed.
- The final exact branch/head remains held for John’s local testing.
- The two unavailable original reports and denied archive payload remain evidentiary limits; no issue existing only there is closed.

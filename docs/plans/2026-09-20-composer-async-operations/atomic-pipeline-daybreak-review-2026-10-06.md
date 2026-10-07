# Actual Daybreak Blue atomic pipeline amendment review — 2026-10-06

Native CLI `gpt-daybreak-blue-latest`, ChatGPT-authenticated Codex 0.160.0, read-only tools, ignored user configuration, ephemeral thread `01a11114-814a-7d13-9246-47842101662a`, environment inheritance disabled. Completed exit 0; empty native stderr. Verdict **NO-GO on the new amendment**. The earlier plan GO remains historical approval of that earlier plan. Amendment implementation is held until all blockers are resolved and reviewed.

The full native report, JSONL, prompt, unchanged explicit authorization and frozen streaming-only source input remain preserved in the consumer workspace. This durable derivative normalizes machine-specific links/home references only. It does not reconstruct the two unavailable original reports or resolve the separate archive transmission denial.

---

## Daybreak Blue amendment verdict: NO-GO

This is a plan-amendment verdict only. It does not revoke or narrow the earlier Daybreak GO for the earlier plan, and it is not code approval.

The amendment correctly identifies the publication gap and chooses the right general direction: review evidence must be part of the original business publication transaction; durable Stop/deadline must positively fence that transaction; trust-revocation evidence needs a separately sealed audit-only path; and postcommit surfacing cannot remain a business writer.

However, the concrete design is not yet implementable without inventing identity relationships or weakening stated invariants.

### Blocking issues

1. The proposed reuse contract cannot guarantee a candidate-bound review cohort.

The amendment requires every review to retain the original candidate state identity, but also proposes directly reusing `_RepositoryInterpretationMutations.create_or_reconcile_pending`.

That writer may return an existing pending or superseded event instead of inserting the prepared event:

- Existing matching pending authority is selected in [pending_interpretation.py:1968](../../../src/elspeth/web/sessions/pending_interpretation.py#L1968).
- A match returns the prior event identity without inserting the prepared candidate-bound event at [pending_interpretation.py:2008](../../../src/elspeth/web/sessions/pending_interpretation.py#L2008).
- The repository implements that result by returning the old row at [repository.py:1154](../../../src/elspeth/web/coordination/repository.py#L1154).

Such an event remains bound to its earlier `composition_state_id`, not the settlement candidate. The existing import writer tolerates this behavior, but the amendment promises a complete cohort whose reviews all use the candidate identity. Those contracts are incompatible.

Exact required resolution:

- Specify and implement a settlement-specific cohort policy.
- Either prove under the locked transaction that no reusable prior event exists, or require candidate-bound insertion while coherently superseding older cards.
- Define the permitted effects on older pending/superseded events.
- Require the settlement result and replay verifier to check that every cohort member is actually bound to the candidate state.
- Add a control with an existing semantically identical pending review on an older state; it must prove the selected policy rather than silently reusing that older event.

2. Final opted-out derived-head identity is not directly persisted, and the amendment does not define a sufficient reconstruction proof.

The proposal and accepted terminal event point only to the candidate:

- `composition_proposals.committed_state_id` is the candidate pointer at [models.py:678](../../../src/elspeth/web/sessions/models.py#L678).
- Settlement writes that candidate pointer at [service.py:3121](../../../src/elspeth/web/sessions/service.py#L3121).
- Interpretation events bind to the original `state_id` at [repository.py:1241](../../../src/elspeth/web/coordination/repository.py#L1241).
- Opt-out resolution creates a new random derived-state ID at [pending_interpretation.py:2137](../../../src/elspeth/web/sessions/pending_interpretation.py#L2137), but the event row contains no direct pointer to that state.
- The repository only records the state’s `derived_from_state_id` chain at [repository.py:1269](../../../src/elspeth/web/coordination/repository.py#L1269).

The amendment says to reconstruct from “persisted event/derived-state authority,” but does not define the proof that distinguishes the last cohort-produced descendant from a later unrelated descendant. Reading the current session head is expressly disallowed and would be incorrect. Timestamp adjacency alone is not an adequate identity relationship.

Exact required resolution:

- Define a deterministic replay algorithm over existing records, including:
  - selection of the exact persisted candidate cohort;
  - its canonical order;
  - pairing each opted-out event to exactly one derived state;
  - verification of every parent link and event-driven state transformation;
  - a stopping rule that excludes later unrelated descendants.
- Demonstrate that every supported interpretation kind leaves sufficient durable material for that pairing.
- If that proof cannot be made unambiguous, add an explicit durable cohort/final-head binding. Because the amendment currently forbids a schema change, discovering that this is required must return the amendment for a new scope/design review.

3. Exact committed replay is underspecified and conflicts with newly generated transport identities.

`prepare_pending_interpretation_event_drafts_for_state` generates fresh event and tool-call UUIDs on every preparation at [interpretation_surfacing.py:395](../../../src/elspeth/web/composer/interpretation_surfacing.py#L395). The amendment then says IDs remain stable for an attempted settlement while also allowing a committed replay with newly prepared transport IDs to reconcile to persisted identities.

There is no specified semantic cohort key proving which persisted events correspond to the replayed drafts. Node, term and kind alone are insufficient where older/superseded reviews exist, and the canonical writer itself can reuse earlier identities as described in blocker 1.

Exact required resolution:

- Define the immutable semantic identity tuple for each cohort member.
- State which fields must match exactly: candidate state, affected node, kind, normalized term, draft, surface origin, provider/model/skill provenance, ordering and accepted/opted-out result.
- Define how duplicates or multiple matching historical events fail closed.
- Define how replay projects the persisted event/tool IDs back into derived-state verification.
- Require replay to be read-only for business state, including transition assistant handling. The current implementation can insert a missing assistant during committed replay at [service.py:3007](../../../src/elspeth/web/sessions/service.py#L3007), contrary to the amendment’s no-new-business-write replay requirement.

4. The positive publication fence does not presently include deadline, and the amendment does not identify the concrete authority change.

The ordinary settlement enters `_session_composer_mutation_transaction`, which checks the exact live session lease and calls `require_composer_operation_mutation_on_connection` for non-audit writes at [service.py:993](../../../src/elspeth/web/sessions/service.py#L993).

The positive composer predicate checks job identity, `status == "running"` and durable Stop, but it does not reject an expired `deadline_at`: [composer_operation_authority.py:802](../../../src/elspeth/web/coordination/composer_operation_authority.py#L802).

Exact required resolution:

- Amend the concrete positive mutation predicate so a job-backed publication requires both `cancel_requested_at IS NULL` and `deadline_at > database_now(conn)` in the same locked SQL lifetime.
- Preserve the existing behavior for synchronous COMPOSE/PROPOSAL authority that has no durable async job.
- Add positive and negative controls for Stop and deadline, including a deadline expiring while waiting for the session lock.
- Prove the entire candidate/cohort/proposal/assistant transaction rolls back when either condition fails.

5. The proposed audit-only trust-revocation authority lacks a concrete operation-to-proposal binding.

`audit_only=True` skips the composer-operation mutation predicate entirely at [service.py:1025](../../../src/elspeth/web/sessions/service.py#L1025). That is necessary to permit revocation evidence after Stop, but it also means the existing audit admission proves only a live session-operation fence.

The proposal record has no composer operation ID/epoch binding; its durable identity includes session, tool call, message/provenance, dispatch and proposal lifecycle fields at [models.py:641](../../../src/elspeth/web/sessions/models.py#L641). Therefore “exact live lease identity, owner/session/operation and lifecycle proposal/dispatch authority” does not by itself prove that this exact operation owns this exact pending proposal.

Exact required resolution:

- Name the existing durable records and predicates that prove the operation-to-proposal relationship for both auto-compose and recompose.
- If the intended proof is through originating user message plus tool/dispatch authority, specify every equality and its behavior for nullable/legacy proposal fields.
- Refuse legacy or ambiguous cases rather than treating same-session membership as ownership.
- If existing records cannot prove this binding, introduce an explicit durable binding and submit that schema/scope change for review.
- Expose only a narrow sealed revocation operation. Do not pass a general `audit_only` transaction capability to ordinary settlement writers or reuse `record_auto_commit_revocation`.

### Required SQL lifetime and cause-priority clarification

The amendment correctly requires required SQL to outlive caller/local cancellation and preserve its actual outcome. The present route helper shields a task and waits through cancellation at [pipeline_settlement.py:64](../../../src/elspeth/web/sessions/routes/composer/pipeline_settlement.py#L64), but the amended design still needs an explicit priority table covering:

- SQL integrity failure;
- ordinary SQL/storage failure;
- durable Stop;
- deadline expiry;
- local shutdown/caller cancellation;
- already-completed terminal result;
- trust-revocation outcome.

The plan must state the returned/raised winner and retained `__cause__` for every relevant collision. “Common terminal priority” and “original exception priority” are not sufficiently concrete implementation instructions.

### Retained test obligations

The older tests are not interchangeable with the new durable-Stop controls:

- The existing cancellation test deliberately distinguishes local shutdown from durable Stop through its `durable_stop` parameter at [test_routes.py:1473](../../../tests/unit/web/sessions/test_routes.py#L1473).
- The trust-revocation control makes the same distinction at [test_routes.py:1864](../../../tests/unit/web/sessions/test_routes.py#L1864).
- The new durable-Stop wrappers are at [test_routes.py:15600](../../../tests/unit/web/sessions/test_routes.py#L15600).

Future implementation must retain the older local-cancel cases without manufacturing Stop markers, while adding independent durable-Stop/deadline transaction controls. Refactoring the old tests into helpers is acceptable only if both actor distinctions and all existing assertions remain independently exercised.

### Future proof obligations and limits

A revised amendment must additionally prove:

- Atomic rollback for failure after candidate insertion and after each earlier cohort insert, including supersession and opt-out-derived writes.
- Exactly one candidate-bound cohort and a truthful proposal acceptance pointer to the candidate/hash.
- Exact, read-only replay with no assistant, interpretation, graph, provider or tool write.
- A later unrelated session head cannot affect replay output.
- Audit-only revocation cannot call any state, interpretation, proposal-decision or assistant writer.
- Foreign, released, expired or owner-mismatched leases fail; durable Stop/deadline may bypass only the narrowly sealed revocation audit, not lease validity.
- PostgreSQL contention tests cover Stop/deadline against the actual locked transaction, not merely a paused coroutine.
- No provider-call count is introduced by preparation, settlement, replay or recovery.
- No tutorial-specific or server-authored pipeline path is introduced.

No tests were run, as instructed. No network, apps, provider calls, environment reads, archive payloads, credentials, secrets, source writes, implementation, merge, deployment or production actions were performed.

The two unavailable original reports remain an evidentiary limitation. This review cannot close issues that may exist only in those reports.

The explicit authorization remains unchanged and intact:

> Assistant: “The readiness work is complete, but Daybreak Blue hasn’t reviewed it yet. The plan is reconciled and the baseline tests passed; implementation is still on hold.
>
> The outstanding items are the two original review reports and two decisions:
> - Should users see answer text as it’s generated, or live progress followed by the completed answer?
> - May I send the revised streaming plan and relevant private Elspeth source to Daybreak Blue through the existing ChatGPT-authenticated Codex CLI for read-only review, excluding credentials and secrets?”
>
> John Morrissey, message Sentinel_1c793cc91b00819196cba470c00f11b3: “live progerss followed by a completed answer, and yes.”

Mandatory tooling context read and applied: `superpowers:using-superpowers` and `plan-review`. Delegation and the skill’s normal report-file output were not used because the review scope expressly required self-review and no writes.

# Diagnostics source, tooling and systems review

This public-safe review supports the [0.8.7 master defect list](https://github.com/dta-au/elspeth/issues/315). Private diagnostic evidence remains separate.

## Confidence Assessment

**High confidence in the bounded source mechanisms and offline reproductions below.** Reviewed source at `eabcd48523cd2fc0ac535493baeb93e35d6a0c16`; 14 targeted offline cases passed, including positive and counterexample controls. Several cases deliberately assert the presence of a defect. This is audit evidence, not release readiness, and no live provider or QA pipeline was executed.

The existing smoke findings describe interacting control and information problems, not merely poor presentation or one slow model call:

| Crosswalk | Finding | Status and priority |
|---|---|---|
| SMK-06 | Complete-message budget enforcement differs by entry and attempt. A synthetic EARLY message reached 16,435 characters against the default 16,000-character allowance; a format retry grew an admitted 15,980-character input to 16,279 without rechecking. | Confirmed extension. Apply admission to the actual rendered input on every attempt. |
| SMK-06 follow-up | A valid 17,019-character user request alone can exceed the default review budget. Whole-line prefix selection can omit a late changed node while preserving earlier unrelated content. | Confirmed capacity/visibility limitation. A truthful WITHHOLD remains correct; no false-approval escape was demonstrated. Do not truncate authoritative request/reply text to manufacture a fit. |
| SMK-10 / SMK-02 | A content-identical empty source patch advances the version and is counted as mutation progress, clearing the anti-anchor failure window. | Confirmed P2 extension beyond duplicate persistence. Separate committed revisions from meaningful progress. |
| **NEW: retained signoff entrypoint** | `AdvisorCheckpointOwner.run_signoff_checkpoint` supplies no publication review, while an accepted END audit record requires its canonical binding. Valid ADMIT and WITHHOLD responses both raise an integrity error through the actual owner path. | Confirmed latent P2 API defect. Shipping route exposure is not established. Require real review evidence or retire the unsupported entrypoint; do not weaken audit validation. |
| SMK-12 | The normal planner projection retains an old draft labeled resolved while dropping accepted-value linkage and prompt parts. The flattened prompt still contains the amendment, producing competing representations. Canonical authoring export/reconciliation preserves the amendment. | Existing P1 preservation family. Fix the editable representation and dependent-edit workflow, preserving refusal and approval authority. |
| SMK-08 | A red runtime cause is retained in chat prose but replaced by a generic advisor error in the structured result that persistence consumes. | Confirmed P2 owner defect. Preserve independent runtime and advisory facts; a frontend wording fix is insufficient. |

Representative source owners:

- Budget: `src/elspeth/web/composer/advisor_checkpoint.py:1168`, retry at `:1285`, and `advisor_policy.py:371`.
- Progress: `src/elspeth/web/composer/discovery_cache.py:33`, `tool_batch.py:3063`, and `anti_anchor.py:144`.
- Retained entrypoint: `src/elspeth/web/composer/advisor_checkpoint.py:993`; audit invariant `advisor_audit.py:444`.
- Reviewed-state projection: `src/elspeth/web/composer/prompts.py:304`; requirement projection `src/elspeth/web/interpretation_state.py:225`.
- Cause preservation: `src/elspeth/web/composer/advisor_checkpoint.py:928`; persistence `src/elspeth/web/sessions/routes/_helpers.py:1903`.

The broader feedback chain is: incomplete or conflicting evidence increases failed reconstruction; refusals and partial dependent edits increase repair work; repair work consumes remaining time and adds history; no-op progress can suppress useful correction feedback. A larger timeout can permit more of the same work without repairing the information deficit. Streaming can improve observability but does not establish completion or semantic fidelity.

Guidance contradictions also need owner-level correction: optional schema fields do not guarantee presence; a tool's root grammar is not expanded by global prose recommending descriptions; structured-response constraints do not remove contradictory authored prompt instructions; fields omitted from a rendered summary prompt are not magically available to the model. Keep correct validators, explicit failure routes and human review intact.

Important counterevidence narrows the conclusions:

- Exact current request/reply/graph publication binding and explicit missing-evidence instructions remain enforced. Omission is a liveness/evidence-quality issue, not a demonstrated deterministic approval escape.
- Structurally invalid terminal states intentionally withhold review-card publication. That branch is not promoted as a new defect.
- Current missing-review recovery creates the missing card, remains idempotent and does not revive resolved evidence. Validation refreshes the frontend review projection afterward. Historical missing-card evidence does not prove this recovery is absent now.
- Current terminal UI publishes the terminal outcome before optional bounded refreshes; current schema context distinguishes past loading from evidence delivered in this request. Historical symptoms must be rechecked against these owners.
- A valid reconstructed graph is not proof of fidelity to the original request. No incorrect QA runtime output or late post-cancellation mutation was established.

## Risk Assessment

**High risk if a symptom fix weakens authority; Moderate for narrowly owned repairs.** Do not fit budgets by dropping the current request, use placeholder publication hashes, auto-approve reviews, relax requiredness, silently select discard, or roll back unrelated successful edits. Preserve provider authorship, exact review bindings, current policy, session fencing, structured causes and partial-success audit evidence.

## Information Gaps

The retained signoff API's intended production reach, current deployment interruption behavior, broader no-op writer coverage, matched provider latency attribution and actual semantic output quality remain unqualified. Tail evidence omission is reproduced; a resulting false model verdict is not. Cancelled upstream usage requires separate accounting evidence before claiming a cost defect.

## Caveats & Required Follow-ups

Complete the currently assigned budget repair first, with every attempt/entry tested. Track evidence selection separately, extend no-op tests to convergence, preserve canonical reviewed-state editing, and retain structured runtime failures alongside advisor outcomes. Validate any repair on its actual candidate before closing the corresponding master item. This addendum does not establish full-branch, CI, operator-signature, merge or release qualification and does not authorize another development lane.

# Freeform tutorial: failed live example diagnosis (2026-09-28)

Scope: read-only examination of tutorial session `b5bedc60-3db7-435f-b469-d58026a8687c`, the captured third browser run, and the later session `00822e0a-980a-45e0-8694-d5181ae3010e`. No live state was changed. The session DB observations below were made at about 02:30 UTC; this is a snapshot, not a claim that nobody can subsequently resolve the pending reviews.

## Verdict

The third run failed in the browser driver, not in the planner tool protocol or the product's Run-readiness gate. Its planner made successful, schema-conforming tool calls and left precisely four required review decisions. The product correctly disabled **Continue to Run** while those reviews were pending. The driver had checked that button as enabled and then used a default-timeout Playwright click; when the async composition/review state disabled it before click completion, Playwright waited 30 seconds and failed, never reaching the driver's review-resolution branch. The exact frontend state-transition instant was not captured, so the race timing is inferred from the driver control flow plus Playwright's "element is not enabled" trace, rather than independently timestamped.

The later run establishes a separate client/contention concern, not an `interpretation_already_resolved` case: it completed compose, run, audit, and graduation, but six POSTs (three review resolves and three validates) returned `409 {"detail":"Session operation is already active"}`. The UI recovered in this instance; the 409s remain real failed requests and browser console errors. Fast successive review clicks and two validation triggers are a plausible source of overlap; exact request interleaving cannot be proven from the available capture.

## Third-session sequence and counterevidence

Source: read-only queries of `data/sessions.db` tables `chat_messages`, `composition_states`, and `interpretation_events`; browser capture `/tmp/guided-removal-live-tutorial-conflicts-20260928.log` and `run-1-exception.png` in `src/elspeth/web/frontend/test-results/staging-tutorial-harness/`.

| Sequence | Persisted event | Result |
| --- | --- | --- |
| 3 | Planner `get_plugin_schema` for CSV source, web scrape, LLM, field mapper, JSON sink | Five tool results, all `success:true` |
| 12–13 | Planner `set_pipeline` | `success:true`, tool validation `is_valid:true`, `errors:[]`; state v1 persisted |
| 16–17 | Planner `patch_node_options` on cleanup node after advisor caught a fingerprint input-contract mismatch | `success:true`, validation `is_valid:true`, `errors:[]`; state v2 persisted |
| 19–20 | Planner `preview_pipeline` | `success:true`, validation `is_valid:true`, `errors:[]` |
| 23–26 | Planner requested three interpretation reviews: generated CSV source, prompt-injection-shield recommendation, raw-field cleanup | Three successful `interpretation_review_pending` results |
| Finalization | Backend surfaced a fourth `llm_prompt_template` review; post-compose state v3 | `is_valid:0`, exactly four `interpretation_review_pending` errors; no other validation errors in that state |

All four `interpretation_events` rows were still `pending` in the read-only snapshot: `invented_source` on `source`, `pipeline_decision` on `summarize_briefs`, `pipeline_decision` on `cleanup_output`, and backend-owned `llm_prompt_template` on `summarize_briefs`. The prompt-template event's `tool_call_id` begins `backend_auto_surface`; the other three have planner `call_...` IDs. The source review is appropriate: although the user supplied three URLs, the planner generated CSV bytes from them. The prompt-shield review is appropriate because fetched external content flows to an LLM; the policy allows user approval of the unshielded design. The cleanup review likewise matches a planner-authored decision. These are expected human decisions, not tool-call errors or late-arriving hidden reviews. They were created at 02:20:15–16 UTC, before the final assistant message at 02:20:26 UTC. The compose HTTP response was 200 after 62,018 ms.

The screenshot shows "Reviewing 4 composer choices", "Awaiting your decision (5)", four red checks, and a disabled **Continue to Run** button. `TutorialFreeformShell.tsx:115-145,169-173` deliberately blocks continuation when `pendingReviewCount > 0`; the persisted four pending reviews explain the four validation errors. `pipeline_composer.md:36-38,88-96,1131-1142` explicitly tells the planner to surface its authored review requirements and leave the backend-owned prompt-template review alone. The planner complied. Its final assistant message said the update was ready for required review. This is counterevidence to "misfired planner call", "wrong planner guidance", and "malformed draft" as explanations for this specific failure.

At the failed-run commit (`e1a2ef001`), `staging-tutorial-driver.mjs:69-99` did `if (await isEnabled(continueToRun)) await continueToRun.click()` with Playwright's 30-second default click timeout. The captured error shows Playwright resolving the button but repeatedly finding it disabled. Because execution was suspended inside `click()`, it could not reach `resolveVisibleReviews(page)` at line 88. This supports a driver time-of-check/time-of-use race. The concurrently edited driver now uses a bounded click and rechecks disabled state; that change was not part of this read-only diagnosis and needs its own verification.

## Later-run 409s: distinct behavior

Source: `/tmp/guided-removal-live-tutorial-final-20260928.log` and read-only DB query of session `00822e0a-980a-45e0-8694-d5181ae3010e`. The capture reports compose/run/audit HTTP 200, `landed:true`, `graduated:true`, four interpretation events `accepted_as_drafted`, and zero pending. It also reports six 409 responses: three `POST /interpretations/{id}/resolve`, three `POST /validate?state_id=...`, all with `Session operation is already active`; `ok:false` resulted from six browser resource console errors. This body is produced by `session_operation_handlers.py:28-30` for `SessionOperationConflictError`, not the interpretation route's explicit `interpretation_already_resolved` branch (`interpretation.py:149-155`).

The frontend can create competing work: `staging-tutorial-driver.mjs:29-57` clicks an enabled review button and loops without waiting for its network resolution; each card has only local in-flight disabling (`useInterpretationResolver.ts:227-240`). On successful resolution, `sessionStore.ts:2631-2657` fire-and-forget starts `validate()`, while `subscriptions.ts:537-550` can also auto-validate on state-version changes. The resolve route acquires a COMPOSE lease (`interpretation.py:127-147`); validate acquires a BLOB_READ lease (`execution/routes.py:932-1005`); both contend on the same session authority. The 409 pattern is therefore consistent with same-session overlap, but the capture lacks per-request timestamps/lease ownership to identify the exact winner for each rejection. The DB's final fence was released and all four reviews resolved; there is no evidence of persistent lock leakage in that session.

Interpretation resolve does not silently mark a failed 409 as approved: `interpretationEventsStore.ts:416-437` propagates the error without changing event projections, and `useInterpretationResolver.ts:227-240` displays it and re-enables the card. Thus recovery is possible, as observed, but the user sees failed requests and validation noise. Treat this as a product-side concurrency/UX issue for follow-up, not as a harmless duplicate-resolve response or a cause of the third run (that capture recorded no 409s and never attempted a resolve).

## Nearby battery deviation

The separate `transform_pipeline/1` battery artifact scored `is_valid:true` but `green:false`: `score.json` reports `wrong_shape` (five components versus four expected), two excess schema discoveries, and one repaired `plugin_options_invalid` set-pipeline attempt. The final graph added a `finalize_total` type-coerce node after computing `line_total`; it is valid but outside the battery's expected minimal topology. This is a genuine planner efficiency/shape deviation in a different task, not evidence that the tutorial's planner made an invalid call.

## Recommended next checks

1. Verify the bounded-click driver change with another live tutorial attempt; assert that pending cards are resolved before Run, not merely that compose returns 200.
2. Capture timestamped request start/end, state version, and session-lease holder for review resolves and validates. Reproduce rapid successive approvals; then make validation scheduling and review mutation coordination converge without user-visible 409 churn, preserving server-side session fencing.
3. Keep the `transform_pipeline` quality deviation in its own planner-evaluation lane; do not weaken tutorial review gates or bypass the provider to make the canary green.

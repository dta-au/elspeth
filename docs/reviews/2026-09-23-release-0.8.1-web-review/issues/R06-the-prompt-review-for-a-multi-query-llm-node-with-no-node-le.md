# R06. The prompt review for a multi-query `llm` node with no node-level `prompt_template` cannot be approved

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-07-interpretation#1 |

## Finding

- **Status and severity:** confirmed, **medium**. Raised as high; the trace refuter rated it medium and the impact refuter kept it high.
- **Location:**
  - `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx:419` (the gate) and `:728-729` (the gate note).
  - `src/elspeth/web/frontend/src/lib/promptTemplateDisplay.ts:184-195` and `:333-337`.
  - Backend producer: `src/elspeth/web/interpretation_state.py:2703-2717`.
- **What is wrong:** 07faf477e added `approveGated = requiresPromptView && (!promptReviewAvailable || ...)`. The frontend's `multiQuerySurfaceFromOptions` requires a string `prompt_template`. The backend `multi_query_prompt_surface_from_options` accepts an absent or `None` template, and an inline-blob marker when every query has a template. It then stages, surfaces and can resolve an `llm_prompt_template` review that the frontend can never render. Before the window the card fell back to `llm_draft` and could be approved after View prompt.
- **Failure scenario:** The composer authors `queries: {a: {template}, b: {template}}` with a `system_prompt` and no node-level template. `_validate_llm_prompt_roles_present` accepts this, and the composer skill teaches this shape. A pending review is written. Approve stays `aria-disabled` and the note says "Reload the session…", which does not help. `supportsAmendment` is false, so there is no Change… path. The review blocks execution.
- **Evidence:** `git diff --stat 7c986dc97 74c0ce0db -- interpretation_state.py` is empty, so the backend producer did not change and the new gate is the regression. `pending_interpretation.py:1956-1959` and `:1175-1178` accept a non-string `prompt_template` when the multi-query surface is present.
- **Suggested fix:** Make `multiQuerySurfaceFromOptions` accept exactly what the backend accepts: a missing or null `prompt_template`, and a marker when every query has a template. Render the node-level template as "not used". Fix the backend docstrings at `interpretation_state.py:2689` and `1638-1641`.
- **Sources:** seam-07-interpretation#1.
- **Verifier notes:** Two corrections to the original finding:
  - There is an exit: adding any node-level `prompt_template` makes the surface non-null. The exit is not discoverable.
  - `promptTemplateDisplay.test.ts:543-554` is correct as written. It uses `prompt_template: 7`, which the backend also rejects. The gap is a missing test for an absent template.


## Source findings and verification

### seam-07-interpretation#1: Prompt review for a multi-query llm node without a node-level prompt_template is permanently unapprovable

- **Reported at:** `src/elspeth/web/frontend/src/components/chat/AcknowledgementCard.tsx:419`; reviewer severity high; category ux-regression; diff-anchored True.
- **Summary:** The window's gate `approveGated = requiresPromptView && (!promptReviewAvailable || ...)` blocks Approve whenever resolvePromptDisplaySegments returns reviewAvailable:false. The frontend multiQuerySurfaceFromOptions (promptTemplateDisplay.ts:192) requires a string prompt_template. The backend multi_query_prompt_surface_from_options (interpretation_state.py:2706-2717) admits an absent/None node-level template, and an inline-blob marker when every query is templated. It stages and surfaces an llm_prompt_template review for these nodes, and the frontend can never render or approve it.
- **Failure scenario:** The composer authors an llm node with queries {a:{template:...}, b:{template:...}}, a system_prompt and no node-level prompt_template. The new _validate_llm_prompt_roles_present admits this, and LLMConfig's rule as restated at state.py:3805 does too. _options_with_default_prompt_template_review stages a pending PT requirement from surface.render_for_review(). _missing_prompt_template_review_sites enumerates the site and a pending event is written. On the card, resolvePromptDisplaySegments falls through to reviewAvailable:false, so Approve stays aria-disabled forever with 'Reload the session…' copy that cannot help. supportsAmendment is false for llm_prompt_template, so there is no Change… path. The pending review blocks execution, and the only exit is opting out of all reviews for the session. At 7c986dc97 the same card fell back to llm_draft and was approvable after View prompt.
- **Evidence:** AcknowledgementCard.tsx:419 and :729 (07faf477e); promptTemplateDisplay.ts:184-195 and :333-337; interpretation_state.py:2706-2717 skips the guard when prompt_template is None; render_for_review 2466-2471 handles node_prompt_template=None; _missing_prompt_template_review_sites 1646-1670; tools/_common.py:383-420. `git diff --stat 7c986dc97 74c0ce0db -- interpretation_state.py` is empty, so the producer did not change and the gate is the regression. promptTemplateDisplay.test.ts:543-554 wrongly asserts 'the backend reads no surface'.
- **Suggested fix:** Make multiQuerySurfaceFromOptions admit exactly what the backend admits: an absent/null prompt_template, and a marker when every query has a template. Render the node-level template as 'not used'. Fix the backend docstrings at interpretation_state.py:2689 and 1638-1641.
- **Verifier (trace):** upheld, confidence high, severity medium. I could not refute this. I traced the path at 74c0ce0db and every step holds.

- **The node is admitted.** LLMConfig types `prompt_template` as `str | None` and requires it only when some query has no template (plugins/transforms/llm/base.py:219, :435). The window's `_validate_llm_prompt_roles_present` (composer/state.py:3829-3844) accepts a node where every query has a template and there is no node-level `prompt_template`.
- **The backend stages the review.** `multi_query_prompt_surface_from_options` (interpretation_state.py:2703-2717) accepts a missing or None `prompt_template`, and an inline blob marker when every query has a template. `prompt_review_draft_from_options` (:2850-2852) then returns `render_for_review()`, and `_options_with_default_prompt_template_review` (tools/_common.py:407-420) stages a pending requirement.
- **The backend writes and can resolve the event.** `_missing_prompt_template_review_sites` (:1648-1649) enumerates the site. The service auto-surfacer (service.py:2030-2043) returns the rendered surface as the draft. `create_pending_interpretation_event` (pending_interpretation.py:1956-1959) and the resolver (:1175-1178) both explicitly accept a non-string `prompt_template` when the multi-query surface is not None.
- **The frontend cannot render it.** `multiQuerySurfaceFromOptions` (promptTemplateDisplay.ts:187-195) returns null unless `prompt_template` is a string. `segmentsFromParts(undefined)` returns null (:92), and the `typeof template === "string"` branch is skipped. So `resolvePromptDisplaySegments` reaches the final branch, which returns `reviewAvailable: false` (:333-337).
- **Approve stays blocked.** In AcknowledgementCard.tsx, `approveGated = requiresPromptView && (!promptReviewAvailable || ...)` (:419) is true whether or not the prompt has been viewed. The Approve click returns early, and the gate note (:728-729) tells the user to reload, which changes nothing.
- **This is new in the window.** Before 07faf477e, `PromptDisplayResult` had no `reviewAvailable` field. That commit added the field and the `!promptReviewAvailable` gate. At 7c986dc97 the card fell back to `llm_draft` and could be approved after View prompt.
- **It is not a recorded ruling.** docs/reviews/2026-09-22-chat-card-repairs.md:9-11 aims to stop a bounded preview from being approved while the composition is still loading. It does not mention this node shape, and recent-code-hints.md has no entry for it.

Two parts of the finding are wrong:
- **The test is correct.** The test at promptTemplateDisplay.test.ts:543-554 uses `prompt_template: 7`, and the backend returns None for that value too (not None, not a string, not a marker). The missing coverage is a test for an absent `prompt_template`.
- **There is an exit.** The user or planner can add any node-level `prompt_template`. It would be listed as unused, but it makes the frontend surface non-null. The card is therefore not permanently unapprovable.

Given that exit, I rate it medium rather than high. It still blocks execution of a valid config, and the on-screen advice to reload does not help.
- **Verifier (impact):** upheld, confidence high, severity high. I could not refute this. I traced the failure path myself at 74c0ce0db. For a multi-query llm node that has per-query templates and no node-level prompt_template, the backend stages, surfaces, writes and can resolve an llm_prompt_template review. At 07faf477e the frontend began requiring reviewAvailable before Approve. The frontend's multi-query parser rejects this shape, so the card ends with reviewAvailable:false and Approve is permanently aria-disabled. No recorded ruling covers it. The composer skill teaches exactly this shape ("one user prompt per query (queries.<name>.template)"), so the path is reachable. Before the window, the gate was `requiresPromptView && !promptViewed`, so the card showed the llm_draft fallback and could be approved after View prompt. That makes this a regression introduced in the window. There are workarounds: asking the composer in chat to add a node-level prompt_template, or opting out of reviews. Neither is discoverable, and the gate note ("Reload the session…") points the user at something that cannot help. High severity stands. One part of the finding is wrong: promptTemplateDisplay.test.ts:543-554 uses prompt_template: 7. The backend also returns None for that shape (it is not a blob marker), so that test's comment is correct. This does not affect the main defect.

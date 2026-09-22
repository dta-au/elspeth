# R39. `cost_unavailable` still offers Retry, and every retry is another provider call that cannot be priced

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Pricing, planner failures and tutorial run |
| Review line | backend, seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-22#1, seam-01-wire#5 |

## Finding

- **Location:** `src/elspeth/web/sessions/protocol.py:184-195`, `sessions/routes/_helpers.py:2894-2897`, `frontend/src/components/chat/MessageBubble.tsx:284` and `stores/sessionStore.ts:3838-3839`.
- **Wrong:** The new code returns 503 "ask an administrator to configure pricing", but the retry gates hide Retry only for `policy_blocked` and `admission_refused`. `COST_UNAVAILABLE` is raised after the provider call (`pipeline_planner.py:4060-4066`).
- **Fix:** Treat the code as permanent in both gates, preferably as one exported set, and in the protocol comment.
- **Sources:** be-22#1, seam-01-wire#5.
- **Verifier notes:** This is not a regression in when Retry appears: the old `invalid_provider_response` copy said "Retry". The protocol's permanent/transient list was never complete.


## Source findings and verification

### be-22#1: cost_unavailable missing from the permanent/transient split; clients offer Retry, and each retry is another provider call that cannot be priced

- **Reported at:** `src/elspeth/web/sessions/protocol.py:195`; reviewer severity medium; category half-wired-change; diff-anchored True.
- **Summary:** The GuidedOperationFailureCode comment (protocol.py:184-190) says the vocabulary carries a PERMANENT-vs-TRANSIENT split that clients read to decide whether to retry. cost_unavailable was added at :195 with no classification. The server treats it as needing an administrator, like admission_refused ('Ask an administrator to configure or correct model pricing before trying again', 503). The frontend retry gates exclude only policy_blocked and admission_refused.
- **Failure scenario:** The deployment's planner model has no pricing, so the provider's cost metadata comes back None. The user sends a message and gets a 503 'ask an administrator', but MessageBubble still shows Retry. pipeline_planner.py:4060-4066 raises COST_UNAVAILABLE after the provider call returns, so each click makes another billed provider call that cannot be priced. It fails the same way every time until pricing is repaired, and nothing limits the number of clicks.
- **Evidence:** protocol.py:184-195. frontend/src/components/chat/MessageBubble.tsx:284 and frontend/src/stores/sessionStore.ts:3838-3839 both suppress retry only for policy_blocked/admission_refused. sessionStore.ts:2325-2359 copies apiErr.failure_code onto local_failure_code. routes/_helpers.py:2894-2897, guided_operations.py:93-96 and guided_plan.py:142-149 carry the admin-action copy. pipeline_planner.py:4060-4066 has the comment 'Cost enforcement is intentionally post-call and pre-parse'.
- **Suggested fix:** Mark cost_unavailable as permanent in the protocol comment. Add it to the retry-suppression predicates in MessageBubble.tsx:284 and sessionStore.ts:3839 alongside admission_refused. Consider exporting a single permanent-code set that both gates use.
- **Verifier (trace):** upheld, confidence high, severity low. The path is reachable at 74c0ce0db, and I found nothing earlier that guards against it. When the planner has no pricing, `provider_cost` comes back None after the provider call returns, and the planner raises COST_UNAVAILABLE. The freeform route maps that to failure_code "cost_unavailable" and returns a 503 whose text says to ask an administrator. The body also carries `failure_code`. The store copies it onto the failed message, and MessageBubble still shows Retry because it hides the button only for policy_blocked and admission_refused. Nothing in the frontend checks cost_unavailable, the planner_code COST_UNAVAILABLE or service_setup_failed to hide Retry. Each click is another recorded provider call that fails the same way while pricing is still missing.

I am lowering the severity to low for three reasons:
1. It is not a regression in when Retry appears. Before 101824e7d, COST_UNAVAILABLE was grouped under invalid_provider_response, and that text said "Retry the request." The window corrected the text but did not update the retry checks.
2. The protocol comment's permanent-vs-transient list was never complete. quota_exceeded, integrity_error, custody_error and admission_refused are not classified in it either, so the missing entry in the comment is weak evidence of a half-wired change. The real mismatch is between the frontend retry checks and the server's text.
3. Every retry needs a manual click, and a user can resend the same text anyway. Removing the button fixes a contradictory UI but does not cap spending.

What remains is a real half-wired change: a new enum value that the server treats as needing an administrator, while the client still offers Retry.

### seam-01-wire#5: cost_unavailable is sent to the freeform client but the failed row still offers Retry

- **Reported at:** `src/elspeth/web/sessions/routes/_helpers.py:2894`; reviewer severity low; category ux; diff-anchored True.
- **Summary:** The new freeform failure_code cost_unavailable (a 503 telling the user an administrator must fix pricing first) is threaded onto the failed message row (sessionStore.ts:2326). MessageBubble.tsx:284 hides Retry only for policy_blocked and admission_refused, so it still offers Retry for a failure that retrying cannot fix until the config changes.
- **Failure scenario:** Pricing for the composer model is missing. The user sends a message, gets the 'Ask an administrator' error, and is shown a Retry button. Each retry spends another provider call and fails the same way.
- **Evidence:** _helpers.py:2894 has the cost_unavailable entry. MessageBubble.tsx:284 has the retry suppression list. MessageBubble.tsx was not touched in the window.
- **Suggested fix:** Either treat cost_unavailable as non-retryable in MessageBubble, or deliberately keep Retry and change the copy to match.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute it. I traced the path end to end at 74c0ce0db and it is reachable as described. The planner raises COST_UNAVAILABLE after the provider call. The window's change maps it to a new freeform failure_code, cost_unavailable, which returns a 503 whose text says an administrator must fix pricing. The client puts that code on the failed message row. MessageBubble still shows Retry for every code except policy_blocked and admission_refused. The commit's own runbook says repeating the request cannot repair a missing catalog entry. So the change is half-wired: the new code and its "ask an administrator" text landed, but the retry affordance was not updated. This is not a regression in retry availability. Before the window, COST_UNAVAILABLE mapped to invalid_provider_response, whose text said "Retry the request", so Retry was offered then too and matched the copy. The new text now contradicts the button. Severity stays low: it is a UX inconsistency and each retry wastes one provider call, but it does not block the user or corrupt anything.

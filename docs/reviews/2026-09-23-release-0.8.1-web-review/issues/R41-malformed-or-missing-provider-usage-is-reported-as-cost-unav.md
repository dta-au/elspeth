# R41. Malformed or missing provider usage is reported as `COST_UNAVAILABLE`, which now reads as a pricing-configuration fault (pre-existing)

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Pricing, planner failures and tutorial run |
| Review line | seams |
| Pre-existing (touches lines outside the window) | yes |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-03-pricing#2 |

## Finding

- **Location:** `src/elspeth/web/composer/pipeline_planner.py:4063-4067` and `core/llm_pricing.py:424-450`.
- **Wrong:** The `provider_cost is None` check comes before the `MALFORMED_RESPONSE` completion-tokens branch, and missing usage and an unknown catalog entry both return `(None, 'not_available')`. 101824e7d now maps the code to "ask an administrator … pricing". The call is audited as SUCCESS.
- **Fix:** Check that usage is admissible before the cost check, or have `llm_pricing` report a distinct "usage invalid" reason.
- **Sources:** seam-03-pricing#2.
- **Verifier notes:** Reaching this requires a Tier-3 anomaly plus a deployment alias that LiteLLM cannot price. `test_pipeline_planner.py:5599-5618` pins the intended `MALFORMED_RESPONSE`, but only when a provider cost is present.


## Source findings and verification

### seam-03-pricing#2: Malformed or missing provider usage is reported as COST_UNAVAILABLE, which 101824e7d now shows as a pricing-configuration fault

- **Reported at:** `src/elspeth/web/composer/pipeline_planner.py:4063`; reviewer severity medium; category error-attribution; diff-anchored False.
- **Summary:** The planner checks `call.provider_cost is None` (raising COST_UNAVAILABLE) before its `completion_tokens is None` MALFORMED_RESPONSE branch. core/llm_pricing.py returns the same (None, 'not_available') for an unknown catalog entry and for missing or malformed usage and cost metadata. 101824e7d took COST_UNAVAILABLE out of the invalid-provider codes and mapped it to cost_unavailable: a 503 'Ask an administrator to configure or correct model pricing', reason service_setup_failed. So an invalid provider response is now presented as the pricing case, the opposite of that commit's aim, and the MALFORMED_RESPONSE branch cannot be reached for calculated-cost providers such as Azure.
- **Failure scenario:** An Azure composer deployment has a priceable composer_pricing_model. One planner response arrives with usage absent or completion_tokens missing. The call is recorded with status SUCCESS, COST_UNAVAILABLE is raised, and the user sees 'The composer could not determine the model cost. Ask an administrator to configure or correct model pricing before trying again.' A retry would have succeeded, and before 101824e7d this case read as invalid_provider_response / 'Retry the request'.
- **Evidence:** Measured provider_cost_from_response(..., pricing_model='openai/gpt-4o-2024-08-06'): full usage gave (0.00045, 'litellm.cost_per_token'); usage missing, completion_tokens missing, negative usage.cost and an unknown model all gave (None, 'not_available'). Mapping sites: sessions/routes/_helpers.py:2894 and :2983, routes/composer/guided_plan.py:307, routes/guided_operations.py:93, routes/messages.py:661-667.
- **Suggested fix:** Before the cost check, test whether usage is admissible (prompt/completion tokens present, optional counters valid) and raise MALFORMED_RESPONSE when it is not. Alternatively, have llm_pricing report a distinct 'usage invalid' reason while keeping the audit source not_available.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute the finding. At 74c0ce0db, pipeline_planner.py:4063 checks `call.provider_cost is None` and raises COST_UNAVAILABLE before the `completion_tokens is None` MALFORMED_RESPONSE branch at :4067. Both values come from the same usage object in build_llm_call_record (llm_response_parsing.py:647-684). When the response has neither usage.cost nor _hidden_params.response_cost, calculate_missing_provider_cost (core/llm_pricing.py:424-434) returns (None, not_available) for invalid optional counters, a missing pricing_model, missing prompt or completion tokens, and impossible cached or reasoning subtotals. It also returns that for an unknown catalog entry (:449-450). So malformed usage takes the COST_UNAVAILABLE path. Commit 101824e7d then removed COST_UNAVAILABLE from _FREEFORM_PLANNER_INVALID_PROVIDER_CODES and mapped it to 'cost_unavailable': a 503 with 'Ask an administrator to configure or correct model pricing', reason service_setup_failed (_helpers.py, guided_plan.py, messages.py). The call is also audited with status SUCCESS, not MALFORMED_RESPONSE. The intended semantics are pinned by test_missing_completion_token_metadata_is_audited_then_rejected (tests/unit/web/composer/test_pipeline_planner.py:5599-5618), which expects MALFORMED_RESPONSE. That test only passes when a provider cost field is present, so it does not cover the calculated-cost path, where the ordering flips the outcome. docs/agents/recent-code-hints.md has no ruling on this. The new runbook only says COST_UNAVAILABLE can involve incomplete usage; it does not say that should read as a pricing fault. I am lowering severity to low for three reasons. The path needs a Tier-3 anomaly: a successful Azure completion with usage or completion_tokens missing, together with a deployment alias LiteLLM cannot price. If LiteLLM can price the returned alias, _hidden_params.response_cost is set and the MALFORMED branch is reached. Finally, nothing is lost: the call is recorded, no content is parsed or dispatched, and the failure is a misleading user message plus a SUCCESS status on the audit row.

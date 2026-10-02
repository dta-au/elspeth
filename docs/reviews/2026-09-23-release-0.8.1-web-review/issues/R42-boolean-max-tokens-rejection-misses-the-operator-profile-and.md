# R42. Boolean `max_tokens` rejection misses the operator profile and LLM source boundaries (pre-existing)

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Pricing, planner failures and tutorial run |
| Review line | seams |
| Pre-existing (touches lines outside the window) | yes |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-03-pricing#3 |

## Finding

- **Location:** `src/elspeth/core/llm_profiles.py:130`, `plugins/sources/llm/config.py:81,322`.
- **Wrong:** b97358ab6 added the `mode='before'` bool rejection only to `LLMConfig` and `QueryDefinition`. `ELSPETH_WEB__LLM_PROFILES` with `"max_tokens": true` loads as `1`, and lowering then hands the node an int, so every call gets a 1-token budget. The adjacent `temperature` field gained `strict=True` in the same window.
- **Fix:** Apply the same rejection, or `strict=True`, to both fields.
- **Sources:** seam-03-pricing#3.
- **Verifier notes:** The input is operator config and the trigger is a narrow typo.


## Source findings and verification

### seam-03-pricing#3: Boolean max_tokens rejection misses the operator profile and LLM source boundaries; profile lowering converts true to 1 first

- **Reported at:** `src/elspeth/core/llm_profiles.py:130`; reviewer severity medium; category half-wired-change; diff-anchored False.
- **Summary:** b97358ab6 added reject_boolean_max_tokens only to LLMConfig (transforms/llm/base.py:374) and QueryDefinition (multi_query.py:184). LLMProfileSettings.max_tokens (line 130, lax int; the adjacent temperature field gained strict=True in this window) and LLMSourceConfig.max_tokens (plugins/sources/llm/config.py:81) still turn True into 1. Web authors cannot set max_tokens because it is a private profile field, so on the web the budget always comes from the unguarded profile, and lowering hands the node an int 1.
- **Failure scenario:** ELSPETH_WEB__LLM_PROFILES='{"fast": {"provider": "openrouter", ..., "max_tokens": true}}' loads without error. Every web LLM transform and source bound to 'fast' sends a 1-token output budget. On an Azure reasoning deployment max_completion_tokens=1 is spent before any visible output, so every call reads as an empty completion.
- **Evidence:** Measured on the worktree: LLMProfileSettings(..., max_tokens=True).max_tokens gives 1; lower_llm_profile_options gives executable max_tokens=1 (int); OpenRouterLLMSourceConfig(..., max_tokens=True).max_tokens gives 1. Control: OpenRouterConfig(..., max_tokens=True) raises 'max_tokens must be an integer, not a boolean'.
- **Suggested fix:** Apply the same mode='before' boolean rejection, or strict=True, to LLMProfileSettings.max_tokens and LLMSourceConfig.max_tokens, including the Gateway source override at sources/llm/config.py:322.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. I checked the claimed path at 74c0ce0db, and it is reachable with nothing blocking it. b97358ab6 added the mode="before" boolean rejection only to LLMConfig (base.py:366-372) and QueryDefinition. Its commit message scopes the fix to "node and query authoring boundaries". LLMProfileSettings.max_tokens (llm_profiles.py:130) is still a lax `int | None`, while `temperature` next to it (line 131) is strict=True. On the web, settings_from_env (web/config.py) decodes ELSPETH_WEB__LLM_PROFILES with json.loads, so a JSON `true` arrives as Python True. It then goes through WebSettings(**kwargs) in lax Python mode, which turns True into 1. RuntimeLLMProfile.from_settings (llm_profiles.py:226) copies ("max_tokens", 1) into provider_options. lower_llm_profile_options (llm_profiles.py:~281) merges those options into the executable config with executable.update(profile.provider_options). By then the value is the int 1, so the LLMConfig before-validator has no bool left to reject. max_tokens is listed in LLM_PROFILE_PRIVATE_FIELDS (line 62), so an author can never supply their own value on a profiled node. The batch path, core/config.py:2718 LLMProfileSettings(**profile_dict), coerces the same way. LLMSourceConfig.max_tokens (sources/llm/config.py:81) and the Gateway override (line 322) are also lax int, and source.py:224/343 passes the value straight to the provider call. I lowered the severity from medium to low. On the profile path the input is operator-owned config, and the operator would have to type a literal `true` where a number belongs, which is a narrow typo. The finding is still real: the same class of input is rejected on the node boundary and accepted on the profile and source boundaries. The adjacent temperature field in the same model got strict=True, which shows the inconsistency. The result also fails silently: every call gets a 1-token budget instead of failing at startup.

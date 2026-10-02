# R46. The Azure Search rejection text still uses the LLM provider/model/pacing repair wording

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Plugin policy (`azure_ai_search`) |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-19#2 |

## Finding

- **Location:** `src/elspeth/web/plugin_policy/validation.py:490-500,545` and `:41-44`.
- **Wrong:** `azure_ai_search` is not in `_STORAGE_PROFILED_COMPONENT_KINDS`, so an authored `endpoint` is explained as an LLM provider binding.
- **Fix:** Add wording about the service binding.
- **Sources:** be-19#2.
- **Verifier notes:** The `private_profile_option` arm is effectively unreachable for Azure, because the schema check fires first.


## Source findings and verification

### be-19#2: Azure Search rejection prose still uses LLM provider/model/pacing repair wording

- **Reported at:** `src/elspeth/web/plugin_policy/validation.py:545`; reviewer severity low; category half-wired; diff-anchored True.
- **Summary:** The window added an Azure arm to the lowering-error dispatch but did not route azure_ai_search away from the LLM-family prose in the unexpected-option branch (validation.py:490-500). The dispatch exists (comment at :38-40) so that non-LLM profiled plugins get repair language the planner can act on.
- **Failure scenario:** A planner authors {profile: search-prod, index: docs, endpoint: https://x.search.windows.net}. The finding reads "option(s) not authorable on a profile-bound node: ['endpoint']. The operator profile supplies provider/model/credential/pacing settings — remove them." The option is named, so repair still works, but the explanation describes an LLM provider binding rather than the search-service binding.
- **Evidence:** validation.py:41-44 _STORAGE_PROFILED_COMPONENT_KINDS lists only aws_s3 and textract; validation.py:497-500 is the fallback wording; profiles.py:1515 adds azure_ai_search to the profiled roster in this window.
- **Suggested fix:** Add service-binding wording for PluginId('transform','azure_ai_search') (e.g. 'the operator profile supplies the Azure AI Search endpoint and authentication') in the unexpected and private_profile_option arms.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding. The path is reachable with the stated inputs. The Azure AI Search public profile schema leaves out every private binding option and sets additionalProperties False. So a node carrying {profile, index, endpoint} fails schema validation, and `endpoint` is reported as unexpected. azure_ai_search is not in _STORAGE_PROFILED_COMPONENT_KINDS, so the message falls through to the LLM-family wording "provider/model/credential/pacing settings". Nothing earlier in the path strips the option or rewrites the message. _normalized_profile_findings only changes error_code. No other module consumes AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES, so no earlier guard exists. The finding understates one point. The Azure lowering code does raise private_profile_option (profiles.py:1316-1317), but the schema check fires first (validation.py:476-509, `continue`), so that arm's wording (validation.py:529-539) is effectively unreachable for Azure Search. The live defect is the unexpected-option arm. Severity stays low. The message names the offending option and tells the planner to remove it, so the repair loop still converges. Only the explanation is wrong: it describes an LLM provider/model/pacing binding, not the search service's endpoint and authentication.

# R44. The contract comment says the private set is shared with the plugin, but the plugin does not use it

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Plugin policy (`azure_ai_search`) |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-04-plugin-policy#3 |

## Finding

- **Location:** `src/elspeth/contracts/azure_ai_search.py:3-4`.
- **Wrong:** Only `web/plugin_policy/profiles.py` imports `AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES`. The plugin's unit test pins it (`test_ai_search.py:82-88`).
- **Fix:** Say the web policy layer owns the set and the plugin test pins it.
- **Sources:** seam-04-plugin-policy#3.


## Source findings and verification

### seam-04-plugin-policy#3: Contract comment claims the private set is shared with the plugin; the plugin does not use it

- **Reported at:** `src/elspeth/contracts/azure_ai_search.py:3`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** The module comment says 'Shared by the plugin and the web policy layer so the two cannot drift', but only web/plugin_policy/profiles.py imports AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES. plugins/transforms/azure/ai_search.py does not; only its unit test does.
- **Failure scenario:** A maintainer trusts the comment and assumes the plugin enforces or derives from the split. They add a binding option to the plugin without touching the contract, expecting the shared constant to keep both sides aligned. The web tier then silently exposes the new option (see F1).
- **Evidence:** grep -rn AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES src/ -> contracts/azure_ai_search.py:5 and web/plugin_policy/profiles.py:31,1223,1316 only.
- **Suggested fix:** Reword to say the web policy layer owns the set and the plugin's test pins it to real options, or have the plugin actually derive something from it.
- **Verifier (trace):** upheld, confidence medium, severity low. The factual core holds at 74c0ce0db. src/elspeth/contracts/azure_ai_search.py:3-4 says the set is "Shared by the plugin and the web policy layer so the two cannot drift". No production plugin code reads it. The only source consumer is web/plugin_policy/profiles.py (import at :31, used at :1223 and :1316). src/elspeth/plugins/transforms/azure/ai_search.py never imports it; the plugin marks itself operator-bound only through `web_config_authority = WebConfigAuthority.OPERATOR_PROFILED` (ai_search.py:90-92). So "shared by the plugin" is imprecise: the thing that shares it is the plugin's unit test, not the plugin.

The finding overstates the harm, though. The comment's "cannot drift" claim is partly backed by that test. tests/unit/plugins/transforms/azure/test_ai_search.py:82-88 checks that every private name is a real field of AzureAISearchConfig, and that the set exactly matches a literal list. Renaming or removing a private option therefore fails a test, so that direction of drift is guarded. The failure scenario the reviewer gives (a new sensitive option added to the plugin that the web tier then exposes) is the other direction. An accurate comment would not prevent it, because the plugin has nothing it could derive privacy from. That risk belongs to F1, not to this comment. What remains is a misleading comment with no runtime effect: low severity, close to a documentation nit, but not wrong.

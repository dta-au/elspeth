# R45. Profiled assistance replaces the plugin's operational hints instead of extending them

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Plugin policy (`azure_ai_search`) |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-04-plugin-policy#5 |

## Finding

- **Location:** `src/elspeth/web/plugin_policy/profiles.py:1653-1663` versus `plugins/transforms/azure/ai_search.py:181-187`.
- **Wrong:** On the web, the five plugin hints are replaced by three about profile binding. None of the five names a private option. The hint about the portal-wizard index field names (chunk and chunk_id) appears nowhere on the web surface. **Scenario:** a wizard-built index gets failed or quarantined rows.
- **Fix:** Append the plugin's hints that do not depend on the binding after the profile hints.
- **Sources:** seam-04-plugin-policy#5.
- **Verifier notes:** With the default hybrid mode, the vector field most likely fails before any `missing_content` skip. The keyword-mode path is as described.


## Source findings and verification

### seam-04-plugin-policy#5: Profiled assistance replaces rather than extends the plugin's binding-neutral operational hints

- **Reported at:** `src/elspeth/web/plugin_policy/profiles.py:1653`; reviewer severity low; category teaching-drift; diff-anchored True.
- **Summary:** The azure_ai_search arm of public_assistance (1653-1663) returns three binding-framed hints and drops the plugin's own hints at ai_search.py:181-187: the portal import-wizard index needs chunk/chunk_id, and top_k/min_score/on_no_results interact. Neither is binding-specific. The vectorizer rule survives in the search_mode field description; the wizard field-name hint does not survive anywhere on the web surface.
- **Failure scenario:** A web author asks for RAG over an index built with the portal 'Import and vectorize data' wizard. The planner keeps field_content=content and field_id=id, every hit is skipped as missing_content, and every row is quarantined under the default on_no_results=quarantine. The YAML/MCP surface's assistance would have taught the fix.
- **Evidence:** Raw hints at plugins/transforms/azure/ai_search.py:181-187; replacement at web/plugin_policy/profiles.py:1653-1663; field_content knob description in the golden says only 'Retrievable index string field holding the chunk text.' with default content.
- **Suggested fix:** Append the plugin's binding-neutral hints (full_assistance.composer_hints minus any that name endpoint or credential options) after the profile hints instead of replacing them.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding. On the web path, `get_plugin_assistance` (web/composer/tools/generation.py:2211-2213) passes the plugin's raw assistance through `catalog.project_agent_assistance` (web/catalog/policy_view.py:208-222). Whenever an Azure Search profile alias is usable, that projection calls `public_assistance`, and at profiles.py:1653-1663 this builds a brand-new PluginAssistance with three profile-framed hints. The plugin's own five hints (ai_search.py:181-187) are thrown away. None of those five hints names a private binding option (endpoint, api_key, use_managed_identity, client_id or api_version). So the commit's stated aim for d3dba8a9b ("names no private option, not even in prose") did not require dropping them. Nothing else on the web surface carries them either:
- the public_schema composer_hints (profiles.py:1301-1305) are also profile-only;
- the `field_content` description (ai_search.py:46) says only "Retrievable index string field holding the chunk text.";
- no teaching markdown under src/ mentions `azure_ai_search`;
- docs/agents/recent-code-hints.md records no ruling on this.
The window introduced both sides: 2b897a957 added the plugin hints and d3dba8a9b added the replacement 40 minutes later, so the finding is anchored to the diff. One correction to the failure scenario: under the default search_mode=hybrid with field_vector=contentVector, a portal-wizard index (chunk, chunk_id, text_vector) most likely fails the Azure query on the vector field before any `missing_content` skip can happen. The `missing_content` → quarantine path at azure_search.py:485-487 holds in keyword mode. Either way the result is failed or quarantined rows, and the dropped plugin hint is what would have taught the fix. I am keeping the severity at low: this is teaching drift, not a correctness or security defect.

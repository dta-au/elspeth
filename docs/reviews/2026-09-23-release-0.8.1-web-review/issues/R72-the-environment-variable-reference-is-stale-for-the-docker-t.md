# R72. The environment-variable reference is stale for the Docker timeout, the transport ceiling and `EXECUTION_RATE_LIMIT`

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Deploy configuration and docs |
| Review line | backend, seams |
| Pre-existing (touches lines outside the window) | yes |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-09-deploy-config#3, be-02#2 |

## Finding

- **Location:** `docs/reference/environment-variables.md:161` (pre-existing), `docs/guides/docker.md:79-80`, and `src/elspeth/web/config.py:503-511`.
- **Wrong:** The reference still says 180.0 and has no rows for the transport ceiling or headroom. An operator who sets 300 without the 360 ceiling gets a startup error; the message names the fix. `ELSPETH_WEB__EXECUTION_RATE_LIMIT` (24dccdd9e) is documented nowhere. The `config.py` comment names only the LLM limiter keys, while other plugins use `azure_ai_search:<hostname>`, `web_scrape`, `blob_fetch` and `dataverse_*`.
- **Fix:** Update the value, add rows for the ceiling, the headroom and `EXECUTION_RATE_LIMIT` (its JSON shape, per-run scope and limiter keys), and pin them in `test_deployment_platform_docs.py`.
- **Sources:** seam-09-deploy-config#3, be-02#2.
- **Verifier notes:** be-02#2's "N concurrent runs × 600/min" scenario is refuted. Web runs are serialised by `ThreadPoolExecutor(max_workers=1)` (`service.py:1091`), so multiplication happens only across processes or replicas. A replica-local `persistence_path` would not share buckets either.


## Source findings and verification

### seam-09-deploy-config#3: Reference says the Docker timeout is 180.0; it is now 300.0, which also needs a ceiling not listed

- **Reported at:** `docs/reference/environment-variables.md:161`; reviewer severity low; category stale-doc / half-wired teaching; diff-anchored False.
- **Summary:** The reference table still says the standalone Docker example uses 180.0, while docker.md:79-80 uses 300.0 plus COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=360.0. The table has no row for the ceiling or headroom. The new EXECUTION_RATE_LIMIT (24dccdd9e) is documented nowhere.
- **Failure scenario:** An operator configuring from the reference sets COMPOSER_TIMEOUT_SECONDS=300 to match docker.md without the ceiling. The default ceiling 300 minus headroom 30 is 270, so _validate_composer_timeout_transport_headroom fails and the service crash-loops. An operator hitting the 60-rows/minute web-run limit has no documented knob.
- **Evidence:** environment-variables.md:161. docker.md:79-80. config.py:73-74 defaults and 1178-1198 validator. No ELSPETH_WEB__EXECUTION_RATE_LIMIT or execution_rate_limit in docs/ or deploy/.
- **Suggested fix:** Update the example value, add rows for the transport ceiling, headroom and EXECUTION_RATE_LIMIT (JSON RateLimitSettings object), and pin them in test_deployment_platform_docs.py.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this finding, and I confirmed it at 74c0ce0db. Commit fa26f34b8 ("allow five minutes for Composer requests", inside the window) changed the Docker example in docs/guides/docker.md to 300.0 and added the 360.0 ceiling. It did not touch docs/reference/environment-variables.md, which still says the example uses 180.0. The reference table also has no row for the transport ceiling or the headroom. The failure path is real: an operator who sets a 300 s timeout and leaves the ceiling at its default gets a startup ValueError. That error message names the fix, so the operator is not stuck, and the severity stays low. The claim about execution_rate_limit also holds: a grep of docs/, deploy/, CHANGELOG.md and README.md returned no matches (exit 1). The same grep over config.py did match, so the search itself works. One caveat: the reference table leaves out many optional settings (for example write_rate_limit_per_minute and composer_planner_*). The missing EXECUTION_RATE_LIMIT row is therefore a documentation gap, not a strict half-wiring defect. The 180.0 value, however, is a clear stale-doc defect.

### be-02#2: execution_rate_limit undocumented; comment omits per-run scope and non-LLM keys

- **Reported at:** `src/elspeth/web/config.py:503`; reviewer severity low; category supportability; diff-anchored True.
- **Summary:** The new operator knob has no entry in docs/reference/environment-variables.md. Its comment says limiters are keyed by provider type (true only for LLM transforms) and does not say that each run gets a fresh in-memory RateLimitRegistry.
- **Failure scenario:** An operator sets services.openrouter.requests_per_minute=600 to stay under a provider quota. service.py:3636 builds a new RateLimitRegistry per run, and with no persistence_path the buckets live only in that run, so N concurrent web runs send up to N x 600/min. An operator trying to throttle azure_ai_search, web_scrape or the Azure safety transforms has no documented key: they use azure_ai_search:<hostname>, web_scrape, blob_fetch, dataverse_* and the plugin self.name.
- **Evidence:** grep -rn EXECUTION_RATE_LIMIT docs deploy returns nothing. llm/transform.py:1733-1742 holds the LLM keys; azure/ai_search.py:138 builds limiter_service_name = f'azure_ai_search:{hostname}'; execution/service.py:3636 builds RateLimitRegistry(rate_limit_config) inside the per-run body.
- **Suggested fix:** Add an ELSPETH_WEB__EXECUTION_RATE_LIMIT entry to environment-variables.md giving the JSON shape, the per-run scope (persistence_path under data_dir shares buckets across runs) and the full limiter-key list. Correct the config.py comment to match.
- **Verifier (trace):** upheld, confidence medium, severity low. The finding holds only in part. Confirmed: ELSPETH_WEB__EXECUTION_RATE_LIMIT is a real operator knob. 24dccdd9e added it as a JSON field in _JSON_OBJECT_FIELDS (config.py:1446). It has no entry in docs/reference/environment-variables.md or anywhere else under docs/ or deploy/, although that reference documents every other JSON web knob (LLM_PROFILES, PLUGIN_CONTROL_MODES, BEDROCK_GUARDRAIL_DEFAULT_PROFILES, AZURE_SEARCH_PROFILES and so on). The config.py comment is also incomplete. It names only the four LLM provider keys, but azure_ai_search uses a different key, 'azure_ai_search:<hostname>' (ai_search.py:138), so an operator has no documented key for non-LLM limiters. Refuted: the headline failure scenario, "N concurrent web runs send up to N x 600/min". ExecutionService runs pipelines on ThreadPoolExecutor(max_workers=1) (service.py:1091, and the class docstring at 1036-1037 says so). Within one process web runs are serialised, so the fresh RateLimitRegistry built per run (service.py:3636, closed at 4331-4332) does not multiply the rate. The worst case in one process is a small burst at a run boundary. Multiplication happens only across processes or replicas. In that case a persistence_path on replica-local disk would not share buckets either, so the suggested "persistence_path shares buckets" fix is also wrong for multi-replica deployments. The per-run scope matches CLI semantics, and the comment explicitly says this is the same block as the CLI's `rate_limit:`, so leaving it out does not make the comment false. What survives is a documentation and supportability gap with no behavioural failure, so it stays low severity.

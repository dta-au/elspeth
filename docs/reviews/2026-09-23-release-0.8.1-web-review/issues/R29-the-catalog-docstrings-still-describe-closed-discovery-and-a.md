# R29. The catalog docstrings still describe closed discovery and all-or-nothing deferral

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-08#2 |

## Finding

- **Location:** `src/elspeth/web/composer/planner_authoring_aids.py:1976-1983` and `pipeline_planner.py:455-458`.
- **Wrong:** They say "neither turn is needed to bind a slug" and "models_by_provider empty". After b08fe591d, partial per-provider deferral is the default. The check at `pipeline_planner.py:465` is still correct.
- **Fix:** Describe per-provider deferral.
- **Sources:** be-08#2.


## Source findings and verification

### be-08#2: Catalog docstrings still say it closes discovery and defers all-or-nothing

- **Reported at:** `src/elspeth/web/composer/planner_authoring_aids.py:2036`; reviewer severity low; category stale-docstring; diff-anchored True.
- **Summary:** The per-provider, largest-first deferral in b08fe591d left two docstrings stale. planner_model_catalog (planner_authoring_aids.py:1976-1983) still says 'neither turn is needed to bind a slug'. pipeline_planner.py:455-458 still says the over-budget arm has 'models_by_provider empty'. On the shipped inventory, partial deferral is now the default.
- **Failure scenario:** A maintainer trusts the pipeline_planner docstring and simplifies the _aid_supplied_information check to 'if catalog["models_by_provider"]'. The manifest then marks model.catalog as supplied while the azure and openrouter slugs are absent. The planner is steered away from list_models, binds a slug from memory, and preflight rejects it.
- **Evidence:** The current code at pipeline_planner.py:465 is correct, because it still requires omitted_provider_count == 0. Only the text is wrong. Measured on the shipped inventory: models_by_provider={'bedrock': 3 ids}, and models_omitted is azure plus openrouter.
- **Suggested fix:** Rewrite both docstrings to describe per-provider deferral. Carried lists are complete, deferred providers are listed in models_omitted, any deferral keeps model.catalog discoverable, and binding a slug from a deferred provider costs a list_models turn.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed the finding at 74c0ce0db. The code is correct; the two docstrings are stale. Commit b08fe591d replaced the all-or-nothing fallback (`models_by_provider = {}` with a marker for every provider) with per-provider deferral that drops the largest lists first. The same commit cut the budget from 32 KiB to 8 KiB. Neither docstring was updated. The pipeline_planner.py docstring still describes the over-budget case as "present with models_by_provider empty and a models_omitted marker per dropped provider". That is now false: in the partial case, models_by_provider is non-empty while providers are still deferred. The planner_model_catalog docstring still says "neither turn is needed to bind a slug", but binding a slug from a deferred provider now needs list_models. The guidance text the planner actually receives (_MODEL_CATALOG_GUIDANCE, lines 2059-2075) is accurate, so the planner is not misled. The only risk is to maintainers, and the check at line 465 is still correct. That keeps the severity low.

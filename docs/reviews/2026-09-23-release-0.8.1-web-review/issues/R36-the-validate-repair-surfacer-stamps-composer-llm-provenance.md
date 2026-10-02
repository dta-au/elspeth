# R36. The `/validate` repair surfacer stamps `composer_llm` provenance on surfaces owed by a state revert

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Interpretation events |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-25#1 |

## Finding

- **Location:** `src/elspeth/web/composer/service.py:3073-3084` (blamed to 889913b1a) and `execution/routes.py:969,991`.
- **Wrong:** The backstop always passes `COMPOSER_LLM` and the current model, provider and skill hash. **Scenario:** the revert commits R (`state.py:767-775`) and its post-commit surfacing fails (`state.py:690-698`). A later `/validate` then writes the missing events as `composer_llm` with the current model. Which provenance gets recorded depends on which repair runs first.
- **Fix:** Add a closed-enum origin for the `/validate` repair with NULL provenance (contract, CHECK, frontend type, epoch), or derive the origin from the validated state.
- **Sources:** be-25#1. Sibling of R35.
- **Verifier notes:** Model stamping on this path predates the window. The window added the explicit origin column and did not convert this caller.


## Source findings and verification

### be-25#1: /validate repair surfacer stamps composer_llm provenance on surfaces owed by a server route (state revert)

- **Reported at:** `src/elspeth/web/composer/service.py:3078`; reviewer severity medium; category audit-integrity; diff-anchored True.
- **Summary:** 889913b1a introduced InterpretationSurfaceOrigin so rows record what raised a surface, and moved state revert, YAML import and E2E seed to server origins with NULL LLM provenance. It tagged ComposerServiceImpl.surface_pending_interpretation_reviews unconditionally as COMPOSER_LLM, using the current deployment's self._model (as both identifier and version), provider-or-'unknown' and skill hash. That method is the /validate backstop (execution/routes.py:969, :991), which calls no LLM and repairs surfacing debt on any state, whichever route created it. The revert route surfaces after its commit (state.py:791), and state.py:690-698 documents that an attempt can die in between.
- **Failure scenario:** The user reverts to state S. revert_state_for_guided_operation commits the reverted state R, which has pending interpretation_requirements, and terminalizes the operation. The surfacing call at state.py:791 then fails (transient DB error on the repair lease, worker restart or cancellation), and the route returns 5xx. The user reloads, and the SPA calls POST /validate?state_id=R. execution/routes.py:991 reaches service.py:3073, which inserts the missing events with surface_origin='composer_llm', model_identifier=model_version=<current composer_model>, provider=<current provider or 'unknown'> and composer_skill_hash=<current skill hash>. The audit trail then claims that the composer LLM, under the current skill, raised the revert's review surfaces, although no LLM ran. If the guided replay repair (_repair_reverted_surfacing_debt, state.py:689-716) had run first, the same sites would carry state_revert and NULL provenance, so the recorded provenance depends on which repair ran first. The user later resolves the user_approved rows, which carries the false origin into the hashed resolved row.
- **Evidence:** service.py:3073-3084 at the pin: `surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM, model_identifier=self._model, model_version=self._model, provider=self._availability.provider or "unknown", composer_skill_hash=self._composer_skill_hash`. git blame shows line 3078 is 889913b1af (in the window). execution/routes.py:944-954 says 'both arms run the backend surfacer in repair mode over the state they are about to validate'. state.py:148-153 shows that the revert's own surfacing uses STATE_REVERT with None provenance. state.py:690-698 documents the post-commit gap in which surfacing dies. The InterpretationSurfaceOrigin docstring (composer_interpretation.py:96) scopes COMPOSER_LLM to the compose loop, its repair pass or a guided commit.
- **Suggested fix:** Stop hard-coding the origin in the backstop. Either add a closed-enum origin for the /validate repair (for example VALIDATE_REPAIR with NULL provenance: contract, the ck_interpretation_events_surface_origin CHECK, the frontend type and an epoch bump), or derive the origin from the validated state's recorded provenance and pass COMPOSER_LLM only for compose-produced states. Add a regression test in which the revert's post-commit surfacing fails, then /validate runs, and assert that the resulting rows are not composer_llm.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. The path is reachable at the pin as described. At service.py:3078 (blamed to 889913b1a, in the window), ComposerServiceImpl.surface_pending_interpretation_reviews always passes surface_origin=COMPOSER_LLM, the current deployment's self._model, provider-or-'unknown' and the skill hash. It never looks at which route produced the state. Both arms of /validate (execution/routes.py:969 and :991) call this method with only_missing_evidence=True for any state, and nothing checks the state's origin first.

The revert route is the one server route whose surfacing is not atomic. YAML import builds its events inside the save transaction with prepare_pending_interpretation_event_drafts_for_state(YAML_IMPORT) at state.py:953-960. Revert commits R in revert_state_for_guided_operation (state.py:767-775), leaves the lease guard, and only then calls _surface_reverted_interpretation_reviews (state.py:790-794). That function takes a fresh COMPOSE lease (state.py:135-141), which is its own point of failure. The route's own docstring (state.py:690-698) records that an attempt can die in that gap.

Evidence is keyed per composition_state_id (_surfaced_evidence_keys(current_state_id=...), service.py:2195-2200). R is a new state id, so all of its surfaceable sites count as missing. If the SPA validates R before anyone retries the revert, the backstop writes those sites as composer_llm with the current model's provenance. If the revert is replayed first, the same sites get STATE_REVERT with NULL provenance (state.py:689-716). So the recorded provenance depends on which repair runs first.

The enum docstring (composer_interpretation.py:96-98) says COMPOSER_LLM means 'the compose loop, its repair pass, or a guided commit', and that revert debt is STATE_REVERT. The backstop therefore contradicts the contract the same commit introduced. I found no recorded ruling in recent-code-hints about the /validate backstop's origin.

I lowered the severity because stamping the model name on this path is not new. Before the window, lines 3079-3082 (f3db92c006) already stamped self._model on backstop repairs of revert debt, while the revert route stamped 'state_revert' labels. The window added an explicit origin column and left this one caller unconverted, so the change is half-wired and not a regression. The trigger is also narrow: the revert's post-commit surfacing has to fail, and then /validate has to run before the revert is retried.

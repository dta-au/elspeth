# Re-anchor: wave 3 lanes (CDA-10, CDA-11, CDA-12, CDA-13)

- Audit baseline: `main` = `1a7ff0e63`.
- New base: `integration/composer-remediation` = `8f5ca4292` (`git rev-list --count main..HEAD` = 254).
- Method: read-only. No tests run, no tracked file edited. All paths below are relative to the
  `composer-remediation` worktree. "Verified" means I read the code on the new base; "inferred"
  is marked as such.
- Summary:

| Lane | Status | One line |
| --- | --- | --- |
| CDA-10 | PARTLY-FIXED | `canonical_schema` now names two shapes (2776b8620); notice repetition, key naming, named-source blob rejection, discovery-failure ordering and both tool descriptions are untouched. Brief's `_dispatch.py` pointer is wrong. |
| CDA-11 | STILL-PRESENT | Handler unchanged. Brief understates the blast radius: the planner reads the result shape. |
| CDA-12 | PARTLY-FIXED (and CHANGED-SHAPE) | 0.8.12 added a text-only surface orientation (e963a5b62); the structural fix is all still to do, and the new orientation text, its two pinned tests and the byte pin now have to be unwound by this lane. Every acceptance number is stale. |
| CDA-13 | STILL-PRESENT | Nothing landed. S3 patch: 5 of 7 files apply, 2 fail (`proposal_blob_refs.py`, `pipeline_capabilities.md`) and both need a real rewrite, not just a rebase. |

---

## CDA-10: PARTLY-FIXED

### 1. Finding status, per part

**(a) Notice repeats and is a command: STILL-PRESENT (verified).**

- `src/elspeth/web/composer/pipeline_planner.py:249`
  `_ALL_INFORMATION_GAPS_CLOSED_NOTICE: Final[str] = "All declared information gaps are closed; emit the terminal proposal now."`
  (string identical to main).
- `pipeline_planner.py:5530-5531`, the last statement of the discovery loop body, with no
  once-per-request guard:

  ```python
  if pending_information and all(information_manifest.supplies(key) for key in pending_information):
      messages.append({"role": "user", "content": _ALL_INFORMATION_GAPS_CLOSED_NOTICE})
  ```

**(b) `canonical_schema` rejection names the offending key: PARTLY-FIXED by `2776b8620`
("make a canonical_schema rejection say what to repair").**

- Done: a non-object `pipeline` reports `{path: pipeline, rule: type, constraint: object}` plus a
  detail (`_terminal_payload_shape_violations`, `pipeline_planner.py:2861-2872`); a `oneOf` over
  bare `required` alternatives names the keys it chooses between
  (`_exclusive_required_detail`, `:2840`; used at `:2907-2910`).
- Not done: the brief's actual ask. `_structural_schema_violations` (`:2875-2912`) still emits only
  `path` (the parent location), `rule` and a scalar `constraint`. For an `additionalProperties`
  violation the unexpected key is never named, even when it is a property name in the schema's own
  vocabulary. The value-free docstring the brief cites is at `:2876-2882`.
- `_pydantic_schema_violations` (`:2915-2959`) does carry pydantic's `loc`, so an `extra_forbidden`
  from the argument model is located. But the JSON-schema pre-check runs first (`:4884-4899`), so a
  closed-object violation is answered there and never reaches it (verified by reading the branch
  order; not executed).

**(c) Named-source `blob_id` / `inline_blob` gets a specific rejection: STILL-PRESENT (verified).**
No such text exists (`grep "cannot bind uploaded\|named sources cannot"` over
`src/elspeth/web/composer` returns nothing). A named source carrying `blob_id` fails the closed
schema at `tools/sessions.py:2243-2245` (`additionalProperties: False`) and comes back as the
generic `canonical_schema` rejection of (b).

**(d) Discovery failure leads with the tool's own error: STILL-PRESENT, and the brief names the
wrong file.**

- `tools/_dispatch.py` changed by 3 lines since main (an MCP root-authority parameter); nothing
  about ordering.
- The envelope F12 describes ("whole-pipeline errors first, the real error under
  `data.argument_error`") is built by the planner, not by `_dispatch.py`:
  - `pipeline_planner.py:5337-5348` builds the rejection with `validation=current_state.validate()`
    and `argument_error_response(exc)` as data;
  - `src/elspeth/web/composer/provider_discovery_response.py:43-65` renders
    `{"argument_error": entry}`;
  - `provider_discovery_response.py:95-112` (`ClosedProviderDiscoveryEnvelope.to_wire`) writes
    `success`, `validation`, `affected_nodes`, `version`, then `data`.
- `provider_discovery_response.py` has 0 commits since main, so this was already true at the audit
  baseline. The comment at `pipeline_planner.py:5331-5336` (SYS-R3-1) forbids the obvious fix of
  moving the argument rejection under `validation`; the fix must omit or empty the state validation
  for this arm instead.

**(e) Tool descriptions: STILL-PRESENT (verified).**

- `get_plugin_assistance` declaration: `tools/generation.py:2401-2423`. The schema property is
  `plugin_name` (`:2431`); the description still does not say "the argument is `plugin_name`, not
  `name`".
- `patch_output_options` declaration: `tools/outputs.py:390-396`. Description is byte-identical to
  main and does not say where `description` belongs.

### 2. Anchors

| Brief | New base |
| --- | --- |
| `pipeline_planner.py` L217 | `:249` |
| append ~L5138 | `:5530-5531` |
| ~L2742-2749 | `:2875-2882` (`_structural_schema_violations` and its docstring) |
| `tools/generation.py:2382-2392` | `:2401-2423` |
| `tools/outputs.py:370-375` | `:390-396` |
| `tools/_dispatch.py` (ordering) | not there; see (d): `pipeline_planner.py:5337-5348` and `provider_discovery_response.py:95-112` |

### 3. Tests

- `tests/unit/web/composer/test_pipeline_planner.py` exists (11,213 lines; 20 commits since main).
- The brief says 6 tests pin the notice string. On the new base it is **7** (verified by grep of
  the literal):

  | Line | Test |
  | --- | --- |
  | 1657 | `test_discovery_round_uses_real_read_only_tool_then_terminal` |
  | 1874 | `test_alias_metadata_success_resolves_the_canonical_fact_in_the_next_planner_request` (new, from `787b96044`) |
  | 2093 | `test_generic_linear_plan_receives_every_targeted_schema_contract` |
  | 2136 | `test_unsupported_compact_schema_delivers_the_full_admitted_contract` |
  | 2270 | `test_plugin_mentions_do_not_declare_schema_gaps_but_discovery_still_runs` |
  | 9892 | `test_discovery_argument_error_is_recoverable_not_fatal` |
  | 10017 | `test_argument_error_does_not_poison_a_corrected_retry_of_the_same_key` |

  Main had 6 (lines 1565, 1946, 1989, 2123, 9655, 9780).
- `tests/unit/web/composer/test_agent_tooling.py` exists. It has no `argument_error` assertion
  today. The discovery `argument_error` envelope is pinned in `test_pipeline_planner.py` at
  `:9879`, `:9924-9925` and `:11184`, so the ordering test belongs there (or both).

### 4. Brief corrections

1. Change "6 tests" to "7 tests" and add the alias-metadata test.
2. Replace the `tools/_dispatch.py` bullet with `provider_discovery_response.py` plus
   `pipeline_planner.py:5337-5348`; cite SYS-R3-1.
3. Reword the `canonical_schema` bullet: 2776b8620 already covers the JSON-string pipeline and the
   `source`/`sources` `oneOf`. What remains is naming the unexpected key of an
   `additionalProperties` violation when that key is a property name in
   `canonical_set_pipeline_schema()`.
4. Add a byte-budget warning. `get_plugin_assistance` is in the planner palette
   (`capability_skill.py:17-23`), so its description is inside the fixed-scaffolding measure of
   `test_initial_request_declares_supplied_information_and_keeps_discovery_available`
   (`test_pipeline_planner.py:1665`, assertion `:1717`). The comments at `:296-304` and `:320-322`
   record zero headroom after the 0.8.12 pins. Inferred, not measured: any byte added to that
   description fails the test until a named increment is added. `patch_output_options` is not in
   the planner palette and is not affected.
5. The notice is not part of the initial request, so rewording it does not move the byte pin.
6. "First check (S4)" remains open. Verified: the planner's `provider_request`
   (`pipeline_planner.py:3854-3872`) carries `intent`, `current_state`,
   `schema_contract_evidence`, `information_manifest`, `instruction` and optional context blocks,
   and no list of session files. I did not establish what the frontend puts in the message text.
7. 0.8.13 caution (inferred): the context budget re-fits the planner's messages before each call.
   Implement "at most once per request" as request-scoped state, and count copies in the request as
   sent.

### 5. Files a correct fix touches

- `src/elspeth/web/composer/pipeline_planner.py`
- `src/elspeth/web/composer/provider_discovery_response.py` (**not in brief**)
- `src/elspeth/web/composer/tools/generation.py`
- `src/elspeth/web/composer/tools/outputs.py`
- `tests/unit/web/composer/test_pipeline_planner.py` (also the byte pin, **not in brief**)
- `tests/unit/web/composer/test_agent_tooling.py`
- `tools/_dispatch.py`: probably not needed.
- Possible: skill text or `redaction_policy_snapshot.json` if the envelope gate pins the changed
  description or wire keys (not checked).

---

## CDA-11: STILL-PRESENT

### 1. Finding status (verified)

`_execute_list_models` is at `tools/generation.py:2560-2654`. It still discards the context and
reads the bundled catalog:

```python
del context  # unused; signature uniformity with the other handlers.   (:2584)
all_models: list[str] = list(read_litellm_model_list())                 (:2585)
```

The declaration at `:2657-2671` still describes provider counts and litellm slugs.
`git diff main HEAD -- tools/generation.py` has no hunk naming `list_models`.

### 2. Anchors

| Brief | New base |
| --- | --- |
| "the `list_models` handler" | `tools/generation.py:2560` (main: 2539); declaration `:2657` (main: 2637) |
| `LLM_PROFILE_PRIVATE_FIELDS` | `src/elspeth/core/llm_profiles.py:38` (0 commits since main) |

### 3. Tests

- `tests/unit/web/composer/test_list_models_profiles.py` does not exist (expected; new).
- Existing tests the brief does not mention pin today's behaviour (all present on main too):
  - `tests/unit/web/composer/test_model_discovery_scope.py`: 11 parametrised cases driving
    `list_models` through the real planner with `read_litellm_model_list` and `get_catalog_values`
    monkeypatched;
  - `tests/unit/web/composer/test_generation_response_contracts.py:68, :136` (`models_summary`);
  - `test_pipeline_planner.py:1690-1707` asserts `list_models` stays in the palette and the
    `model.catalog` manifest keys.

### 4. Brief corrections

The brief treats this as a one-function change. On both bases it is not (verified by reading):

1. **The planner parses the response.** `pipeline_planner.py:563-584`
   (`_resolved_discovery_information_keys`) asserts `data["truncated"]`, `data["count"]` and
   `data["models"]` on every successful filtered call. A new shape without those keys raises
   `AssertionError` inside a planner turn. `pipeline_planner.py:460-467` keys discovery coverage on
   `provider` and `limit` (`model.catalog.summary`, `model.catalog.list:`, `model.catalog.complete:`).
2. **The response contract is closed.** `tools/_generation_responses.py:164-173`
   (`ModelListResponse`, `ModelProvidersResponse`) and `LIST_MODELS_RESPONSE_CONTRACT` (`:352`)
   must gain a profile shape, or the handler's result is refused at admission.
3. **A second copy of the same catalog exists.** `planner_authoring_aids.py:1994-2073`
   (`planner_model_catalog`) builds the `model_catalog` aid from `read_litellm_model_list()` too,
   and `pipeline_planner.py:587-606` uses it to mark `model.catalog` supplied. If `list_models`
   becomes profile-only and this aid does not, the two disagree. Decide in the brief whether the aid
   is CDA-11's or CDA-12's (CDA-12 already owns "remove the `model_catalog` reference").
4. **Data source.** The handler needs the usable aliases with provider and model. Aliases are
   available as `snapshot.usable_profile_aliases` (used at `planner_authoring_aids.py:986-1004`).
   I did not find an existing accessor that returns provider and model for an alias to the tool
   context; `provider` and `model` are in `LLM_PROFILE_PRIVATE_FIELDS`. Locating or adding that
   accessor is part of the lane (not verified further).
5. **Byte pin.** `list_models` is in the planner palette, so a longer description hits the same
   zero-headroom pin as CDA-10 item 4 (inferred).
6. **Dependency.** "Depends on CDA-02 (same file)": `a2ad64974` ("compare freeform edits with
   request-start state") already changed `diff_pipeline` in `tools/generation.py` for 0.8.12. If
   the CDA-02 re-check finds it fixed, this dependency goes away.
7. The CDA-10 / CDA-11 hunks are still about 150 lines apart (`:2401-2423` and `:2560-2671`).

### 5. Files a correct fix touches

- `src/elspeth/web/composer/tools/generation.py`
- `src/elspeth/web/composer/tools/_generation_responses.py` (**not in brief**)
- `src/elspeth/web/composer/pipeline_planner.py` `:460-467`, `:563-584` (**not in brief**; collides
  with CDA-10, CDA-12, CDA-13)
- `src/elspeth/web/composer/planner_authoring_aids.py` `:1994-2073` if the aid is in scope
  (**not in brief**; collides with CDA-12)
- A profile accessor, probably under `src/elspeth/web/plugin_policy/` or
  `src/elspeth/web/catalog/policy_view.py` (**not in brief**, inferred)
- Tests: `test_list_models_profiles.py` (new), `test_model_discovery_scope.py`,
  `test_generation_response_contracts.py`, `test_pipeline_planner.py` (**last three not in brief**)
- Possible: `redaction_policy_snapshot.json`, wire-parity tests and the frontend
  `toolCallDescriptions.ts`, all of which name `list_models` (not checked for shape pins).

---

## CDA-12: PARTLY-FIXED (CHANGED-SHAPE)

### 1. Finding status

**What 0.8.12 did (`e963a5b62`, "tell the proposal planner which surface it is on"): text only.**

- `PLANNER_TERMINAL_INSTRUCTION` (`pipeline_planner.py:2030-2049`) gained a surface orientation:
  "This is the proposal-only surface… Where the shared rules name set_pipeline, apply them to
  emit_pipeline_proposal. You have no mutation tool (set_source, upsert_node, patch_node_options,
  create_blob), no request_interpretation_review, no request_advisor_hint…".
- `skills/pipeline_composer.md:13-22` gained: "These rules are written for the incremental tool
  loop and are also sent to the proposal-only planner… A rule that names a tool your palette does
  not offer does not apply to you as written… If you are reviewing another model's turn, apply the
  rules for the surface that model is on."
- The changelog states the limit itself (`CHANGELOG.md:205-206`): "The text was not restructured;
  about a third of what the proposal planner receives still describes tools it does not have."

**What is still undone (all verified):**

| Brief item | Evidence on new base |
| --- | --- |
| Planner gets core + planner overlay | `service.py:878` renders one text; `service.py:928` passes `composer_skill_text=self._composer_skill_text` to `PlanningApplication`; `planning_application.py:1116` `rendered_skill = self._composer_skill_text`; `pipeline_planner.py:3876` sends it as the system message |
| `skills/pipeline_planner.md` | does not exist |
| Per-surface skill hash | `pipeline_planner.py:3773` `skill_hash = hashlib.sha256(rendered_skill.encode("utf-8")).hexdigest()`, which today equals the tool-loop hash |
| Planner text archived in `skill_markdown_history` | `service.py:1048-1093` archives `self._composer_skill_text` only |
| Advisor reviews planner turns with planner text and palette | `advisor_checkpoint.py:454` `system_msg = self._composer_skill_text + trigger_instructions` (file has 0 commits since main). The one new sentence in the skill ("If you are reviewing another model's turn…") does not tell the advisor which surface the turn was on |
| R7: remove `model_catalog` reference | still at `skills/pipeline_composer.md:962` |
| R15 Reply Register additions | `## Reply Register` is at `pipeline_composer.md:254`; I found no rule on check/readiness/approval questions or on defect filing (grep, not a full read) |
| R8, R13 aids text | `planner_authoring_aids.py` has 3 commits since main (`cc8d2a834`, `cafec6a9c`, `dd61ae93e`); none removes unavailable-plugin names (commit subjects only; not audited line by line) |
| E3 test | `tests/unit/web/composer/test_surface_teaching_parity.py` does not exist |
| `terminal_only` palette kept | present, `capability_skill.py:191-218` (file unchanged since main); sole caller still passes `"full"` |

### 2. Anchors

| Brief | New base |
| --- | --- |
| `capability_skill.py` ~L193-219 | `:191-218` (unchanged) |
| `pipeline_planner.py:3634` (`tool_surface="full"`) | `:4027` |
| `pipeline_planner.py` ~L3494-3514 (message builder) | `:3875-3883`; `provider_request` at `:3854-3872` |
| `advisor_checkpoint.py` ~L453-454 | `:453-454` (unchanged) |
| `service.py` ~L751-801 | `:859-935` (hash checks `:866-873`, render `:878-879`, advisor owner `:889-897`, `PlanningApplication` `:916-940`); archive `:1048-1093` |
| `planner_authoring_aids.py` L867-873 | the per-query interpretation teaching is at `:889-896` |
| `pipeline_composer.md` ~L892-903, ~L1236 (F5) | model-binding paragraph `:958-968` |
| `test_stated_threshold_data_full_walk.py:214` | `tests/integration/web/composer/test_stated_threshold_data_full_walk.py:214`, same line, same assertion (0 commits since main) |

### 3. Tests

- Exist: `test_skills_loader.py` (0 commits since main), `test_capability_skill_identity.py` (2),
  `test_planner_authoring_aids.py` (3),
  `tests/unit/web/sessions/test_skill_markdown_history_authority.py` (0),
  `tests/unit/web/composer/test_tool_result_envelope_gate.py` (2),
  `scripts/cicd/composer_wire_census.py` (0), `scripts/cicd/generate_skill_inventory.py` (0).
- Missing, as expected: `test_surface_teaching_parity.py`.
- `test_stated_threshold_data_full_walk.py:214` is still
  `assert kwargs["messages"][0] == {"role": "system", "content": composer._composer_skill_text}`.
- **New pinned tests this lane must now change (not in brief):**
  - `test_capability_skill_identity.py:84`
    `test_planner_request_instruction_orients_the_planner_on_its_own_surface` pins the orientation
    sentences and the six tool names in `PLANNER_TERMINAL_INSTRUCTION`;
  - `test_capability_skill_identity.py:125`
    `test_interaction_skill_scopes_review_calls_to_surfaces_that_offer_the_tool` pins six sentences
    in `pipeline_composer.md` that exist only because the planner reads that file;
  - `test_capability_skill_identity.py:60`
    `test_capability_core_names_no_planner_exclusive_terminal_tool` (on main too): the core may not
    name `emit_pipeline_proposal`. This agrees with the brief (the overlay names it) and constrains
    "factor shared rules into the core";
  - `test_pipeline_planner.py:270-303` `_SKILL_REWORK_0812_CANONICAL_BYTES = 3878`. Its comment
    records the operator ruling of 2026-11-10: "the prompt is scheduled for a per-surface
    restructure that is expected to replace this pin." CDA-12 is that restructure, so it owns the
    re-baseline. The brief gives the re-baseline to CDA-13 only.
- Other tests that read `_composer_skill_text` or `rendered_skill` and may need a planner
  counterpart (file list from grep, bodies not read): `test_explicit_provider_custody.py`,
  `test_planner_capacity_and_attachment_clarification.py`, `test_private_received_diagnostics.py`,
  `test_provider_context.py`, `test_provider_quota_end_to_end.py`.

### 4. Brief corrections

1. **S2 prototype patch: `git apply --check` exits 0 for the whole patch and for each file.**

   | File | Result |
   | --- | --- |
   | `src/elspeth/web/composer/prompts.py` | applies |
   | `src/elspeth/web/composer/service.py` | applies, 3 hunks at offset +127 (lines 861, 874, 935) |
   | `src/elspeth/web/composer/skills/pipeline_planner.md` | applies (new file) |

   Applying cleanly is not the same as being correct. The patch still lacks the archive, the
   per-surface hash and the advisor change that the brief already lists. The overlay text it adds is
   the 7.3k condensation the brief says not to use.
2. **The overlay source text changed.** `pipeline_composer.md` is 108,942 bytes (main: 103,817;
   18 commits, +115/−49 lines). `pipeline_capabilities.md` is 33,996 bytes (main: 32,811).
   "Move and rewrite the planner-relevant parts" must start from the new text. That text now also
   holds 0.8.12 hot-path facts and the `file_inject` upload rule (`pipeline_composer.md:726-737`,
   `pipeline_capabilities.md:415`).
3. **Unwind the 0.8.12 orientation.** Once the planner has its own overlay:
   - the "also sent to the proposal-only planner" paragraph (`pipeline_composer.md:13-22`) is false
     for the tool loop and should go;
   - the second half of `PLANNER_TERMINAL_INSTRUCTION` (`pipeline_planner.py:2039-2048`) duplicates
     the overlay and belongs in it. Keep whatever the escape hatch needs: the instruction also rides
     the hatch request, per the e963a5b62 message.
4. **E3 must treat a negated mention as not-callable.** The instruction now names six unoffered
   tools on purpose ("You have no mutation tool (set_source, …)"). The brief's baseline (129
   mentions at HEAD, 23 after R1, "allowlist the core's 3 conditional mentions") was counted on main
   and is stale. Re-count on the new base before fixing a target.
5. **0.8.13 context paragraph.** `pipeline_composer.md:85-90` ("Earlier messages may be omitted to
   fit the context budget…") covers both surfaces; the planner reads its
   `conversation_context` half. The planner overlay must carry it.
   `tests/unit/web/composer/test_planner_context_budget.py` exists (not read for text pins).
6. **Acceptance numbers are stale.**
   - "First-call planner prompt at most 45k tokens on DeepSeek (HEAD 52.5k)": HEAD is larger now.
     The fixed scaffolding grew from at most 135,730 to at most 140,475 canonical bytes by the test's
     own constants (+3,878 and +867), and the production system message is recorded as 139,423
     bytes (`test_pipeline_planner.py:313-314`). Re-measure HEAD before setting the cap.
   - `strict_hits` 32/110 and the S2 A/B figures were measured before the orientation text existed.
     Take a same-day baseline on the new base.
   - Live runs now need `ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS` (required, no default)
     and run under a 960 KiB default request ceiling (`CHANGELOG.md:20-28`).
7. **Skill hash changes are now louder.** 0.8.13 records `composer_skill_hash` changes in the
   changelog. A planner hash distinct from the tool-loop hash is a new public audit fact; the lane
   needs a changelog line (AGENTS.md, compatibility consideration).
8. **Context budget (inferred).** The budgeter never sheds the system skill
   (`CHANGELOG.md:63-64`) and the refusal names the largest request component. A different planner
   system text changes those sizes but not the mechanism; I found no skill-size pin in
   `context_budget*.py`.
9. **Dependencies.** Still after CDA-07 and CDA-10 (`advisor_checkpoint.py`, `pipeline_planner.py`).
   Add: before CDA-13, and settle with CDA-11 who edits the `model_catalog` aid.

### 5. Files a correct fix touches

In the brief and still right: `prompts.py`, `capability_skill.py`, `skills/pipeline_composer.md`,
`skills/pipeline_capabilities.md`, `skills/pipeline_planner.md` (new), `planner_authoring_aids.py`,
`pipeline_planner.py`, `advisor_checkpoint.py`, `service.py`, and the listed tests.

**Not in the brief:**

- `src/elspeth/web/composer/planning_application.py` (`:262`, `:285`, `:1116`, `:1202`): it carries
  the text to the planner. S2 avoided editing it by passing the planner text through the existing
  `composer_skill_text` parameter, which leaves a misnamed parameter.
- `tests/unit/web/composer/test_pipeline_planner.py` (the byte pin; collides with CDA-10, CDA-11,
  CDA-13, CDA-17).
- The two orientation tests in `test_capability_skill_identity.py` (the file is in the brief, these
  cases are not).
- `CHANGELOG.md`.
- Possibly `docs/architecture/composer-remediation/reports/skill-rework-report.md`, which the 0.8.12
  commit cites for the pin (not opened).

---

## CDA-13: STILL-PRESENT

### 1. Finding status (verified)

- Schema: `tools/sessions.py:2236-2241` still says "Values share source's shape minus
  blob_id/inline_blob", and the named-source object (`:2242-2262`) is closed with no `blob_id`.
- Argument model: `redaction.py:1777-1795` `_SetPipelineNamedSourceModel` "deliberately exclude[s]
  `blob_id` and `inline_blob`"; `blob_id` is on `_SetPipelineSourceModel` (`:1843`).
- Serializer: `tools/_common.py:4150-4155` serialises named sources with
  `allow_blob_binding=False`.
- State response: `tools/state_responses.py:378` `_require(value.blob_id is None or not named)` and
  `:386` `_require(not named)`.
- Proposal extractor: `sessions/proposal_blob_refs.py:180-195` reads only `source.blob_id` and
  `source.options` for a model-authored `set_pipeline`.
- `file_inject` (0.8.12) writes whole-file content into a row field. It is not a row source and
  does not make the two-CSV request expressible (read of the changelog and skill passage; not
  executed).

### 2. Anchors

| Brief | New base |
| --- | --- |
| `tools/sessions.py` ~L2188 (named-sources schema) | `:2236` (main: 2184) |
| `tools/sources.py:797` `_resolve_source_blob` | `:799`; signature identical to main, so the prototype's call still fits |
| singular-source resolver call (the model to copy) | `tools/sessions.py:1084-1098` |
| `_serialize_set_pipeline_arguments` | `tools/_common.py:4116` |
| `state_responses.py` named-source check | `:375-388` |
| `redaction.py` `_SetPipelineNamedSourceModel` | `:1777` |
| `named_source` row | `skills/pipeline_capabilities.md:253` |

### 3. Tests

- `tests/unit/web/composer/test_named_sources_blob_binding.py`: does not exist (new; the
  prototype's copy applies cleanly as a new file).
- `tests/unit/web/sessions/test_proposal_blob_refs.py`: exists, and grew in `787b96044` (+114),
  `93721763d` (+32) and `c616c8ea5` (+57). It already has
  `test_named_source_path_sentinels_are_extracted` (`:415`) for server-owned state; add the
  model-authored named cases beside it.
- `tests/unit/web/composer/test_set_pipeline_candidate.py`: both pinned cases still exist,
  `named-blob-backed-round-trip-unavailable` (`:498`) and
  `multiple-sources-with-blob-round-trip-unavailable` (`:523`), under
  `test_no_source_internal_defense_uses_prior_source_shape_for_repair_guidance` (`:527`).
- Byte budget: `test_pipeline_planner.py:1717` in
  `test_initial_request_declares_supplied_information_and_keeps_discovery_available`.
  **The brief's "exceeded by 911 bytes" is stale as a fact, though probably close as a number.**
  S3 measured 135,730 → 136,641 against main's ceiling. The ceiling is now 140,475 by the test's
  constants, again with zero headroom, so the overrun equals whatever the lane adds. Inferred, not
  measured: the same text overruns by about the same 911 bytes; the capabilities paragraph has to be
  rewritten (below), so re-measure. If CDA-12 lands first, the pin is CDA-12's new one.

### 4. Brief corrections

**S3 prototype patch: `git apply --check` exits 1 for the whole patch.** Per file
(`--include=<file>`; negative control: a patch to a missing file exits 1):

| File | Result |
| --- | --- |
| `src/elspeth/web/composer/redaction.py` | applies |
| `src/elspeth/web/composer/tools/_common.py` | applies |
| `src/elspeth/web/composer/tools/sessions.py` | applies |
| `src/elspeth/web/composer/tools/state_responses.py` | applies |
| `tests/unit/web/composer/test_named_sources_blob_binding.py` | applies (new file) |
| `src/elspeth/web/composer/skills/pipeline_capabilities.md` | **fails** at line 256 |
| `src/elspeth/web/sessions/proposal_blob_refs.py` | **fails** at line 138 |

1. **`proposal_blob_refs.py` needs a rewrite, not a rebase.** Four commits changed it since main.
   Verified differences the hunk must respect:
   - every return path now canonicalises:
     `tuple(dict.fromkeys(canonical_resource_uuid(value) or value for value in collected))`
     (`:141`, `:152`, `:179`, `:195`). The prototype returns `tuple(dict.fromkeys(collected))`.
     Keeping that would let a brace- or URN-spelt named `blob_id` skip the ownership lookup at
     `:220-229`;
   - a `ValueError` here is converted to `MalformedProposalBlobReferenceError` (`:213-219`) and
     refused at admission as `proposal_blob_reference_malformed`
     (`tool_batch.py:2409`). The prototype's new `ValueError` messages become user-facing refusal
     causes;
   - an `is_owned_composition_state_authority` branch (`:156-179`) now sits before the
     model-authored branch and already walks `sources.<name>.options`. The named `blob_id` change
     goes in the model-authored branch (`:180-195`) only;
   - `_collect_option_blob_references` (`:79-85`) now treats a nested `blob_id` key inside options
     as a reference (for `blob_rows`). That does not cover a top-level `sources.<n>.blob_id`.
2. **`pipeline_capabilities.md` hunk.** The `named_source` row hunk (`:253`) matches. The paragraph
   hunk fails because the sentence was extended to "…without inline custody fields. `inline_blob`
   sits on `source` beside `options`, never inside it, and a source bound by `inline_blob` or
   `blob_id` authors no `path`." (`:259-261`). Re-place the teaching by hand. If CDA-12 has landed,
   put it in the planner overlay instead (S3 §5 already says so).
3. **Update "it applies cleanly at `58e31760`"** to the table above, and "about 145 changed lines
   across 5 `.py` files" stays right for 4 of the 5.
4. **New result key.** The prototype adds `data.named_source_blobs` to the `set_pipeline` result.
   The skill marks taught result keys (`pipeline_composer.md:583`:
   `tool-data set_pipeline data.source_blob; … set_source_from_blobs data.source_blobs`) and
   `test_tool_result_envelope_gate.py` checks shipped keys against them. Inferred: the new key
   needs a taught marker and gate registration. S3 §4 (its lines 263-268) flags this as "if that
   gate pins data keys"; the brief's file list omits `pipeline_composer.md`.
5. **Admission now runs before staging (0.8.12).** `sessions/mutation_capabilities.py:129, :217`
   and `sessions/proposal_blob_effects.py:56` call the extractor. This makes the "required"
   extractor fix more important, not less: with it missing, a two-file proposal skips the ownership
   check and the pending-proposal retention edge on the new paths too (inferred from the call
   sites; not executed).
6. **Resolver.** `_resolve_source_blob` gained UUID canonicalisation in `787b96044`
   (`sources.py`, 28 lines). The signature is unchanged, so named sources inherit it by reuse.
   Add a test with an upper-case or braced named `blob_id`.
7. `pipeline_custody.py` has 0 commits since main; the "keep `inline_blob` out of named sources"
   condition needs no change there.
8. **Compatibility wording.** D2 says no compatibility. The schema and `named_source` row are
   public authoring surface, so the lane still owes a changelog entry stating the change.

### 5. Files a correct fix touches

In the brief and still right: `tools/sessions.py`, `sessions/proposal_blob_refs.py`,
`tools/_common.py`, `tools/state_responses.py`, `redaction.py`, `skills/pipeline_capabilities.md`,
`test_named_sources_blob_binding.py` (new), `test_proposal_blob_refs.py`,
`test_set_pipeline_candidate.py`, `test_pipeline_planner.py`.

**Not in the brief:**

- `src/elspeth/web/composer/skills/pipeline_composer.md` (taught marker for
  `data.named_source_blobs`), or `skills/pipeline_planner.md` after CDA-12.
- `tests/unit/web/composer/test_tool_result_envelope_gate.py` registration, if required (inferred).
- `tests/unit/web/composer/redaction_policy_snapshot.json`, if the snapshot pins the named-source
  model fields (not checked).
- `CHANGELOG.md`.

---

## Contradictions between 0.8.12 / 0.8.13 and the briefs

1. **Zero-headroom byte pin.** `_FIXED_SCAFFOLDING_MAX_CANONICAL_BYTES` is priced to the byte
   (`test_pipeline_planner.py:296-304`, `:320-322`). Every lane that lengthens a planner-palette
   tool description, the instruction, the core or the aids trips it: CDA-10
   (`get_plugin_assistance`), CDA-11 (`list_models`), CDA-12 and CDA-13. The brief assigns the
   re-baseline to CDA-13 alone. The 2026-11-10 ruling in the same comment expects the per-surface
   restructure (CDA-12) to replace the pin. Recommendation: each lane adds its own measured named
   increment, the file's existing pattern, and CDA-12 does the re-baseline.
2. **`test_pipeline_planner.py` is touched by all four lanes** (and CDA-17). The wave table lists
   the collision only for CDA-13 / CDA-17.
3. **`pipeline_planner.py` is touched by CDA-10, CDA-12 and, unlisted, CDA-11.**
4. **CDA-12 inherits a mitigation to remove.** The orientation in `PLANNER_TERMINAL_INSTRUCTION`
   and `pipeline_composer.md:13-22`, with two pinned tests, exists because the surfaces share one
   text.
5. **CDA-10(b) is half done by 2776b8620.** Reuse `_exclusive_required_detail` and the `detail`
   field of `_SchemaViolation` (`pipeline_planner.py:2695-2701`) to name the key, instead of adding
   a new field. The same `detail` field can carry the named-source `blob_id` message of (c).
6. **Refusals go back to the model before staging (0.8.12), with malformed blob references refused
   as `proposal_blob_reference_malformed`.** CDA-13's extractor change now feeds that admission
   path; see CDA-13 correction 1.
7. **UUID spelling.** Blob selectors accept any UUID spelling and custody canonicalises them.
   CDA-13's extractor and tests must follow the same rule.
8. **Context budget (0.8.13).** Not a contradiction for these lanes, but live acceptance for CDA-12
   and CDA-13 now runs under a required prompt-token cap and a 960 KiB byte ceiling, and a too-large
   request is refused as `context_budget_exceeded` rather than with the old generic failure.

## Not verified

- No test was run. Every statement about what a test would do is from reading.
- Byte overruns are inferred from the test's constants and comments, not measured.
- The body of `planner_authoring_aids.py` was not audited for R8 and R13; only anchors and commit
  subjects.
- What the frontend sends for attached files (CDA-10 "first check").
- Whether `redaction_policy_snapshot.json`, the wire-parity tests or the frontend pin the
  `list_models` or named-source shapes.

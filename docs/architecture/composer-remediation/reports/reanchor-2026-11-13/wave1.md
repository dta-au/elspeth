# CDA re-anchor, wave 1 (CDA-01 .. CDA-06)

- Briefs: `composer-desk-audit` worktree, `docs/development/composer-desk-audit-2026-11-12/work-packages.md` §5, written against `main` = `1a7ff0e63`.
- New base: `integration/composer-remediation` at `8f5ca4292` (254 commits ahead of `main`; `git merge-base main HEAD` = `1a7ff0e63`).
- Method: read-only. Every "verified" item below was read in the new-base worktree or measured with `git diff main HEAD -- <path>` / `git log main..HEAD -- <path>`. No tests were run. Items marked **(inference)** were not read end to end.
- Line numbers are new-base lines unless marked "main".

Files named by the six briefs, change since `main` (measured, `git diff --shortstat main HEAD`):

| File | Changed since main |
|---|---|
| `frontend/src/components/chat/ChatPanel.tsx`, `DecisionPanel.tsx`, `tutorial/TutorialFreeformShell.tsx`, `TutorialFreeformShell.test.tsx`, `stores/subscriptions.ts` | no |
| `web/composer/tutorial_service.py` | no |
| `tests/unit/web/composer/test_tutorial_service.py` | +1 line (settings fixture only) |
| `frontend/tests/e2e/tutorial.spec.ts` | +20/-4 (ARGOnite geometry) |
| `web/composer/tool_batch.py` | +395/-127, 10 commits |
| `web/composer/tools/generation.py` | +230/-28, 10 commits |
| `web/composer/tools/declarations.py`, `web/sessions/models.py` | no |
| `plugins/transforms/report_assemble.py`, `plugins/sinks/document_sink.py`, `plugins/transforms/line_explode.py`, `contracts/plugin_semantics.py`, `tests/unit/plugins/transforms/test_report_assemble.py` | no |
| `docs/reference/environment-variables.md` | +20/-2, 4 commits |
| `web/plugin_policy/compiler.py` | +3 (adds `transform:file_inject`) |
| `web/interpretation_state.py`, `plugins/transforms/llm/multi_query.py`, `tests/unit/web/composer/test_state_multi_query_contract.py` | no |
| `web/composer/planner_authoring_aids.py` | +99/-9, 3 commits |
| `tests/unit/web/composer/test_planner_authoring_aids.py` | +125/-1 |
| `web/composer/boot_probe.py`, `web/composer/capability_skill.py` | no |
| `web/composer/pipeline_planner.py` | +449/-57, 18 commits |
| `tests/unit/web/composer/test_boot_probe.py`, `tests/integration/web/composer/test_boot_probe_production_parity.py` | +1 line each (settings fixture only) |
| `contracts/credential_material.py`, `web/credential_guard.py`, `tests/unit/web/test_credential_guard.py` | no |

All named files exist on the new base except `tests/unit/web/composer/test_diff_pipeline_baseline.py`, which the brief marks as new (absent on main too).

---

## CDA-01 — STILL-PRESENT

**Status (verified).** None of the frontend files the brief edits changed since main, nor did `tutorial_service.py`. The "new skin" touched CSS, `ComposerWorkspace.tsx`, `ArtifactWorkspace.tsx`, `TutorialWorkspaceFrame.tsx` and the like, not the chat components.

- The tutorial still mounts the ordinary chat with only forking disabled: `TutorialFreeformShell.tsx:239` `{chatVisible && <ChatPanelContent composer={composer} allowFork={false} />}`.
- `ChatPanel.tsx` still renders `ChatInput` unconditionally (`:1323`) and passes `onApplySuggestion`, `onAskAboutBlocker`, `onRepairGraph` to `DecisionPanel` unconditionally (`:996-998`).
- `subscriptions.ts:459-463` still injects "Every card acknowledged — your pipeline's ready. Select **Run pipeline** to start it." with no tutorial check (the file has no tutorial branch at all).
- `tutorial_service.py:169` and `:195` still return the same undifferentiated `("tutorial_plugin_set", "The saved tutorial pipeline does not match the supported tutorial plugin set.")`.

**Anchors.**

| Brief | New base |
|---|---|
| `ChatPanel.tsx` `allowFork` | prop `:222`, default `:246`, use `:1155` (unchanged) |
| `TutorialFreeformShell.tsx` | `:239` (unchanged) |
| `subscriptions.ts ~L458-463` | `:459-463` (unchanged) |
| `DecisionPanel.tsx ~L99-101` | `:96-103` `blockedVerbsSentence` (unchanged) |
| `tutorial_service.py L169, L195` | `:169`, `:195` (unchanged); the function is `_tutorial_launch_blocker` at `:154` |

Where each affordance the brief hides actually lives (verified by grep, all unchanged since main):

- textbox / upload / blob-manager toggle / Cancel: `ChatInput`, mounted at `ChatPanel.tsx:1323-1335`.
- suggestion Apply: `handleApplySuggestion` `ChatPanel.tsx:853`, passed at `:996`.
- repair-graph: `handleRepairGraph` `ChatPanel.tsx:~859-862`, passed at `:998`.
- "Ask the composer about this": rendered in `DecisionPanel.tsx:253-256`; handler passed at `ChatPanel.tsx:997`.
- "Edit the list": rendered in `components/chat/InlineSourceCreatedTurn.tsx:147`; handler comment at `ChatPanel.tsx:882`.
- "Use as input": `BlobManager` (`components/blobs/BlobManager.tsx`), mounted at `ChatPanel.tsx:1317` `onUseAsInput={handleUseAsInput}`; reachable only through the `ChatInput` toggle.

**Tests.**

- `tests/unit/web/composer/test_tutorial_service.py` exists (1135 lines).
- `TutorialFreeformShell.test.tsx` exists, 10 tests (`:47-258`); none asserts absence of the textbox, Apply, Ask or Edit. No existing coverage of the brief's new assertions.
- `tests/e2e/tutorial.spec.ts` exists; tests at `:222`, `:261`, `:319`. No absence assertions.
- Pinned tests not named in the brief that the lane will hit:
  - `src/stores/subscriptions.test.ts:675` `expect(lastCall[0]).toContain("Run pipeline")` pins the sign-off copy.
  - `DecisionPanel.test.tsx:167` and `ChatPanel.test.tsx:3721` pin "Share inspect link is blocked. Run pipeline is still available."
  - `tutorial.spec.ts:285` collects `.workspace-authoring-pane :is(h2, p, pre, button, textarea)` for the 16px-gutter measure, and `:309` / `:366` assert the transcript keeps a 160px floor. Removing `ChatInput` changes that pane's contents and height; both tests need a re-run, not an edit by assumption **(inference: not run)**.

**Brief corrections.**

1. The script mode cannot be implemented in `ChatPanel.tsx` alone by hiding elements it renders; three of the listed affordances are rendered by `DecisionPanel.tsx` and `InlineSourceCreatedTurn.tsx`. Either `ChatPanel` withholds the handlers (the components must then tolerate an absent handler; `onEditList` in `InlineSourceCreatedTurn` is the pattern to check) or those files gain a prop. Add both files to the lane's file list.
2. `ChatInput` also carries the **Cancel** control (`onCancel={isComposing ? cancelComposition : undefined}`, `:1326`). Hiding it removes the learner's only cancel during the build. The brief should say whether Cancel stays.
3. The interrupted-request retry (`ComposingIndicator` `onRetryInterruptedRequest` → `retryMessage`, `ChatPanel.tsx:1308-1311`) resends a composer message and is not on the brief's list. Decide whether it counts as a "non-card message path".
4. The ARGOnite skin added a "Conversation" title bar to the authoring pane (`tutorial.spec.ts` comments at `:302-308`; `TutorialWorkspaceFrame.tsx` gained `authoringTitle`). The brief's "copy follows the screen" items should be checked against the new chrome, not the audit screenshots.

**File overlap (must touch).** `ChatPanel.tsx`, `TutorialFreeformShell.tsx`, `subscriptions.ts`, `DecisionPanel.tsx`, `tutorial_service.py`, `test_tutorial_service.py`, `TutorialFreeformShell.test.tsx`, `tests/e2e/tutorial.spec.ts`. **Not in brief:** `InlineSourceCreatedTurn.tsx` (likely), `subscriptions.test.ts`, possibly `DecisionPanel.test.tsx` / `ChatPanel.test.tsx`.

---

## CDA-02 — PARTLY-FIXED (and the brief's design now conflicts with a pinned test)

**Status (verified).** The headline defect ("the web tool loop never passes a baseline") is fixed. On main `tool_batch.py` contained no `baseline` at all (`git show main:…/tool_batch.py | grep baseline` → empty). On the new base both dispatch seams pass the request-start state:

- `tool_batch.py:2254` and `:3248`: `baseline=ctx.turn_evidence.effects.initial_state if ctx.turn_evidence is not None else None,`
- Commits: `a2ad64974` "fix(composer): compare freeform edits with request-start state"; `a33cbb6d6` "fix(composer): tell the model when diff_pipeline cannot see held proposals" (adds a `post_call_hints` entry at `tool_batch.py:3286-3308` when the session has pending proposals).
- Tool description now states the baseline: `generation.py:4889` "Compare with this surface's baseline (Web Composer: request-start state, fixed across provider rounds). …".
- No-baseline text reworded: `generation.py:4869` "No baseline available. This surface supplied no comparison state; saved revisions may still exist." (main `:4667`: "No baseline available. Load or create a session first."). Guidance pattern reworded at `generation.py:460-462`; skill text at `skills/pipeline_composer.md:613-616`.

`CHANGELOG.md:186-189` states the limit of the fix: "Asked one turn after an edit it reports no change; the intended baseline is still to be decided."

**Remaining from the brief's goal/acceptance (verified absent).**

- `last_accepted` baseline: not implemented. `grep -rn baseline_kind src/elspeth/web` → no hits. Nothing in the tool loop reads `committed_state_id` for diffing.
- `baseline_kind` in `data`: absent. `diff_states` (`tools/_common.py:1140-`) emits `from_version` only.
- `is_valid: false` on the no-baseline failure: still present. `_execute_diff_pipeline` (`generation.py:4863-4872`) returns `_saved_graph_rejection_result`, which (`_common.py:1375-1383`) prepends a rejection entry to `state.validate()`; `_prepend_rejection_entry` sets `is_valid=False` (`_common.py:~1619`).
- Acceptance "s01 T6-shaped replay (edits after accept) lists the changes": not met; a diff in a later request reports zero changes, and a test pins that (below).

**Anchors.**

| Brief | New base |
|---|---|
| `tool_batch.py`: pass `baseline=` | done at `:2254` (preview dispatch) and `:3248` (main dispatch) |
| `generation.py ~L4645-4680` | handler `_execute_diff_pipeline` `:4844-4882`; declaration `_DIFF_PIPELINE_DECLARATION` `:4885-4900` |
| `diff_baseline_unavailable` | emitted `:4870`; guidance pattern `:460-462`; code list `:1979`; registry `error_codes.py:83` |
| tool description "in `tools/declarations.py` or the generation declarations" | `generation.py:4889-4896`. `tools/declarations.py` is not the place (unchanged, holds no `diff_pipeline` text) |
| `sessions/models.py:706` `committed_state_id` | `:706` (unchanged) |

**Tests.**

- `tests/unit/web/composer/test_diff_pipeline_baseline.py` does not exist.
- `tests/unit/web/composer/test_freeform_diff_baseline.py` **does** (added by `a2ad64974`, 133 lines): `test_diff_uses_request_start_and_resets_for_next_request`, parametrised same-batch/later-round × auto-commit/explicit-approval, through the real dispatch loop. It is the natural home for the lane's tests.
- That test **pins the opposite of the brief's acceptance** at `:131-133`: in the next request, `next_payloads[0]["data"]["from_version"] == result.state.version` and `total_changes == 0`. A `last_accepted` baseline changes this test.
- `tests/unit/web/composer/test_tool_declarations.py:584-587` snapshots the exact `diff_pipeline` description. Commit `a2ad64974` notes the wording was kept short "so the complete tool declaration remains within the existing prompt-size ceiling" — a rewording that names two baselines must stay inside that ceiling **(the ceiling test itself was not located; inference from the commit message)**.
- Other files that pin the diff wire shape: `tests/unit/web/composer/fixtures/generation_response_wire_4f206253.json`, `test_generation_response_contracts.py`, `test_agent_tooling.py`, `validation_guidance_legacy_patterns.json`, `test_validation_guidance_catalogue.py`.

**Brief corrections.**

1. **Decision needed before dispatch.** The brief lists two baselines but never says which one a call uses. On the new base the request-start baseline is always supplied when `ctx.turn_evidence` is not None, so the no-baseline branch is unreachable in a normal web turn, and "With no accepted proposal, the result is an explicit no-baseline message" contradicts shipped, tested behaviour (request-start diff succeeds). The changelog records the baseline as undecided. The brief needs a precedence rule (for example: `last_accepted` when a committed proposal exists, else `request_start`) and must say what auto-commit sessions get, since they stage no proposals **(inference: auto-commit sessions have no `status='committed'` proposal rows; not verified against the proposal store)**.
2. Delete "pass `baseline=` to the dispatch" and "reword `diff_baseline_unavailable`" as done; keep only "stop reporting `is_valid: false`" if the no-baseline branch stays reachable (MCP surface: `composer_mcp/server.py:878`).
3. Adding `baseline_kind` to `data` is no longer a one-file edit. Discovery results now pass a closed response contract: `tool_batch.py:3309-3310` `admit_discovery_result(...)` and `DIFF_PIPELINE_RESPONSE_CONTRACT` (`tools/_generation_responses.py:347`), whose `DiffResponse` model (`:89-105`) admits a fixed field set. A new key must be added to `DiffResponse`, to `diff_states`, to the wire fixture and to the declaration snapshot, or admission fails.
4. The kind must travel with the baseline: `execute_tool(..., baseline=…)` / `ToolContext.baseline` (`tools/_dispatch.py:967`) have no kind parameter, and the MCP server passes its own baseline (`composer_mcp/server.py:412`, `:878`).
5. Reuse, do not rebuild: the pending-proposal lookup already in the dispatch closure (`turn_sessions_service.list_composition_proposals(turn_session_uuid)`, `tool_batch.py:3296-3300`) is the session-service read the brief asks for; `get_state(state_id)` is at `sessions/service.py:7961`. The existing `post_call_hints` mechanism is how "says which baseline it used" was partly delivered.
6. Rename the test file in the brief to the existing `test_freeform_diff_baseline.py`, or state that a second file is wanted.

**File overlap (must touch).** `tool_batch.py`, `tools/generation.py`. **Not in brief:** `tools/_generation_responses.py`, `tools/_common.py` (`diff_states`), `tools/_dispatch.py`, `skills/pipeline_composer.md` (`:613-616` teaches the refusal), `tests/unit/web/composer/test_freeform_diff_baseline.py`, `test_tool_declarations.py`, `fixtures/generation_response_wire_4f206253.json`; possibly `composer_mcp/server.py`. `tools/declarations.py` is **not** needed.

---

## CDA-03 — STILL-PRESENT

**Status (verified).** `report_assemble.py` is unchanged and declares no semantics: `grep -n output_semantics src/elspeth/plugins/transforms/report_assemble.py` → no hits. Declarers on the new base are `line_explode.py`, `json_explode.py`, `llm/transform.py`, `web_scrape.py`, `sources/llm/source.py`, `sinks/text_sink.py`, `sinks/document_sink.py` (plus `infrastructure/base.py`). `document_sink.py:134-145` still carries the `schema.mode: fixed` example. The docs list is still stale and is now **more** stale.

**Anchors.**

| Brief | New base |
|---|---|
| `report_assemble.py:247` `created_output_fields` | `:247-249` (unchanged) |
| `line_explode.py:422` `output_semantics` | `:422`; builder `_build_line_explode_output_semantics` `:217-268` (unchanged) |
| `contracts/plugin_semantics.py:84-110` `TextFraming` | `:84-111` (unchanged) |
| `document_sink.py ~L142` example | `example_use` `:134-145`, `mode: fixed` at `:142` (unchanged) |
| `document_sink.py ~L158` | `probe_config` `:157-159` (unchanged) |
| `document_sink.py ~L131`, `~L455` | `usage_when_not_to_use` `:127-133`; `composer_hints` `:448-460` (unchanged) |
| `docs/reference/environment-variables.md ~L587-592` | list at **`:604-610`** (moved +17) |
| `plugin_policy/compiler.py:17-40` | `REQUIRED_WEB_PLUGIN_IDS` **`:17-43`** |

**The count changed: 15, not 14.** Measured from the live module with both source roots on the path (`elspeth.__file__` resolved inside this worktree):

```
15
['sink:csv', 'sink:document', 'sink:json', 'sink:text', 'source:csv', 'source:json', 'source:llm', 'source:text',
 'transform:field_mapper', 'transform:file_inject', 'transform:line_explode', 'transform:llm',
 'transform:reference_join', 'transform:report_assemble', 'transform:web_scrape']
```

`transform:file_inject` was added by `edf6127d4` "feat(web): authorize file_inject and refuse planner-written content for it" (`compiler.py:32-34`). The doc still lists 9 (`environment-variables.md:607-610`).

**Tests.** `tests/unit/plugins/transforms/test_report_assemble.py` exists (402 lines, unchanged); it has no semantic-contract test, so the three new assertions are uncovered. `tests/unit/plugins/sinks/test_document_sink.py:231, :343` assert that the sink's hints name `report_assemble`; they stay valid.

**Brief corrections.**

1. "list the 14 always-authorized plugins" → **15**, including `transform:file_inject`. Better: tell the lane to derive the list from `REQUIRED_WEB_PLUGIN_IDS` and not to hard-code a count.
2. Line anchors: env-vars `~L604-610`; compiler `:17-43`.
3. The same stale 9-item list exists at `docs/reference/configuration.md:151-154` ("Its required authorization set is: …"). Not in the brief; fix in the same lane or the two references disagree.
4. **Design check on the COMPACT rule (verified against `report_assemble.py:265-296`, unchanged since main, so this is a flaw in the brief rather than drift).** `process` accepts any `str` row value and does not reject CR/LF inside a line; `_render_plain_text` joins the values as they are. So "no `title` and a `join_with` with no CR/LF" does not make the result provably one line: a single input value carrying a newline yields a newline-bearing report. `line_explode` may declare COMPACT because it mechanically removes every line boundary (`line_explode.py:223-229`); `report_assemble` does not. Declaring COMPACT from configuration alone would let `report_assemble → text` validate and then divert at run time, the failure ADR-039 exists to prevent. Options for the maintainer: declare `NEWLINE_FRAMED` when a title or a CR/LF join is configured and `UNCONSTRAINED` otherwise (it is then a true "not statically decidable" claim, which the doctrine permits); or make the transform reject CR/LF in inputs under the compact configuration so the claim becomes true. The brief's third test ("`join_with=" "` and no title is allowed") depends on this choice.
5. The `html_fragment` format with a title always contains `\n` (`:236`); the `markdown` and `plain_text` title paths contain `\n\n` (`:219`, `:225`). The brief's "no title" condition covers these; no correction, noted so the lane tests all three formats.

**File overlap (must touch).** `report_assemble.py`, `document_sink.py`, `docs/reference/environment-variables.md`, `test_report_assemble.py`. **Not in brief:** `docs/reference/configuration.md`. No collision with files changed in 0.8.12/0.8.13 other than the env-vars doc (four doc commits in other sections).

---

## CDA-04 — STILL-PRESENT

**Status (verified).** `interpretation_state.py`, `multi_query.py`, `interpretation_surfacing.py`, `sessions/pending_interpretation.py` and `plugins/transforms/llm/transform.py` are all unchanged since main. `vague_term_wiring_count` (`interpretation_state.py:2947-3018`) still returns 1 on a matching `interpretation_ref` part without asking whether any query renders the node-level template. Both teaching sentences are still present.

**Anchors.**

| Brief | New base |
|---|---|
| `interpretation_state.py` `vague_term_wiring_count ~L2947` | `:2947-3018` (unchanged) |
| `interpretation_state.py ~L2805-2807` | `:2805-2807` inside `approved_prompt_artifact_hash_from_options` (`:2796-2842`): `surface = multi_query_prompt_surface_from_options(options)` / `if surface is not None and not surface.node_template_users: prompt = None` (unchanged) |
| `multi_query.py ~L248-249` | `:245-250`; the sentence "Put reviewed slots in the node-level template and reference plain row variables here." is at `:248-249` (unchanged) |
| `planner_authoring_aids.py:868-876` (L867 comment) | **`:889-897`, comment at `:888`** (moved +21; text byte-identical to main) |
| "Reviewed slots belong in the node-level template." (L871-872) | **`:892-893`** |
| callers of the gate (from the evidence file) | `interpretation_state.py:674`; `interpretation_surfacing.py:145`, `:373`; **`tools/sessions.py:2871`** (evidence says `:2819`; file changed +60/-8) |
| `MultiQueryPromptSurface.node_template_users` | `interpretation_state.py:2419-2420` (unchanged) |

**Tests.**

- `tests/unit/web/composer/test_state_multi_query_contract.py` exists (416 lines, unchanged); no vague-term/multi-query test in it.
- `tests/unit/web/composer/test_planner_authoring_aids.py` exists (2968 lines, +125 since main). `grep "Reviewed slots\|NEVER put"` over `tests/unit/web/composer/*.py` → no hits, so no test pins the sentence and none yet covers the brief's new aid assertion.
- The audit's own cross-reference, `tests/unit/web/composer/test_request_interpretation_review_tool.py` (test_05g), was not re-located **(not checked)**.

**Brief corrections.**

1. Aid anchor: `planner_authoring_aids.py:889-897`, comment `:888`, sentence `:892-893`.
2. "A message naming the query" needs more than the count: `vague_term_wiring_count` returns an `int` and its four callers build their own messages (`interpretation_state.py:678` with `VAGUE_TERM_UNWIRED_PREFIX`; `tools/sessions.py:2871-2880` `ToolArgumentError`; `interpretation_surfacing.py:145`, `:373` silently skip). Either the gate gains a sibling that returns the offending query names, or the lane edits the message sites. The brief should list `tools/sessions.py` and `interpretation_surfacing.py` as likely-touched, and note that `tools/sessions.py` has moved under it.
3. The hash item is under-specified against the code. `approved_prompt_artifact_hash_from_options` already hashes every per-query template when the node is multi-query (`:2831-2838`, `approved_prompt_artifact_hash(prompt_template=surface.node_prompt_template, …, queries=tuple(queries))`); what `:2805-2807` does is null the *fallback-validation* `prompt` when no query uses the node template. The lane should start from the finding's wording (README F14: "the attested prompt hash omits the unused node template") and state precisely what must change, or drop the item if the staging rejection makes the unused-template case unreachable. **(Reading of the function is verified; whether a change is still needed is a judgement.)**
4. A third copy of the teaching exists in `docs/reference/configuration.md:1506` ("Put reviewed slots in …"). Not in the brief.
5. The rest of the aids unit is confirmed intact, so the brief's "keep the rest" instruction still applies as written. The node-fallback rule the evidence cites at `:442-448` is now at `:448-455`.
6. `scripts/cicd/generate_skill_inventory.py` and `scripts/cicd/composer_wire_census.py` both exist.

**File overlap (must touch).** `interpretation_state.py`, `multi_query.py`, `planner_authoring_aids.py`, `test_state_multi_query_contract.py`, `test_planner_authoring_aids.py`. **Not in brief:** `tools/sessions.py` and `interpretation_surfacing.py` (message sites), `docs/reference/configuration.md`. `planner_authoring_aids.py` and its test are shared with CDA-12 and were heavily edited in 0.8.12/0.8.13 (`cc8d2a834`, `cafec6a9c`, `436cd4688`).

---

## CDA-05 — STILL-PRESENT

**Status (verified).** `boot_probe.py` is unchanged. The hatch probe still sends three tools (`boot_probe.py:210-218`):

```python
tools=[
    *recovery_tool_definitions(contract.hatch.dialect),
    planner_terminal_tool_definition(dialect=contract.hatch.dialect),
],
```

Production still sends the full palette (`pipeline_planner.py:4663-4669`):

```python
message, calls, audited_call = await call_model(
    model_override=model_config.escape_hatch_model,
    tools_override=planner_tool_definitions(discovery_policy, dialect=model_config.escape_hatch_tool_contract_dialect),
```

`planner_tool_definitions` (`pipeline_planner.py:1741-1760`) returns discovery tools + recovery controls + terminal. The parity comment at `boot_probe.py:202-208` still says "sends recovery controls and the terminal".

**Anchors.**

| Brief | New base |
|---|---|
| `boot_probe.py ~L201-228` | comment `:202-208`, hatch block `:209-229` (unchanged) |
| `pipeline_planner.py:4275` | **`:4665`** (moved +390; the line itself is identical to main) |
| `planner_tool_definitions` | `pipeline_planner.py:1741` |
| `capability_skill.py` `terminal_only` (out of scope) | file unchanged |

**Tests.** Both files exist; each gained only a `composer_context_max_prompt_tokens` fixture line. Neither asserts the brief's new equality. Existing tests that pin the **current** three-tool shape and will change:

- `test_boot_probe.py:141-186` `test_requests_include_hatch_terminal_on_a_forwarding_hatch`: `kwargs["tools"] == [*recovery_tool_definitions(…), planner_terminal_tool_definition(…)]`, the `expected` request built the same way, and strict counts `(2, 1, 0)` at `:186`.
- `test_boot_probe_production_parity.py:491-493`: `probe_hatch_strict == {"handoff_to_user": True, "restore_parked_graph": True, "emit_pipeline_proposal": False}` and the subset comparison against production.
- `test_boot_probe_production_parity.py:558` (strict map of the three tools on the hatch-only-forwards route).
- The surface name `hatch_terminal` is pinned at `test_boot_probe.py:156, :218, :385` and parity `:319, :554`. Keep the name unless the brief says to rename.

**Brief corrections.**

1. Production anchor → `pipeline_planner.py:4665`.
2. Production passes a per-request `discovery_policy`; the probe has none. "Equal `planner_tool_definitions(...)` for the hatch dialect" should be written as `planner_tool_definitions(dialect=contract.hatch.dialect)` (policy `None` = the full registered discovery set), matching how the probe already builds `planner_tools` at `boot_probe.py:185`. State that the probe covers the unfiltered superset.
3. The 0.8.13 context budget fits planner requests before sending (`pipeline_planner.py:3977-3980`, `_fit_planner_context_budget`). It sheds tool **results**, history and hatch transcript events (`:3503-3520`), not tool definitions, so it does not change the tool list the probe must mirror. Every settings fixture now needs `composer_context_max_prompt_tokens` (a required setting since `436cd4688`); new tests must set it.
4. Name the three pinned tests above in the brief as tests that change.

**File overlap (must touch).** `boot_probe.py`, `test_boot_probe.py`, `test_boot_probe_production_parity.py`. Read-only dependency on `pipeline_planner.py`. Nothing outside the brief.

---

## CDA-06 — STILL-PRESENT

**Status (verified).** `contracts/credential_material.py`, `web/credential_guard.py` and `web/validation.py` are unchanged. There is one copy string for every surface (`contracts/credential_material.py:24-26`):

```python
CREDENTIAL_REFUSAL_DETAIL: Final[str] = (
    "This control content appears to contain a credential. Store the value through the secret service and use a secret reference."
)
```

`CredentialMaterialRefused` carries `surface` but ignores it for copy (`web/credential_guard.py:52-67`): `super().__init__(CREDENTIAL_REFUSAL_DETAIL)` at `:57` and `"detail": CREDENTIAL_REFUSAL_DETAIL` at `:63`.

**Anchors.** The brief gives no line numbers. Current locations:

- copy constant: `contracts/credential_material.py:24-26`.
- surface-carrying exception and payload: `web/credential_guard.py:52-67`.
- HTTP renderers of `to_payload()`: `web/app.py:1749-1758` (global handler, 422); `web/sessions/composer_turn.py:1844-1845`; `web/sessions/routes/composer/operations.py:140-141`. Further `CredentialMaterialRefused` references exist in `web/sessions/routes/sessions.py`, `web/execution/routes.py`, `composer/visible_tool_event.py`, `composer/graph_recovery.py`, `composer/advisor_transcript.py` **(counted by grep, not each read)**.
- other direct users of the constant (tool-result text shown to the model, and pydantic field validation): `composer/tools/sources.py:1983`, `composer/tools/blobs.py:142`, `composer/tools/sessions.py:2530`, `web/validation.py:142`.
- model-produced surfaces actually in use: `composer_provider_response` (`provider_gateway.py:651-1023`, `tool_batch.py:350, :366`), `composer_planner_response` (`pipeline_planner.py:1918-1976, :4073-4359`), `composer_advisor_response` (`advisor_checkpoint.py:545-660`), and **`composer_provider_thinking`** (`provider_thinking.py:62`).
- user/request-side surfaces for contrast: `composer_provider_request` (`provider_gateway.py:536`), `composer_state_before_provider` (`service.py:1308`), `composer_yaml_import`.

**Tests.** `tests/unit/web/test_credential_guard.py` exists (582 lines, unchanged). It already has provider-response refusals to extend: `test_provider_response_is_refused_before_release` (`:499`), `test_provider_reasoning_is_refused_before_release` (`:521`), `test_text_provider_reasoning_is_refused_before_release` (`:545`), and `test_structured_refusal_contains_only_closed_evidence` (`:309`). None asserts surface-dependent copy. Tests pinning the current string, which must keep passing for user surfaces: `tests/unit/web/sessions/test_routes_state.py:2809, :2912` (surface `composer_yaml_import`, literal copy) and one reference in `tests/unit/web/composer/test_service_pricing_identity.py`.

**Brief corrections.**

1. The surface is not known in `contracts/credential_material.py`; that module holds only the constant and the detector. The surface-aware choice belongs in `web/credential_guard.py` (`CredentialMaterialRefused.__init__` / `to_payload`). The contracts module needs at most a second constant. This was already true on main; the brief's file list points at the wrong layer.
2. `composer_*_response` does not cover every model-produced surface: `composer_provider_thinking` does not match the pattern. Use an explicit closed set of model-output surfaces, not a name glob, so a new surface cannot silently fall on the wrong side.
3. "Any HTTP mapping that renders it": all three renderers pass `exc.to_payload()` through unchanged, so fixing `to_payload` (and the exception message) covers them; no renderer edit should be needed **(inference from the three sites read)**. The frontend has no handling keyed on `credential_material_rejected` (grep over `frontend/src` → no hits), so it shows `detail` as delivered **(inference)**.
4. The payload's `detail` is covered by a closed-evidence test (`test_credential_guard.py:309`); the new copy must stay a fixed string with no candidate text.
5. Adjacent 0.8.12 change, no conflict: an oversized blob read now returns `blob_content_size_limit` and no longer a credential refusal (`composer/tools/blobs.py:2298-2305`), which removes one source of misleading credential copy that the audit may have observed.

**File overlap (must touch).** `web/credential_guard.py` (**not in brief**), `contracts/credential_material.py` (if the second constant lives there), `tests/unit/web/test_credential_guard.py`. Shares `web/credential_guard.py` / the guard with CDA-18 and CDA-19; recompute those collisions.

---

## Cross-lane: 0.8.12 / 0.8.13 changes that contradict or pre-empt a brief

1. **CDA-02.** Request-start baseline, reworded refusal and reworded description are shipped (`a2ad64974`, `a33cbb6d6`), with a test that pins "baseline resets on the next request". The brief's `last_accepted` baseline reverses that pinned behaviour and the changelog calls the baseline undecided. Needs a maintainer ruling on precedence before the lane runs.
2. **CDA-02.** Discovery results are now admitted through closed response contracts (`admit_discovery_result`, `_generation_responses.py`). Any new key in `diff_pipeline`'s `data` must be added there. The `post_call_hints` envelope field is the existing channel for "say which baseline was used" without widening `data`.
3. **CDA-03.** `transform:file_inject` joined the required web core (`edf6127d4`); the brief's "14" is 15.
4. **CDA-05 (and any lane building `WebSettings` in tests).** `composer_context_max_prompt_tokens` is a required setting (`436cd4688`); the three test fixtures the wave-1 briefs touch already carry it.
5. **CDA-01.** ARGOnite added a "Conversation" title bar and bordered pane cards; the e2e geometry assertions in `tutorial.spec.ts` were rewritten for it. No functional change to the chat components.
6. **CDA-04.** No new mechanism; only the aid text moved (+21 lines) under three unrelated edits to `planner_authoring_aids.py`.
7. **CDA-06.** No new mechanism.

## Not verified

- No test or suite was run; "pinned test will change" statements are from reading assertions.
- The prompt-size ceiling that constrained the `diff_pipeline` description was not located.
- Whether auto-commit sessions ever hold a `status='committed'` proposal row was not checked against the store.
- The remaining `CredentialMaterialRefused` handler sites beyond the three quoted were counted, not read.

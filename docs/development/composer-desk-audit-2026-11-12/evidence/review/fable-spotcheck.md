# Spot-check: composer remediation plan versus its sources

Reviewed at HEAD `58e31760` on `ccr-d14093ba-glu6bw`, 2026-11-13. Read-only: no edits to tracked files, no commits, no pytest runs, no live composer or LLM calls.

## Verdict: CLEARS

No BLOCKERs. 9 FIX, 9 NIT.

Four of the FIXes (FIX-1 to FIX-4) should be applied before the affected lanes are dispatched through `lane-manager`. They do not send a lane to the wrong behaviour, but each would cost a lane a failed `verify` or an unbriefed red test.

Scope: about 60 claims sampled across CDA-01..CDA-22, `tickets.json`, the plan and the runbook. CDA-23 was skipped as instructed. S5 stage 2 was not assessed.

Labels: **EXECUTED** means a command was run and its output read. **READ** means the file was read at the cited lines. Nothing here was confirmed by running tests.

## Findings

### FIX

**FIX-1. CDA-22 cites as preserved a test its own design turns red, and omits the other tests that pin the old behaviour.**

- The brief (`work-packages.md:817-819`) says a failed discovery call "keeps its current charging (`test_service.py:3586`)".
- That test sets `composer_max_discovery_turns=1`, scripts two discovery rounds then two mutations, and asserts `budget_exhausted == "composition"` with `await_count == 4` (READ `tests/unit/web/composer/test_service.py:3586-3622`). Under the brief's design the loop stops after the second discovery call with `discovery`.
- Three more tests pin "discovery has no aggregate cap" and are not named in the brief:
  - `test_discovery_continues_beyond_configured_hint` (`test_service.py:3367`, docstring "Discovery has no aggregate cap");
  - `test_discovery_continues_until_timeout_emits_failure_event` (`test_service.py:5442`);
  - `test_preview_keeps_authoring_available_until_the_real_deadline` (`test_discovery_terminal_preview.py:97`, `composer_max_discovery_turns=1`, at least the `tools` parametrisation).
- The last file is outside CDA-22's test command and `test_files`, so only the full suite would catch it.
- Impact on those tests is inferred from reading; I did not run them against a patched tree.
- Fix: list these as "pinned tests that change" and add `test_discovery_terminal_preview.py` to the test command.

**FIX-2. CDA-10: `tickets.json` names `tools/declarations.py`, which holds no tool descriptions; the real location collides with CDA-11 in wave 3a.**

- `declarations.py` has 0 `description=` assignments (EXECUTED `grep -c`; the same grep finds them in `generation.py`, so the instrument works).
- `get_plugin_assistance`'s description is at `tools/generation.py:2382-2392`; `patch_output_options`'s is at `tools/outputs.py:370-375` (READ).
- `lane_manager.py verify` fails a lane when any listed `files` entry is unchanged (READ `lane_manager.py:659-663`), so CDA-10 would fail verify unless it makes a spurious edit to `declarations.py`.
- CDA-10 must edit `generation.py`, which is CDA-11's only file in the same wave. That contradicts "lanes inside a wave touch disjoint files" (`work-packages.md:109`). The hunks are about 150 lines apart, so a clean merge is likely.
- Fix: replace `declarations.py` with `generation.py` and `outputs.py` in the ticket, and either serialise CDA-10 and CDA-11 or note the shared file.

**FIX-3. CDA-13: the file list disagrees with the S3 prototype and S3 §4.2.**

- `tickets.json` lists `tools/sources.py`. The prototype does not touch it; `_resolve_source_blob` (`sources.py:797`) is reused unchanged (EXECUTED `git apply --stat` on `prototype.patch`; READ S3 note line 25). A lane starting from the prototype fails verify on "expected files not changed".
- The brief and the ticket both omit `src/elspeth/web/composer/redaction.py`, which the prototype changes (12 lines: `blob_id` moves onto `_SetPipelineNamedSourceModel`, `extra="forbid"`) and S3 §4.2 lists first (READ `S3-multifile-custody.md:289`).
- S3 §4.3 also names `tests/unit/web/sessions/test_proposal_blob_refs.py` for new named-source cases; the brief does not.
- The prototype applies cleanly at HEAD (EXECUTED `git apply --check`).

**FIX-4. CDA-18: "the scan point" is one of at least a dozen, and the pinned test exercises a different one.**

- The brief and ticket name `llm_response_parsing.py` (`build_llm_call_record`, L754) and `credential_guard.py`.
- The same helper is also called from `provider_gateway.py:272`, reached through `_require_no_credential_material_in_completion*` at `:83, :157, :188, :654, :703, :724, :734, :817, :1013`; from `tool_batch.py:1015`; and from `advisor_checkpoint.py:579` (EXECUTED grep).
- The pinned test `test_credential_guard.py:521` drives `gateway._call_llm` and asserts surface `composer_provider_response`, which is the gateway path. S4's Luna failure was on that surface too (READ `S4-cross-model.md:230`).
- A change confined to `build_llm_call_record` would not stop the turn failing.
- If the lane touches `advisor_checkpoint.py` it overlaps CDA-07, which runs in parallel.
- Fix: name the gateway, tool-batch and advisor sites, or direct the lane to scrub at `admit_llm_provider_metadata` (`llm_response_parsing.py:576`), which all paths share.

**FIX-5. `remediation-plan.md` R2 still says to reword the scope sentence.**

- R2 (line 27): "Reword the reused scope sentence".
- S1 measured it harmless and asked for that bullet to be replaced (READ `S1-advisor-evidence.md:190, 255-257`). CDA-07 correctly says "Do not reword". The plan is stale; the lane brief is right.

**FIX-6. `remediation-plan.md:80` says CDA-12 "cut the planner's prompt by 50% in S2".**

- S2 has no 50% figure. It reports −36% prompt tokens over all turns, −40% on the first call, −42% whole-request bytes and −70.5% for the system message (READ `S2-planner-overlay.md:146-150, 223, 400-401`; EXECUTED grep for "50" found none).

**FIX-7. `remediation-plan.md` R7 contradicts itself.**

- Line 32 says "Remove `list_models` from both web palettes", then "D4 approved: `list_models` returns each usable profile". CDA-11 implements the second. Delete the first.

**FIX-8. Runbook preflight does not exercise two of the six "safe" prompts.**

- Preflight runs s01, s02, s03, s05, s20 and r01 (`remediation-plan.md:201-203`).
- "What can you do?" and "Build me a pipeline." exist only in s04 (T1, T3) (READ `battery-turns.md`).
- The abort criterion ("FAILED or WITHHELD on a safe-list prompt") therefore cannot fire for them.
- Fix: add s04 to the preflight, as `work-packages.md` §6 already does for the post-wave-2 re-run.

**FIX-9. Latency lanes on the rootless path do not carry the AGENTS.md standing review trigger.**

- CDA-17 (planner deadline reserve), CDA-20 (retries) and CDA-16's cache marker change the planner entry path for latency or cost. AGENTS.md requires per-transition provider-call scrutiny for these.
- §4 of `work-packages.md` lists only the two invariants (EXECUTED grep for "per-transition|standing review" found nothing).
- CDA-17's "hand-off" has no stated target. Say what the planner hands off to, and that no proposal may be authored without a provider call.

### NIT

1. **"Safe in every run" rests on n=1 for five of six prompts.** Only the feedback-classify prompt has repeats (5 PROPOSAL). None is FAILED or WITHHELD.
2. **CDA-01 is called frontend-only but edits `composer/tutorial_service.py`.** The edit is rejection copy in an existing admission check (L169, L195), which the invariants permit. Reword to "no new backend path".
3. **CDA-07 ticket `files` makes `advisor_checkpoint.py` and `planning_application.py` mandatory changes.** The rubric lives in `advisor_context.py:90`, and carrying the snapshot on `TurnEvidence` may leave both untouched. The ticket also omits `pipeline_planner.py`, which the brief tells the lane to edit.
4. **D6 says CDA-19 runs "after CDA-18"; the wave table says parallel.**
5. **Stale wording.** CDA-06 says R6(a) "is blocked on decision D1"; R6 in the plan says "(a) Later, if approved". D1 is approved.
6. **CDA-03 anchor.** `document_sink.py` "~L131, ~L453-455": the `mode: fixed` example is at L142 and L158. L131 and L455 are assistance and error text.
7. **CDA-22 anchor.** `_helpers.py:3115` maps the planner code `DISCOVERY_EXHAUSTED`, not `ComposerConvergenceError`. The mapping the lane needs is `protocol.py:545-553`, which is correct.
8. **Wave table.** 3c (CDA-13) is listed after wave 4. Both CDA-13 and CDA-17 change `test_pipeline_planner.py` (byte-budget re-baseline versus new tests); state the order.
9. **Worktree note.** `-o pythonpath=<root>/src` drops `elspeth-lints/src`, which `pyproject.toml:482` configures and AGENTS.md asks for.

Two observations I could not classify from the sources:

- **CDA-04 before CDA-12.** After wave 1 the stager rejects node-level `vague_term` slots on multi-query nodes with their own templates, while `planner_authoring_aids.py:867-873` keeps teaching "reviewed slots belong in the node-level template" until wave 3b. Inferred; may raise planner rejections in between.
- **"144 source lines across 5 files" (CDA-13).** `git apply --stat` gives 145 changed lines across the 5 `.py` files, plus 11 in `pipeline_capabilities.md`. The S3 note does not state the figure.

## Checks run

### 1. File:line anchors in lane briefs

All READ with `sed -n` at HEAD unless marked.

| Lane | Anchor | Result |
|---|---|---|
| CDA-01 | `subscriptions.ts:458-463` "Select **Run pipeline**" | Pass |
| CDA-01 | `DecisionPanel.tsx:99-101` | Pass |
| CDA-01 | `tutorial_service.py:169, :195` `tutorial_plugin_set` detail | Pass |
| CDA-02 | `tools/generation.py:4642-4679` `_execute_diff_pipeline`, `diff_baseline_unavailable` | Pass |
| CDA-02 | `tool_batch.py` passes no baseline | Pass (EXECUTED `grep -c baseline` = 0; `_dispatch.py:810` accepts one) |
| CDA-02 | `sessions/models.py:706` `committed_state_id`; status `committed` exists (`protocol.py:123`) | Pass |
| CDA-03 | `report_assemble.py:247`, `line_explode.py:422`, `plugin_semantics.py:84` | Pass |
| CDA-03 | `plugin_policy/compiler.py:17-40` has 14 ids; env doc L587-592 lists 9 | Pass |
| CDA-03 | `document_sink.py` ~L131, ~L453 | NIT 6 |
| CDA-04 | `interpretation_state.py:2947`, `:2805-2807`; `multi_query.py:246-249` | Pass |
| CDA-05 | `boot_probe.py:202-216` sends recovery tools plus terminal; `pipeline_planner.py:4275` sends `planner_tool_definitions` | Pass |
| CDA-07 | `turn_effects.py:290` `TurnEvidence`; `:851` binding; `:863` `semantic_repair_key` | Pass |
| CDA-07 | `advisor_abandonment.py:109-119` record, `:129` | Pass |
| CDA-07 | Five `review_candidate` sites: `composition_completion.py:949, :2259, :2369`; `planning_application.py:915, :960` | Pass (EXECUTED grep found exactly these five plus the definition) |
| CDA-07 | `advisor_context.py:314` inside `_advisor_user_sections` (def at :223); `advisor_user_content_parts` at :526; END instructions at :90 | Pass |
| CDA-07 | `composition_completion.py:2554-2560` repair instruction, "Correct the reply's…" | Pass |
| CDA-07 | `pipeline_planner.py:575-657` `prior_assistant_messages` | Pass |
| CDA-08 | `turn_effects.py:952`; `no_tool_policy.py:746`; `advisor_policy.py:451-455` | Pass |
| CDA-09 | `composer_preflight.py:329`; `service.py:1883`; `composition_completion.py:2582` | Pass |
| CDA-10 | `pipeline_planner.py:217` notice; `:5138-5139` append; `:2742-2749` value-free projection | Pass |
| CDA-10 | Tool descriptions in `tools/declarations.py` | **FIX-2** |
| CDA-12 | `capability_skill.py:193-219` `terminal_only`; `planner_authoring_aids.py:867-873`; `pipeline_planner.py:3494-3513`; `advisor_checkpoint.py:453-454`; `service.py:751-801`; `_maybe_upsert_skill_markdown_history` at `service.py:921` | Pass |
| CDA-13 | `tools/sessions.py:2184-2189` ("minus blob_id/inline_blob"); `sources.py:797`; `_common.py:4047`; `pipeline_capabilities.md:248` `named_source` row | Pass |
| CDA-13 | File list | **FIX-3** |
| CDA-16 | `advisor_checkpoint.py:453-495` cache marker at :491-495 | Pass |
| CDA-18 | `llm_response_parsing.py:754-765` in `build_llm_call_record` (def :706); `credential_guard.py:91-126` | Pass for the anchor; **FIX-4** for completeness |
| CDA-19 | `credential_material.py:85` `CREDENTIAL_PATTERN_SPECS`; `credential_assignment` at ~:101-111; `CREDENTIAL_DETECTOR_VERSION` at :22 | Pass |
| CDA-20 | `provider_gateway.py:451`; `pipeline_planner.py:3189, :3789`; `advisor_checkpoint.py:1348` `attempts = 2`; `classify_provider_failure` at `provider_errors.py:121` | Pass |
| CDA-21 | `composer_operation_authority.py:401` `deadline_at=now+…`; `claim_next` :497; worker `:448, :760, :947, :1402-1432` | Pass |
| CDA-22 | `service.py:664`, `:1809`, `:2173` comment, `:3151`, `_classify_and_budget_turn` :1697; `pipeline_planner.py:4858`; `protocol.py:545-553` | Pass |
| CDA-22 | Comment "from `854297e0`" | Pass (EXECUTED `git log -S`) |
| CDA-22 | Shipped value 10; audit value 24 | Pass (EXECUTED grep of `deploy/`, `evals/composer-live-audit/README.md:21`) |

### 2. Test files and pinned tests

- **Existing test files.** All 29 named existing files are present; all 9 marked "(new)" are absent (EXECUTED `[ -e ]` loop; the mix of present and absent results shows the check discriminates).
- **Instruments.** `replay.py`, `analyse.py` (`strict_hits` at :65), both `prototype.patch` files, `s10_seq12_pipeline.json`, `run_scenarios.sh`, `generate_skill_inventory.py --check` and `composer_wire_census.py --scorecard` exist (EXECUTED).
- **`test_pipeline_planner.py:6732`.** `test_discovery_result_does_not_inject_removed_budget_pressure` is there and asserts no "discovery turns remain" text (READ). Pass.
- **`test_service.py:7989`.** `test_turn_budget_convergence_detail_names_the_tool_call_loop`, parametrised over `composition` and `discovery` (READ). Pass.
- **`test_advisor_tool.py:614`.** `test_advisor_only_turn_does_not_consume_discovery_budget` (READ). Pass.
- **`test_service.py:3586`.** Exists but asserts the opposite of what the brief implies. **FIX-1.**
- **`test_credential_guard.py:520-565`.** Two tests assert a reasoning-only `sk-…` raises `CredentialMaterialRefused` (READ). Pass.
- **`test_pipeline_planner.py:5385`.** Sets `reasoning_content` to a credential in the `combined_reasoning` case (READ). Pass. That case also sets `response.model`, so it keeps failing after CDA-18.
- **`test_compose_loop_persistence.py:799-808`.** Parametrised over `reasoning_content`, `reasoning_details`, `thinking_blocks` (READ). Pass.
- **`test_stated_threshold_data_full_walk.py:214`.** Asserts the planner system message equals `composer._composer_skill_text` (READ). Pass.
- **CDA-10 "6 tests pin the notice".** 6 references in `test_pipeline_planner.py` (EXECUTED grep). Pass.

### 3. Numbers versus spike notes

| Claim | Source | Result |
|---|---|---|
| S2: 14/43 → 1/43 | `S2:227, 404` | Pass |
| S2: prompt tokens −36% | `S2:223, 401` | Pass |
| S2: rejections excluding s10, 3 → 8 | `S2:226, 267` | Pass |
| S2: failed or withheld turns 1 → 3 | `S2:220, 406` | Pass |
| S2: s10 183 s/US$0.37 → 10 s/US$0.08 | `S2:213-214, 242-245` | Pass |
| S2: 52.5k → 31.4k first-call tokens; 32/110; 129 → 23; 2 of 15 | `S2:400, 113, 150, 285` | Pass |
| S2: sizing 2.5–3.5 days; 1.5–2 days | `S2:549-552` | Pass |
| S2: "cut the planner's prompt by 50%" (plan) | not in S2 | **FIX-6** |
| S1: 25/30 → 30/30; 20/20; C4 1/5; ≈0.42 | `S1:100, 169-174, 276` | Pass |
| S3: 10.8 s and 29.3 s, n=2; +911 bytes; 240 s baseline; HTTP 500 on follow-up | `S3:165-167, 131, 14, 166` | Pass |
| S4: 1 of 37 blobs; 422 on r01 T3 | `S4:181, 228` | Pass |
| S4: about $0.17 against $0.08 per edit turn | `S4:314` | Pass |
| S4: withhold rates Sonnet 0/3, Luna 2/2, DeepSeek 4/6 | `S4:178` | Pass |
| S4: planner cache about 74%; about 20k tokens uncached | `S4:120, 128` | Pass |
| S5: 80 senders → 44 / 20 / 16, p90 202 s; 8–10 ceiling; ~31 s build; ~36 s failed Run | `S5:118, 317, 326-327` | Pass (stage 1 only) |
| Runbook: planner p90 62 s, max 242 s; "110 s" | `battery-turns.md` 61.9, 241.6, 109.6 | Pass |
| CDA-22: battery max 9 calls per tool-loop turn | `battery-turns.md` `calls_max: 9` | Pass |

### 4. Waves

- **`tickets.json` completeness.** 20 tickets: CDA-01..13, 15, 17..22. Missing are 14, 16 and 23, all intentional (EXECUTED python over the JSON). Pass.
- **Schema.** Keys are `ticket, title, files, test_files, test_command, description`, which is what `lane_manager.py:337` expects; the `init` flags in §6 exist (READ `lane_manager.py:1018-1027`). Pass.
- **Same-wave overlap from `tickets.json` alone.** None across wave 1, 2a/2a′, 3a and 4 (EXECUTED set intersection). This instrument only sees listed files, so it could not catch FIX-2 or FIX-4; those came from reading the briefs against the code.
- **Same-wave overlap from the briefs.** CDA-10 and CDA-11 share `generation.py` (**FIX-2**). CDA-18 may share `advisor_checkpoint.py` with CDA-07 (**FIX-4**).
- **"Depends on" versus wave order.** CDA-08→07, 09→08, 10→wave 1, 11→02, 12→07+10, 13→12, 17→12, 18→06, 19→06, 20→17, 22→12 all agree with the table (READ). Only D6's "after CDA-18" disagrees (NIT 4).
- **`files` as a hard gate.** `verify` requires every listed file and test file to change (READ `lane_manager.py:659-663`). This is what makes FIX-2, FIX-3 and NIT 3 matter.

### 5. Composer invariants

- **Server-authored structure.** None proposed. CDA-04, CDA-10 and CDA-13 add rejection or custody resolution; CDA-12 is teaching; CDA-22's last-chance call goes through the provider (READ all briefs). S3 states the same for the prototype (`S3:283-285`). Pass.
- **Tutorial-only backend path.** None. CDA-01's backend edit is copy in the existing admission check (NIT 2). CDA-15 adds the tutorial brief to an eval corpus only. Pass.
- **Standing review trigger.** Missing from the latency lanes (**FIX-9**).

### 6. Runbook "safe" prompts versus `evidence/battery-turns.md`

All READ.

| Safe prompt | Rows | Labels |
|---|---|---|
| Feedback export classify, one CSV | c02 T1, r01–r03 T1, s02 T1 | 5 PROPOSAL |
| "Change the threshold to 500" | s03 T2 | REPLY |
| "Explain what this pipeline does in two sentences" | s01 T7 | REPLY |
| "Undo that last change" | s01 T8 | REPLY |
| "What can you do?" | s04 T1 | REPLY |
| "Build me a pipeline." | s04 T3 | REPLY |

- No safe prompt is FAILED, WITHHELD or GATE-NOT-CLEARED. Pass.
- "Usable with care" matches: s02 T2 took 109.6 s; s03 T4 is GATE-NOT-CLEARED.
- "Avoid" matches: s10 FAILED twice, s04 T2 and p01 T2 WITHHELD, s06 T1/T2 FAILED.
- "SharePoint withheld 2 of 2" has one row in this table; the second run is cited at `README.md:183` and is not in `battery-turns.md`.
- Preflight coverage gap: **FIX-8**.
- Runbook settings exist with the stated defaults: `composer_async_worker_concurrency` 4 (max 16), `composer_async_max_queued_operations` 64, `auth_rate_limit_per_minute` 20, `composer_prompt_guard_mode` `required`, and "The composer is busy" at `operations.py:182` (READ `web/config.py:303, 401-402, 545`). Pass.
- The four plugins the runbook calls "already in the required core" are in `compiler.py:17-40`. Pass.

## Not checked

- No test was run, so every statement about a test going red or green is from reading.
- CDA-14, CDA-15, CDA-16 and CDA-17 anchors beyond those listed; the frontend files beyond existence and the three cited line ranges.
- README findings F1–F21 beyond F4, F20 and F21 and the spike table; `evidence/surface-teaching-crosscheck.md` and the two verification reports were not sampled.
- Raw captures behind the spike numbers; I compared the plan to the spike notes, not the notes to their data.

## Re-check (39d86b3a)

Re-checked 2026-11-13 at HEAD `39d86b3a`. Read-only; no tests run. `git diff --stat 58e31760 HEAD -- src tests` is empty (EXECUTED), so every code anchor from the first pass still holds.

**Verdict: CLEARS.** No BLOCKER. The nine original FIXes are applied and correct against the code, with three follow-on FIXes and four NITs below.

### Remaining FIX

**R-FIX-1. CDA-04: the range `planner_authoring_aids.py:867-873` is the wrong unit, and the replacement is unspecified.**

- L867 is a comment; the string literal runs L868-876 (READ). L873 falls mid-literal.
- Only one sentence conflicts with CDA-04: "Reviewed slots belong in the node-level template." (L871-872).
- The rest of the literal stays true after CDA-04 and must survive: "NEVER put {{interpretation:...}} tokens inside a queries.*.template" (L868-871) and the `{{ row.<variable> }}` rule with its session reference (L872-876).
- The brief does not say what to teach instead. From `interpretation_state.py:2805-2807`, the node-level template reaches only queries without their own template; say that.
- CDA-04's test command and `test_files` are unchanged, and §4's teaching rule still lists only "CDA-07, 10, 12". A planner teaching edit should also run `test_planner_authoring_aids.py` and the planner byte-budget test in `test_pipeline_planner.py`.
- No conflict with CDA-12: it now says CDA-04 owns this text, and the two lanes are in different waves. No test pins the sentence (EXECUTED grep over `tests`, `src`, `scripts`; it found the source file, so the grep works).

**R-FIX-2. CDA-17: `handoff_to_user` exists, but no palette restricts the planner to it.**

- It is a planner recovery control, not the terminal: `PLANNER_RECOVERY_TOOL_NAMES = ("handoff_to_user", "restore_parked_graph")`, `PLANNER_TERMINAL_TOOL_NAME = "emit_pipeline_proposal"` (READ `capability_skill.py:39-40`; schema at `recovery_controls.py:21-46`, with a `message` field).
- The planner manifest check accepts only `full` or `terminal_only`, and `terminal_only` is exactly the three tools handoff, restore and emit (READ `capability_skill.py:200-219`). A handoff-only call fails that check unless `capability_skill.py` changes, and that file is not in CDA-17's list.
- CDA-12 is told to "remove or wire the dead `terminal_only` option". If it removes it, CDA-17 loses the nearest existing palette.
- Fix: either name the `terminal_only` palette as the hand-off call and tell CDA-12 to keep it, or add `capability_skill.py` to CDA-17 for a handoff-only surface.

**R-FIX-3. Two "Depends on" lines and two ticket descriptions were not updated with the wave table.**

- CDA-19 still says "Depends on. CDA-06" (`work-packages.md:786`); the table says after CDA-18. Its ticket description still says "Wave 2a'".
- CDA-17 still says "Depends on. CDA-12" (`:850`); the table says after CDA-13. The wave-4 row still reads "CDA-17 after CDA-12" and the ticket says "Wave 4", so a dispatcher starting wave 4 after 3b would run CDA-17 beside CDA-13.
- Every other "Depends on" agrees with the table (READ all 12).

### NIT

1. **CDA-18 advisor attribution.** `advisor_checkpoint.py:579` calls the gateway wrapper and so reaches `provider_gateway.py:272`; it is `:646` that reaches `llm_response_parsing.py:754`. The conclusion is unaffected.
2. **CDA-13 test command** still runs only `test_named_sources_blob_binding.py`, though `test_proposal_blob_refs.py` is now a required test file.
3. **CDA-10 ticket** makes `tools/_dispatch.py` a mandatory change while the brief says "`_dispatch.py` / `_common.py`".
4. **CDA-23** is now READY with a full brief but is not in `tickets.json`, while `work-packages.md:1252` says the file holds every READY lane. I did not review the CDA-23 brief.

### The seven requested checks

| Check | Result |
|---|---|
| (a) CDA-18: three direct callers of `credential_guard.py:91` | Pass. Exactly `provider_gateway.py:272`, `tool_batch.py:1015`, `llm_response_parsing.py:754` (EXECUTED grep) |
| (a) Advisor path needs no edit to `advisor_checkpoint.py` | Pass, inferred from reading. `:579` discards the wrapper's return and uses only the reply text; the call record is built at `:646` through `:754`, where the scrub applies. NIT 1 on the citation |
| (b) CDA-22 pinned tests and command | Pass. All four tests are at the cited lines; `test_discovery_terminal_preview.py` is in both the command and `test_files` |
| (c) CDA-13 files versus `prototype.patch` | Pass. The five `.py` files match `git apply --stat` exactly; `sources.py` is gone. NIT 2 |
| (d) CDA-10 anchors | Pass. `generation.py:2382` is `name="get_plugin_assistance"` with the description following; `outputs.py:370-375` is the `patch_output_options` declaration and description |
| (e) CDA-04 teaching replacement | **R-FIX-1** |
| (f) Same-wave overlaps in `tickets.json` | Pass. Pairwise intersection over all 20 tickets (EXECUTED) shows one same-wave overlap, CDA-10/CDA-11 on `generation.py`, which the table now orders. The script found 18 cross-wave overlaps, so it can detect one |
| (f) Reorderings versus "Depends on" | **R-FIX-3** |
| (g) CDA-17 hand-off target | **R-FIX-2** |

Plan and runbook edits checked: R2 now keeps the scope sentence; R7's "remove `list_models`" is gone; the 50% figure is now −36% and −40%, matching S2; preflight includes s04. All pass (READ diff).

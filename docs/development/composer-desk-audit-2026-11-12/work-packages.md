# Composer fix list and subagent work packages (2026-11-13)

This turns the audit ([README.md](README.md)) and its plan
([remediation-plan.md](remediation-plan.md)) into lanes that subagents can
burn down. Lanes run under the repo's `lane-manager` skill: worktree per lane,
failing-first pytest, `verify` red → green → suite, then serial merge. The
machine-readable tickets are in [lanes/tickets.json](lanes/tickets.json).

## 1. Fix list

Status: **READY** (a lane exists), **DECISION** (blocked on the maintainer),
**NOT FIXING** (recommended against, with reason).

| Finding | Fix | Status | Lane |
|---|---|---|---|
| F18 tutorial is not script-only | R16 | READY | CDA-01 |
| F20 run-state copy ignores screen | R3 item 4 | READY | CDA-01 |
| F3 `diff_pipeline` has no baseline | R5 | READY | CDA-02 |
| F11 `report_assemble` → `document` warning | R11 | READY (confirm compat note) | CDA-03 |
| F15 stale always-authorized list | R11 docs | READY | CDA-03 |
| F14 approved interpretation not in multi-query prompts | R13 (reject at staging) | READY | CDA-04 |
| F19 boot probe does not test the real hatch | R17 | READY | CDA-05 |
| F4 credential refusal blames the user | R6(c) copy | READY | CDA-06 |
| F4 reasoning-only hit fails the turn | R6(a) | DECISION D1 | — |
| F2 advisor lacks capability facts; context lost after withhold | R2, R3 item 3 | READY | CDA-07 |
| F9 "Correction to my previous message" | R9 | READY | CDA-07 |
| F17 composer explains internals (rubric half) | R15 (END rubric, blocker detail in context) | READY | CDA-07 |
| F2/F8 withheld and receipt copy | R3 items 1–2 | READY | CDA-08 |
| F16 non-recompute path; CLEAN over `None` cannot clear | R14 | READY | CDA-09 |
| F10 "emit now" notice | R10 | READY | CDA-10 |
| F12 tool-result legibility | R12 | READY | CDA-10 |
| F1 multi-file on the planner (interim: name the problem, hand off) | R4(b) | READY | CDA-10 |
| F1 multi-file on the planner (expressible) | R4(a) | DECISION D2 | CDA-13 |
| F5 `list_models` unfiltered | R7 (handler) | READY | CDA-11 |
| F7 planner taught the tool-loop policy; F5/F17/F14 teaching text; F6 cross-references | R1, R7 skill text, R15 skill text, R13 teaching, R8, E3 | READY (after wave 3a) | CDA-12 |
| F13 generic first-turn reply | E1 | READY | CDA-14 |
| Battery corpus gaps | E4 | READY | CDA-15 |
| Advisor cache 52% | P2 investigation | READY (after CDA-07) | CDA-16 |
| Hatch deadline / output-cap exits | P5 | READY (after CDA-12) | CDA-17 |
| Budget/timeout warning | P6 | DECISION D5 (config, not code) | — |
| Skip advisor on chat turns | P3 | NOT FIXING (loses real catches, e.g. s03 T4) | — |
| Register as a gated artifact | E2 | Optional, only with E3 | — |

## 2. Decisions needed before the blocked lanes

- **D1. R6(a), credential guard on reasoning.** Approve dropping
  reasoning-only hits from the call record instead of failing the turn.
  - The scope is `reasoning_content` and `reasoning_details` only, never the
    replayed `thinking_blocks`, content or tool arguments.
  - It reverses behaviour pinned by tests.
  - That reasoning would then be missing from the audit record for the call.
- **D2. R4(a), multi-file sources.** Allow `blob_id` on named `sources`
  entries. This changes the persisted `sources` shape, so it needs a stated
  compatibility boundary under the delivery posture.
- **D3. R11 compatibility.** With `output_semantics()`, `report_assemble` →
  `text` becomes a hard error instead of a warning. Confirm this is intended.
- **D4. R7 disclosure.** May the composer tell users which concrete model a
  profile maps to? Until you decide, CDA-11 lists profile aliases only.
- **D5. P6 demo budgets.** Raise the timeout, or size the turn budgets to it
  (configuration only).

R16 is decided: you confirmed the tutorial is a fixed prompt the learner
cannot edit (ADR-031). CDA-01 enforces that in the frontend only.

## 3. Waves

Lanes inside a wave touch disjoint files and run in parallel. A later wave
starts from the tree that contains the earlier waves' merges. Cap test
parallelism per lane (for example `-n 4`); CPUs do not multiply across lanes.

| Wave | Lanes | Why serial |
|---|---|---|
| 1 | CDA-01, CDA-02, CDA-03, CDA-04, CDA-05, CDA-06 | Independent, small, mostly demo-relevant |
| 2a | CDA-07 | Shares `composition_completion.py`, `turn_effects.py` and `planning_application.py` with 2b and 2c |
| 2b | CDA-08 | After CDA-07 (`turn_effects.py`, `no_tool_policy.py`) |
| 2c | CDA-09 | After CDA-08 (`composition_completion.py` gate clearing) |
| 3a | CDA-10, CDA-11 | `pipeline_planner.py` and the tool handlers respectively; CDA-11 waits for CDA-02 (`tools/generation.py`) |
| 3b | CDA-12 | After CDA-10 and CDA-07 (`pipeline_planner.py` message builder, advisor system text, skills) |
| 4 | CDA-14, CDA-15, CDA-16, CDA-17 | Polish and performance; CDA-17 after CDA-12 |
| — | CDA-13 | After D2 and CDA-12 |

Only wave 1 plus CDA-07 and CDA-08 matter before the demo, and only if they
land before the code freeze (remediation plan, WP1a).

## 4. Rules every lane brief carries

- Read `AGENTS.md`, and `CONTRIBUTING.md` § Whole-tree gates, **before**
  writing code.
  - No `getattr`/dynamic-attribute sites, `noqa`, `type: ignore` or lint
    suppressions.
  - Run every whole-tree gate whose scanned inputs you change.
- **Composer invariants.**
  - No server-authored pipeline structure.
  - No tutorial-only composer (backend) path; CDA-01 is frontend-only.
- **Test first.** Write the failing test in the named test file first. It
  must fail at an assertion on the base commit, not by crashing. Assert that
  new symbols exist (`importlib.util.find_spec`, `hasattr` in the test only)
  before using them.
- **Evidence.** Each finding's evidence is in `README.md` and `evidence/`.
  - Raw live captures are on the maintainer's machine under
    `.claude/lanes/composer-desk-audit-20261112/`, which is not in git.
  - Reproduce from code, not from captures.
- **Prompt and teaching changes** (CDA-07, 10, 12): run
  `scripts/cicd/generate_skill_inventory.py --check` and
  `scripts/cicd/composer_wire_census.py --scorecard`, plus the planner
  teaching gates under `tests/unit/web/composer/`. Skill-hash consumers will
  change; that churn is expected.
- **Delivery.** Write `report.md` in the lane directory and return one line.

## 5. Lanes

Each lane lists files (the expected artifact), test files (where the failing
test lands), the lane test command, and acceptance. Frontend lanes also name
the Vitest command the orchestrator runs by hand, because `verify` measures
pytest only.

### CDA-01: Tutorial Build step is script-only (R16, F18, F20)

- **Goal.** After **Send tutorial brief**, the learner can only take the card
  decisions the script needs: Accept the proposal, Acknowledge, View prompt,
  Approve. Nothing else may send the composer a message.
- **Files.**
  - `src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx`: add a script
    mode next to `allowFork` that hides `ChatInput`/upload, suggestion Apply,
    repair-graph, "Ask the composer about this", inline-source edit and
    "Use as input".
  - `src/elspeth/web/frontend/src/components/tutorial/TutorialFreeformShell.tsx`:
    use the script mode.
  - `src/elspeth/web/frontend/src/stores/subscriptions.ts` (~L458-463): never
    tell a tutorial learner to "Select Run pipeline".
  - `src/elspeth/web/frontend/src/components/chat/DecisionPanel.tsx`
    (~L99-101): copy follows the screen.
  - `src/elspeth/web/composer/tutorial_service.py` (L169, L195): the 409
    detail names what differs, for example the extra output's name and
    plugin, or the missing transform.
- **Test files.**
  - `tests/unit/web/composer/test_tutorial_service.py`: the 409 detail names
    the extra output.
  - Vitest: `TutorialFreeformShell.test.tsx` asserts that after the brief
    there is no textbox, no Apply, no "Ask the composer about this", and no
    "Edit the list".
  - `src/elspeth/web/frontend/tests/e2e/tutorial.spec.ts`: assert the same
    absence in the browser walk.
- **Test command.** `pytest tests/unit/web/composer/test_tutorial_service.py -n 0 -q`.
  Vitest, run by hand: `npm --prefix src/elspeth/web/frontend run test -- TutorialFreeformShell`.
- **Acceptance.**
  - In a browser walk, no non-card message path exists after the brief.
  - The canary (brief → Accept → cards → Continue to Run) still reaches Run.
  - The ordinary (non-tutorial) chat is unchanged; existing ChatPanel tests
    stay green.
- **Out of scope.** Relaxing the one-output admission rule.

### CDA-02: `diff_pipeline` gets a baseline in the web tool loop (R5, F3)

- **Goal.** `diff_pipeline` works in the tool loop and says which baseline it
  used.
- **Files.**
  - `src/elspeth/web/composer/tool_batch.py`: pass `baseline=` to the dispatch
    of `diff_pipeline`.
  - `src/elspeth/web/composer/tools/generation.py` (~L4645-4680):
    - include `baseline_kind` (`request_start` | `last_accepted`) in `data`;
    - reword `diff_baseline_unavailable`;
    - stop reporting `is_valid: false` for a valid pipeline in that failure.
  - The tool description in `tools/declarations.py` or the generation
    declarations: state both baselines.
- **Baseline sources.**
  - **Request start:** the state at turn start.
  - **Last accepted:** the newest proposal with `status='committed'` and its
    `committed_state_id` (`sessions/models.py:706`), read through the session
    service. No schema change.
  - If none exists, say so plainly.
- **Test files.** `tests/unit/web/composer/test_diff_pipeline_baseline.py`
  (new).
  - A tool-loop dispatch of `diff_pipeline` after an accepted proposal plus
    two edits lists both edits under `last_accepted`.
  - With no accepted proposal, the result is an explicit no-baseline message
    with no `is_valid: false`.
- **Test command.** `pytest tests/unit/web/composer/test_diff_pipeline_baseline.py -n 0 -q`.
- **Acceptance.** An s01 T6-shaped replay (edits after accept) lists the
  changes. A no-change session still reports no change.
- **Out of scope.** Frontend diff views.

### CDA-03: `report_assemble` declares output semantics; docs list fixed (R11, F11, F15)

- **Files.**
  - `src/elspeth/plugins/transforms/report_assemble.py`: add
    `output_semantics()` (value type STR, unconstrained framing; see
    `report_assemble.py:247`).
  - `src/elspeth/plugins/sinks/document_sink.py` (~L131, ~L453-455): fix the
    example's `schema.mode: fixed`, which errors after `report_assemble`.
  - `docs/reference/environment-variables.md` (~L587-592): list the 14
    always-authorized plugins from `plugin_policy/compiler.py:17-40`.
- **Test files.** `tests/unit/plugins/transforms/test_report_assemble.py`.
  - `report_assemble` → `document` validates with no high-severity
    semantic-contract warning.
  - `report_assemble` → `text` behaviour is pinned (decision D3).
- **Test command.** `pytest tests/unit/plugins/transforms/test_report_assemble.py -n 0 -q`.
- **Report.** Whether `json_explode` (seen in s12) needs the same treatment.
  Do not change it in this lane.

### CDA-04: Reject node-level vague_term slots that a multi-query node never renders (R13 code, F14)

- **Goal.** A `vague_term` wired at node level on an `llm` node is rejected at
  staging when any query that should receive it has its own template.
- **Files.**
  - `src/elspeth/web/interpretation_state.py` (`vague_term_wiring_count`,
    ~L2947): the staging gate.
  - The same file (~L2805-2807): include the effective per-query prompts in
    the attested prompt hash.
  - `src/elspeth/plugins/transforms/llm/multi_query.py` (~L248-249): correct
    the assistance text.
- **Test files.** `tests/unit/web/composer/test_state_multi_query_contract.py`.
  Use the repro from `evidence/llm-wiring-verification.md`: `set_pipeline`
  must reject it, with a message naming the query.
- **Test command.** `pytest tests/unit/web/composer/test_state_multi_query_contract.py -n 0 -q`.
- **Also report (read-only).** A query listing stored sessions whose
  committed states carry this wiring, so the maintainer can judge whether any
  records were affected. Do not modify data.
- **Out of scope.** `planner_authoring_aids.py` teaching, which is CDA-12.

### CDA-05: Boot probe sends the real escape-hatch request (R17, F19)

- **Files.**
  - `src/elspeth/web/composer/boot_probe.py` (~L201-228): build the hatch
    request as production does, with the full planner palette on the advisor
    endpoint (`pipeline_planner.py:4275`). Fix the parity comment.
- **Test files.**
  - `tests/unit/web/composer/test_boot_probe.py`.
  - `tests/integration/web/composer/test_boot_probe_production_parity.py`:
    the hatch probe's tools equal `planner_tool_definitions(...)` for the
    hatch dialect.
- **Test command.** `pytest tests/unit/web/composer/test_boot_probe.py tests/integration/web/composer/test_boot_probe_production_parity.py -n 0 -q`.
- **Out of scope.** The dead `terminal_only` option in `capability_skill.py`,
  which is CDA-12.

### CDA-06: Credential refusal copy for model-produced content (R6(c), F4)

- **Goal.** When the credential guard rejects a provider response (reasoning,
  content or tool arguments), the user is not told that *they* supplied a
  credential. Copy for user-pasted secrets is unchanged.
- **Files.**
  - `src/elspeth/contracts/credential_material.py`: surface-aware detail for
    `composer_*_response` and provider surfaces.
  - Any HTTP mapping that renders it.
- **Test files.** `tests/unit/web/test_credential_guard.py`.
  - A rejection on surface `composer_planner_response` renders model-oriented
    copy.
  - A user-message rejection keeps today's copy.
- **Test command.** `pytest tests/unit/web/test_credential_guard.py -n 0 -q`.
- **Out of scope.** R6(a), which is blocked on decision D1.

### CDA-07: Advisor gets the capability facts; repair and withhold flow (R2, R9, R15 rubric, R3 item 3; F2, F9, F17)

- **Files.**
  - `src/elspeth/web/composer/turn_effects.py` (`TurnEvidence`, ~L290): carry
    the plugin snapshot and render a compact capability record (available ids,
    prohibited ids, usable profile aliases), bound by snapshot hash. Reuse
    `advisor_abandonment.py:109-129`.
  - The five `review_candidate` call sites, which must all supply it:
    - `composition_completion.py:949, :2259, :2369`;
    - `planning_application.py:915, :960`.
  - `advisor_checkpoint.py` / `advisor_context.py`: END rubric sentences.
    - The record proves existence, absence or prohibition, not what a plugin
      can do.
    - Reword the reused "absence does not establish…" scope sentence.
    - A reply that shows tool-call JSON or asks the user to file a defect is
      FLAGGED (R15).
  - `composition_completion.py`, the "Completion advisory review — BLOCKING"
    repair instruction:
    - drop "Correct the reply's…";
    - say the user has not seen the draft (R9).
  - Withheld context carry-forward: the next turn's history must keep the
    withheld answer as a labelled unpublished draft, or the original content.
    Do not let it become the saved-scope sentence (s04 T4). Planner history is
    built in `pipeline_planner.py` (`prior_assistant_messages`, ~L575-645);
    edit only the history-construction code there.
  - Give the composer the current gate blocker detail in its context (R15).
- **Test files.**
  - `tests/unit/web/composer/test_advisor_capability_evidence.py` (new):
    - every `review_candidate` site produces advisor evidence containing the
      capability record;
    - a planted "SharePoint source exists" claim against a snapshot without it
      yields a rubric/evidence pair that makes the claim unsupported.
  - `tests/unit/web/composer/test_turn_effects.py`.
  - `tests/unit/web/composer/test_advisor_checkpoint.py`.
- **Test command.** `pytest tests/unit/web/composer/test_advisor_capability_evidence.py tests/unit/web/composer/test_turn_effects.py tests/unit/web/composer/test_advisor_checkpoint.py -n 4 -q`.
- **Acceptance (live, by the orchestrator after merge).** On the demo model
  pair, ×3 each: SharePoint, PDF, Teams and "is this safe" are admitted. A
  planted false capability claim is withheld.

### CDA-08: Withheld-reply and receipt presentation (R3 items 1–2; F2, F8)

- **Files.**
  - `src/elspeth/web/composer/turn_effects.py` (`render_saved_scope`, ~L952)
    and `no_tool_policy.py` (~L170-215, `compose_checkpoint_receipt_message`
    ~L746):
    - a withheld replacement is a system notice using the
      `advisor_policy.py:451-455` wording;
    - the receipt is never duplicated (s09 T2).
  - `src/elspeth/web/frontend/src/components/chat/MessageBubble.tsx`:
    - render the withheld notice as a system note, not "ELSPETH said:";
    - render the checkpoint receipt as a collapsed secondary line. Keep it;
      it is the backend check against hallucinated "I updated X".
- **Test files.**
  - `tests/unit/web/composer/test_checkpoint_receipts.py`.
  - `tests/unit/web/composer/test_no_tool_policy_segments.py`.
  - Vitest: `MessageBubble.test.tsx`.
- **Test command.** `pytest tests/unit/web/composer/test_checkpoint_receipts.py tests/unit/web/composer/test_no_tool_policy_segments.py -n 0 -q`.
  Vitest by hand: `npm --prefix src/elspeth/web/frontend run test -- MessageBubble`.
- **Depends on.** CDA-07.

### CDA-09: Turn-end preflight for every wired state (R14, F16)

- **Files.**
  - `src/elspeth/web/composer/composer_preflight.py`
    (`reuse_or_recompute_runtime_preflight`, ~L329-380): recompute for every
    state with sources and outputs, matching today's blob behaviour.
  - `src/elspeth/web/composer/service.py` (~L1883, the review-handoff exit):
    apply the same rule.
  - `src/elspeth/web/composer/composition_completion.py` (~L2582-2620): a
    CLEAN verdict over a now-present preflight clears a prior block. Keep
    `graph_rejected` semantics: an unchanged, flagged graph stays blocked.
- **Test files.** `tests/unit/web/composer/test_preflight_recompute_wired_state.py`
  (new). A valid, unchanged, blob-free (`llm`-source) state with no preview
  yields a non-`None` turn preflight, and a CLEAN verdict then clears the gate.
- **Test command.** `pytest tests/unit/web/composer/test_preflight_recompute_wired_state.py tests/unit/web/composer/test_runtime_preflight_pending_review_verification.py -n 0 -q`.
- **Report.** Extra preflight cost per question-only turn (secret resolution,
  plugin instantiation), and any new red suffixes on half-built drafts.
- **Depends on.** CDA-08.

### CDA-10: Planner notices, rejection legibility and multi-file hand-off (R10, R12, R4(b); F10, F12, F1 interim)

- **Files.**
  - `src/elspeth/web/composer/pipeline_planner.py`:
    - `_ALL_INFORMATION_GAPS_CLOSED_NOTICE` (L217, append ~L5138): information,
      not a command; at most once per request;
    - `canonical_schema` rejections name the offending key when it is in the
      schema's closed vocabulary (~L2742-2749 keeps the value-free design);
    - a named-`sources` entry carrying `blob_id`/`inline_blob` gets a specific
      rejection: "named sources cannot bind uploaded files on this surface…".
  - `src/elspeth/web/composer/tools/_dispatch.py` / `tools/_common.py`: a
    discovery-tool failure leads with the tool's own error and omits
    whole-pipeline validation.
  - Tool descriptions (`tools/declarations.py` or the generation
    declarations):
    - `get_plugin_assistance` states its `plugin_name` argument explicitly
      (no alias);
    - `patch_output_options` says where `description` belongs.
- **Test files.**
  - `tests/unit/web/composer/test_pipeline_planner.py`: update the 6 tests
    pinning the notice string, then add:
    - at most one notice per request;
    - a named-sources `blob_id` rejection names the problem;
    - a `canonical_schema` rejection names the key.
  - `tests/unit/web/composer/test_agent_tooling.py`: a discovery-failure
    ordering test.
- **Test command.** `pytest tests/unit/web/composer/test_pipeline_planner.py tests/unit/web/composer/test_agent_tooling.py -n 4 -q`.
- **Depends on.** Wave 1 merged.

### CDA-11: `list_models` returns only what this deployment can use (R7 handler, F5)

- **Files.** `src/elspeth/web/composer/tools/generation.py` (the
  `list_models` handler): on the web, return the usable operator profile
  aliases, not litellm's catalog. Whether to disclose the model behind a
  profile follows decision D4; until then, aliases only.
- **Test files.** `tests/unit/web/composer/test_list_models_profiles.py` (new).
  With one profile `standard` configured, the result contains only that
  alias, and no `1024-x-1024` or `openai/gpt-4o`.
- **Test command.** `pytest tests/unit/web/composer/test_list_models_profiles.py -n 0 -q`.
- **Depends on.** CDA-02 (same file). The skill-text half of R7 is in CDA-12.

### CDA-12: Per-surface teaching (R1, P1, R8, E3, plus the teaching halves of R7, R13 and R15)

- **Goal.** No surface is taught a tool it is not offered as something to
  call. The planner is taught `emit_pipeline_proposal`.
- **Files.**
  - `src/elspeth/web/composer/prompts.py`, `capability_skill.py`: render the
    capability core plus a planner overlay instead of the freeform policy, and
    a per-surface skill hash. Remove or wire the dead `terminal_only` option
    (~L193-219).
  - `src/elspeth/web/composer/skills/pipeline_composer.md`,
    `pipeline_capabilities.md`:
    - R7: profiles only; "to change models ask your operator"; remove the
      `model_catalog` reference.
    - R15 Reply Register rule: answer check, readiness and approval questions
      with the user's next action; never show tool-call JSON or suggest
      filing a defect.
  - `src/elspeth/web/composer/planner_authoring_aids.py`:
    - R8: name only available plugins, in the digest, the aids and the hints;
    - R13: **replace** L867-870 teaching.
  - `src/elspeth/web/composer/pipeline_planner.py`: the message builder
    (~L3494-3514) uses the planner overlay.
  - `advisor_checkpoint.py` (~L453-454): state which skill text the advisor
    receives.
- **Test files.**
  - `tests/unit/web/composer/test_surface_teaching_parity.py` (new; this is
    E3). For each surface, scan the **whole** outbound request: system text,
    tool schemas, `authoring_aids` and `instruction`. Assert that no
    unoffered tool is named as callable, and that every offered tool is
    taught. Include a known-positive and a known-negative control.
  - `tests/unit/web/composer/test_skills_loader.py`.
  - `tests/unit/web/composer/test_capability_skill_identity.py`.
  - `tests/unit/web/composer/test_planner_authoring_aids.py`.
- **Test command.** `pytest tests/unit/web/composer/test_surface_teaching_parity.py tests/unit/web/composer/test_planner_authoring_aids.py tests/unit/web/composer/test_skills_loader.py tests/unit/web/composer/test_capability_skill_identity.py -n 4 -q`.
- **Also run.** `generate_skill_inventory.py --check`,
  `composer_wire_census.py --scorecard`, and the planner teaching gates.
  Composer invariant 1 applies: teaching only, no server structure.
- **Acceptance (live, orchestrator).** On the demo pair, ×3 runs of
  s01–s20: the strict palette-confusion rate (crosscheck pattern) is below 5%.
- **Depends on.** CDA-07, CDA-10.

### CDA-13: Multi-file sources on the planner (R4(a), F1). BLOCKED on D2

- **Files.**
  - `src/elspeth/web/composer/tools/sessions.py` (~L2188): the canonical
    named-sources schema.
  - Planner custody: named sources resolve `blob_id` through the existing
    `_resolve_source_blob(source_name=…)`.
- **Conditions.**
  - Keep `inline_blob` out of named sources.
  - Define precedence against reviewed-source custody.
  - Reject `blob_id` together with `path`.
  - Update the pinned canonical field inventory.
- **Test files.** `tests/unit/web/composer/test_named_sources_blob_binding.py`
  (new). Two uploaded blobs bound as two named sources build, and a row-union
  combining topology validates.
- **Test command.** `pytest tests/unit/web/composer/test_named_sources_blob_binding.py -n 0 -q`.
- **Also run.** The testcontainer suite, if persistence code changes.

### CDA-14: Show the proposal's own description on the first turn (E1, F13)

- **Files.** The frontend proposal card and chat message rendering: show
  `metadata.description` under the fixed sentence. The backend constants are
  unchanged.
- **Test files.** Vitest on the proposal card. Pytest is not applicable; mark
  the lane frontend-only and verify by hand.

### CDA-15: Add demo-shaped cases to the battery corpus (E4)

- **Files.** `evals/composer-battery/corpus.md` and
  `evals/composer-battery/scenarios/<case>/scenario.json`:
  - two-file combine;
  - capability questions;
  - "what changed since I accepted";
  - security screening;
  - "use GPT-4o";
  - the tutorial brief.
- **Test files.** `tests/unit/evals/composer_battery/test_corpus.py`, which
  enforces the corpus rules.
- **Test command.** `pytest tests/unit/evals/composer_battery -n 0 -q`.

### CDA-16: Advisor prompt-cache investigation (P2)

Read-only investigation, with a fix only if it is trivial. The question is why
the advisor's cache hit is 52% against the composer's 92–94%. Check whether
per-turn evidence precedes stable text in `advisor_checkpoint.py:453-495`.
Deliver measurements and a proposed change in `report.md`.

### CDA-17: Planner deadline reserve and output-cap handling (P5)

- **Files.** `src/elspeth/web/composer/pipeline_planner.py`:
  - hand off before the operation deadline, keeping a reserve (the planner
    and operation deadlines are equal today);
  - add a rule for `finish_reason=length` in the hatch.
- **Test files.** `tests/unit/web/composer/test_pipeline_planner.py`: a fake
  clock past the reserve yields a hand-off, not a deadline error.
- **Depends on.** CDA-12.

## 6. Running it

```bash
cd <repo root>
python .agents/skills/lane-manager/lane_manager.py init --lanes-dir .lanes --run-id cda-wave1 \
  --base-ref <integration branch> --suite-command "pytest tests/ -n 4 -q" \
  --tickets docs/development/composer-desk-audit-2026-11-12/lanes/tickets.json
# dispatch only the lanes of the current wave; pass each brief verbatim to the subagent
```

`tickets.json` holds every READY lane. Initialise one run per wave, or
initialise them all and dispatch wave by wave. Frontend-only checks (CDA-01,
CDA-08, CDA-14) need the orchestrator to run Vitest by hand, because `verify`
measures pytest only.

After wave 2, re-run the live battery on the demo model pair:

```bash
evals/composer-live-audit/run_scenarios.sh … s01 s02 s04 p01 r01
```

Compare with `evidence/battery-turns.md`.

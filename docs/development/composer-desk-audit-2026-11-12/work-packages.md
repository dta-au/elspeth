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
| F11 `report_assemble` → `document` warning | R11 | READY (D3: config-derived framing, recommended) | CDA-03 |
| F15 stale always-authorized list | R11 docs | READY | CDA-03 |
| F14 approved interpretation not in multi-query prompts | R13 (reject at staging) | READY | CDA-04 |
| F19 boot probe does not test the real hatch | R17 | READY | CDA-05 |
| F4 credential refusal blames the user | R6(c) copy | READY | CDA-06 |
| F4 reasoning-only hit fails the turn | R6(a) | READY (D1 approved) | CDA-18 |
| F2 advisor lacks capability facts; context lost after withhold | R2, R3 item 3 | READY | CDA-07 |
| F9 "Correction to my previous message" | R9 | READY | CDA-07 |
| F17 composer explains internals (rubric half) | R15 (END rubric, blocker detail in context) | READY | CDA-07 |
| F2/F8 withheld and receipt copy | R3 items 1–2 | READY | CDA-08 |
| F16 non-recompute path; CLEAN over `None` cannot clear | R14 | READY | CDA-09 |
| F10 "emit now" notice | R10 | READY | CDA-10 |
| F12 tool-result legibility | R12 | READY | CDA-10 |
| F1 multi-file on the planner (interim: name the problem, hand off) | R4(b) | READY | CDA-10 |
| F1 multi-file on the planner (expressible) | R4(a) | READY (D2 approved, no compatibility) | CDA-13 |
| F5 `list_models` unfiltered | R7 (handler) | READY | CDA-11 |
| F7 planner taught the tool-loop policy; F5/F17/F14 teaching text; F6 cross-references | R1, R7 skill text, R15 skill text, R13 teaching, R8, E3 | READY (after wave 3a) | CDA-12 |
| F13 generic first-turn reply | E1 | READY | CDA-14 |
| Battery corpus gaps | E4 | READY | CDA-15 |
| Advisor cache 52% | P2 investigation | READY (after CDA-07) | CDA-16 |
| Hatch deadline / output-cap exits | P5 | READY (after CDA-12) | CDA-17 |
| Budget/timeout warning | P6 | RESOLVED (D5: presenter scripts turns to fit the budget) | — |
| F4b credential guard refuses ordinary prose with a "Credentials:" label (S4) | Narrow `credential_assignment` | READY (D6 approved) | CDA-19 |
| Provider 429 not retried; the user sees "model unavailable" at once (S5) | Retry with backoff inside the deadline | READY (D7 approved) | CDA-20 |
| `/api/system/status` reports the composer available while the provider refuses for credit (S5) | Runbook credit check now; status probe later | READY (runbook) / proposed | — |
| Queue time counts against the 240 s deadline (S5) | Count the deadline from worker claim, not submission | READY (D7 approved) | CDA-21 |
| Planner `authoring_aids` unmarked for Anthropic prompt caching (S4) | Cache marker | READY | CDA-16 |
| Advisor demands a mutation on a read-only question; the composer then edits (S4, Sonnet r01 T2) | END rubric sentence | READY | CDA-07 |
| Advisor reviews planner turns against the tool-loop policy, so an honest planner decline is withheld (S2, S4: s10 on every model) | Advisor gets the planner text and palette | READY | CDA-12 |
| Opaque encrypted reasoning pattern-scanned; an `sk-` run inside OpenAI's blob failed a turn (S4, Luna, 1/37) | Never scan opaque reasoning fields | READY (D1 approved) | CDA-18 |
| F21 discovery budget not enforced in the tool loop (also hot-path recon) | Enforce `composer_max_discovery_turns` in the loop | DECISION D8 | CDA-22 (proposed) |
| Composer efficiency (hot-path recon): validate ≥3×/mutation, Stage-2 per mutation with reviews pending, rounds² context serialisation, provider-call bookkeeping, repeated fence proofs | P7–P10, E7 (remediation plan § Composer efficiency) | Plan of record governs; measure first. P10's snapshot-row skip needs a ruling | — |
| Skip advisor on chat turns | P3 | NOT FIXING (loses real catches, e.g. s03 T4) | — |
| Register as a gated artifact | E2 | Optional, only with E3 | — |

## 2. Decisions (maintainer, 2026-11-13)

- **D1 approved.** R6(a): a credential-shaped hit found **only** in
  `reasoning_content`/`reasoning_details` drops that reasoning from the call
  record, records the event, and continues. A hit in replayed
  `thinking_blocks`, content or tool arguments still fails. Few models expose
  reasoning, so the main beneficiary is a model that does. Lane: CDA-18.
- **D2 approved, with no compatibility.** Named `sources` may bind
  uploaded blobs. Make it work with the new system only. Do not add migration,
  dual-shape reading or compatibility shims for older stored states. Lane:
  CDA-13.
- **D3 walked through; recommendation recorded.**
  - `report_assemble` declares its output framing from its own configuration:
    `COMPACT` when the result can only be one line (no title, `join_with`
    without CR/LF); otherwise newline-framed text.
  - Effect on `document`: the HIGH warning goes away.
  - Effect on `text`: a multi-line report becomes a validation **error**.
    Today that pipeline validates with a warning and then writes nothing,
    because the text sink diverts every value containing a newline.
  - A single-line configuration still works.
  - Lane: CDA-03. Pending the maintainer's OK on the walk-through.
- **D4 approved.** The composer may and should say which provider and model
  a profile uses when asked. Lanes: CDA-11 (tool result) and CDA-12 (skill
  text).
- **D5 resolved without code.** The presenter scripts the demo turns to fit
  the configured budget. P6 is not a lane.

Raised by the spikes, **both approved 2026-11-13**:

- **D6 approved. F4b (S4).** The `credential_assignment` pattern
  (`contracts/credential_material.py:85`) refuses visible prose such as
  "**Credentials:** managed by the operator…". This was reproduced
  in-process. Sonnet's ordinary answer to "Is this safe to run on sensitive
  data?" failed with 422. Narrow the pattern so the value must be a
  credential-shaped token rather than prose (proposed CDA-19, after CDA-18),
  or keep it and accept the false positive?
- **D7 approved. Provider 429s (S5).** Retry with backoff inside the turn deadline
  (`provider_errors.py`), which changes audit retry semantics; and, with it,
  whether the deadline should count from admission rather than submission.

Open, from the hot-path recon:

- **D8. F21.** Should the tool loop enforce `composer_max_discovery_turns`, as
  the planner does and as the setting's name implies? Enforcing it adds a
  terminal state (`convergence_discovery_budget`) to follow-up turns. The
  alternative is to document that the setting is planner-only.
- **P10 ruling.** May the composer skip writing a state snapshot row whose
  content hash equals the previous row's?

R16 is also decided: you confirmed the tutorial is a fixed prompt the learner
cannot edit (ADR-031). CDA-01 enforces that in the frontend only.

## 3. Waves

Lanes inside a wave touch disjoint files and run in parallel. A later wave
starts from the tree that contains the earlier waves' merges. Cap test
parallelism per lane (for example `-n 4`); CPUs do not multiply across lanes.

| Wave | Lanes | Why serial |
|---|---|---|
| 1 | CDA-01, CDA-02, CDA-03, CDA-04, CDA-05, CDA-06 | Independent, small, mostly demo-relevant |
| 2a′ | CDA-18 | Parallel with CDA-07; after CDA-06 (`test_credential_guard.py`) |
| 2a | CDA-07 | Shares `composition_completion.py`, `turn_effects.py` and `planning_application.py` with 2b and 2c |
| 2b | CDA-08 | After CDA-07 (`turn_effects.py`, `no_tool_policy.py`) |
| 2c | CDA-09 | After CDA-08 (`composition_completion.py` gate clearing) |
| 3a | CDA-10, CDA-11 | `pipeline_planner.py` and the tool handlers respectively; CDA-11 waits for CDA-02 (`tools/generation.py`) |
| 3b | CDA-12 | After CDA-10 and CDA-07 (`pipeline_planner.py` message builder, advisor system text, skills) |
| 4 | CDA-14, CDA-15, CDA-16, CDA-17 | Polish and performance; CDA-17 after CDA-12 |
| 3c | CDA-13 | After CDA-12 (planner custody and schema) |
| 2a′ (also) | CDA-19 | After CDA-06 (`credential_material.py`); parallel with CDA-18 |
| 4 (also) | CDA-21 | Coordination and worker only; after wave 3 |
| 4b | CDA-20 | After CDA-17 (`pipeline_planner.py` call path) |

**Cost coupling (hot-path recon).** CDA-09 adds Stage-2 validation to
question-only turns, and CDA-20 multiplies per-call bookkeeping on retries.
Both must report their measured added cost in `report.md`. CDA-12 reduces
context serialisation cost, so measure P-14 again after it.

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
- **Worktree test trap (S3).** In a worktree, pytest's configured
  `pythonpath` (`pyproject.toml`) silently overrides an exported
  `PYTHONPATH`. When proving a test red on base, pass
  `-o pythonpath=<root>/src` and check `elspeth.__file__`.
- **Spike artifacts.** Design notes are in
  `docs/development/composer-desk-audit-2026-11-12/spikes/`. Instruments and
  prototype patches are in `evals/composer-live-audit/spikes/` (see its
  README). Start CDA-07, CDA-12 and CDA-13 from them.
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
    `output_semantics()` for `output_field` (value type STR; see
    `report_assemble.py:247`). Framing is derived from configuration:
    - `COMPACT` when the result is always one line: no `title`, and a
      `join_with` with no CR/LF;
    - otherwise `NEWLINE_FRAMED`.

    Follow `line_explode.py:422` and the `TextFraming` doctrine in
    `contracts/plugin_semantics.py:84-110`; do not use `UNCONSTRAINED` as a
    shortcut.
  - `src/elspeth/plugins/sinks/document_sink.py` (~L131, ~L453-455): fix the
    example's `schema.mode: fixed`, which errors after `report_assemble`.
  - `docs/reference/environment-variables.md` (~L587-592): list the 14
    always-authorized plugins from `plugin_policy/compiler.py:17-40`.
- **Test files.** `tests/unit/plugins/transforms/test_report_assemble.py`.
  - `report_assemble` → `document` validates with no high-severity
    semantic-contract warning.
  - `report_assemble` → `text` with the default `join_with="\n"` is a
    semantic CONFLICT (validation error).
  - With `join_with=" "` and no title, it is allowed.
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
    - Keep the record bytes identical to `advisor_abandonment.py:109-119`,
      through one shared renderer. Do **not** reword the scope sentence; S1
      measured it harmless.
    - Carry the record on `AdvisorPublicationReview` from `TurnEvidence` (one
      snapshot per request). Render it as its own section in
      `_advisor_user_sections` (`advisor_context.py:314`), immediately before
      the candidate/request projection.
      `advisor_user_content_parts` (`advisor_context.py:526-625`) counts
      sections by position and must count the new one.
    - Bind `snapshot_hash` into `binding` and `semantic_repair_key`
      (`turn_effects.py:851, :863`), as the abandonment path does.
    - Append the rubric sentence as the last bullet of
      `_ADVISOR_END_PUBLICATION_INSTRUCTIONS`.
    - A reply that shows tool-call JSON or asks the user to file a defect is
      FLAGGED (R15).
    - On a read-only question ("What changed?", "Explain…"), the advisor
      must not demand a pipeline change; a missing control is reported in
      the reply, not by editing (S4: Sonnet r01 T2 edited the pipeline after
      the advisor asked for a prompt-injection shield).
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
    - the `AdvisorPublicationReview` binding changes when only
      `snapshot_hash` changes;
    - the rendered END user message contains the record section exactly
      once, before the projection section;
    - the record text equals the abandonment record for the same snapshot.
  - `tests/unit/web/composer/test_turn_effects.py`.
  - `tests/unit/web/composer/test_advisor_checkpoint.py`.
- **Test command.** `pytest tests/unit/web/composer/test_advisor_capability_evidence.py tests/unit/web/composer/test_turn_effects.py tests/unit/web/composer/test_advisor_checkpoint.py -n 4 -q`.
- **Acceptance (orchestrator, after merge).**
  - **Replay** (`evals/composer-live-audit/spikes/S1-advisor-replay/replay.py`,
    cases W1–W4, K1, K2, n=5 each, arm built from the new code path): at least
    29 of 30 ADMIT.
  - **Planted false existence or prohibition claims** (C1, C2, C3, C5, n=5
    each): 20 of 20 WITHHOLD. Also replay the natural S4 case (Luna p01 T2:
    "Yes. I can read PDFs when you upload one"): it must be FLAGGED.
  - **Live**, ×3 each on the demo model and one other model: SharePoint, PDF,
    Teams and "is this safe" are admitted. The withhold rate depends on
    phrasing (S4: Sonnet 0/3, Luna 2/2, DeepSeek 4/6), and a live ×3 alone
    passes an unfixed build about 42% of the time, so the replay is the gate.
  - **Read-only questions:** an r01 T2-shaped replay ("What changed?") is
    ADMITTED without a mutation demand, on at least two composer models.
  - **Non-goal:** false claims about what a real plugin can do (C4, "csv reads
    xlsx") are not caught by the record (S1: 1 of 5). Document this.
- **Size.** About 1 agent-day (S1).

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
- **Acceptance.** The receipt is never duplicated, including on the
  advisor-unavailable withhold (S4: Luna s06 T2).
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
- **First check (S4).** Verify whether the empty-session planner request
  names the session's attached files. S4's captures show none, but its
  driver uploads files without mentioning them in the message, so confirm
  what the real frontend sends before changing anything (Luna asked the user
  to upload files that were already attached). R4(b)'s hand-off must also
  survive the advisor; that half is CDA-12's advisor change.
- **Test command.** `pytest tests/unit/web/composer/test_pipeline_planner.py tests/unit/web/composer/test_agent_tooling.py -n 4 -q`.
- **Depends on.** Wave 1 merged.

### CDA-11: `list_models` returns only what this deployment can use (R7 handler, F5)

- **Files.** `src/elspeth/web/composer/tools/generation.py` (the
  `list_models` handler): on the web, return the usable operator profile
  aliases, each with its provider and model (D4 approved), not litellm's
  catalog. Never include credential refs, endpoints or keys
  (`LLM_PROFILE_PRIVATE_FIELDS` stays private except `provider` and `model`).
- **Test files.** `tests/unit/web/composer/test_list_models_profiles.py` (new).
  With one profile `standard` → openrouter `deepseek/deepseek-v4.1-flash`
  configured, the result contains exactly that alias, provider and model, and
  no `1024-x-1024`, `openai/gpt-4o` or credential reference.
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
    - R7: models are bound through operator profiles. When asked, name the
      profile's provider and model from `list_models` (D4). Changing the model
      is an operator change. Remove the `model_catalog` reference.
    - R15 Reply Register rule: answer check, readiness and approval questions
      with the user's next action; never show tool-call JSON or suggest
      filing a defect.
  - `src/elspeth/web/composer/planner_authoring_aids.py`:
    - R8: name only available plugins, in the digest, the aids and the hints;
    - R13: **replace** L867-870 teaching.
  - `src/elspeth/web/composer/pipeline_planner.py`: the message builder
    (~L3494-3514) uses the planner overlay.
  - `advisor_checkpoint.py` (~L453-454): the advisor still reviews planner
    turns against the freeform policy. Give it the planner text and a
    statement of the planner's palette for planner turns. This is required,
    not optional: in S2 both arms' honest s10 declines were WITHHELD because
    the policy names `set_source_from_blobs`, and S4 saw the same on Sonnet.
  - `src/elspeth/web/composer/service.py` (~L751-801): render the planner text,
    assert its on-disk hash, pass it to `PlanningApplication`, and **archive
    it in `skill_markdown_history`**. Today only the tool-loop text is
    archived, so planner proposals would cite an unarchived skill hash (S2).
  - `src/elspeth/web/composer/skills/pipeline_planner.md` (new): the planner
    overlay. Take the **wiring** from S2's prototype
    (`evals/composer-live-audit/spikes/S2-planner-overlay/prototype.patch`),
    not its text. S2's live A/B showed the 7.3k condensation drops material
    the planner really uses (field-naming rules, the detailed LLM review
    rules, the repair table): s09 failed with `planner_repair_exhausted`,
    s08 hit the output cap twice and lost the shield card, and 2 of 15 runs
    put data in named `sources`. Build the overlay by **moving and
    rewriting** the planner-relevant parts of the freeform policy (about
    30–50k characters), and do not mention named `sources` until CDA-13
    lands.
  - Drift: review staging, the Reply Register, termination and decline rules
    now live in two overlays. Factor shared rules into the core, or pin them
    with the E3 test.
- **Test files.**
  - `tests/unit/web/composer/test_surface_teaching_parity.py` (new; this is
    E3). For each surface, scan the **whole** outbound request: system text,
    tool schemas, `authoring_aids` and `instruction`. Assert that no
    unoffered tool is named as callable, and that every offered tool is
    taught. Include a known-positive and a known-negative control.
  - `tests/unit/web/composer/test_skills_loader.py`.
  - `tests/unit/web/composer/test_capability_skill_identity.py`.
  - `tests/unit/web/composer/test_planner_authoring_aids.py`.
  - `tests/unit/web/sessions/test_skill_markdown_history_authority.py`: the
    planner hash is archived and resolvable.
  - A planner-rendering case in `test_capability_skill_identity.py`.
  - Surface parameterisation of `test_tool_result_envelope_gate.py` and
    `composer_wire_census.py`. Today no teaching gate reads what the planner
    is sent (S2).
  - Re-point `test_stated_threshold_data_full_walk.py:214`, which asserts the
    planner gets the freeform text.
  - A test that the deployment overlay reaches the planner rendering.
- **Test command.** `pytest tests/unit/web/composer/test_surface_teaching_parity.py tests/unit/web/composer/test_planner_authoring_aids.py tests/unit/web/composer/test_skills_loader.py tests/unit/web/composer/test_capability_skill_identity.py -n 4 -q`.
- **Also run.** `generate_skill_inventory.py --check`,
  `composer_wire_census.py --scorecard`, and the planner teaching gates.
  Composer invariant 1 applies: teaching only, no server structure.
- **Acceptance.**
  - **E3 baseline:** 129 unoffered-tool mentions in the whole planner request
    at HEAD; 23 after R1 alone (S2). The rest belong to R8 and R13 and to the
    schema text. Allowlist the core's 3 conditional mentions.
  - **Live, orchestrator,** on the demo pair, ×3 runs of s01–s20 plus s10 and
    c03:
    - strict palette-confusion rate below 5%, using `strict_hits` in
      `evals/composer-live-audit/spikes/S2-planner-overlay/analyse.py`
      (controlled; 32/110 on the audit corpus);
    - first-call planner prompt at most 45k tokens on DeepSeek (HEAD 52.5k;
      the short overlay measured 31.4k, and a fuller one needs the room);
    - **no increase in proposal rejections or `planner_repair_exhausted`
      against a same-day baseline on s05, s08, s09, s12 and s13, n ≥ 3 per
      arm.** These are where S2's short overlay regressed (3 → 8 rejections,
      1 failure). This is the decisive measurement;
    - the same review cards as the baseline on s08, s09 and s13, including
      `prompt_injection_shield_recommendation` where the baseline stages it;
    - the s10 reply reaches the user (not WITHHELD), states the
      one-uploaded-file limit with workable options, and shows no field keys
      or tool names (S2's overlay reply leaked `source.blob_id`);
    - c03 answered, not WITHHELD, in at least 2 of 3 runs;
    - a human reviews the s10 and c03 replies for honesty.
  - **S2 live A/B** (15 first turns per arm, n=1–2): missing-tool reasoning
    14/43 → 1/43 planner calls; prompt tokens −36%; s10 183 s/US$0.37 →
    10 s/US$0.08; but rejections excluding s10 rose 3 → 8 and failed or
    withheld turns 1 → 3.
- **Size.** 2.5–3.5 agent-days (S2). R1 plus E3 plus the advisor half is
  1.5–2 days, including at least one overlay iteration against live
  evidence. Consider splitting: **CDA-12a** (wiring, archive, advisor text,
  E3, overlay) and **CDA-12b** (R7, R8, R13 and R15 text).
- **Depends on.** CDA-07, CDA-10.

### CDA-13: Multi-file sources on the planner (R4(a), F1). D2 approved, no compatibility

- **Files.**
  - `src/elspeth/web/composer/tools/sessions.py` (~L2188): the canonical
    named-sources schema.
  - Planner custody: each named `sources.<name>.blob_id` resolves through the
    existing `_resolve_source_blob(source_name=<name>,
    rejection_operation=CANDIDATE)`. This is the resolver the singular
    `source.blob_id` and the tool loop's `set_source_from_blob` already use,
    so it produces the identical state shape (S3).
  - `src/elspeth/web/sessions/proposal_blob_refs.py`: **required.** The
    pending-proposal blob extractor reads only `source.blob_id`. Without the
    fix, a planner proposal over two files skips the ownership check at
    creation and does not protect the blobs from deletion.
  - `src/elspeth/web/composer/tools/_common.py`
    (`_serialize_set_pipeline_arguments`).
  - `src/elspeth/web/composer/tools/state_responses.py`: the
    `get_pipeline_state` response check refused named-source `blob_id`
    values. The S3 live run crashed with HTTP 500 on the first follow-up turn
    until this was fixed (regression test in the prototype).
  - Start from S3's prototype (`evals/composer-live-audit/spikes/S3-multifile-custody/prototype.patch`;
    144 source lines across 5 files).
- **Conditions.**
  - Keep `inline_blob` out of named sources (schema-rejected).
  - Reject `blob_id` together with `options.path`/`options.file` as
    `source_binding_ambiguous`.
  - A hash-matched reviewed binding plus `blob_id` is also
    `source_binding_ambiguous`, rather than one silently winning.
  - The proposal blob-reference extractor collects `sources.<n>.blob_id`.
  - Update the pinned canonical field inventory (`named_source` row in
    `pipeline_capabilities.md`).
- **Test files.** `tests/unit/web/composer/test_named_sources_blob_binding.py`
  (new). The s10 combining topology passes `build_set_pipeline_candidate`
  and `validate_pipeline_for_trained_operator`: two named csv sources, a
  per-source `value_transform` literal (`quarter`) into one sink, plus the
  queue → passthrough variant. Do **not** use `row_union`: it is a barrier
  over fork branches, not a fan-in of independent sources. Use
  `s10_seq12_pipeline.json` (the model's own attempt) as a fixture.
- **Pinned tests that change.** Two cases in `test_set_pipeline_candidate.py`
  now round-trip. The planner fixed-scaffolding byte budget in
  `test_pipeline_planner.py` is exceeded by 911 bytes; re-baseline it.
- **Test command.** `pytest tests/unit/web/composer/test_named_sources_blob_binding.py -n 0 -q`.
- **No compatibility.** Do not add migration, dual-shape reading or shims
  for older stored states (D2).
- **Also run.** The testcontainer suite, if persistence code changes.
- **Live evidence (S3, n=2).** With the prototype, the planner authored a
  valid two-source pipeline with the `quarter` column on its first proposal,
  in 10.8 s and 29.3 s. The audit baseline was a 240 s timeout. Acceptance:
  s10 builds on the first or second proposal, and the follow-up turn ("only
  keep invoices over 1000") completes without error.
- **Size.** 0.5–1 agent-day (S3).
- **Depends on.** CDA-12.

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

Also (S4): on Anthropic routes the planner's byte-stable `authoring_aids`
message has no cache marker, so about 20k tokens per planner call are billed
uncached (Sonnet planner cache hit 74%, against 99% in the edit loop). Adding
the marker is a trivial fix candidate; measure it.

### CDA-18: Drop credential-shaped reasoning instead of failing the turn (R6(a), F4; D1 approved)

- **Goal.** A credential-shaped string found only in `reasoning_content` or
  `reasoning_details` removes that reasoning from the call record. The event
  is recorded with surface and detector category, never the value, and the
  turn continues.
  - A hit in `thinking_blocks` (replayed, signature-bound), visible content or
    tool arguments still fails exactly as today.
  - This applies to planner, tool-loop and advisor calls.
  - **Opaque fields are never pattern-scanned** (S4): `reasoning_details`
    items of type `reasoning.encrypted` and signature fields carry base64url
    text. Keep or drop them by provider contract. On GPT-5.6 Luna an `sk-`
    run inside one such blob failed r01 T3 ("Run it.") with 422 (1 of 37
    blobs).
- **Files.**
  - `src/elspeth/web/composer/llm_response_parsing.py`
    (`build_llm_call_record`, ~L740-770): the scan point.
  - `src/elspeth/web/credential_guard.py` (~L91-126): the
    reasoning-specific helper.
- **Test files.** `tests/unit/web/composer/test_reasoning_credential_scrub.py`
  (new). Using the s06 shape (`"password": "Client emailed the admin pass…"`
  in reasoning only):
  - the call record has the reasoning removed;
  - a scrub event is recorded;
  - no exception is raised;
  - the same string in tool arguments still raises;
  - the Luna blob shape (`reasoning.encrypted` whose `data` holds `sk-` plus
    20 or more base64url characters) does not fail the turn and is not
    reported as a credential hit.
- **Pinned tests to update.** `tests/unit/web/test_credential_guard.py`
  (~L520-560), `tests/unit/web/composer/test_pipeline_planner.py` (~L5385)
  and `tests/unit/web/composer/test_compose_loop_persistence.py` (~L796-806).
  Change only the assertions about reasoning-only hits.
- **Test command.** `pytest tests/unit/web/composer/test_reasoning_credential_scrub.py tests/unit/web/test_credential_guard.py -n 0 -q`.
- **Depends on.** CDA-06.

### CDA-19: Credential guard stops refusing label-style prose (F4b; D6 approved)

- **Goal.** Visible prose such as "**Credentials:** managed by the operator
  through the secret store." or "Credentials: managed by your operator, never
  stored in the pipeline." passes. Real assignments still fail:
  `password: hunter2`, `api_key = sk-…`, `"password": "Tr0ub4dor&3"`.
- **Files.** `src/elspeth/contracts/credential_material.py`. Narrow the
  `credential_assignment` pattern (~L85 onwards) so the value must be a
  credential-shaped token, not purely markdown or punctuation, and not
  followed by further prose words. Keep the detector version semantics:
  bump `detector_version` if the stored contract requires it.
- **Test files.** `tests/unit/web/test_credential_label_prose.py` (new).
  - Both label sentences above pass.
  - The S4 Sonnet reply (in the S4 note) passes.
  - `password: hunter2`, the s06 reasoning sample and every existing
    positive in `tests/unit/contracts/test_credential_material.py` still
    refuse.
- **Test command.** `pytest tests/unit/web/test_credential_label_prose.py tests/unit/contracts/test_credential_material.py -n 0 -q`.
- **Also run.** `tests/unit/web/test_credential_guard.py` and
  `tests/unit/web/test_credential_boundary_inventory.py`. This is a security
  control, so add a negative control for every relaxed case.
- **Depends on.** CDA-06.

### CDA-20: Retry provider 429s with backoff inside the turn deadline (D7 approved)

- **Goal.** A provider 429 (or other `retryable` failure per
  `provider_errors.classify_provider_failure`) on a composer or planner call
  is retried with bounded exponential backoff and jitter, honouring
  `Retry-After`, only while the remaining turn deadline allows. Each attempt
  is recorded as its own LLM call record with an attempt index; no attempt is
  hidden from the audit trail. A non-retryable failure is unchanged.
- **Files.**
  - `src/elspeth/web/composer/provider_gateway.py`: a shared retry helper
    around `_litellm_acompletion` (~L451-569).
  - `src/elspeth/web/composer/provider_errors.py`: retry-after extraction.
  - If the planner calls `_litellm_acompletion` directly
    (`pipeline_planner.py` ~L3189, ~L3789), route it through the helper.
  - The advisor already retries twice (`advisor_checkpoint.py:1348`); align
    it or document why not.
- **Test files.** `tests/unit/web/composer/test_provider_rate_limit_retry.py`
  (new). With a fake provider that returns 429 twice and then succeeds:
  - the turn succeeds;
  - the audit holds 3 call records;
  - the backoff respects `Retry-After`;
  - with too little deadline left, the turn fails with today's message and
    no extra attempt.
- **Test command.** `pytest tests/unit/web/composer/test_provider_rate_limit_retry.py tests/unit/web/composer/test_provider_errors.py -n 0 -q`.
- **Depends on.** CDA-17.

### CDA-21: The turn deadline starts at worker claim, not submission (D7 approved)

- **Goal.** Time a turn spends queued no longer consumes its
  `composer_timeout_seconds`. The deadline is set when a worker claims the
  operation. Keep a separate, generous queue-wait bound, so a request cannot
  wait forever and the user sees a distinct "queued too long" outcome. Today
  `deadline_at` is fixed at submission
  (`coordination/composer_operation_authority.py` ~L401).
- **Files.**
  - `src/elspeth/web/coordination/composer_operation_authority.py`
    (insert ~L380-405, `claim_next` ~L497).
  - `src/elspeth/web/sessions/composer_async_worker.py`: deadline reads
    (~L448, L760, L947, L1402-1432).
  - The frontend's `deadline_remaining_ms` consumers, only if the wire
    meaning changes.
- **Test files.** `tests/unit/web/coordination/test_composer_operation_authority.py`.
  - An operation queued for longer than the timeout still gets the full
    timeout once claimed.
  - An operation that exceeds the queue-wait bound fails with its own
    outcome.
- **Test command.** `pytest tests/unit/web/coordination/test_composer_operation_authority.py -n 0 -q`.
- **Also run.** `pytest tests/ -m testcontainer -n 0`: this changes
  persistence and claim/lease behaviour under contention.

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

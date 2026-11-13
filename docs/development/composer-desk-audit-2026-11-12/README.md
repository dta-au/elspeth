# Composer desk audit: tool calls, skills and advisor (2026-11-12, revised 2026-11-13)

Audit of the Web Composer's model-facing surface ahead of a live executive
demonstration. It covers the composer loop, the empty-session planner, the
advisor, the tool registry and the skill/plugin guidance those models are
taught.

**Revision 2026-11-13.** Seven independent adversarial reviewers re-derived
every claim from the code and the raw captures. This version applies their
corrections; see [Independent review](#independent-review).

Read alongside:

- [remediation-plan.md](remediation-plan.md): fixes, performance work and
  enhancements, sequenced into work packages, plus the demo-day runbook.
- [work-packages.md](work-packages.md): the fix list and subagent lanes
  (with [lanes/tickets.json](lanes/tickets.json) for the lane-manager CLI).
- [tool-call-register.md](tool-call-register.md): every tool on every surface,
  generated from the live registries and joined with live call outcomes.
- [evidence/](evidence/): turn table, per-surface teaching cross-check,
  verification reports, screenshots and the seven review reports
  (`evidence/review/`).

Code baseline: `main` at `1a7ff0e6` (0.8.11 candidate merge).

> **Scope of the evidence.** Live results come from one model pair, accessed
> through OpenRouter: `deepseek/deepseek-v4.1-flash` as composer and
> `z-ai/glm-5.3` as advisor. Most scenarios ran once. Rates and latencies are
> indicative of that pair, not of the product with another model.
> Code-level causes are model-independent.

## Bottom line for the demo

**Go, with the runbook.** Over 27 sessions and 70 turns:

- 66 turns completed;
- 4 failed (2 × F4, 2 × F1);
- 3 completed turns had their reply withheld (F2, F2, s09 T2);
- 1 turn ended with the completion advisory not cleared (s03 T4).

Edits on an existing pipeline produced valid graphs in every run. That
validity was measured after the test driver auto-accepted each review card.
Edit turns took a median of 24 s, with 5 of 29 above 40 s (maximum 110 s).
Every tool the editing loop is taught is one it is offered, and no live call
went to a tool that was not offered.

The failures cluster where a presenter or an audience is likely to go:

| # | Demo phrasing | What the audience sees | Live (this model pair) |
|---|---|---|---|
| F1 | Attach two files to a new session: "combine these" | 240 s spinner, then "The composer request timed out" | 1 session, both turns failed |
| F2 | "Can you connect to SharePoint?" / "Can you read PDFs?" / "Is this safe on sensitive data?" | A correct answer replaced by "Verified checkpoint evidence covers 0 saved draft revisions…" and a "Completion advisory review did not clear" note | SharePoint 2/2; 4 of the 6 "not available here" answers withheld |
| F18 | **Tutorial** (meant to be a fixed script): pressing Apply on "Consider adding error routing…" (offered on every build), or typing in the still-enabled chat box | All cards done, Checks green, the chat says "your pipeline's ready", then **Continue to Run** refuses: "The saved tutorial pipeline does not match the supported tutorial plugin set." | 1/1 when Apply is pressed |
| F8 | Every follow-up edit or question | Each reply ends with a system note: "Checkpoint save status: Verified checkpoint evidence covers N saved draft revisions…" | 29 of 29 tool-loop replies |
| F3 | "What changed since the version I accepted?" | "I couldn't produce a change list: this session has no comparison baseline loaded" | 4/4 tool calls failed |
| F4 | Screen notes "for passwords or confidential material" | HTTP 422: "This control content appears to contain a credential" | 2/2 turns failed |
| F5 | "Use GPT-4o" / "Which models can I use?" | A refusal, or the opposite: a claim that it can pin a model by name | 3 of 3 such turns misleading |

Three risks the runbook cannot fully control:

- the audience typing capability questions (F2);
- hands-on tutorial users pressing Apply (F18);
- anyone attaching more than one file to a new session (F1).

The most serious **product-integrity** issue is F14. An interpretation the
user approves can silently fail to reach the prompt that runs. It does not
show in a demo, but it should be fixed before real use.

## Method

1. **Model's-eye view.** I rendered what each surface is sent from the
   production functions, plus captures of real provider requests. There are
   four surfaces:

   | Surface | Model | Tools |
   |---|---|---|
   | **Tool loop** | composer | 45: 43 registry tools plus `handoff_to_user` and `restore_parked_graph` |
   | **Planner** (empty session) | composer | 23: discovery, blob and secret lookups, the two recovery controls, and the terminal `emit_pipeline_proposal` |
   | **Planner escape hatch** | **advisor** | The same 23-tool planner palette (`pipeline_planner.py:4275`, `planning_application.py:1152`) |
   | **Reply stage** (after review cards are staged) | composer | Only the 2 recovery controls (`service.py:1968-1993`) |

   The advisor itself is offered no tools.
2. **Static cross-check.** I compared, per surface, which tools and
   identifiers the prompt teaches against which tools it is offered. The
   matcher was controlled against a known positive and a known negative. The
   project's own gates `generate_skill_inventory.py --check` and
   `composer_wire_census.py --scorecard` both exit 0; a reviewer injected
   drift and confirmed they catch it. The scorecard covers registry tools
   only, not the recovery controls or the planner terminal.
3. **Live battery.** Six local `elspeth web` instances ran from this commit.
   - **Budgets:** composition 10, discovery 24, timeout 240 s.
   - **Capture:** a litellm callback recorded every provider request and
     response, including the composer's `reasoning_content`.
   - **Battery:** 27 sessions, 70 user turns, 258 provider calls.
   - **Later runs:** re-runs and tutorial checks bring the total to 33 sessions
     and 85 turns. A 10-case smoke run and one smoke session are not counted.
   - **Model reasoning as evidence:** it showed where the instructions confused
     the model. Every finding was then traced to code.
   - **Turn table:** `evidence/battery-turns.md`, regenerated by
     `evals/composer-live-audit/turn_table.py`.
4. **Browser walks.** The workspace and the first-run tutorial were driven
   with Playwright against the built frontend.
5. **Verification and review.** Two read-only agents checked advisor
   evidence parity and LLM-node wiring. Seven further agents adversarially
   reviewed the whole audit; their reports are in `evidence/review/`.

Deviations from production:

- Prompt guard ran in `debug-when-unconfigured` mode (no Azure Prompt Shields).
- SQLite stores, local auth, and one OpenRouter LLM profile (`standard`).
- The audit user was granted the deployment-wide `user` role through a test
  fixture.
- These do not change code-level causes. Rates and latencies may differ.

The harness and scenarios are in `evals/composer-live-audit/`, so the battery
can be re-run against the demo deployment.

## Findings

Severity: **DEMO-BLOCKER** means likely to fail visibly on a natural
phrasing; **HIGH** means it costs turns or gives a wrong or confusing outcome
on common paths, or is an integrity defect; **MEDIUM** is recoverable or an
edge path; **LOW** is polish. Verification is **EXECUTED** (reproduced live
or in-process), **READ** (traced through code), **REPORTED** (from your
session, not reproduced) or **SUSPECTED**.

### DEMO-BLOCKER

#### F1. A new session cannot combine two attached files; the planner burns the whole deadline trying

- **Root cause: a schema/custody gap** (verified by RV1 across every route):
  - The planner can bind exactly one file, through the singular `source.blob_id`.
  - Named `sources` entries forbid `blob_id` and `inline_blob` ("Values share
    source's shape minus blob_id/inline_blob", `tools/sessions.py:2188`).
  - The schema does not allow `source` and `sources` together.
  - The `blobs` list that `blob_rows` needs is server-owned.
  - Source paths are confined to session blob storage, whose location the
    planner is never told.
- **Contributing:** guidance names tools the planner does not have (F7):
  - the skill, and the `blob_rows` hint "Bind blobs with
    set_source_from_blobs — never author or edit the blobs list directly"
    (`plugins/sources/blob_rows.py:176`), point at a tool-loop-only tool;
  - the repeated "emit the terminal proposal now" notice (F10) pressed the
    model to conclude a binding "must be possible".
- **What happens (EXECUTED, s10, 1 session, 2 turns).**
  - **T1.** "Combine these two quarters into one file…" with `q1.csv` and
    `q2.csv` attached. The composer made discovery calls and 3 proposal
    attempts; the last two were rejected (`canonical_schema`, then
    `source_path_policy_invalid`). The planner then escalated to the
    **escape hatch**, which runs on the advisor model with the same 23-tool
    palette.
    - The hatch spent about 141 s over two calls. The second was cancelled at
      the 240 s deadline.
    - The user got `504 composer_operation_deadline_expired`: "The composer
      request timed out. Reload to review the current session state before
      resubmitting."
    - The hatch's reasoning states the gap: "That tool isn't available in this
      palette."
  - **T2.** "Now only keep invoices over 1000". The session was still empty,
    so the planner ran again.
    - There was one truncated reply and two rejections.
    - The hatch's reply then hit the 16,384-token output cap
      (`finish_reason=length`), giving 500 `planner_repair_exhausted`.
    - These are the only 2 output-cap truncations among 110 planner calls.
- **Fix.** R4. Only R4 makes the request buildable; R1 alone does not.
- **Interim fix.** The rejection should name the problem. Today the planner
  sees only `sources/item additionalProperties`.

#### F2. The advisor withholds correct "not available here" answers because it never sees the catalog they came from

- **Where.**
  - The advisor's system prompt is the full composer skill, as "binding local
    policy" (`advisor_checkpoint.py:453-454`). So it applies the skill's rule
    that capability claims must come from "the supplied discovery digest…".
  - The digest is deliberately excluded from the advisor transcript, on the
    planner path (`pipeline_planner.py:3494-3514`) and the tool loop
    (`service.py:2453-2462`).
  - No section of the advisor message carries a catalog
    (`advisor_context.py:223-331`).
- **What happens (EXECUTED; recount by RV2).** 23 completed empty-session
  turns produced no proposal: 19 questions plus 4 declined build requests.
  4 of those 23 were withheld.
  - **Withheld:** "Can you connect to our SharePoint?" (2/2 runs). "Can you
    read PDFs?" (1/2; the two runs used different plugin allowlists). "Is this
    safe to run on sensitive data?" (1/2; the withheld reply rested on
    `authoring_aids.prompt_shield`).
  - **Admitted:** "Can you send results to Teams?" with the same kind of
    evidence.
  - Of the 6 "this deployment can't do X" answers, 4 were withheld.
  - The planner path gets one advisor review and no repair turn
    (`planning_application.py:938-1010`). The inner `attempts = 2`
    (`advisor_checkpoint.py:1348`) only retries provider or parse failures.
- **What the user reads.** The withheld body is replaced by
  `render_saved_scope()` (`turn_effects.py:37-39`): "Verified checkpoint
  evidence covers 0 saved draft revisions. There is no net authored change
  from the starting draft." It renders as ordinary assistant text. A
  "System note" follows: "Completion advisory review did not clear…
  Pipeline readiness was not re-verified this turn…"
  (`no_tool_policy.py:207-212`). Purpose-written withheld copy exists but is
  unused in chat (`advisor_policy.py:451-455`).
- **Also (READ, s04 T4).** After a withhold, the next turn's context holds
  only the saved-scope sentence. The composer loses its own correct answer
  and later contradicts it ("I couldn't confirm a SharePoint connector").
- **Fix.**
  - **R2:** give the advisor the compact capability record from
    `advisor_abandonment.py:109-129`, stored on `TurnEvidence` so all five
    `review_candidate` call sites agree. Add one rubric sentence: the record
    proves existence, absence or prohibition, not capability details.
  - **R3:** user-facing copy, and carrying the withheld context forward.

#### F18. The tutorial is meant to be a fixed script, but its Build step lets the learner change the request; one click breaks it with no actionable message

**Design intent** (ADR-031, confirmed by the maintainer 2026-11-13): the
tutorial sends a **fixed** brief, and the learner is not meant to edit it.
The brief is fixed "so a broken general Composer affordance cannot be hidden
by rephrasing".

**Gap.** After the brief is sent, the Build step mounts the ordinary
freeform chat (`ChatPanelContent`), restricted only by `allowFork={false}`
(`TutorialFreeformShell.tsx`). Six controls still send the composer
something other than the script:

- the free-text box with Send, and file upload (`ChatPanel.tsx:1323`);
- **Apply** on validation suggestions (`ChatPanel.tsx:853-857`);
- the repair-graph button (`:861`);
- "Ask the composer about this" (`:863`);
- "Edit the list" on the inline source (`:899-910`);
- "Use as input" in the blob manager (`:935-941`).

The browser canary (`tests/e2e/tutorial.spec.ts`) exercises none of them,
so CI stays green. The case below is the one observed.

- **Where.**
  - The tutorial's **Continue to Run** calls `GET /api/tutorial/{id}/readiness`.
    That runs run-admission `_tutorial_launch_blocker`
    (`composer/tutorial_service.py:154-232`), which requires **exactly one**
    output and that it be `sink:json` (L174). This one-output rule exists
    only in code; ADR-031 does not document it.
  - The decision panel offers **Apply** on validation suggestions. Apply sends
    the suggestion to the composer as a user request (`ChatPanel.tsx:853-857`).
  - One suggestion is "Consider adding error routing to a retention output —
    failed rows are currently discarded" (`composer/state.py:10079`).
    - It appeared on 3 of 3 tutorial-brief builds.
    - When it is the only suggestion its Apply is a primary button
      (`DecisionPanel.tsx:390`).
    - It is counted in "Awaiting your decision (N)" (`decisionPanelRows.ts:166-187`).
- **What happens (EXECUTED, browser walk).**
  - After Accept and then Apply, the composer added a `failed_rows` json output
    and routed every failure there. The advisor returned CLEAN.
  - The learner then made 4 more decisions: 3 Acknowledge, plus a prompt review
    whose Approve unlocks only after View prompt.
  - Checks showed ✓. The chat said "Every card acknowledged — your pipeline's
    ready. Select **Run pipeline** to start it." This line comes from
    `stores/subscriptions.ts:458-463` and fires on workspace validity, not
    tutorial readiness. There is no Run pipeline button in that step.
  - Continue to Run returned 409 `tutorial_plugin_set`. Screenshot:
    [evidence/tutorial-continue-refused.png](evidence/tutorial-continue-refused.png).
  - Nothing names the extra output. The composer cannot see the admission
    rule, and must not be taught tutorial-only rules (Composer invariant 2).
- **Without Apply** the composer chose "discard" with one output in 3 of 3
  sends of the same brief. In-process, those states pass the plugin-set and
  profile gates; the readiness endpoint itself was not called for them.
- **Recovery.** Typing "Remove the failed_rows output and discard failures
  instead." fixed it in one ~26 s turn when tried once. That is itself an
  edit of the fixed script, so the runbook says exit and restart the
  tutorial instead.
- Whether your 2026-11-13 session was this case is unverified; no capture of
  it exists.
- **Fix.** R16: make the Build step script-only, so only card decisions are
  possible after the brief, as ADR-031 intends. Name the difference in the
  409, and make the e2e canary assert those controls are absent.

### HIGH

#### F8. A backend receipt ends every follow-up reply

- **Where.** `composition_completion.py:2632`, `service.py:2679,3186`.
- **What happens (EXECUTED).** 29 of 29 tool-loop replies end with a System
  note: "Checkpoint save status: Verified checkpoint evidence covers N saved
  draft revisions. There is/are … Checkpoint evidence does not establish task
  completion." It appears even on pure explanations. Planner replies never
  carry it (0 of 41). On withheld tool-loop turns the sentence appears twice
  (s09 T2).
- **Why it can't just be removed.** The receipt is the only backend check
  against a hallucinated "I updated X". R3 restyles it rather than deleting it.

#### F14. An approved interpretation can silently never reach the prompts that run (integrity)

- **Where.** When a user accepts or amends a `vague_term` review, the resolver
  writes the value only into the node-level `prompt_template`
  (`web/sessions/pending_interpretation.py:818-822`). An `llm` query with its
  own template never uses the node-level one
  (`plugins/transforms/llm/transform.py:644-649`).
- **What happens (EXECUTED in-process).**
  - When every query has its own template, no query's rendered prompt contains
    the approved value.
  - In the mixed case, it reaches only the queries without their own template.
  - The staging gate `vague_term_wiring_count` (`interpretation_state.py:2947`)
    does not check this.
  - The attested prompt hash omits the unused node template
    (`interpretation_state.py:2805-2807`).
- **Teaching makes it likelier.** `planner_authoring_aids.py:867-870` and
  `multi_query.py:248-249` tell the model to put reviewed slots in the
  node-level template.
- In the live battery, DeepSeek noticed and avoided it in s08.
- **Fix.** R13.

#### F3. `diff_pipeline` cannot succeed anywhere in the web tool loop

- **Where.** The handler fails with "No baseline available. Load or create a
  session first." whenever `context.baseline is None`
  (`tools/generation.py:4659-4668`). No tool-loop dispatch path supplies a
  baseline (RV3 traced every `execute_tool` and `ToolContext` caller,
  including `session_tool.py` and the proposal and commit routes). The tool
  description promises one.
- **What happens (EXECUTED).** 4 of 4 live calls failed.
  - Only s01 T6 had a real change since acceptance (v2→v16), and the user got
    "I couldn't produce a change list…".
  - In r01–r03 nothing had changed since acceptance, so the composer's
    "Nothing has changed" in r02 was correct.
  - The failure result also reports `is_valid: false` on a valid pipeline.
  - The "load or create a session" advice is impossible to act on.
- **Fix.** R5.

#### F5. The skill teaches model selection by slug, which no web deployment exposes; `list_models` returns an unfiltered catalog

- **Where.**
  - The skill says an `llm` node needs "either `options.profile` or a
    catalog/discovery-served `options.model`", and teaches the
    `llm_model_choice` card (`pipeline_composer.md` ~L892-903, ~L1236). It
    also cites a `model_catalog` aid that no request supplies.
  - On the web, every llm schema goes through the profile registry, and
    `model` is profile-private (`catalog/policy_view.py:191-201`,
    `LLM_PROFILE_PRIVATE_FIELDS`). RV3 found no web configuration that exposes
    `model`; both llm plugins are `OPERATOR_PROFILED`.
  - All 14 live llm schema reads had `profile` and no `model`/`provider`.
  - `list_models` returns litellm's whole catalog: 3,938 models under 91
    provider prefixes, including image-size and quality tiers such as
    `1024-x-1024` and `standard`.
- **What happens (EXECUTED).**
  - In p01 T1 and s08 T2 the composer told the user it could pin a specific
    model slug, and the advisor admitted both.
  - In s02 T4 ("Use GPT-4o") it refused correctly, but claimed "GPT-4o is
    served there", inferred from the litellm dump.
- **Fix.** R7.

#### F4. The credential guard fails the whole turn on credential-shaped text in the model's private reasoning

High in general; a DEMO-BLOCKER for any data-protection or security-screening
demo.

- **Where.** `build_llm_call_record` scans `reasoning_content` and
  `reasoning_details` for credentials (`llm_response_parsing.py:759-770` via
  `credential_guard.py:91-126`). The planner catches the refusal and fails the
  turn (`pipeline_planner.py:3913-3963`). The same scan covers tool-loop and
  advisor calls (`provider_gateway.py:155-160`, `advisor_checkpoint.py:620`).
- **What happens (EXECUTED, s06 T1 and T2).**
  - The corpus prompt `error_routing` asks to screen deal notes "for
    passwords, secrets or confidential material".
  - DeepSeek drafted sample notes in its reasoning: T1 tripped on
    `"password": "Client emailed the admin pass…`, T2 on `password: hunter2`.
    Even `password: h` trips the assignment pattern.
  - Content and every tool argument passed; the reasoning alone failed both
    turns with 422 `credential_material_rejected`.
  - The user, who typed no credential, read "This control content appears to
    contain a credential. Store the value through the secret service…".
  - Whether another model drafts such samples in reasoning is model-dependent.
- **Fix.** R6, which needs your decision. Its option (a) reverses behaviour
  pinned by tests (`test_credential_guard.py:520-560` and others) and must not
  apply to replayed Anthropic `thinking_blocks`.

#### F7. The planner is sent the tool-loop interaction policy: 23 tools it cannot call, 109 mentions

- **Where.**
  - The planner's system message equals `render_system_prompt(data_dir)`: a
    32.8k-char shared capability core, plus a 103k-char "Freeform Composer
    Interaction Policy".
  - It names 23 unoffered tools 109 times: `set_pipeline` ×34,
    `request_interpretation_review` ×19, `create_blob` ×8, and others. All
    but 3 mentions are in the policy part.
  - A further 11 mentions sit in the planner's own tool schemas (9 inside
    `emit_pipeline_proposal`), 8 in `authoring_aids` and 1 in the terminal
    instruction.
  - `emit_pipeline_proposal` is named by its tool description and the
    request's `instruction` field, not by the system message.
- **What happens (EXECUTED; RV1 and RV7 recount).** Of 110 planner-palette
  calls, the share whose reasoning works around the palette depends on the
  pattern used:
  - **Strict** (names an unoffered tool as unavailable): 24–31 calls, 22–28%.
  - **Broad:** 60–64%, dominated by the skill's own phrase "proposal-only",
    often used correctly.
  - Both patterns are in `evidence/surface-teaching-crosscheck.md`.
  - This is a contributing cause of F1, not its root.
- **Fix.** R1.

### MEDIUM

#### F10. The planner's "emit now" notice repeats and fires on an incomplete notion of done

- **Where.** `_ALL_INFORMATION_GAPS_CLOSED_NOTICE` ("All declared information
  gaps are closed; emit the terminal proposal now.", `pipeline_planner.py:217`)
  is re-appended after **every** fully covered discovery round (~L5138).
- **What happens (EXECUTED).**
  - It appeared in 29 of 41 battery planner turns, up to 5 copies in one
    request (s10 T1).
  - It arrives before attachments are inspected (s02, s09) and on
    informational questions (s04 T5).
  - It pushed s10 toward an impossible binding.
- **Fix.** R10; 6 tests pin the string.

#### F6. A default deployment cannot compute new fields

- **What happens (EXECUTED).** On the 14-plugin required core,
  "turn quantity and unit price into numbers, work out a line total" was
  declined (smoke, c01).
  - Type conversion at ingest works: the csv source coerces declared columns
    (`csv_source.py:186`).
  - Filtering works with a gate.
  - What fails is computing a new field (`value_transform`) or converting a
    type after ingest (`type_coerce`).
- **Prose names unavailable plugins.** Plugin `not_for`/hint prose names 6
  unavailable plugins in the planner prompt, for example `field_mapper`
  pointing at `value_transform`/`type_coerce`. The model noticed: "This is
  contradictory unless the digest is partial" (smoke-edit T1).
- **Fix.** For the demo, configure the allowlist (runbook). Product fix R8.

#### F11. The recommended "many rows → one briefing" shape carries a HIGH warning nobody can clear

- **Where.** `ReportAssemble` never declares `output_semantics()`
  (`plugins/transforms/report_assemble.py:247`), although the `document`
  sink's guidance recommends exactly this pairing
  (`document_sink.py:131, 453-455`).
- **What happens (EXECUTED).** s08 shows a high-severity "Semantic contract …
  declares no semantic facts … must declare output_semantics()".
  - No option clears it.
  - It does not block execution; the advisor returned CLEAN.
  - The same warning class appears in s12 (json source → `json_explode`).
  - The sink's own example uses `schema.mode: fixed`, which is a hard
    `sink_locked_extras` error after `report_assemble`.
- **Fix.** R11. Adding `output_semantics()` also turns `report_assemble` →
  `text` from a warning into an error, which is a compatibility note.

#### F16. Completion gate: a narrow non-recompute path, and a gate that a clean verdict cannot clear

This finding was rewritten after review; the original overstated it.

- **Where.** `composer_preflight.reuse_or_recompute_runtime_preflight`
  (`:344-380`) recomputes the turn-end preflight when:
  - the turn changed the pipeline;
  - **any option holds a `blob_ref`** (49 of 51 non-empty battery states do);
    or
  - the state is invalid.

  It returns `None` only for a valid, unchanged, blob-free state (for example
  an `llm` source) when the model did not call `preview_pipeline`. With
  `None`, a CLEAN advisor verdict cannot clear an earlier block:
  `AdvisorGatePassed` needs a non-`None` preflight, and a `None` decision
  keeps the prior block (`composition_completion.py:2582-2620`,
  `completion_gates.py:268-269`).
- **What the battery shows.** "Not re-verified" wording appears in 0 of 33
  tool-loop turns. It appears only on the four F2 empty-session withholds.
  - s03 T4 had a valid preflight. Its gate stayed shut by design: the advisor
    flagged that exact graph (`graph_rejected`), so only a graph change clears
    it. Its "after your next pipeline change" wording was accurate.
  - s09 T2 was the pending-review handoff.
- **What it does not block (EXECUTED in a browser).** Run stays enabled;
  gate facts change only `completion_ready`. The panel reads "Share inspect
  link is blocked. Run pipeline is still available."
  ([evidence/advisor-gate-ui-s03.png](evidence/advisor-gate-ui-s03.png))
- **Suspected, needs a browser check.** The frontend re-runs `/validate` only
  when the composition version changes. A gate fact saved on the same version
  (s03 T4, v11) may leave the panel and Share button showing the pre-turn
  verdict until reload.
- **REPORTED (2026-11-13).** The composer, asked why the pipeline wasn't
  ready, credited `preview_pipeline` with clearing the gate. That turn
  mutated the pipeline, so it recomputed and F16 does not explain it. No
  capture exists.
- **Fix.** R14 (reworked).

#### F19. The escape-hatch turn is not what the boot probe checks

- **Where.** The boot probe builds a 3-tool hatch request (recovery controls
  plus terminal; `boot_probe.py:202-216`) and its comment claims parity. The
  production hatch sends the full 23-tool planner palette to the advisor
  model (`pipeline_planner.py:4275`).
- **Consequence.** A schema rejection on the advisor endpoint would surface
  only on the hatch path, which fires after the planner has already failed
  (F1). The audit's first version repeated the probe's description in four
  places; all are now corrected.
- **Fix.** R17.

#### F9. "Correction to my previous message" when the user saw no previous message

- **What happens (EXECUTED, s02 T3, s20 T2).** The advisor rightly caught an
  overclaim in each draft.
- The repair instruction says "The end user has not read these findings",
  but its opening "Correct the reply's unsupported claims…" primes the
  model. The published reply opened "Correction to my previous message…" and
  "Correction: I misstated…".
- **Fix.** R9.

#### F17. The composer explains platform internals and recommends filing bugs (REPORTED)

- **Where.** The Reply Register (`pipeline_composer.md:230-254`) bars tool
  keys in unprompted summaries but allows identifiers "if the user asks".
- **What happens (REPORTED from your session).** The reply showed
  `{"tool": "preview_pipeline", "arguments": {}}`, described the "completion
  advisory gate" and suggested filing a bug.
- None of the battery's replies did this. The composer never sees why the
  gate blocked, so it guesses.
- **Fix.** R15.

#### F12. Tool-result legibility

- **Buried argument error.** Discovery failures in an empty session lead
  with irrelevant whole-pipeline errors ("No source configured"); the real
  argument error sits under `data.argument_error`.
- **Sibling argument names differ.** `get_plugin_schema(name)` takes
  `name`; `get_plugin_assistance(plugin_name)` takes `plugin_name`. The model
  used `name` for both (s19).
- **Proposal rejections (battery).** 13 of 40 `emit_pipeline_proposal` calls
  were rejected; 7 of the 16 rejections across all runs are
  `canonical_schema`.
  - Leaving the offending key unnamed is a deliberate value-free design
    (`pipeline_planner.py:2742-2749`).
  - Naming keys that are in the schema's closed vocabulary would cover all
    observed cases.
- **Misplaced option.** `patch_output_options` names the rejected
  `description` but not where it belongs.
- **Fix.** R12.

### LOW

#### F13. The planner's fixed reply sentence

On the 22 proposal turns the whole reply is a fixed sentence
(`protocol.py:209-236`), for example "I prepared the requested pipeline and
staged it for your review. It has interpretation review cards…". It encodes
the preflight verdict deliberately, but says nothing about what was built.
Planner turns that end in text are written by the model. Fix: E1.

#### F15. Default-allowlist documentation is stale

`docs/reference/environment-variables.md:587-592` lists 9 always-authorized
plugins; the compiler's required core (`plugin_policy/compiler.py:17-40`)
has 14.

#### F21. The configured discovery budget does not apply after the first turn

- **Where.** `composer_max_discovery_turns` (required setting; 24 in this
  audit's environment, 10 in the shipped profiles) is enforced only by the
  empty-session planner (`pipeline_planner.py:4858`). The tool loop counts
  discovery-only rounds (`service.py` ~L1809, `discovery_turns_used`) but
  never compares them with a limit: `service.py` holds no discovery maximum,
  only `_max_composition_turns` (~L664, ~L2181). The
  `convergence_discovery_budget` terminal the frontend and battery know about
  is reachable only from the planner.
- **What happens (READ; also raised independently by the hot-path recon).**
  On a non-empty session, a model that keeps calling discovery tools is bound
  only by the wall-clock deadline. Every round pays the validation and
  bookkeeping costs in the remediation plan's efficiency table. The battery
  never hit it (max 9 provider calls per tool-loop turn), so this is a guard
  gap, not an observed failure.
- **Fix.** CDA-22 (D8 approved). The startup budget warning already
  counts discovery turns as if they applied to both surfaces.

#### F20. Run-state copy ignores which screen the user is on

The panel copy "Run pipeline is blocked." (`DecisionPanel.tsx:99-101`) and
the chat line "Select Run pipeline to start it." (`stores/subscriptions.ts:458-463`)
are derived from workspace validity. Inside the tutorial they name a button
that does not exist there (F18).

## Performance (measured on the battery)

Turns are split by the surface that actually ran. Percentiles use the
nearest-rank method. Source: `evidence/battery-turns.md` (summary block).

| Measure | Planner turns (41) | Tool-loop turns (29) |
|---|---|---|
| Turn latency p50 / p90 / max | 21.5 s / 61.9 s / 241.6 s (timeout) | 23.6 s / 84.6 s / 109.6 s |
| Provider calls per turn p50 / max | 3 / 9 | 4 / 9 |
| Turns with an advisor call | 15 of 41 (proposal turns skip it) | 29 of 29 |

| Per call (258 calls) | Calls | Prompt tokens p50 | Prompt-cache hit |
|---|---|---|---|
| Planner (composer) | 102 | 54k | 92% |
| Tool loop (composer) | 97 | 69k | 94% |
| Advisor | 49 | 48k | 52% |
| Reply stage | 6 | 64k | 57% |
| Escape hatch (advisor model) | 4 | 61k | 39%, p50 52 s per call |

Cost drivers:

- every role receives the 136k-char skill;
- an advisor call on every tool-loop turn, including "Thanks";
- proposal rejections, 13 of 40 in the battery, each costing a full planner
  call;
- escape-hatch calls on the advisor model, which are slow.

Startup also warns `composer_turn_budget_underfunded` (34 configured turns,
about 16 fundable at 240 s). The harness adds up to about 1 s of polling
granularity per turn, and a call cancelled at the deadline leaves no capture
record.

## What worked well

- **Tool-loop edits completed in every run**, with valid graphs after the
  driver's auto-accepts (s01, s03, s05, s07, s12, s15, s16, s17, s20). Replies
  disclosed real tradeoffs, for example "JSON content under a .csv name", "a
  workspace holds a single pipeline", and move versus copy.
- **No phantom features at identifier level.**
  - Every backticked snake_case identifier in the skill was checked against
    shipped code. The total checked is 228–253, depending on how identifiers
    are split.
  - Only 7 were not found, all illustrative example values (`teeth_count`,
    `branch_one`, …).
  - This is a "the name exists somewhere in `src/`" check, not a semantic one.
- **Teaching and offering match on the tool loop.** It is offered exactly the
  45 tools it is taught.
- **No unoffered calls.** No live call was made to an unoffered or unknown
  tool. The check was validated by injecting both cases.
- **The advisor catches real overclaims** (F9 cases, s03 T4, s09 T2). The
  problem is the evidence it is given and the copy the user reads, not the
  idea.
- **Conversational handling.** "Build me a pipeline" asks good questions,
  prompt-injection requests are refused, and "Thanks" is handled. F2 and
  F18 are not covered by this claim.

## Not covered

- The tutorial **run** stage, which scrapes `dta-au.github.io`; pipeline
  execution after Run; results and run diagnostics. S5 reached Run twice,
  but this container's egress reset every scrape.
- Prompt guard in `required` mode (every instance ran
  `debug-when-unconfigured`).
- The demo's actual model and provider pair (S4 ran Sonnet 5.5 and GPT-5.6
  Luna as alternatives, once per scenario).
- Concurrent multi-user load beyond S5's measurements.
- Real review-card handling time (the driver auto-accepted).
- Attaching files to a non-empty session.
- YAML import and sharing; PostgreSQL stores.
- Anthropic and Bedrock model families, which differ in prompt caching and
  thinking replay.
- Whether F14 has affected stored records.
- Dollar cost per turn. The battery spent about US$4.8 on OpenRouter for this
  pair.

## Design spikes (2026-11-13)

Five spikes measured the hardest fixes before any lane starts. Notes are in
[spikes/](spikes/); instruments in `evals/composer-live-audit/spikes/`.

| Spike | Question | Answer |
|---|---|---|
| [S1](spikes/S1-advisor-evidence.md) | Does a capability record fix F2? | Yes, in replay: withheld-correct answers 25/30 → 30/30 admitted; planted false existence claims 20/20 withheld. Capability over-claims about real plugins are not caught (1/5). CDA-07. |
| [S2](spikes/S2-planner-overlay.md) | Does a planner-only overlay fix F7? | Partly. A short overlay cut missing-tool reasoning 33% → 2% and prompt tokens 36%, and turned s10 into an honest 10 s decline, but regressed harder builds (rejections 3 → 8). The overlay must be a rewrite of the planner-relevant policy, and the advisor must see it. CDA-12. |
| [S3](spikes/S3-multifile-custody.md) | Can named sources bind uploads (F1)? | Yes. With the prototype the planner built the two-file pipeline on its first proposal (10.8 s, 29.3 s; baseline 240 s timeout). Found a follow-up crash in `state_responses.py`. CDA-13. |
| [S4](spikes/S4-cross-model.md) | Are the findings DeepSeek-specific? | No. F3, F5, F8, F10 and F18 reproduce on Sonnet 5.5 and GPT-5.6 Luna; F1 and F2 on at least one. New F4 trigger in OpenAI's opaque reasoning blob. Sonnet is the strongest composer measured but not worth switching to before CDA-07 and CDA-19. |
| [S5](spikes/S5-load-and-tutorial-run.md) | Will it hold a room, and does the tutorial run? | About 8–10 simultaneous senders on defaults (modelled). Tutorial build ~31 s; Run blocked by this container's egress, so the run stage is still unmeasured. Double-submit and per-user rate limits behave. |

## Independent review

On 2026-11-13 seven Opus reviewers each tried to disprove one slice of this
audit against the code and the raw captures. Their reports are in
[evidence/review/](evidence/review/).

| Reviewer | Slice | Confirmed / corrected / over-or-understated / refuted |
|---|---|---|
| RV1 | Planner surface (F1, F7, R1, R4, R8) | 8 / 10 / 2 / 1 |
| RV2 | Advisor and completion gate (F2, F8, F9, F16, F17) | 8 / 5 / 1 / 2 |
| RV3 | Tool-loop tools (F3, F5, F10, F12, F13) | 0 / 5 / 1 / 0 |
| RV4 | Guard, plugins, LLM wiring (F4, F6, F11, F14, F15) | 6 / 5 / 1 / 0 |
| RV5 | Tutorial and runbook (F18, R16, WP0) | 9 / 10 / 2 / 1 |
| RV6 | Register, evidence, performance | 9 / 10 / 1 / 1 |
| RV7 | Whole document and plan | 4 / 12 / 8 / 6 |

What changed as a result:

- **Corrected:**
  - **Escape hatch.** It was described as a 3-tool surface. It is the
    23-tool planner palette on the advisor model (F19).
  - **Counts and performance.** 68 turns became 70 (battery) and 85 (all);
    the performance table is now split by surface.
  - **F7 rate.** 60% became a strict and broad pair.
  - **F2 and F3 framing**, and **F4's code citation.**
  - **F16** was rewritten and lowered.
  - **The runbook's "safe" prompt list.** Two of its prompts failed live.
- **Severity changes:** F6 down to MEDIUM; F8 up to HIGH; F14 elevated as
  the top integrity risk.
- **Added:** F19 and F20, and the s04 T4 context loss under F2.
- **Upheld:** the core of F1, F2, F3, F4, F5, F14 and F18.

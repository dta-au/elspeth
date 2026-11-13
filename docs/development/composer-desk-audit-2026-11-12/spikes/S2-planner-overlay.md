# Spike S2: planner overlay instead of the freeform policy (CDA-12 / R1, F7)

Spike date 2026-11-13. Tree `c987b2b2`. Worktree
`.claude/worktrees/S2-planner-overlay` (branch `spike/S2-planner-overlay`, not
committed). Raw files are under
`.claude/lanes/composer-desk-audit-20261112/spikes/S2-planner-overlay/` (below:
`RAW/`).

## 1. Question

At HEAD the empty-session planner, and its escape hatch, are sent
`render_system_prompt(data_dir)`. That is the 32.8k-char capability core plus
the 103k-char "Freeform Pipeline Composer Interaction Policy". The policy names
23 tools the planner is not offered, 109 times (F7). It never names the
planner's own terminal tool, `emit_pipeline_proposal`.

R1 (lane CDA-12) proposes sending the core plus a short planner-specific
overlay instead. The question is whether that improves planner behaviour
without regressions:

- **Improvement wanted:** fewer reasoning episodes about missing tools, fewer
  `emit_pipeline_proposal` rejections, fewer prompt tokens, lower latency.
- **Regressions to rule out:** more invalid proposals, review requirements no
  longer staged, worse replies.

## 2. Method

### Prototype (EXECUTED)

The patch is `RAW/prototype.patch`, 156 lines across 3 files.

- **New overlay.** `skills/pipeline_planner.md`, 7,257 chars; the full text is
  in the Appendix. It covers:
  - the palette and the three ways a turn may end;
  - build-the-requested-shape rules;
  - an honest statement of the one-uploaded-file limit;
  - review staging inside node options, as a per-kind table;
  - LLM-step rules, cleanup before sinks, repair, and the Reply Register.
  - It names no unoffered tool.
- **`prompts.py`.** New `render_planner_system_prompt(data_dir)`. It returns
  `render_with_pipeline_capabilities(planner overlay)` plus the same deployment
  overlay as the tool loop (the existing seam, `capability_skill.py:155`). It
  also loads the overlay with its hash.
- **`service.py`.**
  - Renders `_planner_skill_text` once.
  - Asserts the overlay's on-disk hash, as for the other two skills.
  - Passes that text to `PlanningApplication` as `composer_skill_text`. That
    value becomes `rendered_skill` at `planning_application.py:1077` and
    reaches `messages[0]` at `pipeline_planner.py:3495`.
  - The hatch reuses `messages[:2]` (`pipeline_planner.py:3510`), so it gets
    the overlay too.
- **Unchanged:** the tool loop, the advisor checkpoint, the tool schemas,
  `authoring_aids` and `PLANNER_TERMINAL_INSTRUCTION`.
- **Invariants:** no composer invariant is touched. The change is prompt text
  only, with no server-authored structure and no tutorial path.

### Static measurement (EXECUTED, `RAW/static_measure.py` → `RAW/static_measure.json`)

- **What it measures:** the whole first outbound planner request.
  - System message, rendered in-process for each arm.
  - Planner tool schemas: `planner_tool_definitions(initial policy, NONE)`.
  - `authoring_aids`: `messages[1]` of a captured audit planner request
    (`live/sessions/s10-two-files/turn01`).
  - The `instruction`.
- **Matcher:** `(?<![A-Za-z0-9_])NAME(?![A-Za-z0-9_])` over the 23 tools in
  `composer_loop_tool_definitions − planner_tool_definitions`.
- **Controls:**
  - positive `set_pipeline` = 34 (audit figure);
  - negative `nonexistent_tool_xyz` = 0;
  - boundary string → 1 (expected 1);
  - naive substring count = 35, which shows the boundary is doing work.

### Live A/B (EXECUTED, two batches)

Two instances, both on the broad allowlist with DeepSeek v4.1 Flash as the
composer and GLM-5.3 as the advisor and escape hatch:

- baseline `S2-base` on :8501, from the main checkout;
- prototype `S2-proto` on :8502, from the worktree.
  - Its environment carried the worktree `PYTHONPATH`.
  - Every captured prototype request had a 40,027-char system message, and
    every baseline request 135,863.

Each arm ran first turns only, through `evals/composer-live-audit/drive_session.py`
(`RAW/run_arm.sh`, `RAW/run_arm2.sh`; `RAW/scenarios/` = first turn, absolute
fixture paths). The scenarios ran serially, one driver per instance.

- **Batch 1 (08:42 UTC).** s01, s02, s03 and s20 completed on both arms, and
  s05 on the prototype. Then the shared OpenRouter account ran out of credit
  (402). The 402-failed attempts are kept in `RAW/failed-402/` and excluded.
- **Batch 2 (13:24 UTC, after a top-up).**
  - s10, c03, s05 (baseline), s08, s09, s12 and s13 on both arms.
  - Second repetitions of s01, s02, s03 and s20 on both arms.
  - `RAW/run_arm2.sh` checked the cost of the captured calls before each
    scenario, against a US$3.80 stop and any 402 or 429. Neither tripped.
- **Total:** 15 first turns per arm.
  - n = 2 for s01, s02, s03 and s20; n = 1 for s05, s08, s09, s10, s12, s13
    and c03.
  - The order was not randomised; each arm ran its own queue.

### Analysis instrument (`RAW/analyse.py` → `RAW/analysis_r1.json` for batch 1, `RAW/analysis_all.json` for both batches)

- Labels follow the `turn_table.py` rules.
- An emit rejection is the tool result for that `tool_call_id`, read from a
  later request, with `success: false`.
- Staged requirements are taken from the last emitted proposal.
- Review cards are the ones the driver resolved after accepting the proposal.
- **Strict palette-confusion** is counted per reasoning sentence. A sentence
  counts when it names an unoffered tool, contains a negation, and contains an
  availability word.
  - Controls run on every invocation (3 positives hit, 4 negatives miss).
    One negative is "The set_pipeline document shape is available to me…".
  - Calibration on the audit's own corpus (`live/sessions`): **32/110 planner
    calls (29%)**. RV1 found 31/110 and RV7 24/110, so the instrument
    reproduces the audit's strict figure.

**Spend** is summed from the per-call `usage.cost` of my captures
(`RAW/spent.py`):

- batch 1: US$0.134;
- batch 2: US$0.814;
- **total US$0.948.**

The account-level `usage_daily` delta is not attributable, because other spikes
shared the account.

**Rejections** are counted as the larger of two sources:

- the capture-derived count;
- the `composer.planner_attempt outcome=candidate_rejected` lines in the
  instance's `web.log`, joined by `session_id` (`RAW/rejections_weblog.py`).

Neither source alone is complete:

- `instance.sh` truncated batch 1's `web.log` when the instance restarted.
- A rejection that ends a turn never appears in a later request.

Aggregates come from `RAW/aggregate.py` → `RAW/aggregate.json`.

## 3. Results

### 3.1 Static: whole first planner request (EXECUTED, `RAW/static_measure.json`)

| Surface | Baseline chars | Prototype chars | Baseline unoffered-tool mentions | Prototype mentions |
|---|---|---|---|---|
| System message | 135,863 | **40,027** (−70.5%) | 109 (23 tools) | **3** (core only: `set_pipeline` L14, `set_source` L67, `request_interpretation_review` L156) |
| Planner tool schemas | 36,901 | 36,901 | 11 | 11 |
| `authoring_aids` (captured) | 54,861 | 54,861 | 8 | 8 |
| `instruction` | 595 | 595 | 1 | 1 |
| **Whole request** | **228,220** | **132,384** (−42%) | **129** | **23** |
| `emit_pipeline_proposal` named in the system message | 0 | 2 | | |

The remaining 23 mentions are outside the system message and still need R8,
R13 and the E3 scope:

- tool schemas: `request_interpretation_review` ×3, `set_source_from_blob` ×3,
  `set_pipeline` ×2, `set_source_from_blobs` ×2, `create_blob` ×1;
- `authoring_aids`: `create_blob` ×3, `request_interpretation_review` ×2,
  `set_pipeline` ×2, `set_source_from_blobs` ×1;
- core ×3 and instruction ×1.

8 offered discovery tools are not named in the prototype's system message:

- `diff_pipeline`, `get_audit_info`, `preview_pipeline`, `get_blob_content`,
  `read_attachment`, `get_blob_metadata`, `list_composer_blobs`,
  `validate_secret_ref`.
- They are taught only by their own schemas. The freeform policy's
  `read_attachment` / `get_blob_content` data-key teaching (`pipeline_composer.md:393-409`)
  no longer reaches the planner. See §4, gates.

### 3.2 Live A/B, 15 first turns per arm (EXECUTED; `RAW/analysis_all.json`, `RAW/rejections_weblog.json`, `RAW/sessions/{base,proto}/`)

Per run. "Calls" counts all provider calls, including the advisor and the
escape hatch. Card kinds are abbreviated:

| Abbreviation | Card kind |
|---|---|
| `inv` | `invented_source` |
| `prompt` | `llm_prompt_template` |
| `vague` | `vague_term` |
| `decision` | `pipeline_decision` |

| Run | Arm | Label | s | Calls | Prompt tok | Compl. tok | US$ | Rejections (codes) | Strict-conf. calls | Cards after accept | Valid |
|---|---|---|---|---|---|---|---|---|---|---|---|
| s01 r1 | base | PROPOSAL | 15.4 | 2 | 110,822 | 4,653 | 0.0080 | 0 | 1 | inv | yes |
| s01 r1 | proto | PROPOSAL | 10.1 | 2 | 70,185 | 3,230 | 0.0066 | 0 | 0 | inv | yes |
| s01 r2 | base | PROPOSAL | 22.3 | 2 | 110,800 | 4,418 | 0.0077 | 0 | 1 | inv | yes |
| s01 r2 | proto | PROPOSAL | 25.5 | 3 | 106,598 | 4,255 | 0.0095 | 1 (transform_on_success_dangling) | 0 | inv | yes |
| s02 r1 | base | PROPOSAL | 31.4 | 4 | 241,575 | 9,642 | 0.0195 | 1 (locked_input_extras) | 0 | prompt | yes |
| s02 r1 | proto | PROPOSAL | 16.3 | 3 | 113,498 | 4,347 | 0.0089 | 0 | 0 | prompt | yes |
| s02 r2 | base | PROPOSAL | 57.7 | 4 | 220,037 | 11,047 | 0.0171 | 0 | 1 | prompt, vague | yes |
| s02 r2 | proto | PROPOSAL | 10.5 | 3 | 103,616 | 3,025 | 0.0059 | 0 | 0 | prompt | yes |
| s03 r1 | base | PROPOSAL | 21.5 | 2 | 108,444 | 6,784 | 0.0099 | 0 | 2 | inv | yes |
| s03 r1 | proto | PROPOSAL | 11.1 | 2 | 66,100 | 3,672 | 0.0058 | 0 | 0 | inv | yes |
| s03 r2 | base | PROPOSAL | 18.2 | 2 | 108,435 | 4,426 | 0.0070 | 0 | 0 | inv | yes |
| s03 r2 | proto | PROPOSAL | 21.2 | 3 | 103,195 | 4,823 | 0.0092 | 1 (plugin_options_invalid) | 0 | inv | yes |
| s20 r1 | base | PROPOSAL | 10.1 | 3 | 163,351 | 2,303 | 0.0342 | 1 (canonical_schema) | 0 | inv | yes |
| s20 r1 | proto | PROPOSAL | 8.2 | 2 | 65,399 | 1,845 | 0.0126 | 0 | 0 | inv | yes |
| s20 r2 | base | PROPOSAL | 13.1 | 2 | 107,719 | 3,190 | 0.0053 | 0 | 1 | inv | yes |
| s20 r2 | proto | PROPOSAL | 21.4 | 2 | 65,415 | 3,107 | 0.0050 | 0 | 0 | inv | yes |
| s05 r1 | base | PROPOSAL | 40.4 | 2 | 111,042 | 11,360 | 0.0161 | 0 | 2 | inv | yes |
| s05 r1 | proto | PROPOSAL | 29.3 | 2 | 68,717 | 9,852 | 0.0140 | 0 | 0 | inv | yes |
| s08 r1 | base | PROPOSAL | 114.9 | 2 | 117,958 | 24,090 | 0.0335 | 0 | 2 | inv, prompt×2, decision, vague×3 | yes |
| s08 r1 | proto | PROPOSAL | 225.2 | 5 | 211,264 | 49,242 | 0.0757 | 2 (canonical_schema, canonical_schema) | 0 | inv, prompt, vague×3 | yes |
| s09 r1 | base | PROPOSAL | 44.3 | 3 | 163,503 | 10,157 | 0.0148 | 0 | 0 | prompt, vague | yes |
| s09 r1 | proto | FAILED: planner_repair_exhausted | 56.8 | 7 +hatch | 262,635 | 12,701 | 0.0535 | 4 (canonical_schema, plugin_options_invalid, sink_locked_extras, locked_input_extras) | 0 | — | — |
| s12 r1 | base | PROPOSAL | 20.3 | 2 | 110,379 | 4,198 | 0.0073 | 0 | 0 | inv | yes |
| s12 r1 | proto | PROPOSAL | 22.3 | 3 | 107,775 | 3,397 | 0.0069 | 0 | 0 | inv | yes |
| s13 r1 | base | PROPOSAL | 97.8 | 3 | 181,401 | 18,332 | 0.0302 | 1 (canonical_schema) | 0 | inv, prompt, vague | yes |
| s13 r1 | proto | PROPOSAL | 39.8 | 2 | 73,360 | 10,190 | 0.0158 | 0 | 1 | inv, prompt, vague | yes |
| c03 r1 | base | REPLY | 10.1 | 3 | 138,764 | 1,855 | 0.0229 | 0 | 0 | — | — |
| c03 r1 | proto | WITHHELD | 8.1 | 3 | 96,258 | 1,399 | 0.0215 | 0 | 0 | — | — |
| s10 r1 | base | WITHHELD | 183.2 | 9 +hatch | 515,041 | 38,616 | 0.3710 | 3 (canonical_schema, canonical_schema, canonical_schema) | 4 | — | — |
| s10 r1 | proto | WITHHELD | 10.2 | 3 | 95,140 | 1,469 | 0.0785 | 0 | 0 | — | — |

Aggregates (`RAW/aggregate.json`):

| | Baseline (15) | Prototype (15) | Baseline excl. s10 (14) | Prototype excl. s10 (14) |
|---|---|---|---|---|
| Labels | 13 PROPOSAL, 1 REPLY, 1 WITHHELD | 12 PROPOSAL, 2 WITHHELD, 1 FAILED | 13 PROPOSAL, 1 REPLY | 12 PROPOSAL, 1 WITHHELD, 1 FAILED |
| Seconds, sum / median | 700.7 / 22.3 | 516.0 / 21.2 | 517.5 / 21.9 | 505.8 / 21.3 |
| Provider calls | 45 | 45 | 36 | 42 |
| Prompt tokens | 2,509,271 | **1,609,155 (−36%)** | 1,994,230 | **1,514,015 (−24%)** |
| Completion tokens | 155,071 | 116,554 | 116,455 | 115,085 |
| Cost US$ | 0.6045 | **0.3294 (−46%)** | 0.2335 | 0.2509 (+7%) |
| Proposal rejections (turns with any) | 6 (4) | 8 (4) | **3 (3)** | **8 (4)** |
| Strict palette-confusion, planner-palette calls | **14/43 (33%)** | **1/43 (2.3%)** | 10/35 (29%) | 1/41 (2.4%) |
| Valid after the driver accepted | 13 | 12 | 13 | 12 |

#### What changed for the better (EXECUTED)

- **Palette confusion is essentially gone: 33% to 2.3% of planner calls.**
  - The one prototype hit is s13: "I should call request_interpretation_review
    — but my palette doesn't include it". It was prompted by the tool schemas'
    residual mentions (§3.1), which R1 does not touch.
  - The baseline rate reproduces the audit's 22-29% (s10 alone produced 4
    confusion calls, including GLM hatch reasoning).
- **s10 multi-file dead end: 183 s to 10 s.**

  | | Baseline | Prototype |
  |---|---|---|
  | Time | 183.2 s | 10.2 s |
  | Provider calls | 9 | 3 |
  | Prompt tokens | 515k | 95k |
  | Cost | US$0.371 | US$0.079 |
  | Rejected proposals | 3 (`canonical_schema`) | 0 |
  | Escape hatch (2 GLM calls) | yes | no |

  - **The baseline** submitted 3 rejected proposals, escalated to the escape
    hatch, and only then declined.
  - **The prototype** called `list_blobs` once and then declined in plain
    text. Its reply was honest and accurate:
    - "I can't combine those two files on this surface … can bind exactly one
      uploaded file as pipeline input … named multi-file inputs aren't
      available here … I can't invent a combined file".
    - It offered both workable options: combine the two files into one upload,
      or build for one quarter now and stamp the quarter label as a reviewable
      decision.
    - One Reply Register slip: it showed the user the option key
      `source.blob_id`.
- **Token use.** Prompt tokens fell 24-36%. On simple builds (s01-s03, s20,
  both repetitions) latency and cost fell too (the summed s and US$ columns
  above).

#### What got worse (EXECUTED; n = 1 per scenario unless stated)

- **More rejected proposals outside s10: 3 to 8.** The extra rejections are
  concentrated in the harder scenarios:
  - **s09 (angry/urgent routing): the prototype FAILED `planner_repair_exhausted`.**
    - Rejections, in order: `canonical_schema` oneOf (its first proposal
      carried an empty `sources: {}` beside `source`), then
      `plugin_options_invalid` (an LLM output suffix `flag` collided with the
      plugin's own fields), then `sink_locked_extras`.
    - It then went to the escape hatch, which was rejected with
      `locked_input_extras`.
    - The baseline built s09 first time.
  - **s08 (grant briefing).** Two prototype calls hit the 16,384-token output
    cap and emitted `{}` (85 s and 72 s; two `canonical_schema` rejections).
    The third call succeeded. Result: 225 s versus 115 s for the baseline.
    This is the RV1 M2 output-cap failure, now off the s10 path.
  - **One rejection each in s01 r2 and s03 r2.**
    - s01 r2: `transform_on_success_dangling`.
    - s03 r2: the first proposal put generated rows in a named `sources` entry
      (`source:expenses … path: Field required`).
  - **Named `sources`.** 2 of 15 prototype runs wrote it (s03 r2, s09); 0 of
    15 baseline runs did. The overlay's own multi-file paragraph mentions
    named `sources`, which is a plausible priming cause (INFERRED).
- **Missed review staging (s08).**
  - The baseline staged `pipeline_decision:prompt_injection_shield_recommendation`
    on its LLM node; the prototype did not.
  - The source rows were LLM-generated rather than uploaded, so whether that
    review was required is arguable. It is still a lost card.
  - The baseline built two LLM nodes (2 prompt cards); the prototype built one
    node with 3 `vague_term`s.
  - Every other card set matched across arms, except baseline s02 r2, which
    added a `vague_term` for the user's own categories.
- **c03 ("Which models can I use?"): baseline REPLY, prototype WITHHELD.**
  - Both planners gave essentially the same answer: the operator profile
    `standard`, about 3,967 catalog models, narrow by provider.
  - The advisor flagged the prototype's "standard" claim as unsourced, yet
    admitted the same claim in the baseline reply.
  - That is advisor variance (n = 1), not an overlay effect, but the user saw
    a withheld receipt.

#### The advisor hides the s10 win (EXECUTED)

**Both arms' s10 replies were WITHHELD** by the completion advisor. The user
saw only "Verified checkpoint evidence covers 0 saved draft revisions … Composer
completion is withheld".

The advisor flagged the prototype's honest decline as `request_not_met`, on
grounds that are false for the planner surface:

- "the pipeline language explicitly supports … `sources` plural container,
  `set_source_from_blobs` … Do not decline on a single-source limitation".
- The advisor's system text is still the freeform policy
  (`advisor_checkpoint.py:454`), which teaches `set_source_from_blobs`.

So R1 alone fixes the planner's reasoning but does not change what the user
sees on s10. The advisor half (§4 item 2) is required in the same lane.

### 3.3 Not run

- The tutorial scenarios (out of scope).
- A third repetition, and repetitions of s05-s13, c03 and s10. The US$4 cap was
  not the limit (US$0.95 spent); the coordinator's priority list ended at
  repetition 2.
- At n = 1, the s08 and s09 regressions could be variance. **They need at
  least 3 repetitions before the lane accepts the overlay** (see §4
  acceptance).

### 3.4 Gates and tests in the worktree (EXECUTED; logs `RAW/pytest_gates.log`, `RAW/pytest_service.log`, `RAW/gate_*.log`)

All of the following exited 0:

| Command (worktree `PYTHONPATH`, `-n 2`) | Result |
|---|---|
| 21 skill/teaching/advisor test files: `test_skills_loader`, `test_capability_skill_identity`, `test_prompts`, `test_tall_dialog_fixture_prompt_hash_pin`, `test_planner_teaching_gate`, `test_l4_teaching_text_drift`, `test_tool_knob_teaching_gate`, `test_tool_result_envelope_gate`, `test_planner_authoring_aids`, `test_planner_capacity_and_attachment_clarification`, `test_advisor_*` (4), `test_compose_loop_interpretation_review_dispatch`, `test_explicit_provider_custody`, `test_provider_context`, `test_provider_quota_end_to_end`, `sessions/test_skill_markdown_history_authority`, `scripts/test_composer_wire_scorecard`, `test_tool_declarations` | exit 0: 953 passed, 1 skipped |
| `test_service`, `test_pipeline_planner`, `test_prompt_guard_wiring`, `test_compose_service_structure`, `test_tutorial_service`, `test_tool_model_wire_parity`, `scripts/test_composer_wire_census`, plus 8 integration files (inline-source provenance, freeform proposal prevalidation, planner failure translation, prompt-review card e2e, pipeline custody, stated-threshold walk, composer-runtime agreement, interpretation runtime handoff) | **exit 1: 1505 passed, 5 failed.** All 5 are `test_stated_threshold_data_full_walk.py::test_referential_planner_threshold_retirement_preserves_first_candidate[*]`, failing at `:214` `assert kwargs["messages"][0] == {"role": "system", "content": composer._composer_skill_text}`. That pin says the planner system message IS the freeform skill text, so the failure is expected and caused by the prototype (R1 changes exactly this). |
| `generate_skill_inventory.py --check` | exit 0 |
| `composer_wire_census.py --scorecard` | exit 0 |
| `ruff check` / `ruff format --check` on the changed files | clean |

Apart from that one integration pin, nothing failed, and that is itself the
finding. The only test that pins what the planner is sent is
`test_stated_threshold_data_full_walk.py:214`, which asserts it EQUALS the
freeform text. No teaching gate measures the planner surface:

- Every teaching gate derives its "taught" side from `build_system_prompt(None)`,
  the freeform/loop rendering:
  - `test_tool_result_envelope_gate.py:2347`;
  - `composer_wire_census.py:863`;
  - `test_capability_skill_identity.py:55/67/144/232`.
- The envelope gate's own comment (`test_tool_result_envelope_gate.py:109-111`)
  already assumes "the planner reads pipeline_capabilities.md and its own
  briefs". That was false at HEAD and becomes true with R1.
- The planner tests inject `rendered_skill` directly, so none of them notices
  which text the service passes.
- `PlannerCapabilityManifest` only checks that the core prefix occurs once
  (`capability_skill.py:204-209`), and the prototype satisfies it.

So the prototype silently drops the freeform policy's read-tool teaching from
the planner, and no gate notices.

## 4. Recommendation for CDA-12

**R1 is worth doing, but not with this short overlay alone, and not without
the advisor half.** The evidence changed the recommendation after batch 2:

- **Confirmed gains.**
  - Strict palette confusion fell from 33% to 2.3% of planner calls.
  - The s10 dead end shrank from 183 s / US$0.37 / 3 rejections / hatch to
    10 s / US$0.08 / 0 rejections, with an honest decline.
  - Prompt tokens fell 24-36%; the system message is −70%.
  - These hold across 15 turns per arm.
- **Regressions the 7.3k overlay introduced or exposed** on the harder builds:
  - outside s10, rejections rose from 3 to 8;
  - 1 `planner_repair_exhausted` failure (s09), against 0;
  - 2 output-cap truncations (s08);
  - 1 lost prompt-shield review (s08);
  - named-`sources` misuse in 2 of 15 runs.
- **Likely cause (INFERRED, n = 1 per hard scenario).** The overlay dropped
  freeform-policy material the planner was really using:
  - the field-name and declaration rules in Source Facts;
  - the LLM Review Interactions detail, including the prompt-shield rule and
    query and output-field rules;
  - Requested Workflow Integrity;
  - the Mechanical Repairs table.
  - The `locked_*_extras` and output-suffix collisions are exactly that
    territory. The baseline paid ~20k extra prompt tokens per call for it, and
    got fewer rejections on s08 and s09.
- **The advisor still judges planner turns against the freeform policy**, and
  withheld the prototype's correct s10 decline. Without the advisor change,
  R1's most visible win does not reach the user.

### Measured effect (15 first turns per arm)

| Measure | Baseline | Prototype | Change |
|---|---|---|---|
| First-call prompt tokens | 52.5k | 31.4k | −40% (deterministic) |
| Prompt tokens, all turns | 2.51M | 1.61M | −36% |
| Cost, all turns | US$0.60 | US$0.33 | −46% (−7% excl. s10: +US$0.017) |
| Latency, sum (median) | 701 s (22.3) | 516 s (21.2) | s10 dominates |
| Strict confusion | 14/43 calls | 1/43 calls | −93% |
| Rejections excl. s10 | 3 | 8 | **worse** |
| Turns not PROPOSAL/REPLY | 1 (s10) | 3 (s10, c03, s09) | **worse** |

### Design (the prototype's wiring is the right shape; its overlay is too thin)

- Use the existing `render_with_pipeline_capabilities` seam, with
  `core + planner overlay + deployment overlay`.
- Leave the loop's text byte-identical, so there is no strip logic.
- Render once per service instance, as today.
- **Build the overlay by moving, not condensing.**
  - Start from the freeform sections the planner actually uses: Source Facts
    (field names and declarations), LLM Review Interactions and Subjective LLM
    Terms, Untrusted content, Output data minimization, Requested Workflow
    Integrity, Assumption Review (matrix), Termination States and the
    Mechanical Repairs table.
  - Rewrite them for the proposal surface: stage in options; no review tool;
    no mutation-tool names.
  - Drop only the loop mechanics: mutation tables, batching, echo reading,
    Blob Source macros, the advisor tool and `CLARIFY:`.
  - Expect roughly 30-50k chars, still well under 103k. Re-measure the hard
    scenarios before accepting.
- **Do not mention named `sources` in the multi-file paragraph.** Say "use the
  singular `source`; this surface reads one uploaded file". Two of 15
  prototype runs reached for `sources` after reading the current wording.

### What the lane must also handle (READ, with file:line)

1. **Skill-history archive and provenance hash.**
   - A planner proposal's `composer_skill_hash` is `proposal.skill_hash`
     (`sessions/service.py:3289`). That is the hash of the planner's
     `rendered_skill` (`pipeline_planner.py:3392`).
   - The planner `ToolContext` stamps the same hash on blobs
     (`pipeline_planner.py:3451`).
   - But `_maybe_upsert_skill_markdown_history` archives only
     `_composer_skill_text` (`service.py:950-976`).
   - At HEAD both hashes are equal, so the archive covers planner rows. With
     R1, planner-staged interpretation events and blob provenance would cite a
     hash with **no archived content**: an audit gap. The prototype does NOT
     fix this.
   - The lane must archive the planner text (filename `pipeline_planner.md`)
     under its own hash, and test that the planner proposal's hash resolves in
     `skill_markdown_history`.
2. **Advisor (REQUIRED in the same lane: EXECUTED evidence, §3.2).**
   - In s10 the advisor withheld the prototype's correct decline, citing
     `set_source_from_blobs` and plural `sources` from the freeform policy.
   - `AdvisorCheckpointOwner` still gets the freeform text
     (`service.py:764`, `advisor_checkpoint.py:454`).
   - On planner turns the advisor would judge planner output against a policy
     the planner no longer received. The prototype leaves this unchanged.
   - Decide explicitly: pass the planner text on planner-turn checkpoints, or
     state in `advisor_checkpoint.py` that the advisor reviews against the
     loop policy. Either way it is a second per-surface hash input.
   - The advisor cache-prefix tests (`test_advisor_cache_prefix.py`) pin
     prefix reuse.
   - Recommended: on planner-turn checkpoints, give the advisor the planner
     rendering plus an explicit statement of the planner palette, so it
     cannot demand a tool the surface lacks.
3. **Deployment overlay.** The prototype appends
   `{data_dir}/skills/pipeline_composer.md` to the planner prompt too. Add a
   test, mirroring `test_capability_skill_identity.py:370`, that the planner
   rendering carries it.
4. **Lost teaching.** See §3.1:
   - the `read_attachment` / `get_blob_content` data keys;
   - the full LLM Review Interactions detail: `source_data_contract`, the
     multi-query rules, prompt-shield coverage via `required_input_fields`;
   - Requested Workflow Integrity (A/B single-variable rule);
   - the Mechanical Repairs table.

   The overlay condenses or omits these. Either move each planner-relevant
   rule into the overlay or the core, or accept the loss explicitly. **E3 must
   also assert "every offered tool is taught" per surface, so this loss is
   caught.**
5. **Teaching gates are single-surface.** Make the envelope / knob / wire-census
   gates take the surface's rendering as a parameter:
   - planner rendering × planner tools;
   - loop rendering × loop tools.

   Otherwise the gates keep certifying the loop text while the planner text
   drifts.
6. **Failing pin.** Re-point `tests/integration/web/composer/test_stated_threshold_data_full_walk.py:214`
   (5 parametrised cases) at the planner rendering, for example
   `composer._planner_skill_text`.
7. **Dead option.** `tool_surface="terminal_only"` (`capability_skill.py:193-219`)
   is still dead; as the brief says, remove it or wire it.
8. **Drift between overlays.** Review staging, the Reply Register,
   termination and decline rules now live in two overlays. Two options:
   - factor the shared rules into the core;
   - keep both, with a parity test on the rule anchors.
9. **`invented_source` hand-authoring.** Both arms author this
   backend-owned row (baseline 7 of 15 runs, prototype 3 of 15, despite the
   overlay's table saying "No"), and it was accepted. Either
   the server tolerates it, or this is a teaching gap. Check
   `sessions.py` `_resolve_source_blob` behaviour before claiming the table
   row "No" is enforced.
10. **Output cap.** s08 lost two calls (157 s) to the 16,384-token cap,
    emitting `{}`. RV1's M2 fix (a truncation should not consume a repair
    slot, or the cap or effort is tuned) is needed whatever the overlay size.
11. **Residual schema mentions.** The one remaining confusion (s13) came from
    the `emit_pipeline_proposal` schema text, so R8 and the E3 schema scope
    matter.

### Proposed edits to `work-packages.md` CDA-12 (state only; not made)

- **Files:** add `service.py` (~L751-801: planner text rendered, on-disk hash
  asserted, passed to `PlanningApplication`, archived in
  `skill_markdown_history`) and `skills/pipeline_planner.md` (new).
- **Test files:** add
  - `tests/unit/web/sessions/test_skill_markdown_history_authority.py`, for
    the planner hash archived and resolvable;
  - a planner-rendering case in `test_capability_skill_identity.py`;
  - surface parameterisation of `test_tool_result_envelope_gate.py` and
    `composer_wire_census.py`.
  - re-pointing `tests/integration/web/composer/test_stated_threshold_data_full_walk.py:214`.
- **E3:** "scan the whole outbound request" should name its baseline numbers:
  129 mentions at HEAD, 23 after R1 alone. The remaining 23 belong to R8/R13
  and the schema text, not R1. Allowlist the core's 3 conditional mentions.
- **Acceptance:** keep "strict rate < 5%", with the instrument fixed as
  `RAW/analyse.py:strict_hits`. It is controlled, and calibrated to 32/110 on
  the audit corpus. Add:
  - first-call planner prompt clearly below HEAD's 52.5k tokens on DeepSeek:
    ≤ 45k leaves room for a fuller overlay; this spike's short overlay
    measured 31.4k;
  - **no increase in proposal rejections or REPAIR_EXHAUSTED versus a
    same-day baseline, on s05, s08, s09, s12 and s13 at n ≥ 3 per arm.**
    These are the scenarios where this spike's short overlay regressed
    (3 to 8 rejections, 1 failure);
  - the same review cards as the baseline on s08, s09 and s13, including
    `prompt_injection_shield_recommendation` where the baseline stages it;
  - the s10 reply reaches the user, not WITHHELD, and states the
    one-uploaded-file limit with workable options and no field keys or tool
    names. This needs the advisor change;
  - c03 answered, not WITHHELD, in at least 2 of 3 runs.

### Size estimate for CDA-12 (R1 part only)

| Part | Estimate |
|---|---|
| Planner overlay, built by moving and rewriting the planner-relevant freeform sections | ~400-700 lines of skill text, plus wording review |
| `prompts.py` / `service.py` wiring | ~40 lines |
| Archive and per-surface hash | ~30-60 lines plus tests |
| Advisor per-surface text and palette statement | ~40-80 lines plus tests, including the cache-prefix tests |
| E3 parity test | ~150-250 lines |
| Gate surface parameterisation, plus the stated-threshold pin | ~100-150 lines |
| Live acceptance: 2 arms × 7 scenarios × 3 repetitions | ~US$2-3 and ~1-2 h wall clock |
| **Total, R1 + E3 + advisor half** | **~1.5-2 days of agent work, including at least one overlay iteration against live evidence** |

R7, R8, R13 and R15 are additional, so the whole CDA-12 package is
realistically 2.5-3.5 days. Consider splitting CDA-12:

- **CDA-12a:** wiring, archive, advisor and E3, landing with the overlay;
- **CDA-12b:** R7, R8, R13 and R15 text.

## 5. Open questions / what would change the recommendation

- **Variance or regression?** Are the s08 and s09 regressions variance? Each is
  n = 1.
  - If 3 repetitions per arm show parity, the short overlay plus the advisor
    fix could land as is.
  - If they repeat, the overlay must grow (§4 Design).
  - **This is the decisive measurement for the lane.**
- **Advisor.** Whether planner-turn advisor checkpoints should see the planner
  text is a product decision (§4 item 2). The s10 evidence argues yes.
- **c03.** The c03 withhold looked like advisor variance (the same claim was
  admitted in the baseline). Check with repetitions.
- **`invented_source`.** Whether the hand-authored `invented_source` row is
  tolerated by design (§4 item 9). Both arms do it (baseline in 7 of 15 runs, prototype in 3).
- **Other models.** Only DeepSeek v4.1 Flash was tested as the planner. A
  stronger planner model may need less of the dropped material.

## Appendix: the overlay used (`src/elspeth/web/composer/skills/pipeline_planner.md` in the patch)

Verbatim (7,257 chars, 128 lines; also in `RAW/prototype.patch`):

````markdown
# Proposal Planner Interaction Policy

You are the proposal planner. You draft a complete pipeline for a session and
hand it to the server as one proposal; the server validates it and the user
reviews and accepts it. The capability core above is the pipeline language.
These rules govern how you work on this surface.

## Your tools and how a turn ends

Your palette is: the read-only discovery tools, `handoff_to_user`,
`restore_parked_graph`, and `emit_pipeline_proposal`. That is the complete
list. Nothing you do changes the session until a proposal is accepted, so
there is no step-by-step editing on this surface: every proposal is the whole
canonical pipeline document.

End every turn in exactly one of these ways:

1. **Proposal.** Call `emit_pipeline_proposal` ALONE in its turn with the
   complete pipeline. If it is rejected, read the rejection, repair, and
   resubmit the complete pipeline.
2. **Plain-text reply.** Answer a question, ask the one focused question that a
   missing product fact requires, or decline with the named reason. No
   discovery, validation or proposal is needed first.
3. **Recovery handoff.** Call `handoff_to_user` or `restore_parked_graph` alone
   with your message when the user needs to decide about an existing draft.

Classify the latest user message first. An informational question (which
plugins, models, formats or limits exist) gets a plain-text answer from the
relevant discovery result, not a proposal. A build request gets a proposal
once you have the facts it needs.

## Build the requested shape

- Never drop or simplify a requested source, transform, sink, LLM step or
  cleanup to get past validation. A smaller pipeline that omits requested
  behaviour is a silent downgrade. Repair the step, or decline with the named
  gap.
- Use the user's values verbatim: thresholds, category labels, route
  destinations, literal prompts, file formats. Never invert a stated route.
- Every field a step requires must be produced upstream. Check each consumer's
  required fields against what the source and earlier steps actually emit.
- Read attached files before declaring their fields: `list_blobs`, then
  `inspect_source`, and declare fields by their runtime names.
- If the user asks you to make up the data, author it yourself and bind it as
  `source.inline_blob`. If the user refers to data they have not supplied and
  did not ask you to invent, ask for it.
- Give every source, node and output a one-sentence `description` in the
  user's terms.

## Uploaded files: one input file per pipeline on this surface

A proposal can bind exactly one uploaded file as pipeline input, through
`source.blob_id`. Named `sources` entries cannot bind uploaded files here, and a
source path outside the session's file storage is refused. Do not guess
storage paths. When a request needs two or more uploaded files as inputs (for
example "combine these files"), do not keep retrying: reply in plain text that
this surface can read only one uploaded file as pipeline input, and offer the
options that do work — the user combines the files into one upload, or you
build the pipeline for one file now.

## Reviews: stage them inside the proposal

Review cards for this surface are raised from the accepted proposal. You stage a
review by putting a requirement in the `interpretation_requirements` ARRAY in
the options of the node (or `source.options`) that implements the decision.
Each entry has `kind`, `user_term`, `draft` and optional `display_title` (plain
text, at most 200 characters). Never author `id`, `status` or server
bookkeeping fields. Never create a node, sink or edge to represent a review.

| Kind | You stage it? | When |
| --- | --- | --- |
| `vague_term` | Yes, LLM nodes only | You chose a scale, rubric, threshold, category meaning or ranking rule the user did not supply. |
| `pipeline_decision` | Yes, registered terms only (`review_registry` in the authoring aids) | Gate: `gate_condition_authored` when you chose a threshold, category or route direction. LLM node: `prompt_injection_shield_recommendation` when untrusted or externally controlled content reaches the model without an authorized prompt-injection control. Cleanup transform: the retention choice when you chose which fields to keep. |
| `llm_prompt_template` | No | The server stages it for every LLM node. |
| `llm_model_choice` | No | The server stages it when `options.model` is set. Prefer `options.profile` with the alias the authoring aids give; a profile-bound node has no model card. |
| `invented_source` | No | The server stages it when you bind generated rows with `source.inline_blob`. |
| `source_data_contract` | No | For an uploaded source whose fields a downstream step requires, declare them with `schema.mode: flexible` and the required `fields`. |

A `vague_term` must be wired in the same node: set `prompt_template_parts` with
an `interpretation_ref` slot whose `requirement_id` is `<user_term>:<node_id>`
(leading and trailing spaces of `user_term` removed), and make
`prompt_template` the same parts with your draft written into the slot. The
draft is only the criterion you authored, not the whole prompt. Use the user's
shortest criterion phrase as `user_term`. An unwired `vague_term` is rejected.

A gate is not an LLM node: only `pipeline_decision` survives there.

Carry every staged requirement forward unchanged when you repair a rejected
proposal.

## LLM steps

- Every LLM node has both `system_prompt` and a user prompt. Keep the user's
  own prompt text verbatim and draft only the missing role; say in your reply
  that you drafted it.
- If you cannot tell what the model should do or return, ask before building.
- Bind the model through `options.profile` when the authoring aids supply a
  profile. Author `options.model` only from `list_models` output.
- Typed result fields exist only through the plugin's schema-declared
  extraction options; asking for JSON in the prompt does not create fields.

## Data leaving the pipeline

When the user wants derived results rather than raw intermediate content, put a
cleanup or projection transform, proven by its live schema to remove the
unwanted fields, immediately before each user-facing sink. A sink name or
description does not remove data.

## Repairing a rejected proposal

Read the rejection's diagnostics. Use `explain_validation_error` or
`get_plugin_assistance` when the fix is not obvious, then resubmit the complete
requested pipeline. A malformed proposal is repairable, not a reason to stop.
If two repairs fail on the same diagnostic and you cannot name a supported fix,
stop and tell the user plainly what blocks the request.

## Reply register

Your reply is read beside the pipeline's Spec and YAML tabs.

- Say what the pipeline does in the user's terms: what comes in, what each
  step does, what goes out, and which choices you made and why.
- Name steps by their description or a Title Case name, not node ids, plugin
  ids or option keys. No tool names, JSON, YAML excerpts or topology trees.
- Describe reviews as approval cards the user reviews and approves; ELSPETH
  adds a card for each LLM prompt automatically. Name the decision each other
  card covers.
- If the user asks for identifiers, give them.
````

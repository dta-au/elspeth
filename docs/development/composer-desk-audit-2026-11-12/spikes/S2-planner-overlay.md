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

### Live A/B (PARTIAL, credit exhausted)

Two instances, both on the broad allowlist with DeepSeek v4.1 Flash as the
composer:

- baseline `S2-base` on :8501, from the main checkout;
- prototype `S2-proto` on :8502, from the worktree.
  - Its environment carried the worktree `PYTHONPATH`.
  - Every captured prototype request had a 40,027-char system message, and
    every baseline request 135,863.

Each arm ran first turns only, through `evals/composer-live-audit/drive_session.py`
and `RAW/run_arm.sh` (`RAW/scenarios/` = first turn, absolute fixture paths).

At 08:45 UTC the shared OpenRouter account ran out of credit: every call
returned 402 "Insufficient credits", and the orchestrator confirmed
`total_usage 1241.16 > total_credits 1240`.

- **Usable pairs:** 4 complete pairs (s01, s02, s03, s20 at n = 1), plus a
  prototype-only s05.
- **Not usable:** every other turn failed `provider_unavailable` (402) and is
  excluded.

### Analysis instrument (`RAW/analyse.py` → `RAW/analysis_r1.json`)

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

**Spend:** US$0.134, summed from the per-call `usage.cost` of my captures
(baseline $0.0815, prototype $0.0523). The account-level `usage_daily` delta is
not attributable because other spikes shared the account.

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

### 3.2 Live A/B, 4 complete pairs, n = 1 each (EXECUTED, `RAW/analysis_r1.json`, `RAW/sessions/{base,proto}/`)

| Scenario | Arm | Label | s | Calls | Prompt tok | 1st-call prompt tok | Completion tok | US$ | Emits | Rejections | Strict-confusion calls | Cards after accept | Valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| s01 | base | PROPOSAL | 15.4 | 2 | 110,822 | 52,567 | 4,653 | .0080 | 1 | 0 | 1 | invented_source | yes |
| s01 | proto | PROPOSAL | 10.1 | 2 | 70,185 | 31,407 | 3,230 | .0066 | 1 | 0 | 0 | invented_source | yes |
| s02 | base | PROPOSAL | 31.4 | 4 | 241,575 | 52,547 | 9,642 | .0195 | 2 | 1 (`locked_input_extras`) | 0 | llm_prompt_template | yes |
| s02 | proto | PROPOSAL | 16.3 | 3 | 113,498 | 31,387 | 4,347 | .0089 | 1 | 0 | 0 | llm_prompt_template | yes |
| s03 | base | PROPOSAL | 21.5 | 2 | 108,444 | 52,560 | 6,784 | .0099 | 1 | 0 | 2 | invented_source | yes |
| s03 | proto | PROPOSAL | 11.1 | 2 | 66,100 | 31,400 | 3,672 | .0058 | 1 | 0 | 0 | invented_source | yes |
| s20 | base | PROPOSAL | 10.1 | 3 | 163,351 | 52,552 | 2,303 | .0342 | 2 | 1 (`canonical_schema` oneOf) | 0 | invented_source | yes |
| s20 | proto | PROPOSAL | 8.2 | 2 | 65,399 | 31,392 | 1,845 | .0126 | 1 | 0 | 0 | invented_source | yes |
| **Sum** | base | 4/4 PROPOSAL | **78.4** | **11** | **624,192** | | **23,382** | **.0716** | 6 | **2** | **3/11 calls** | same set | 4/4 |
| **Sum** | proto | 4/4 PROPOSAL | **45.7** | **9** | **315,182** | | **13,094** | **.0339** | 4 | **0** | **0/9 calls** | same set | 4/4 |

Prototype-only extras:

- **s05 (fork/coalesce).** PROPOSAL, 29.3 s, 2 calls, 0 rejections, valid.
  The topology is faithful: trim, then a fork gate, two passthrough paths, a
  `require_all`/`nested` coalesce, an always-true final gate, and a JSON sink.
  The baseline s05 died on the 402 after one 12,002-completion-token discovery
  call, so it is not comparable.
- **s08.** The first call (39,430 prompt tokens, 0 confusion) succeeded; the
  rest got 402.

The baseline's strict-confusion sentences (verbatim):

- s01: "For the proposal planner, we don't have request_interpretation_review tool."
- s03: "I don't call request_interpretation_review (not in tool inventory for this
  planner surface — …)"
- s03: "…that's for the request_interpretation_review call."

Review staging:

- Both arms produced the same review cards after acceptance in all 4 pairs.
- Both arms sometimes hand-authored the backend-owned `invented_source` row
  in `source.options`:
  - baseline s03;
  - prototype s01, despite the overlay's table saying "No".
  - Both proposals were accepted.

Reply quality: no difference could be judged.

- All 9 proposal turns returned the same server-templated reply ("I prepared
  the requested pipeline and staged it for your review…").
- The turns where the planner's own prose reaches the user (c03 capability
  question, s10 multi-file decline) never ran.

### 3.3 NOT RUN (credit exhausted)

The following were planned and not run:

- s05, s08, s09, s12, s13, c03 baseline;
- s08, s09, s12, s13, c03 prototype;
- every rep-2 run;
- s10 on both arms.

To run them later (ports 8501/8502, about US$1-2 at the observed ~$0.01-0.02
per planner turn, more for s10's hatch on GLM):

```bash
# baseline (main checkout, no PYTHONPATH)
cd <repo root> && env -u PYTHONPATH evals/composer-live-audit/instance.sh 8501 S2-base
# prototype (worktree)
cd <repo root>/.claude/worktrees/S2-planner-overlay && \
  PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src evals/composer-live-audit/instance.sh 8502 S2-proto
SP=<repo root>/.claude/lanes/composer-desk-audit-20261112/spikes/S2-planner-overlay
B=<repo root>/evals/composer-live-audit/runs/instances/S2-base
P=<repo root>/.claude/worktrees/S2-planner-overlay/evals/composer-live-audit/runs/instances/S2-proto
L="s05-fork s08-grants-llm s09-angry s12-json-explode s13-multiquery c03-core-capabilities s10-two-files"
$SP/run_arm.sh 8501 $B/capture $B/credentials.json base 2 $L &   # use rep 2: rep-1 dirs hold the failed attempts
$SP/run_arm.sh 8502 $P/capture $P/credentials.json proto 2 $L &
wait; python3 -I $SP/analyse.py $SP/analysis_r2.json base=$SP/sessions/base proto=$SP/sessions/proto
# then the full rep 2 for s01 s02 s03 s20 as rep 3; kill both instance PIDs after.
```

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

**R1 is worth doing.**

### Measured effect

Measured on 4 pairs, it is consistent and in every case in the expected
direction:

| Measure | Baseline | Prototype | Change |
|---|---|---|---|
| Prompt tokens per turn | 624k | 315k | −50% |
| First-call prompt | 52.5k | 31.4k | −40% |
| Latency | 78.4 s | 45.7 s | −42% |
| Completion tokens | | | −44% |
| Cost | | | −53% |
| Provider calls | 11 | 9 | |
| Emit rejections | 2 | 0 | |
| Strict palette-confusion calls | 3/11 | 0/9 | |

- There was no regression in the observed sample: same labels, same review
  cards, all valid.
- **n is small (1 per pair), so these are directional, not significant.** The
  improvement in tokens and system size is deterministic. The latency,
  rejection and confusion gains still need the NOT RUN replicate.
- The dead-end scenario (s10) and conversational replies (c03) are untested.
  These are exactly where R1 should matter most, and where a short overlay
  could most plausibly regress.

### Design (the prototype is the right shape)

- Use the existing `render_with_pipeline_capabilities` seam, with
  `core + planner overlay + deployment overlay`.
- Leave the loop's text byte-identical, so there is no strip logic.
- Render once per service instance, as today.

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
2. **Advisor.**
   - `AdvisorCheckpointOwner` still gets the freeform text
     (`service.py:764`, `advisor_checkpoint.py:454`).
   - On planner turns the advisor would judge planner output against a policy
     the planner no longer received. The prototype leaves this unchanged.
   - Decide explicitly: pass the planner text on planner-turn checkpoints, or
     state in `advisor_checkpoint.py` that the advisor reviews against the
     loop policy. Either way it is a second per-surface hash input.
   - The advisor cache-prefix tests (`test_advisor_cache_prefix.py`) pin
     prefix reuse.
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
9. **`invented_source` hand-authoring.** Both arms sometimes author this
   backend-owned row (baseline s03, prototype s01) and it was accepted. Either
   the server tolerates it, or this is a teaching gap. Check
   `sessions.py` `_resolve_source_blob` behaviour before claiming the table
   row "No" is enforced.

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
- **E3:** "scan the whole outbound request" should name its baseline numbers:
  129 mentions at HEAD, 23 after R1 alone. The remaining 23 belong to R8/R13
  and the schema text, not R1. Allowlist the core's 3 conditional mentions.
- **Acceptance:** keep "strict rate < 5%", with the instrument fixed as
  `RAW/analyse.py:strict_hits`. It is controlled, and calibrated to 32/110 on
  the audit corpus. Add:
  - first-call planner prompt ≤ 35k tokens on DeepSeek (measured 31.4k);
  - no increase in emit rejections or invalid proposals versus a same-day
    baseline;
  - s10 and c03 replies reviewed by a human for honesty (multi-file limit
    stated, no tool names).

### Size estimate for CDA-12 (R1 part only)

| Part | Estimate |
|---|---|
| Overlay text | ~150 lines |
| `prompts.py` / `service.py` wiring | ~40 lines |
| Archive and per-surface hash | ~30-60 lines plus tests |
| Advisor decision | ~20 lines plus tests |
| E3 parity test | ~150-250 lines |
| Gate surface parameterisation | ~100 lines across 3 gates |
| **Total, R1 + E3** | **~0.5-1 day of agent work plus one live A/B (~US$2)** |

R7, R8, R13 and R15 are additional, so the whole CDA-12 package is
realistically 1.5-2 days. Overlay wording should iterate on live evidence,
especially s10 and c03.

## 5. Open questions / what would change the recommendation

- **The NOT RUN cells.**
  - If s10 or c03 on the prototype produce a worse reply (tool names, a false
    "can't", no limitation stated), or the replicate shows more rejections or
    missed `vague_term` / `prompt_injection_shield_recommendation` staging,
    the overlay must grow toward the freeform policy's review sections before
    landing.
  - None of the 4 pairs exercised `vague_term`, `gate_condition_authored` or
    prompt-shield staging: s08, s09 and s13 do, and they did not run.
- **Advisor.** Whether planner-turn advisor checkpoints should see the planner
  text is a product decision (§4 item 2).
- **`invented_source`.** Whether the hand-authored `invented_source` row is
  tolerated by design (§4 item 9).

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

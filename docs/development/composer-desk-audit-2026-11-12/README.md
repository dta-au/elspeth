# Composer desk audit: tool calls, skills and advisor (2026-11-12)

Audit of the Web Composer's model-facing surface ahead of a live executive
demonstration. It covers the composer loop, the empty-session planner, the
advisor, the tool registry and the skill/plugin guidance those models are
taught. Read alongside:

- [remediation-plan.md](remediation-plan.md): fixes, performance work and
  enhancements, sequenced into work packages, plus the demo-day runbook.
- [tool-call-register.md](tool-call-register.md): every tool on every surface,
  generated from the live registries and joined with live call outcomes.
- [evidence/](evidence/): turn table, per-surface teaching cross-check and the
  two verification reports.

Code baseline: `main` at `1a7ff0e6` (0.8.11 candidate merge).

## Bottom line for the demo

The tool loop works. Across 68 live turns, edits on an existing pipeline
(add a column, drop a field, change a threshold, insert a filter, undo,
explain, fork, aggregate) produced valid graphs in 12–40 s with honest replies.
Identifier-level teaching is clean: of 248 backticked identifiers in the
skill, none names a tool, argument or code that does not exist.

The failures cluster in places the presenter is likely to go:

| # | Demo phrasing | What the audience sees | Live |
|---|---|---|---|
| F1 | Attach two files: "combine these" | 240 s spinner, then "The composer request timed out" | 2/2 failed |
| F2 | "Can you connect to SharePoint?" / "Can you read PDFs?" / "Is this safe on sensitive data?" | The correct answer is replaced by "Verified checkpoint evidence covers 0 saved draft revisions… Completion advisory review did not clear" | 4 of 23 informational first turns withheld: SharePoint 2/2, PDF 1/2, "is this safe" 1/2 |
| F3 | "What changed since the version I accepted?" | "I couldn't produce a change list: this session has no comparison baseline loaded" | 4/4 `diff_pipeline` calls failed |
| F4 | "Screen the notes for passwords or confidential material" | HTTP 422 "This control content appears to contain a credential" | 2/2 failed |
| F5 | "Use GPT-4o" / "Which models can I use?" | A refusal that also says "GPT-4o is served there", read from an unfiltered 3,938-model catalog | 1/1 |
| F6 | Any arithmetic, type conversion or filter on a default deployment | An honest refusal: the plugins exist but are not enabled | 2/2 on default allowlist |
| F18 | **Tutorial:** clicking "Apply" on the "Consider adding error routing…" suggestion | Every card is acknowledged and Checks are green; the chat says "your pipeline's ready", then "Continue to Run" refuses: "The saved tutorial pipeline does not match the supported tutorial plugin set." | 1/1 in a browser walk |

F1, F2 and F4 are the ones that look broken on stage. F6 is configuration
only. F3 and F5 read as competent refusals, but on stage they still sound
like "I can't".

## Method

1. **Model's-eye view.** I rendered exactly what each surface is sent, using
   the production functions plus captures of real provider requests:
   - the tool-loop system prompt (`render_system_prompt`, 135,863 chars);
   - the 45 loop tools (`composer_loop_tool_definitions`);
   - the planner's 23 tools (`planner_tool_definitions`) and its context messages;
   - the 3 escape-hatch tools;
   - the advisor request.
2. **Static cross-check.** I compared, per surface, which tools and
   identifiers the prompt teaches against which tools it is offered. The
   word-boundary matcher was controlled against a known positive
   (`` `set_pipeline` ``) and a known negative (`build_set_pipeline_candidate`).
   The project's own gates were run too: `generate_skill_inventory.py --check`
   (exit 0) and `composer_wire_census.py --scorecard` (exit 0, no differences
   across 43 tools).
3. **Live battery.** Six local `elspeth web` instances ran from this commit,
   using OpenRouter `deepseek/deepseek-v4.1-flash` as composer and
   `z-ai/glm-5.3` as advisor.
   - **Budgets:** composition 10, discovery 24, timeout 240 s.
   - **Capture:** a litellm callback recorded every provider request and
     response, including DeepSeek's full `reasoning_content`.
   - **Workload:** 27 multi-turn sessions and 68 user turns, covering the
     battery corpus and demo-style phrasings, including one instance on the
     **default** plugin allowlist.
   - **Model reasoning as evidence:** the model's reasoning showed where the
     instructions confused it. Every finding below was then traced to code.
4. **Verification agents.** Two read-only agents checked advisor evidence
   parity and LLM-node wiring. Their reports are under `evidence/`.

Deviations from production, which do not change any finding:

- Prompt guard ran in `debug-when-unconfigured` mode (no Azure Prompt Shields).
- SQLite stores, local auth, and one OpenRouter LLM profile (`standard`).
- The audit user was granted the deployment-wide `user` role directly in
  the scratch session store.

The harness and scenarios are in `evals/composer-live-audit/`, so the battery
can be re-run against the demo deployment.

## Findings

Severity: **DEMO-BLOCKER** means likely to fail visibly on a natural
phrasing; **HIGH** means it costs turns or gives a wrong or confusing outcome
on common paths; **MEDIUM** is recoverable or an edge path; **LOW** is
polish. Verification is **EXECUTED** (reproduced live or in-process), **READ**
(traced through code) or **SUSPECTED**.

### F1. Multi-file requests are unbuildable on the planner; the skill and plugin hints point at a tool the planner does not have. DEMO-BLOCKER

- **Where.**
  - Every request in an empty session goes to the planner, which is offered
    23 tools and not `set_source_from_blobs`
    (`pipeline_planner.planner_tool_definitions`).
  - Named `sources` entries in the canonical document forbid `blob_id`:
    "Values share source's shape minus blob_id/inline_blob"
    (`src/elspeth/web/composer/tools/sessions.py:2188`).
  - The `blob_rows` plugin hint tells the planner: "Bind blobs with
    set_source_from_blobs — never author or edit the blobs list directly"
    (`src/elspeth/plugins/sources/blob_rows.py:176`).
- **What happens (EXECUTED, s10).** "Combine these two quarters into one file…"
  with `q1.csv` and `q2.csv` attached.
  - The planner made 9 calls over 241.6 s, ending in `504
    composer_operation_deadline_expired`.
  - Its reasoning says so directly: "Composer hints say 'Bind blobs with
    set_source_from_blobs…' That tool isn't available in this palette."
  - The follow-up "Now only keep invoices over 1000" hit the same planner,
    because the session was still empty. It failed after 167.7 s with HTTP
    500 `planner_repair_exhausted`: "The composer could not produce a valid
    pipeline within its repair budget. Retry the request, or revise it if
    this recurs."
  - The escape hatch did engage. After three rejected proposals (repair
    budget 2), the planner escalated to the advisor model with the full
    23-tool palette.
    - In T1 that call took 69 s and spent the remaining wall clock.
    - In T2 it took 71 s and ended in `REPAIR_EXHAUSTED`.
    - Neither turn handed off to the user with an explanation before the
      deadline.
- **What should happen.** Either the planner can express multi-blob sources,
  or it says plainly that this needs two steps.
- **Fix.** See R1 and R4 in the remediation plan.

### F2. The advisor withholds correct capability answers because it never sees the catalog they came from. DEMO-BLOCKER

- **Where.**
  - The advisor system prompt is the full composer skill as "binding local
    policy" (`advisor_checkpoint.py:453-454`). It therefore applies the
    skill's rule that capability claims must come from "the supplied discovery
    digest's `prohibited` entries…".
  - The digest itself is deliberately excluded from the advisor transcript,
    on the planner path (`pipeline_planner.py:3494-3514`) and the tool loop
    (`service.py:2453-2462`).
  - No section of the advisor's user message carries a catalog
    (`advisor_context.py:223-331`).
- **What happens (EXECUTED).**
  - **Withheld:** "Can you connect to our SharePoint?" (s04 T2) and "Can you
    read PDFs?" (p01 T2). Both correct answers were withheld:
    `FLAGGED/WITHHOLD`, "the discovery digest wasn't supplied".
  - **Admitted:** "Can you send results to Teams?" (p01 T3) had the same
    evidence class and no tool call, and was admitted. The outcome is
    nondeterministic.
  - **Across all runs:** of 23 informational planner turns, 4 were withheld:
    SharePoint 2/2, PDF 1/2, and "Is this safe to run on sensitive data?"
    1/2, the last in the harness re-run.
  - **No repair on the planner path.** There is exactly one review with no
    repair turn (`planning_application.py:938-1010`). The inner `attempts = 2`
    (`advisor_checkpoint.py:1348`) retries only provider or parse failures.
  - **What the user reads.** The withheld body is replaced by
    `render_saved_scope()` (`turn_effects.py:37-39`): "Verified checkpoint
    evidence covers 0 saved draft revisions. There is no net authored change
    from the starting draft." A grey "System note" box follows, using the
    graph-gate wording "Completion advisory review did not clear… Pipeline
    readiness was not re-verified this turn…" (`no_tool_policy.py:207-212`).
    Purpose-written withheld-reply copy already exists but is unused in chat
    (`advisor_policy.py:451-455`).
- **Fix.** R2: give the advisor the compact capability record that
  `advisor_abandonment.py:109-129` already builds (available and prohibited
  ids, profile aliases; about 1.1 KB). Then R3: replace the withheld-reply copy.

### F3. `diff_pipeline` cannot succeed anywhere in the web tool loop. HIGH

- **Where.** The handler returns "No baseline available. Load or create a
  session first." whenever `context.baseline is None`
  (`tools/generation.py:4659-4668`). No `execute_tool` call in
  `tool_batch.py` passes `baseline=`; only the MCP server and the
  planner/commit paths do. The tool description promises "the baseline
  supplied by this surface, which may be the request-start state".
- **What happens (EXECUTED).**
  - Live calls: 4/4 failed (s01 T6, s02 T2, r01 T2, r03 T2).
  - The model's answer to "What changed since the version I accepted?" varied
    between runs: "I couldn't produce a change list…" (s01, r01), "Nothing has
    changed" (r02), and re-validating, then describing (r03).
  - The error text tells the model to "load or create a session", which is
    impossible and misleading.
- **Fix.** R5: pass the turn-start state (and, for "since I accepted", the
  last committed or accepted state) as `baseline`. Until then, take
  `diff_pipeline` off the loop palette or reword the error.

### F4. The credential guard fails the whole turn on the model's private reasoning about a security-screening request. HIGH (DEMO-BLOCKER for a data-protection demo)

- **Where.** The planner response guard scans reasoning content as well as
  tool arguments (`pipeline_planner.py` ~L1831-1889, 3671,
  `surface="composer_planner_response"`).
- **What happens (EXECUTED, s06 T1 and T2).**
  - The corpus prompt `error_routing` asks to screen deal notes "for
    passwords, secrets or confidential material".
  - DeepSeek's reasoning drafted sample notes containing `"password": "Client
    emailed the admin pass…"`. Bisecting the reasoning to the smallest
    tripping window gives exactly that string.
  - Both turns returned `422 credential_material_rejected`, and the user, who
    typed no credential, read "This control content appears to contain a
    credential. Store the value through the secret service…".
  - The third turn ("just drop failures") succeeded.
- **Tradeoff.** This is a security control. Do not weaken it blindly; see R6
  for the options, which need your decision.

### F5. On the web composer, the skill teaches model selection by slug, which the deployment never offers; `list_models` returns an unfiltered catalog. HIGH

- **Where.**
  - The skill says "an `llm` node needs either `options.profile` or a
    catalog/discovery-served `options.model`" and teaches the
    `llm_model_choice` card (`pipeline_composer.md` ~L892-903, L1236).
  - On the web, every llm schema is projected through the profile registry
    (`catalog/policy_view.py:191-201`), and `model` is a profile-private field
    (`LLM_PROFILE_PRIVATE_FIELDS`). The served schema offers only `profile`
    (enum `["standard"]`; EXECUTED).
  - `list_models` returns litellm's whole catalog: 3,938 models under about 90
    "providers", including image-size and quality tiers such as
    `1024-x-1024`, `low` and `standard` (EXECUTED, p01 T1).
- **What happens.** "Use GPT-4o for the classification step" (s02 T4) got a
  correct refusal. The same refusal says "GPT-4o (`openai/gpt-4o`) is served
  there — so the model itself is available to the deployment", which is
  inferred from the litellm dump. In s08 the same model reasoned the opposite,
  that it could set `options.model` behind an approval card.
- **Fix.** R7: make the model-choice paragraph conditional on the surface
  actually exposing `model`, and filter `list_models` to the deployment's
  profiles (or remove it from the web palette).

### F6. With the default plugin allowlist the composer cannot do arithmetic, type conversion or filtering, and plugin prose names the missing plugins. HIGH (configuration)

- **What happens (EXECUTED).**
  - On a default deployment (the 14-plugin required core: csv/json/text plus
    `llm` sources, csv/json/text/`document` sinks, and the transforms
    `field_mapper`, `llm`, `web_scrape`, `line_explode`, `reference_join`,
    `report_assemble`), the corpus `transform_pipeline`
    request ("turn quantity and unit price into numbers, work out a line
    total") was declined honestly (smoke and c01).
  - The follow-up "only keep orders over 100 dollars" was declined too.
  - `field_mapper`'s `not_for` prose tells the model to "use value_transform
    for calculated values and type_coerce…", which are plugins it then gets
    `plugin_not_enabled` for. The model's reasoning noticed: "This is
    contradictory unless the digest is partial."
- **Fix.** For the demo, enable the needed plugins (runbook in the
  remediation plan). Product fix R8: only cross-reference plugins that are
  available.

### F7. The planner is sent the full tool-loop skill: 23 tools it cannot call, and its own terminal tool is never named. HIGH (root cause behind F1 and part of F2)

- **Where.**
  - The planner's system message is byte-identical to the tool-loop skill
    (sha256 prefix `2e4c56762a74` on both).
  - It mentions 23 tools the planner is not offered, 109 times in all:
    `set_pipeline` ×34, `request_interpretation_review` ×19,
    `create_blob` ×8, `upsert_edge` ×5 and so on (full list in the register).
  - `emit_pipeline_proposal` appears nowhere in it. The bridge is a single
    sentence in the last user message: "call emit_pipeline_proposal alone in
    its turn with one complete canonical set_pipeline argument object".
- **What happens (EXECUTED).** 66 of 110 planner calls (60%) spend reasoning
  working out which taught tools they lack, for example "On proposal-only
  surface, there is no request_interpretation_review tool advertised", or
  "'surface by YOU' is impossible on this palette". It mostly converges, but
  this is exactly the "the skill says X but I don't have X" failure, and F1
  is where it stops converging.
- **Fix.** R1: render a surface-specific skill. Gate it with the same
  per-surface cross-check this audit used.

### F8. Backend receipt text on every tool-loop reply. MEDIUM (user-facing)

- **Where.** `composition_completion.py:2632`, `service.py:2679,3186`.
- **What happens.** Every tool-loop reply, including "Explain what this
  pipeline does in two sentences", ends with a System note: "Checkpoint save
  status: Verified checkpoint evidence covers 0 saved draft revisions. There
  is no net authored change from the starting draft. Checkpoint evidence does
  not establish task completion." On a withheld tool-loop turn the sentence
  appears twice (s09 T2).
- **Fix.** R3.

### F9. "Correction to my previous message" when the user saw no previous message. MEDIUM (user-facing)

- **What happens (EXECUTED, s02 T3, s20 T2).** The advisor rightly caught an
  overclaim in each draft: "applied in both the system role and the user
  template" was wrong, as was "review card waiting" for a card already
  accepted. The repair instruction says "The end user has not read these
  findings: write your final reply to stand on its own", yet the published
  reply opens "Correction to my previous message…" or "Correction: I
  misstated…".
- **Cause.** The composer sees its unpublished draft in history as if it had
  been delivered.
- **Fix.** R9: tell it explicitly that the draft was not shown, and have it
  restate rather than correct.

### F10. The planner's "emit now" notice fires on an incomplete notion of done. MEDIUM

- **Where.** `_ALL_INFORMATION_GAPS_CLOSED_NOTICE = "All declared information
  gaps are closed; emit the terminal proposal now."` is appended once the
  server's declared keys, typically plugin schemas, are supplied
  (`pipeline_planner.py:217, 5138-5139`).
- **What happens (EXECUTED).** It arrives before the model has inspected an
  attached file (s02, s09) and on purely informational questions (s04 T5).
  The reasoning shows the conflict: "I need to inspect the source to declare
  fields. Wait — the prompt says … emit the terminal proposal now", and
  "Emitting an empty proposal would be wrong".
- **Fix.** R10: reword it as information, not a command.

### F11. The recommended "many rows → one briefing" shape always shows a HIGH warning nobody can clear. HIGH (demo optics)

- **Where.** `ReportAssemble` never declares `output_semantics()`, although
  it knows its output is a string (`plugins/transforms/report_assemble.py:247`).
  The `document` sink's own guidance recommends exactly this pairing
  (`document_sink.py:131, 453-455`).
- **What happens (EXECUTED, s08 live and in-process).** The pipeline shows a
  **high**-severity warning: "Semantic contract: 'document' sink 'briefing'
  requires field 'briefing' (document.field.verbatim_text) but upstream
  producer 'assemble_briefing' (report_assemble) declares no semantic
  facts… must declare output_semantics()".
  - No source, transform or sink option clears it.
  - It does not block execution or auto-commit; the advisor returned CLEAN.
  - Separately, the document sink's example uses `schema.mode: fixed`. After
    `report_assemble`, which emits 8 extra metadata fields, that is a hard
    `sink_locked_extras` error.
- **Fix.** R11: add `output_semantics()` to `report_assemble` (STR,
  unconstrained framing) and correct the sink example.
- Details:
  [evidence/llm-wiring-verification.md](evidence/llm-wiring-verification.md).

### F14. Approved interpretation never reaches multi-query LLM prompts. HIGH (integrity)

- **Where.** When a user accepts or amends a `vague_term` review, the
  resolver writes the value only into the node-level `prompt_template`
  (`web/sessions/pending_interpretation.py:818-822`). Any query with its own
  template never uses the node-level one (`plugins/transforms/llm/transform.py:644-649`).
- **What happens (EXECUTED in-process).**
  - `set_pipeline` accepted a multi-query node whose every query has its own
    template, with the review wired at node level.
  - After resolution, no query's rendered prompt contained the approved
    value (`accepted_in_prompt=False`).
  - The staging gate `vague_term_wiring_count` (`interpretation_state.py:2947`)
    never checks whether any query uses the node-level template.
  - The user approves a definition that the run then ignores. In the live
    battery DeepSeek reasoned its way around it in s08 ("the resolved value
    wouldn't reach the model"), so it did not fire there. Nothing teaches the
    limitation.
- **Fix.** R13: reject this wiring at staging when no query renders the
  node-level template, and add one teaching line.

### F16. On a valid, unchanged pipeline the completion gate re-checks only if the model calls `preview_pipeline`. HIGH (reported from a live session, 2026-11-13)

- **Where.** `composer_preflight.py:344-380`
  (`reuse_or_recompute_runtime_preflight`) recomputes the turn-end preflight
  only when:
  - the turn changed the pipeline;
  - the state holds a blob reference; or
  - the state is **invalid**.

  For a valid, unchanged pipeline with no `preview_pipeline` call this turn,
  it returns `None`. The advisor gate then takes the "unverified" branch
  (`advisor_checkpoint.py:1044-1063`, `advisor_signoff_unverified_validation`).
  The user reads "Completion advisory review did not clear… Pipeline
  readiness was not re-verified this turn; validation and the advisory review
  run again after your next pipeline change."
- **What happens.**
  - **Live battery.** Both tool-loop turns that ended with an uncleared
    advisory gate (2 of 33: s03 T4, s09 T2) were turns without
    `preview_pipeline`. Across tool-loop turns that changed the pipeline,
    the model skipped preview in 4 of 15.
  - **Reported session.** After a turn with `upsert_node` + `set_output` +
    `request_interpretation_review`, the gate did not clear on a valid
    pipeline with all reviews resolved. Asked why, the composer explained
    that `preview_pipeline` is "what satisfies the completion advisory gate",
    showed the raw tool-call JSON, and told the user the behaviour was a bug
    worth filing.
- **What it does and does not block (EXECUTED in the browser, this commit).**
  - The **Run** button stays enabled. Persisted gate facts withhold only
    `completion_ready` (`execution/completion_gates.py:380-445`). The
    decision panel says "Share inspect link is blocked. Run pipeline is still
    available." and the action bar says "Advisory checks don't block Run."
    Screenshot: [evidence/advisor-gate-ui-s03.png](evidence/advisor-gate-ui-s03.png).
  - What the uncleared gate does block: **Share inspect link**, and a
    "completed" turn.
  - The panel repeats its re-run sentence twice. It also tells the user the
    gate reruns only "after your next pipeline change", although on a valid
    pipeline only a pipeline change or a model-initiated preview can clear it.
  - If Run was actually disabled in the reported session, the cause is
    something else: pending review cards, a composer turn in flight, or a
    build other than this commit. That needs the Run button's reason text
    from that session to confirm.
- **Fix.** R14: compute the turn-end preflight server-side for every
  non-empty state, valid or not, so the gate never depends on the model
  choosing to call `preview_pipeline`. This is the same deterministic check
  `POST /validate` already runs, so it is validation, not authoring, and
  within the composer invariants. Also fix the panel copy (R3).

### F17. The composer explains platform internals and tells the user to file bugs. MEDIUM (user-facing)

- **Where.** The Reply Register (`pipeline_composer.md:230-254`) bars tool
  keys and enum values in unprompted summaries, but allows identifiers "if
  the user asks". Nothing covers questions about the composer's own gates.
- **What happens (reported session).** Asked why the pipeline wasn't ready,
  the model showed `{"tool": "preview_pipeline", "arguments": {}}`, described
  the "completion advisory gate", and recommended filing a bug against it.
  For an executive audience that reads as the product diagnosing itself.
- **Fix.** R15: add a Reply Register rule. When asked about a check,
  readiness or approval state, state the user's next action in product
  terms. Never ask the user to run a tool or file a defect. Pair it with R14,
  which removes the situation that prompts the question.

### F18. Tutorial: one click on a suggested fix makes the tutorial pipeline unrunnable, with no actionable message. DEMO-BLOCKER

- **Where.**
  - The tutorial's "Continue to Run" calls `GET /api/tutorial/{id}/readiness`,
    not the workspace `execution_ready`. That runs the run-admission check
    `_tutorial_launch_blocker` (`composer/tutorial_service.py:154-215`), which
    requires **exactly one** output and that it be `sink:json`
    (`output_valid = len(output_ids) == 1 …`, L174).
  - The decision panel offers a one-click **Apply** on every validation
    suggestion. Apply sends the suggestion to the composer as a user request
    (`frontend/src/components/chat/ChatPanel.tsx:853-857`).
  - One of those suggestions is "Consider adding error routing to a retention
    output — failed rows are currently discarded" (`composer/state.py:10079`).
- **What happens (EXECUTED in a browser on the first-run tutorial).**
  - After sending the tutorial brief, accepting the proposal and clicking
    Apply on that suggestion, the composer did exactly what was asked. It
    added a `failed_rows` json output and routed the source and all three
    steps' failures to it. The advisor returned CLEAN.
  - The learner then cleared four review cards: three Acknowledge plus one
    prompt review, where Approve unlocks only after View prompt.
  - After that, Checks showed ✓ and the chat said "Every card acknowledged —
    your pipeline's ready. Select **Run pipeline** to start it."
  - The tutorial step has no "Run pipeline" button. "Continue to Run" returned
    409 `tutorial_plugin_set`: "The saved tutorial pipeline does not match the
    supported tutorial plugin set." Screenshot:
    [evidence/tutorial-continue-refused.png](evidence/tutorial-continue-refused.png).
  - Neither the learner nor the composer is told that the second output is
    the problem. The composer cannot see the tutorial admission rule, and
    must not be taught tutorial-only rules (Composer invariant 2). So asking
    it why produces guesses, such as the preview/advisory explanation
    reported on 2026-11-13 (F16, F17).
- **Without Apply** the composer chose "discard" with a single output in
  3 of 3 sends of the same brief, and the tutorial would continue. This is
  user-triggered, not random, and the Apply button sits in the decision
  panel the learner is told to work through.
- **Fix.** R16. Choose one:
  - make the tutorial admission accept an optional failure-retention json
    output (it is a correct, safer graph);
  - do not offer suggestion Apply inside the tutorial shell (a UI-only
    change; the composer path is untouched);
  - at minimum, name the mismatch in the 409 detail, for example "This
    tutorial runs one JSON output; your draft also writes failed_rows. Ask
    the Composer to remove that output, or exit the tutorial to keep it."

  In all three cases, the chat must not say "Select Run pipeline" where no
  such button exists.

### F15. Default-allowlist documentation is stale. LOW

`docs/reference/environment-variables.md:587-592` lists 9 always-authorized
plugins. The compiler's required core (`plugin_policy/compiler.py:17-40`)
has 14; the doc is missing `source:llm`, `sink:document`, `line_explode`,
`reference_join` and `report_assemble`. In-process, a default policy's
digest names 8 unavailable plugins in `not_for`, hint or aids text: F6's
cross-references, plus `blob_csv_expand`, `blob_fetch`, `json_explode`,
`blob_json_expand`, `azure_prompt_shield` and `azure_content_safety`.

### F12. Tool-result legibility. LOW–MEDIUM (budget)

- **Buried argument errors (EXECUTED).** Discovery failures in an empty
  session lead with irrelevant whole-pipeline errors ("No source configured",
  "No sinks configured"). The real cause sits under `data.argument_error`,
  for example `get_blob_content` with `expected_content_hash: 6862` (s11).
- **Inconsistent sibling arguments.** `get_plugin_schema(plugin_type, name)`
  but `get_plugin_assistance(plugin_type, plugin_name)`. The model called the
  latter with `name` (s19).
- **Unnamed extra property.** `canonical_schema` rejections name the path
  and rule but not the extra property. The model nested
  `nodes`/`edges`/`outputs` inside `source` and needed a turn to find it
  (6 of 16 proposal rejections).
- **Other unnamed fields (minor).** `patch_output_options` rejects a
  `description` (step descriptions belong on `set_output`). Other
  `plugin_options_invalid` messages are legible.

### F13. Generic first-turn reply. LOW (enhancement)

Every successful planner turn ends with a fixed sentence, "I prepared the
requested pipeline and staged it for your review. It has interpretation
review cards to resolve before it can run." (`protocol.py:209-236`), and
nothing about what was built. The constants are deliberate (they encode the
preflight verdict). For generated data the review-card wording always fires,
because invented sources always need a card.

## Performance (measured, 68 turns)

| Measure | First turn (planner) | Follow-up (tool loop) |
|---|---|---|
| Turn latency p50 / p90 / max | 27 s / 57 s / 242 s (timeout) | 25 s / 103 s / 110 s |
| Provider calls per turn p50 / max | 3 / 9 | 4 / 9 |
| Prompt tokens per call p50 | 56k | 70k (advisor 55k) |
| Prompt-cache hit (sum cached / sum prompt) | 88% | 93% (advisor 45%) |
| Proposal rejections | 16 of 53 `emit_pipeline_proposal` calls | — |

The cost drivers:

- the 136k-char skill sent to all three roles;
- an advisor call on every turn, including "Thanks, looks good";
- proposal rejections, each costing a full planner call.

Startup also warns `composer_turn_budget_underfunded … fundable_turns_estimate=16`
against 34 configured turns at 240 s. The performance items are in the
remediation plan (P1–P6).

## What worked well

- **Identifier-level teaching is pinned.**
  - Of 248 backticked identifiers in the skill, the 7 found nowhere in shipped
    code are all illustrative example values (`teeth_count`, `branch_one`, …).
  - The tool loop is offered exactly the 45 tools it is taught.
  - No live call was ever made to an unoffered or unknown tool.
  - The argument-wire scorecard is clean.
- **Tool-loop edits are reliable** (s01, s03, s05, s07, s12, s15, s16, s17,
  s20), and replies disclose real tradeoffs. Examples: "JSON content under a
  .csv name", "a workspace holds a single pipeline", and the move-vs-copy
  ambiguity.
- **The advisor catches real overclaims** (F9 cases, s09's missed review
  card). The problem is the evidence it is given and the copy the user
  reads, not the idea.
- **Conversational handling is good:** "Build me a pipeline", prompt-injection
  refusal, "Is this safe on sensitive data?" via `get_audit_info`, and
  "Thanks".

## Not covered

- Tutorial run stage (executes a real pipeline; outside the composer loop).
- Pipeline execution, results and run diagnostics after "Run".
- Frontend rendering beyond the advisor-notice path.
- YAML import and sharing.
- Prompt guard in `required` mode.
- PostgreSQL stores.
- Anthropic and Bedrock model families, which differ in prompt caching and
  thinking replay.

Results are for one composer/advisor pair and a single run per scenario,
except the three repeats of the headline flow. Treat rates as indicative.

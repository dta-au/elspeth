# Advisor evidence parity — why a correct digest-grounded answer gets WITHHELD

Read-only investigation, HEAD `1a7ff0e6`, 2026-11-12. Nothing under src/, tests/ or docs/ was edited.
Labels: **READ** means I read the code. **EXECUTED** means I measured it with a script over the captured
`llm_calls.json` / `terminal.json` records under `.claude/lanes/composer-desk-audit-20261112/live/sessions/`.
**SUSPECTED** means I reasoned it out and did not reproduce it.

## TL;DR

The END advisor is asked to check that every claim in the candidate reply is supported by evidence.
It never gets the deployment catalog that the primary model answered from. On the planner path that
catalog is `authoring_aids.discovery_digest` plus `information_manifest`. On the tool loop it is the
catalog context and state context messages. Meanwhile the advisor's system prompt is the full composer
skill, and that skill says capability claims must be grounded in "the supplied discovery digest". So
the advisor looks for a digest, doesn't find one, and WITHHOLDs a correct answer.

The failure has reproduced twice so far: SharePoint (s04 turn02) and PDFs (p01 turn02). A third
question of the same class passed by chance: Teams (p01 turn03).

On the planner path a WITHHOLD ends the turn after one review, and the primary model gets no repair.
The user then sees a backend saved-scope sentence, styled as if the model said it, followed by a
graph-gate notice that talks about "pipeline readiness" and "your next pipeline change".

The one-request precedent for the fix already exists. The `cannot_complete` abandonment review gives
the advisor a "current deployment capabilities" record (`advisor_abandonment.py:109-129`). The END
publication review does not.

---

## Q1. What the advisor receives for a no-tool / informational reply on the planner path

**Call chain (READ)**
- The planner's prose reply raises `PlannerDeclined` (`pipeline_planner.py:4388-4407`).
- That is caught at `planning_application.py:1201-1233`, which calls `_review_planner_decline` (`planning_application.py:893`).
- `_review_planner_decline` does two things:
  - builds the review with `turn_evidence.review_candidate(..., scope="planner")` (`:915`);
  - runs one END pass with `_advisor_checkpoint._run_advisor_checkpoint(phase="end", pass_index=1, publication_review=review, ...)` (`:938-951`).
- `_build_checkpoint_arguments` (`advisor_checkpoint.py:733-857`) puts these items in the END arguments:
  - a fixed `problem_summary`, which is graph-centric (`:812-841`);
  - empty `recent_errors` and `attempted_actions` on pass 1 (`:786-787`);
  - `schema_excerpt` = `summarize_pipeline_for_advisor(state)`, covering topology and options only. It carries no validation and no catalog (`advisor_context.py:1067`; `:759`).
  - `publication_review` (`:846-849`).
- The advisor user message has exactly these sections (`advisor_context.py:223-331`):
  - trigger and problem;
  - prior findings;
  - attempted actions;
  - current request (`:257-275`);
  - visible transcript (`:276-287`);
  - schema excerpt (`:288-313`);
  - publication projection (`:314-330`).
- There is no catalog, digest, manifest or policy section.

**What the publication projection carries (READ)**
`TurnEvidence.review_candidate` (`turn_effects.py:686-888`) builds the payload at `:761-783`. It contains:
- `candidate_reply`, `current_request` and `current_graph_hash`;
- `saved_effects`: verification, revision count, state ids, hashes, `net_changed` and `changed_fields`;
- `inspection`: blob page coverage and source metadata;
- abandonment limitations / stop review, only if present (`:784-807`);
- `retained_operation_history` (`:808-809`).

There is no plugin snapshot, no `authoring_aids`, no `information_manifest` and no `schema_contract_evidence`.

**What the visible transcript carries (READ)**
- The planner's first three messages are excluded on purpose (`pipeline_planner.py:3494-3514`):
  - [0] is the skill;
  - [1] is `{"authoring_aids":..., "schema_contract_evidence":...}`;
  - [2] is the request, including `information_manifest`, `conversation_context` and `retained_operation_history`.
- `transcript_owner.begin_messages(messages)` puts all three on the exclusion list (`advisor_transcript.py:659-661`, `:676-679`).
- The snapshot advertises this exclusion as `"scope": "same_session_visible_events_only; native_thinking_and_static_internal_prompts_excluded"` (`advisor_transcript.py:781`).

**What the advisor system message carries (READ + EXECUTED)**
- The system message is `self._composer_skill_text + advisor_system_instructions_for_trigger(...)` (`advisor_checkpoint.py:453-454`).
- In the s04 turn02 capture, the advisor system message (139,987 chars) is a byte-identical prefix extension of the planner system message (135,863 chars). They diverge only at the appended "Advisor checkpoint mode" block.
- So the advisor is told to treat the skill as binding policy (`advisor_context.py:73`). The skill says:
  - "Consult the authoring_aids discovery digest delivered in the request context … plan directly from it" (`skills/pipeline_capabilities.md:22-25`);
  - "Say that a plugin is unavailable or policy-denied only when live discovery proves it — use the supplied discovery digest's `prohibited` entries first … never a bare assertion" (`skills/pipeline_capabilities.md:63-68`).
- The END instructions then say "ADMIT only when the exact candidate's claims are supported … WITHHOLD … any candidate whose required evidence is unavailable or omitted" (`advisor_context.py:96-98`).
- Together these make the false WITHHOLD the advisor's correct reading of its instructions.

**Comparison against the captured s04 turn02 request (EXECUTED)**
Script: counted marker strings in planner request `seq=3` messages vs advisor request `seq=4` messages[1].

| marker | planner request | advisor user message |
|---|---|---|
| `discovery_digest` | present | 0 |
| `authoring_aids` | present | 0 (the 1 hit is in the system/skill text) |
| `information_manifest` / `plugin_selection` / `policy_snapshot` | present | 0 |
| plugin names (`blob_rows`, `"sources"`) | present | 0 |

- The advisor transcript holds 6 events:
  - the turn01 user message;
  - two `backend_public_call_metadata` audit rows with `visible_response_coverage_gap`;
  - the turn01 assistant reply;
  - the turn02 user message;
  - the candidate (`tool_calls: []`).
- `availability_gaps: []`. `schema_excerpt` reads `Source: (none set) / Nodes: (none) / Sinks: (none)`. `inspection.currently_available: []`.
- The advisor's reasoning says it outright: *"no visible discovery tool output — the discovery digest wasn't supplied, no list_sources result visible."*
- Its findings: *"no discovery evidence supports them … consult the current authoring-aids discovery digest or call the offered source-discovery tool."* The verdict was `FLAGGED` / `WITHHOLD`.
- The planner's reasoning in seq=3 shows it answered correctly from the digest: *"available sources include blob_rows, csv, json, null, text, llm. No SharePoint source … Prohibited lists are empty."*

**Second reproduction (EXECUTED)**
- p01-capability-qs turn02, "Can you read PDFs?": the planner made no tool call and answered from the digest. The advisor (seq 31) returned FLAGGED/WITHHOLD.
- Findings: *"no discovery tool result supporting these claims is visible … (no list_sources/list_transforms outcome, no supplied digest …)"*.
- The user saw the same withheld text as in s04.

**Control: the same class passed by chance (EXECUTED)**
- p01 turn03, "send results to our Teams channel?": the reply made the same kind of digest-grounded "not in the sink catalog" claim, with no tool call.
- The advisor (seq 33) returned CLEAN/ADMIT. Its reasoning: *"sink catalog not shown … can't flag withheld evidence."*
- So the outcome is a coin flip on identical evidence. It is not a policy decision.

**Control: a tool-grounded answer passes (EXECUTED)**
- p01 turn01, "Which models can I use?": the planner called `list_models`, so the tool result is in the transcript.
- The advisor (seq 29) returned ADMIT. Its reasoning: *"answered from list_models tool result."*

## Q2. Is the same asymmetry present on the tool-loop path?

**Code (READ): yes.**
- `build_messages` assembles these messages (`prompts.py:565-626`):
  1. the system prompt (skill);
  2. the catalog context (`CATALOG_CONTEXT_PREFIX` "Deployment plugin catalog and authoring aids (AUTHORITATIVE REFERENCE DATA…)"), which holds `available_plugins`, `plugin_policy` (available/selected ids, `usable_profile_aliases`, `control_modes`) and `authoring_aids` (`prompts.py:216-269`);
  3. chat history;
  4. the state context from `build_context_string` (`prompts.py:365-501`), which holds `current_state`, its `validation` (errors, warnings, suggestions), `composer_progress` and `schema_contract_evidence`;
  5. the user message.
- `service.py:2453-2462` builds these and immediately calls `turn_evidence.transcript.begin_messages(llm_messages)`. Every one of them is excluded from the advisor transcript.
- The only tool-loop messages the advisor does see are:
  - the per-turn recovery / retained-operation system messages, appended after `begin_messages` (`service.py:2464-2468`);
  - assistant turns and tool results from this turn and from persisted history.

**Captured turns (EXECUTED)**
- Scanned 34 advisor calls across all sessions.
- In all 17 tool-loop END/abandonment advisor requests, the strings below appear in the primary's last request and never in the advisor user message:
  - `Deployment plugin catalog`
  - `available_plugins`
  - `plugin_policy`
  - `usable_profile_aliases`
  - `Pipeline state and session progress`
  - `composer_progress`
  - `schema_contract_evidence`
  - `discovery_digest`
  - `authoring_aids`
- The 17 requests:
  - s01 t02–t08 (seq 7, 12, 17, 21, 24, 26, 30)
  - s02 t02 (12), t03 (16, 18), t04 (25, 27)
  - s03 t02–t04 (8, 12, 14)
  - s05 t02 (4)
  - s07 t02 (35)
  - s08 t02 (20)
  - s09 t02 (23)
  - s12 t02 (46)
  - s15 t02 (30)
- What the advisor does see is tool results by name, for example `get_plugin_schema`, `list_models`, `preview_pipeline` and `set_pipeline`.
- So on the tool loop it can verify a claim only if a tool call this turn, or a persisted earlier turn, produced the fact.
- Tool-loop builds rarely trip this, because claims there are usually backed by mutation or schema tool results.
- The only tool-loop WITHHOLDs observed were legitimate:
  - s02 t03: the reply claimed a system-prompt edit that `saved_effects` contradicted;
  - s09 t02: a missing review card.
- (The one exception to the scan is s10 t01 seq 13. That is glm-5.3 serving as the planner escape-hatch "senior", not the advisor, and its messages do contain the digest.)

## Q3. Attempt budget and whether the primary model can repair

**Planner path (READ)**
- There is exactly one END review and no primary repair: `pass_index=1` (`planning_application.py:946`).
- The verdict goes straight to `admitted` / blocked (`:961-1010`), and `repair_turns_used=0` (`:995`).
- There is no loop back into the planner.

**Inner "attempts" (READ)**
- `attempts = 2  # bounded retry` (`advisor_checkpoint.py:1348`).
- The loop re-asks only after a provider failure or a malformed / schema-invalid reply (`:1422-1483`). A parsed verdict returns at once (`:1467-1470`).
- So a rendered FLAGGED/WITHHOLD uses exactly one advisor call.
- EXECUTED: s04 t02 has exactly 2 calls, planner seq 3 then advisor seq 4.
- EXECUTED: s04 t04 shows the retry in action. Its first advisor reply was CLEAN with a non-null `note`, which violates the "For CLEAN … note must be null" schema rule. The format re-prompt then produced a second CLEAN (seq 8 → 9).

**Tool loop (READ)**
- The budget is `composer_advisor_checkpoint_max_passes`, `default=2` (`web/config.py:478-480`). The loop is at `composition_completion.py:2305-2339`.
- A WITHHOLD on a non-last pass where `allow_repair_continue` is true injects a "[Completion advisory review — BLOCKING …]" user message carrying the fenced findings (`:2542-2578`). The primary model then rewrites, and pass 2 runs.
- EXECUTED, s02 t03: seq 16 WITHHOLD → primary seq 17 "Correction …" → seq 18 ADMIT.
- `allow_repair_continue` is false on several terminal arms:
  - `composition_completion.py:1223`;
  - `service.py:2050`;
  - `service.py:2237`;
  - `composition_completion.py:1743` sets it to `proof_runtime_preflight is None and not confirmed_stop`.
- When it is false, the first WITHHOLD is terminal.
- s09 t02 (one advisor call, terminal WITHHOLD) went down one of those arms. Which one: SUSPECTED, not traced.

**The notice wording**
- The wording "did not clear after the available attempts" is fixed copy (`no_tool_policy.py:207-211`).
- On the planner path "the available attempts" means one review and zero repairs.

## Q4. Where the withheld text comes from

**Body sentence (READ)**
- `CHECKPOINT_UNCHANGED_SCOPE` = "Verified checkpoint evidence covers {count} saved draft revisions. There is no net authored change from the starting draft." (`turn_effects.py:37-39`). It is rendered by `TurnEvidence.render_saved_scope()` (`turn_effects.py:952-964`).
- When publication is not admitted, `_advisor_blocked_result` replaces the model's prose with it: `raw_content = (assistant_message.content …) if publication_admitted else saved_scope` (`advisor_checkpoint.py:958`).
- The docstring states the intent: "otherwise only trusted saved-scope facts may appear" (`advisor_checkpoint.py:884-886`).
- The planner path passes it twice: `assistant_message=_AdmittedAssistantMessage(content=turn_evidence.render_saved_scope())` and `saved_scope=turn_evidence.render_saved_scope()` (`planning_application.py:993, 1002`), with `publication_binding=None`, so it can never be admitted.
- The tool loop does the same at `composition_completion.py:2384-2391` and `:2524`.
- So the sentence is not prepended to the answer. It is the replacement for the withheld answer.

**Notice (READ)**
- In `planning_application.py:975-988` the reason resolves to `"publication_withheld"`, because `verdict.ok` is True for a rendered FLAGGED.
- `runtime_preflight=None` (`:999`) selects the absent-preflight arm (`advisor_checkpoint.py:1043-1053`).
- That arm calls `compose_advisor_signoff_unverified_message(raw_content)` (`no_tool_policy.py:950-963`), which appends `_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX` = `"\n\n---\n\n[ELSPETH-SYSTEM] Completion advisory review did not clear after the available attempts. Composer completion is withheld. Pipeline readiness was not re-verified this turn; validation and the advisory review run again after your next pipeline change."` (`no_tool_policy.py:35-36, 82-93, 207-212`).
- A purpose-built publication-withheld wording already exists: "The completion review did not admit the reply's claims; the reply was withheld." (`advisor_policy.py:451-455`). It feeds only the validation `detail`. The chat notice does not use it.

**Rendering (READ + EXECUTED)**
- `visible_message_segments` splits `content` at `raw_content` (`no_tool_policy.py:769-798`). The saved-scope sentence becomes an `AssistantTextSegment` and the suffix becomes a `TrustedSystemNoticeSegment` (the marker is stripped).
- EXECUTED: the s04 t02 and p01 t02 `terminal.json` segments are `[{"kind":"text","content":"Verified checkpoint evidence covers 0 …"}, {"kind":"trusted_system_notice","content":"Completion advisory review did not clear …"}]`.
- The frontend renders them as follows (`MessageBubble.tsx:133, 243-261`):
  - `text` goes through `MarkdownRenderer` inside the assistant bubble, with the sr-only label "ELSPETH said:";
  - `trusted_system_notice` goes into `<div className="trusted-system-notice" role="status"><span className="sr-only">System note:</span>…</div>`;
  - the notice box is muted, bordered and small-font (`chat.css:128-136`);
  - copy-to-clipboard prefixes notices with "System note: " (`MessageBubble.tsx:86-92`).
- The literal `[ELSPETH-SYSTEM]` is never shown.
- What the user sees: a normal assistant-prose line "Verified checkpoint evidence covers 0 saved draft revisions. There is no net authored change from the starting draft.", followed by a grey boxed note "Completion advisory review did not clear after the available attempts. Composer completion is withheld. Pipeline readiness was not re-verified this turn; validation and the advisory review run again after your next pipeline change."
- That backend sentence is attributed to ELSPETH as if the model said it, in a session that has no pipeline at all.

**Checkpoint-receipt footer (READ + EXECUTED)**
- Every settled tool-loop return goes through `self._completion._append_checkpoint_receipt(public_result, ...)` (`service.py:2679` terminate phase; `service.py:3186` classify phase).
- The only skip condition is `result.pipeline_commit_intent is not None or result.graph_recovery_intent is not None` (`composition_completion.py:2632-2633`).
- Otherwise `compose_checkpoint_receipt_message` always appends "Checkpoint save status: <saved scope> Checkpoint evidence does not establish task completion." (`no_tool_policy.py:746-766`).
- It is not appended on the planner path, which has no `_append_checkpoint_receipt` call. That matches the EXECUTED scan: none of the s04 / p01 / smoke-edit planner turns have it.
- EXECUTED: all 26 tool-loop terminal results in the captures carry it, including pure explanations with zero tool calls. One example is s01 t07: the primary made 0 tool calls, and the reply is followed by "Checkpoint save status: Verified checkpoint evidence covers 0 saved draft revisions…".
- EXECUTED: on a tool-loop withheld turn the saved-scope sentence appears twice, once as `text` and once inside the receipt. s09 t02 segments show "Verified checkpoint evidence covers 6 saved draft revisions…" as text, then the pending-handoff notice, then "Checkpoint save status: Verified checkpoint evidence covers 6 saved draft revisions…".

**Knock-on effect on later context (EXECUTED)**
- In s04 t04 the planner's `conversation_context.prior_assistant_messages[history_index=3]` is just "Verified checkpoint evidence covers 0 saved draft revisions…".
- The composer's own SharePoint answer and the fact it was withheld are both gone from its context.
- The t04 reply then said "I couldn't confirm a SharePoint connector from what I have". That is the opposite of what it had correctly established.

## Q5. Which other question types are at risk

**The rule.** A reply is at risk when two things are both true:
1. it asserts a deployment-specific fact;
2. the only grounding for that fact is a primary-only context section.

The primary-only sections are: digest, manifest, `schema_contract_evidence`, `authoring_aids.*`, the tool-loop catalog context, and the state-context `validation`. A reply escapes when the fact comes from the skill text (shared with the advisor) or from a tool result (visible in the transcript).

| Question type | Grounding the primary typically uses | Advisor can see it? | Risk |
|---|---|---|---|
| "Can you connect to X / read PDFs / post to Teams?" (absence claim) | digest `plugins` + `prohibited` | no | **CONFIRMED** (s04 t02, p01 t02 WITHHOLD; p01 t03 ADMIT on identical evidence, so the result is nondeterministic) |
| "Why can't I use plugin X?" (policy reason verbatim) | digest `prohibited[].reason` / tool-loop `plugin_policy` | no, unless `list_*` was called | SUSPECTED high |
| "Which models can I use?" | `usable_profile_aliases` in digest / catalog; `list_models` for slugs | only if `list_models` was called | passed in p01 t01 because it called the tool; a profile-alias-only answer is SUSPECTED at risk |
| "Is it safe for sensitive data?" | `authoring_aids.prompt_shield / user_disclosure / model_custody` or `get_audit_info` | only via the tool | passed in s04 t05 (called `get_audit_info`, ADMIT); a no-tool answer is SUSPECTED at risk |
| "What can you do?" | skill (shared) + digest | mostly, via the skill | passed in s04 t01; plugin-specific enumerations (`web_scrape`, `blob_fetch`) are SUSPECTED low-to-medium |
| "What does this expression / template syntax allow?" | `authoring_aids.expression_grammar` | no | SUSPECTED |
| Tool loop: "Is my pipeline valid / why is it failing?" | state-context `validation` (`prompts.py:421-427`); `summarize_pipeline_for_advisor` has no validation | only if a tool result this turn or a persisted one carries it | SUSPECTED |
| Tool loop: "Which transforms / sinks are available?" | catalog `available_plugins` | no | SUSPECTED high (same shape as the confirmed case) |
| Prompt-injection refusal, greetings, thanks | none | n/a | low (s04 t04 and t06 ADMIT) |

## Q6. Smallest fix (not implemented)

**Recommended: give the END advisor the same policy-snapshot facts the primary answered from, as server-owned evidence bound into the review.**

1. Add `plugin_snapshot: PluginAvailabilitySnapshot` to `TurnEvidence.review_candidate` (`turn_effects.py:686`).
   - Add a `"current_deployment_capabilities"` record to `payload` / `provider_payload` (`:761-823`).
   - Reuse the abandonment projection unchanged: `available_ids`, `prohibited_ids` (WEB_SURFACE_PROHIBITED), `usable_profile_aliases`, `catalog_scope` (`advisor_abandonment.py:109-119`). Lift it into a shared helper.
   - Bind `plugin_snapshot.snapshot_hash` into `binding` and `semantic_repair_key`, as the abandonment review does at `advisor_abandonment.py:167`.
2. Thread the snapshot to the four call sites:
   - `planning_application.py:915` and `:960`. `_plan_and_stage_empty_pipeline` already holds `plugin_snapshot` (`:1030`).
   - `composition_completion.py:2259` and `:2369`. The gate already receives `plugin_snapshot`.
3. Add one sentence to `_ADVISOR_END_PUBLICATION_INSTRUCTIONS` (`advisor_context.py:93-117`): "`current_deployment_capabilities` is the policy snapshot the composer's discovery digest / catalog was rendered from. A plugin presence/absence or prohibition claim consistent with it is evidenced; one that contradicts it is not."

**Size**
- EXECUTED: for this deployment the projection is about 1.1 KB (40 ids, 0 prohibited).
- The full digest is 19 KB, and all `authoring_aids` are 54.9 KB.
- Ship ids + prohibited + aliases, not the prose digest. The advisor already has the skill.
- If expression-grammar / safety answers also need coverage, add only the specific `authoring_aids` keys whose hash the digest budget already reports. That is a follow-up.

**Invariants**
- This is server-side evidence for validation and rejection. It authors no pipeline structure and does not bypass the provider: the LLM still answers, and the advisor still judges.
- So it does not touch Composer invariant 1. It is keyed on nothing tutorial-specific, so invariant 2 holds as long as it applies to every session and both paths (planner and tool loop).
- It changes the publication binding, which is the intended behaviour, so existing tests that pin binding bytes will churn.

**Alternatives considered**

| Option | Assessment |
|---|---|
| Teach the advisor only that "the digest is authoritative" without supplying it | Rejected. The advisor would have to ADMIT unverifiable capability claims, which is exactly the check the WITHHOLD path exists for. Hallucinated "we have a SharePoint connector" replies would pass. |
| Exempt informational / no-tool replies from the END gate | Removes a real safeguard: the gate also catches false status claims, as in s02 t03. This needs a developer decision; surface the tradeoff, do not just do it. |
| Give the planner path one repair pass, like the tool loop | Invariant-safe, but costs extra provider calls. With missing evidence the "repair" would just be the planner calling `list_sources` to satisfy the advisor. It treats the symptom. Worth doing independently so a genuine WITHHOLD can self-correct instead of dead-ending. |

**Separate UX defects to file (all READ + EXECUTED above)**
- (a) A planner `publication_withheld` reuses the graph-gate notice ("Pipeline readiness … after your next pipeline change") even when the session has no pipeline. Use the existing `advisor_policy.py:451-455` publication wording instead.
- (b) The saved-scope replacement body renders as `kind:"text"` ("ELSPETH said:"). It is backend copy and should be a trusted segment.
- (c) On a tool-loop withheld turn the saved-scope sentence is duplicated by the receipt (s09 t02).
- (d) The receipt is appended to every tool-loop reply, including pure Q&A with 0 revisions. `composition_completion.py:2632` is the single condition point.
- (e) After a withheld reply, later turns' planner context holds only the saved-scope sentence (s04 t04 history_index 3). The composer loses its own answer and then contradicts it.

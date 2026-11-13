# RV3-tool-loop-tools: adversarial review of F3, F5, F10, F12, F13 (+ R5, R7, R10, R12, E1)

Reviewer: RV3-tool-loop-tools. Tree: branch `ccr-d14093ba-glu6bw` @ `45b65e50` (the code under review
is the same as `main` @ `1a7ff0e6` for the cited files). Read-only. No servers, no provider calls.

Instruments (all in the scratchpad, never in the tree):
`<session scratchpad>`.
They join every assistant `tool_calls[].id` to its `role: tool` result across the 12 raw capture files
(`live/capture/llm_calls.jsonl`, `live/instances/{core1,lv1..lv4}/capture/llm_calls.jsonl`,
`evals/composer-live-audit/runs/instances/{smoke,tb1,tb2,tb3,tut1,ui1}/capture/llm_calls.jsonl`), deduplicated
by call id (400 unique calls, 340 with a result). Controls: known positive `diff_pipeline` (s01 T6, call
`call_01a1d180a2417ce19ecdf3ae`) is found; known negative `no_such_tool_xyz` returns 0. The notice scanner was
controlled against the live string in s02 T1 (positive) and c01 T1 (negative, no discovery round).

## Verdict table

| Claim | Verdict | One-line reason |
|---|---|---|
| F3 `diff_pipeline` cannot succeed in the web tool loop | CONFIRMED-WITH-CORRECTION | No loop dispatch path passes `baseline` (proved over every caller), 4/4 live failures reproduced; but r02's "Nothing has changed" was the CORRECT answer (r01–r03 had no change since acceptance), so the "answers varied" framing and R5's acceptance test are wrong; the envelope also mislabels a valid pipeline as invalid (missed). |
| F5 skill teaches slug model selection the web never offers; `list_models` unfiltered | UNDERSTATED (with small corrections) | Refutation attempt failed: no web configuration exposes `options.model`. Impact is worse than "1/1 competent refusal": p01 T1 and s08 T2 told the user they could pin a model slug, and the advisor ADMITted both. |
| F10 planner "emit now" notice fires on an incomplete notion of done | CONFIRMED-WITH-CORRECTION | Quotes reproduced; but it is not appended "once": it re-appends after every fully covered discovery round (up to 5 copies in one request, s10 T1), fires on 29 of 41 battery planner turns, and pushed s10 into the false inference behind F1. |
| F12 tool-result legibility | CONFIRMED-WITH-CORRECTION | All three mechanisms reproduced, but "6 of 16" is wrong (7 of 16 are `canonical_schema`; 2 of 16 are the nested `nodes/edges/outputs` case), the unnamed key is a deliberate value-free design, and `patch_output_options` DOES name `description`. |
| "16 of 53 proposals rejected" (F12/P4/R12/register) | CONFIRMED-WITH-CORRECTION | 53 emits / 16 fed-back rejections reproduced exactly over the `live/` captures, but 13 of those emits (3 rejections) are the 10 tutorial-canary smoke runs, not the 68-turn battery the table is labelled with. Battery-only: 13 of 40. |
| F13 generic first-turn reply | CONFIRMED-WITH-CORRECTION | The constant is the entire 129-char reply on 20 of 22 battery proposal turns; but "every successful planner turn" is wrong (text-reply planner turns are model-written), and "always fires for generated data" is contradicted by s16 (model-generated rows, auto-commit constant, no card). |

Counts: CONFIRMED 0, CONFIRMED-WITH-CORRECTION 5, UNDERSTATED 1, OVERSTATED 0, REFUTED 0, UNVERIFIABLE 0.

---

## F3 — CONFIRMED-WITH-CORRECTION

### Attempt to refute "no web tool-loop dispatch path supplies a baseline" (READ, every caller)

`diff_pipeline` is a sync discovery handler (`tools/generation.py:4642-4679`, failure at `:4661-4669`).
It is reachable only through `execute_tool` (`tools/_dispatch.py:795`, which forwards `baseline=` into
`ToolContext` at `:964`) or `execute_discovery_tool_with_context` (`tools/_dispatch.py:999`). Every caller:

| Caller | Passes `baseline`? | Can it dispatch `diff_pipeline`? |
|---|---|---|
| `tool_batch.py:3008` (generic loop dispatch, `_dispatch_one` closure) | no (kwargs `:3009-3036`) | **yes — this is the loop path** |
| `tool_batch.py:2059` (proposal preview) | no | no: guarded by `tool_name in _MUTATION_TOOLS and tool_name != "set_pipeline"` (`:2057`) |
| `tool_batch.py:1565, 1753, 1860` (`ToolContext(...)` for batch-dependency refusal and `set_pipeline` candidate finalisation) | no | no: not a dispatch of `diff_pipeline` |
| `sessions/routes/composer/proposals.py:492` (proposal accept replay) | no | no: replays a staged mutation proposal |
| `session_tool.py:126` / `:396` (session-aware async tools) | n/a | no: only `request_interpretation_review` has a kwarg branch; anything else raises (`:452-460`) |
| `pipeline_commit.py:504/516, 569/583` | `baseline=current_state` | commit path, not the loop |
| `pipeline_planner.py:3429/3441` → `execute_discovery_tool_with_context` (`:4907`) | `baseline=current_state` | planner surface, not the loop |
| `composer_mcp/server.py:402/409, 864` | yes | not web |

`git log -S "baseline=" -- src/elspeth/web/composer/tool_batch.py src/elspeth/web/composer/service.py`
returns nothing, so the loop has never passed it in available history. Refutation fails.

### Live outcomes (EXECUTED, raw captures)

`scan_calls.py diff_pipeline` over all 12 capture files: **4 calls, 4 failures**, each returning
`{"success": false, "validation": {"is_valid": false, "errors": [{"component": "rejected_mutation", "message": "No baseline available. Load or create a session first.", "severity": "high", "error_code": "diff_baseline_unavailable"}] ...}`.
First occurrences: s01 T6 (`live/capture`), s02 T2 (lv1), r01 T2 (lv2), r03 T2 (lv3) — matches the audit.

### Corrections

1. **r02 was right, not erratic.** State versions from `state.json` + `post_turn_actions.json`:
   r01 T1 v2 (accept_proposal) → T2 v2; r02 T1 v3 → T2 v3; r03 T1 v4 → T2 v4. Nothing changed since the accepted
   version in r01–r03. r02's reasoning (`r02-feedback-repeat/turn02/llm_calls.json`, seq 0) reads the supplied
   `turn_start_version 3, current_version 3` and answers "nothing has changed" — correct. Rewrite the bullet as:
   "r01 and s01 replied 'I couldn't produce a change list…'; r02 answered correctly from the session context
   that nothing had changed; r03 re-validated and described." The only live case with a real change since
   acceptance is **s01 T6** (accepted v2 at T1; tool-loop edits took it to v16 by T5).
2. **Scope wording.** On the planner surface `diff_pipeline` is offered (`PLANNER_DISCOVERY_TOOL_NAMES` includes
   it, EXECUTED) and does receive `baseline=current_state` (`pipeline_planner.py:3441`), so it "succeeds" there,
   always with zero changes because the planner never mutates. The heading's "in the web tool loop" is accurate;
   the bottom-line table should not imply the planner fails too.
3. **Line cite.** `tools/generation.py:4659-4668` → `:4659-4669` (return statement ends at 4669). Trivial.
4. **Severity.** HIGH is defensible only for s01-style sessions with unaccepted tool-loop edits; in r01–r03 the
   failure cost nothing user-visible (all three replies were honest). Consider MEDIUM, or keep HIGH but say the
   visible damage is the s01 shape.

---

## F5 — UNDERSTATED (core confirmed; impact larger; small corrections)

### Attempt to refute: is there ANY web configuration where `options.model` is exposed? No. (READ + EXECUTED)

- `PolicyCatalogView.get_schema` returns the full schema only when `self._profiles is None`
  (`web/catalog/policy_view.py:191-201`). On the web `_profiles` is never `None`: the constructor takes a
  non-optional `OperatorProfileRegistry` (`policy_view.py:21-29`); the only `None` route is
  `for_trained_operator`, which requires `snapshot.is_trained_operator` (`:32-46`). The web app always builds
  the registry (`web/app.py:2022`); `ComposerApplicationPolicy.build` raises if it is missing in non-trained
  mode (`composer/application_policy.py:36-40`). The trained-operator composer root
  (`ComposerServiceImpl.for_trained_operator`, `service.py:834-850`) has no production caller (only
  `tests/integration/web/composer/*` and the CLI MCP server).
- The registry registers the LLM resolver for `source/llm` and `transform/llm` unconditionally, whatever the
  settings (`plugin_policy/profiles.py:1540-1551`), so `public_schema` always routes llm through
  `_LLMProfileResolver.public_schema`, which drops every `LLM_PROFILE_PRIVATE_FIELDS` member including `model`
  and `provider` (`profiles.py:436-438`; `core/llm_profiles.py:38-50`).
- `web_config_authority = WebConfigAuthority.OPERATOR_PROFILED` is a class constant on both llm plugins
  (`plugins/transforms/llm/transform.py:1241`, `plugins/sources/llm/source.py:129`); validation then requires a
  profile (`plugin_policy/validation.py:803`). With no usable alias the plugin is `PROFILE_UNAVAILABLE` and not
  offered at all (`plugin_policy/availability.py:108-135`, docstring `policy_view.py:80-84`).
- Live: all 14 `get_plugin_schema(name="llm")` results in the captures (13 transform, 1 source) carry
  `profile` with `enum ["standard"]` and no `model` key (EXECUTED, parsed `data.json_schema.properties`).

So F5 is not overstated on configuration. `list_models` ignores its context entirely (`del context`,
`tools/generation.py:2563`) and serves `read_litellm_model_list()` plus the live OpenRouter slice. Live:
7 calls; every unfiltered call returned `total_models: 3938` over **91** prefixes including `1024-x-1024`,
`low`, `medium`, `high`, `standard` (EXECUTED).

### Why UNDERSTATED

The audit counts F5 as "1/1, a competent refusal". Two further first-class replies told the user something
false and the advisor admitted both (`publication_verdict: ADMIT`):

- **p01 T1** "Which models can I use?" → "You can bind a model in two ways in this deployment: … **A specific
  model slug.** … The session catalog currently reports **3,938 models**…"
  (`live/sessions/p01-capability-qs/turn01/terminal.json`; advisor seq 29 CLEAN/ADMIT).
- **s08 T2** → "Yes, you can change it, two ways: **Switch to a specific model.** I can rebind the assessment
  step to a literal model identifier… ELSPETH would raise an approval card"
  (`live/sessions/s08-grants-llm/turn02/terminal.json`; advisor seq 20 CLEAN/ADMIT, reasoning: "offers to rebind
  to a literal model from the catalog… The reply is responsive and truthful").

The audit's "In s08 the same model reasoned the opposite" understates it: s08 *told the user*. The bottom-line
cell for "Which models can I use?" ("A refusal that also says GPT-4o is served there") does not describe p01,
which was not a refusal. Correct the Live column to "3 of 3 model-choice turns cite the litellm catalog; 2 of
3 offer a slug path that does not exist on the web".

### Small corrections

- "The served schema offers only `profile`" → "offers `profile` (enum `["standard"]`) and no `model` or
  `provider`" (the served schema has 17 properties, e.g. `prompt_template`, `queries`, `response_format`).
- "about 90 providers" → 91 prefixes.
- "the skill teaches model selection by slug" → "the skill prefers `options.profile` but teaches a slug path as
  an equal fallback (`pipeline_composer.md:892-902`, `:1236`) and cites a `model_catalog` aid
  (`:896`) that no request carries any more" — `planner_authoring_aids.py:1975-1977` says it "no longer rides in
  every authoring request", and the only `model_catalog` string in the s08 T1 planner request is the skill text
  itself (EXECUTED).

---

## F10 — CONFIRMED-WITH-CORRECTION

Quotes reproduced verbatim from DeepSeek `reasoning_content`: s02 T1 seq 2 ("I need to inspect_source first. But
instruction says emit terminal proposal now"), s09 T1 seq 16, s04 T5 seq 11 (including "Emitting an empty
proposal would be wrong", which occurs only in that file). Cites `pipeline_planner.py:217` and `:5138-5139` are
correct.

Corrections:

1. **Not "once".** The trigger is `pending_information and all(manifest.supplies(k) for k in pending_information)`
   (`pipeline_planner.py:5138`), where `pending_information` is the declared set plus every key of every
   discovery call the model has made (`:3461`, `:4877-4878`). That condition is true after nearly every
   successful discovery round, and the message is appended each time. Measured (`scan_notice.py`): the notice
   appears in **29 of 41** battery planner turns (the 12 without are text-only informational turns with no
   discovery round); up to **5 identical copies in one request** (s10 T1), 4 (s11 T1), 3 (s10 T2).
2. **Root of "incomplete notion of done".** Attachment inspection is never a declared key, so the server
   cannot know a file is still uninspected; the notice fires because the model's *own* calls so far are covered.
3. **Missed consequence (links to F1).** s10 T1 seq 9: "'All declared information gaps are closed; emit the
   terminal proposal now.' That's a system nudge telling me to emit. So an answer IS expected to be emittable.
   Therefore multi-blob binding must be possible." The notice actively steered the multi-file turn toward the
   impossible binding that then timed out.

---

## F12 — CONFIRMED-WITH-CORRECTION

- **Buried argument errors — confirmed.** Exactly 2 discovery argument errors exist in the captures; both are
  planner turn-1 calls in empty sessions and both lead with `no_source_configured`, `no_sinks_configured`, with
  the real cause under `data.argument_error`: s11 T1 `get_blob_content` (`expected_content_hash: 6862`,
  "Value has invalid type") and s19 T1 `get_plugin_assistance` (`plugin_name` missing). This shape is pinned
  by `tests/unit/web/composer/test_pipeline_planner.py:9652`
  (`assert {…error_code…} == {"no_source_configured", "no_sinks_configured"}`).
- **Sibling argument names — confirmed.** Served schemas: `get_plugin_schema` {`plugin_type`, `name`};
  `get_plugin_assistance` {`plugin_type`, `plugin_name`, `issue_code`} (EXECUTED via `get_tool_definitions()`).
  1 of 16 live `get_plugin_assistance` calls used `name` (s19).
- **Unnamed extra property — confirmed, count wrong, and deliberate.** The 16 fed-back rejections break down
  as: `canonical_schema` 7 (`81603e57` smoke, `be3a87f7` s01 T1, `8e34ce35` s07, `b75ad04e` s13, `afc9ad3f`
  s08 T1, `bce4789f` s18, `b56c37f0` s10 T1); of those, 5 carry an `additionalProperties` violation and only
  **2** are the "nested `nodes`/`edges`/`outputs` inside `source`" case the README describes (s01 T1, s08 T1).
  The other extras were `sources` nested in `source` (s18) and `inline_blob`/`blob_id` on named `sources` items
  (smoke, s10 — the F1 multi-source binding gap, since named source items admit only `description`,
  `on_success`, `on_validation_failure`, `options`, `plugin`; EXECUTED on `canonical_set_pipeline_schema()`).
  Replace "(6 of 16 proposal rejections)" with "(7 of 16 rejections were `canonical_schema`; 2 of them the nested
  case)" — or, battery-only, "6 of 13". The omission of the key is by design: `_structural_schema_violations`
  is documented as "located, value-free repair facts… model-named map keys become generic tokens… rejected
  values never enter this projection" (`pipeline_planner.py:2742-2749`).
- **`patch_output_options` — wording wrong.** Both live rejections say
  `"Invalid options for sink 'csv': description: Extra inputs are not permitted"`, which names the field. The
  gap is that nothing says `description` belongs on `set_output`. Move it out of "Other unnamed fields".

## "16 of 53" — CONFIRMED-WITH-CORRECTION

Recount from raw captures (`scan_all.py` + `map_emit.py`): `live/` captures hold **53** `emit_pipeline_proposal`
calls and **16** with a fed-back rejection result (live-main 23/6, core1 1/0, lv1 7/3, lv2 8/3, lv3 6/1,
lv4 8/3). But 13 of the 53 live-main emits (3 rejections: `9736e790`, `81603e57`, `bd17723d`) map to no session
directory; their planner intent is the tutorial colours brief, and `live/runs/smoke/canary/1..10` holds the
matching ten canary runs. Those are not part of the "68 turns" in the table header. **Battery-only: 13 rejections
of 40 emits** (33%; the mixed figure is 30%). Evals reruns add 6 emits, 0 rejections. A further 4 emits sit in
failed turns with no result fed back (s06 T1/T2 credential guard, s10 T1/T2 timeout) and are counted in neither
the rejected nor an accepted bucket by the audit; say so.

Note outside my slice: `live/sessions` holds 27 sessions and **70** turn directories (excluding `smoke-edit`),
not 68.

---

## F13 — CONFIRMED-WITH-CORRECTION

`protocol.py:209-236` defines five constants; `planning_application.py:869-884` selects one by the Stage-2
verdict, and planner proposal turns carry no model prose ("this surface's model emitted a tool call, never
prose", `:862-866`). Battery replies on proposal turns (EXECUTED over `terminal.json`): 20 are exactly the
129-char review-card constant; 2 (s11 T1, s16 T1) are the auto-commit constant.

Corrections: "Every successful planner turn ends with a fixed sentence" → "every planner turn that stages a
proposal *is* a fixed sentence" (planner turns that end in a text reply — p01 T1, c03 T1, s04 T5, the s10 refusal
— are model-written). "For generated data the review-card wording always fires" is contradicted by s16
("rows should come from a model… five plausible support tickets"), which got "I prepared and validated the
requested pipeline. ELSPETH will commit it atomically." Restrict it to inline invented data.

---

## Remediation comments

**R5 (F3).** The fix location is right (add `baseline=` at `tool_batch.py:3008`), but as written it can make
things worse:
- A request-start baseline answers "what changed this turn". Asked at the start of a new turn, "since the version
  I accepted" then returns `total_changes: 0`. In s01 T6 that turns today's honest "I couldn't produce a change
  list" into a confidently wrong "nothing changed" (true answer: v2→v16, the GST column, dropped price,
  rename/format change and quantity filter). Ship it only with the baseline kind on the wire (for example
  `baseline_kind` beside `from_version`) and skill text saying a turn-start diff does not answer "since I
  accepted".
- There is no "accepted version" marker today. The nearest facts are accepted-proposal rows and
  `committed_state_id` (`sessions/service.py:3747-3752`, `composer/turn_effects.py:261`). Tool-loop edits in
  s01 were never "accepted" by any recorded act, so "since I accepted" needs a product decision about what
  counts as acceptance. S is optimistic.
- Fix the acceptance criterion: r01–r03 must answer "nothing changed since v2/v3/v4", and s01 T6 must list
  the v2→v16 changes. As written ("lists the real change in r01–r03") it cannot pass against a correct build.
- Also reword the guidance pattern at `tools/generation.py:460-462` ("the session was created without a
  loaded baseline state…"), and stop the failure envelope reporting `is_valid: false` (see Missed 1).
- Interim "take it off the loop palette" is safe and invariant-neutral. The planner palette keeps a
  trivially empty diff, so remove it there too or leave it as harmless.
- Invariants: not touched (read-only tool).

**R7 (F5).** "Conditional on the served schema exposing `model`" is a branch that cannot be taken on the web
(see the configuration proof above). The interaction skill is loaded only by the web composer (`prompts.py:50`).
So simply teach profile-only on the web, delete the stale `model_catalog` reference, and remove or neuter the
`llm_model_choice` row for web. Check the trained-operator test harness, which shares the skill but serves full
schemas. Remove `list_models` from **both** palettes: the loop, and the planner's `PLANNER_DISCOVERY_TOOL_NAMES`
plus its `model.catalog` information key (`pipeline_planner.py:215`, `:481`). "Alias to provider/model where
disclosure policy allows" is not a side option: `LLM_PROFILE_PRIVATE_FIELDS` deliberately withholds `model`
and `provider`, so disclosure is an operator decision. Extend the acceptance check to p01 T1 and s08 T2 (no
offer to pin a slug). The advisor reads the same skill, which is why it admitted both; fixing the skill fixes
the advisor's yardstick too.

**R10 (F10).** The rewording is invariant-safe (pressure text, not structure). It is not sufficient alone:
(a) append the notice at most once per request, or replace the previous copy; (b) fix the trigger so an
attached-but-uninspected blob keeps a declared pending key, or drop the notice entirely. Six assertions pin
the exact string (`tests/unit/web/composer/test_pipeline_planner.py:1565, 1946, 1989, 2123, 9655, 9780`).
Re-measure discovery rounds and first-turn latency after the change: the imperative may exist to cut
discovery loops, and softening it can cost turns (relevant to P4).

**R12 (F12).**
- "Omit whole-pipeline validation" breaks the uniform envelope that every surface and
  `test_pipeline_planner.py:9652` pin. A smaller fix: for discovery-tool argument errors, put the argument
  error in a top-level `error` / lead position and drop state errors from the envelope, changing the pinned
  test deliberately.
- "Accept both names with one deprecated" adds an alias to closed (and `openai_strict`) schemas. The same tools
  are served by the CLI MCP server (`composer_mcp/server.py`), so either rename (a public-interface change that
  needs an explicit compatibility statement under the 0.8.5+ posture) or keep the names and leave it alone. One
  occurrence in 16 calls is weak evidence.
- "Name the offending property" collides with the deliberate value-free projection. A safe variant: name the
  extra key only when it is in the canonical schema's closed property vocabulary. That covers all 5 observed
  cases (`nodes`, `edges`, `outputs`, `sources`, `inline_blob`, `blob_id`) and leaks no model-authored strings.
  Alternatively list the allowed properties at that path, which are schema-owned.
- Change the metric baseline to battery-only 13/40 (and `canonical_schema` 6/13).

**E1 (F13).** A model-written summary keeps the composer invariant, but the terminal tool's only property is
`pipeline` (EXECUTED: `planner_terminal_tool_definition` → `['pipeline']`). Adding prose means a schema change,
F4 credential-guard exposure on new content, advisor coverage and the `raw_assistant_content` pairing
(`planning_application.py:860-868`). A smaller, equally invariant-safe option: render the proposal's
model-authored `metadata.name` / `metadata.description` above the verdict constant. Every observed emit carries
them, e.g. "Read three sample colours from a JSON file and write them straight back out as JSON with no changes."
That needs no new model output and no server synthesis. Caveat: the description can drift from the graph
(s01 T6 flagged a stale description), and it must not say "validated" unless Stage-2 is green.

---

## Missed issues (in slice)

1. **The `diff_pipeline` failure envelope calls a valid pipeline invalid.** In s01 T6 the session validation was
   `is_valid: true` (`live/sessions/s01-orders-edits/turn06/validation.json`), but the tool result told the
   model `is_valid: false` with a `high` `rejected_mutation` error. This comes from `_saved_graph_rejection_result`
   used for a read-only tool (`tools/generation.py:4665`). It is a legibility defect on the same path R5
   touches.
2. **The F5 user-facing false capability claims** (p01 T1, s08 T2), both admitted by the advisor. See F5.
3. **The skill cites a `model_catalog` aid that no request supplies** (`pipeline_composer.md:896` vs
   `planner_authoring_aids.py:1975-1977`).
4. **The F10 notice repeats (up to 5 copies) and fed the s10 multi-file dead end.** See F10.
5. **The planner offers `diff_pipeline` with baseline = current state, so it can only ever report zero changes**
   (`pipeline_planner.py:3441`, the `PLANNER_DISCOVERY_TOOL_NAMES` membership was checked by execution). Harmless
   but wasted palette space; decide alongside R5.

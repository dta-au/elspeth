# Composer skill prompt audit (0.8.12 base)

Scope: `src/elspeth/web/composer/skills/pipeline_composer.md` (C, 103,817 chars, 1,449 lines) and
`src/elspeth/web/composer/skills/pipeline_capabilities.md` (K, 32,811 chars, 509 lines).
Read-only audit; no source, test or skill edited. "VERIFIED" = read in code/test at this base. "INFERRED" = reasoned, not run.
Line numbers are file lines in the current tree (C = composer, K = capabilities).

## 0. How the text is assembled and who receives it (VERIFIED)

- `prompts.py:75,96` -> `render_system_prompt()` = `K + "\n" + strip_comments(C)` (+ optional deployment overlay).
  `service.py:751` renders it once; the SAME string is used by
  - the freeform loop (`service.py:3232` -> `build_messages`) : ~43 tools (the AUTOGEN inventory, C:340-345);
  - the proposal planner (`planning_application.py:1093` -> `pipeline_planner.py:3516`, system message): palette =
    `PLANNER_DISCOVERY_TOOL_NAMES` subset (<=21 read-only tools, `capability_skill.py:17-38`) + `handoff_to_user` +
    `restore_parked_graph` + `emit_pipeline_proposal`. The probe tap shows 23 tools per call;
  - the escape hatch (`pipeline_planner.py:4231-4280`): same system message + `_escape_hatch_notice()`, palette from
    `planner_tool_definitions(discovery_policy)` (discovery + 2 recovery + terminal). The 3-tool `terminal_only` mode
    exists only in the manifest validator (`capability_skill.py:208-225`); no caller outside it (grep);
  - the advisor (`advisor_checkpoint.py:454`, `system_msg = skill_text + trigger_instructions`): full 136 KB again.
- MCP (`composer_mcp/`) never loads either skill (grep: zero references). K:6-7 ("Every authoring surface receives these exact
  bytes") is false for MCP.
- Third and fourth voices ride in the same context and are NOT in the two files: the per-request `authoring_aids`
  user message (`planner_authoring_aids.py`, 144 KB source, rule lists at :880-975, :355-410, :455-470) and the terminal
  tool-schema description notes (`tools/_common.py:180-205`, `tools/sources.py:119-125`). Most contradictions below are
  skill-vs-aids-vs-schema, not skill-vs-skill.
- Design constraint (VERIFIED): `tests/unit/web/composer/test_capability_skill_identity.py:60-68` forbids the string
  `emit_pipeline_proposal` in K or in `build_system_prompt(None)`. So the planner is told its terminal tool name only by
  `PLANNER_TERMINAL_INSTRUCTION` (`pipeline_planner.py:1964`), a 700-char user-message field. Consequence: the planner
  receives a document titled "Freeform Pipeline Composer Interaction Policy" (C:1) that says `set_pipeline` 33 times and
  never names its own terminal tool. K:13-16 claims "your interaction rules ... name yours" - they do not (C has 0 hits).

## 1. Contradictions

### 1.1 invented_source ownership (the DeepSeek 32k-char dither) - VERIFIED root cause
Five texts, three answers:
| # | Text | Says |
|---|------|------|
| a | C:1233 matrix: `invented_source | backend on generated blob bind | YOU` ; C:579-581 "keep its backend-staged `invented_source` interpretation requirement, then call `request_interpretation_review(kind="invented_source")`" | backend stages; caller surfaces with the tool |
| b | schema note `tools/_common.py:193` (reaches the planner via `tools/sources.py:123`, source options description of the terminal): "On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead." | planner includes the row |
| c | `planner_authoring_aids.py:466` "Author rows only for the planner-owned kinds (vague_term ..., pipeline_decision, invented_source)"; `:934` "The planner-owned kinds are vague_term ..., registered pipeline_decision, and invented_source." | planner authors it |
| d | K:155-161 (gate/pipeline_decision only): "where [your palette] does not [carry the review tool], the staged requirement rides in that gate node's own options inside the terminal proposal" | (correct for pipeline_decision; reads as general) |
| e | C:1269-1278 "Before stopping, enumerate pending requirements ... call `request_interpretation_review`" | tool the planner cannot call |

Code (VERIFIED): `tools/sessions.py:1198-1203` `_options_with_inline_blob_source_review(...)` appends/replaces a pending
`invented_source` row (`draft = content`, `user_term` server-derived, `tools/sources.py:375-396`,
`_options_with_pending_requirement(replace_kind=INVENTED_SOURCE)` `_common.py:332-380`) for every LLM-authored
`source.inline_blob` in the set_pipeline candidate builder, which the proposal path reuses. Test proof:
`tests/integration/web/composer/test_freeform_proposal_prevalidation.py:911-921`: the planner-authored `initial_arguments`
have NO `interpretation_requirements` on the source; the server-canonical arguments carry
`source_review:inline_source_data`, kind `invented_source`, status `pending`, `draft == inline_blob.content`.
So: the planner must never author it. What happens if it does (server row replaces the authored one via `replace_kind`) is
INFERRED from `_options_with_pending_requirement`, not run.

Corrected single statement (put once in the matrix, delete a/b/c/e variants):
> `invented_source`: never authored on any surface. The server stages it when `source.inline_blob`/a generated blob is
> bound. Freeform: after the bind succeeds, call `request_interpretation_review(kind="invented_source", affected_node_id="source")`
> (omit `llm_draft`). Proposal surface: nothing to author or call; the card is surfaced from the sealed proposal.

Fix sites: C:579-598, C:1233, C:745-776 (generated-source discipline), C:1269-1278; `planner_authoring_aids.py:466,934,
2257-2259`; `tools/_common.py:186-195` (`_INTERPRETATION_REQUIREMENTS_OWNERSHIP_SCHEMA_NOTE`: add "except invented_source,
llm_prompt_template, llm_model_choice, source_data_contract, which the server stages"). Measured cost of the ambiguity: in
the 49 tap calls (planner surface only, 0 freeform) the reasoning mentions `invented_source` 704 times and "backend" 523
times in 737,575 reasoning chars; the first four sampled calls each quote the b-sentence ("On a proposal-only surface,
include the pending requirements ...") and then guess.

### 1.2 Review tool addressed to a palette that lacks it - VERIFIED
`request_interpretation_review` is in the freeform inventory (C:343) and not in `PLANNER_DISCOVERY_TOOL_NAMES`.
Texts that instruct calling it unconditionally: C:25-31 rule 2 ("then surface it"), C:163-170 step 5, C:923, C:996-997,
C:1232-1237 matrix ("Review surfaced by: YOU"), C:1262-1278, C:1327-1330 checklist, C:1343-1347 terminal state 2,
C:1444 repair row. K:155-161 and aids :940-947 correctly scope the call ("where your palette carries"). One rule, two
voices: scoped in the capability core, unconditional in the interaction overlay. Corrected statement:
> Stage every authorable requirement (`vague_term`, registered `pipeline_decision`) in the component's
> `interpretation_requirements`. If your palette offers `request_interpretation_review`, also call it once per pending
> authorable row after the topology validates. If it does not, the sealed proposal surfaces the cards; do not look for the tool.

### 1.3 "Done means" / termination vs the proposal terminal - VERIFIED
C:51-55 and C:1336-1360 define done as a valid `preview_pipeline`, review calls, advisor `STOP_CONFIRMED`, `CLARIFY:`.
Planner terminal states are: `emit_pipeline_proposal` accepted, plain-text reply, `handoff_to_user`/`restore_parked_graph`
(`PLANNER_TERMINAL_INSTRUCTION`). `preview_pipeline` is in the planner palette but previews the (empty) current state,
so "a valid preview" is not reachable before the terminal (INFERRED from `planning_application.py` comment "plans an EMPTY
topology by construction"). C:144-151 and C:1356-1358 half-handle this ("in the proposal planner, reply in ordinary text").

### 1.4 Rejection feedback shape (stale for planner) - VERIFIED
C:488-531 teaches `validation.errors[]`, `rejected_component`, top-level `plugin_schemas`, `validation_guidance`,
`graph_repair_suggestions`, `PREVALIDATION_REJECTED`. The planner's terminal rejection is `_PlannerFeedbackWire`
(`pipeline_planner.py:2631-2663`): `{success:false, validation:{is_valid, errors:[{component, severity, error_code,
error_class, explanation, suggested_fix, detail, plugin_contracts, connectivity, contract, ...}]}, guidance, repeat_notice,
truncation_notice}`. No `rejected_component`, `plugin_schemas` (it is `plugin_contracts` inside each error),
`validation_guidance`, `graph_repair_suggestions`. The planner's actual repair surface is untaught in the skill text.

### 1.5 Source `guaranteed_fields` - VERIFIED (skill is silent where it matters)
C:851-852 "you must not author `schema.guaranteed_fields` for uploaded or path-bound data" is correct
(`tools/sources.py:697-760`) but the rule that matters for generated rows is absent: for LLM-authored inline content the
guard exempts (`llm_authored=True` returns None, :729) AND the server derives the stamp
(`_options_with_derived_guarantees`, `tools/sessions.py:1204-1213`). So for a generated CSV the planner declares
`schema` (mode + typed `fields`) and omits `guaranteed_fields`; nothing says so. C:1433-1434 repair rows ("Producer
guarantees are empty and producer is source | Patch source schema using inspected fields") contradict C:852 for an
uploaded source.

### 1.6 Smaller contradictions
- C:104 "Use Landscape/run-analysis tools outside this composer skill": no such tool on any surface (VERIFIED by inventory).
- K:68-71 "a recipe miss ... Recipes accelerate common builds": no recipe code exists (`grep -ril recipe src/elspeth/web/composer/*.py` = none) and composer invariant 1 bans server-derived structure. Stale.
- C:344 inventory lists `create_blob`, `wire_*`, `set_*` as available; the planner has none of them but C:635-651, C:317-323, C:1440-1441 give repair flows that call `create_blob`.
- C:1225-1226 "When `interpretation_review_disabled=true`, still call the tool": INFERRED: the flag is a session preference (sessions schemas, `tools/sessions.py:3419`, `_dispatch.py:251`); no prompt-context builder I read exposes it, so the model cannot condition on it.

## 2. Wrong-surface guidance (text the receiving surface cannot act on)

Method: line-range character counts of C sections (python over the file), then tool-name grep against the planner palette.
Numbers are approximate (section granularity); hybrid sections are estimated, marked ~.

### 2.1 Proposal planner and hatch (receive C+K = 136,628 chars)
Whole sections addressed to freeform-only tools (measured line ranges):
| C range | Content | chars |
|---|---|---|
| 98-106 | Router table (past-run tools, escalation path) | 810 |
| 123-151 | `request_advisor_hint`, `CLARIFY:` protocol | 2,149 |
| 153-182 | action path + edit-intent table (`splice_transform`, `patch_node_options`, `upsert_*`) | 1,991 |
| 184-217 | complex-build batching (`set_source`, `upsert_node`, ...) | 2,064 |
| 334-370 | Tool inventory (43 names, planner has <=21 of them) + Advisor Review | 2,820 |
| 533-570 | "On success": `applied_component`, `validation_delta`, `inline_blob` echo | 2,885 |
| 635-652 | Blob Source (`create_blob` + bind) | 1,023 |
| 1214-1314 | Assumption Review + ownership matrix ("call the review tool") | 7,286 |
| 1315-1386 | Termination States (preview, review calls, advisor) | 5,168 |
| 1419-1446 | 6 Mechanical-Repairs rows naming unavailable tools | 2,776 |
Subtotal in C: ~28,970 chars. Plus ~35% of the LLM-review/Subjective-terms prose (C:778-1154, 24 KB) that tells the model
to call the review tool or says "in the same `set_pipeline`" (~4,000, estimate) and C:488-531 failure-envelope fields the
planner never receives (~3,300, section "On failure"). Plus K: 56-61 echo/`get_pipeline_state` (~900), K:399-422
chain compaction ("through ordinary node/edge tools, then preview and diff", 3,141, optional operator-led, freeform only).
**Planner: ~40,000 of 136,628 chars (~29%) addressed to tools or envelopes it cannot use.** (estimate +/-3,000)

### 2.2 Freeform loop
Receives planner-only phrasing: K:44-52 terminal-alone / proposal-only batching (~750), C:149-151, C:1356-1358, "proposal
planner" asides (~600), plus the whole `handoff_to_user` recovery block is valid for both. **Freeform: ~2,000 chars (~1.5%).**
The freeform text is the native audience; the planner is the orphan surface.

### 2.3 Advisor (VERIFIED it receives the same 136 KB, `advisor_checkpoint.py:454,491`)
The advisor has no mutation tools (INFERRED from its role; not checked). Everything in 2.1 plus mutation guidance is dead
weight for it. Not analysed further; flag for the advisor owner.

### 2.4 MCP
No skill text delivered (VERIFIED). Nothing to cut; but K:6-7 overclaims.

## 3. Stale or false claims on the hot path

Verified TRUE (so the planner may rely on them, but must be able to find them):
- CSV coercion: `plugins/sources/csv_source.py:5-7,115-122` fixed/flexible coerce declared columns, observed keeps `str`.
  K:311-312 states it correctly. JSON/text/azure/s3 sources also pass `allow_coercion=True` (grep), so "only where the live
  contract permits" is the right hedge. Not stale; the dither came from K:309-320 being a 10-line paragraph with five
  different rules (coercion at source, no conversion downstream, int->float only, structured-output type binding,
  consumer float declaration) with no worked case.
- Sinks do not coerce: `plugins/sinks/csv_sink.py:5,464-469` `allow_coercion=False`. The skill never says this; K:314-316
  says it indirectly.
- Gate boolean routes: `core/config.py:932-966` requires labels `"true"`/`"false"` for boolean conditions. K:137-161 never
  says the labels; the only worked example is the fork macro C:626 (`routes={"true":"fork","false":"fork"}`). There is no
  worked "gate -> two sinks" exemplar anywhere (`planner_authoring_aids.py` has `routes` only at :2468,:2564, both fork) -
  yet it is the commonest topology ("gate amount>1000 into two CSVs").
- Sink schema declaration: the rule exists only in aids `:880-897` (declare `schema.fields` when named business columns
  are known; observed CSV sink locks header from first row; `guaranteed_fields` on a sink is not a header declaration).
  The skills say nothing (C:1421 only "Missing source or sink schema/options").
- Gate `on_error`, `require_all_rows` etc. in K:112-132: dense, correct, low frequency.

Verified STALE / FALSE:
- K:68-71 recipes (no such mechanism).
- C:104 Landscape tools row.
- C:1233 matrix cell "YOU" for the surface of `invented_source` on the planner (see 1.1).
- C:488-531 failure fields for the planner (see 1.4).
- K:13-16 "your interaction rules ... name [your terminal tool]" (they do not; test forbids it).
- K:6-7 "Every authoring surface" (MCP gets nothing).
- C:1:"Freeform Pipeline Composer Interaction Policy" as the title of text sent to the proposal planner.

UNVERIFIED (flagged, not tested): C:626 `condition='True'` fan-out macro still validates under current gate rules;
C:1424-1428 type_coerce/`value_transform` repair mechanics (plugins exist: `plugins/transforms/{type_coerce,value_transform,
batch_top_k,reference_join}.py`); C:1225 `interpretation_review_disabled`.

## 4. Bloat (ranked cut list; chars are estimates from line ranges)

Current: C 103.8K + K 32.8K = 136.6K chars (~34K tokens at 4 chars/token; the brief's ~50k tokens includes tool schemas).
Pinned-text constraints per item are in section 6; items touching a `taught:` block need the teaching-gate fence updated.

| Rank | Item | Where | Now | After | Saved | Notes |
|---|---|---|---|---|---|---|
| 1 | Mechanical Repairs table: per-code rows duplicate the runtime `validation_guidance` (explanation + suggested_fix per code, `error_codes.py`; taught C:496-502) and prose already given above | C:1417-1446 | 11.9K | 4.5K | ~7.4K | Rows 1439-1445 (~2.0K) restate C:299-323, 642-651, 962-970, 1193-1212 verbatim in spirit. Rows 1424-1428,1430 (3.9K) are what the error payload already carries. Keep rows 1421-1423, 1433-1438, 1446 trimmed. |
| 2 | Subjective LLM Terms: wiring rule stated 6x (C:25-31, 886-891, 913-924, 992-996, 1045-1047, 1049-1067), user_term phrase micro-rules (C:1009-1023), "tall" example, legacy-token paragraph (1069-1076) | C:972-1153 | 11.4K | 4.5K | ~6.9K | Keep: authorship test, parts+ref wiring once, one JSON example, id projection rule. |
| 3 | LLM Review Interactions: HARD RULE/Register rule/four-checks restate the matrix; "Every LLM node carries BOTH prompt roles" 1.6K fine | C:778-971 | 12.7K | 7.5K | ~5.2K | `llm_prompt_template` appears 16x in C (0 in K); say once. |
| 4 | Assumption Review prose around the matrix | C:1214-1314 | 7.3K | 3.8K | ~3.5K | Matrix + 4 sentences + recovery in 5 lines; fold C:1248-1299 into the matrix. |
| 5 | Termination States: checklist duplicates matrix + Register; "do not call review while errors" appears 3x (C:1262-1267, 1368-1372, 1376-1378) | C:1315-1386 | 5.2K | 1.8K | ~3.4K | |
| 6 | Reading a tool result prose between taught blocks (read_attachment narrative duplicates the tool description; cross-turn replay paragraph) | C:381-391, 421-429 | ~2.6K | 0.6K | ~2.0K | Taught blocks themselves must stay (gate). |
| 7 | Requested Workflow Integrity: "silent downgrade / smaller pipeline" x6 (C:18-24, 271-297, 1325, 1436-1437) | C:256-333 | 5.0K | 2.0K | ~3.0K | |
| 8 | Chain compaction (operator-led, optional, freeform only) | K:399-422 | 3.1K | 0 (move to the relevant tool description or delete) | ~3.1K | Frequency ~0; costs every call. |
| 9 | Cross-file duplicates: Step Descriptions (C:219-228 = K:266-270), Untrusted content (C:1155-1176 ~ K:469-485), Output minimization (C:1193-1213 ~ K:479-485), Required failure retention (C:1177-1192 ~ K:112-122), echo rule x3 (C:44-49, K:56-61, C:540-541) | both | ~4.9K | 0.4K | ~4.5K | Keep the K copy (shared); test `test_capability_facts_have_one_document_owner` points the same way. |
| 10 | Router/handoff/opening-turn prose | C:57-152 | ~7.6K | 3.4K | ~4.2K | Handoff action list can be 4 lines; "Opening build turns" paragraph C:108-142 is ~3 distinct rules written as 40 lines. |
| 11 | Declared types / row-template minutiae and ordered-query worked example (belongs to llm plugin assistance, which the text itself says to read) | K:322-398 | ~6.2K | 2.7K | ~3.5K | The list of refused call spellings (`row.keys()`, `(row.note \| upper)()` ...) is low-frequency incident prose. |
| 12 | Operating Contract (5 rules restating later sections; rule 4 is a paragraph) | C:13-56 | 2.9K | 1.4K | ~1.5K | |
| 13 | Source Facts header-normalisation paragraph (spelled examples) | C:687-731 | ~3.3K | 1.6K | ~1.7K | Error codes `field_name_header_spelling` carry it at run time. |
| 14 | Tool Inventory AUTOGEN block | C:339-346 | 1.3K | 0.3K (names only the planner/freeform palette, or drop) | ~1.0K | CI `generate_skill_inventory.py --check` pins it; tool schemas are already sent. Needs script change. |
| 15 | Capability "Capability and trust boundary" + dedicated incident phrasing | K:502-510 | 0.4K | 0 | 0.4K | Governance text with no decision value. |

Total identified: ~51.6K of 136.6K chars (38%). **Realistic single-prompt target: <= 85,000 chars (~21k tokens), i.e. -37%.**
If the planner gets its own overlay instead of the freeform one (section 5, option B): planner prompt ~50-55K chars
(K ~27K after cuts + planner overlay ~22-25K), freeform ~75K. Each figure is an estimate from line ranges; nothing here
was re-measured after a trial edit.

## 5. Structure and management problems

1. **No "which surface am I on" orientation, and the test forbids naming it in the skill.** The planner reads a freeform
   policy; its only surface statement is a 700-char user-message instruction. Cheapest honest fix is in code, not the skill:
   extend `PLANNER_TERMINAL_INSTRUCTION` (`pipeline_planner.py:1964`) by one sentence (edit 2 below). Structural fix:
   a planner overlay file (option B).
2. **Option B (structural, higher effort):** `render_system_prompt` returns `K + C` for every surface. Replace with
   `K + C_freeform` and `K + C_planner`, where `C_planner` = Operating contract (surface-true), handoff, reply register, workflow
   integrity, review staging-in-proposal, source/LLM/gate authoring rules, repair loop on `_PlannerFeedbackWire`.
   Cost: `service.py:751`/hash pins (`PIPELINE_COMPOSER_INTERACTION_SKILL_HASH`, `skill_markdown_history`, per-surface hash
   audit), `test_prompts.py` (see 6), `test_pipeline_planner.py` size ratchet (ceiling only; shrink is allowed, :1640),
   `test_capability_skill_identity.py:53-58` (`rendered.startswith(core)` still holds). Planner already has its own
   per-request voice; this makes the overlay explicit. Not required for the top-10 edits.
3. **Decision the model needs first is buried.** The planner's first-hour decisions are: (i) which tool ends the turn,
   (ii) which interpretation rows do I author vs not, (iii) source schema/inline_blob shape, (iv) gate route labels. In C they
   sit at C:1228-1237 (matrix), C:745-776, nowhere, nowhere. The Operating Contract (C:13-56) front-loads review-card *prose
   register* (rule 4, 8 lines) before any of these.
4. **Rules scattered over both files plus aids plus schema notes:** gate authorship rules appear in K:148-161, C:937-961 and
   aids :960-975; interpretation ownership in C:25-31, 830-840, 1228-1246, K:155-161, aids :466/:934/:880+, schema note
   `_common.py:186`; field-wiring in K:276-307 and C:1433-1437. `test_capability_facts_have_one_document_owner` enforces
   one owner only for the `[capability:*]` anchors.
5. **A small table would replace ~12K chars.** One 8-row "who stages / who surfaces / on which surface" table (kinds x
   {freeform, proposal}) replaces C:25-31 (rule 2), C:163-170, C:830-843, C:1228-1246, C:1262-1278, C:1327-1330 and the
   matching K:155-161 sentence. A second 6-row "symptom -> first call" table replaces most of C:1417-1446 given the runtime
   already ships `explanation`/`suggested_fix`.
6. **Defensive caveats with no decision value** (examples): C:1239-1241 "None of this matrix's vocabulary belongs in user
   prose" (stated again at C:836-843, 1331, Reply Register); C:353-355 CLEAN-result disclaimer; C:366-370 advisor "Advice
   alone does not establish valid option values"; K:502-510.
7. **Editor/process text in the model's context:** C:1216-1222 AUTHORING CONVENTION is stripped (non-`taught:` comments
   removed, `prompts.py:56-63`), fine; but "Spec tab" (C:223) and "narrow chat rail" (C:232) leak UI geometry the planner
   cannot observe.

## 6. Constraints on editing (tests and pins that bind skill text) - VERIFIED by reading/grep

| Pin | File | What it pins | Edit cost |
|---|---|---|---|
| ~270 literal-phrase `assert "..." in flattened` | `tests/unit/web/composer/test_prompts.py` (TestBuildSystemPrompt 615-1085, TestReplyRegisterRule 1480-1530, TestMutationEchoOperatingContract 1533-1557) | exact sentences of C: delegated-source block (697-751, ~40 phrases), uploaded-blob, persona columns, draft-first, plugin whiplash, rubric/authorship (819-930), cleanup (937), repair integrity (953), pending-review terminal (989), field wiring, identity, utility transforms, register, echo contract | Every cut/merge in sections 1-4 above needs the matching asserts rewritten. They are the cost centre. |
| capability-core identity | `test_capability_skill_identity.py` | K anchors `[capability:*]` x6 exactly once in K and absent from C (:102-126); `[capability-node:*]` x7 (`CAPABILITY_CORE_NODE_GUIDANCE`, `test_node_type_vocabulary.py`); `emit_pipeline_proposal` absent from K and `build_system_prompt` (:60); `set_pipeline` present in K; echo passage in discovery section (:91-100); "only implicit self-publishing node kinds are" sentence (:127-141); no deployment plugin names in the render (:143-176: `web_scrape`, `field_mapper`, ...); gate section keywords (:198-206); the exact public requirement shell sentence "You author ONLY `kind`, `user_term`, `draft`, and optional `display_title`." in C (:179-185) | Medium. Echo test pins K:56-61, which is dead weight on the planner. |
| field inventory table | `capability_skill.py:54-96`, run on EVERY planner call (`validate_capability_field_contract`) | K:243-255 table must match the terminal schema exactly or the call raises `AuditIntegrityError`; row regex `^\| ([a-z_]+) \| (`...`)... \|$` | Do not touch except with a schema change. |
| digest voice | `test_planner_authoring_aids.py:1472-1500` | three K phrases word-for-word with `_DISCOVERY_DIGEST_GUIDANCE`; `Model identifiers come from the supplied model catalog ...`; `list_secret_refs` | Low. |
| aids/skill phrase agreement | `test_planner_authoring_aids.py` ("A test pins the overlapping phrases against the live core", aids comment :942-945) | gate/`request_interpretation_review` scoping sentence shared by K:155-161 and aids :960-975 | Edit both together (edit 3). |
| teaching gate (whole tree) | `test_tool_result_envelope_gate.py` + `tool_result_envelope_fence.json`, `planner_teaching_fence.json`, `scripts/cicd/composer_teaching.py`, `test_composer_wire_census.py`, `test_composer_wire_scorecard.py` | every key shipped in a `ToolResult` envelope is taught by a `<!-- taught:begin ... -->` owned passage of the rendered prompt or fenced with a reason; scopes validated against the live surface/tool/path census | The "Reading a tool result" blocks (C:374-570) cannot be deleted wholesale: each taught block backs an admitted key. Move a block only by updating scope/fence. This is why C:372-570 (14.5K) stays at ~12K at best. |
| tool inventory | `scripts/cicd/generate_skill_inventory.py --check` in `.github/workflows/ci.yaml:128` and `scripts/full-suite-gate.sh:232` (`contracts` stage) | the AUTOGEN block C:339-346 equals the registered tools | Script change to shrink/drop. |
| edit-routing | `test_tool_declarations.py:815-825` | C contains "one-transform insertion", `splice_transform`, "option-only edit" | Low. |
| size ratchet | `test_pipeline_planner.py:176-276,1640` | ceiling only (106 KiB +10% + allowances); shrinking is not a failure ("a judgement call for the operator, not a gate"). Its comment states the harness request renders `pipeline_capabilities.md` only (:208) - INFERRED that the ratchet under-measures production, which renders K+C. | None for cuts. |
| skill hash audit | `service.py:951-1000` (`composer_skill_hash` recomputed from the rendered text; `skill_markdown_history`), `contracts/composer_interpretation.py:246`, `test_skill_markdown_history_authority.py`, `test_compose_loop_interpretation_review_dispatch.py` | hash is derived, not stored as a literal: any edit changes `composer_skill_hash` on new records; no literal-hash fixture found. A web restart is required for a deployed process to pick up edits (per memory/runbook; not re-verified). | None in tests; operational note. |
| fixture | `test_tall_dialog_fixture_prompt_hash_pin.py` | NOT a skill pin (its own TS fixture prompt); listed only because the name suggests otherwise | none |
| incident-skill harness | `scripts/skill_rgr/` (scenarios, harness) | scenario runner for skill changes; not run here | use for the planner-surface scenario after edits |

Repo invariants to respect: no tutorial-special text (ADR-031); no wording that implies server-authored structure; the
planner is never bypassed (nothing below proposes server derivation - every edit tells the planner what it authors or what
the server will do to what it authored, and the server behaviour cited is existing).

## 7. Top 10 edits, ranked by (confusion removed x frequency) / effort

Frequency basis: the planner surface is the surface the maintainer is probing; every planner call carries all of this.
"Pins" = tests that must be updated (section 6).

1. **Single invented_source rule (kills the 32k-char dither).** Effort S. Pins: `test_prompts.py:697-751` (delegated-source block),
   `test_planner_authoring_aids.py` (planner-owned-kinds sentence), schema-note tests if any.
   - C:1233 before: `| invented_source | backend on generated blob bind | YOU | Includes artifacts ... Lives on source.options.interpretation_requirements; ... Bind first, omit llm_draft ... |`
     after: `| invented_source | server, when it binds source.inline_blob / a generated blob | freeform: YOU (after bind, affected_node_id="source", omit llm_draft); proposal surface: nobody - the card is surfaced from the sealed proposal | NEVER author this row on any surface; do not put it in source.options.interpretation_requirements. |`
   - aids :466 and :934: delete `invented_source` from "planner-owned kinds" (list becomes `vague_term`, registered `pipeline_decision`).
   - `_common.py:193` schema note: append "Exception: invented_source, llm_prompt_template, llm_model_choice and source_data_contract are staged by the server; never author them."
   - C:579-598: cut to the two sentences "Bind generated rows as source.inline_blob; the server stages `invented_source`. Freeform only: then call the review tool with affected_node_id="source", omitting llm_draft."
2. **Surface delta in the per-request instruction (no skill change, no hash change).** Effort S. Pins: `test_capability_skill_identity.py:71-80`
   (additive assert), request-bytes ceiling (negligible). Append to `PLANNER_TERMINAL_INSTRUCTION`:
   > "This surface offers only read-only discovery plus emit_pipeline_proposal; where the rules say `set_pipeline` read
   > `emit_pipeline_proposal`; there is no request_interpretation_review, request_advisor_hint, create_blob or mutation tool and no applied_component echo.
   > Put vague_term and registered pipeline_decision rows inside the proposal's component options; never author
   > invented_source, llm_prompt_template, llm_model_choice or source_data_contract; the server stages those.
   > A rejection returns validation.errors[] with explanation, suggested_fix and plugin_contracts - repair from those."
3. **Scope the review-call sentences once ("where your palette carries ...").** Effort S-M. Pins: `test_prompts.py` (rubric/pending-review
   blocks ~819-1017), aids/K shared phrase test. Rewrite C:25-31 rule 2 tail "then surface it" -> "then, where your palette offers
   `request_interpretation_review`, call it"; C:1262-1278 and C:1327-1330 likewise; C:1343-1347 state 2 gets "or the proposal was emitted with all required rows staged".
4. **Add the missing hot-path facts as 5 lines in K (Field/Declared Types and gate bullet).** Effort S. Pins: K field-contract anchors
   (`[capability:field-contracts]` stays), `test_static_planner_guidance...` (no plugin names - keep generic, say "the CSV source", not a plugin id list: `csv` is fine, `web_scrape` is not).
   Insert after K:312:
   > "Generated or authored rows: declare the source `schema` with `mode` and typed `fields` (a fixed/flexible CSV source coerces them at ingestion; observed CSV leaves every cell a string). Omit `guaranteed_fields` for rows you generated - the server derives it. Sinks never coerce: declare only types that actually arrive. A sink whose named columns are known declares `schema.fields` (an observed CSV sink locks its header from the first row)."
   Insert in K:137 gate bullet: "A boolean `condition` routes with the labels `"true"` and `"false"`; each route value is a downstream input or a sink name."
   Verified sources: `csv_source.py:5-7`, `csv_sink.py:464-469`, `tools/sources.py:729`+`sessions.py:1204`, aids :880-897, `core/config.py:932-966`.
5. **One worked "source -> gate -> two sinks" exemplar** in the aids (not the skill; the aids already carry exemplars and are live-rendered with real plugin names): ~25 JSON lines. Effort M. Pins: `test_planner_authoring_aids.py` exemplar/domain-vocabulary guards (exemplar must not use the test domain vocabulary, aids docstring :10-23). This replaces the model's need to reason about route labels, sink schema and source schema from five scattered rules.
6. **Delete stale/wrong-surface one-liners.** Effort S. Pins: none found for these exact strings. Delete K:68-71 "Recipes accelerate ..." sentence; C:104 router row; C:223 "on the Spec tab"; C:232 "narrow chat rail"; C:1225-1226 `interpretation_review_disabled` sentence; fix K:13-16 "your interaction rules ... name yours" -> "your per-request instructions name yours".
7. **Collapse Mechanical Repairs to the rows that carry information the runtime does not (~7K).** Effort M. Pins: `test_prompts.py` repair asserts (~953-1017, 1042-1085). Delete rows C:1439-1445 (duplicate of earlier prose), C:1424-1428 and 1430 (per-code facts already in `validation_guidance`/`error_codes.py`), keep 1421-1423, 1431-1438, 1446. Before-check that the catalogue test `test_validation_guidance_catalogue.py` covers each deleted code (not done here).
8. **Replace C:1228-1246 + prose with the surface-split matrix** (kinds x staged-by x surfaced-by-freeform x on-proposal). Effort M. Pins: `test_prompts.py` matrix/registers, `TestReplyRegisterRule`. Removes ~6K of restatement (items 3-5 of the cut list). Pair with edit 1.
9. **Cut Subjective LLM Terms and LLM Review Interactions to one statement of each rule (~12K).** Effort M-L, highest churn in `test_prompts.py` (`test_core_skill_treats_authored_rubrics_as_reviewable`, `test_authored_scoring_example_keeps_scale_and_anchors_in_one_criterion`, `..._preserves_user_criterion_phrase...`). Keep: authorship test, one wired example, id projection rule, display_title rule. Do after 1-8 land and a planner-surface run shows no regression (`scripts/skill_rgr/`).
10. **Move K:399-422 chain compaction out of the always-on prompt** and trim K:322-398 ordered-query/row-template minutiae (~6.6K). Effort S-M. Pins: no test found for "compaction" in skill text (grep: only unrelated hits in `test_planner_authoring_aids.py`/`llm/transform.py`); K identity tests unaffected as long as `[capability:field-contracts]` anchor and K:243-255 table stay.

Recommended order: 2, 1, 6, 4, 3 (one small PR: ~+0.6K, removes the observed dither, no big churn); then 5, 8, 7; then 9, 10, and the planner overlay (option B) only if the single-prompt target (85K) is not enough.

## 8. Not verified / open

- Behaviour when a planner DOES author an `invented_source` row with `inline_blob` (INFERRED: server row replaces it; not run).
- Whether the sealed-proposal path surfaces every staged `vague_term` card with no review tool (K:158-160 asserts; I verified staging, not surfacing).
- Advisor prompt composition beyond `advisor_checkpoint.py:454` and its palette.
- Character counts are section-granular; hybrid-section shares (the ~35% / ~4,000) are estimates.
- The probe tap is a single DeepSeek battery (49 calls, all planner surface); frequencies are indicative, not a sample of production traffic.

# RV6: tool-call register, harness and performance evidence (adversarial review)

Reviewer: RV6-register-evidence. Tree: branch `ccr-d14093ba-glu6bw` @ `45b65e50`.
All scripts I ran are copied to `.claude/lanes/composer-desk-audit-20261112/review/RV6-scripts/`
(run from the repo root with `.venv/bin/python`). "battery6" means the six capture files
`live/capture/llm_calls.jsonl` + `live/instances/{core1,lv1,lv2,lv3,lv4}/capture/llm_calls.jsonl`
(287 provider calls). "70 battery turns" means every turn under `live/sessions/` except `smoke-edit`.

## Verdict table

| # | Claim | Verdict | One-line reason |
|---|---|---|---|
| 1 | Register static columns (tool sets per surface, kinds, args, required/optional, strict, cacheable, approval, redaction) match live objects | CONFIRMED-WITH-CORRECTION | 46 rows, every Loop/Planner/arg/flag cell reproduced independently; but the **Hatch** column and `counts.escape_hatch=3` are wrong (production hatch = 23 tools), and "args" are registry args, not the web wire args |
| 2 | Escape hatch = "recovery controls plus the terminal", 3 tools (register intro, README Method, crosscheck, composer-tools.md Surfaces) | REFUTED | `pipeline_planner.py:4275` sends `planner_tool_definitions(discovery_policy, …)` = 23 tools, to the advisor model; 4 live GLM-5.3 requests carry 23 tools and make discovery calls |
| 3 | Committed register JSON/MD is a faithful build from captures | CONFIRMED | Byte-identical regeneration from battery6 + `runs/instances/smoke` captures (302 calls), EXECUTED |
| 4 | "about 290 provider calls over 74 turns" | CONFIRMED-WITH-CORRECTION | 302 calls; they span 70 battery turns + 6 s04 re-run turns + 2 smoke-edit turns + a 10-case smoke run (23 calls) that is not a driver turn |
| 5 | Live call counts per tool (≥10 rows spot-checked) | CONFIRMED-WITH-CORRECTION | All 27 exercised rows reproduce; but 5 calls labelled `planner:` were made by the advisor-model escape hatch (surface classifier bug) |
| 6 | Live ok/fail counts + caveat "a retried request can count a result twice" | CONFIRMED-WITH-CORRECTION (caveat inadequate) | Double count confirmed (get_blob_metadata 7 vs 5 unique, list_sources 4 vs 3); the larger bias is UNDER-observation (emit 53 calls/16 results, request_interpretation_review 11/2), not mentioned |
| 7 | "248 backticked identifiers, 7 not found, all illustrative" | CONFIRMED-WITH-CORRECTION | Same 7 names reproduced and all illustrative; 248 denominator not reproducible (228 or 253 by instrument); Bottom-line wording overclaims what the instrument proves |
| 8 | "Tool loop is offered exactly the 45 tools it is taught" | CONFIRMED | 45 offered, every one ≥1 word-boundary mention; mention counts recomputed identical for all 46 rows |
| 9 | "No live call to an unoffered or unknown tool" | CONFIRMED | 0 in battery6+smoke; instrument controlled (injected `set_pipeline`+`bogus_tool` into a planner response → both flagged) |
| 10 | Argument-wire scorecard clean / skill inventory `--check` exit 0 | CONFIRMED | `composer_wire_census.py --scorecard` exit=0, `failures: []`, 43 tools; `generate_skill_inventory.py --check` exit=0, and exit=2 with injected drift |
| 11 | Performance table "measured, 68 turns" | CONFIRMED-WITH-CORRECTION | 70 battery turns (27 sessions), not 68; two p90s not reproducible (see below); advisor tokens wrong; rejections figure includes non-battery smoke calls |
| 12 | Cost driver "an advisor call on every turn" | OVERSTATED | 26 of 70 turns had no advisor call (every planner turn that staged/committed a proposal, plus s06 and s10) |
| 13 | Startup warning `composer_turn_budget_underfunded … fundable_turns_estimate=16` vs 34 | CONFIRMED | Present in `live/web.log` |
| 14 | "What worked well": tool-loop edits reliable; conversational handling incl. "Is this safe…" via `get_audit_info` | CONFIRMED-WITH-CORRECTION | s03 t4 (tool-loop explain turn) was advisor-withheld; "Is this safe" used `get_audit_info` in both runs but was WITHHELD in the re-run |
| 15 | composer-tools.md Surfaces table | CONFIRMED-WITH-CORRECTION / REFUTED (hatch row) | Loop row exact (43 + exactly `handoff_to_user`, `restore_parked_graph`); planner row right; hatch row wrong; reply-stage surface missing |
| 16 | composer-tools.md "This page documents 31 tools" / E6 "31 of 46 live tools" | CONFIRMED-WITH-CORRECTION | 31 headings, but one is `generate_yaml` (MCP-only export, not a web model tool): 30 of 46. The 16-tool undocumented list is exact |
| 17 | composer-tools.md budgets: required settings, no default | CONFIRMED | `web/config.py:369-370` `Field(..., ge=1)` |
| 18 | battery-turns.md table fields (s, status, calls, valid, reply, failure code) | CONFIRMED | 76/76 rows match `summary.json` exactly |
| 19 | battery-turns.md Outcome classifier (WITHHELD / declined / ok) | CONFIRMED-WITH-CORRECTION | Classifier not committed anywhere; misses s03 t4 (withheld notice with model text retained); labels near-identical "Build me a pipeline" replies differently |
| 20 | Harness turn attribution by time window | CONFIRMED | Separate capture file per instance, serial per instance; 0 double-attributed calls; all 264 attributed calls contain their own turn's user text |
| 21 | Harness `reply` extraction | CONFIRMED | `result.message.content` equals the joined `segments` (text + trusted_system_notice) the API returns |
| 22 | E2 / register plan | Comments below | Plan step 5 names a non-existent script; generator carries `# noqa`; hatch must be rebuilt from the production call; dedupe by tool_call_id |

Counts: CONFIRMED 9, CONFIRMED-WITH-CORRECTION 10, OVERSTATED 1, REFUTED 1 (claim 2; also the hatch row of 15), plan comments 1.

---

## Non-CONFIRMED verdicts: evidence and exact corrections

### 1 / 2 / 15. The escape hatch is offered 23 tools, not 3 (REFUTED)

READ + EXECUTED.

- Production: `src/elspeth/web/composer/pipeline_planner.py:4268-4280` — on a hatch turn
  `call_model(model_override=model_config.escape_hatch_model, tools_override=planner_tool_definitions(discovery_policy, dialect=model_config.escape_hatch_tool_contract_dialect), …)`.
  `planner_tool_definitions` (`:1654-1674`) returns discovery + recovery + terminal = 23. The hatch notice
  itself says "Read-only discovery tools remain available" (`:1967-1984`).
- Model: `planning_application.py:1152` `escape_hatch_model=self._settings.composer_advisor_model` — the hatch is
  the **advisor** model, not the composer.
- Triggers (`pipeline_planner.py:4315, 4341, 4444, 4467, 4591, 4669, 4767, 4804, 4863`): malformed reply,
  repair exhausted, composition exhausted, discovery exhausted — not only "out of budget".
- Live (EXECUTED, `RV6-scripts/census.py`): 4 requests by `z-ai/glm-5.3` with 23 tools (live seq 51, 52;
  lv4 seq 13, 21). They called `get_plugin_schema`, `emit_pipeline_proposal`, `get_blob_content`×2,
  `explain_validation_error`.
- The audit's 3-tool shape is copied from the boot probe: `boot_probe.py:202-216` probes the hatch with
  recovery + terminal only, and its comment "The escape-hatch turn sends recovery controls and the terminal"
  is false against `pipeline_planner.py:4275` (see Missed issue M1).

Corrections:
- `build_register.py:42`: `hatch = by_name(planner_tool_definitions(dialect=D))` (and say "advisor model").
  Re-render: Hatch ✓ for all 23 planner tools; `counts.escape_hatch` 23.
- `tool-call-register.md:14-15`: "Hatch: the planner's escape-hatch turn, sent to the advisor model with the
  full 23-tool planner list (discovery, recovery controls, terminal)."
- `README.md:48`: "the 23 escape-hatch tools (the planner list, on the advisor model)".
- `evidence/surface-teaching-crosscheck.md` "escape hatch 3" → 23.
- `docs/reference/composer-tools.md:45`: `| **Escape hatch** | A planner turn that exhausts a repair, composition or discovery budget or returns an unusable reply; runs on the advisor model | 23: the same list as the proposal planner |`.
- Add a fourth row (see M2): `| **Reply stage** | A tool-loop turn after interpretation review cards are staged (also the undecodable-saved-graph recovery path) | 2: handoff_to_user, restore_parked_graph |`.

The Loop row is exact (EXECUTED `RV6-scripts/static.py`): loop − registry = {`handoff_to_user`, `restore_parked_graph`},
registry − loop = ∅, 45 on both dialects.

### 1 (args). "Required / optional args" are registry args, not what the web model sees

EXECUTED. Wire vs registry diff (`composer_loop_tool_definitions` vs `get_tool_definitions`):
- `set_pipeline` on the web wire (both dialects) has the single property `pipeline` (required `["pipeline"]`);
  the register shows `nodes, edges, outputs` required and `source, sources, metadata` optional.
- Under `openai_strict`, 10 no-argument tools carry `_elspeth_no_arguments`.
Correction: rename the columns "Dispatch args (registry)" and add a note: "On the web wire `set_pipeline`
takes one `pipeline` envelope (`wire_projection`), unwrapped by `tool_batch` before dispatch." All other
static cells (kind, strict, cacheable 8 tools, approval = `delete_blob`/`update_blob`, redaction) reproduced
row for row with no difference.

### 4. Call/turn denominators

EXECUTED. The committed JSON is reproduced byte-identically by
`build_register.py out.json <battery6> runs/instances/smoke/capture/llm_calls.jsonl` (302 calls; the
rendered markdown body is also identical). Of those: 258 calls in the 70 battery turns, 6 in smoke-edit
(2 turns), 23 in a 10-case smoke run (`live/smoke.log`, seq 1-23 on the main instance; not driver turns),
15 in the 6-turn s04 re-run. Correction to `tool-call-register.md:5`: "302 provider calls: the 70-turn
battery, the 6-turn s04 re-run and two smoke runs (31 calls)".

### 5. Live call counts: spot-check (EXECUTED, `RV6-scripts/results.py`)

Independent count over battery6 + smoke, all equal to the register: `get_plugin_schema` 117 (99/18),
`emit_pipeline_proposal` 53, `upsert_node` 17, `preview_pipeline` 16, `get_plugin_assistance` 15,
`inspect_source` 13, `get_pipeline_state` 11, `request_interpretation_review` 11, `list_blobs` 9,
`list_composer_blobs` 8, `list_models` 7, `patch_node_options` 6, `patch_output_options` 6,
`set_pipeline` 6, `diff_pipeline` 4, `get_audit_info` 2, `get_blob_content` 5. No duplicate tool_call ids.

Correction: the surface split is mislabelled. `build_register.py:114-122` classifies by tool list only, so
(a) advisor-model hatch requests (23 tools) are counted as `planner`, and (b) 2-tool reply-stage requests
would be counted as `escape_hatch`. Affected cells: `explain_validation_error` planner:1 is a hatch call
(its only call); `get_blob_content` planner:4 includes 2 hatch calls; `get_plugin_schema` planner:99
includes 1; `emit_pipeline_proposal` planner:53 includes 1 by GLM-5.3. Fix: classify by model + tool list
(`model == advisor and tools` → hatch; `tools == {handoff_to_user, restore_parked_graph}` → reply stage).
`mine_sessions.py:29-34` has the same bug: its digest "hatch: 6" are reply-stage calls, and its "planner"
totals (and the crosscheck's "66 of 110 planner calls … work around the palette") include hatch calls by
the advisor model.

### 6. Result counts and the caveat

EXECUTED. Register method (tool messages after the last assistant message) vs unique-by-tool_call_id:
`get_blob_metadata` 7/0 vs 5/0 (5 calls), `list_sources` 4/0 vs 3/0 (3 calls); all other rows equal.
Cause: lv4 seq 18→19 — the planner re-requests after an inadmissible reply with a second user nudge and no
new assistant message, so the same tool messages trail both requests. "retried request" is close enough
for this mechanism, but the visible symptom (results > calls) should simply be removed by deduplicating on
`tool_call_id`.

The caveat is inadequate because the dominant bias is the opposite: a result is only seen if a later
request re-sends it as a `role: tool` message. Never observed: every accepted `emit_pipeline_proposal`
(53 calls, 0 ok / 16 fail seen), most `request_interpretation_review` results (11 calls, 2 seen — the
reply stage re-encodes history as text, `service.py:1968-1993` `_reply_only_messages`), the last call of
every turn, and calls whose rejection is folded into the hatch transcript (`pipeline_planner.py:4201-4243`).
Correction for `tool-call-register.md:92`: "Results are only those re-sent to a model in a later request:
terminal and last-in-turn calls are not observed (e.g. accepted proposals, staged review cards), so ok/fail
are lower bounds and not rates." Better: source outcomes from the session's LLM audit records
(`messages?include_llm_audit=true`, already saved per turn as `messages.json`).

### 7. Identifier check

EXECUTED (`RV6-scripts/idents.py`). Controlled: `set_pipeline` found, a fabricated `teeth_count_zzz` not
found. Note the first naive pass found all names because the skill markdown is itself under `src/elspeth`;
the corpus must exclude `web/composer/skills/*.md` (the audit's wording implies it did). Excluding them,
snake_case tokens inside backtick spans = 253 unique; the 7 not found are exactly the audit's 7, and all are
worked-example values (`pipeline_capabilities.md:371-375`, `pipeline_composer.md:627-629, 682, 1105-1106`).
With whole-span matching the denominator is 228 and only 4 are missing (`branch_one/two` and
`service_type` occur only inside larger spans). So "248" is instrument-specific and not reproduced;
substance holds.

Wording: the Bottom line (`README.md:22-23`) says "none names a tool, argument or code that does not
exist". The instrument only shows each token occurs somewhere in `src/elspeth` (any string, any file); it
cannot show the name is valid where it is taught (e.g. an argument taught for the wrong tool). Change to
"every backticked identifier except 7 illustrative example values occurs in shipped code; tool names are
checked per surface by the register".

### 11. Performance table (EXECUTED, `RV6-scripts/perf.py`, `brute.py`, `emits.py`)

Turns: `live/sessions` holds 27 sessions × 70 turns (plus `smoke-edit`, 2 turns). "68" is not reproducible
with any subset I tried (70, 72, 76, 78). 14 of the 43 follow-up turns ran on the **planner** (empty state:
s04×5, c01 t2, c03×2, p01×3, s06×2, s10 t2), so "First turn (planner) / Follow-up (tool loop)" are not the
same partition. My values under both partitions:

| Measure | README | turn 1 (n=27) | planner-surface turns (n=41) | tool-loop turns (n=29) | all follow-ups (n=43) |
|---|---|---|---|---|---|
| latency p50 | 27 / 25 | 26.3 | 21.5 | 23.6 | 18.2 |
| latency p90 (linear / nearest-rank) | **57** / **103** | 62.0 / 62.1 | 61.9 / 61.9 | 61.0 / 84.6 | 52.5 / 55.1 |
| latency max | 242 / 110 | 241.6 | 241.6 | 109.6 | 167.7 |
| calls/turn p50 / max | 3/9, 4/9 | 3 / 9 | 3 / 9 | 4 / 9 | 3 / 9 |

Prompt tokens p50 per call (70 turns): planner calls 54.3k (55.2k on turn-1 turns) vs README 56k; tool-loop
calls 69.3k (README 70k ✓); advisor calls 48.1k overall, 60.8k on tool-loop turns, 34.8k on planner turns —
README "advisor 55k" matches none. Cache hit: all calls on turn-1 turns 88.9% (README 88% ✓); tool-loop
calls 94.0% (README 93%); advisor on tool-loop turns 45.7% (README 45% ✓), 52% overall.

Rejections: within the 70 battery turns there are 40 `emit_pipeline_proposal` calls and 13 observed
rejections (≥13; up to 18 not accepted counting s06's credential-guard 422s and s10's unanswered emits).
"16 of 53" counts battery6 including the 10-case smoke run (13 emits, 3 rejections) that is outside the
"measured, 68 turns" scope, and 1 emit made by the advisor-model hatch.

Corrections for `README.md:480-488`:
- heading "Performance (measured, 70 turns)"; columns "Planner-surface turns (41)" / "Tool-loop turns (29)";
- latency "22 s / 62 s / 242 s (timeout)" and "24 s / 61 s (85 s nearest-rank) / 110 s" (or keep turn-1:
  "26 s / 62 s / 242 s"); the 57 s and 103 s p90s must go;
- tokens "54k" and "69k (advisor 48k; 61k on tool-loop turns)"; cache "89%" and "94% (advisor 46% on
  tool-loop turns)";
- rejections "13 of 40 `emit_pipeline_proposal` calls in the battery turns". P4 in the remediation plan
  repeats "16 of 53" — update it too.

### 12. "an advisor call on every turn" (OVERSTATED)

EXECUTED. 26 of 70 turns have zero advisor calls: every planner turn that staged or committed a proposal
(c02, r01-r03 t1, s01-s03 t1, s05 t1, s07-s09 t1, s11-s20 t1), the three s06 turns and both s10 turns.
Advisor calls happen on every tool-loop turn (29/29) and on planner text-reply turns. Correction for
`README.md:493`: "an advisor call on every tool-loop turn and every planner text reply, including
'Thanks, looks good'". P3's "~55k tokens per conversational turn" should be ~35k (advisor p50 on
planner-surface conversational turns 34.8k, ~3.6 s).

### 14. "What worked well"

- Tool-loop reliability: substance holds (all listed loop turns `valid=True`), but s03 t4 ("Why did you
  choose that design?") ended with "[ELSPETH-SYSTEM] Completion advisory review did not clear … Composer
  completion is withheld." (`live/sessions/s03-gates/turn04/terminal.json`, segments 2-3). Add it as the
  exception or drop s03 from the list.
- "'Is this safe on sensitive data?' via `get_audit_info`": both runs called `get_audit_info`
  (`live/sessions/s04-conversation/turn05/llm_calls.json` seq 10; `runs/sessions/s04-conversation/turn05`
  seq 11), but the re-run reply was WITHHELD. Qualify: "answered well in 1 of 2 runs; the other was
  withheld (F2)".
- Bottom line "valid graphs in 12–40 s": s02 t2 (109.6 s) and s09 t2 (103.4 s) are tool-loop edits;
  say "most edits in 12–40 s; two took over 100 s".

### 16. composer-tools.md tool count (E6)

EXECUTED. `docs/reference/composer-tools.md` has 31 `### \`name\`` headings; `generate_yaml` (line 210,
"Export Surface") is an MCP session tool / HTTP export, not offered to any web composer model. So the
page documents 30 of the 46 live tools; the 16 listed as undocumented are exactly the complement.
Corrections: `composer-tools.md:47` "This page documents 30 of the composer's model tools (plus the MCP
`generate_yaml` export)"; `remediation-plan.md:55` "30 of 46". Also `composer-tools.md:43` "the 43 registry
tools below" — only 30 are below. Not refreshed and still wrong: line 32 "Cacheable per-composition" for
all discovery tools — only 8 of 20 discovery tools are in `_CACHEABLE_DISCOVERY_TOOL_NAMES`; and
`composer_max_tool_calls_per_turn` (default 16, `web/config.py:371`) is a third budget the table omits.

### 19. battery-turns.md Outcome classifier

- The generator for `battery-turns.md` is not in `evals/composer-live-audit/` nor in the lane directories
  (searched for "declined/limited" and "WITHHELD" in every `.py` there; 0 hits). The Outcome column is not
  reproducible. Commit it beside `mine_sessions.py`.
- WITHHELD is keyed on the reply starting "Verified checkpoint evidence" (my reconstruction matches all 5
  WITHHELD rows). It misses s03 t4, whose model text is retained but which carries the "Completion advisory
  review did not clear … withheld" notice; it is labelled `ok`. Classify on the `trusted_system_notice`
  segments instead of the prefix. Advisor non-clearance events are therefore 6, not 5, in the table
  (this also touches F2's counts; RV2 owns F2).
- declined/limited is a keyword match ("can't" within the first few hundred chars reproduces 74/76 labels):
  "Build me a pipeline" is `ok` in s04 t3 and `declined/limited` in the re-run t3 for equivalent clarifying
  questions. Say in the table header that Outcome is a heuristic.

---

## Remediation comments (E2, E6, register plan)

- **Plan step 1** ("they already read only live objects"): true, but the hatch list is assembled by hand
  from the boot-probe shape, which is wrong. Every surface list should come from the same function the
  production call uses (`planner_tool_definitions(policy, dialect=hatch dialect)` for the hatch;
  `recovery_tool_definitions` for the reply stage). Ideally expose one owned `surface_tool_lists()` in
  product code and have both the probe and the register consume it, so the probe drift in M1 cannot recur.
- **Plan step 1, gate hygiene**: `build_register.py:114` and `render_register.py:14` carry `# noqa: E731`.
  AGENTS.md Editing Rules forbid lint suppressions; moving these into `scripts/cicd/` as-is adds two. Use
  `def`. The generator also reads private `_registry._CACHEABLE_DISCOVERY_TOOL_NAMES`; give it a public
  accessor before it becomes a CI gate. Check the whole-tree dynamic-attribute gates before landing
  (`d.get("function", d)` patterns).
- **Plan step 2**: add a "dialect" note — the register pins `OPENAI_STRICT`; tool membership is identical on
  `none` (45/23, EXECUTED) but argument shapes differ (`_elspeth_no_arguments`). Add a "wire args" column
  next to the dispatch args (set_pipeline's `pipeline` envelope).
- **Plan step 3**: the static columns diff cleanly today (I regenerated them, 0 differences), so `--check`
  is feasible. It must be mutation-controlled (drop a tool from a surface → red).
- **Plan step 5**: `evals/composer-live-audit/run_battery.sh` does not exist; the committed script is
  `run_scenarios.sh` (`run_battery*.sh` exist only under `.claude/lanes/…/live/`). Also make the live join
  dedupe results by `tool_call_id`, classify surfaces by model + tool list, and take outcomes from the
  session LLM-audit messages rather than "tool messages re-sent later".
- **E6**: right direction; fix the counts above and add the hatch/reply-stage rows. Doing the rewrite "after
  R1" is fine, but the factual errors in the new Surfaces table (hatch row) should be fixed now because the
  page is the reference doc.
- No composer-invariant issue: all of this is documentation/harness; nothing authors pipeline structure.

---

## Missed issues

- **M1 (product, MEDIUM): the boot probe does not probe the production escape-hatch request.**
  `boot_probe.py:202-216` sends the advisor endpoint recovery controls + terminal (3 tools) under the hatch
  dialect, with a comment claiming that is what the hatch sends. Production sends the 23-tool planner list
  (`pipeline_planner.py:4275`). The 20 discovery schemas are never validated against the advisor
  endpoint/dialect at boot, so a strict-grammar or schema rejection by the advisor route would surface only
  on the hatch — the rarest and most expensive path (it fires after a planner has already failed). The
  loop probe explicitly guarantees "exactly the list production sends" (`provider_gateway.py:394-395`); the
  hatch probe does not. Fix: build the probe's hatch tools with
  `planner_tool_definitions(PlannerDiscoveryPolicy.initial(...), dialect=contract.hatch.dialect)`, or
  correct the comment and record the gap. (READ; not executed against a provider.)
- **M2: a fourth tool surface is undocumented.** After review cards are staged, the tool loop makes a
  reply-stage call with only `handoff_to_user` and `restore_parked_graph` (`service.py:1968-1993`); the same
  2-tool list is used for undecodable saved graphs (`service.py:1060-1076`). 6 live calls in the battery
  (s02 t2, s09 t2, s12 t2, s15 t2, s16 t2, s17 t2). Absent from the register, the README Method and the
  composer-tools.md Surfaces table; the harness mislabels it "hatch".
- **M3: capture coverage.** `launch_web.py` only hooks litellm's async success/failure callbacks. All
  composer calls are async (`provider_gateway.py:569`), so coverage is complete for completed calls, but a
  call cancelled at the 240 s deadline (s10 t1) leaves no record. State this under "Not covered".
- **M4: harness latency includes a pre-turn `GET /state` and up to `poll_after_ms` (1 s) of poll
  quantisation** (`drive_session.py:179-206`). Immaterial at p50 ≈ 20 s, but say "client-observed".

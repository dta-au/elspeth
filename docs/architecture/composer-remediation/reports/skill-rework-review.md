# Independent review: composer skill quick-win pass (0.8.12)

Reviewed range: `git log --reverse 2f38b807b..11ad07d36` (9 commits) on
`fix/composer-remediation-0.8.12`. Read-only review on 2026-11-10. During the
review the branch tip moved to `fea48d82b` (one comment-only change to
`tests/unit/web/composer/test_pipeline_planner.py`, recording an operator
ruling); the test runs below executed on `fea48d82b`.

Findings were formed from the diff and the code. The author's report
(`docs/architecture/composer-remediation/reports/skill-rework-report.md`) was
read only afterwards, to check claims.

## Verdict

No defect that blocks landing on correctness grounds. One process item must be
confirmed by a human (M1). Four prompt sentences or examples should be
corrected because they are overstated, incomplete, or teach a pattern that
fails at run time (S1 to S4). The rest are notes.

## Evidence run

| Check | Result |
|---|---|
| 12 focused files (`test_capability_skill_identity`, `test_pipeline_planner`, `test_planner_authoring_aids`, `test_prompts`, `test_set_pipeline_candidate`, `test_l4_teaching_text_drift`, `test_skills_loader`, `test_tool_knob_teaching_gate`, `test_tool_result_envelope_gate`, `test_tall_dialog_fixture_prompt_hash_pin`, `test_tool_declarations`, `sessions/test_skill_markdown_history_authority`), `-n 4` | 1177 passed, exit 0 (`.claude/lanes/skill-review/focused-head.log`) |
| 7 further consumers (`test_planner_teaching_gate`, `test_advisor_tool`, `test_prompt_guard_wiring`, `test_generation_schema_response`, `test_validation_error_codes`, integration `test_freeform_pipeline_planner`, `test_stated_threshold_data_full_walk`), `-n 4` | 215 passed, exit 0 (`focused-2.log`) |
| `scripts/cicd/generate_skill_inventory.py --check` | exit 0 |
| Provenance | `elspeth.__file__` and `elspeth_lints.__file__` both resolve inside this worktree |
| Negative control (ceiling) | A throwaway pytest plugin outside tracked files made the capability core read one ASCII byte longer. Pad 0: 2 passed, exit 0. Pad 1: 2 failed, exit 1, `assert 139225 <= 139224` (`ceiling-pad0.log`, `ceiling-pad1.log`) |
| `tests/integration/web/composer/test_freeform_proposal_prevalidation.py` (planner proposal path staging), `-n 4` | 173 passed, exit 0 (`focused-3.log`) |
| Probes through the real `build_set_pipeline_candidate` | Exemplar and 18 variants; results cited per finding (`probe1.log`, `probe1b.log`; probes 3 and 4 quoted below) |

Not run: the full suite, ruff, mypy, the testcontainer suite, any live model.

## MUST-FIX before landing today

### M1. The ceiling was raised without a ruling inside the reviewed range; the ruling is recorded only by a later agent-authored commit

`tests/unit/web/composer/test_pipeline_planner.py:268-292` (constant
`_SKILL_REWORK_0812_CANONICAL_BYTES = 3494`).

At `11ad07d36` the constant's own comment said "NOT YET OPERATOR-RULED" while
the comment at lines 158-163 of the same file says the ceiling "only ever moves
by an operator ruling". Commit `fea48d82b` (outside the range) rewrites the
comment to "Operator ruling 2026-11-10: the 3,494 B allowance, gate_routing aid
included, is approved". That commit carries the same `Claude-Session` trailer
as the nine reviewed commits. I cannot verify the ruling from the tree.

Correction: the maintainer confirms the ruling recorded in `fea48d82b` is his.
If it is, nothing else is needed. If not, either revert the constant and cut
3,494 B, or use 1,682 and drop the `gate_routing` aid as the comment describes.

What the ceiling protects, how it is wired, and whether it still bites:

- It measures the canonical-JSON byte length of the planner's first request
  with the per-request parts removed: system message (capability core plus
  interaction skill), the authoring-aids and catalog reference message, the
  fixed payload keys (including `instruction` and `information_manifest`) and
  the whole tool palette. `test_initial_request_declares_supplied_information_and_keeps_discovery_available`
  asserts `fixed_bytes <= _FIXED_SCAFFOLDING_MAX_CANONICAL_BYTES`
  (line 1666), for a live and a small model catalog.
- The ceiling is `int(106 * 1024 * 1.10)` = 119,398 plus ten additive named
  allowances; the new constant is the tenth term (line 304). Measured sum:
  139,224.
- Actual is exactly 139,224, so headroom is zero. My one-byte control goes
  red, so the test still fails if the prompt grows further.
- Limits of that protection: the in-test "exact boundary / +1 byte" block
  (lines 1667-1676) pads a copy by arithmetic and can never fail for a prompt
  reason; and nothing mechanical stops the next change adding an eleventh
  constant. The comment is the only control on that, which is why M1 matters.
- The sum already includes a 4,096 B `_FIXED_SCAFFOLDING_WORKING_HEADROOM_BYTES`
  term, and actual still equals the ceiling: the declared working room is
  fully consumed. The ceiling now sits 28% above the 106 KiB baseline; the
  earlier allowances cite their own rulings in the comments above this one
  (I did not audit them).

## SHOULD-FIX

### S1. The new disclosure sentence licenses a disclosed downgrade and contradicts the rule it sits in

`src/elspeth/web/composer/skills/pipeline_composer.md:34-37` (Operating
Contract rule 1), pinned at `tests/unit/web/composer/test_prompts.py:1632`.

New text: "If what you deliver omits or substitutes any part of the request,
say so first in your reply and in `metadata.description` ("Not built: ...");
if the missing part is the request's core operation, decline with the named gap
instead."

The same rule opens "Never drop a requested source / transform / sink / LLM /
cleanup step to pass validation ... repair the node, or refuse with a named
gap", and Requested Workflow Integrity (lines 284-289, 297-301) says a smaller
pipeline "requires a named-gap refusal if it is truly unbuildable" and "the
omission is the bug". The new sentence makes a third outcome available for any
non-core part, with no condition that the part be unbuildable: omit it and say
so. "Done means" still lists no such terminal state.

Smallest correction: condition the sentence on the named gap, for example "If
a non-core part is a named gap and you deliver the rest, say so first in your
reply and in `metadata.description` ("Not built: ..."); if the gap is the
request's core operation, decline instead." Update the pin.

### S2. "An authored copy is replaced" is false when the bound rows are the user's own

`src/elspeth/web/composer/skills/pipeline_composer.md:612-613` ("the server
stages it from the bytes it binds and replaces an authored copy") and `:1274`
(matrix: "an authored copy is replaced"). The report repeats it (section 2,
`c6dea535e`; section 9 item 8).

`src/elspeth/web/composer/tools/sources.py::_options_with_source_blob_review` returns the options
untouched when `SOURCE_AUTHORING_KEY` is absent, which is every verbatim bind
(`src/elspeth/web/composer/tools/blobs.py::_blob_creation_provenance`: content that is a substring of
the user message). Probe, exemplar plus an authored row
`{"kind": "invented_source", "user_term": "my_rows", "draft": "I made these"}`:

- generated content: accepted; the stored row is
  `source_review:inline_source_data` with the bound bytes as draft (replaced,
  as claimed);
- verbatim content: accepted; the stored row is `source_review:my_rows`,
  `status: pending`, `draft: "I made these"` (kept, not replaced, not
  rejected).

The instruction "never author" is right, and more important than the text
says; only the stated consequence is wrong. Smallest correction: delete the
two "replaced" clauses (it also returns bytes), or say "an authored copy is
replaced, or on the user's own rows left as a spurious pending card". The
surviving authored row on verbatim data is a pre-existing code defect, not
introduced here; it belongs in the report's defect list.

No test added in this range exercises replacement on either path:
`test_invented_source_authorship_has_one_answer_across_skill_aids_and_schema_note`
(`tests/unit/web/composer/test_capability_skill_identity.py:240`) asserts
strings only. I did not search the older suites for a test of the verbatim
case.

### S3. The exemplar pairs a `flexible` source with `fixed` sinks, a pattern that validates and then fails at run time when the file has any undeclared column

`src/elspeth/web/composer/planner_authoring_aids.py:2693` (sinks
`mode: fixed`) and `:2704` (source `mode: flexible`).

As written the exemplar is valid: the real validator accepts it on both the
verbatim and the generated path, with no errors and no warnings, and the
option names, route labels and types are right. The hazard is what it teaches
by example. A flexible source passes undeclared columns through; a fixed sink
schema is built with `extra="forbid"` (`src/elspeth/plugins/infrastructure/schema_factory.py:184`)
and `src/elspeth/engine/executors/sink.py:316-322` raises `PluginContractViolation` on a
row that fails it. Probe: source `flexible` declaring only `weight_kg: int`,
sinks `fixed` declaring only `weight_kg: int`, same two-column CSV:
`acceptable=True`, no errors, no warnings. Direct check of the fixed schema
with the extra column: `Extra inputs are not permitted`. So a planner that
copies the modes and declares "the compared field" (which is what rule 2 of
`_GATE_ROUTING_RULES` asks for) gets a green proposal that stops on the first
row. Build acceptance is measured; the run-time failure is inferred from the
two code sites and the schema check, not from an executed pipeline.

Smallest correction: make the exemplar source `fixed` (it declares every
column of its own inline rows, so nothing else changes), or make the sinks
`flexible`. Both variants were probed and are accepted with no errors and no
warnings. Then re-measure the ceiling.

### S4. Gate route values are listed as exhaustive but omit `discard`, and the two new texts disagree with each other

`src/elspeth/web/composer/skills/pipeline_capabilities.md:141-143` ("each
route value is a sink name, a downstream node's `input` connection, or
`fork`") and `src/elspeth/web/composer/planner_authoring_aids.py:497` ("Each
route value is a sink_name or a downstream node's input connection").

`src/elspeth/core/config.py:858-859` accepts `fork` and `discard` as route destinations,
and the composer validator accepts
`routes: {"true": "freight", "false": "discard"}` (probe: `acceptable=True`).
Neither text teaches a discard route, and a search of the whitespace-flattened
system prompt finds `discard` beside "route" only in the `on_error` guidance;
the `routes` schema is a bare string map. So a "keep only rows where ..." request is steered to a second sink.
The aid sentence also drops `fork`, which the same aids payload uses in the
fork exemplars.

Smallest correction: "... a sink name, a downstream node's `input`
connection, `fork`, or `discard`" in the core, and the same list in the aid
rule (or remove the aid sentence, since the core now states it).

## NOTES

- **N1. Advisor is not addressed by the surface orientation.**
  `pipeline_composer.md:13-21` says the rules go to the tool loop and the
  planner and "a rule that names a tool your palette does not offer does not
  apply to you". `src/elspeth/web/composer/advisor_checkpoint.py:454` sends the same text to the
  advisor, which has no palette and reviews tool-loop turns; read literally,
  no tool rule applies to what it is judging. The capability core's new first
  paragraph names three readers, the interaction skill two. One sentence
  would close it ("If you are reviewing another model's turn, apply the rules
  for the surface that model is on"). Inferred risk, not observed behaviour.
- **N2. A server error message now contradicts itself.**
  `src/elspeth/web/composer/tools/blobs.py:790-794` says "stage an authorable pending source review
  with set_source or patch_source_options" and then appends the new
  `_INTERPRETATION_REVIEW_FOLLOWUP`, which says to author only `vague_term`
  and registered `pipeline_decision` rows and never the source kinds.
- **N3. "ONE mutation call" against the batching guidance.**
  `pipeline_composer.md:481` is correct for approval mode (verified:
  `src/elspeth/web/composer/tool_batch.py:2330-2340` returns the unchanged state's validation,
  `base_state_id` is the current state, and
  `src/elspeth/web/sessions/routes/composer/proposals.py:439-440` refuses a stale base). Lines
  204-216 still recommend bounded construction batches without that
  exception. The more specific sentence should win, but it is 270 lines away.
- **N4. One repair row kept the old create-a-blob advice.**
  `pipeline_composer.md:1478` ("create a blob from the generated rows") was
  not scoped, while the prose it summarises (lines 677-683) now prefers
  `source.inline_blob` and scopes `create_blob` to palettes that offer it.
- **N5. Weak spots in the new tests.**
  `test_a_non_boolean_label_pair_on_the_same_gate_is_rejected`
  (`test_planner_authoring_aids.py:973`) asserts only `acceptable is False`;
  I confirmed the rejection is `gate_route_labels_mismatch`, but any
  rejection would pass. The exemplar is validated only on the verbatim path
  (its content is placed in the user message); the generated path also passes
  (probe). `test_prompts.py:1556` reads `tool_batch.py` source text for a
  literal rather than exercising the no-op result. `TestSkillFollowsCurrentToolBehaviour`
  and `test_interaction_skill_scopes_review_calls_to_surfaces_that_offer_the_tool`
  otherwise assert sentences the same commits introduced; they pin wording,
  not behaviour. The splice, diff, preview and approval sentences have no
  behavioural test in this range.
- **N6. One planner rejection does not have the shape the instruction
  describes.** A proposal rejection is compiled into `_PlannerFeedbackWire`
  (`success`, `validation.errors`). The mixed-terminal-batch rejection
  (`src/elspeth/web/composer/pipeline_planner.py:1939-1948`) returns
  `success`, `error_code` and `message` at the top level with no
  `validation`. The instruction says "a rejected proposal", and that case
  executes no proposal, so the sentence is defensible; a model that looks for
  `validation.errors` there finds none.
- **N7. Coercion sentence is correct and non-exhaustive.** Measured against
  `create_schema_from_config(..., allow_coercion=True)`: `"42"` to `int`,
  `"1.5"` and `"7"` to `float`, the six listed bool spellings, plus `on`/`off`,
  `t`, `y` and case variants; `"4.5"`, `"1e3"`, `""` fail for `int`, `"nan"`
  fails for `float`. The test checks the schema factory, not the CSV plugin
  (`src/elspeth/plugins/sources/csv_source.py` does pass `allow_coercion=True`).

## Pins judged

| Pin | Judgement |
|---|---|
| `test_prompts.py:1013` "Repair the topology first, then surface the review cards from the repaired state" | Honest. The Termination States copy was deleted; the surviving copy at `pipeline_composer.md:1299-1302` carries the same contract and the companion assertion on its first half still passes against it. |
| `test_set_pipeline_candidate.py:2086-2094` full rejection message | Honest. It is an exact-equality pin on the message that embeds `_INTERPRETATION_REVIEW_FOLLOWUP`; it moved with the constant. The dropped sentence ("The user resolves the card and ELSPETH writes resolved review metadata") is no longer told to the model in this message. |
| `test_planner_authoring_aids.py:1946` `== 3` to `== 4` | Honest. The fourth exemplar is added to both tuples and still validated against the live provider schema. |
| `_SKILL_REWORK_0812_CANONICAL_BYTES` | See M1. Arithmetic is exact and the gate still bites; the authority to move it is the open question. |

No existing assertion was deleted or made vacuous.

## Verified true (code read or probed)

- Planner palette: 23 tools, exactly the 20 discovery tools, the two recovery
  tools and `emit_pipeline_proposal`; every tool the instruction names as
  absent is absent there and present in the 45-tool loop palette. The escape
  hatch continues the same planner conversation with
  `planner_tool_definitions(...)` as its tools
  (`src/elspeth/web/composer/pipeline_planner.py:4302-4306`), so the
  instruction is true there too. The planner does have `preview_pipeline` and
  `diff_pipeline`, and the skill's new sentences allow for that.
- The planner proposal path stages `invented_source` through the same
  builder the probes used: `src/elspeth/web/composer/pipeline_planner.py`
  calls `build_set_pipeline_candidate` at lines 3106, 3133 and 3174, and
  `test_freeform_proposal_prevalidation.py::_assert_repaired_inline_builder_stages`
  (run, passing) shows authored arguments without a requirement and
  canonical arguments carrying the server's row.
- Planner rejection shape: schema, argument and candidate rejections of a
  proposal compile into `_PlannerFeedbackWire`
  (`src/elspeth/web/composer/pipeline_planner.py:2592`, `2727-2742`);
  `component` and `error_code` are required keys, the other four named keys
  are optional. See N6 for the one rejection that differs.
- Boolean gate labels (`src/elspeth/core/config.py:929-958`); a `condition: 'True'` fork
  gate uses the same labels, so the fork exemplars agree.
- `row_union` branch keys must be gate `fork_to` names (`src/elspeth/web/composer/state.py:9466-9476`).
- Batch-aware plugins refuse `required_input_fields` and are told to use
  `schema.required_fields` (`src/elspeth/web/composer/state.py:1779-1798`).
- Only non-optional declared fields are guaranteed
  (`SchemaConfig.get_effective_guaranteed_fields`, probed for `fixed` and
  `flexible`); generated CSV binds auto-declare guarantees
  (`src/elspeth/web/composer/tools/sources.py:512-560`), so "you need not author `guaranteed_fields`"
  holds for rows the model generated.
- Sinks never coerce (`allow_coercion=False` at every schema construction
  under `src/elspeth/plugins/sinks/`: text, json, csv, document, database,
  dataverse, chroma, azure blob, S3, Power Automate); a sink declaring `weight_kg: str` against an `int` producer is
  refused at build with `edge_field_type_incompatible`.
- `inline_blob` inside `options` is refused (`plugin_options_invalid`).
- `splice_transform` needs exactly one matching `on_success` edge
  (`src/elspeth/web/composer/tools/transforms.py:1162-1168`).
- No-op and refused mutations under approval (`src/elspeth/web/composer/tool_batch.py:226-250`);
  `post_call_hints` can be server-authored.
- `diff_pipeline` refuses with `diff_baseline_unavailable` when the surface
  passes no baseline (`src/elspeth/web/composer/tools/generation.py:4669-4679`).
- The loop palette has no run-analysis tool (`get_audit_info` describes the
  audit trail's configuration, not a run).
- All four cut passages survive elsewhere with their clauses intact (step
  descriptions in the capability core lines 272-276; malformed arguments at
  `pipeline_composer.md:324-328`; repair-before-review at 1299-1302; the
  matrix-vocabulary reminder in the register rule at 1254-1257 and Reply
  Register).
- No tutorial-specific text or branch was added; nothing tells the model to
  let the server author structure; no validator was changed.
- No markdown damage: table column counts are consistent in both files,
  `taught:begin`/`taught:end` are balanced (18/18 and 2/2), no duplicated
  words, no trailing whitespace, the AUTOGEN inventory check passes.

## Inferred, or not reviewed

- "`preview_pipeline` reports ... never row data" was checked against the
  tool's description and docstring only; I did not inspect every
  `proof_diagnostics` payload for row values.
- "which the server stages" for `source_data_contract`, `llm_prompt_template`
  and `llm_model_choice` on the planner path was not traced; it matches the
  pre-existing ownership matrix.
- Whether `create_blob` itself becomes an approval proposal was not checked.
- Run-time graph construction and execution of the new exemplar were not
  exercised, by me or by its tests;
  `tests/integration/pipeline/test_composer_runtime_agreement.py` does not
  enumerate the exemplars and was not run.
- The tutorial script was not traced end to end; the exemplar's parcel and
  weight vocabulary does not appear in the frontend tutorial components.
- The report's 58 failures attributed to another lane were not reproduced.
- The report's per-commit byte figures were not re-measured, only the final
  139,224.
- No model was run, so every statement about what a model will do with a
  sentence is a reading of the text, not an observation.

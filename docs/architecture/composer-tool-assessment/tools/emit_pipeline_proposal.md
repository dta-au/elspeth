# `emit_pipeline_proposal` — planner-only complete proposal terminal

Baseline: `3b28d537be98234da8135c418bf185a998c18b68`, restored 0.8.9.

## 1. Purpose and usage

This is the planner's terminal call for submitting one complete provider-authored
pipeline after capability discovery. On rejection the model repairs and
resubmits the complete document. It is not an incremental graph mutation, an
execution command, or a success announcement to the user.

Only the planner route exposes it. The ordinary canonical list and freeform
loop do not; the latter uses `set_pipeline`. Composer MCP does not advertise it.
The planner, and therefore this tool, is selected only when the current
composition state is structurally empty (no source, sources, nodes, edges or
outputs) and a sessions service, session id and user message id are all present
([route gate](../../../../src/elspeth/web/composer/service.py#L1234)). Every
request against a non-empty pipeline goes to the freeform loop, so this tool is
the authoring path for every first build and never for an edit. The planner must have an admitted request, immutable capability/policy context,
provider invocation ownership, candidate finalizer/validator and session-backed
custody for the eventual proposal. The tool is available on both dialects but
explicitly non-strict even on `openai_strict` because its full-document schema
is not a strict-capable transport grammar.

## 2. Contract

The root is closed and has one required field, `pipeline`, containing the
canonical `set_pipeline` document schema. The complete schema is appended below.
The inner document is validated again by `SetPipelineArgumentsModel`, so passing
JSON Schema is necessary but not sufficient. A sourceless candidate (or one
supplying both `source` and `sources`) is rejected by the JSON Schema root
`oneOf` precheck with `canonical_schema` feedback naming `rule: oneOf`; the
Pydantic `_exactly_one_source_configuration` validator is a second gate. The
dedicated `no_source_configured` missing-source branch after both is not
reachable from provider input (see `emit_pipeline_proposal-N1`). Omitted fields, explicit null and authored field presence
must retain their canonical meaning; the server must not infer missing topology
as a substitute for provider authorship.

The terminal must be alone, and the consequence depends on the sibling type.
With discovery siblings, every call gets `TERMINAL_CALL_NOT_ALONE` tool feedback,
nothing is dispatched and one repair unit is spent. Two terminal calls in one
turn are `MALFORMED_RESPONSE` and end planning. A terminal batched with a
recovery control also ends planning as `MALFORMED_RESPONSE`, because the
recovery-sibling check runs first (`emit_pipeline_proposal-N2`). Terminal
arguments that are not a strict JSON object end planning with
`MALFORMED_RESPONSE` too, unless the response hit the completion cap or reported
`finish_reason="length"`; those cases are repaired as truncation. These fatal
outcomes do not engage the escape hatch. Successful admission runs the required-control
finalizer, candidate validation and plan construction, yielding a `PipelinePlanResult`
with provider-authored/effective identity, validation and audit evidence. The
application still stages/publishes the proposal and review; a validated plan is
not proof of session commit or execution readiness. User approval is not always
required. If the trust-mode snapshot and the current preference both say
`auto_commit` and the Stage-2 preflight over the candidate state is green, staging
mints a `PipelineCommitIntent`
([auto-commit gate](../../../../src/elspeth/web/composer/planning_application.py#L855)).
Turn settlement then commits without review, or downgrades to review if the
authority was revoked meanwhile
([settlement](../../../../src/elspeth/web/sessions/composer_turn.py#L1132)).

Admission has one durable effect before any user decision. A candidate whose
`source.inline_blob` passes validation is materialised as a session blob under
the operation fence during plan construction. The blob id is deterministic and
the write is idempotent ([custody](../../../../src/elspeth/web/composer/pipeline_planner.py#L3110),
[reservation](../../../../src/elspeth/web/blobs/service.py#L2804)), so an
unchanged resubmission reuses the blob. A scripted run shows the `ready` blob row
and its file exist while no proposal row exists yet. Nothing on the rejection
path releases the blob. Pipeline rejection touches only the proposal, proposal-event
and session tables, and every blob-row delete belongs to explicit deletion, fork
cleanup or pending-reservation cleanup (§7, `emit_pipeline_proposal-N4`). A refused
proposal therefore leaves its inline blob in the session. The model receives no user-visible reply
channel: the staged turn's message is one of the fixed `PIPELINE_STAGED_*` server
strings ([protocol](../../../../src/elspeth/web/composer/protocol.py#L209)), and
any prose the model sends with the accepted terminal call is not delivered.
Step `description` fields and `metadata.description` are the only explanatory
text the model authors that reaches reviewers.

Failures include wire/schema/Pydantic rejection, unavailable plugins/profiles,
missing sources, semantic/runtime validation, invalid review references, repeated
rejections, provider failures, response truncation and expired/budget-exhausted
turns. The planner owns retries, deadline, repair/composition/tool-call limits
and optional configured escape hatch. Both LiteLLM retry spellings are pinned
to zero so physical attempts remain individually budgeted/audited. No candidate
cache or general idempotency key is advertised. Repeated-rejection fingerprints
inform repair; they are not an exactly-once commit guarantee. Session operation
fences and proposal authority own concurrent publication.

## 3. Implementation trace

- [Terminal payload/definition](../../../../src/elspeth/web/composer/pipeline_planner.py#L1569),
  [policy-selected planner exposure](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647),
  [provider retry ownership](../../../../src/elspeth/web/composer/pipeline_planner.py#L1605).
- [Provider response parsing](../../../../src/elspeth/web/composer/pipeline_planner.py#L1794)
  (the terminal is excluded from wire decoding at
  [L1869](../../../../src/elspeth/web/composer/pipeline_planner.py#L1869); a second
  terminal is refused at [L1898](../../../../src/elspeth/web/composer/pipeline_planner.py#L1898)),
  [recovery-sibling check](../../../../src/elspeth/web/composer/pipeline_planner.py#L4352),
  [terminal singleton protocol](../../../../src/elspeth/web/composer/pipeline_planner.py#L4401),
  [terminal branch](../../../../src/elspeth/web/composer/pipeline_planner.py#L4454) with its
  [root/schema/Pydantic gates](../../../../src/elspeth/web/composer/pipeline_planner.py#L4476)
  and the unreachable [missing-source branch](../../../../src/elspeth/web/composer/pipeline_planner.py#L4515).
- [Pydantic feedback](../../../../src/elspeth/web/composer/pipeline_planner.py#L2859),
  [required-control finalizer call](../../../../src/elspeth/web/composer/pipeline_planner.py#L4531),
  [plan construction](../../../../src/elspeth/web/composer/pipeline_planner.py#L4626),
  [plan assembly helper](../../../../src/elspeth/web/composer/pipeline_planner.py#L3041).
- [Planner application](../../../../src/elspeth/web/composer/planning_application.py#L1017)
  bridges the plan into session-backed staging/review;
  [pipeline commit authority](../../../../src/elspeth/web/composer/pipeline_commit.py)
  binds authored/effective content to audit and durable publication.
- [Proposal routes](../../../../src/elspeth/web/sessions/routes/composer/proposals.py)
  handle user acceptance/refusal; [turn persistence](../../../../src/elspeth/web/sessions/composer_turn.py)
  owns terminal response. Browser workspace reads resulting state/proposals;
  it does not consume the private planner terminal as an execution receipt.

### 3.1 Route inventory

Measured by source trace and two read-only introspections (§7); the §7.1 probe runs later exercised the planner precheck, the terminal-alone rule and the inline-blob custody write behind some of these rows. Name searches
were controlled: each file reported as having no mention of the tool does mention
`set_pipeline`.

| Route | Reaches / consumes | Gate | Evidence |
|---|---|---|---|
| Name authority | `PLANNER_TERMINAL_TOOL_NAME` | constant; the capability-manifest check requires the terminal last, after the two recovery controls | [capability_skill.py](../../../../src/elspeth/web/composer/capability_skill.py#L38), [order check](../../../../src/elspeth/web/composer/capability_skill.py#L216) |
| Definition / stamping | `planner_terminal_tool_definition` → `stamp_planner_terminal` | `openai_strict` adds `strict: false`; `none` sends no `strict` key | [definition](../../../../src/elspeth/web/composer/pipeline_planner.py#L1577), [stamp](../../../../src/elspeth/web/composer/tools/wire_projection.py#L869) |
| Planner palette, ordinary turns | discovery (policy-filtered) + recovery + terminal | `planner_tool_definitions(policy, dialect)`; discovery may be narrowed by the policy, terminal is never removed | [L1647](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647) |
| Planner selection | `compose()` → `_plan_and_stage_empty_pipeline` → `plan_pipeline` | structurally empty state + sessions service + session id + user message id | [service.py](../../../../src/elspeth/web/composer/service.py#L1234), [planning_application.py](../../../../src/elspeth/web/composer/planning_application.py#L1114) |
| Escape hatch | same palette re-stamped for the hatch dialect, advisor model/endpoint | configured `escape_hatch_model`/provider; only after a repair/discovery/composition/prose exhaustion; one shot | [L4268](../../../../src/elspeth/web/composer/pipeline_planner.py#L4268) |
| Boot probe | recovery controls + terminal for the hatch route | only when the hatch dialect is not `none`; reply capped, no planning | [boot_probe.py](../../../../src/elspeth/web/composer/boot_probe.py#L210) |
| Required controls | `wire_required_controls` as `candidate_finalizer` | every schema-valid sourced candidate | [planning_application.py](../../../../src/elspeth/web/composer/planning_application.py#L159) |
| Staging | `_stage_pipeline_plan` builds the summary and redaction as `set_pipeline`. The proposal row is inserted with `tool_name="set_pipeline"`, and the accepted evidence also carries `set_pipeline` | always after an accepted plan; auto-commit as in §2 | [L607](../../../../src/elspeth/web/composer/planning_application.py#L607), [L668](../../../../src/elspeth/web/composer/planning_application.py#L668), [row insert](../../../../src/elspeth/web/sessions/mutation_capabilities.py#L246), [accepted evidence](../../../../src/elspeth/web/sessions/service.py#L3893) |
| Auto-commit settlement | turn settlement applies the `PipelineCommitIntent`, or downgrades to review when auto-commit authority was revoked | trust mode `auto_commit` (snapshot and current) + green Stage 2 | [composer_turn.py](../../../../src/elspeth/web/sessions/composer_turn.py#L1132), [downgrade](../../../../src/elspeth/web/sessions/composer_turn.py#L1155) |
| REST accept / reject | canonical pipeline proposals (those with a pipeline authority, inferred from source naming) settle the sealed draft by `draft_hash` and are not re-dispatched through `execute_tool` | authenticated session, proposal lease, `draft_hash` required (422 without) | [accept](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L290), [settlement](../../../../src/elspeth/web/sessions/routes/composer/pipeline_settlement.py#L662), [reject](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L724) |
| Recovery controls | `handoff_to_user` / `restore_parked_graph` end planning instead of a terminal | alone in the turn; a terminal sibling is fatal | [L4352](../../../../src/elspeth/web/composer/pipeline_planner.py#L4352) |
| Audit consumers | private candidate capture, credential guard, provider-context re-encoding, diagnostic evidence | name-keyed branches for `emit_pipeline_proposal` | [capture](../../../../src/elspeth/web/composer/pipeline_planner.py#L3689), [credential_guard.py](../../../../src/elspeth/web/credential_guard.py#L264), [provider_context.py](../../../../src/elspeth/web/composer/provider_context.py#L116), [diagnostic_evidence.py](../../../../src/elspeth/web/composer/diagnostic_evidence.py#L469) |
| Visible tool events | name-keyed custody projection exists, but no producer captures a terminal event | `VisibleToolEvent.capture` call sites are planner discovery dispatch and the freeform `turn_audit` only | [visible_tool_event.py](../../../../src/elspeth/web/composer/visible_tool_event.py#L116), [planner capture](../../../../src/elspeth/web/composer/pipeline_planner.py#L5013) |
| Freeform loop / `tool_batch.py` | not advertised; an unadvertised call reaches the dispatcher's generic `Unknown tool` failure | — | [_dispatch.py](../../../../src/elspeth/web/composer/tools/_dispatch.py#L921) |
| Composer MCP | absent | — | no mention in `composer_mcp/server.py` |
| Frontend | no production consumer. The proposal is shown as a `set_pipeline` proposal; only an E2E harness reads `selected_tools` attempt data | — | `frontend/tests/e2e/harness/planner-efficiency.test.ts` |

No tutorial-specific branch was found on any route above.

## 4. Alternative paths

Discovery can precede one valid proposal; a rejection can lead to corrected
full-document resubmission. Required controls may alter the effective candidate
under explicit server-owned admission policy while the provider's authored
identity is retained. This is an admission safeguard, not permission for the
server to author a replacement pipeline.

Mixed terminal/discovery replies reject the whole batch before dispatch, close
the tool protocol with feedback and spend repair budget. Terminal/terminal and
terminal/recovery replies, and terminal arguments that are not strict JSON
below the output cap, end planning with `MALFORMED_RESPONSE` and get no repair
or hatch. Calling a mutation tool such as `set_pipeline` from the planner
ends planning with `DISCOVERY_ONLY`. Truncated responses
discard even parseable candidate calls before validation. Exhausted ordinary
repair can enter a configured independent model hatch; a hatch failure preserves
the original exhaustion classification. Recovery controls remain available and
can terminate planning without candidate construction. A strict route still
admits this terminal locally through its non-strict schema and parser.

## 5. Usability assessment

**Intended restriction, high confidence:** full-document repair, planner-only
availability and explicit `strict: false` are deliberate. The model must finish
discovery in earlier turns; freeform callers select `set_pipeline` instead.
Transport strictness is not a substitute for server validation.

**Structural limitation, high confidence in source, moderate in operational
impact:** proposal meaning is distributed across terminal parsing, set-pipeline
models, required controls, plan construction, application staging, review and
commit settlement. A terminal success cannot tell the orchestrator which later
obligation remains unless it interprets these distinct outcomes correctly. No
specific invalid commit or provider-bypass defect is demonstrated here; the
focused protocol tests give counterevidence to those claims.

**Provisional findings from the sheet review** (the coordinator will renumber them):

- **`emit_pipeline_proposal-N1` — demonstrated (scripted planner run plus
  two-gate mutation, §7).** The missing-source branch
  ([L4515](../../../../src/elspeth/web/composer/pipeline_planner.py#L4515)) cannot be
  reached from provider input. Its helper's docstring says "Both `source` and
  `sources` are optional on the terminal schema"
  ([L2020](../../../../src/elspeth/web/composer/pipeline_planner.py#L2020)), but the
  measured schema has a root `oneOf` that rejects both omission and duplication
  first, and `SetPipelineArgumentsModel` rejects them again. The tests confirm this.
  The sourceless fixture expects `canonical_schema`/`oneOf`, and the hatch test
  maps `missing_source` to `expected_code = "canonical_schema"`
  ([tests](../../../../tests/unit/web/composer/test_pipeline_planner.py#L4287)).
  The test named `..._gets_bounded_no_source_repair` asserts schema feedback, not
  `no_source_configured`. No incorrect behaviour reaches the planner. The
  documentation and branch are stale. The mutation run shows the branch is
  reachable only when both gates are removed. With the precheck `oneOf` removed,
  Pydantic still answers `canonical_schema` (rule `value_error`). With the
  source-xor validator also neutered, the branch fires `no_source_configured`.
  The dead branch's feedback carries an `explanation` and a `suggested_fix`. The
  live path gives the model only `{"path":"pipeline","rule":"oneOf"}`, the same
  for a missing source and for both sources (enhancements 1 and 4).
- **`emit_pipeline_proposal-N2` — demonstrated (scripted planner run, §7), intent
  unverified.** The same
  model mistake, a terminal that is not alone, is repairable with a discovery
  sibling but fatal with a recovery sibling. The recovery check at
  [L4352](../../../../src/elspeth/web/composer/pipeline_planner.py#L4352) runs before
  the terminal check at [L4401](../../../../src/elspeth/web/composer/pipeline_planner.py#L4401).
  The run ended terminal + `handoff_to_user` (either order) and terminal +
  `restore_parked_graph` with `MALFORMED_RESPONSE` "recovery control must be the only
  tool call". Each made one provider request, with no dispatch and no candidate
  construction. The same happened with an escape hatch configured. Terminal +
  `list_sources` was repaired by `TERMINAL_CALL_NOT_ALONE`. No repository test
  covers terminal plus recovery.
  `test_mixed_recovery_batch_is_rejected_before_dispatch` uses `list_sources` with
  `handoff_to_user`. The per-request instruction says only that "a mixed terminal batch
  executes no calls" ([L1936](../../../../src/elspeth/web/composer/pipeline_planner.py#L1936)).
- **`emit_pipeline_proposal-N3` — intended (test-pinned), undisclosed to the
  model.** Two terminals in one turn, and terminal arguments that are not a
  strict JSON object below the output cap, end planning as `MALFORMED_RESPONSE`
  without repair or hatch. The tests are
  `test_two_terminal_calls_in_one_turn_stay_malformed` and
  `test_unparseable_arguments_below_cap_without_length_finish_stay_fatal`
  ([tests](../../../../tests/unit/web/composer/test_pipeline_planner_protocol_rejections.py#L257)).
  The tool description gives no hint that these mistakes are unrecoverable.
- **`emit_pipeline_proposal-N4` — demonstrated effect-disclosure gap (scripted
  run for the pre-approval write; controlled static trace for retention, §7).**
  Admitting a candidate with `inline_blob` writes a durable session blob before
  user approval (§2). The run found one `ready` row with `created_by="assistant"`
  and its file, and zero proposal rows. A candidate that is not admitted leaves
  no residue. A planner proposal can also be auto-committed without
  per-proposal review under `auto_commit` with a green preflight. Neither effect is
  stated in the tool description or the skills. No rejection or settlement path
  releases the blob, so it outlives a refused proposal. The only blob-row deletes
  found are explicit `delete_blob`, fork cleanup and pending-reservation cleanup.
  Session-deletion cascade was not traced. Runtime reject-then-query was not exercised.
  Whether retention is intended is a maintainer decision.
- **`emit_pipeline_proposal-N5` — intended (test-pinned), audit consequence.** On a
  `PlannerHandoff`, staging passes `private_candidates=()`
  ([L1191](../../../../src/elspeth/web/composer/planning_application.py#L1191);
  `test_application_handoff_defers_audit_and_omits_optional_candidate_capture`).
  Rejected terminal candidates submitted before a handoff are therefore not retained
  as private evidence. The decline, prompt-guard, error and success exits do retain
  them. The asymmetry is deliberate ("optional") but undocumented in this sheet's
  audit claims.
- **`emit_pipeline_proposal-N6` — source-established usability limitation.** The
  model has no channel to explain its design to the user. Terminal-turn prose
  is discarded and the user receives a fixed server message (§2). Skill text that
  asks for a "stage reply" (§5.1) cannot be satisfied on this surface.
- **`emit_pipeline_proposal-N7` — hypothesis.** The `emit_pipeline_proposal` arms in
  [visible_tool_event.py](../../../../src/elspeth/web/composer/visible_tool_event.py#L271)
  (historical replay refusal) have no current producer; they may be defensive or
  may cover rows written by an earlier version. This is unverified.
- **`emit_pipeline_proposal-N8` — source-established schema gap, consequence
  unverified.** Named `sources` entries exclude `blob_id`/`inline_blob`
  ([model](../../../../src/elspeth/web/composer/redaction.py#L1777)), and the planner
  palette has no blob-binding mutation. On the empty-state planner route, a
  multi-source proposal therefore has no evident way to bind uploaded blobs. Whether
  options such as a path can substitute was not traced. This belongs equally to the
  `set_pipeline` sheet.

### 5.1 Guidance parity

The planner's system message is the same rendered `pipeline_composer.md` plus
capability core that the freeform loop receives:
`rendered_skill = self._composer_skill_text`
([planning_application.py](../../../../src/elspeth/web/composer/planning_application.py#L1077)).
The manifest check only requires the core as a prefix
([capability_skill.py](../../../../src/elspeth/web/composer/capability_skill.py#L205)).
The only planner-specific guidance is the per-request `instruction`
([L1936](../../../../src/elspeth/web/composer/pipeline_planner.py#L1936)), the
prose-nudge and hatch notices, and the tool description.

- **Name coverage.** The tool name occurs 0 times in both skill files. As a grep
  control, `set_pipeline` occurs 31 and 2 times. The capability core is
  deliberately surface-neutral ("your surface's terminal authoring tool",
  [pipeline_capabilities.md L41](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L41))
  and says the interaction rules name the tool. `pipeline_composer.md` never does,
  so only the per-request instruction names it. The autogenerated Tool Inventory
  ([pipeline_composer.md L339](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L339))
  lists the freeform build/edit/review tools and omits the terminal.
- **Guidance that points at tools the planner does not have.** The planner handles
  every new build, but the edit table says "Create a new pipeline → `set_pipeline`,
  or bounded construction batches"
  ([L175](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L175)).
  Complex New Pipeline Batching prescribes `set_source`/`set_output`/`upsert_node`/`upsert_edge`
  ([L189](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L189)).
  None of these is in the planner palette (measured: 20 discovery, 2 recovery, 1
  terminal), and obeying them ends planning with `DISCOVERY_ONLY` (§4). The code
  already recognises this hazard for authoring aids
  ([planner_authoring_aids.py](../../../../src/elspeth/web/composer/planner_authoring_aids.py#L947))
  but the composer skill is unscoped. Source-established; how often models actually
  follow this text is unmeasured.
- **Unsatisfiable completion criteria.** "Done means … a valid preview"
  ([L51](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L51)) and the
  termination checklist ("matching request_interpretation_review call",
  [L1330](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1330);
  "mentioned in my reply", [L1327](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1327))
  cannot be met on this surface. The proposal is not in session state while the
  planner runs, so `preview_pipeline` previews the empty state, and no review tool
  or reply exists. Partial carve-outs exist at
  [L127](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L127),
  [L149](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L149) and
  [pipeline_capabilities.md L156](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L156),
  and the schema's `options` descriptions carry the proposal-only review rule. The
  checklist carries no carve-out.
- **Wrong statement.** pipeline_capabilities.md says "Only the provider planner
  authors the proposal through ordinary node/edge tools, then preview and diff it"
  ([L416](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L416)).
  The provider planner has no node/edge tools, and chain compaction applies to an
  existing pipeline, which never routes to the planner.
- **Undeliverable instruction.** "State the gate's condition and every route's
  destination in your stage reply"
  ([pipeline_capabilities.md L153](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L153),
  [pipeline_composer.md L959](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L959))
  cannot be followed here (N6).
- **Schema-description references to absent tools.** `source.blob_id` ("resolved
  exactly like set_source_from_blob"), source `options` ("Bind blobs through
  set_source_from_blob, set_source_from_blobs …") and `inline_blob` ("Fields
  mirror create_blob") name tools that are not in the planner palette. The `options`
  text does carry an explicit proposal-only clause, so the risk is low.
- **Description omissions.** The declaration description (alone, full-document
  repair) is accurate as far as it goes. It does not say that a second terminal,
  a recovery sibling or malformed JSON is fatal (N2, N3), that the reply is a fixed
  server message (N6), or that inline sources and auto-commit have effects (N4).
  The root `pipeline` property has no description.
- **Accurate parity.** The instruction, the capability core
  ([L46–48](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L46))
  and the declaration agree that the terminal must be alone and that a rejected
  proposal is repaired and resubmitted as a full document. Handoff/restore
  availability in the planner and hatch
  ([pipeline_composer.md L66](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L66))
  matches the palette.

## 6. Structural remediation

Retain the current planner boundary and provider authorship. Give the proposal
flow a shared owned lifecycle contract: authored candidate → admitted effective
candidate → staged proposal → approved/committed or refused/stale. The planner
and freeform `set_pipeline` should consume the same candidate admission and
receipt vocabulary while preserving their legitimate exposure/wire differences.
Keep execution readiness separate from model completion and proposal acceptance.

Owners are Composer orchestration for budgets/terminal decisions, candidate
admission for validation/required controls, plugins for configuration semantics,
and Sessions for atomic publication and evidence. The related tools are
`set_pipeline`, incremental graph edits, `preview_pipeline`, advisor/interpretation
review and recovery terminals. Strengthen current seams rather than replacing
them with an unbounded agent framework.

Keep existing tool names and omission/null semantics. Any stored proposal,
review or receipt envelope change needs explicit compatibility handling; restored
0.8.9 state is not disposable. Acceptance must prove valid end-to-end staging,
schema and semantic rejection without publication, no sibling effects on mixed
terminal calls, truncation rejection before finalization, unchanged deadline
through repair, one audit per physical provider attempt, stale approval refusal,
and required-control/provider-authority provenance on both entry paths.

### 6.1 Simple enhancements

None of these is implemented. Each is bounded and keeps provider authorship.

1. **Remove or correct the dead missing-source branch (N1).** Where:
   [L2020](../../../../src/elspeth/web/composer/pipeline_planner.py#L2020),
   [L4515](../../../../src/elspeth/web/composer/pipeline_planner.py#L4515). Why: the
   docstring contradicts the measured `oneOf`, and the branch implies a
   `no_source_configured` repair that never occurs. Acceptance: the sourceless and
   both-source fixtures still get `canonical_schema`/`oneOf`, and no test or
   comment claims a schema-legal sourceless candidate. Rename the misnamed
   `test_freeform_sources_omitted_candidate_gets_bounded_no_source_repair`.
2. **Make terminal/recovery mixing consistent with terminal/discovery mixing,
   or disclose it (N2).** Where:
   [L4352](../../../../src/elspeth/web/composer/pipeline_planner.py#L4352). Why: the
   same "not alone" mistake has opposite consequences. Acceptance: a scripted
   `emit_pipeline_proposal` + `handoff_to_user` turn either receives
   `TERMINAL_CALL_NOT_ALONE` with no dispatch and one repair unit spent, or the
   instruction and description state that it is fatal. A test pins whichever
   behaviour is chosen.
3. **Name root violations in schema feedback.** Where:
   [L4476](../../../../src/elspeth/web/composer/pipeline_planner.py#L4476). Why: an extra
   root key or a missing `pipeline` currently yields bare `canonical_schema` with no
   `schema_violations`, unlike the inner precheck. Acceptance: `{"pipeline": …, "x": 1}`
   feedback carries a value-free `additionalProperties`/`required` violation at path
   `""` or `pipeline`.
4. **Distinguish neither-source from both-source in the `oneOf` violation.** Why:
   both give `{"path":"pipeline","rule":"oneOf"}` (measured), so the model cannot
   tell what to fix. Acceptance: a value-free `detail` such as `source_xor_sources`
   with `missing` or `both`, carrying no authored values.
5. **Drop the impossible `payload.pipeline is None` arm.** Where:
   [L4486](../../../../src/elspeth/web/composer/pipeline_planner.py#L4486). Why:
   `_PlannerTerminalPayload` is `strict=True` with `pipeline: dict[str, Any]`
   ([L1569](../../../../src/elspeth/web/composer/pipeline_planner.py#L1569)).
   Acceptance: mypy and the existing tests pass unchanged.
6. **Complete the declaration description.** Where:
   [L1591](../../../../src/elspeth/web/composer/pipeline_planner.py#L1591). Add a root
   `pipeline` description. State that a second terminal, a recovery sibling or
   non-JSON arguments end planning (N3), that turn prose is not shown to the user,
   and that step `description` and `metadata.description` reach reviewers (N6).
   Acceptance: the boot-probe and capability-manifest hash tests are updated
   deliberately, and the description stays true against the planner protocol tests.
7. **Scope the composer skill's build guidance to the palette (§5.1).** Where:
   pipeline_composer.md L51, L175, L189, L1327, L1330, L959, and
   pipeline_capabilities.md L153, L416. Why: unconditional instructions to call
   tools absent from the planner end it as `DISCOVERY_ONLY`. Acceptance: each rule
   uses the existing "where your palette carries …" phrasing, and a parity test
   rejects any planner-palette-absent tool named in an unconditional rule. Follow
   the authoring-aids precedent at
   [test_planner_authoring_aids.py](../../../../tests/unit/web/composer/test_planner_authoring_aids.py#L2369).
8. **Add the missing terminal + recovery test.** Acceptance: one test asserting the
   chosen N2 behaviour, with no discovery dispatch and no candidate construction.

## 7. Evidence, confidence, risk and limits

The parent assessment executed a six-file recovery/planner selection that includes
[planner protocol rejections](../../../../tests/unit/web/composer/test_pipeline_planner_protocol_rejections.py),
[strict dialect](../../../../tests/unit/web/composer/test_planner_strict_dialect.py),
and [planner recovery](../../../../tests/unit/web/composer/test_planner_graph_recovery.py):
**86 passed, exit 0** for the whole selection ([evidence](../evidence.md)), not
for these three files alone. Assertions cover invalid
batches without dispatch, classified budgets, discarded length-stopped calls,
deadline preservation, physical-attempt audits and terminal non-strict stamps.
The review located but did not independently execute all proposal-lifecycle and
runtime acceptance tests. No live provider, PostgreSQL contention or browser
execution was exercised.

The original sheet review ran no tests. It used two read-only introspections with both
source roots on `PYTHONPATH`, and `elspeth.__file__` resolved into this checkout.
(1) `Draft202012Validator(canonical_set_pipeline_schema())`: the known-good
single-source document gave no errors. Omitting both sources and supplying both
each gave only `oneOf` at `['oneOf']`, and `source: null` gave `type`.
(2) `planner_tool_definitions(dialect=…)`: 23 names on both dialects, ending
`handoff_to_user`, `restore_parked_graph`, `emit_pipeline_proposal`. The terminal
carries `strict: False` on `openai_strict` and no `strict` key on `none`. Neither
`set_pipeline` nor `request_interpretation_review` is in the discovery set.

Source/local protocol confidence is high; a claim of
full end-to-end readiness would be unsupported. Unifying lifecycle receipts has
high integrity/compatibility risk if authored identity, required controls,
fencing or review evidence is weakened; proceed by bounded contract slices.

### 7.1 Probe runs (2026-11-09)

Production tree `3b28d537b`. `$WT` is the worktree and
`$L` is `.claude/lanes/composer-sheet-review-20261109/probes/emit_pipeline_proposal`
in the main checkout. The ad-hoc probe files live in `$L`, not in
`tests/`. They import the planner unit-test helpers and load the four test conftests as
plugins: `pytest $L/<probe>.py -c $WT/pyproject.toml --rootdir=$WT -p tests.conftest
-p tests.unit.conftest -p tests.unit.web.conftest -p tests.unit.web.composer.conftest -n 0 -s`.
Every run printed `elspeth.__file__` and `pipeline_planner.__file__` under the tree
being measured. Each mutant ran against its own copy. For mutA and mutB, `$WT/src` was replaced by
`$L/<variant>/src` in both `PYTHONPATH` and `-o pythonpath`, and `redaction.__file__`
was printed as well. All three resolved into the variant copy. The worktree's named
test ids ran in the brief's fixed pytest form.

| Probe | Finding | Outcome | Exit | Observed |
|---|---|---|---|---|
| `$L/test_probe_n2.py` | N2 | confirmed | 0 | Controls discriminate. (a) `list_sources`+`handoff_to_user` → `MALFORMED_RESPONSE`. (b) `list_sources`+terminal → `TERMINAL_CALL_NOT_ALONE` on both calls, then accepted with `repair_count=1`. Probe: terminal+`handoff_to_user` (both orders), terminal+`restore_parked_graph`, and terminal+handoff with a hatch configured each gave `MALFORMED_RESPONSE` "recovery control must be the only tool call". Each made 1 provider request, with dispatch and `build_set_pipeline_candidate` called 0 times. One private candidate `received_not_dispatched` and one attempt `response/malformed_response/terminal`. |
| 3 named ids in [test_pipeline_planner.py](../../../../tests/unit/web/composer/test_pipeline_planner.py) (`…_bounded_no_source_repair`, `…_each_get_schema_feedback`, `test_escape_hatch_retains_terminal_candidate_across_pre_custody_rejections`) | N1 control | confirmed | 0 | 4 passed (3 ids, one parametrized). |
| `$L/test_probe_n1.py` on baseline, mutA, mutB | N1 mutation | confirmed | 0 / 0 / 0 (probe); 0 / 1 / 1 (controls) | mutA is a lane copy of `src/` with `oneOf` stripped at the planner precheck call site. mutB is mutA plus the `_exactly_one_source_configuration` raise neutered. Missing source, by tree: baseline `canonical_schema` `rule: oneOf`; mutA `canonical_schema` `rule: value_error`; mutB `no_source_configured` with explanation and suggested fix. Both sources, by tree: baseline `oneOf`; mutA `value_error`; mutB `source_binding_ambiguous`. Controls: mutA 2 failed and 2 passed; mutB 3 failed and 1 passed. |
| `$L/test_probe_n4.py` plus id `test_invalid_inline_draft_exhaustion_leaves_zero_pre_custody_residue` | N4 pre-approval write | confirmed | 0 / 0 | Admitted inline candidate: blob rows went 0 → 1, `ready`, `created_by="assistant"`, id equal to the bound `source.blob_id`, file on disk, and 0 `composition_proposals` rows. Negative control: a non-admitted inline candidate leaves 0 rows and 0 files. |
| `$L/n4-static-trace.log` (grep/awk, controlled) | N4 release on reject/stale | confirmed (static) | 0 | Known-positive: the custody write sites are found. Every `blobs_table` delete in `src/elspeth/web` traces to one of three paths. (1) Explicit deletion: `delete_blob` → `_delete_blob_with_ledger` → `_commit_blob_deletion_plan` → `commit_blob_deletion`. (2) Pending-row cleanup inside `_persist_fenced_blob_record`: `discard_pending_blob` on failed create, and `_reconcile_abandoned_creations_locked` → `retire_abandoned_blob_reservation`. `_discard_nonidempotent_reservation` deletes only `status == "pending"`. (3) Fork cleanup from the fork route. The rejection modules: `pipeline_rejection_custody.py` has 0 `blob` mentions and `pipeline_settlement.py` has 1 (the storage-ceiling setting). The reject handler body in `proposals.py` (L724–860) has 0. That file's other 20 mentions are on the accept path, which shows the grep is live. The `SessionServiceImpl` rejection methods touch only `composition_proposals_table`, `proposal_events_table` and `sessions_table`. A runtime reject-then-query was not exercised. |

## Runtime argument schema and measured exposure

This async/terminal control has no synchronous declaration; its specialized owner is traced above.

| Surface | Exposed |
|---|---|
| Canonical emitter | no |
| Freeform loop | no |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "pipeline": {
      "type": "object",
      "properties": {
        "source": {
          "type": "object",
          "additionalProperties": false,
          "description": "Source configuration. Use blob_id to bind an already uploaded session blob, or inline_blob to materialize user-provided literal data atomically with the pipeline. null carries NO meaning — it is not 'keep the existing source'. Every call must supply source or sources as a real object: on a full rebuild, re-supply the complete existing source block, including blob_id only when that block carries one.",
          "properties": {
            "plugin": {
              "type": "string"
            },
            "blob_id": {
              "type": [
                "string",
                "null"
              ],
              "description": "Existing ready session blob ID to bind as this source (resolved exactly like set_source_from_blob)."
            },
            "options": {
              "type": "object",
              "description": "Plugin-specific source config. Required by most file/data sources. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
            },
            "on_success": {
              "type": "string",
              "description": "Connection-name string the source PUBLISHES. Some downstream consumer (node 'input' or output 'sink_name') MUST equal this. Connections match by string, not by node id.",
              "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
              "maxLength": 64,
              "not": {
                "anyOf": [
                  {
                    "enum": [
                      "continue",
                      "fork",
                      "on_success"
                    ]
                  },
                  {
                    "type": "string",
                    "pattern": "^__"
                  }
                ]
              }
            },
            "on_validation_failure": {
              "type": "string",
              "description": "Required explicit non-empty source-validation failure choice: 'discard' or an eligible named sink. Any other value, including 'quarantine', must match a configured output/sink name. Full replacement does not reuse prior choices; null/empty is invalid."
            },
            "description": {
              "type": [
                "string",
                "null"
              ],
              "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
            },
            "inline_blob": {
              "type": [
                "object",
                "null"
              ],
              "additionalProperties": false,
              "description": "Inline source content to create as a session blob before binding. Fields mirror create_blob.",
              "properties": {
                "filename": {
                  "type": "string"
                },
                "mime_type": {
                  "type": "string",
                  "enum": [
                    "application/json",
                    "application/jsonl",
                    "application/x-jsonlines",
                    "text/csv",
                    "text/jsonl",
                    "text/plain"
                  ]
                },
                "content": {
                  "type": "string",
                  "maxLength": 262144
                },
                "description": {
                  "type": [
                    "string",
                    "null"
                  ]
                }
              },
              "required": [
                "filename",
                "mime_type",
                "content"
              ]
            }
          },
          "required": [
            "plugin",
            "on_success",
            "on_validation_failure"
          ]
        },
        "sources": {
          "type": "object",
          "description": "Named source roots keyed by stable source name; use instead of source for multi-source pipelines. Values share source's shape minus blob_id/inline_blob. null carries NO meaning; every call must supply source or sources as a real object.",
          "additionalProperties": {
            "type": "object",
            "additionalProperties": false,
            "properties": {
              "plugin": {
                "type": "string"
              },
              "options": {
                "type": "object",
                "description": "Plugin-specific source config. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
              },
              "on_success": {
                "type": "string",
                "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                "maxLength": 64,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "on_validation_failure": {
                "type": "string",
                "description": "Required explicit non-empty source-validation failure choice: 'discard' or an eligible named sink. Any other value, including 'quarantine', must match a configured output/sink name. Full replacement does not reuse prior choices; null/empty is invalid."
              },
              "description": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
              }
            },
            "required": [
              "plugin",
              "on_success",
              "on_validation_failure"
            ]
          },
          "propertyNames": {
            "pattern": "^[a-z][a-z0-9_-]*$",
            "maxLength": 38,
            "not": {
              "enum": [
                "continue",
                "fork",
                "on_success"
              ]
            }
          }
        },
        "nodes": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": {
                "type": "string",
                "pattern": "^[a-zA-Z][a-zA-Z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "enum": [
                    "continue",
                    "fork",
                    "on_success"
                  ]
                }
              },
              "node_type": {
                "type": "string"
              },
              "plugin": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "input": {
                "type": "string",
                "description": "Upstream published connection this node consumes, normally an on_success or gate route. Valid implicit aggregation/queue/coalesce publications can equal the publisher's node id. For coalesce/row_union use their branch/input rules. Error routes cannot feed arbitrary processing nodes; only a permitted enclosing bound-region closer is eligible.",
                "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                "maxLength": 64,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "on_success": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "Connection-name string this node PUBLISHES; some downstream input/sink_name MUST equal it. Omit for gates (they route via condition+routes). A coalesce publishes under its own node id, and its on_success, when set, may ONLY name a sink. A row_union's on_success must name a downstream processing connection, never a sink.",
                "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                "maxLength": 64,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "on_error": {
                "type": [
                  "string",
                  "null"
                ],
                "minLength": 1,
                "description": "Node-level error policy (transform/aggregation/gate): 'discard' or a declared sink name. A transform or gate inside a valid bound region may instead divert loss to that enclosing region's closer; aggregations cannot. Arbitrary processing inputs are forbidden. Transforms/aggregations require an explicit non-empty choice; full replacement does not reuse prior choices. For a gate it covers row expression-evaluation errors and is authored on the node, never as an edge; omit it to preserve fail-fast behavior. A failed aggregation batch applies this policy to every buffered row; discard records quarantine outcomes.",
                "pattern": "^[a-z0-9_][a-z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "options": {
                "type": "object",
                "description": "Plugin-specific node config. Row-column options use the field name carried by upstream rows, normally a normalized header or source field_mapping target. Runtime-owned LLM option fields are not settable: approved_prompt_artifact_hash. Omit them; ELSPETH re-derives them during review reconciliation or execution. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
              },
              "condition": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "routes": {
                "type": [
                  "object",
                  "null"
                ],
                "additionalProperties": {
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                },
                "description": "Gate route mapping to sink names, downstream connection names, 'fork', or 'discard' for an audited terminal drop.",
                "propertyNames": {
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                }
              },
              "fork_to": {
                "type": [
                  "array",
                  "null"
                ],
                "items": {
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                }
              },
              "branches": {
                "type": [
                  "array",
                  "object",
                  "null"
                ],
                "items": {
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                },
                "additionalProperties": {
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                },
                "description": "Branch inputs for coalesce or row_union. For row_union, every branches value is a real consumed input connection; input must repeat the first value only as an adapter placeholder.",
                "propertyNames": {
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                }
              },
              "policy": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "merge": {
                "type": [
                  "string",
                  "null"
                ]
              },
              "trigger": {
                "type": [
                  "object",
                  "null"
                ],
                "properties": {
                  "count": {
                    "type": [
                      "integer",
                      "null"
                    ]
                  },
                  "timeout_seconds": {
                    "type": [
                      "number",
                      "null"
                    ]
                  },
                  "condition": {
                    "type": [
                      "string",
                      "null"
                    ]
                  }
                },
                "additionalProperties": false
              },
              "output_mode": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "Aggregation mode: transform (default), or passthrough only when the plugin's catalogue aggregation_output_modes includes it; passthrough requires one emitted row per buffered row."
              },
              "expected_output_count": {
                "type": [
                  "integer",
                  "null"
                ],
                "description": "Expected aggregation output row count for output_mode='transform' (the default); omit for 'passthrough'. Also omit when output count depends on group_by distinct values."
              },
              "timeout_seconds": {
                "type": [
                  "number",
                  "null"
                ],
                "exclusiveMinimum": 0,
                "description": "Optional finite positive timeout for structural barrier nodes (coalesce or row_union)."
              },
              "description": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
              },
              "scope_name": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "Scope identifier for the EXPAND group this collector closes (collector only)."
              },
              "scope_opener": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "Node id of the multi-row transform that opens the collector's EXPAND group (collector only)."
              },
              "scope_policy": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "Group arrival policy (collector only): 'require_all' or 'best_effort'. REQUIRED for collectors — there is no default; the author decides whether a lost member fails the group."
              }
            },
            "required": [
              "id",
              "node_type",
              "input"
            ],
            "additionalProperties": false
          },
          "description": "Node specs. A queue node is a structural fan-in point: node_type='queue', id == input == the shared connection name, plugin and every routing field omitted, options at most an optional description; multiple producers may publish that name precisely because the queue is declared. A row_union is plugin-free require_all fork reconvergence: at least two ordered branches, input set to the first branch connection, on_success published to downstream processing (not a sink), no options/routing/policy/merge fields, optional timeout_seconds. A collector closes a declared EXPAND scope: node_type='collector', a batch-aware plugin, and the scope binding authored on the node — scope_name, scope_opener (the multi-row transform that opens the group), and scope_policy ('require_all' or 'best_effort', no default)."
        },
        "edges": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": {
                "type": "string"
              },
              "from_node": {
                "type": "string"
              },
              "to_node": {
                "type": "string"
              },
              "edge_type": {
                "type": "string",
                "enum": [
                  "on_success",
                  "on_error",
                  "route_true",
                  "route_false",
                  "fork"
                ]
              },
              "label": {
                "type": [
                  "string",
                  "null"
                ]
              }
            },
            "required": [
              "id",
              "from_node",
              "to_node",
              "edge_type"
            ],
            "additionalProperties": false
          },
          "description": "Edge specs. edge_type='on_error' is supported for transform/aggregation sink wiring only; a gate's evaluation-error policy is the node's on_error field. Advisory display records: node connection fields are the authoritative runtime wiring, and [] is complete for a fully-wired topology."
        },
        "outputs": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "sink_name": {
                "type": "string",
                "description": "Sink identifier and routing target: name it from an upstream success/gate route or an eligible source-validation, processing-error or sink-write-failure policy. It need not match a node id.",
                "pattern": "^[a-z0-9_][a-z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "plugin": {
                "type": "string"
              },
              "options": {
                "type": "object",
                "description": "Plugin-specific sink config. The interpretation_requirements option root is not settable on outputs. Stage an authorable review on its source or node instead."
              },
              "on_write_failure": {
                "type": "string",
                "description": "Required explicit non-empty failure choice; full replacement does not reuse prior choices.",
                "pattern": "^[a-z0-9_][a-z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "description": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
              }
            },
            "required": [
              "sink_name",
              "plugin",
              "on_write_failure"
            ],
            "additionalProperties": false,
            "examples": [
              {
                "sink_name": "results",
                "plugin": "json",
                "options": {
                  "path": "outputs/results.json",
                  "schema": {
                    "mode": "observed"
                  },
                  "mode": "write",
                  "collision_policy": "auto_increment"
                },
                "on_write_failure": "discard"
              }
            ]
          },
          "description": "Output specs. For csv/document/json/text file sinks in runnable web pipelines, options must include path, schema, explicit mode ('write' or 'append'), and explicit collision_policy. Sink schema.fields and custom header keys use carried row names, normally normalized source headers or mapping targets."
        },
        "metadata": {
          "type": [
            "object",
            "null"
          ],
          "additionalProperties": false,
          "description": "Pipeline metadata: {name?, description?}",
          "properties": {
            "name": {
              "type": [
                "string",
                "null"
              ]
            },
            "description": {
              "type": [
                "string",
                "null"
              ]
            }
          }
        }
      },
      "oneOf": [
        {
          "required": [
            "source"
          ]
        },
        {
          "required": [
            "sources"
          ]
        }
      ],
      "required": [
        "nodes",
        "edges",
        "outputs"
      ],
      "additionalProperties": false
    }
  },
  "required": [
    "pipeline"
  ],
  "additionalProperties": false
}
```

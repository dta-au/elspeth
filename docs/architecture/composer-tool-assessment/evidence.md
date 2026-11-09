# Evidence, reproductions and assessment limits

All source conclusions bind to `3b28d537be98234da8135c418bf185a998c18b68`
(restored 0.8.9). This file records what was actually exercised and provides
repeatable controlled probes. It is not a full-suite or release-acceptance report.
The [inventory](inventory.md) records the separate live tool-name measurement.

## Environment and authority

The initial checkout was clean and had no `.venv`. The parent created a local
environment using the existing lock, without refreshing dependency files:

```bash
uv sync --frozen --extra dev --extra webui --extra llm --extra mcp
uv sync --frozen --extra dev --extra webui --extra llm --extra mcp --extra azure --extra aws
```

Both exited 0. The second command supplied declared optional Azure/AWS SDKs
after six managed-identity profile tests correctly exposed their missing local
prerequisite. The resulting interpreter used Python 3.14.3; pytest used 9.0.3.
`elspeth` and `elspeth_lints` import paths were checked against this checkout.
The source tree, tests, configuration, `pyproject.toml` and `uv.lock` were not
modified. Only assessment documents are delivery changes; environment, probes
and logs are ignored working state under `.claude/lanes/`.

No live model/provider requests, deployed service operations, migrations,
production data mutations, operator-key access, signing, issue updates, commits
or pushes were performed. Scripted-provider tests and in-memory/local SQLite
fixtures are identified as such. Package installation is environment preparation,
not evidence about provider behavior.

Each suite wrote directly to its own log and the shell read its completed exit
status. All used `-n 0`; no full suite was launched. Two initial lane invocations
used pytest's default temporary directory before the repo-local `--basetemp`
requirement was corrected. Later tests/probes used repository-local temp paths;
the original results are retained rather than silently replaced.

## Completed focused selections

The rows below are separate selections with overlap; do not sum them into a
unique test inventory or an all-Composer coverage claim.

| Selection | Raw terminal result | Exit | Interpretation |
|---|---|---:|---|
| Source/graph seven-file selection | `6 failed, 550 passed, 43 warnings in 79.19s` | 1 | Six Azure profile positives lacked optional `azure.identity`; not attributed to a production defect |
| Named Azure-profile cases after frozen SDK installation | `6 passed, 42 deselected, 43 warnings in 1.84s` | 0 | Only the six prerequisite failures rerun; remaining selected tests not needlessly repeated |
| `TestInspectSourceTool` | `25 passed, 42 warnings in 2.67s` | 0 | Bounded inspection, facts, integrity and encoding cases |
| Blob/output/secret seven-file selection | `448 passed, 43 warnings in 66.95s` | 0 | Scoped handler/dispatch/result/authority behavior |
| Generation/session/async five-file selection | `370 passed, 85 warnings in 51.20s` | 0 | Discovery, selected response contracts, pipeline mutation, review and advisor behavior |
| Generation/preview targeted classes and file | `195 passed, 43 warnings in 10.36s` | 0 | State/model/audit/assistance/explanation and preview assertions |
| Parent recovery/planner six-file selection | `86 passed, 43 warnings in 28.83s` | 0 | Qualification, scripted recovery, strict dialect and protocol rejection |
| Parent recovery persistence file | `12 passed, 42 warnings in 1.88s` | 0 | SQLite atomic settlement, refusal, stale-head protection and replay |

Warnings are predominantly deprecated `asyncio.iscoroutinefunction` use. No
warning-clean or all-supported-Python-versions claim is made.

To rerun a selection, start in the repository root, set both source roots and a
unique local temporary directory, and capture the exit explicitly. Example:

```bash
mkdir -p .claude/lanes/composer-assessment-recheck/tmp
TMPDIR="$PWD/.claude/lanes/composer-assessment-recheck/tmp" \
PYTHONPATH=src:elspeth-lints/src .venv/bin/python -m pytest \
  tests/unit/web/composer/test_graph_recovery.py \
  tests/unit/web/composer/test_planner_graph_recovery.py \
  tests/unit/web/sessions/test_graph_recovery_turn.py \
  tests/unit/web/sessions/test_graph_recovery_adversarial.py \
  tests/unit/web/composer/test_planner_strict_dialect.py \
  tests/unit/web/composer/test_pipeline_planner_protocol_rejections.py \
  -n 0 --basetemp=.claude/lanes/composer-assessment-recheck/pytest \
  > .claude/lanes/composer-assessment-recheck/tests.log 2>&1
task_exit=$?
printf 'exit=%s\n' "$task_exit"
cat .claude/lanes/composer-assessment-recheck/tests.log
```

Other exact selections were:

```text
Source/graph:
tests/unit/web/composer/test_source_rejection_contracts.py
tests/unit/web/composer/test_splice_transform_tool.py
tests/unit/web/composer/test_set_source_from_blobs.py
tests/unit/web/composer/test_explicit_failure_choices.py
tests/unit/web/composer/test_tools_mutations.py
tests/unit/web/composer/test_transform_tools_review_and_guard_parity.py
tests/unit/web/composer/test_tools_discovery.py

Prerequisite rerun (global -k applies to both selected targets):
tests/unit/web/composer/test_tools_mutations.py::TestTransformProviderConfigPathSecurity
tests/unit/web/composer/tools/test_tools_preview.py::TestInspectSourceTool
-k 'admits_a_profiled or refuses_a_private_option or cannot_add_a_private_option'

Dedicated inspection:
tests/unit/web/composer/tools/test_tools_preview.py::TestInspectSourceTool

Blob/output/secret:
tests/unit/web/composer/test_tools_blobs.py
tests/unit/web/composer/test_blob_inline_tools.py
tests/unit/web/composer/test_blob_response_contracts.py
tests/unit/web/composer/test_output_rejection_contracts.py
tests/unit/web/composer/test_secret_rejection_contracts.py
tests/unit/web/composer/test_secret_response_contracts.py
tests/unit/web/composer/test_secret_wiring_collector.py

Generation/session/async:
tests/unit/web/composer/test_tools_discovery.py
tests/unit/web/composer/test_generation_response_contracts.py
tests/unit/web/composer/tools/test_tools_pipeline.py
tests/unit/web/composer/test_request_interpretation_review_tool.py
tests/unit/web/composer/test_advisor_tool.py

Generation/preview:
tests/unit/web/composer/test_tools.py::TestGetPipelineState
tests/unit/web/composer/test_tools.py::TestExplainValidationError
tests/unit/web/composer/test_tools.py::TestListModels
tests/unit/web/composer/test_tools.py::TestGetAuditInfo
tests/unit/web/composer/test_tools.py::TestGetPluginAssistance
tests/unit/web/composer/tools/test_tools_preview.py

Parent persistence:
tests/unit/web/coordination/test_graph_recovery_persistence.py
```

## Controlled observations

### Blob discovery, identity and missing content

The probe uses actual dispatcher handlers, SQL row counts and genuine SQLite
operation authority with existing test fixtures. It creates no remote resources.

```text
stored=1  list_blobs=1  list_composer_blobs=1  oldest_visible=true
stored=50 list_blobs=50 list_composer_blobs=50 oldest_visible=true
stored=51 list_blobs=50 list_composer_blobs=50 oldest_visible=false
inline_root_keys=[blobs]
standard_result_keys=[affected_nodes,data,success,validation,version]
canonical_metadata_success=true uppercase_metadata_success=false
missing ready storage: get_blob_content.success=false read_attachment.success=false
same operation context, canonical service: exception=BlobContentMissingError
restored-file whole-read control: success=true truncated=false end_char=31
wrong expected hash control: success=false data=null
probe exit=0
```

BOS-01 is demonstrated truncation without disclosure; BOS-03 is demonstrated
metadata identity inconsistency. BOS-02 proves different fault treatment,
not that safe tools must expose raw exceptions or that corrupt bytes escaped.
Other callers sharing raw-text lookup are source-established affected paths,
not independently replayed runtime cases. The probe's initial guessed import
failed before cases ran and was corrected; only its completed controlled run
supplies evidence.

### Source/graph admission and progress

The parent independently reran the lane's actual-handler/public-dispatch probe:

```text
known_positive_splice True 5
preexisting_invalid_splice False [splice_validation_failed, no_sinks_configured,
  edge_unknown_node, transform_on_success_dangling]
empty_patch metadata same_content True versions 4 5 progress True
empty_patch node_patch same_content True versions 4 5 progress True
empty_patch source_patch same_content True versions 4 4 progress False
inspect_without_custody_authority True data_dir None operation_context None authority None
get_blob_content_without_custody_authority AuditIntegrityError
canonical_queue_control True
queue_forbidden_scope scope_name: intrinsic guard refuses; public_dispatch_success True
queue_forbidden_scope scope_opener: intrinsic guard refuses; public_dispatch_success True
queue_forbidden_scope scope_policy: intrinsic guard refuses; public_dispatch_success True
each stored queue: scope_name=None scope_opener=None scope_policy=None
parent probe exit=0
```

SG-07 demonstrates field loss before kind validation, not malformed stored queue
execution. SG-04 demonstrates internal custody admission inconsistency, not a
public HTTP bypass or reachable race. SG-03 demonstrates differing no-op/progress
semantics, not data corruption or an exactly-once promise. SG-02 demonstrates an
undisclosed splice validity precondition; changing that policy needs design.

### Generation baseline and diagnostic identity

The parent reran direct dispatcher controls independently:

```text
baseline_omitted success False total_changes None
  codes [diff_baseline_unavailable, no_source_configured, no_sinks_configured]
baseline_changed success True total_changes 1
baseline_identical success True total_changes 0
explanation unknown_node_type:
  keys [error_text, explanation, suggested_fix], error_code None
explanation LOG UNKNOWN_NODE_TYPE END:
  keys [error_code, error_text, explanation, suggested_fix], error_code unknown_node_type
explanation ZZZ_UNRECOGNIZED_QQQ:
  keys [error_text, explanation, suggested_fix], error_code None
parent probe exit=0
```

GS-01's route defect is supported by the full caller/default/handler source
chain plus these dispatcher controls. Two attempted scripted full-service probe
configurations failed at existing lifecycle harness admission before any provider
or tool ran (`Shared executor recovery callback absent`). They provide **no**
end-to-end route reproduction evidence. Keep a correctly wired service-level
regression in implementation acceptance rather than claiming it already passed.

## Independent review and dispositions

Astra first inspected source and challenged the method: count distinct exposure
surfaces; separate wire/semantic/persisted/UI contracts; separate handler success
from durable effect settlement; do not label every special path a defect.
The method was applied to the inventory and all sheets.

Astra then reviewed the parent's synthesis/plan against source and other lanes.
Its verdict was sound direction with required clarifications. All five design
corrections were adopted: preserve typed visual annotations; retain streaming
inspection memory; declare paging mutation/snapshot semantics; split unrelated
remediation dependencies; use stage-specific/discriminated outcomes referencing
existing receipts rather than a new all-optional result carrier. It separately
confirmed SG-07's narrow admission-loss framing.

Peer review crossed authorship boundaries: source/graph reviewer checked Astra's
generation/session/async sheets and the parent's controls; blob reviewer checked
source/graph sheets. Corrections include explicit standalone lifecycle details,
specific source anchors and propagation of the shared blob-ID finding to source
binding/inspection. These are ordinary document reviews, not sealed approvals or
implementation acceptance. The parent also checked decisive sources and reran
the source/generation probes.

## Document checks and final source state (initial assessment run)

This section records the initial assessment run, before the assessment was
committed (`6f9cbe4e5`) and before the 2026-11-09 review wave edited the sheets
and shared documents. Its figures and tree-state lines are historical; the
review wave's own checks are in
[§ Per-sheet review wave](#per-sheet-review-wave-2026-11-09).

The completed document instrument compared sheet names to the runtime-emitted
union, required all seven assessment sections, parsed every retained input
schema and compared it to the measured emitter, compiled the retained Python
probe blocks, and resolved relative links and source line bounds. It caught
and corrected stale line anchors and a decorated-handler source-location
projection; handler locations now unwrap the decorator before inspection.
The instrument has known-good controls and rejects an in-memory missing sheet,
extra sheet, missing target, removed required section and changed schema.

```text
documents=51
tool_sheets=46
runtime_schema_matches=46
relative_links_checked=933
errors=[]
document_validation_exit=0
```

These checks establish document/schema/link consistency, not substantive tool
correctness; source inspection, peer review and controlled probes provide that
separate evidence. Final source state was rechecked:

```text
HEAD=3b28d537be98234da8135c418bf185a998c18b68
git diff --exit-code HEAD -- src tests config pyproject.toml uv.lock: exit=0
git status: main...origin/main; only docs/architecture/composer-tool-assessment/ untracked
```

No remote fetch or incoming `.10 lite` integration was performed. The assessment
files are left uncommitted for review.

## Gaps, hypotheses and use limits

- No complete live-provider, transport, browser, deployment or PostgreSQL
  contention acceptance was attempted. Strict and non-strict local schemas
  do not establish how every provider adapter behaves remotely.
- No general plugin qualification or full-runtime completeness verdict follows
  from these tool assessments. Relevant plugin/capability paths were traced.
- A source-inspection concurrent-read race remains a hypothesis requiring an
  actual permitted lifecycle schedule. `clear_source` review parity is narrowed:
  node review hashes never cover sources, so no node-review anchor can drift;
  pending review records held outside composition state remain unverified
  (`clear_source-N4`).
- Secret-reference list/validate divergence is demonstrated as a static
  mismatch (LSR-N1), and its misattribution consequences are demonstrated in
  memory under an injected interleaving (`validate_secret_ref-N1`). A true
  concurrent race against `/api/secrets` remains unrun; neither result is an
  authorization fault.
- Whole-file reads before bounded response/preview output are source-established;
  memory/latency severity was not measured. Recovery histories above 100 parks
  were not exercised.
- Architectural cause/ownership recommendations are design judgments grounded
  in these observations. They do not authorize production changes, unsafe retry,
  data cleanup, migration or the weakening of current preserve/refuse behavior.
- Incoming `.10 lite` changes require targeted source and control refresh. The
  abandoned full `.10` patch was not used as authority.

## Per-sheet review wave (2026-11-09)

### Method

One Opus reviewer per tool sheet, 46 in all, re-traced its sheet against the
baseline source, corrected it in place and added §3.1 route inventory, §5.1
guidance parity, §6.1 simple enhancements and new sheet-scoped findings
(`<tool>-N<n>`, or the sheet's own prefix). Each wrote a report with
corrections, new findings, routes, enhancements, guidance problems,
cross-cutting notes, proposed probes and interference. Reports are untracked
local files at `.claude/lanes/composer-sheet-review-20261109/<tool>.md`
relative to the main checkout; the sheets carry the durable content.

Evidence used by reviewers:

- source tracing, with `grep` instruments controlled by a known-positive match;
- read-only Python introspection with `elspeth.__file__` verified inside the
  worktree;
- in-memory runs of handlers, `execute_tool` dispatch, MCP `_dispatch_tool`,
  planner manifest helpers and selected services on in-memory SQLite; a few
  drove the compose loop with a scripted fake LLM
  (`request_interpretation_review-N1`) or the real advisor call path with a
  stubbed provider (`request_advisor_hint-N1`).

"Demonstrated in memory" in the register means one of these runs observed the
behaviour. It is not a route-level, provider, deployed or browser observation.

### Limits

Reviewers ran no pytest, live provider, PostgreSQL, deployed service, browser
or npm. Source-established rows rest on traces whose line anchors the reviewers
checked against the baseline; their consequences (HTTP status, turn outcome)
are often marked hypothesis in the sheet. Two lanes applied short scripted
exact-string substitutions to their own sheets' link lines instead of the edit
tool (`get_expression_grammar`, `inspect_source`); several lanes recorded the
advisor tool as rate-limited. No lane reported instructions from a
non-coordinator sender.

The integrating pass re-checked the decisive cross-cutting claims by grep
against the baseline: the explicit-approve dry-run gate and its missing refused
branch (`tool_batch.py:2057-2127`); 0 occurrences of `secret_wiring_policy` in
the accept route; 0 occurrences of `admit_discovery_result` under
`composer_mcp/` against 2 in `tool_batch.py`; `DISCOVERY_NO_GAIN` only as enum,
error-code entry and comment, and no `.covers(` caller; no
`WEB_SURFACE_PROHIBITED` producer in the snapshot builder, the other snapshot
constructors copying or passing `()`, and `e7f6f1521` an ancestor of HEAD; both
`PlannerDiscoveryPolicy` constructions passing the full tool tuple; the planner
rendering the composer skill (`planning_application.py:1077`).

### Coordinator mutation probe

The only pytest run in the wave, maintainer-approved, tested
`get_expression_grammar-N4` with lane-private copies of `src/` and
`elspeth.__file__` verified to resolve into each copy:

| Variant of the grammar's Operators line | `test_expression_grammar_documents_every_operator` |
|---|---|
| Baseline | passed, exit 0 |
| `<` and `>` removed | passed, exit 0 (defect: substring membership is masked by `<=`/`>=`) |
| `==` removed (control) | failed, exit 1 |

The test therefore does not guard the operators it names. No other proposed
probe has been run.

### Test files outside the recorded selections

Reviewers cited tests that no selection above includes, read but not run:
`test_recovery_control_palettes.py` (recovery palette, mixed-batch and
malformed-head entries), `test_model_discovery_scope.py` (planner-route model
scope), `test_patch_source_options_noop.py` (the SG-03 source reference) and
`test_blob_proposal_accept.py` (blob proposal receipt replay, BOS-04).

### Proposed-but-unrun probes

One line per probe; the exact commands are in the lane reports named above
(untracked, `.claude/lanes/composer-sheet-review-20261109/<tool>.md`), several
of which embed machine-specific paths and must be transcribed, not copied.
Probes the reviewers already ran in memory are excluded. "Would show" is the
question each answers, not a predicted result.

| Sheet | Finding | Would show |
|---|---|---|
| `clear_source` | N1 | Orphan `"source"` edge at service level, and how preflight/YAML treat it |
| `clear_source` | N2 | Whether an explicit-approve clear of an absent root creates a proposal, and the accept outcome |
| `clear_source` | N5 | Whether clear+rebind loses review carry-over that a direct rebind keeps |
| `clear_source` | N4 | Whether removing a source with a pending `invented_source` review leaves an open request |
| `clear_source` | N1 (UI) | Whether GraphView silently drops an edge from a non-existent root |
| `create_blob` | N2 | Revoked identity through the tool: plugin crash vs the upload route's 503 |
| `create_blob` | N3 | Identity-cap refusal labelled "Session blob quota exceeded" and uncoded |
| `create_blob` | N4 | NUL filename through `run_tool_batch`: PLUGIN_CRASH with no durable effect |
| `create_blob` | strict `description: null` | Control that strict null decodes to omission end to end |
| `create_blob` | N1 | Whether the planner attempts `create_blob` under a scripted SDK |
| `create_blob` | N7 | Ready blob with no persisted invocation after a process death |
| `delete_blob` | N1 | End-to-end deletion of a bound blob named by a non-canonical spelling |
| `delete_blob` | N3 | Turn outcome when explicit-approve staging raises `ValueError` |
| `delete_blob` | N2 | Bound blob staged, then 422 auto-reject on accept |
| `diff_pipeline` | N1 | Planner self-diff returning zero changes end to end |
| `diff_pipeline` | N2 | MCP baseline rebased after a failed `load_session` |
| `diff_pipeline` | N5 | Diff refusal listed as resolved in the next mutation's `validation_delta` |
| `emit_pipeline_proposal` | N2 | Terminal plus recovery sibling ends with `MALFORMED_RESPONSE` and no candidate |
| `emit_pipeline_proposal` | N1 | Mutation control that the missing-source branch is unreachable while `oneOf` holds |
| `emit_pipeline_proposal` | N4 | Whether rejection or staleness releases an inline-custody blob |
| `explain_validation_error` | N1–N5 | Re-confirmation of the measured catalogue counts on a receiving branch |
| `get_audit_info` | N5 | Whether any web path can carry `landscape.export` |
| `get_audit_info` | GS-03 | Frontend pin of the corrected sentence (vitest) |
| `get_blob_content` | N1 | 262,145 vs 262,144 bytes through the handler: credential vs success |
| `get_blob_content` | N2 | Every page refusing "cannot fit" on a draft with ten dangling nodes |
| `get_blob_content` | N3 | Raw vs canonical echo for an uppercase selector |
| `get_blob_content` | N6 | ESTALE from `read_bytes` escaping as `OSError` |
| `get_blob_metadata` | BOS-03 | Every accepted spelling vs lookup, on SQLite and on PostgreSQL |
| `get_blob_metadata` | N2 | Manifest covering the uppercase key but not the canonical one |
| `get_blob_metadata` | N4 | Credential-filename refusal code and empty data |
| `get_pipeline_state` | N1, N3 | Not-found `error_code` and selector normalization through public dispatch |
| `get_pipeline_state` | N4 | Absolute vs relative blob path spelling changing the export verdict |
| `get_pipeline_state` | N2 | Persisted tool result collapsing to the unknown-key sentinel |
| `get_pipeline_state` | N9 | Whether a node payload copied from the export is accepted by `upsert_node` |
| `get_plugin_assistance` | N1 | Planner rejection feedback lacking `requirement_code` |
| `get_plugin_assistance` | N3 | Empty `issue_code` closing the planner key |
| `get_plugin_assistance` | N4 | MCP vs admitted freeform result parity |
| `get_plugin_assistance` | N5 | Profiled `llm` assistance telling the model to pin a private field |
| `get_plugin_assistance` | N1 (planner) | Whether any planner channel exposes the semantic code |
| `get_plugin_schema` | N2 | What happens when an MCP agent obeys the `operator_profiled` description |
| `get_plugin_schema` | N1 | How often the 48 KiB projection falls back to the canonical shape |
| `get_plugin_schema` | N4 | Catalogue fix contradicting the structural-node message |
| `handoff_to_user` | N3 | User-visible operation code for a mixed recovery batch |
| `handoff_to_user` | N1 | Pending `set_pipeline` proposal superseded by a reset |
| `handoff_to_user` | N2 | Safe checkpoint selecting the newer same-operation checkpoint |
| `inspect_source` | N1 | Absent storage file softened to an uncoded failure |
| `inspect_source` | N2 | Unhealed crash leftover visible to inspection; concurrent update race |
| `inspect_source` | N5, N6 | `;`-delimited and headerless files producing wrong or raw headers |
| `inspect_source` | BOS-03 | Uppercase and braced selectors on inspection |
| `inspect_source` | N7 | Two encodings sharing one planner key |
| `list_blobs` | N1 | "Gaps closed" notice after a truncated inventory |
| `list_blobs` | N4 | Tie ordering on SQLite and PostgreSQL |
| `list_blobs` | N7 | File panel omitting the 51st blob (browser) |
| `list_blobs` | N5 | Credential-shaped stored filename withholding the whole inventory |
| `list_blobs` | BOS-01 | Re-confirmation of the evidence probe on a receiving branch |
| `list_composer_blobs` | N3, N4 | Oversize and binary ready blobs listed and wireable |
| `list_composer_blobs` | N4 (validation) | Validation result for a wired binary blob |
| `list_composer_blobs` | XC-01 | Planner behaviour when the skill asks for an inline marker |
| `list_composer_blobs` | N7 | Boundary membership varying with equal `created_at` |
| `list_models` | N4 | Bundled slice and no provenance when OpenRouter is not primed |
| `list_models` | N1 | Prefixed-provider output under a live OpenRouter snapshot |
| `list_models` | N7, GS-03 | Progress headline and tooltip in a browser |
| `list_secret_refs` | LSR-N1 | Rotated `secret_key` observed end to end in a freeform turn |
| `list_sinks` | N1, N2 | The projection and emptiness findings against the full test baseline |
| `list_sinks` | N9 | Whether repeated cached discovery is bounded only by the deadline |
| `list_sinks` | N5 | Cache-hit audit row lacking `validation` |
| `list_sources` | N1 | No production policy emitting the categorical reason |
| `list_sources` | N2 | Same placement trap for sinks/transforms; which duplicate the runtime reads |
| `list_sources` | strict `{}` | Freeform strict empty call returning ARG_ERROR with repair text |
| `list_transforms` | N15 | Planner executing a repeated read with no `DISCOVERY_NO_GAIN` |
| `list_transforms` | N1 | No `plugin_not_allowed_on_web` on a running dev service |
| `list_transforms` | all-cache-hit | Provider calls vs budget counters on repeated cached reads |
| `patch_node_options` | N5 | Whether `set_pipeline` heals a corrupt digest like `upsert_node` |
| `patch_node_options` | N6 | Explicit-approve proposal for a patch on a missing node |
| `patch_node_options` | N7 | Real MCP dispatcher admitting an out-of-root `persist_directory` |
| `patch_node_options` | N10 | Partial `interpretation_requirements` effect on planner-owned rows |
| `patch_output_options` | PO-N4 | Refused preview still staged, then 422 on accept |
| `patch_output_options` | PO-N2 | Turn-level crash for a non-string sink path |
| `patch_source_options` | N2 | No-op proposal staged, then plain 409 on accept |
| `patch_source_options` | N6 | MCP stdio refusal on a path-bound source |
| `patch_source_options` | N8 | Planner feedback naming a tool the planner lacks |
| `patch_source_options` | N7 | Rendered "EXECUTABLE VERBATIM" patch reaching an uploaded source |
| `patch_source_options` | N5 | Whether the removed review event survives outside the graph |
| `preview_pipeline` | N1 | Contract violation through the real custody path |
| `preview_pipeline` | N6 | Preflight computed before rejecting extra arguments |
| `preview_pipeline` | N2 | Planner preview showing `runtime_preflight_not_run` on empty state |
| `preview_pipeline` | N7 | Preview judging the pre-proposal state without disclosure |
| `preview_pipeline` | N11 | MCP remediation text for a `blob_ref` source |
| `read_attachment` | N2 | ESTALE through `run_tool_batch`: plugin crash |
| `read_attachment` | N1 | Which operation kind each caller uses, before enforcing the allowlist |
| `read_attachment` | N3 | Non-ASCII document against a live provider |
| `remove_node` | N5 | Removing a required control and the explicit-approve rewrite |
| `remove_node` | explicit approval | Proposal round trip for `remove_node` |
| `remove_node` | N11 | Persisted-response redaction of `applied_component` |
| `remove_output` | N2 | Absent-sink proposal staged, then 422 on accept |
| `request_advisor_hint` | N8 | SPA outcome stamp per advisor status |
| `request_advisor_hint` | N2 | Transcript size vs the prompt cap |
| `request_advisor_hint` | N7 | Oversized abandonment context failing as generic `ADVISOR_ERROR` |
| `request_advisor_hint` | N6 | Anti-anchor reset suppressing the §7.7 hint |
| `request_advisor_hint` | N13 | Clarification framing after `cannot_complete` |
| `request_interpretation_review` | N1, N2, N3 | The in-memory results as focused tests with existing fixtures |
| `request_interpretation_review` | PostgreSQL | Stage/resolve contention on PostgreSQL |
| `restore_parked_graph` | N5 | Turn-level failure code for a malformed restore |
| `restore_parked_graph` | N1, N2 | Success-path supersession, and an incremental proposal left to 409 |
| `restore_parked_graph` | N3 | Uppercase park id refused |
| `set_metadata` | N6 | Compose-loop effect of a rename on progress consumers |
| `set_metadata` | N5 | Whether wire limits bound a 10M-character name |
| `set_metadata` | N2 | Reachability of the unrelated-output refusal from a loaded state |
| `set_output` | N1, N3 | Turn-level crash for `path: null`; explicit-approve staging |
| `set_pipeline` | N8 | The builder-count test turning red when it patches the bound name |
| `set_pipeline` | N1 | MCP stdio storing `/etc/passwd` as a source path |
| `set_pipeline` | N5 | Refusal channel for `source` plus `sources` by trust mode |
| `set_source` | N8 | Accept-route outcome when a preview `ToolArgumentError` recurs |
| `set_source` | N1 | Service-level PLUGIN_CRASH for `path: 123` |
| `set_source` | N1 sibling | Provider-config path type escape via `upsert_node` |
| `set_source_from_blob` | N5 | Guarantee-stamp carry-over with genuine review evidence |
| `set_source_from_blob` | N1 | Explicit-approve card omitting accept-time controls |
| `set_source_from_blob` | N2 | JSONL bound as `json` failing at runtime |
| `set_source_from_blobs` | N7 | Turn outcome when staging raises `ValueError` |
| `set_source_from_blobs` | N2 | Whether a `source_data_contract` review is reachable for `blob_rows` |
| `splice_transform` | N8 | Explicit-approve staging of a refused splice |
| `splice_transform` | N9 | Replay after required-control auto-wiring failing topology |
| `update_blob` | N7 | Turn outcome for an unknown id under explicit approval |
| `update_blob` | N1 | Turn-level `AuditIntegrityError` for an `error` blob |
| `update_blob` | N8 | Deletion-ledger row escalating on update |
| `upsert_edge` | N1 (UI) | Whether GraphView renders a `"source"` edge under named sources |
| `upsert_edge` | N9 | Approval UI omitting the routing change (browser) |
| `upsert_node` | N1 | Aggregation/coalesce cases of the validity divergence |
| `upsert_node` | N1 (turn) | Whether the runtime failure reaches the model as a repairable code |
| `upsert_node` | MCP | Out-of-root `persist_directory` admitted on MCP |
| `upsert_node` | N4 | Review status after replacing a node with a queue |
| `upsert_node` | baseline | Existing suites the §6.1 fixes touch, before implementation |
| `validate_secret_ref` | N3 | Profiled credential shadowed by a user row through `validate_pipeline` |
| `validate_secret_ref` | N1 | True concurrent race on SQLite and PostgreSQL |
| `validate_secret_ref` | batch scan | How a whole-batch credential refusal reaches the agent |
| `wire_blob_inline_ref` | N2, N3 | Explicit-approve consequence of staging an unchecked wire |
| `wire_blob_inline_ref` | N1 | The validity result under the real catalogue |
| `wire_blob_inline_ref` | N1 (turn) | Unrelated `delete_blob` failing after the wire |
| `wire_secret_ref` | N1 | Accept-route denial with the allowlist configured |
| `wire_secret_ref` | N3 | Undeclared option with a marker at runtime preflight |
| `wire_secret_ref` | explicit approval | Unchecked wire staged as `APPROVAL_REQUIRED` |

### Integration checks

A read-only checker run by the integrating pass (kept with the lane reports as
`.claude/lanes/composer-sheet-review-20261109/check_integration.py`) scanned the
five shared documents. It checks that every relative link target exists, that
`#Lnn` anchors fall within the target's line count, that heading anchors exist,
that no user-home absolute path appears, and that every cited finding ID (`<tool>-N<n>`,
the `-N<n>` shorthand after a tool ID, `LSR-`/`PO-`/`METADATA-N<n>`, and
`handoff_to_user` N<n>) occurs in the named sheet. A table pass resolves the
bare N-numbers in the probe table's Finding column against each sheet's own
prefix.

| Run | Links | Line anchors | Heading anchors | Inline IDs | Table IDs | Problems | Exit |
|---|---|---|---|---|---|---|---|
| Shared documents, final | 214 | 29 | 8 | 258 | 134 | 0 | 0 |
| Negative control: scratch copy with a missing sheet, an anchor past EOF, a missing heading, fabricated N99/N98 inline IDs for `list_sinks`, `upsert_edge` (shorthand form), `list_secret_refs` and `handoff_to_user`, three fabricated probe rows (`clear_source`, `handoff_to_user`, `set_metadata`) and one genuine probe row | 217 | 30 | 9 | 263 | 138 | 10 (each planted defect; genuine row passes) | 1 |

The probe table has 141 rows. Fourteen of them have Finding cells that are
register IDs or prose (for example `BOS-03`, `XC-01`, `strict {}`, `baseline`),
which the table pass cannot check. The first run found one wrong citation,
which was corrected before the final run. Quick-win citations to §6.1 labels (E*, item, row) were checked against
each sheet separately. Unnumbered §6.1 tables are cited by row position. The
tool sheets themselves were not re-checked by this pass.

## Reproduction source

The following scripts are retained as document code blocks so a fresh checkout
does not depend on ignored lane files. Save each block under a new repo-local
`.claude/lanes/composer-assessment-recheck/` directory and run from the repository
root with `PYTHONPATH=.:src:elspeth-lints/src .venv/bin/python <script>`.
They depend on the pinned baseline's existing test helpers. Reinspect them before
running against changed source; an assertion failing there can mean the defect
was fixed or the helper contract changed. Their assertions intentionally confirm
the observed defect alongside known-good controls, not production correctness.

### Blob inventory, identity and content reproduction

```python
"""Controlled local Composer reconnaissance; no provider calls."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select

from elspeth.core.litellm_policy import configure_litellm_pricing

configure_litellm_pricing()

from elspeth.web.blobs.service import BlobServiceImpl, content_hash
from elspeth.web.composer.tools import blobs
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer.test_tools import (
    _blob_operation,
    _empty_state,
    _mock_catalog,
    _session_engine_with_session,
    execute_tool,
)

ROOT = Path(__file__).resolve().parent


def inventory_case(number: int) -> dict:
    engine, session_id = _session_engine_with_session()
    now = datetime.now(UTC)
    ids = []
    with engine.begin() as connection:
        for index in range(number):
            blob_id = str(uuid4())
            ids.append(blob_id)
            connection.execute(blobs_table.insert().values(
                id=blob_id, session_id=session_id, filename=f"input-{index}.txt",
                mime_type="text/plain", size_bytes=1, content_hash="a" * 64,
                storage_path=str(ROOT / "probe-bytes" / "blobs" / session_id / f"{blob_id}_input-{index}.txt"),
                created_at=now + timedelta(seconds=index), created_by="user", status="ready",
            ))
    standard = execute_tool("list_blobs", {}, _empty_state(), _mock_catalog(), session_engine=engine, session_id=session_id)
    inline = execute_tool("list_composer_blobs", {}, _empty_state(), _mock_catalog(), session_engine=engine, session_id=session_id)
    canonical_metadata = execute_tool("get_blob_metadata", {"blob_id": ids[0]}, _empty_state(), _mock_catalog(), session_engine=engine, session_id=session_id)
    uppercase_metadata = execute_tool("get_blob_metadata", {"blob_id": ids[0].upper()}, _empty_state(), _mock_catalog(), session_engine=engine, session_id=session_id)
    with engine.connect() as connection:
        stored = connection.execute(select(func.count()).select_from(blobs_table)).scalar_one()
    observed = {
        "stored": stored, "list_blobs_success": standard.success, "list_blobs_returned": len(standard.data),
        "list_composer_blobs_success": inline.success, "list_composer_blobs_returned": len(inline.data["blobs"]),
        "oldest_visible": ids[0] in [item["id"] for item in standard.data],
        "inline_root_keys": sorted(inline.data),
        "standard_result_keys": sorted(standard.to_dict()),
        "canonical_metadata_success": canonical_metadata.success,
        "uppercase_metadata_success": uppercase_metadata.success,
    }
    if number <= 50:
        assert observed["list_blobs_returned"] == number and observed["oldest_visible"]
    else:
        assert observed["list_blobs_returned"] == 50 and not observed["oldest_visible"]
    engine.dispose()
    return observed


def missing_content_case() -> dict:
    engine, session_id = _session_engine_with_session()
    blob_id = str(uuid4())
    storage = ROOT / "probe-bytes" / "blobs" / session_id / f"{blob_id}_missing.txt"
    storage.parent.mkdir(parents=True)
    with engine.begin() as connection:
        connection.execute(blobs_table.insert().values(
            id=blob_id, session_id=session_id, filename="missing.txt", mime_type="text/plain",
            size_bytes=1, content_hash="a" * 64, storage_path=str(storage),
            created_at=datetime.now(UTC), created_by="user", status="ready",
        ))
    assert not storage.exists()
    observed = {}
    with _blob_operation(engine, session_id) as (authority, context):
        for name in ("get_blob_content", "read_attachment"):
            result = execute_tool(name, {"blob_id": blob_id}, _empty_state(), _mock_catalog(),
                                  data_dir=str(ROOT / "probe-bytes"), session_engine=engine, session_id=session_id,
                                  session_operation_context=context, session_operation_authority=authority)
            observed[name] = {"success": result.success, "errors": [entry.message for entry in result.validation.errors]}
        service = BlobServiceImpl(engine, ROOT / "probe-bytes", session_operation_authority=authority)
        try:
            service.read_blob_content_sync(UUID(blob_id), context)
        except Exception as error:
            observed["service"] = {"exception": type(error).__name__}
        else:
            raise AssertionError("Missing content unexpectedly succeeded")
        positive_bytes = "A complete reference document.\n".encode()
        storage.write_bytes(positive_bytes)
        with engine.begin() as connection:
            connection.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(
                size_bytes=len(positive_bytes), content_hash=content_hash(positive_bytes)))
        positive = execute_tool("read_attachment", {"blob_id": blob_id}, _empty_state(), _mock_catalog(),
                               data_dir=str(ROOT / "probe-bytes"), session_engine=engine, session_id=session_id,
                               session_operation_context=context, session_operation_authority=authority)
        assert positive.success and positive.data["content"] == positive_bytes.decode()
        mismatch = execute_tool("read_attachment", {"blob_id": blob_id,"expected_content_hash":"0"*64}, _empty_state(), _mock_catalog(),
                               data_dir=str(ROOT / "probe-bytes"), session_engine=engine, session_id=session_id,
                               session_operation_context=context, session_operation_authority=authority)
        assert not mismatch.success and mismatch.data is None
        observed["whole_read_positive_control"] = {"success":positive.success,"truncated":positive.data["truncated"],"end_char":positive.data["end_char"]}
        observed["whole_read_hash_mismatch_control"] = {"success":mismatch.success,"data":mismatch.data}
    assert observed["service"]["exception"] == "BlobContentMissingError"
    assert not observed["get_blob_content"]["success"] and not observed["read_attachment"]["success"]
    engine.dispose()
    return observed


print(json.dumps({"inventory_positive_control_1": inventory_case(1),
                  "inventory_boundary_control_50": inventory_case(50),
                  "inventory_overflow_51": inventory_case(51),
                  "missing_content": missing_content_case()}, indent=2))
```

### Source/graph admission and progress reproduction

```python
"""Local assessment probes; no production changes or provider calls."""

from pathlib import Path
from dataclasses import replace

from elspeth.core.litellm_policy import configure_litellm_pricing

configure_litellm_pricing()

from tests.unit.web.composer.test_splice_transform_tool import _state, _arguments, _context
from tests.unit.web.composer.tools.test_tools_preview import TestInspectSourceTool
from tests.unit.web.composer.test_tools import _trained_tool_context, _mock_catalog, _empty_state
from elspeth.web.composer.tools.transforms import _execute_splice_transform, _execute_set_metadata, _execute_patch_node_options
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.composer.state import queue_node_contract_error
from elspeth.web.composer.tools.sources import _execute_patch_source_options, _execute_inspect_source
from elspeth.web.composer.tools.blobs import _execute_get_blob_content
from elspeth.web.composer.discovery_cache import tool_result_mutated_composition_state
from elspeth.contracts.errors import AuditIntegrityError

s, c = _state(), _context()
valid = _execute_splice_transform(_arguments(), s, c)
assert valid.success and valid.updated_state.version == s.version + 1
print("known_positive_splice", valid.success, valid.updated_state.version)
broken = replace(s, outputs=())
refused = _execute_splice_transform(_arguments(), broken, c)
assert not refused.success and refused.updated_state is broken
print("preexisting_invalid_splice", refused.success, [(e.error_code, e.message) for e in refused.validation.errors])

for name, fn, args in [
    ("metadata", _execute_set_metadata, {"patch": {}}),
    ("node_patch", _execute_patch_node_options, {"node_id": "before", "patch": {}}),
    ("source_patch", _execute_patch_source_options, {"patch": {}}),
]:
    result = fn(args, s, c)
    before, after = s.to_dict(), result.updated_state.to_dict()
    del before["version"]
    del after["version"]
    assert result.success and before == after
    print("empty_patch", name, "same_content", before == after, "versions", s.version, result.updated_state.version,
          "progress", tool_result_mutated_composition_state(version_before=s.version, result=result))

inspection = TestInspectSourceTool()
root = Path(".claude/lanes/composer-assessment-20261109/source-graph-probe-data")
root.mkdir(parents=True, exist_ok=True)
TestInspectSourceTool._setup.__wrapped__(inspection, root.resolve())
context = _trained_tool_context(_mock_catalog(), session_engine=inspection.engine, session_id=inspection.session_id)
inspected = _execute_inspect_source({"blob_id": inspection.blob_id}, _empty_state(), context)
assert inspected.success
print("inspect_without_custody_authority", inspected.success, "data_dir", context.data_dir,
      "operation_context", context.session_operation_context, "authority", context.session_operation_authority)
try:
    _execute_get_blob_content({"blob_id": inspection.blob_id}, _empty_state(), context)
except AuditIntegrityError as error:
    print("get_blob_content_without_custody_authority", type(error).__name__, str(error))
else:
    raise AssertionError("control: raw content reader must refuse missing custody authority")
inspection.engine.dispose()

queue_args = {"id": "joined", "node_type": "queue", "input": "joined"}
queue_ok = execute_tool("upsert_node", queue_args, s, c.catalog, plugin_snapshot=c.plugin_snapshot, validate_arguments=True)
assert queue_ok.success
queue = next(node for node in queue_ok.updated_state.nodes if node.id == "joined")
assert queue_node_contract_error(queue) is None
print("canonical_queue_control", queue_ok.success)
for key, value in [("scope_name", "declared"), ("scope_opener", "before"), ("scope_policy", "require_all")]:
    invalid = replace(queue, **{key: value})
    assert queue_node_contract_error(invalid) is not None
    result = execute_tool("upsert_node", {**queue_args, key: value}, s, c.catalog, plugin_snapshot=c.plugin_snapshot, validate_arguments=True)
    admitted = next(node for node in result.updated_state.nodes if node.id == "joined")
    print("queue_forbidden_scope", key, "guard", queue_node_contract_error(invalid),
          "public_dispatch_success", result.success, "stored", admitted.scope_name, admitted.scope_opener, admitted.scope_policy)
    assert result.success and admitted.scope_name is None and admitted.scope_opener is None and admitted.scope_policy is None
```

### Generation baseline and diagnostic identity reproduction

```python
"""Controlled dispatcher observations; no providers or production writes."""

from dataclasses import replace

from elspeth.core.litellm_policy import configure_litellm_pricing

configure_litellm_pricing()

from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool

baseline = _empty_state()
changed = replace(baseline, version=baseline.version + 1,
                  metadata=replace(baseline.metadata, name="Changed assessment name"))
for label, context in (("baseline_omitted", {}), ("baseline_changed", {"baseline": baseline}),
                       ("baseline_identical", {"baseline": changed})):
    result = execute_tool("diff_pipeline", {}, changed, _mock_catalog(), **context)
    codes = [entry.error_code for entry in result.validation.errors]
    changes = result.data["total_changes"] if result.success else None
    print(label, "success", result.success, "total_changes", changes, "codes", codes)
    if label == "baseline_omitted":
        assert not result.success and "diff_baseline_unavailable" in codes
    else:
        assert result.success and changes == (1 if label == "baseline_changed" else 0)

for query in ("unknown_node_type", "LOG UNKNOWN_NODE_TYPE END", "ZZZ_UNRECOGNIZED_QQQ"):
    result = execute_tool("explain_validation_error", {"error_text": query}, baseline, _mock_catalog())
    assert result.success
    print("explanation", query, "keys", sorted(result.data), "error_code", result.data.get("error_code"))
    if query == "LOG UNKNOWN_NODE_TYPE END":
        assert result.data["error_code"] == "unknown_node_type"
    else:
        assert "error_code" not in result.data
```

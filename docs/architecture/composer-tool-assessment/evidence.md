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
  node review hashes never cover sources, so no node-review anchor can drift,
  and the probe wave refuted the remaining residual: a pending session-level
  review event for the removed root is retired as superseded on save
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

The review-wave reviewers ran no pytest, live provider, PostgreSQL, deployed
service, browser or npm. The later probe wave ran pytest, scripted compose and
planner loops, REST accept routes and jsdom vitest files
([outcomes](#probe-wave-outcomes-2026-11-09)), still with no live provider,
PostgreSQL, deployed service or real browser. Source-established rows rest on traces whose line anchors the reviewers
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

The only pytest run in the review wave, maintainer-approved, tested
`get_expression_grammar-N4` with lane-private copies of `src/` and
`elspeth.__file__` verified to resolve into each copy:

| Variant of the grammar's Operators line | `test_expression_grammar_documents_every_operator` |
|---|---|
| Baseline | passed, exit 0 |
| `<` and `>` removed | passed, exit 0 (defect: substring membership is masked by `<=`/`>=`) |
| `==` removed (control) | failed, exit 1 |

The test therefore does not guard the operators it names. Every other proposed
probe was then taken up by the probe wave below.

### Test files outside the recorded selections

Reviewers cited tests that no selection above includes, read but not run:
`test_recovery_control_palettes.py` (recovery palette, mixed-batch and
malformed-head entries), `test_model_discovery_scope.py` (planner-route model
scope), `test_patch_source_options_noop.py` (the SG-03 source reference) and
`test_blob_proposal_accept.py` (blob proposal receipt replay, BOS-04).

### Probe wave outcomes (2026-11-09)

The review wave proposed 141 probes. A second wave of one lane per sheet ran
them on 44 sheets (`get_expression_grammar`'s mutation probe had already run;
`remove_edge` had none). Each lane recorded its runs, commands and exit codes in
its sheet's §7.1; that sheet
record is the authority for every row below. Method, limits and the network
record are in [the next subsection](#probe-wave-method-and-network-record).

Each row has exactly one outcome: **confirmed** (the run shows the finding),
**refuted** (the run shows it is wrong), **inconclusive** (it ran but did not
discriminate), **not runnable here** (with the missing prerequisite), or **not
run** (one row half a lane skipped without a prerequisite). Where one proposed
row covered parts with different outcomes, it is split, so the 141 proposals
are 148 rows: `inspect_source` N2, `list_blobs` N4, `list_composer_blobs` N7,
`get_blob_metadata` BOS-03, `set_metadata` N6 and `upsert_edge` N1 (UI) and N9.
A qualifier after the outcome word says what part ran.

| Sheet | Finding | Question | Outcome | What the run showed, or what is missing | Record |
|---|---|---|---|---|---|
| `clear_source` | N1 | Orphan `"source"` edge at service level, and how preflight/YAML treat it | confirmed | Compose turn: clearing the default root kept the edge with `is_valid=True`; runtime preflight and YAML ignore edges | [§7.1](tools/clear_source.md#71-probe-runs-2026-11-09) |
| `clear_source` | N2 | Whether an explicit-approve clear of an absent root creates a proposal, and the accept outcome | confirmed | Absent-root clear staged a proposal identical to a real one; accept 422 `proposal_validation_failed`, auto-rejected (not 409) | [§7.1](tools/clear_source.md#71-probe-runs-2026-11-09) |
| `clear_source` | N5 | Whether clear+rebind loses review carry-over that a direct rebind keeps | confirmed | Carry-over half: direct rebind stays resolved, clear then rebind reopens pending. Two-proposal half not run | [§7.1](tools/clear_source.md#71-probe-runs-2026-11-09) |
| `clear_source` | N4 | Whether removing a source with a pending `invented_source` review leaves an open request | refuted | The dead-site sweep on save retires the pending event as `superseded`; nothing stays open | [§7.1](tools/clear_source.md#71-probe-runs-2026-11-09) |
| `clear_source` | N1 (UI) | Whether GraphView silently drops an edge from a non-existent root | not runnable here | Needs a real browser: vitest reaches only `src/**` and the unit suite mocks `@xyflow/react` | [§7.1](tools/clear_source.md#71-probe-runs-2026-11-09) |
| `create_blob` | N2 | Revoked identity through the tool: plugin crash vs the upload route's 503 | confirmed | Accounting outage ends the turn as `plugin_crash`, no blob; a revoked identity is refused earlier, at compose admission (trigger narrowed) | [§7.1](tools/create_blob.md#71-probe-runs-2026-11-09) |
| `create_blob` | N3 | Identity-cap refusal labelled "Session blob quota exceeded" and uncoded | confirmed | Uncoded, and byte-identical to the genuine session-limit refusal | [§7.1](tools/create_blob.md#71-probe-runs-2026-11-09) |
| `create_blob` | N4 | NUL filename through `run_tool_batch`: PLUGIN_CRASH with no durable effect | confirmed | Bare `ValueError` becomes `plugin_crash`; no blob row or file | [§7.1](tools/create_blob.md#71-probe-runs-2026-11-09) |
| `create_blob` | strict `description: null` | Control that strict null decodes to omission end to end | confirmed | `openai_strict` null reaches the handler as omission; dialect `none` refuses null | [§7.1](tools/create_blob.md#71-probe-runs-2026-11-09) |
| `create_blob` | N1 | Whether the planner attempts `create_blob` under a scripted SDK | not runnable here | Needs a live provider: a scripted SDK replays its script and cannot show what the planner chooses | [§7.1](tools/create_blob.md#71-probe-runs-2026-11-09) |
| `create_blob` | N7 | Ready blob with no persisted invocation after a process death | confirmed | Simulated in-process death (no forked kill): a ready blob whose hash appears only in `blobs`, surviving a successor turn's reconcile | [§7.1](tools/create_blob.md#71-probe-runs-2026-11-09) |
| `delete_blob` | N1 | End-to-end deletion of a bound blob named by a non-canonical spelling | confirmed | Hyphenless, braced and `urn:` spellings delete a bound blob at `execute_tool` level; `update_blob` likewise. Exit 1 is the finding's assertions | [§7.1](tools/delete_blob.md#71-probe-runs-2026-11-09) |
| `delete_blob` | N3 | Turn outcome when explicit-approve staging raises `ValueError` | confirmed | Unknown, uppercase and hyphenless ids end the compose loop with a bare `ValueError`; no invocation, row or proposal | [§7.1](tools/delete_blob.md#71-probe-runs-2026-11-09) |
| `delete_blob` | N2 | Bound blob staged, then 422 auto-reject on accept | confirmed | Staged with no handler call; accept 422, auto-rejected, bytes intact | [§7.1](tools/delete_blob.md#71-probe-runs-2026-11-09) |
| `diff_pipeline` | N1 | Planner self-diff returning zero changes end to end | confirmed | Real planner, scripted SDK: `total_changes` 0 in tool message and audit; a mutant binding another baseline gives 1 | [§7.1](tools/diff_pipeline.md#71-probe-runs-2026-11-09) |
| `diff_pipeline` | N2 | MCP baseline rebased after a failed `load_session` | confirmed | After a failed load the baseline is rebased (0 changes, against 1 without it) | [§7.1](tools/diff_pipeline.md#71-probe-runs-2026-11-09) |
| `diff_pipeline` | N5 | Diff refusal listed as resolved in the next mutation's `validation_delta` | confirmed | The diff refusal appears in the next `set_metadata`'s `resolved_errors`; a `list_models` control gives none | [§7.1](tools/diff_pipeline.md#71-probe-runs-2026-11-09) |
| `emit_pipeline_proposal` | N2 | Terminal plus recovery sibling ends with `MALFORMED_RESPONSE` and no candidate | confirmed | Either order: `MALFORMED_RESPONSE`, one provider request, no dispatch, no candidate | [§7.1](tools/emit_pipeline_proposal.md#71-probe-runs-2026-11-09) |
| `emit_pipeline_proposal` | N1 | Mutation control that the missing-source branch is unreachable while `oneOf` holds | confirmed | Baseline returns the bare `oneOf` error; a mutant without `oneOf` reaches the dead branch's codes | [§7.1](tools/emit_pipeline_proposal.md#71-probe-runs-2026-11-09) |
| `emit_pipeline_proposal` | N4 | Whether rejection or staleness releases an inline-custody blob | confirmed | Pre-approval write run (a ready blob, no proposal row); no release on rejection by static trace only | [§7.1](tools/emit_pipeline_proposal.md#71-probe-runs-2026-11-09) |
| `explain_validation_error` | N1–N5 | Re-confirmation of the measured catalogue counts on a receiving branch | confirmed | 263 registered codes, 168 catalogued, 95 not; N1 also end to end on planner and freeform | [§7.1](tools/explain_validation_error.md#71-probe-runs-2026-11-09) |
| `get_audit_info` | N5 | Whether any web path can carry `landscape.export` | confirmed | No Web writer: import then `generate_yaml` carries no `landscape` key; an injected-key mutant goes red | [§7.1](tools/get_audit_info.md#71-probe-runs-2026-11-09) |
| `get_audit_info` | GS-03 | Frontend pin of the corrected sentence (vitest) | not runnable here | Pins the corrected sentence, so it needs the fix and a new test file under `src/`; the existing file passes with the wrong sentence | [§7.1](tools/get_audit_info.md#71-probe-runs-2026-11-09) |
| `get_blob_content` | N1 | 262,145 vs 262,144 bytes through the handler: credential vs success | confirmed | 262,145 bytes refused as a credential, 262,144 read; a real credential gives the identical code and message | [§7.1](tools/get_blob_content.md#71-probe-runs-2026-11-09) |
| `get_blob_content` | N2 | Every page refusing "cannot fit" on a draft with ten dangling nodes | confirmed | Onset is 6 dangling nodes (the trained 1–7 sweep; the 0/2/5/8/9/10 sweep first refused at 8); with out-of-catalog plugins a web-authored snapshot's 7-node refusal is false | [§7.1](tools/get_blob_content.md#71-probe-runs-2026-11-09) |
| `get_blob_content` | N3 | Raw vs canonical echo for an uppercase selector | confirmed | Raw echo; coverage splits into two incomplete series. Advisor recovery and planner key not probed | [§7.1](tools/get_blob_content.md#71-probe-runs-2026-11-09) |
| `get_blob_content` | N6 | ESTALE from `read_bytes` escaping as `OSError` | confirmed | Injected ESTALE escapes as `OSError`; the service raises `BlobContentMissingError`. Real NFS not reached | [§7.1](tools/get_blob_content.md#71-probe-runs-2026-11-09) |
| `get_blob_metadata` | BOS-03 (SQLite) | Every accepted spelling vs lookup, on SQLite | confirmed | Uppercase, mixed, braced, hyphenless and `urn:` spellings each miss; canonical succeeds | [§7.1](tools/get_blob_metadata.md#71-probe-runs-2026-11-09) |
| `get_blob_metadata` | BOS-03 (PostgreSQL) | The same on PostgreSQL | not runnable here | Needs Docker for the PostgreSQL testcontainer | [§7.1](tools/get_blob_metadata.md#71-probe-runs-2026-11-09) |
| `get_blob_metadata` | N2 | Manifest covering the uppercase key but not the canonical one | confirmed | At the planner discovery seam (SQLite): the uppercase key closes as unavailable, the canonical key stays open | [§7.1](tools/get_blob_metadata.md#71-probe-runs-2026-11-09) |
| `get_blob_metadata` | N4 | Credential-filename refusal code and empty data | confirmed | Refusal coded, data empty; a mutant disabling the branch leaves the existing tests green | [§7.1](tools/get_blob_metadata.md#71-probe-runs-2026-11-09) |
| `get_pipeline_state` | N1, N3 | Not-found `error_code` and selector normalization through public dispatch | confirmed | Public freeform route: not-found has no `error_code`; case and padding variants miss; the planner mints keys for rejected inputs | [§7.1](tools/get_pipeline_state.md#71-probe-runs-2026-11-09) |
| `get_pipeline_state` | N4 | Absolute vs relative blob path spelling changing the export verdict | confirmed | Three spellings of one path are stored; only the relative one exports | [§7.1](tools/get_pipeline_state.md#71-probe-runs-2026-11-09) |
| `get_pipeline_state` | N2 | Whether the persisted record loses the served variant because the tool row collapses to the unknown-key sentinel | refuted | The `role=tool` row does collapse `data`, but a `role=audit` private evidence row keeps the served result; the audit-visibility claim is wrong | [§7.1](tools/get_pipeline_state.md#71-probe-runs-2026-11-09) |
| `get_pipeline_state` | N9 | Whether a node payload copied from the export is accepted by `upsert_node` | confirmed | The node-view echo is refused (runtime-owned `approved_prompt_artifact_hash`); without that key it is accepted | [§7.1](tools/get_pipeline_state.md#71-probe-runs-2026-11-09) |
| `get_plugin_assistance` | N1 | Planner rejection feedback lacking `requirement_code` | confirmed | The planner wire carries the violation with no `requirement_code`; freeform carries it | [§7.1](tools/get_plugin_assistance.md#71-probe-runs-2026-11-09) |
| `get_plugin_assistance` | N3 | Empty `issue_code` closing the planner key | confirmed | `""` succeeds empty and shares one key with omitted/`None` | [§7.1](tools/get_plugin_assistance.md#71-probe-runs-2026-11-09) |
| `get_plugin_assistance` | N4 | MCP vs admitted freeform result parity | confirmed | Divergence only under a nan/inf test double; real payloads byte-identical | [§7.1](tools/get_plugin_assistance.md#71-probe-runs-2026-11-09) |
| `get_plugin_assistance` | N5 | Profiled `llm` assistance telling the model to pin a private field | confirmed | The hint is served on the profiled view; a candidate obeying it is refused `plugin_options_invalid` | [§7.1](tools/get_plugin_assistance.md#71-probe-runs-2026-11-09) |
| `get_plugin_assistance` | N1 (planner) | Whether any planner channel exposes the semantic code | confirmed | Across three scripted planner requests the code appears 0 times | [§7.1](tools/get_plugin_assistance.md#71-probe-runs-2026-11-09) |
| `get_plugin_schema` | N2 | What happens when an MCP agent obeys the `operator_profiled` description | confirmed | MCP `upsert_node` with the profile field is refused `plugin_options_invalid` | [§7.1](tools/get_plugin_schema.md#71-probe-runs) |
| `get_plugin_schema` | N1 | How often the 48 KiB projection falls back to the canonical shape | confirmed | Replay, not a planner run: fallback is rare (rereads or the six largest); the projection drops little beyond `web_config_authority` | [§7.1](tools/get_plugin_schema.md#71-probe-runs) |
| `get_plugin_schema` | N4 | Catalogue fix contradicting the structural-node message | confirmed | Structural names get `plugin_not_installed` and a catalogue fix that misreads them; impact bounded | [§7.1](tools/get_plugin_schema.md#71-probe-runs) |
| `handoff_to_user` | N3 | User-visible operation code for a mixed recovery batch | confirmed | Freeform 500 `operation_failed`; planner 502 `MALFORMED_RESPONSE`; no repair round | [§7.1](tools/handoff_to_user.md#71-probe-runs-2026-11-09) |
| `handoff_to_user` | N1 | Pending `set_pipeline` proposal superseded by a reset | confirmed | Proposal half: `reset_to_turn_start` rejected the pending proposal; a park control left it pending. Interpretation sweep not exercised | [§7.1](tools/handoff_to_user.md#71-probe-runs-2026-11-09) |
| `handoff_to_user` | N2 | Safe checkpoint selecting the newer same-operation checkpoint | confirmed | At the session-service layer | [§7.1](tools/handoff_to_user.md#71-probe-runs-2026-11-09) |
| `inspect_source` | N1 | Absent storage file softened to an uncoded failure | confirmed | Uncoded failure while the service raises `BlobContentMissingError` | [§7.1](tools/inspect_source.md#71-probe-run-2026-11-09) |
| `inspect_source` | N2 (crash leftover) | Unhealed crash leftover visible to inspection | confirmed | Inspection hits an unhealed pre-update sidecar that `get_blob_content` restores | [§7.1](tools/inspect_source.md#71-probe-run-2026-11-09) |
| `inspect_source` | N2 (race) | Concurrent update race | not runnable here | Needs a lease-aware harness driving a public `update_blob` during a compose turn | [§7.1](tools/inspect_source.md#71-probe-run-2026-11-09) |
| `inspect_source` | N5, N6 | `;`-delimited and headerless files producing wrong or raw headers | confirmed | `a;b` stays one header with no warning; a headerless first row is returned as headers (provider wire only) | [§7.1](tools/inspect_source.md#71-probe-run-2026-11-09) |
| `inspect_source` | BOS-03 | Uppercase and braced selectors on inspection | confirmed | SQLite: uppercase, braced and `urn:` spellings return not found | [§7.1](tools/inspect_source.md#71-probe-run-2026-11-09) |
| `inspect_source` | N7 | Two encodings sharing one planner key | confirmed | Default, `None` and `utf-8-sig` share one key (key level; emission impact hypothesis) | [§7.1](tools/inspect_source.md#71-probe-run-2026-11-09) |
| `list_blobs` | N1 | "Gaps closed" notice after a truncated inventory | confirmed | Real planner loop: 51 stored, 50 returned, then the "gaps closed" notice | [§7.1](tools/list_blobs.md#71-probe-runs-2026-11-09) |
| `list_blobs` | N4 (SQLite) | Tie ordering on SQLite | confirmed | Ties follow insertion order, so the boundary survivor varies; repeated calls stable | [§7.1](tools/list_blobs.md#71-probe-runs-2026-11-09) |
| `list_blobs` | N4 (PostgreSQL) | Tie ordering on PostgreSQL | not runnable here | Needs Docker for the PostgreSQL testcontainer | [§7.1](tools/list_blobs.md#71-probe-runs-2026-11-09) |
| `list_blobs` | N7 | File panel omitting the 51st blob (browser) | not runnable here | Needs a browser and a running server | [§7.1](tools/list_blobs.md#71-probe-runs-2026-11-09) |
| `list_blobs` | N5 | Credential-shaped stored filename withholding the whole inventory | confirmed | The whole inventory is withheld (SQLite; how such a filename is written is unverified) | [§7.1](tools/list_blobs.md#71-probe-runs-2026-11-09) |
| `list_blobs` | BOS-01 | Re-confirmation of the evidence probe on a receiving branch | confirmed | Re-run byte-identical: stored 1/50/51, listed 1/50/50 | [§7.1](tools/list_blobs.md#71-probe-runs-2026-11-09) |
| `list_composer_blobs` | N3, N4 | Oversize and binary ready blobs listed and wireable | confirmed | 300,000-byte text, PNG and PDF blobs listed and wired | [§7.1](tools/list_composer_blobs.md#71-probe-runs-2026-11-09) |
| `list_composer_blobs` | N4 (validation) | Validation result for a wired binary blob | confirmed | Some give `malformed_inline_blob_content`; an ASCII PDF under utf-8 or any binary under latin-1 raises an uncaught `ValueError` (new N8) | [§7.1](tools/list_composer_blobs.md#71-probe-runs-2026-11-09) |
| `list_composer_blobs` | XC-01 | Planner behaviour when the skill asks for an inline marker | confirmed | Scripted planner: a marker to a verbatim blob is admitted (also to a nonexistent blob); the gap is teaching, not capability | [§7.1](tools/list_composer_blobs.md#71-probe-runs-2026-11-09) |
| `list_composer_blobs` | N7 (SQLite) | Boundary membership varying with equal `created_at` | confirmed | 55 tied rows: the excluded five depend on insertion order | [§7.1](tools/list_composer_blobs.md#71-probe-runs-2026-11-09) |
| `list_composer_blobs` | N7 (PostgreSQL) | The same on PostgreSQL | not runnable here | Needs Docker for the PostgreSQL testcontainer | [§7.1](tools/list_composer_blobs.md#71-probe-runs-2026-11-09) |
| `list_models` | N4 | Bundled slice and no provenance when OpenRouter is not primed | confirmed | In memory, no end-to-end turn: the bundled slice with no provenance field | [§7.1](tools/list_models.md#71-probe-runs-2026-11-09) |
| `list_models` | N1 | Prefixed-provider output under a live OpenRouter snapshot | confirmed | Injected live snapshot: the prefixed ids are unchanged and the validator rejects them. A real fetch needs network | [§7.1](tools/list_models.md#71-probe-runs-2026-11-09) |
| `list_models` | N7, GS-03 | Progress headline and tooltip in a browser | not runnable here | Needs a browser (vitest cannot reach a lane-private file); N7 confirmed at the progress producer only | [§7.1](tools/list_models.md#71-probe-runs-2026-11-09) |
| `list_secret_refs` | LSR-N1 | Rotated `secret_key` observed end to end in a freeform turn | not runnable here | Needs a live provider and a running web server; an in-memory substitute bounded the consequence | [§7.1](tools/list_secret_refs.md#71-probe-runs-2026-11-09) |
| `list_sinks` | N1, N2 | The projection and emptiness findings against the full test baseline | inconclusive | A focused 141-test baseline is green but asserts neither finding; the full suite was not run. A separate re-measure confirmed both | [§7.1](tools/list_sinks.md#71-probe-runs-2026-11-09) |
| `list_sinks` | N9 | Whether repeated cached discovery is bounded only by the deadline | confirmed | Mechanism corrected: cached and uncached rereads both run to the deadline; only mutations hit a cap | [§7.1](tools/list_sinks.md#71-probe-runs-2026-11-09) |
| `list_sinks` | N5 | Cache-hit audit row lacking `validation` | confirmed | The audit row holds three keys; the model saw five | [§7.1](tools/list_sinks.md#71-probe-runs-2026-11-09) |
| `list_sources` | N1 | No production policy emitting the categorical reason | confirmed | Empty across a five-cell policy matrix; a producer mutant goes red | [§7.1](tools/list_sources.md#71-probe-runs-2026-11-09) |
| `list_sources` | N2 | Same placement trap for sinks/transforms; which duplicate the runtime reads | refuted | Sinks and transforms do not advertise the failure fields; the runtime duplicate is harmless | [§7.1](tools/list_sources.md#71-probe-runs-2026-11-09) |
| `list_sources` | strict `{}` | Freeform strict empty call returning ARG_ERROR with repair text | confirmed | `ARG_ERROR` with repair text; handler not called | [§7.1](tools/list_sources.md#71-probe-runs-2026-11-09) |
| `list_transforms` | N15 | Planner executing a repeated read with no `DISCOVERY_NO_GAIN` | confirmed | The planner re-executed the read (172,151 bytes each time); no refusal | [§7.1](tools/list_transforms.md#71-probe-runs-2026-11-09) |
| `list_transforms` | N1 | No `plugin_not_allowed_on_web` on a running dev service | not runnable here | Needs a running dev service | [§7.1](tools/list_transforms.md#71-probe-runs-2026-11-09) |
| `list_transforms` | all-cache-hit | Provider calls vs budget counters on repeated cached reads | confirmed | Mechanism corrected: 41 cached turns, counter stays 1; the uncached control climbs to 41 and still returns. Freeform has no discovery cap | [§7.1](tools/list_transforms.md#71-probe-runs-2026-11-09) |
| `patch_node_options` | N5 | Whether `set_pipeline` heals a corrupt digest like `upsert_node` | confirmed | `set_pipeline` heals it; the patch refuses | [§7.1](tools/patch_node_options.md#71-probe-runs-2026-11-09) |
| `patch_node_options` | N6 | Explicit-approve proposal for a patch on a missing node | confirmed | Staged; accept 422, auto-rejected | [§7.1](tools/patch_node_options.md#71-probe-runs-2026-11-09) |
| `patch_node_options` | N7 | Real MCP dispatcher admitting an out-of-root `persist_directory` | confirmed | The real MCP handler stores it | [§7.1](tools/patch_node_options.md#71-probe-runs-2026-11-09) |
| `patch_node_options` | N10 | Partial `interpretation_requirements` effect on planner-owned rows | refuted | No data loss: rows restored resolved; omitting a referenced row refuses | [§7.1](tools/patch_node_options.md#71-probe-runs-2026-11-09) |
| `patch_output_options` | PO-N4 | Refused preview still staged, then 422 on accept | confirmed | Refused previews staged; accept 422, auto-rejected | [§7.1](tools/patch_output_options.md#71-probe-run-2026-11-09) |
| `patch_output_options` | PO-N2 | Turn-level crash for a non-string sink path | confirmed | `path: 123` ends the turn as `plugin_crash` | [§7.1](tools/patch_output_options.md#71-probe-run-2026-11-09) |
| `patch_source_options` | N2 | No-op proposal staged, then plain 409 on accept | confirmed | Staged; accept 409, proposal stays pending, a second accept 409s again. Accept ran on a directly created proposal | [§7.1](tools/patch_source_options.md#71-probe-runs-2026-11-09) |
| `patch_source_options` | N6 | MCP stdio refusal on a path-bound source | confirmed | Shipped stdio construction with `--data-dir` refuses `source_path_policy_invalid` | [§7.1](tools/patch_source_options.md#71-probe-runs-2026-11-09) |
| `patch_source_options` | N8 | Planner feedback naming a tool the planner lacks | confirmed | Offline planner run: the fix names three patch tools the palette lacks; live-model effect hypothesis | [§7.1](tools/patch_source_options.md#71-probe-runs-2026-11-09) |
| `patch_source_options` | N7 | Rendered "EXECUTABLE VERBATIM" patch reaching an uploaded source | confirmed | Rendered by the tolerant preview; obeyed in a compose turn; refused `source_data_contract_required` | [§7.1](tools/patch_source_options.md#71-probe-runs-2026-11-09) |
| `patch_source_options` | N5 | Whether the removed review event survives outside the graph | refuted | The durable review event survives; only in-graph provenance is lost | [§7.1](tools/patch_source_options.md#71-probe-runs-2026-11-09) |
| `preview_pipeline` | N1 | Contract violation through the real custody path | confirmed | Two of three JSON branches raise `FrameworkBugError` through real custody and a service compose; the third was not reached | [§7.1](tools/preview_pipeline.md#71-probe-runs-2026-11-09) |
| `preview_pipeline` | N6 | Preflight computed before rejecting extra arguments | confirmed | One preflight call before `ARG_ERROR` on dialect `none`; none on `openai_strict` | [§7.1](tools/preview_pipeline.md#71-probe-runs-2026-11-09) |
| `preview_pipeline` | N2 | Planner preview showing `runtime_preflight_not_run` on empty state | confirmed | The production decline makes no preflight call; the planner reply carries the "server wiring omission" text | [§7.1](tools/preview_pipeline.md#71-probe-runs-2026-11-09) |
| `preview_pipeline` | N7 | Preview judging the pre-proposal state without disclosure | confirmed | Tool semantics: no pending-proposal disclosure; model behaviour hypothesis | [§7.1](tools/preview_pipeline.md#71-probe-runs-2026-11-09) |
| `preview_pipeline` | N11 | MCP remediation text for a `blob_ref` source | confirmed | A `blob_ref` session loaded on MCP gets web-only repair text | [§7.1](tools/preview_pipeline.md#71-probe-runs-2026-11-09) |
| `read_attachment` | N2 | ESTALE through `run_tool_batch`: plugin crash | confirmed | Injected ESTALE ends the compose loop as a plugin crash; HTTP 500 mapping source-established | [§7.1](tools/read_attachment.md#71-probe-runs-2026-11-09) |
| `read_attachment` | N1 | Which operation kind each caller uses, before enforcing the allowlist | confirmed | `CREATE` and `ARCHIVE` succeed in both shared readers; every production caller uses a read kind | [§7.1](tools/read_attachment.md#71-probe-runs-2026-11-09) |
| `read_attachment` | N3 | Non-ASCII document against a live provider | not runnable here | Needs a live provider | [§7.1](tools/read_attachment.md#71-probe-runs-2026-11-09) |
| `remove_node` | N5 | Removing a required control and the explicit-approve rewrite | confirmed | Removal is allowed with a warning; the next valid edit re-splices it with a stale echo; explicit mode rewrites to `set_pipeline` | [§7.1](tools/remove_node.md#71-probe-runs-2026-11-09) |
| `remove_node` | explicit approval | Proposal round trip for `remove_node` | confirmed | REST accept 200 commits a dangling state; missing node 422; stale base 409; generic summary | [§7.1](tools/remove_node.md#71-probe-runs-2026-11-09) |
| `remove_node` | N11 | Persisted-response redaction of `applied_component` | confirmed | Mechanism corrected: the declarative entry drops the fields into `_unknown_response` | [§7.1](tools/remove_node.md#71-probe-runs-2026-11-09) |
| `remove_output` | N2 | Absent-sink proposal staged, then 422 on accept | confirmed | Staged; accept 422, auto-rejected | [§7.1](tools/remove_output.md#71-probe-run-2026-11-09) |
| `request_advisor_hint` | N8 | SPA outcome stamp per advisor status | confirmed | One status stamped failed or completed depending on `error_class` | [§7.1](tools/request_advisor_hint.md#71-probe-runs-2026-11-09) |
| `request_advisor_hint` | N2 | Transcript size vs the prompt cap | confirmed | A 416,468-character request admitted under a 2,000-character cap | [§7.1](tools/request_advisor_hint.md#71-probe-runs-2026-11-09) |
| `request_advisor_hint` | N7 | Oversized abandonment context failing as generic `ADVISOR_ERROR` | confirmed | Compose layer: generic error, budget charged, no SDK call; route reachability unverified | [§7.1](tools/request_advisor_hint.md#71-probe-runs-2026-11-09) |
| `request_advisor_hint` | N6 | Anti-anchor reset suppressing the §7.7 hint | confirmed | Advisor success between failures suppresses the hint | [§7.1](tools/request_advisor_hint.md#71-probe-runs-2026-11-09) |
| `request_advisor_hint` | N13 | Clarification framing after `cannot_complete` | confirmed | Compose layer: the notice is absent after `cannot_complete`; production reachability unverified | [§7.1](tools/request_advisor_hint.md#71-probe-runs-2026-11-09) |
| `request_interpretation_review` | N1, N2, N3 | The in-memory results as focused tests with existing fixtures | confirmed | Pytest compose loop with suite fixtures; the run's exit 1 is 13 assertions written to the defect | [§7.1](tools/request_interpretation_review.md#71-probe-runs-2026-11-09) |
| `request_interpretation_review` | PostgreSQL | Stage/resolve contention on PostgreSQL | not runnable here | Needs Docker for the PostgreSQL testcontainer | [§7.1](tools/request_interpretation_review.md#71-probe-runs-2026-11-09) |
| `restore_parked_graph` | N5 | Turn-level failure code for a malformed restore | confirmed | Freeform and malformed head 500 `operation_failed`; planner 502; no repair round | [§7.1](tools/restore_parked_graph.md#71-probe-runs-2026-11-09) |
| `restore_parked_graph` | N1, N2 | Success-path supersession, and an incremental proposal left to 409 | confirmed | Restore rejects pending `set_pipeline` proposals, also for a head-equal park; an incremental one stays pending and 409s | [§7.1](tools/restore_parked_graph.md#71-probe-runs-2026-11-09) |
| `restore_parked_graph` | N3 | Uppercase park id refused | confirmed | SQLite: uppercase, braced and padded ids refused as unavailable | [§7.1](tools/restore_parked_graph.md#71-probe-runs-2026-11-09) |
| `set_metadata` | N6 (a)–(d) | Compose-loop effect of a rename on progress consumers | not run | The lane probed only (e); (a)–(d) remain source traces. Prerequisite (stated on the sheet): a freeform turn on a non-empty state, since an empty state routes to the planner | [§7.1](tools/set_metadata.md#71-probe-runs-2026-11-09) |
| `set_metadata` | N6 (e) | The same, for the failure-cause branch | refuted | Production ids route an empty state to the planner, so the freeform branch is never entered | [§7.1](tools/set_metadata.md#71-probe-runs-2026-11-09) |
| `set_metadata` | N5 | Whether wire limits bound a 10M-character name | confirmed | Names up to 262,144 bytes persist; beyond that `CredentialMaterialRefused` escapes `compose()`; 2M characters hit wire bounds | [§7.1](tools/set_metadata.md#71-probe-runs-2026-11-09) |
| `set_metadata` | N2 | Reachability of the unrelated-output refusal from a loaded state | confirmed | Reachable from public YAML import; accept 422, auto-rejected | [§7.1](tools/set_metadata.md#71-probe-runs-2026-11-09) |
| `set_output` | N1, N3 | Turn-level crash for `path: null`; explicit-approve staging | confirmed | `path` null or 123 ends the turn as `plugin_crash`; three refused previews staged; accept 422 | [§7.1](tools/set_output.md#71-probe-run-2026-11-09) |
| `set_pipeline` | N8 | The builder-count test turning red when it patches the bound name | confirmed | With both names patched the original assertion goes red | [§7.1](tools/set_pipeline.md#71-probe-runs-2026-11-09) |
| `set_pipeline` | N1 | MCP stdio storing `/etc/passwd` as a source path | confirmed | Through the stdio-constructed handler: stored with `is_valid=True`; preview fails `path_allowlist`; public YAML has no paths. Byte transport not exercised | [§7.1](tools/set_pipeline.md#71-probe-runs-2026-11-09) |
| `set_pipeline` | N5 | Refusal channel for `source` plus `sources` by trust mode | refuted | Both modes give `ARG_ERROR`; the narrower difference is that `auto_commit` loses the field-specific `repair_instruction` | [§7.1](tools/set_pipeline.md#71-probe-runs-2026-11-09) |
| `set_source` | N8 | Accept-route outcome when a preview `ToolArgumentError` recurs | confirmed | Staged anyway; accept 500, proposal stays pending, a second accept 500s again | [§7.1](tools/set_source.md#71-probe-runs-2026-11-09) |
| `set_source` | N1 | Service-level PLUGIN_CRASH for `path: 123` | confirmed | 123, null, list and NUL each end the turn as `plugin_crash`; HTTP mapping not run | [§7.1](tools/set_source.md#71-probe-runs-2026-11-09) |
| `set_source` | N1 sibling | Provider-config path type escape via `upsert_node` | refuted | A non-string `persist_directory` is refused cleanly; a NUL string does raise `ValueError` from three node tools | [§7.1](tools/set_source.md#71-probe-runs-2026-11-09) |
| `set_source_from_blob` | N5 | Guarantee-stamp carry-over with genuine review evidence | confirmed | A stamp-only echo keeps the stamp and drops the resolved row; stamp plus rows carries the acknowledgement to another file | [§7.1](tools/set_source_from_blob.md#71-probe-runs-2026-11-09) |
| `set_source_from_blob` | N1 | Explicit-approve card omitting accept-time controls | confirmed | No preview; accept commits two controls the card never showed | [§7.1](tools/set_source_from_blob.md#71-probe-runs-2026-11-09) |
| `set_source_from_blob` | N2 | JSONL bound as `json` failing at runtime | confirmed | Loads 0 rows (discard) or 1 quarantined row; correct bindings load 3 | [§7.1](tools/set_source_from_blob.md#71-probe-runs-2026-11-09) |
| `set_source_from_blobs` | N7 | Turn outcome when staging raises `ValueError` | confirmed | Missing or uppercase id ends the turn with a bare `ValueError`; identical-content pair staged with no handler call | [§7.1](tools/set_source_from_blobs.md#71-probe-runs-2026-11-09) |
| `set_source_from_blobs` | N2 | Whether a `source_data_contract` review is reachable for `blob_rows` | confirmed | Reachable when a downstream node demands a field; resolve arm and runtime consequence not run | [§7.1](tools/set_source_from_blobs.md#71-probe-runs-2026-11-09) |
| `splice_transform` | N8 | Explicit-approve staging of a refused splice | confirmed | Refused and no-op previews staged; refused 422 auto-reject; no-op 409, proposal stays pending | [§7.1](tools/splice_transform.md#71-probe-runs-for-n8-and-n9-2026-11-09) |
| `splice_transform` | N9 | Replay after required-control auto-wiring failing topology | confirmed | In-memory composition, not a compose turn: replay refuses `splice_topology_unsupported` | [§7.1](tools/splice_transform.md#71-probe-runs-for-n8-and-n9-2026-11-09) |
| `update_blob` | N7 | Turn outcome for an unknown id under explicit approval | confirmed | Unknown, uppercase, hyphenless and `error`-status ids end the turn with a bare `ValueError` | [§7.1](tools/update_blob.md#71-probe-runs-2026-11-09) |
| `update_blob` | N1 | Turn-level `AuditIntegrityError` for an `error` blob | confirmed | Turn ends `plugin_crash`; bytes unchanged | [§7.1](tools/update_blob.md#71-probe-runs-2026-11-09) |
| `update_blob` | N8 | Deletion-ledger row escalating on update | confirmed | Simulated death: `update_blob` raises `AuditIntegrityError` while `list_blobs` says ready | [§7.1](tools/update_blob.md#71-probe-runs-2026-11-09) |
| `upsert_edge` | N1 (UI, jsdom) | Whether GraphView renders a `"source"` edge under named sources | confirmed | Graph model and Routes/Wiring lists show a phantom `source → out` route while the source reads not connected | [§7.1](tools/upsert_edge.md#71-probe-runs-2026-11-09) |
| `upsert_edge` | N1 (UI, canvas) | The canvas drawing of that edge | not runnable here | Needs a real browser | [§7.1](tools/upsert_edge.md#71-probe-runs-2026-11-09) |
| `upsert_edge` | N9 (card, jsdom) | Approval UI omitting the routing change | confirmed | The proposal card shows only the edge and a generic summary | [§7.1](tools/upsert_edge.md#71-probe-runs-2026-11-09) |
| `upsert_edge` | N9 (Accept panel) | The same in the hosted Accept panel | not runnable here | Needs a browser running the hosted approval flow | [§7.1](tools/upsert_edge.md#71-probe-runs-2026-11-09) |
| `upsert_node` | N1 | Aggregation/coalesce cases of the validity divergence | confirmed | Stage-1 valid, runtime "No producer" for all four kinds | [§7.1](tools/upsert_node.md#71-probe-runs-2026-11-09) |
| `upsert_node` | N1 (turn) | Whether the runtime failure reaches the model as a repairable code | confirmed | It reaches the model only as an uncoded line naming the consumer | [§7.1](tools/upsert_node.md#71-probe-runs-2026-11-09) |
| `upsert_node` | MCP | Out-of-root `persist_directory` admitted on MCP | confirmed | Stored at admission; stdio preview fails `path_allowlist`; `generate_yaml` omits it | [§7.1](tools/upsert_node.md#71-probe-runs-2026-11-09) |
| `upsert_node` | N4 | Review status after replacing a node with a queue | confirmed | Worse than hypothesised: integrity error, and later edits refused until the reviewed node is removed | [§7.1](tools/upsert_node.md#71-probe-runs-2026-11-09) |
| `upsert_node` | baseline | Existing suites the §6.1 fixes touch, before implementation | inconclusive | A baseline by construction (307 passed); it tests no finding | [§7.1](tools/upsert_node.md#71-probe-runs-2026-11-09) |
| `validate_secret_ref` | N3 | Profiled credential shadowed by a user row through `validate_pipeline` | confirmed | Narrowed: real both ways at the helper and `validate_pipeline`; production needs a credential change within one turn | [§7.1](tools/validate_secret_ref.md#71-probe-runs-2026-11-09) |
| `validate_secret_ref` | N1 | True concurrent race on SQLite and PostgreSQL | not runnable here | Needs a running server for a true `/api/secrets` race, and Docker for PostgreSQL | [§7.1](tools/validate_secret_ref.md#71-probe-runs-2026-11-09) |
| `validate_secret_ref` | batch scan | How a whole-batch credential refusal reaches the agent | confirmed | The agent gets nothing; the user gets 422 with admission/quota copy | [§7.1](tools/validate_secret_ref.md#71-probe-runs-2026-11-09) |
| `wire_blob_inline_ref` | N2, N3 | Explicit-approve consequence of staging an unchecked wire | confirmed | Missing-node wire staged, accept 422; uppercase, hyphenless or missing id ends the turn with a bare `ValueError` | [§7.1](tools/wire_blob_inline_ref.md#71-probe-runs-2026-11-09) |
| `wire_blob_inline_ref` | N1 | The validity result under the real catalogue | confirmed | Same under the real catalogue: `is_valid=True` for keys the plugin schema rejects | [§7.1](tools/wire_blob_inline_ref.md#71-probe-runs-2026-11-09) |
| `wire_blob_inline_ref` | N1 (turn) | Unrelated `delete_blob` failing after the wire | confirmed | In a turn and across turns, an unrelated `delete_blob` raises `AuditIntegrityError` | [§7.1](tools/wire_blob_inline_ref.md#71-probe-runs-2026-11-09) |
| `wire_secret_ref` | N1 | Accept-route denial with the allowlist configured | confirmed | Mutation-controlled: accept 422 at baseline, 200 when the route passes the policy | [§7.1](tools/wire_secret_ref.md#71-probe-results-2026-11-09) |
| `wire_secret_ref` | N3 | Undeclared option with a marker at runtime preflight | confirmed | Authored `is_valid=True`; runtime validation fails closed at `plugin_instantiation` | [§7.1](tools/wire_secret_ref.md#71-probe-results-2026-11-09) |
| `wire_secret_ref` | explicit approval | Unchecked wire staged as `APPROVAL_REQUIRED` | confirmed | No handler call before staging; an allowlisted wire then 422s on accept | [§7.1](tools/wire_secret_ref.md#71-probe-results-2026-11-09) |

Outcome totals over the 148 rows: 121 confirmed, 8 refuted, 2 inconclusive,
16 not runnable here, 1 not run. They were counted by extracting the table rows
(lines matching ``^| `tool` |`` between this heading and the next) and tallying
the fourth cell; the instrument returned 148 rows and, for the known
`clear_source` rows, 3 confirmed, 1 refuted and 1 not runnable, matching the
table. The 16 not-runnable rows need a browser or a running server (6),
Docker for PostgreSQL (4), a live provider (3, one of which also needs a
running server), a running server and PostgreSQL together
(`validate_secret_ref-N1`), the fix itself plus a new test file under `src/`
(`get_audit_info` GS-03), or a lease-aware concurrency harness (the
`inspect_source-N2` race). The refutations are `clear_source-N4`,
`get_pipeline_state-N2`, `list_sources-N2` (its sink/transform mirror),
`patch_node_options-N10`, `patch_source_options-N5`, METADATA-N6(e),
`set_pipeline-N5` and the `set_source` provider-path sibling; each sheet keeps
the narrower residual it measured.

Probes not in the table also ran: baselines of the existing tests each sheet
cites, re-runs of the review wave's in-memory probes under the network guard,
and incidental observations (for example `diff_pipeline-N6`, `handoff_to_user`
N8 and the new `list_composer_blobs-N8`). Each sheet's §7.1 lists them.

### Probe wave method and network record

**Run forms.** Every lane printed `elspeth.__file__` and showed that it
resolved into the worktree or into its lane-private mutation copy. Runs took
these forms: in-memory handler, `execute_tool` and service runs on in-memory
SQLite; real `compose()` and planner loops with a scripted provider
(`litellm.acompletion` or the gateway's `_call_llm` replaced by a scripted
double); the REST proposal-accept route driven in process; the Composer MCP
`call_tool` handler from the shipped `_build_stdio_server` construction (the
stdio byte pipe itself was not exercised); named pytest ids or single probe
files with `-n 0`; jsdom vitest files under lane-private configs; and
lane-private copies of `src/` for mutation controls (`diff_pipeline-N1`,
`get_audit_info-N5`, `get_blob_metadata-N4`, `list_sources-N1`,
`emit_pipeline_proposal-N1`, `wire_secret_ref-N1`). No tracked file other than
the 44 sheets changed; one lane created and removed a temporary file under
`tools/`, and several report that gitignored caches (`.hypothesis/`,
`__pycache__`, a vite temp directory) may have been touched.

**Not runnable here.** No live provider, running web server, deployed
service, browser, Docker/PostgreSQL or network was used. The rows marked not
runnable above need one of these. PostgreSQL behaviour of the raw-text UUID
comparisons (BOS-03), true concurrent races and live-model choices therefore
remain open.

**Network record.** The probe runs were meant to make no outbound request.
One did. An early `create_blob` compose probe used an empty state, which routes
to the proposal planner; the planner imports `_litellm_acompletion` by name, so
a patch of the gateway's `_litellm_acompletion` attribute (where the retained
probe file patches; the first script was not kept) would not reach it, and the
real wrapper would call the real `litellm.acompletion`. That is the inferred
mechanism; a patch of `litellm.acompletion` itself would have applied. One request reached a
provider endpoint without a valid credential and failed with
`AuthenticationError`; nothing ran. A fail-closed rule followed for later runs:
a dead-proxy environment prefix (`HTTPS_PROXY`/`HTTP_PROXY` set to
`127.0.0.1:9`, `LITELLM_LOCAL_MODEL_COST_MAP=True`,
`ELSPETH_DISABLE_NETWORK=1`), and an in-process guard that makes
`litellm.acompletion`, `litellm.completion` and the by-name planner and gateway
aliases raise, proved by a positive control before each probe. Many guards also
blocked `socket.connect`. The `validate_secret_ref` guard covered only three
entry points, and `upsert_edge`'s vitest runs had the prefix only.

Some runs predate the rule or had only the environment prefix. The lanes that
ran first (`clear_source`, `delete_blob`, `diff_pipeline`,
`emit_pipeline_proposal`, `explain_validation_error`, `get_audit_info`,
`get_blob_content`) report neither in their results; their compose and planner
runs used scripted provider doubles, and the last two reach no provider path.
Early runs of `get_pipeline_state`, `get_blob_metadata`, `get_plugin_schema`
and `handoff_to_user` predate the rule; each lane re-ran under it or explains
why the early run could not reach a provider (scripted doubles with finite
side effects, or `litellm` never imported). One `set_output` baseline run had
the prefix but not the guard.

Two incidental runs (`list_blobs`' first P5 run and a `get_blob_metadata`
guarded run) logged `litellm`'s import-time model-cost-map fetch from
`raw.githubusercontent.com`; the dead proxy refused each attempt, which is why
`LITELLM_LOCAL_MODEL_COST_MAP=True` joined the prefix. `get_plugin_schema`
reports the same refused fetch in the first guarded run of each probe (logs
since overwritten), and `get_pipeline_state` triggered it once deliberately as
an instrument control. Guard hits that were blocked in process are recorded
in `patch_output_options`, `set_metadata` and `update_blob`. No other outbound
request is known. That conclusion rests on scripted providers, guard logs and
call counts, not on packet capture.

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
| Shared documents after the probe-wave integration | 392 | 33 | 162 | 399 | 140 | 0 | 0 |
| Negative control: lane-directory copy of the documents and sheets (with the source tree linked in) unchanged, then with a missing sheet, a missing heading, an anchor past EOF, fabricated N99/N98 inline IDs for `remove_node` and `handoff_to_user`, and one genuine inline ID (`upsert_edge-N9`) | 392 → 395 | 33 → 34 | 162 → 163 | 399 → 402 | 140 | 0 unchanged; 5 planted (each planted defect; genuine ID passes) | 0; 1 |

The probe-wave outcome table has 148 rows (141 proposals, seven split).
Fifteen have Finding cells that are register IDs or prose (for example
`BOS-03 (SQLite)`, `XC-01`, `strict {}`, `baseline`), which the table pass
cannot check; the extra one against the earlier fourteen is the split BOS-03 row.
In the review-wave run the first pass found one wrong citation, corrected before
the final run; in the probe-wave run two earlier passes found three links to
renamed or not-yet-written anchors, corrected before the final run. The tool-sheet checker
(`check_sheets.py`, 46 sheets, 5,444 links, 5,372 line anchors) also exited 0 on
the probe-wave sheets. Quick-win citations to §6.1 labels (E*, item, row) were checked against
each sheet separately. Unnumbered §6.1 tables are cited by row position. Beyond
the link and ID checks, the integrating pass did not re-check the tool sheets'
content; conflicts it noticed were reported, not edited.

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

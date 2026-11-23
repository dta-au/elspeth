# Analysis of findings from the initial judge review of the 0.8.0 PR

The initial judge review exposed defects in how ELSPETH preserves integrity
failures, distinguishes corrupt internal evidence from absent evidence, and
checks the lifecycle of internal authority. Several suppression explanations
described controls that existed but did not protect the exception or execution
path being suppressed. This document records the concrete failures and the
evidence used to repair them.

This is a living engineering analysis of the 2026-09-08 remediation batch.
The starting source revision is `e2b91f2a0`. Source links identify the affected
modules; the named symbols and before/after descriptions remain useful when
line numbers change. Per-defect results below describe focused checks; the
[final verification section](#final-verification-and-release-decision) records
the integrated failures, their focused corrections, and the limits of the
release decision.

## What the completed review establishes

The operator's completed run published **424 accepted signatures** and removed
**148 stale entries**. It left **88 blocked findings** unsigned. All 660 actions
were accounted for; interrupted attempts preserved their decisions before the
final coherent publication.

Those numbers count signing actions, not unique bugs. Several findings concern
the same behavior, and a BLOCK can identify a missing justification or an
inappropriate suppression mechanism without establishing a runtime defect.
Conversely, an accepted signature approves one suppression against its bound
source and policy; it does not prove that the enclosing feature is correct.

At the start of remediation, the available saved output contained the full
judge rationale for **24 of the 88 blocks**. The other **64** retained their
blocked identities and proposed explanations, but their rejection rationales
were not recovered. Those sites require independent source review; the
proposed explanation must not be presented as the judge's reason.

The records below distinguish a judge report from an independently inspected
source path and, subsequently, from a regression test. They do not assume that
every rejection should become a code change.

## Confirmed source defects

### Corrupt source-contract evidence became an ordinary recognition miss

**Location:** [source_demand.py](../../src/elspeth/web/composer/source_demand.py),
`parse_source_data_contract_accepted_fields`,
`source_data_contract_fields_for_demand_recompute`, and the legacy parser.

**Trigger and prior behavior:** A server-authored source-contract draft contains
malformed JSON. The current parser catches `TypeError` or `ValueError` from
`json.loads` and returns `None`. Other malformed envelope fields also return
the ordinary non-match result. The recomputation path repeats this behavior
and includes a separate legacy-version recognition path.

**Impact:** Persisted internal evidence corruption becomes indistinguishable
from evidence that does not match the recognizer. Downstream acknowledgement
checks can prevent a malformed draft from granting execution authority, but
they do not surface the integrity failure that this parser discarded. The
source explicitly identifies these drafts as server-authored; serialization
does not make them external input.

**Evidence and repair status:** The recovered judge rationale identifies the
decode-failure branches; independent inspection confirms their `return None`
behavior and the explicit provenance comment. The repair lane independently
confirmed the defect and implemented a strict parser that raises
`AuditIntegrityError` for malformed JSON, invalid shape, unsupported version,
or a mismatched recomputation artifact hash. It also removed legacy parsing
and the legacy carry-forward branch. Tests now cover corrupt acknowledged
evidence both with and without live demand, unsupported versions, broken
artifact binding, and authoring round trips. The lane's final focused run
passed 785 tests, followed by 53 passing source-demand tests after adding five
shape-corruption cases. See the final verification section for integration.
This does not mean every incomplete review becomes an integrity exception:
existing recognition of an incomplete review or mismatched acknowledgement
hash can still create an explicit blocking review site. The repaired paths
are malformed/unsupported owned drafts and the recomputation artifact check.

### Fork-authority cleanup silently accepted missing registrations

**Location:** [coordination/repository.py](../../src/elspeth/web/coordination/repository.py),
`_build_fork_mutation_connection_controls.unregister_fork_mutation_connection`
and `_SessionOperationAuthorityRepository.__build_locked_fork_pair_controls.locked_pair_transaction`.

**Trigger and prior behavior:** A registration is absent when the corresponding
single cleanup/revocation runs. Both registries use defaulted `pop(..., None)`
on process-owned state. The operation therefore tolerates an unmatched removal
or a broken registration lifecycle.

**Impact:** The later authority resolver still rejects an absent registration,
so this finding does not by itself demonstrate unauthorized access. The defect
is loss of the lifecycle-integrity signal at the point where the registry
should still contain the registered item. A later fail-closed lookup does not
validate successful cleanup.

**Evidence and repair status:** The recovered rationales distinguish authority
admission from cleanup integrity. Independent inspection confirms defaulted
removal of `registered_pairs`. The lifecycle lane implemented strict revocation
with explicit `AuditIntegrityError` and preserved cleanup ordering. Its final
fork/registry/guided-fork-service selection passed 235 tests, and the
PostgreSQL reversed-pair contention proof passed. See final verification for
the integrated PostgreSQL result and its correction.

### Guided conversion replaced an integrity exception with an HTTP outcome

**Location:** [composer/guided.py](../../src/elspeth/web/sessions/routes/composer/guided.py),
`post_guided_convert`.

**Trigger and prior behavior:** An owned conversion operation raises
`AuditIntegrityError` or another unexpected first-party exception. The broad
handler classifies the failure, records a guided-operation settlement, and
calls `raise_guided_operation_failure`. That helper emits a closed HTTP error
rather than re-raising the original exception.

**Impact:** Recording a terminal operation outcome and logging a traceback do
not preserve the typed integrity failure. The route's error translation masks
the signal that the system's integrity policy requires to escape.

**Evidence and repair status:** Both the recovered rationale and independent
source inspection identify the explicit `AuditIntegrityError` classification
and replacement path. The repair retains settlement and cleanup while
re-raising the original fatal exception. The session-routes selection passed
107 focused tests and seven attribute/envelope tests. See final verification
for integration.

### Cleanup logging protected logger failures, not the captured cleanup failure

**Locations:**

- [composer/proposals.py](../../src/elspeth/web/sessions/routes/composer/proposals.py),
  `_close_proposal_lease_after_commit`.
- [guided_operations.py](../../src/elspeth/web/sessions/routes/guided_operations.py),
  `_GuidedOperationLeaseGuard.__aexit__` and cancellation cleanup in
  `reserve_or_replay_guided_operation` and `run_guided_reconciliation_mutation`.
- [routes/sessions.py](../../src/elspeth/web/sessions/routes/sessions.py),
  `_close_fork_operation_leases`.
- [sessions/service.py](../../src/elspeth/web/sessions/service.py),
  `SessionServiceImpl.archive_session` and its `run_owned_phase` helper.

**Trigger and prior behavior:** Cleanup, settlement, or an owned phase raises
an integrity exception while another failure or cancellation is already
active. The handler saves a class-name note and emits a diagnostic, then
returns or re-raises only the earlier failure. The proposal postcommit path
also contains every cleanup `Exception` after an otherwise durable success.

**Impact:** A cleanup-origin `AuditIntegrityError` can disappear behind
cancellation, an ordinary primary error, or a successful postcommit outcome.
The logging helper's Tier-1 re-raise applies only to exceptions raised while
calling the logger. It does not inspect or re-raise the cleanup exception whose
class name is supplied as a diagnostic field.

**Evidence and repair status:** Independent inspection confirms the postcommit
return, the fork close loop's primary-error branch, and archive's cancellation
and lease-close handlers. The recovered judge evidence also identifies a test
that expected cancellation when the archive phase itself raised
`AuditIntegrityError`. That expectation tested the defective priority rule.
Repair coverage must inject fatal
errors from the phase or cleanup operation itself, as well as from logging,
and must confirm that required peer cleanup still completes. The fork lane
has implemented propagation after draining both leases; its focused module
passed six tests, including
`tests/unit/web/sessions/test_fork_dual_fence_route.py::test_reverse_close_propagates_integrity_after_closing_both_leases`
with one and two fatal close failures. This is lane-level evidence; see final
verification for integration. The final lifecycle lane also passed 39 guided
cleanup tests and 20 archive secondary-failure tests. These distinguish
cleanup-origin integrity failures from logger-origin failures and preserve
both failures when more than one fatal operation fails.

### Fork settlement fence loss bypassed a captured integrity failure

**Location:** [routes/sessions.py](../../src/elspeth/web/sessions/routes/sessions.py),
`register_session_routes.fork_from_message`.

**Trigger and prior behavior:** Blob cleanup has already captured an integrity
exception in `cleanup_integrity_exc`. A subsequent
`fail_guided_fork_operation` call loses its guided or session fence. The
fence-loss handler continues the reservation loop before reaching the
conditional re-raise of the captured integrity exception.

**Impact:** Refusing to settle under stale authority is correct, but retrying
the authority operation discards an independently detected integrity failure.
The later re-raise exists but is unreachable on this branch. Logging the
cleanup failure does not repair that control-flow gap.

**Evidence and repair status:** The recovered judge rationale and independent
source inspection agree on the `continue` preceding the re-raise. The lifecycle
lane repaired propagation before retry while retaining lease cleanup and
refusal to settle with stale authority. Its final 235-test selection includes
fork cleanup and fence-loss regression coverage. See final verification for
integration.

### Auto-title provider error handling also swallowed a database timeout

**Location:** [_auto_title.py](../../src/elspeth/web/sessions/_auto_title.py),
`maybe_auto_title_session`.

**Trigger and prior behavior:** The database write that saves a generated title
raises `TimeoutError`. The same `try` covers both title generation and the
database update, so the handler intended for a provider timeout also absorbs
the persistence failure.

**Impact:** A first-party title-write failure is treated as an expected
best-effort provider failure. The exception class alone cannot distinguish
those operations; the scope of the handler determines which failures it can
legitimately contain.

**Evidence and repair status:** The repair lane independently confirmed this
adjacent defect while reviewing the auto-title block. It narrowed the
provider-error handling to the provider operation and added database-write
regressions for both `RuntimeError` and `TimeoutError`. The final 107-test
session-routes selection passed. See final verification for integration.

### Guided coverage treated a broken component index as unproven coverage

**Location:** [guided/deferred_intents.py](../../src/elspeth/web/composer/guided/deferred_intents.py),
`_DeferredCoverageContext.exclusively_reached_gate` and `route_output_name`.

**Trigger and prior behavior:** A consumer identity is missing from the owned
`exact_components` index. Its constructors establish a total mapping for those
consumers, but optional lookup converted the missing entry into `None` or an
unproven coverage result.

**Impact:** A broken internal graph index became an ordinary inability to prove
coverage. The caller lost the distinction between an incomplete internal
contract and a graph that legitimately does not establish the requirement.

**Evidence and repair status:** The guided lane independently confirmed the
constructor contract and replaced optional lookup with direct indexing.
`tests/unit/web/composer/guided/test_block_remediation.py::test_coverage_walk_rejects_missing_owned_component`
covers success and branch paths. The lane reported 465 focused tests passing,
then 24 passing tests after strengthening the regressions. See final
verification for integration.

### Guided wire projection dropped valid schema fields from confirmable proposals

**Location:** [guided/emitters.py](../../src/elspeth/web/composer/guided/emitters.py),
`_wire_schema`.

**Trigger and prior behavior:** An authored schema uses valid YAML shorthand
or the accepted `field_type` spelling. The wire projection bypassed
`SchemaConfig.from_dict` normalization and silently dropped those fields while
the proposal could still report `can_confirm=True`.

**Impact:** The review surface did not faithfully present the authored schema.
A user could confirm a proposal whose displayed field contract omitted valid
fields from the underlying configuration.

**Evidence and repair status:** The guided lane reproduced the mismatch and
switched projection to the canonical parser.
`tests/unit/web/composer/guided/test_block_remediation.py::test_wire_projection_preserves_all_valid_schema_field_spellings`
checks complete projected fields and confirmability;
`::test_malformed_wire_field_cannot_make_a_proposal_confirmable` checks that
invalid fields produce `contract_config_invalid` and prevent confirmation.
Both are included in the guided lane results above. See final verification
for integration.

### Cleanup diagnostics fabricated provenance when exception formatting failed

**Location:** [orchestrator/cleanup.py](../../src/elspeth/engine/orchestrator/cleanup.py),
`_safe_cleanup_error_text`.

**Trigger and prior behavior:** An exception's `__str__` raises. The fallback
constructs an “unrepresentable” marker, then measures and hashes that marker as
though it were the original error message.

**Impact:** Diagnostic provenance attributes describe fabricated replacement
text rather than an available raw message. The fallback should state that
formatting failed and that raw-message length and digest are unavailable.

**Evidence and repair status:** The engine lane independently confirmed the
behavior. Its repair records the original and formatting exception classes
and leaves raw digest and length `None`.
`tests/unit/engine/orchestrator/test_cleanup_failure_ceremony.py::TestCleanupDoesNotMaskPendingException::test_unrepresentable_exception_records_an_explicit_marker`
passed in the focused lane run. The final engine selection passed 762 tests;
see final verification for integration.

### A telemetry diagnostic could replace an already-audited LLM result

**Location:** [clients/llm.py](../../src/elspeth/plugins/infrastructure/clients/llm.py),
`AuditedLLMClient._emit_telemetry_after_audit`.

**Trigger and prior behavior:** Telemetry fails after the primary call has been
audited. Its diagnostic calls `str(tel_err)` and includes raw traceback
information. A defective formatter can raise from this recovery path, and
external exception text can enter the diagnostic.

**Impact:** Reporting an ordinary telemetry failure can replace the successful
primary result or expose provider-controlled content through logging.

**Evidence and repair status:** The engine lane replaced raw formatting with
exception class and correlation fields.
`tests/unit/plugins/clients/test_audited_llm_client.py::test_telemetry_failure_is_acknowledged_without_formatting_external_error`
passed in the focused run and is included in the final engine selection. See
final verification for integration.

### A fatal final heartbeat could be latched after its last observer

**Location:** [orchestrator/heartbeat.py](../../src/elspeth/engine/orchestrator/heartbeat.py),
`RunHeartbeatThread`, and orchestrator shutdown paths.

**Trigger and prior behavior:** The final heartbeat during shutdown detects a
fatal failure and stores it for the owning thread. Shutdown does not guarantee
another read of that failure after the heartbeat stops.

**Impact:** The background worker records a fatal integrity failure internally,
but the caller can complete without receiving it. A latch is insufficient if
the lifecycle has already passed its final check.

**Evidence and repair status:** The engine lane added a fatal-latch check after
mandatory teardown in fresh, resumed, and follower execution.
`tests/unit/engine/orchestrator/test_cleanup_failure_ceremony.py::TestPartialResultCeremonySurvivesCleanupFailure::test_final_heartbeat_failure_reaches_caller_after_seat_release`
passed through the real orchestrator. The final engine selection passed 762
tests, with three additional resume-owner tests passing. See final
verification for integration.

### Database exception containment ran before the live integrity classification

**Location:** [orchestrator/heartbeat.py](../../src/elspeth/engine/orchestrator/heartbeat.py),
`RunHeartbeatThread._emit_degraded`.

**Trigger and prior behavior:** A SQLAlchemy exception subclass is registered
as a Tier-1 integrity failure after module import. The `SQLAlchemyError` catch
runs before the live Tier-1 guard and treats it as ordinary diagnostic loss.

**Impact:** Handler order defeats the dynamic integrity registry. The same
exception can be recognized as fatal elsewhere but suppressed here.

**Evidence and repair status:** The engine lane corrected the ordering.
`tests/unit/engine/orchestrator/test_run_heartbeat_thread.py::TestHeartbeatDegraded::test_late_registered_database_integrity_failure_is_not_diagnostic_loss`
passed in the focused run and is included in the final engine selection. See
final verification for integration.

### Missing profile selection metadata became an unavailable profile

**Location:** [planner_authoring_aids.py](../../src/elspeth/web/composer/planner_authoring_aids.py),
`_usable_llm_profile_alias`.

**Trigger and prior behavior:** The owned availability snapshot contains a
usable profile but lacks its required selected-alias entry. Availability
construction produces these entries together; optional lookup hid the missing
half of that contract.

**Impact:** A corrupt internal snapshot looked like an ordinary lack of a
selected profile, changing the authoring information supplied to the planner.
An explicit `None` selection and a missing required entry are different states.

**Evidence and repair status:** The composer lane verified the paired
construction and changed to required lookup. Both cases in
`tests/unit/web/composer/test_planner_authoring_aids.py::test_profile_selection_distinguishes_explicit_none_from_corrupt_snapshot`
passed. See final verification for integration.

### Corrupt tool-error state silently erased the error code

**Location:** [composer/protocol.py](../../src/elspeth/web/composer/protocol.py),
`ToolArgumentError.code`.

**Trigger and prior behavior:** The owned backing state for a tool argument
error code is missing or malformed. The accessor converts the violation to
`None`, dropping the structured classification that downstream code consumes.

**Impact:** A broken first-party exception contract becomes an unclassified
error instead of surfacing the internal defect. Safe exception display does
not require suppressing corruption in the separate classification accessor.

**Evidence and repair status:** The composer lane changed corrupt access to
raise `FrameworkBugError` while retaining safe display behavior. Four cases in
`tests/unit/web/composer/test_service.py::TestToolArgumentError::test_corrupt_private_code_cannot_silently_erase_audit_classification`
passed, along with the 51-test protocol selection. See final verification for
integration.

### Malformed checkpoint bytes escaped as ordinary decoding errors

**Location:** [checkpoint/recovery.py](../../src/elspeth/core/checkpoint/recovery.py),
`RecoveryManager.reconstruct_token_row`.

**Trigger and prior behavior:** A stored token checkpoint contains invalid
UTF-8 or invalid JSON. Decoding raises an ordinary `ValueError` subclass before
the existing envelope guard can classify the corrupted audit payload.

**Impact:** The system loses the explicit Tier-1 failure classification for
corruption in persisted checkpoint data. The existing shape check only covers
payloads that decode successfully.

**Evidence and repair status:** The original rejection rationale was not
recovered. Independent source review and production-path tests established
the classification gap. The repair raises `AuditIntegrityError` with token/run
context and preserves the decoding cause. The checkpoint and SQLCipher
selection passed 125 tests, including
`tests/unit/core/checkpoint/test_recovery.py::test_reconstruct_token_row_rejects_corrupt_envelope`.
See final verification for integration.

### SQLCipher URL booleans silently changed configuration meaning

**Location:** [landscape/database.py](../../src/elspeth/core/landscape/database.py),
`LandscapeDB._create_sqlcipher_engine`.

**Trigger and prior behavior:** A boolean URL option uses an unrecognized
spelling, or a valid SQLAlchemy true spelling such as `on`, `y`, or `t`.
Conversion silently produces `False` instead of rejecting the malformed
setting or preserving the recognized true value.

**Impact:** The encrypted database connection can use different behavior from
the authored configuration and the corresponding SQLAlchemy interpretation.
Rejecting repeated query parameters does not address scalar-value parsing.

**Evidence and repair status:** The original rejection rationale was not
recovered. The storage lane independently confirmed and repaired this adjacent
defect with explicit boolean parsing. Tests reject malformed boolean values
before database creation and exercise real connection thread enforcement for
accepted spellings. The 125-test storage result above includes
`tests/unit/core/landscape/test_database_sqlcipher.py::TestSQLCipherCreateAndRead::test_malformed_boolean_query_rejected_before_database_creation`
and `test_boolean_query_spelling_matches_sqlalchemy`. See final verification
for integration.

### Core configuration depended upward on plugin implementations

**Locations:** [core/config.py](../../src/elspeth/core/config.py) and
[core/llm_profiles.py](../../src/elspeth/core/llm_profiles.py).

**Trigger and prior behavior:** Core configuration loading or LLM profile
admission imports plugin-layer implementation details. Profile admission
constructs a full transform configuration to reuse provider validation,
coupling foundational configuration policy to a higher implementation layer.

**Impact:** Changes to plugin implementations can affect importing and using
core configuration. This is a real dependency-direction violation; it does
not by itself establish an observed production outage.

**Evidence and repair status:** The original rejection rationales were not
recovered. The core lane independently confirmed the imports. It moved shared
provider policy into a core-owned module, retained validation in both profile
and plugin admission, and moved plugin-aware loading orchestration into the
application layer. The profile/provider selection passed 135 tests; the
configuration selection passed 390. See final verification for integration.

### Failed event persistence still allowed a live broadcast

**Location:** [execution/service.py](../../src/elspeth/web/execution/service.py),
`ExecutionServiceImpl._persist_and_broadcast_run_event`.

**Trigger and prior behavior:** Appending the run event raises `OSError` or a
SQLAlchemy error. The handler continues to broadcast an event that was not
persisted and did not receive the durable sequence.

**Impact:** Live clients can observe an event absent from the persistent event
stream. A reconnecting or replaying client cannot recover that same event from
the authoritative history.

**Evidence and repair status:** The execution lane independently confirmed the
path and changed it to propagate the persistence failure before broadcasting.
`TestEventBusBridge.test_failed_event_persistence_never_broadcasts` passed in
the initial focused run; the final execution selection passed 574 tests. See
final verification for integration.

### WebSocket error responses swallowed integrity failures

**Location:** [execution/routes.py](../../src/elspeth/web/execution/routes.py),
`create_execution_router.websocket_run_progress`.

**Trigger and prior behavior:** Ownership, initial event loading, or idle
polling detects an integrity failure. The route logs the failure and closes
the socket with code 1011, then consumes the original exception.

**Impact:** The client receives an error, but the server loses the typed
integrity failure. Protocol cleanup is necessary; it does not replace
propagation to the server's failure handling.

**Evidence and repair status:** The execution lane changed these paths to
close and unsubscribe, then re-raise the original exception even if logging
or socket close fails. The updated dangling-foreign-key regression passed;
additional initial-load, idle-poll, and failing-logger cases are included in
the final 574-test execution selection. See final verification for integration.

### Artifact lookup confused operational failures with ordinary candidate rejection

**Location:** [execution/routes.py](../../src/elspeth/web/execution/routes.py),
`_artifact_error_type` and `_resolved_allowed_artifact_paths`.

**Trigger and prior behavior:** An arbitrary `HTTPException` carries a detail
mapping that resembles a known recoverable artifact error. Classification by
shape accepts it as that owned error. Separately, `Path.resolve` raises
`OSError`; the candidate is omitted, and the eventual response reports 403
outside the allowlist.

**Impact:** A structural lookalike can enter an internal recovery path, and a
filesystem failure can be misreported as an authorization refusal. Neither
behavior preserves the actual cause of the failed lookup.

**Evidence and repair status:** The execution lane introduced private nominal
exception types and made resolution failure an explicit chained
`artifact_path_resolution_failed` 500 outcome. The final execution selection
passed 574 tests. See final verification for integration.

### A malformed present blob reference disappeared from proof resolution

**Location:** [execution/service.py](../../src/elspeth/web/execution/service.py),
`ExecutionServiceImpl._authoritative_proof_blob_resolver`.

**Trigger and prior behavior:** A proof input contains a present but malformed
`blob_ref`. The resolver skips it as though no usable reference was supplied.

**Impact:** Malformed input becomes absence, erasing the reason proof
resolution could not use the supplied reference.

**Evidence and repair status:** The execution lane changed the path to raise
a source-specific `MalformedBlobRefError` before proof processing while
preserving the existing execution error routing. The final execution selection
passed 574 tests. See final verification for integration.

### Session lease joining discarded task failures after cancellation

**Location:** [coordination/lifecycle.py](../../src/elspeth/web/coordination/lifecycle.py),
`_join_finish_task` and the session lease context manager's `__aexit__`.

**Trigger and prior behavior:** The caller is cancelled while the owned lease
finish task is still running, and that task then fails. The join path consumes
the task failure in favor of cancellation. A related context-manager path
also suppresses cleanup failure while a body exception is active.

**Impact:** Mandatory cleanup can fail without its exception reaching the
caller. Repeated cancellation makes this especially easy to miss in tests
that only verify the join eventually completes.

**Evidence and repair status:** This is a related defect independently found
during coordinator review, not a separately counted original BLOCK. The
repair propagates the task's original failure with context and groups
simultaneous body and cleanup failures so neither is discarded. The complete
51-test lease module passed, including
`test_close_release_failure_survives_repeated_cancellation`. See final
verification for integration.

### Advisor retry handling masked internal bugs and exposed a tool-audit ordering gap

**Location:** [composer/service.py](../../src/elspeth/web/composer/service.py),
`ComposerServiceImpl._run_advisor_checkpoint` and
`_compose_loop._dispatch_and_persist_tool_turn`.

**Trigger and prior behavior:** The advisor checkpoint raises an unexpected
first-party exception. Its broad retry handler treats that failure like a
provider fault instead of preserving the original exception. Narrowing the
catch also exposed a caller ordering gap: an early checkpoint ran before
publication of the already-completed tool turn's audit evidence.

**Impact:** Internal bugs can be retried or converted into a checkpoint verdict.
Simply removing that recovery could then let completed tool state survive
without its corresponding durable tool audit.

**Evidence and repair status:** The composer lane restricted recovery to
declared provider failures, timeout, and connection errors. It also put
completed-turn audit persistence in `finally` around the early checkpoint.
The original advisor exception now propagates after audit publication.
`tests/unit/web/composer/test_service.py::TestComposeTimeout::test_early_advisor_internal_failure_preserves_completed_tool_audit`
uses real SQLite state to check exception identity, persisted assistant/tool
rows, and the current source state for three internal exception classes. The
final advisor/service selection passed 254 tests. See final verification for
integration.

### Smaller contract and diagnostic repairs

The following source-confirmed repairs belong to the same review but do not
establish additional observed production incidents:

| Location | Prior defect and repair | Focused evidence |
| --- | --- | --- |
| `ComposerServiceImpl._render_schema_for_advisor` | Defaulted removal hid an impossible missing withheld-field counter. Direct deletion now enforces the installed-counter contract. | Included in the 254-test advisor/service selection; one-item and oversized-schema cases retain honest withheld counts. |
| `tools._dispatch._json_type_label` and `_schema_error_summary` | String coercion could turn corrupt schema type values or required names into plausible diagnostics. Required schema values are now consumed under their declared contract without coercion. | Eight new schema-formatting tests and 11 existing observed-CSV argument tests passed. |
| `source_inspection._redact_url_candidate` | Malformed URL authority could raise `ValueError` from a redactor documented not to raise. The path now returns an explicit redaction marker. | Source-inspection/tutorial selection: 161 tests passed. |
| `source_inspection.observed_columns_from_path` | An unused helper swallowed every `OSError` into empty column evidence. The unused helper and its obsolete tests were removed. | Main composer selection: 268 tests passed. |
| `accept_composition_proposal` | The auto-reject path caught every `ValueError` as an already-terminal proposal race. It now catches only the explicit `ProposalStateConflictError`; unrelated internal errors propagate. | The final 107-test session-routes selection includes terminal-race and internal-`ValueError` regression cases. |
| Follower heartbeat ownership checks | A fatal heartbeat latch was checked only after coordination loss, permitting continued claims when only the fatal latch was set. Unconditional fatal checks now stop the owner. | Included in the final 762-test engine selection. |
| Heartbeat thread entry point | Clock or logger failures could escape the daemon thread without reaching the fatal latch. The outer thread boundary now transports those failures to the owner. | Included in the final 762-test engine selection. |
| Audit-export cleanup | A fatal orphan-marking error could skip private spool closure, and a cleanup diagnostic failure could replace a primary Tier-1 error. Mandatory closure and primary-error preservation now cover both paths. | Included in the final 762-test engine selection. |
| `parse_completion_gates` | An arbitrary `Mapping` implementation could supply an owned persisted signoff envelope. Admission now accepts only the closed owned `dict`/`MappingProxyType` representations. | `test_noncanonical_mapping_cannot_supply_a_persisted_signoff`, included in the final 574-test execution selection. |
| Schema-probe cleanup | A failing cleanup logger could replace the primary error. Secondary failures now attach safe exception notes without invoking that fragile logger. | `test_cleanup_failures_are_attached_to_primary_without_calling_a_fragile_logger`, included in the final 574-test execution selection. |
| Execution `_on_pipeline_done` | Logging could fail before lease closure was scheduled. Diagnostics now run in tracked cleanup after exact lease closure, and shutdown can surface their failure. | `test_done_callback_logger_failure_is_tracked_after_exact_lease_close`, included in the final 574-test execution selection. |

## Blocks that require evidence or policy correction

Some recovered rationales explicitly stop short of identifying a runtime bug.
These cases remain distinct from the confirmed failures above:

| Finding | Evidence requested by the judge |
| --- | --- |
| `CompositionState._validate_with_probe_cache` aggregation exception handling | Whether the blocking aggregation validator runs on every path for which advisory parsing abstains; cite and exercise that path. |
| `_parse_template_names` and `_validate_prompt_template_variable_bindings` | Whether malformed templates are rejected at admission before the advisory analysis can return no result. |
| `_check_schema_contracts._resolved_producer_field_type` | Whether node-id validation is reached on every path relied upon by the proposed explanation. |
| `record_tutorial_completed_path` | The exact committed-outcome control and a regression test, rather than a generic reference to a test file. |
| `_join_shielded_task_after_cancellation` | Exact caller symbols and test nodeids showing that caller cancellation is eventually propagated after cleanup. |
| `fork_from_message` bounded fence-loss retry | A precise control location for reservation and exhaustion, rather than the phrase “bounded outer loop.” |
| `_SessionPendingInterpretationPlanner.plan` optional metadata extraction | The judge found legitimate external-data parsing but required supported boundary metadata instead of a per-line suppression. This is a declaration/mechanism issue, not proof that the lookup fabricated data. |
| `parse_completion_gates` | The judge objected to revalidation of an owned persisted contract. Its rationale acknowledges an explicit error rather than recovery; review the contract and prescribed representation before claiming a runtime failure. |

A weak explanation must be corrected with verifiable source and regression
evidence. It must not be strengthened by inventing a control, relabeling
internal data as external, or changing clear code solely to avoid a finding.
The remediation lanes supplied amended explanations and focused evidence for
the legitimate cases. Those explanations still await fresh judge adjudication;
the table records the original evidence request, not a claim that the agent's
amendment has been approved.

## Confirmed deferred findings at the end-of-day pause

These related defects were present in the starting tree and remain unfixed in
this batch. Their current evidence is source review; no new runtime
reproduction was completed before the pause. They are separate follow-up work,
not additional counted original BLOCKs or completed repairs.
All three are in
[coordination/lifecycle.py](../../src/elspeth/web/coordination/lifecycle.py).

| Tracker issue | Source-confirmed failure mechanism | Status |
| --- | --- | --- |
| `elspeth-ba3af151a7` | `_join_owned_tasks` and `_close` can discard additional failures when multiple owned tasks or cleanup operations fail. Preserving only one exception loses the other failure signals. | Deferred; preserve all simultaneous failures and add regression coverage. |
| `elspeth-4844c270fa` | Lease acquisition/adoption cancellation paths reduce operation errors to notes while propagating cancellation. The operation failure itself is not preserved as a propagated exception. | Deferred; review exception priority and test cancellation combined with acquisition/adoption failure. |
| `elspeth-fa0e13545b` | `_restore_archive_current` can discard a secondary fatal failure during archive recovery. | Deferred; preserve the original and secondary failures and test the combined path. |

## Final verification and release decision

The first frozen integrated run at `2aaa38fe7` finished with **48,721 passed,
21 failed, 79 skipped, and six expected failures** in the default selection
(exit 1). The full PostgreSQL selection finished with **303 passed and one
failed** (exit 1). Neither run was green.

The operator then explicitly waived another full-suite run and authorized
merge after focused correction and verification of every failed case. The
following corrections changed tests or the test-policy scanner, with no
production-code changes after the frozen run. Focused counts overlap and must
not be added to produce a unique test total.

| Failure group | Correction and focused result |
| --- | --- |
| Native line-only writer pins | Refreshed 107 writer pins using the native mechanism; 16 focused tests passed. |
| Configuration-loading parity | The scanner missed six rejection sites moved into `config_loading`; updated scanner coverage and three mock specifications. Twenty-one focused tests passed. |
| TS02 heartbeat test double | Added the newly required method to the fake heartbeat contract. The failed case passed. |
| Guided schema fixture | Corrected invalid `mode: declared` to the supported `fixed` mode. Nineteen focused tests passed. |
| Completion-parser lint expectations | Updated obsolete R5 expectations to the repaired parser. Thirty-four focused tests passed. |
| PostgreSQL fence-loss expectation | Updated the test to expect the group retaining both fence-loss failures. The coordinator's repeat on the exact integration state passed: one test, exit 0. |

All 21 default-suite failures have therefore been corrected and passed in
focused selections, and the failed PostgreSQL case passed its integrated
rerun. The corrections are committed at `aaa9ac065`; production source is
unchanged from the initial frozen run. The full default and PostgreSQL suites
have **not** been
rerun after those corrections. The release decision relies on the completed
broad runs, the focused correction results, and the operator's explicit
waiver; it is not a claim that the final tree passed a full-suite rerun.

The all-rules lint inventory changed from 1,834 findings before remediation
to 1,946 afterward, both exit 1. Most of the increase is signature-binding
drift from the source changes. The L1 layering findings are gone. No new
signing was performed, and amended suppression explanations still require
the operator/judge workflow.

The completed lane checks below provide additional focused evidence. They
overlap each other and the integrated selections.

| Area | Completed focused evidence |
| --- | --- |
| Core configuration, storage, contracts, expression parsing and TUI | 979 passed, one skipped in the final combined core selection; boundary gates passed. |
| Source-demand and interpretation state | 785 passed, then 53 passed after additional malformed-shape regressions. |
| Guided projection and coverage | 465 passed, then 24 strengthened regressions passed. |
| Composer authoring aids and telemetry | 268 passed; 51 protocol tests passed; 161 source-inspection/tutorial tests passed. |
| Composer advisor and audit publication | 254 passed in the final advisor/service selection. |
| Session lifecycle and registry | 235 fork/registry/service tests, 39 guided cleanup tests, 20 archive tests, and one PostgreSQL contention proof passed. |
| Session routes | 107 focused tests and seven attribute/envelope tests passed; boundary gates passed. |
| Coordinator lease-join repair | 51 tests passed in the lease module. |
| Engine and plugin cleanup, heartbeat, telemetry | 762 focused tests and three additional resume-owner tests passed. |
| Execution, artifacts, WebSocket, proof and schema cleanup | 574 focused tests passed. |

No proposed replacement justification in this document is a fresh judge
approval. The three deferred findings above remain open regardless of the
merge decision.

## What this review adds to ordinary testing

The most consequential pattern is an exception-handling control that protects
the wrong failure. “Tier-1 exceptions propagate” sounds sufficient until the
review follows which exception enters that handler. Protecting the logger is
different from preserving a fatal failure raised by the operation being logged.

A second pattern is path coverage: a re-raise, validator, or authority check
can exist in the right function and still be bypassed by an earlier return,
retry, or cancellation branch. The fork settlement finding demonstrates why
the existence of a control is weaker evidence than its execution on the
specific failing path.

The process also challenged tests that faithfully pinned an incorrect
behavior. A passing test that expects an integrity exception to become
cancellation does not establish integrity preservation. The regression should
first demonstrate the lost signal, then require the original failure to
survive without skipping mandatory cleanup.

These are concrete benefits of asking a reviewer to justify a suppression
against its surrounding contract. They do not establish a defect-detection
rate or prove every rejection correct. The remaining review work is to test
the disputed behavior, repair actual defects, and retain explicit evidence for
legitimate exceptions.

# Composer Facade Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The root retains index ownership and controls source-write release.

**Goal:** Retire the unreachable private `PlanningApplication` adapter while retaining the direct planner library and migrating every meaningful legacy integration oracle onto its current production owner.

**Architecture:** H2.1 already sends ordinary composition through the common provider loop; H2.2 already shares complete authenticated chronology. Execute the reviewed cost and mutation-authority companions first, including their actual custody, cancellation and storage qualification. Then remove only the dead adapter/wiring and adapt finite physical SDK scripts and assertions to common authoring, EARLY/END, P3/P4 and current HTTP outcomes.

**Tech Stack:** Python 3.12+, pytest, existing FastAPI operation harness, SQLAlchemy session store, existing physical LiteLLM SDK seam and required-work owners.

---

## Binding, ownership and prerequisites

This is a docs-only executable packet. No source/test edit, import, test run,
provider call, installation, service operation or Git index/ref/object write
is authorized by writing it. Before this file was created,
`git check-ignore -v docs/superpowers/plans/2026-10-22-composer-facade-retirement.md`
returned no output and exit 1. Coordination remains in the ignored
`.claude/lanes/first-carry-20261021/h2-facade-retirement.md`.

Original H2.3 source: planning commit
`170ec0ba3f801034b774b31c2193f897c7d3993d`,
`docs/superpowers/plans/2026-10-20-composer-heuristic-cleanup.md`.
Integration baseline: HEAD `51bb8131723baa822c1ce65ce1a981722d6c90f6`,
staged content tree `37e0b0a4e767dd18c18aa7b3f15a965ca4ff43b5`.
Subsequent guard implementation belongs to its existing writers; use their
qualified integrated result, not the historical tree, for execution.

Required prerequisites are the complete current versions of:

- [Common provider cost](2026-10-22-composer-common-provider-cost.md), Tasks1–6,
  including typed `CommonProviderCostRequest`, original eligible V1 scope,
  actual settlement before charge, unknown-cost503 and actual-role tests.
- [Common mutation authority](2026-10-22-composer-common-mutation-authority.md),
  A1–A5 and affected gates: request-start ceiling, current locked predicate,
  actual refusal publication, complete repeated cancellations and original
  failures, SQLite/PostgreSQL and filesystem/Stop lock ordering.
- [Independent plan review](../../composer-common-preservation-plan-review.md)
  is permission to implement those plans, not evidence that they are complete.
  Root must explicitly release H2.3 source writes after their implementation
  and qualification. Do not start this packet while a shared-source owner is
  writing its dependencies.

No continuation runtime is introduced here. The companions own their future
frame amendments. Preserve direct `pipeline_planner.plan_pipeline`, all direct
discovery/budget/cost/transport tests, nominal chronology/context types and
current candidate, credential, source-proof and prompt-guard fixes. No private
palette on public entry, tutorial branch, lexical routing or server graph.

Compatibility: new empty-graph ordinary requests use common direct graph/P4
publication in auto mode. They no longer manufacture the adapter's intermediate
auto proposal or `pipeline_commit_intent`. Explicit approval still creates a
canonical pending proposal with exact base/hash. The qualified authority
companion preserves both initial/current trust agreement and the late locked
publication predicate. It also supplies actual common auto P3/P4 cancellation
coverage; lower-level proposal finish-once guarantees stay at their own ports.
No data migration, reset, downgrade or rewriting of historical graph/audit
records is authorized.

Saved diagnostic boundary: `envelope-tool-test-repair.md` sections169–507 and
its native JSON/XML/logs measured268 registered cases/24 functions, selecting24
representatives:18 failed/6 passed;244 parameters unexecuted. Service source
moved during that diagnostic. It is diagnosis, not qualification. In particular
fixture exhaustion, wrong-model assertions, obsolete `emit_pipeline_proposal`
and repeated fixed call IDs do not establish a production500 contract.

## Task 1 — Remove only the unreachable production adapter

**Files:** delete `src/elspeth/web/composer/planning_application.py`; modify
only the adapter import/constructor assignment in `service.py`, the obsolete
attribution in `prompts.py`, and the individual adapter app-state assignments in
`src/elspeth/web/app.py` and `tests/integration/web/composer/parity/conftest.py`.

**Interfaces:** consumes the qualified common cost/mutation prerequisites and
Tasks2–8, including Task2's same-writer malformed-hash control; produces the
same supported common Composer and direct `pipeline_planner.plan_pipeline`
interfaces with no installed private adapter. Root alone releases source
writes, reserves native capacity and stages/commits the reviewed paths.

- [ ] Complete and qualify Tasks2–8 first. Use Task9's existing environment
  setup to create its ordinary `test_private_planner_retirement.py` unit and run
  its RED command **before this task's deletion**. Require native exit1 at the
  exact private-file-exists assertion on an otherwise qualified guard tree;
  collection, import or fixture failures do not satisfy this step.
- [ ] Root confirms actual remaining adapter references and helper/import
  closure with the existing controlled authority API. Preserve direct-library
  exports, current provider/guard/custody/P4 repairs and every other owner.
  This task does not authorize a new scanner, test runner or framework.
- [ ] Delete `planning_application.py` in full. Delete this exact import:

```diff
-from elspeth.web.composer.planning_application import PlanningApplication
```

- [ ] Delete exactly this constructor assignment, retaining both neighboring
  assignments and every argument to every other constructor:

```diff
         self._schema_disclosure = SchemaDisclosureTracker()
-        self._planning_application = PlanningApplication(
-            sessions_service=sessions_service,
-            policy_context=self._policy_context,
-            chargeable_admission=self._chargeable_admission,
-            preflight=self._preflight,
-            schema_disclosure=self._schema_disclosure,
-            settings=settings,
-            availability=self._availability,
-            planner_dialect=self._planner_dialect,
-            hatch_dialect=self._hatch_dialect,
-            advisor_provider=self._advisor_provider,
-            composer_skill_text=self._composer_skill_text,
-            session_engine=session_engine,
-            secret_service=secret_service,
-            secret_wiring_policy=self._secret_wiring_policy,
-            endpoint_base_url=self._endpoint_base_url,
-            endpoint_api_key=self._endpoint_api_key,
-            advisor_endpoint_base_url=self._advisor_endpoint_base_url,
-            advisor_endpoint_api_key=self._advisor_endpoint_api_key,
-            advisor_checkpoint=self._advisor_checkpoint,
-            completion=self._completion,
-            composer_prompt_guard_mount=self._provider_gateway.prompt_guard_mount,
-            advisor_prompt_guard_mount=self._advisor_prompt_guard_mount,
-        )
```

- [ ] Delete the exact individual app/parity assignments:

```diff
-    app.state.planning_application = app.state.composer_service._planning_application
```

```diff
-    app.state.planning_application = composer._planning_application
```

- [ ] Replace only the obsolete file attribution in
  `project_server_owned_option_metadata`'s docstring, retaining the actual
  `plan_pipeline` argument name and the projector body:

```diff
-    ``provider_current_state`` in ``planning_application.py``) so a read-modify-write over
+    ``provider_current_state`` in the direct planner library) so a read-modify-write over
```

H2.2 already removed its unused authorship import. Do not reapply H2.1/H2.2
or remove nominal direct-library exports. Routine unused imports caused by
these exact deletions may be removed; no suppressions or signature edits.

- [ ] Run Task9's ordinary retirement GREEN command, then its affected modules,
  relevant whole-tree gates and combined qualification. The pin must exit0;
  actual direct-library/common/provider/guard behavior is qualified separately.
  Root alone applies producer-derived gate updates and normal Git hygiene.

## Task 2 — Exact mixed-unit retirement and surviving writer/admission code

**Files:** the exact test modules below, plus the ordinary structural regression
created by Task9. No production writer changes belong to this task.

**Interfaces:** consumes the current real-sessions and acquired COMPOSE-context
fixtures, the qualified Task3 SDK helper, Tasks4–8 replacement oracles, and the
existing `_persist_tool_invocations`/`_persist_llm_calls` writer signatures.
Produces adapter-free mixed modules while retaining the original meaningful
audit, admission, privacy, cancellation and direct-library contracts.

- [ ] Port the complete malformed executor-result hash unit below **before**
  retiring its adapter caller. Replace
  `test_current_planner_persistence_rejects_malformed_bound_content_hash` in
  `tests/unit/web/composer/test_compose_loop_persistence.py` with this complete
  definition. Keep its original invocation construction, canary and exact
  error oracle; the second half is the positive continuation through the same
  real writer and acquired context:

```python
@pytest.mark.asyncio
async def test_surviving_tool_writer_rejects_malformed_bound_content_hash(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
    from elspeth.web.sessions.routes._helpers import _persist_tool_invocations

    arguments = {
        "source": {"plugin": "null", "on_success": "rows", "on_validation_failure": "discard"},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    arguments_canonical = canonical_json(arguments)
    authority_arguments_canonical = composer_authority_canonical_json(arguments)
    result_canonical = canonical_json(
        {
            "success": True,
            "validation": {
                "is_valid": True,
                "errors": [],
                "warnings": [],
                "suggestions": [],
                "semantic_contracts": [],
                "graph_repair_suggestions": [],
            },
            "affected_nodes": [],
            "version": 1,
            "pipeline_content_hash_schema": "composer.pipeline-dispatch-result.v1",
            "pipeline_content_hash": "RAW_CURRENT_PLANNER_HASH_/private/operator/path_sk-secret",
        }
    )
    invocation = ComposerToolInvocation(
        tool_call_id="call_current_planner_hash_canary",
        tool_name="set_pipeline",
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode()).hexdigest(),
        result_canonical=result_canonical,
        result_hash=hashlib.sha256(result_canonical.encode()).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=0,
        version_after=1,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
        authority_arguments_canonical=authority_arguments_canonical,
        authority_arguments_hash=hashlib.sha256(authority_arguments_canonical.encode()).hexdigest(),
    )

    sessions = composer_service_with_real_sessions._sessions_service
    assert sessions is not None
    session_uuid = UUID(result_session_id)
    async with acquire_compose_context(sessions, session_uuid) as compose_context:
        with sessions._engine.connect() as connection:
            before = [
                dict(row)
                for row in connection.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == result_session_id)
                    .order_by(chat_messages_table.c.sequence_no)
                ).mappings()
            ]
        with pytest.raises(AuditIntegrityError, match="content hash is malformed"):
            await _persist_tool_invocations(
                sessions,
                session_uuid,
                (invocation,),
                None,
                plugin_crash_pending=False,
                session_operation_context=compose_context,
            )
        with sessions._engine.connect() as connection:
            refused = [
                dict(row)
                for row in connection.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == result_session_id)
                    .order_by(chat_messages_table.c.sequence_no)
                ).mappings()
            ]
        assert refused == before

        # Syntactic projection control; this is not a computed graph digest.
        valid_hash = "0123456789abcdef" * 4
        valid_result = json.loads(result_canonical)
        valid_result["pipeline_content_hash"] = valid_hash
        valid_result_canonical = canonical_json(valid_result)
        valid = replace(
            invocation,
            result_canonical=valid_result_canonical,
            result_hash=hashlib.sha256(valid_result_canonical.encode()).hexdigest(),
        )
        (binding,) = await _persist_tool_invocations(
            sessions,
            session_uuid,
            (valid,),
            None,
            plugin_crash_pending=False,
            session_operation_context=compose_context,
        )
        with sessions._engine.connect() as connection:
            persisted = [
                dict(row)
                for row in connection.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == result_session_id)
                    .order_by(chat_messages_table.c.sequence_no)
                ).mappings()
            ]
        assert persisted[: len(before)] == before
        private_row, public_row = persisted[len(before) :]
        assert private_row["role"] == public_row["role"] == "audit"
        assert private_row["writer_principal"] == public_row["writer_principal"] == "compose_loop"
        assert private_row["composition_state_id"] is public_row["composition_state_id"] is None
        assert public_row["sequence_no"] == private_row["sequence_no"] + 1
        (private_envelope,) = private_row["tool_calls"]
        evidence = ComposerDiagnosticEvidence.from_envelope(
            private_envelope,
            expected_session_id=result_session_id,
            expected_operation_id=compose_context.fence.operation_id,
            expected_tool_call_id=invocation.tool_call_id,
        )
        assert evidence.operation_epoch == compose_context.fence.operation_epoch
        assert evidence.tool_name == "set_pipeline" and evidence.status == "success"
        assert evidence.arguments.disposition == "retained"
        assert evidence.arguments.content == arguments_canonical
        assert evidence.authority_arguments is not None
        assert evidence.authority_arguments.content == authority_arguments_canonical
        assert evidence.result.disposition == "retained"
        assert evidence.result.content == valid_result_canonical
        assert evidence.result.retained_sha256 == valid.result_hash

        (public_envelope,) = public_row["tool_calls"]
        assert public_envelope["_kind"] == "audit"
        public_invocation = public_envelope["invocation"]
        public_result = json.loads(public_row["content"])
        assert public_result["pipeline_content_hash_schema"] == "composer.pipeline-dispatch-result.v1"
        assert public_result["pipeline_content_hash"] == valid_hash
        assert public_invocation["result_canonical"] == public_row["content"]
        assert public_invocation["result_hash"] == hashlib.sha256(public_row["content"].encode()).hexdigest()
        assert public_invocation["arguments_hash"] == hashlib.sha256(
            public_invocation["arguments_canonical"].encode()
        ).hexdigest()
        assert type(binding) is PipelineDispatchAuditBinding
        assert binding == PipelineDispatchAuditBinding.from_persisted_envelope(public_envelope)
        assert binding.tool_call_id == invocation.tool_call_id and binding.tool_name == "set_pipeline"
        assert binding.status is ComposerToolStatus.SUCCESS
        assert binding.result_hash == public_invocation["result_hash"]
        canary = json.loads(result_canonical)["pipeline_content_hash"]
        assert all(
            canary not in row["content"] and canary not in json.dumps(row["tool_calls"])
            for row in (private_row, public_row)
        )
```

The named success control is this unit's positive continuation: only the result
hash value and its matching canonical/result digest change; the actual writer,
context, invocation arguments and status stay the same. Its supported source
chain is `_helpers._persist_tool_invocations` →
`audit_storage.redacted_tool_invocation_content_and_envelope` →
`PipelineDispatchAuditBinding.from_persisted_envelope`, followed by the real
atomic public/private write. The controlled valid value proves syntactic
projection and persistence, not executor graph-digest computation. Keep the
separate actual common candidate/finalizer/P4 tests unchanged.

- [ ] Root runs this exact unit under Task9's existing serial native-exit
  discipline before any adapter deletion. Require native exit0, meaning both
  the original exact `AuditIntegrityError`/complete SQL no-write assertion and
  the same-writer positive SQL/binding assertions executed. An unrelated
  constructor/argument/fixture error is a failure to fix, not the required
  negative-control result.
- [ ] Delete each complete definition and its decorators in this table, not its
  mixed module. Delete imports/helpers only after root's existing controlled
  reference check proves exclusive consumption. This is an exact edit selector,
  not a regex-derived coverage count:

| Module under `tests/` | Exact retired definition |
| --- | --- |
| `unit/web/composer/test_audit_cohort_settlement.py` | `test_planner_success_path_cohort_is_all_or_nothing_at_every_write_index`; `_plan_and_stage_empty`; `test_planner_decline_path_cohort_is_all_or_nothing_at_every_write_index`; `test_planner_cohort_cancelled_mid_settlement_lands_whole_and_creates_no_proposal` |
| `unit/web/composer/test_compose_loop_persistence.py` | `test_planner_audit_cohort_carries_the_prose_the_planner_refused_to_publish` only; the malformed-hash unit is ported above, not discarded |
| `unit/web/composer/test_private_evidence_persistence.py` | `test_received_and_accepted_one_shot_candidates_settle_in_actual_planner_cohort` |
| `unit/web/composer/test_planner_graph_recovery.py` | `test_application_handoff_defers_audit_and_omits_optional_candidate_capture` |
| `unit/web/composer/test_provider_context_planner_integration.py` | `test_application_projection_boundary_preserves_parts_and_complete_byte_admission` |
| `unit/web/composer/test_runtime_preflight_pending_review_verification.py` | class `TestPlannerPreviewPreflightStructuralCallback` only |
| `unit/web/composer/test_required_control_autowire.py` | class `TestServiceFinalizerFactory` only |
| `unit/web/composer/test_visible_tool_event_persistence.py` | `test_actual_planner_tools_and_public_call_gaps_survive_joined_cohort_reload` |
| `unit/web/sessions/test_pipeline_rejection_callers.py` | `test_actual_planner_creation_cancel_retains_nonzero_child_rejection_scope` only after Task7's actual lower-level replacement and the remaining prerequisites qualify |

- [ ] After root's existing controlled canonical reference authority proves exclusive consumption, remove only the authorized unused helper/import literal chunks caused by the exact retirements above. In `test_audit_cohort_settlement.py`, that conditional helper set is `_seed_user_message`, `_proposal_count`, `_InsertInjector`, `_PLANNER_COHORT`, `_green_preflight`, `_plan`, and `_FakePreferences`. Remove the obsolete section2 private per-write-index header only together with its retired helper group. Replace the complete module docstring with the exact surviving-contract description below. Preserve every retained test body, `_GatedInsert`, common fault-firing/SQL checks, the `stable_hash` import and both actual provider-context hash assertions byte-exact. Keep the section3 comment and numbering unchanged; do not reword other comments or perform format cleanup. Canonical authority and native qualification remain separate gates; the literal cleanup packet does not establish zero use, collection parity or passing tests.

```python
"""Common audit-cohort settlement and exactly-once draining.

A tool turn already persisted inline must not drain again through a route
handler. Buffered public/private audit rows settle in one transaction.

A cancelled cohort drains through the shield and lands whole; a mid-cohort
failure leaves no partial rows while the original cancellation propagates.
"""
```

Remove only this obsolete section header with its authorized private helper group:

```diff
-# ---------------------------------------------------------------------------
-# 2. per-write-index fault injection on the planner cohort
-# ---------------------------------------------------------------------------
-
```

After the same reference check, remove only the unused imports below, preserving every other name on a shared import line. These names are consumed by the retired definitions/helper group; retain them if a surviving or imported consumer is found.

| Module under `tests/unit/web/` | Exact conditional import names |
| --- | --- |
| `composer/test_audit_cohort_settlement.py` | `AbsentBase`, `AsyncMock`, `AuditIntegrityError`, `BufferingRecorder`, `CompositionState`, `PipelineMetadata`, `PipelinePlanResult`, `PipelineProposal`, `PlannerDeclined`, `SessionOperationKind`, `ValidationReadiness`, `ValidationResult`, `composition_proposals_table`, `patch`, `uuid4` |
| `composer/test_planner_graph_recovery.py` | `BufferingRecorder`, `GraphRecoveryIntent`, `PluginAvailabilitySnapshot`, `PolicyCatalogView`, `PrivatePlannerCandidate`, `_composer_service_with_session`, `_empty_state`, `_make_settings`, `create_catalog_service`, `fenced_operation_context`, `uuid4` |
| `composer/test_provider_context_planner_integration.py` | `BufferingRecorder`, `PipelinePlannerError`, `ProviderInvocationOwner`, `RequiredAuthorityKind`, `RequiredWorkAuthority`, `RequiredWorkBinding`, `RequiredWorkCoordinator`, `RequiredWorkRole`, `_PlannerPreviewPreflightCallbacks`, `uuid4` |
| `sessions/test_pipeline_rejection_callers.py` | `AdvisorCheckpointOwner`, `BufferingRecorder`, `ComposerAvailability`, `ComposerChargeableAdmission`, `ComposerPreflight`, `CompositionCompletion`, `MagicMock`, `PipelinePlanResult`, `PlanningApplication`, `PluginPolicyContextFactory`, `SchemaDisclosureTracker`, `SecretWiringPolicy`, `ToolContractDialect`, `_initial_composition_state`, `composition_proposals_table` |

Preserve direct-library context/recovery and every remaining canonical/manual required-child caller. Apply the `_PlannerPreviewPreflightCallbacks` import deletion once; it is already named above. Do not duplicate alternate preparation edits.

The historical session-worker spelling
`test_actual_planner_decline_review_shares_call_budget_and_saved_terminal` has
already been superseded and is **not** a current deletion selector. Preserve
the existing `test_actual_planner_decline_end_failure_preserves_audit_and_never_publishes`
and all other actual common worker/END/cost/required-custody tests.

- [ ] Delete `tests/unit/web/composer/test_planner_stage_runtime_preflight.py`
  in full as the obsolete private adapter's staging/announcement contract. Its
  staging units call the adapter; its final literal unit checks only that
  adapter contract's `PIPELINE_STAGED_*` constants. This does not authorize
  deleting shared protocol constants or common preflight/runtime-review,
  custody/atomicity/guard tests. Preserve root's P4 repairs.
- [ ] In `TestAutoWireIdempotence.test_every_no_op_path_is_identity_preserving`,
  remove only the trailing block beginning
  `# REQUIRED posture through the service finalizer factory` through
  `assert finalize(frozen) is frozen`. Keep direct `wire_required_controls`
  identity controls.
- [ ] In `test_nonempty_compose_sends_current_amended_parts_through_real_provider_boundary`,
  remove exactly the following patch and assertion. All real SDK, current
  amended prompt parts and no-effects assertions remain:

```diff
-        patch.object(
-            service._planning_application,
-            "_plan_and_stage_empty_pipeline",
-            autospec=True,
-            side_effect=AssertionError("nonempty graph selected empty planner"),
-        ) as empty_planner,
```

```diff
-    empty_planner.assert_not_called()
```

Remove the now-unused `_PlannerPreviewPreflightCallbacks` import in that
module. Preserve its direct-library projection tests and actual byte-bound
owners; diagnostic capture alone is not physical provider admission proof.

- [ ] In `test_chargeable_admission.py`, remove only the obsolete facade
  `_sessions_service_optional`/`_chargeable_admission` assignments from
  `test_public_entry_refuses_before_provider_work`,
  `test_allowed_diagnostics_reaches_provider`, and
  `test_concurrent_diagnostics_restore_separate_session_scopes`. In the public
  refusal unit remove only its private `plan_pipeline` patch and
  `planner.assert_not_called()`.
- [ ] Preserve the complete current
  `test_same_valid_request_reaches_common_or_advisor_entry_only_when_admitted`
  unchanged, including both entry/allowed decorators, the exact nominal
  `ComposerSessionPreferencesRecord`, matching `UUID(_SESSION_ID)`,
  autospecced preference getter, common/Advisor interception, operation list
  and final `provider_quota._SCOPE.get() is None` assertion. A1 captures the
  initial preference before admission/common entry; do not restore an older
  template over current prerequisite-owned fixtures.

This is logical admission/scope coverage. Preserve separate actual SDK
`test_rootless_admission_controls_actual_provider_transition`, auto-title
admission, diagnostic/concurrent scope and H2.1 actual-provider coverage. The
logical unit does not qualify physical provider custody or publication.

- [ ] Before removing `_PlannerAuditHost`, retire only the complete Task2-selected
  `test_visible_tool_event_persistence.py::test_actual_planner_tools_and_public_call_gaps_survive_joined_cohort_reload`
  after its current owner tests in Task9 qualify. Its local `_PlannerAuditHost`
  import and call consume the shared host and disappear with that complete
  definition. Preserve the complete
  `test_public_response_gap_has_actual_fence_and_call_binding` and its local
  `_call` import, plus all other common visible-tool storage, privacy,
  rollback, source-binding and malformed-evidence controls in that module.
  Root repeats the existing controlled reference check on the settled candidate
  and requires no remaining external `_PlannerAuditHost` consumer before packet
  acceptance. An atomic reviewed packet containing this consumer retirement and
  the summary edit is also permitted; do not release a summary-only host deletion
  while the consumer remains.

- [ ] In `test_llm_call_audit_summary.py`, remove `_PlannerAuditHost`,
  `TestAllThreeDrainSitesShareOneProjection`,
  `test_freeform_planner_persistence_atomically_interleaves_physical_calls_and_semantic_attempts`
  and `_attempt`. Remove exclusively unused planner-attempt/PlanningApplication/
  `replace` imports. Keep `_call`, `_compose_context`, the capturing service,
  their consumed imports, and every literal legacy/key-order/finish-reason unit.
  Add the actual surviving writer test:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", (None, *ROUTINE, *ABNORMAL, "provider_specific_halt"))
async def test_surviving_writer_uses_shared_summary(finish_reason: str | None) -> None:
    service = _CapturingSessionService()
    session_id = uuid4()
    call = _call(finish_reason=finish_reason)
    await _persist_llm_calls(
        cast(SessionServiceProtocol, service), session_id, (call,), None,
        plugin_crash_pending=False,
        session_operation_context=_compose_context(session_id),
    )
    (saved,) = service.messages
    assert saved.content == llm_call_audit_summary(call)
```

Replace that module's consolidation paragraph with:

```python
"""The surviving LLM audit writer uses the shared operator summary.

Literal legacy JSON pins preserve field order and bytes independently of the
writer test. Routine reasons are omitted; abnormal and unknown reasons remain
visible. The actual surviving writer is exercised with each reason family.
"""
```

- [ ] Root runs the complete affected mixed modules named in Task9, including
  the ported malformed-hash unit, unchanged logical/physical admission, literal
  summary pins, and actual shared-writer summary test. Keep the structural
  retirement pin separate from this pre-retirement selection: its expected RED
  precedes Task1, and its GREEN follows Task1. Require recorded native exit0
  for the mixed-module selection and read the complete results. Keep direct
  library retry/attempt-order, context/recovery, common P4/END and Task7
  cancellation controls; source references in the ledger are not passing
  evidence.
- [ ] Review the exact changed definitions plus every touched file; root alone
  stages the reviewed packet after its fresh SPEC/Astra gates and normal
  branch-safety/pathspec checks. No private logs or plan-preparation files are
  staged. Task1's adapter deletion remains a later root-controlled step.

## Task 3 — Reusable finite physical SDK and audit assertions

**Create:** `tests/helpers/composer_common_sdk.py`. This is a response script
and assertion helper, not a second app/provider/audit implementation. Actual
Composer, guard, admission, quota, SQL, EARLY and END bodies remain live.

- [ ] Add the complete module below before changing the integration units.

```python
"""Finite physical SDK responses for common Composer integration tests."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select

from elspeth.contracts.composer_content import CompleteValueManifest
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.recovery_controls import recovery_context_message
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from tests.helpers.advisor_review import parse_advisor_publication_review


@dataclass
class Function:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: Function


@dataclass
class Message:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Choice:
    message: Message


@dataclass
class Response:
    choices: list[Choice]
    usage: Mapping[str, object]
    model: str
    id: str
    provider: str | None = None


@dataclass(frozen=True)
class Step:
    role: Literal["primary", "early", "end", "hint"]
    content: str | None = None
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None
    failure: BaseException | None = None
    publication: Literal["ADMIT", "WITHHOLD"] = "ADMIT"
    verdict: Literal["CLEAN", "FLAGGED"] = "CLEAN"
    malformed: bool = False
    before_return: Callable[[], Awaitable[None]] | None = None


class CommonSDK:
    def __init__(self, *, request: str, candidate: str, steps: tuple[Step, ...], saved: int = 0):
        self.request = request
        self.candidate = candidate
        self.steps = steps
        self.saved = saved
        self.requests: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.phase: str | None = None
        self.composer = None

    def bind(self, composer, monkeypatch):
        self.composer = composer
        actual = composer._advisor_checkpoint._run_advisor_checkpoint

        async def observe(**kwargs):
            assert self.phase is None
            self.phase = kwargs["phase"]
            self.phases.append(self.phase)
            try:
                return await actual(**kwargs)
            finally:
                self.phase = None

        monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe)

    async def __call__(self, **kwargs):
        ordinal = len(self.requests)
        assert ordinal < len(self.steps), "Unscripted physical SDK transition"
        step = self.steps[ordinal]
        self.requests.append(deepcopy(kwargs))
        assert self.composer is not None
        if step.role == "primary":
            assert kwargs["model"] == self.composer._settings.composer_model
            assert self.phase is None
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
            assert any(row["role"] == "user" and row["content"] == self.request for row in kwargs["messages"])
        else:
            assert kwargs["model"] == self.composer._settings.composer_advisor_model
            assert kwargs.get("tools") is None
            if step.role in {"early", "end"}:
                assert self.phase == step.role and "response_format" in kwargs
            else:
                assert self.phase is None
        if step.before_return is not None:
            await step.before_return()
        if step.failure is not None:
            raise step.failure
        content = step.content
        if step.role in {"early", "end"}:
            payload = {"verdict": step.verdict, "category": "other", "steps": [],
                       "findings": "Review uses the actual request and saved effects.", "note": None}
            if step.role == "end":
                review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
                assert review["current_request"] == self.request
                assert review["candidate_reply"] == self.candidate
                assert review["saved_effects"]["saved_revision_count"] == self.saved
                assert review["saved_effects"]["net_changed"] is bool(self.saved)
                payload["publication_verdict"] = step.publication
            content = json.dumps(payload)
        calls = []
        if step.tool_name is not None:
            assert step.arguments is not None
            calls.append(ToolCall(f"common-tool-{ordinal + 1}", Function(step.tool_name, json.dumps(step.arguments))))
        return Response(
            choices=[] if step.malformed else [Choice(Message(content, calls))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            model="provider/common-v1", id=f"common-sdk-{ordinal + 1}", provider="DeepInfra",
        )

    def assert_done(self):
        assert len(self.requests) == len(self.steps)
        assert self.phases == [step.role for step in self.steps if step.role in {"early", "end"}]


def assert_initial_wire(request, *, text: str, state: CompositionState, session_id: str):
    messages = request["messages"]
    assert any(row["role"] == "user" and row["content"] == text for row in messages)
    state_rows = [row["content"] for row in messages if row["role"] == "user"
                  and row["content"].startswith(STATE_CONTEXT_PREFIX + "\n")]
    (rendered,) = state_rows
    view = json.loads(rendered.split("\n", 1)[1])["current_state"]
    assert set(view) == {"schema_version", "data", "content_manifest"}
    assert view["schema_version"] == "composer.bounded-view.v1"
    manifest = CompleteValueManifest.from_payload(view["content_manifest"])
    assert manifest.scope.session_id == UUID(session_id)
    assert manifest.scope.composition_content_hash == composition_content_hash(state)
    if not state.sources and not state.nodes and not state.outputs:
        assert manifest.values == ()
        assert view["data"]["sources"] == {} and view["data"]["nodes"] == []
        assert view["data"]["edges"] == [] and view["data"]["outputs"] == []
    names = {entry["function"]["name"] for entry in request["tools"]}
    assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
    assert all(not any(key.startswith("_elspeth_") for key in row) for row in messages)


def assert_route_context(request, call, *, session_id: str, user_id: str):
    recovery = call.kwargs["recovery_context"]
    retained = call.kwargs["retained_operation_context"]
    assert type(recovery) is GraphRecoveryContext
    assert type(retained) is RetainedOperationContext
    retained.validate_for(session_id=session_id, actor_user_id=user_id)
    assert request["messages"][-2]["content"] == recovery_context_message(recovery)
    assert request["messages"][-1]["content"] == retained.provider_message()


def assert_actual_audits(engine, requests, statuses, *, session_id: str):
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == session_id)
                            .order_by(chat_messages_table.c.sequence_no)).mappings().all()
        attempts = conn.execute(select(quota_provider_attempts_table)
                                .where(quota_provider_attempts_table.c.session_id == session_id)).mappings().all()
    calls = [row["tool_calls"][0]["call"] for row in rows
             if row["role"] == "audit" and row["tool_calls"] and row["tool_calls"][0]["_kind"] == "llm_call_audit"]
    assert len(calls) == len(requests) == len(statuses)
    assert len({call["call_id"] for call in calls}) == len(calls)
    attempts_by_id = {attempt["attempt_id"]: attempt for attempt in attempts}
    assert set(attempts_by_id) == {call["call_id"] for call in calls}
    private_requests = {}
    for row in rows:
        envelopes = row["tool_calls"] or ()
        if not envelopes or envelopes[0]["_kind"] != "composer_diagnostic_evidence":
            continue
        assert len(envelopes) == 1 and row["role"] == "audit" and row["writer_principal"] == "compose_loop"
        envelope = envelopes[0]
        if envelope["evidence"]["status"] != "provider_request":
            continue
        call_id = envelope["evidence"]["tool_call_id"]
        attempt = attempts_by_id[call_id]
        evidence = ComposerDiagnosticEvidence.from_envelope(
            envelope, expected_session_id=session_id, expected_operation_id=attempt["operation_id"],
        )
        assert evidence.operation_epoch == attempt["operation_epoch"]
        assert call_id not in private_requests and evidence.arguments.content is not None
        private_requests[call_id] = json.loads(evidence.arguments.content)
    assert set(private_requests) == set(attempts_by_id)
    for call, request, status in zip(calls, requests, statuses, strict=True):
        assert call["status"] == status
        assert call["model_requested"] == request["model"]
        assert call["messages_hash"] == stable_hash(request["messages"])
        tools = request.get("tools")
        assert call["tools_spec_hash"] == (None if tools is None else stable_hash(tools))
        private = private_requests[call["call_id"]]
        assert private["messages"] == request["messages"] and private["tools"] == tools
        assert call["planner_call_ordinal"] is None
        if status in {"timeout", "api_error", "cancelled"}:
            assert call["model_returned"] is None
            assert call["prompt_tokens"] is None and call["completion_tokens"] is None
            assert call["provider_cost"] is None and call["provider_cost_source"] == "not_available"
    return calls
```

The private request comparison uses the actual admitted-attempt operation
identity; it does not trust the private envelope as its own expected binding.
The typed cost-request amendment remains owned by its companion: this helper
captures the actual full SDK request and does not replace that policy view.

`CommonSDK` is an async callable fixture. Give `AsyncMock` its bound async
method with `side_effect=script.__call__` so awaiting the mock returns the
finite response rather than another coroutine.

## Task 4 — Additional closed Advisor evidence caller

**Modify:** `src/elspeth/web/composer/advisor_checkpoint.py` only the existing
`_call_advisor_with_audit_body` capture seam, and
`tests/unit/web/composer/test_advisor_checkpoint.py` only the
`test_audit_only_request_rejection_paths_retain_closed_evidence` definition.

The safe rejection reference must resolve to the private failure written by
actual provider settlement. Retain the caught SDK error in the existing call
record builder with these three additions; the existing bounded diagnostic
capture and settlement write the private record:

```diff
--- a/src/elspeth/web/composer/advisor_checkpoint.py
+++ b/src/elspeth/web/composer/advisor_checkpoint.py
@@ -531,6 +531,7 @@
         response_metadata: _AdmittedLLMProviderMetadata | None = None
         error_class: str | None = None
         error_message: str | None = None
+        failure_to_capture: Exception | None = None
         kwargs: dict[str, Any] = build_advisor_request_options(
             model=advisor_model,
             temperature=self._settings.composer_temperature,
@@ -636,6 +637,7 @@
             error_message = type(exc).__name__
             raise
         except OpenAIError as exc:
+            failure_to_capture = exc
             failure = classify_provider_failure(exc)
             status = failure.audit_status if failure is not None else ComposerLLMCallStatus.API_ERROR
             error_class = type(exc).__name__
@@ -690,6 +692,7 @@
                             error_message=error_message,
                             credential_surface="composer_advisor_response",
                             provider_custody=provider_custody,
+                            failure=failure_to_capture,
                             private_request=private_request,
                         )
                         if provider_custody is not None:
```

Retain both reason codes and EARLY semantics. Replace the complete test
definition with this code, retaining its existing fixtures. Wire the real
InterpretationSurfacing dependency locally because the shared END gate reads
the session's pending interpretation events:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["early", "end"])
@pytest.mark.parametrize("reason_code", ["invalid_request", "context_length_exceeded"])
async def test_audit_only_request_rejection_paths_retain_closed_evidence(
    make_service, physical_advisor, nonempty_state, empty_state, monkeypatch, entry, reason_code
):
    from litellm.exceptions import BadRequestError

    from elspeth.contracts.provider_rejection import SafeRequestRejection
    from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage
    from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord, AdvisorTerminalPublication
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, diagnostic_evidence_identity
    from elspeth.web.composer.discovery_cache import RuntimePreflightCache

    service = make_service()
    request = "Review the fixed synthetic pipeline."
    fenced = physical_advisor.bind(service, request=request)
    service._interpretation_surfacing._sessions_service = physical_advisor.sessions
    failure = BadRequestError("PRIVATE_PROVIDER_BODY", model="advisor", llm_provider="openai", body={"code": reason_code})
    calls = _install_budget_advisor_transport(monkeypatch, [failure])
    recorder = make_recorder()
    with prompt_guard_user_scope(""):
        if entry == "early":
            messages = []
            ran = await service._advisor_checkpoint._maybe_run_early_checkpoint(
                state=nonempty_state, prev_state=empty_state, llm_messages=messages,
                recorder=recorder, **fenced,
            )
            assert ran and messages == []
        else:
            evidence = TurnEvidence(
                session_id=fenced["session_id"], session_operation_context=fenced["session_operation_context"],
                request_id="audit-only-rejection", request=request,
                initial_state_id=None, initial_state=empty_state,
            )
            outcome = await service._completion._evaluate_terminal_no_tool_advisor_gate(
                state=empty_state, turn_evidence=evidence, current_state_id=None,
                assistant_message=_AdmittedAssistantMessage(content="Cannot assemble that pipeline."),
                llm_messages=[], recorder=recorder, progress=None,
                advisor_checkpoint_passes_used=0, repair_turns_used=0,
                persisted_assistant_message_id=None, persisted_assistant_content=None,
                persisted_tool_call_turn=False, allow_repair_continue=False,
                runtime_preflight=None, user_message=request, user_id="alice",
                runtime_preflight_cache=RuntimePreflightCache(), initial_version=empty_state.version,
                session_scope=fenced["session_id"], plugin_snapshot=None,
                deadline=asyncio.get_running_loop().time() + 60, **fenced,
            )
            assert outcome.action == "return" and outcome.result is not None
            publication = outcome.result.advisor_terminal_publication
            assert publication is not None and publication.reason == "request_rejected"
    assert len(calls) == len(recorder.llm_calls) == 1
    with physical_advisor.engine.connect() as connection:
        rows = connection.execute(select(chat_messages_table)).all()
    envelopes = [envelope for row in rows for envelope in (row.tool_calls or ())]
    kind = "advisor_checkpoint_pass_audit" if entry == "early" else "advisor_terminal_publication_audit"
    retained = [envelope for envelope in envelopes if envelope["_kind"] == kind]
    assert len(retained) == 1
    payload = retained[0]["pass" if entry == "early" else "publication"]
    details = SafeRequestRejection.from_wire(payload["request_rejection"])
    assert details.reason_code == reason_code and details.reason_source == "sdk_body"
    assert details.diagnostic_reference == diagnostic_evidence_identity("provider-failure", recorder.llm_calls[0].call_id)
    decoded = AdvisorCheckpointPassRecord.from_dict(payload) if entry == "early" else AdvisorTerminalPublication.from_dict(payload)
    assert decoded.request_rejection == details
    public = [envelope for envelope in envelopes if envelope["_kind"] != "composer_diagnostic_evidence"]
    assert "PRIVATE_PROVIDER_BODY" not in json.dumps(public)
    private = [ComposerDiagnosticEvidence.from_envelope(
        envelope, expected_session_id=fenced["session_id"],
        expected_operation_id=fenced["session_operation_context"].fence.operation_id,
    ) for envelope in envelopes if envelope["_kind"] == "composer_diagnostic_evidence"]
    failures = [item for item in private if item.status == "provider_failure"]
    assert len(failures) == 1 and failures[0].result.content is not None
    assert "PRIVATE_PROVIDER_BODY" in failures[0].result.content
    assert all("PRIVATE_PROVIDER_BODY" not in (row.content or "") for row in rows)
```

The common terminal owner builds the actual publication review and records its
non-retryable request-rejected outcome; no private continuation budget or CLEAN
verdict is injected. END has both a checkpoint pass and terminal-publication
receipt; only the terminal receipt is selected for its terminal assertion.

## Task 5 — Public HTTP failures, publication and interpretation

**Modify:** `tests/integration/web/composer/test_freeform_planner_failure_translation.py`.
Retain `test_provider_can_answer_without_graph_mutation_for_any_request_wording`
unchanged at its reviewed4436... body. Retain every parameter and body of the
four lower-level progress/policy/DTO functions. The common cost companion adds
tests to this same module; preserve them and its `_build_app` settings keywords.

- [ ] Add imports:

```python
from tests.helpers.composer_common_sdk import (
    CommonSDK, Step, assert_actual_audits, assert_initial_wire, assert_route_context,
)
```

- [ ] Replace only `_build_app`'s obsolete routing docstring with:

```python
    """Real common Composer, authenticated routes, durable worker and session store.

    Only the physical SDK is scripted. Guard, admission, provider settlement,
    tool execution, required audit and public outcome projection remain real.
    """
```

- [ ] Keep all20 wording decorators; replace the complete timeout function:

```python
def test_every_request_wording_uses_common_loop_and_audits_provider_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str,
) -> None:
    script = CommonSDK(request=message, candidate="", steps=(Step("primary", failure=TimeoutError(_PROVIDER_LEAK_SENTINEL)),))
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    script.bind(composer, monkeypatch)
    common = AsyncMock(spec=composer._compose_loop, wraps=composer._compose_loop)
    monkeypatch.setattr(composer, "_compose_loop", common)
    sid = client.post("/api/sessions", json={"title": "common timeout"}).json()["id"]
    settled = settle_sync(client, client.app, path=f"/api/sessions/{sid}/messages", body={
        "content": message, "operation_id": str(uuid4()), "state_id": None,
    })
    status, body = settled.error()
    assert status == 422, body
    assert set(body) == {"detail"}
    detail = body["detail"]
    assert set(detail) == {"error_type", "budget_exhausted", "turns_used", "reason", "detail", "recovery_text", "request_id", "timeout_seconds"}
    assert detail["error_type"] == "convergence" and detail["budget_exhausted"] == "timeout"
    assert detail["turns_used"] == 0 and detail["reason"] == "convergence_wall_clock_timeout"
    assert detail["detail"] == (
        "Composer did not converge within 0 turns (budget exhausted: timeout). "
        "The request ran out of time before the turn could be completed."
    )
    assert detail["recovery_text"] == (
        "Retry once the provider responds faster, or ask an operator to raise the composer wall-clock budget."
    )
    assert str(UUID(detail["request_id"])) == detail["request_id"]
    assert 0 < detail["timeout_seconds"] <= composer._settings.composer_timeout_seconds
    progress = client.get(f"/api/sessions/{sid}/composer-progress").json()
    assert progress["phase"] == "failed" and progress["inflight_requests"] == 0
    assert progress["reason"] == detail["reason"] and progress["likely_next"] == detail["recovery_text"]
    common.assert_awaited_once()
    actual = common.await_args
    assert actual is not None and actual.args[0] == message
    assert actual.args[3] == sid and actual.args[5] == "alice"
    assert type(actual.args[2]) is CompositionState
    script.assert_done()
    assert sdk.await_count == 1
    assert_initial_wire(script.requests[0], text=message, state=actual.args[2], session_id=sid)
    assert_route_context(script.requests[0], actual, session_id=sid, user_id="alice")
    (call,) = assert_actual_audits(engine, script.requests, ("timeout",), session_id=sid)
    assert call["error_class"] == call["error_message"] == "TimeoutError"
    assert _disposition_rows(engine) == []
    _assert_no_sentinel_leak(engine, json.dumps(body), expected_withheld_replies=0,
                             expected_provider_failures=1, expected_failure_class="TimeoutError")
    _assert_withheld_prose_is_not_visible_or_replayed(client, sessions, sid)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_proposals_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0
        assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "tool")).all() == []
```

Use `settled.error()` for error status/body. `settled.final.status_code` is the
operation polling transport status and is not the embedded original HTTP error.
`settled.result()` validates and returns the completed result; a200 polling
response alone is insufficient.

- [ ] Replace `_malformed_completion` completely, removing only its now-unused
  `_PROSE_NUDGE_BUDGET` import. A malformed surface is not empty assistant prose:

```python
def _malformed_completion() -> Any:
    async def completion(**_kwargs: Any) -> _Response:
        return _Response(choices=[], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01})
    return completion
```

- [ ] Keep all four parameters and the entire private-receipt mutation-control
  tail of `test_send_message_freeform_planner_failure_is_translated`. Replace
  its table's malformed/timeout/provider-error expected values with:

```python
        (_malformed_completion, 502, "composer_error", "MALFORMED_RESPONSE", 1, 0),
        (_timeout_completion, 422, "convergence", "TIMEOUT", 1, 0),
        (_provider_error_completion, 502, "llm_unavailable", "PROVIDER_ERROR", 3, 0),
```

Keep the existing cost-unavailable tuple **unchanged**:

```python
        (_cost_unavailable_completion, 503, "cost_unavailable", "COST_UNAVAILABLE", 1, 0),
```

Replace its block from `# (a) deliberate safe response` through the last
disposition-row assertion immediately before `# The already-durable...` with:

```python
    status, body = settled.error()
    assert status == expected_status, body
    detail = body["detail"]
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress["phase"] == "failed" and progress["inflight_requests"] == 0
    dispositions = _disposition_rows(engine)
    if expected_planner_code == "COST_UNAVAILABLE":
        assert detail["error_type"] == "composer_planner_failure"
        assert detail["failure_code"] == "cost_unavailable" and detail["planner_code"] == "COST_UNAVAILABLE"
        assert progress["reason"] == "service_setup_failed" and "pricing" in progress["likely_next"]
        assert "administrator" in detail["detail"]
        assert len(dispositions) == 1
        assert dispositions[0].tool_calls[0]["failure_code"] == "cost_unavailable"
        assert dispositions[0].tool_calls[0]["surface"] == "freeform"
        assert dispositions[0].tool_calls[0]["planner_code"] == "COST_UNAVAILABLE"
    elif expected_planner_code == "TIMEOUT":
        assert detail["error_type"] == "convergence" and detail["budget_exhausted"] == "timeout"
        assert detail["turns_used"] == 0 and detail["reason"] == "convergence_wall_clock_timeout"
        assert dispositions == [] and progress["reason"] == detail["reason"]
    else:
        assert detail["error_type"] == expected_failure_code
        assert dispositions == []
        assert "planner_code" not in detail and "failure_code" not in detail
        assert progress["reason"] == ("service_setup_failed" if expected_planner_code == "MALFORMED_RESPONSE" else "provider_unavailable")
```

All subsequent native-call count, private-custody/visible-history/log sentinel,
corrupted summary/raw/writer/mixed/hash/binding/missing/duplicate receipt
controls and SQL-zero assertions remain. The cost branch still requires503;
this plan does not turn unknown money into a successful no-cost reply.

- [ ] Capture actual SDK args in that existing unit with this exact setup
  replacement (substitute its initial `_build_app(...)` line):

```python
    physical_requests = []
    actual_completion = completion_factory()
    async def physical(**kwargs):
        physical_requests.append(deepcopy(kwargs))
        return await actual_completion(**kwargs)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=physical)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
```

After its existing call-count assertion add:

```python
    expected_statuses = {
        "MALFORMED_RESPONSE": ("malformed_response",), "TIMEOUT": ("timeout",),
        "PROVIDER_ERROR": ("api_error", "api_error", "api_error"), "COST_UNAVAILABLE": ("success",),
    }[expected_planner_code]
    assert sdk.await_count == expected_llm_audit_rows
    assert_actual_audits(engine, physical_requests, expected_statuses, session_id=session_id)
```

- [ ] In `test_recompose_freeform_planner_failure_is_translated`, preserve its
  real persisted originating user row and recompose request. Replace only the
  old private-envelope/disposition expectations with:

```python
    status, body = settled.error()
    assert status == 502, body
    assert body["detail"]["error_type"] == "composer_error"
    assert "planner_code" not in body["detail"] and "failure_code" not in body["detail"]
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress["phase"] == "failed" and progress["reason"] == "service_setup_failed"
    assert _disposition_rows(engine) == []
    (audit,) = _llm_audit_rows(engine)
    assert audit.tool_calls[0]["call"]["status"] == "malformed_response"
    with engine.connect() as conn:
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(composition_proposals_table)).all() == []
        users = conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.role == "user")).scalars().all()
        assert users == [str(user_message.id)]
```

Keep its existing `_assert_no_sentinel_leak`, replay and captured-log checks.

- [ ] Replace the complete exhaustion-decline unit and remove its exclusively
  consumed `_decline_after_exhaustion_completion` helper. The public stop is
  authored through the actual common hint contract, then independently reviewed:

```python
def test_send_message_freeform_planner_decline_is_a_normal_assistant_message(tmp_path, monkeypatch):
    request = "Build a pipeline that joins two unbounded streams by matching keys."
    decision = json.dumps({"decision": "STOP_CONFIRMED", "reason": "The requested streaming join is unavailable.",
                           "next_action": None, "question": None})
    script = CommonSDK(request=request, candidate=_DECLINE_TEXT, steps=(
        Step("primary", tool_name="request_advisor_hint", arguments={
            "trigger": "cannot_complete", "problem_summary": "The requested streaming join is unavailable.",
            "recent_errors": [], "attempted_actions": [],
        }),
        Step("hint", content=decision), Step("primary", content=_DECLINE_TEXT), Step("end"),
    ))
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    script.bind(client.app.state.composer_service, monkeypatch)
    sid = client.post("/api/sessions", json={"title": "supported stop"}).json()["id"]
    settled = settle_sync(client, client.app, path=f"/api/sessions/{sid}/messages", body={
        "content": request, "operation_id": str(uuid4()), "state_id": None,
    })
    body = settled.result()
    script.assert_done()
    assert sdk.await_count == 4
    assert _DECLINE_TEXT in body["message"]["content"]
    assert body["state"] is None and body["proposals"] == []
    assert _disposition_rows(engine) == []
    assert_actual_audits(engine, script.requests, ("success",) * 4, session_id=sid)
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table)).mappings().all()
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(composition_proposals_table)).all() == []
    admitted = [row for row in rows if row["role"] == "assistant" and row["raw_content"] == _DECLINE_TEXT]
    assert len(admitted) == 1
    passes = [row["tool_calls"][0]["pass"] for row in rows if row["role"] == "audit" and row["tool_calls"]
              and row["tool_calls"][0]["_kind"] == "advisor_checkpoint_pass_audit"]
    assert len(passes) == 1 and passes[0]["phase"] == "end"
    assert passes[0]["publication_verdict"] == "ADMIT" and len(passes[0]["publication_binding"]) == 64
    assert client.get(f"/api/sessions/{sid}/composer-progress").json()["phase"] != "failed"
```

Direct planner discovery-exhaustion/hatch tests stay in `test_pipeline_planner.py`.
The public test still exercises an actual supported stop decision and END;
the old incorrect fourth-call model assertion is not a retained guarantee.

- [ ] Replace the complete definition of
`test_send_message_ordinary_turn_natural_decline_is_a_normal_assistant_message`
in `tests/integration/web/composer/test_freeform_planner_failure_translation.py`
with this code. Preserve both parameter cases:

```python
@pytest.mark.parametrize("publication_verdict", ["ADMIT", "WITHHOLD"])
def test_send_message_ordinary_turn_natural_decline_is_a_normal_assistant_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_verdict: str,
) -> None:
    """Ordinary prose uses the bounded common publication/repair protocol."""
    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.composer.no_tool_policy import (
        AssistantTextSegment,
        TrustedSystemNoticeSegment,
        visible_message_segments,
    )
    from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
    from elspeth.web.sessions.routes._helpers import _composer_chat_history
    from tests.helpers.composer_common_sdk import CommonSDK, Step, assert_actual_audits

    reviews = _observe_publication_reviews(monkeypatch)
    if publication_verdict == "ADMIT":
        steps = (Step("primary", content=_DECLINE_TEXT), Step("end", publication="ADMIT"))
        expected_primary_count, expected_end_count = 1, 1
    else:
        steps = (
            Step("primary", content=_DECLINE_TEXT),
            Step("end", publication="WITHHOLD"),
            Step("primary", content=_DECLINE_TEXT),
            Step("end", publication="WITHHOLD"),
        )
        expected_primary_count, expected_end_count = 2, 2
    script = CommonSDK(request=_EMPTY_INTENT, candidate=_DECLINE_TEXT, steps=steps)
    physical_sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, physical_sdk)
    script.bind(client.app.state.composer_service, monkeypatch)
    created = client.post("/api/sessions", json={"title": "ordinary turn decline"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": _EMPTY_INTENT, "operation_id": str(uuid4()), "state_id": None},
    )
    assert settled.final.status_code == 200, settled.final.text
    body = settled.result()
    script.assert_done()
    expected_calls = expected_primary_count + expected_end_count
    assert physical_sdk.await_count == len(script.requests) == expected_calls
    assert sum(step.role == "primary" for step in steps) == expected_primary_count
    end_requests = [
        request for request, step in zip(script.requests, steps, strict=True) if step.role == "end"
    ]
    assert len(end_requests) == expected_end_count
    reviewed_payloads = []
    for request in end_requests:
        review = parse_advisor_publication_review(request["messages"][1]["content"])
        assert review["current_request"] == _EMPTY_INTENT and review["candidate_reply"] == _DECLINE_TEXT
        assert review["saved_effects"]["saved_revision_count"] == 0
        assert review["saved_effects"]["net_changed"] is False
        reviewed_payloads.append(
            {key: value for key, value in review.items() if key not in {"current_request_ref", "candidate_reply_ref"}}
        )
    calls = assert_actual_audits(
        engine, script.requests, ("success",) * expected_calls, session_id=session_id
    )
    assert all(call["provider_cost"] == 0.01 for call in calls)
    assert len(_llm_audit_rows(engine)) == expected_calls
    assert body["state"] is None and body["proposals"] == []
    assert _disposition_rows(engine) == []
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress.get("phase") != "failed"
    with engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table).where(chat_messages_table.c.session_id == session_id)
            .order_by(chat_messages_table.c.sequence_no)
        ).mappings().all()
        attempts = conn.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == session_id)
        ).mappings().all()
        ledger = conn.execute(
            select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == session_id)
        ).mappings().all()
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(composition_proposals_table)).all() == []
    assert len(attempts) == len(ledger) == expected_calls
    assert all(row["settled_at"] is not None for row in attempts)
    assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in ledger}
    assistant_rows = [
        row for row in rows if row["role"] == "assistant" and (row["content"] or "").startswith(_DECLINE_TEXT)
    ]
    assert len(assistant_rows) == (1 if publication_verdict == "ADMIT" else 0)
    visible = client.get(f"/api/sessions/{session_id}/messages")
    assert visible.status_code == 200, visible.text
    replay = json.dumps(_composer_chat_history(asyncio.run(sessions.get_messages(UUID(session_id)))))
    withheld_by_origin = {}
    for row in rows:
        if row["role"] != "audit" or not row["tool_calls"]:
            continue
        envelope = row["tool_calls"][0]
        if envelope["_kind"] == "composer_withheld_reply":
            withheld_by_origin.setdefault(envelope["origin"], []).append(row)
    if publication_verdict == "WITHHOLD":
        assert _DECLINE_TEXT not in settled.final.text
        assert _DECLINE_TEXT not in visible.text and _DECLINE_TEXT not in replay
        for origin in ("repair_gate_superseded", "advisor_publication_withheld"):
            (withheld,) = withheld_by_origin[origin]
            assert withheld["content"] == _DECLINE_TEXT
            assert withheld["tool_calls"][0]["content_hash"] == hashlib.sha256(_DECLINE_TEXT.encode()).hexdigest()
    else:
        assert _DECLINE_TEXT in visible.text and _DECLINE_TEXT in replay
        assert withheld_by_origin == {}
        (saved,) = [row for row in rows if row["id"] == body["message"]["id"]]
        assert saved["raw_content"] == _DECLINE_TEXT
        assert body["message"]["content"] == saved["content"]
        segments = visible_message_segments(content=saved["content"], raw_content=saved["raw_content"])
        assert segments[0] == AssistantTextSegment(_DECLINE_TEXT)
        assert body["message"]["segments"] == [{"kind": part.kind, "content": part.content} for part in segments]
        assert len(segments) == 2 and type(segments[1]) is TrustedSystemNoticeSegment
    passes = [
        row["tool_calls"][0]["pass"] for row in rows
        if row["role"] == "audit" and row["tool_calls"]
        and row["tool_calls"][0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert len(passes) == expected_end_count
    for record, payload in zip(passes, reviewed_payloads, strict=True):
        assert record["phase"] == "end" and record["source"] == "model"
        assert record["verdict"] == "clean" and record["publication_verdict"] == publication_verdict
        assert record["provider_attempts"] == 1 and record["first_attempt_accepted"] is True
        assert record["publication_binding"] in {
            review.binding for review in reviews if json.loads(review.provider_payload_json) == payload
        }
```

- [ ] Replace the complete later-imperative unit with the following. It keeps
  actual provider authoring, one tool checkpoint before END and the separate
  HTTP `post_compose` state version. Common direct auto publication has no
  intermediate proposal history:

```python
def test_later_explicit_imperative_reaches_common_loop_and_auto_commits(tmp_path, monkeypatch):
    from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline
    request = "How does this work? Now build it."
    candidate = "The requested CSV to JSONL draft was saved."
    holder = {}
    saved_before_end = []
    async def before_end():
        with engine.connect() as conn:
            (checkpoint,) = conn.execute(select(composition_states_table)).mappings().all()
            assert conn.execute(select(composition_proposals_table)).all() == []
            saved_before_end.append(dict(checkpoint))
    async def primary(**kwargs):
        return _Response([_Choice(_Message(None, [_ToolCall("imperative-set-1", _Function(
            "set_pipeline", json.dumps({"pipeline": _pipeline(tmp_path, holder["id"])}),
        ))]))], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}, id="imperative-primary")
    script = CommonSDK(request=request, candidate=candidate, saved=1, steps=(
        Step("primary"), Step("early"), Step("primary", content=candidate),
        Step("end", before_return=before_end),
    ))
    async def complete(**kwargs):
        ordinal = len(script.requests)
        response = await script(**kwargs)
        return await primary(**kwargs) if ordinal == 0 else response
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=complete)
    client, engine, _sessions = _build_app(tmp_path, monkeypatch, sdk)
    script.bind(client.app.state.composer_service, monkeypatch)
    sid = client.post("/api/sessions", json={"title": "later imperative"}).json()["id"]
    holder["id"] = sid
    source = tmp_path / "blobs" / sid / "input.csv"
    source.parent.mkdir(parents=True)
    source.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / sid).mkdir(parents=True)
    settled = settle_sync(client, client.app, path=f"/api/sessions/{sid}/messages", body={
        "content": request, "operation_id": str(uuid4()), "state_id": None,
    })
    body = settled.result()
    assert body["state"] is not None and body["proposals"] == []
    script.assert_done()
    assert sdk.await_count == 4
    assert_actual_audits(engine, script.requests, ("success",) * 4, session_id=sid)
    with engine.connect() as conn:
        assert conn.execute(select(composition_proposals_table)).all() == []
        checkpoint, state = conn.execute(
            select(composition_states_table).order_by(composition_states_table.c.version)
        ).mappings().all()
        assert [checkpoint["provenance"], state["provenance"]] == ["tool_call", "post_compose"]
        assert saved_before_end == [dict(checkpoint)]
        assert checkpoint["id"] != state["id"] == body["state"]["id"]
        assert (checkpoint["version"], state["version"]) == (1, 2)
        assert all(checkpoint[key] == state[key] for key in ("sources", "nodes", "edges", "outputs", "metadata_"))
    assert body["state"]["sources"]["source"]["plugin"] == "csv"
    assert body["state"]["outputs"][0]["plugin"] == "json"
```

The END saved-effects scope contains the one actual tool checkpoint. HTTP
settlement subsequently stores the `post_compose` version with the same graph
and the final review projection; that later version is the returned state.
Bind both rows to their actual publication/operation evidence and keep the
pre-END checkpoint unchanged. Do not collapse the two records, manufacture a
proposal, or count settlement as a second provider-authored mutation.

- [ ] Preserve the complete parameterized interpretation/publication/acceptance/
  amendment test, with the dialect replacements and graph-review expectations
  below. Both bases now receive a real common END review:

```diff
-    planner_path = base_kind == "absent"
-    primary_count = 1 if planner_path else 2
-    end_count = 0 if planner_path else 1
+    has_base = base_kind == "present"
+    primary_count = 2
+    end_count = 1
```

```diff
-        mutation_name = "emit_pipeline_proposal" if planner_path else "set_pipeline"
+        mutation_name = "set_pipeline"
         assert mutation_name in names
-        assert ("set_pipeline" if planner_path else "emit_pipeline_proposal") not in names
+        assert "emit_pipeline_proposal" not in names
```

Keep only actual persisted-head projection conditions base-dependent. The
provider sequence and END fact are unconditional; replace the old absent-base
no-fact/no-gate branches as specified below rather than mechanically retaining
them under `if not has_base`. Replace its initial result-message branch in full
with:

```python
    assert result["message"]["content"].startswith(candidate_reply)
    assert "Composer completion is withheld." in result["message"]["content"]
```

At its final actual-call/model assertions replace the conditional set updates:

```python
    expected_request_ids = {"planner-request-1", "post-tool-request-2", "pending-proposal-end-review"}
    expected_models = {"provider/planner-v1", "provider/post-tool-v1", client.app.state.composer_service._settings.composer_advisor_model}
```

Replace its final conditional END-pass block with the unconditional block:

```python
    assert len(passes) == 1
    assert passes[0]["phase"] == "end" and passes[0]["source"] == "model"
    assert passes[0]["verdict"] == "flagged" and passes[0]["publication_verdict"] == "ADMIT"
    assert passes[0]["provider_attempts"] == 1 and passes[0]["first_attempt_accepted"] is True
    assert passes[0]["publication_binding"] in {
        review.binding for review in reviews if json.loads(review.provider_payload_json) == end_payloads[0]
    }
```

For an absent base, preserve public state None and zero immutable graph rows
before acceptance. A real common END still creates one private graph-review
fact with null `state_id`; it must not create an artificial checkpoint or drop
the fact. Keep the present-base stored-state projection and exact original
immutable snapshot. Inside the existing preacceptance connection block,
replace the conditional no-fact/present-fact assertion with:

```python
        (envelope,) = envelopes
        review_fact = AdvisorGraphReviewFact.from_envelope(envelope)
        assert review_fact.state_id == (base_record.id if has_base else None)
        assert review_fact.session_id == UUID(session_id)
        assert review_fact.terminal_operation_id == settled.accepted.json()["operation_id"]
        assert review_fact.action == "blocked" and review_fact.identity.source == "model"
        assert review_fact.gates.advisor_signoff is not None
        assert review_fact.gates.advisor_signoff.cause.value == "graph_rejected"
        assert _sessions._graph_review_on_connection(
            conn, session_id=UUID(session_id), stored=base_record,
        ) == review_fact
```

This exercises the actual null-state reader for an absent base and the actual
stored-state reader for a present base. Bind the fact to the real END and
COMPOSE operation; no fixture-authored review fact is acceptable.

After real proposal acceptance, make the existing stored-gate assertions
unconditional for both bases:

```python
    assert state["composer_meta"]["completion_gates"]["advisor_signoff"]["status"] == "blocked"
    assert state["composer_meta"]["completion_gates"]["advisor_signoff"]["for_graph"] == review_fact.identity.for_graph
```

Keep the actual accepted composition construction and completion-gate imports.
Replace the old absent no-gate/present readiness branch with the existing
pending-review assertion for both bases:

```python
    readiness = merge_completion_gates(
        _green_result(), parse_completion_gates(state["composer_meta"]), composition,
    ).readiness
    assert not readiness.completion_ready
    assert any(blocker.detail == ADVISOR_SIGNOFF_PENDING_DETAIL for blocker in readiness.blockers)
```

The retained fact reviewed the preacceptance graph, so it cannot clear the
changed accepted candidate. These assertions preserve the prior blocker and
its pending-review projection; they do not claim a fresh END reviewed that
candidate. Both cases keep the actual pending proposal, immutable
preacceptance rows, real HTTP acceptance, atomic review cohort
hashes/IDs/provenance, amendment and approved artifact, network/runtime-provider
prohibition and actual attempt/token ledger assertions.
Remove the now-unused local private-staged-message import. Update this unit's
docstring to say both bases use the common `set_pipeline` dialect; its historical
regression and all substantive review assertions remain.

## Task 6 — Direct common integration functions and real cancellation ports

**Modify:** `tests/integration/web/composer/test_freeform_pipeline_planner.py`.
Keep `_pipeline`, `_empty_state`, `_recipe_composer_context`,
`_assert_no_pipeline_side_effects`, and the existing matching-provider-cancel
test. Remove `_terminal_response` and `_routing_terminal_response` after their
last old consumers are replaced. Preserve the cost/authority companion's
imports of `_pipeline`; its shape remains unchanged.

- [ ] Add these imports and shared helper definitions to this existing module.
  They extend the current real session fixture; they create no second app.

```python
from copy import deepcopy

import litellm
from sqlalchemy import event

from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from elspeth.web.sessions.service import ComposerRequiredAuditPersistenceError
from tests.helpers.composer_common_sdk import CommonSDK, Step, assert_actual_audits, assert_initial_wire


def _source_files(tmp_path, sid):
    source = tmp_path / "blobs" / str(sid) / "input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / str(sid)).mkdir(parents=True, exist_ok=True)


async def _common_build(tmp_path, monkeypatch, *, trust_mode, persisted_base=False, before_return=None, history=None, pipeline=None):
    request = "Build a CSV to JSONL pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    await sessions.update_composer_preferences(session.id, trust_mode=trust_mode, density_default="high", actor="test")
    base = None
    if persisted_base:
        base = await sessions.save_composition_state(session.id, CompositionStateData(
            sources={}, nodes=[], edges=[], outputs=[],
            metadata_={"name": "Untitled Pipeline", "description": ""}, is_valid=False,
        ), provenance="session_seed")
    _source_files(tmp_path, session.id)
    authored = pipeline if pipeline is not None else _pipeline(tmp_path, str(session.id))
    auto = trust_mode == "auto_commit" and before_return is None
    candidate = "The draft was saved." if auto else "The proposed pipeline is waiting for approval."
    steps = [Step("primary", tool_name="set_pipeline", arguments={"pipeline": authored}, before_return=before_return)]
    if auto:
        steps.append(Step("early"))
    steps.extend((Step("primary", content=candidate), Step("end")))
    script = CommonSDK(request=request, candidate=candidate, steps=tuple(steps), saved=int(auto))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    result = await composer.compose(
        request, [] if history is None else history, _empty_state(), session_id=str(session.id),
        current_state_id=None if base is None else str(base.id), user_id="planner-user", user_message_id=str(user.id),
    )
    script.assert_done()
    assert sdk.await_count == len(steps)
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    calls = assert_actual_audits(engine, script.requests, ("success",) * len(steps), session_id=str(session.id))
    return engine, sessions, session, user, composer, base, result, script, calls
```

The helper's `before_return` scenario is review-only; it never claims an auto
effect based on a stale configured mode. Both true auto and pending cases run
their full finite physical sequence and actual checkpoint bodies.

- [ ] Retain trust × base decorators and replace the first integration body:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("trust_mode", ["auto_commit", "explicit_approve"])
@pytest.mark.parametrize("persisted_base", [False, True], ids=["absent-base", "present-base"])
async def test_empty_build_obeys_common_trust_mode_and_exact_base(tmp_path, monkeypatch, trust_mode, persisted_base):
    engine, sessions, session, _user, _composer, base, result, script, _calls = await _common_build(
        tmp_path, monkeypatch, trust_mode=trust_mode, persisted_base=persisted_base,
    )
    proposals = await sessions.list_composition_proposals(session.id)
    assert result.pipeline_commit_intent is None
    with engine.connect() as conn:
        states = conn.execute(select(composition_states_table).order_by(composition_states_table.c.version)).mappings().all()
    if trust_mode == "explicit_approve":
        (proposal,) = proposals
        assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
        assert deep_thaw(proposal.arguments_json) == _pipeline(tmp_path, str(session.id))
        assert proposal.base_state_id == (None if base is None else base.id)
        metadata = proposal.pipeline_metadata
        assert metadata is not None
        assert metadata.base == ({"kind": "absent"} if base is None else {
            "kind": "present", "state_id": str(base.id), "composition_content_hash": composition_content_hash(_empty_state()),
        })
        assert result.state == _empty_state()
        assert len(states) == int(base is not None)
        assert script.phases == ["end"]
    else:
        assert proposals == [] and result.state.sources["source"].plugin == "csv"
        assert result.state.outputs[0].plugin == "json"
        assert len(states) == int(base is not None) + 1
        assert states[-1]["sources"]["source"]["plugin"] == "csv"
        assert script.phases == ["early", "end"]
```

- [ ] Replace the served-endpoint function, preserving its exact persisted
  field witness, and add the direct-library witness in Task8:

```python
@pytest.mark.asyncio
async def test_common_llm_call_audit_persists_the_served_endpoint(tmp_path, monkeypatch):
    _engine, _sessions, _session, _user, _composer, _base, _result, script, calls = await _common_build(
        tmp_path, monkeypatch, trust_mode="explicit_approve",
    )
    assert len(calls) == len(script.requests) == 3
    assert all(call["provider_served"] == "DeepInfra" for call in calls)
    assert {call["provider_request_id"] for call in calls} == {"common-sdk-1", "common-sdk-2", "common-sdk-3"}
```

- [ ] Preserve both existing trust-direction decorators. Replace the complete
  trust-flip function with the code below. It flips actual persisted preferences
  while the primary response is in flight; no preference getter is patched.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("initial_trust_mode,flipped_trust_mode", [
    ("auto_commit", "explicit_approve"), ("explicit_approve", "auto_commit"),
], ids=["downgrade-mid-plan", "upgrade-mid-plan"])
async def test_trust_mode_change_during_provider_requires_review(tmp_path, monkeypatch, initial_trust_mode, flipped_trust_mode):
    request = "Build a CSV to JSONL pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    _source_files(tmp_path, session.id)
    await sessions.update_composer_preferences(session.id, trust_mode=initial_trust_mode, density_default="high", actor="test")
    flips = []
    async def flip():
        flips.append(await sessions.update_composer_preferences(
            session.id, trust_mode=flipped_trust_mode, density_default="high", actor="test",
        ))
    candidate = "The proposed pipeline is waiting for your review."
    script = CommonSDK(request=request, candidate=candidate, steps=(
        Step("primary", tool_name="set_pipeline", arguments={"pipeline": _pipeline(tmp_path, str(session.id))}, before_return=flip),
        Step("primary", content=candidate), Step("end"),
    ))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    result = await composer.compose(request, [], _empty_state(), session_id=str(session.id),
                                    user_id="planner-user", user_message_id=str(user.id))
    script.assert_done()
    assert sdk.await_count == 3 and script.phases == ["end"]
    (transition,) = flips
    assert transition.prior.trust_mode == initial_trust_mode
    assert transition.current.trust_mode == flipped_trust_mode
    assert (await sessions.get_composer_preferences(session.id)).trust_mode == flipped_trust_mode
    assert result.message.startswith(candidate)
    assert "for your review" in result.message
    assert result.pipeline_commit_intent is None and result.state == _empty_state()
    (proposal,) = await sessions.list_composition_proposals(session.id)
    assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
    with engine.connect() as conn:
        assert conn.execute(select(composition_states_table)).all() == []
    assert_actual_audits(engine, script.requests, ("success",) * 3, session_id=str(session.id))
```

The authority companion's held late-P4/locked preference race tests remain the
other half of revocation coverage; this in-flight flip cannot replace them.

- [ ] Replace the authoritative-history function in full. Persist real history
  IDs before the current request; do not forge authorship flags:

```python
@pytest.mark.asyncio
async def test_referential_empty_build_projects_authoritative_prior_user_requests(tmp_path, monkeypatch):
    request = "Build the requested pipeline."
    original = "Read transactions.csv, route rows where amount > 500 to high_value, route every other row to standard, and write both as JSONL."
    refinement = "Keep an audit trace field in both outputs."
    engine, sessions, session, first_user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=original)
    prior = [
        first_user,
        await sessions.add_message(session.id, "assistant", "I can prepare that pipeline.", writer_principal="compose_loop"),
        await sessions.add_message(session.id, "user", refinement, writer_principal="route_user_message"),
        await sessions.add_message(session.id, "assistant", "Understood.", writer_principal="compose_loop"),
    ]
    current = await sessions.add_message(session.id, "user", request, writer_principal="route_user_message")
    records = [*prior, current]
    assert len({record.id for record in records}) == 5
    sequence_numbers = [record.sequence_no for record in records]
    assert all(number is not None for number in sequence_numbers)
    assert sequence_numbers == sorted(sequence_numbers)
    assert len(set(sequence_numbers)) == 5
    history = _composer_chat_history(prior)
    from elspeth.web.composer.request_history import authoritative_conversation_context
    context = authoritative_conversation_context(request, history)
    assert context is not None
    assert [(item.history_index, item.content) for item in context.prior_user_requests] == [(0, original), (2, refinement)]
    assert [(item.history_index, item.content) for item in context.prior_assistant_messages] == [(1, "I can prepare that pipeline."), (3, "Understood.")]
    assert history[0]["_elspeth_user_message_id"] == str(prior[0].id)
    assert history[2]["_elspeth_user_message_id"] == str(prior[2].id)
    _source_files(tmp_path, session.id)
    await sessions.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="test")
    authored = _pipeline(tmp_path, str(session.id))
    (tmp_path / "blobs" / str(session.id) / "input.csv").write_text(
        "name,amount,audit_trace\nAda,600,fixture\n", encoding="utf-8",
    )
    authored["nodes"] = [{"id": "route_amount", "node_type": "gate", "input": "rows",
                          "condition": "row['amount'] > 500", "routes": {"true": "high_value", "false": "standard"}}]
    authored["outputs"] = [{**deepcopy(authored["outputs"][0]), "sink_name": name,
        "options": {**deepcopy(authored["outputs"][0]["options"]),
                    "path": str(tmp_path / "outputs" / str(session.id) / f"{name}.jsonl")}}
        for name in ("high_value", "standard")]
    candidate = "The requested routing proposal is waiting for approval."
    script = CommonSDK(request=request, candidate=candidate, steps=(
        Step("primary", tool_name="set_pipeline", arguments={"pipeline": authored}),
        Step("primary", content=candidate), Step("end"),
    ))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    result = await composer.compose(request, history, _empty_state(), session_id=str(session.id),
                                    user_id="planner-user", user_message_id=str(current.id))
    script.assert_done()
    assert sdk.await_count == 3
    for physical_primary in script.requests[:2]:
        primary = physical_primary["messages"]
        positions = [next(i for i, row in enumerate(primary) if row["role"] == role and row["content"] == text)
                     for role, text in [("user", original), ("assistant", "I can prepare that pipeline."),
                                        ("user", refinement), ("assistant", "Understood.")]]
        assert positions == sorted(positions)
        assert len(positions) == len(set(positions)) == 4
        (current_position,) = [i for i, row in enumerate(primary)
                               if row["role"] == "user" and row["content"] == request]
        assert current_position > positions[-1]
    assert context.additional_prior_user_requests_omitted == 0
    assert [item.content for item in context.prior_user_requests] == [original, refinement]
    assert "I can prepare that pipeline." not in json.dumps([item.content for item in context.prior_user_requests])
    assert "Understood." not in json.dumps([item.content for item in context.prior_user_requests])
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert result.state == _empty_state() and result.pipeline_commit_intent is None
    (proposal,) = await sessions.list_composition_proposals(session.id)
    assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
    assert deep_thaw(proposal.arguments_json)["nodes"][0]["condition"] == "row['amount'] > 500"
    with engine.connect() as conn:
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(blobs_table)).all() == []
    assert_actual_audits(engine, script.requests, ("success",) * 3, session_id=str(session.id))
```

This unit retains exact chronology, authenticated row IDs, assistant
non-authority and actual canonical routing proposal. H2.2 independently retains
complete chronology/refusal in direct-library tests. Remove the obsolete
`PLANNER_TERMINAL_INSTRUCTION` and `COMPOSER_HISTORY_USER_AUTHORED_KEY` imports
as these definitions replace their last uses. Retain the terminal response
helpers and other imports while their later Task8 consumers still need them.

- [ ] Replace the complete old cancellation lifecycle definition, including
  its async marker and three-argument parameter decorator, with this public
  common/explicit split. Preserve both trust modes and the exact original
  provider, cancellation, canonical-row and post-cancel write obligations:

```python
@pytest.mark.parametrize("trust_mode", ["auto_commit", "explicit_approve"])
def test_cancellation_preserves_actual_common_or_pending_proposal_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_mode: str,
) -> None:
    if trust_mode == "auto_commit":
        from tests.integration.web.composer.test_common_mutation_authority_cancellation import run_public_cancel_case

        run_public_cancel_case(tmp_path, monkeypatch, seam="started_p4", fail_sql=False, count=3)
        return

    async def explicit_case() -> None:
        request = "Build a CSV to JSONL pipeline."
        engine, sessions, session, user, composer = await _recipe_composer_context(
            tmp_path, monkeypatch, message=request
        )
        _source_files(tmp_path, session.id)
        await sessions.update_composer_preferences(
            session.id, trust_mode="explicit_approve", density_default="high", actor="test"
        )
        script = CommonSDK(
            request=request,
            candidate="",
            steps=(
                Step(
                    "primary",
                    tool_name="set_pipeline",
                    arguments={"pipeline": _pipeline(tmp_path, str(session.id))},
                ),
            ),
        )
        script.bind(composer, monkeypatch)
        sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
        monkeypatch.setattr(litellm, "acompletion", sdk)
        entered, release = threading.Event(), threading.Event()

        def park(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("INSERT INTO composition_proposals"):
                entered.set()
                assert release.wait(10)

        event.listen(engine, "before_cursor_execute", park)
        task = asyncio.create_task(
            composer.compose(
                request,
                [],
                _empty_state(),
                session_id=str(session.id),
                current_state_id=None,
                user_id="planner-user",
                user_message_id=str(user.id),
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel("cancel during actual explicit proposal SQL")
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 10)
            assert type(caught.value) is asyncio.CancelledError
            assert caught.value.args == ("cancel during actual explicit proposal SQL",)
            script.assert_done()
            assert sdk.await_count == 1 and script.phases == []
            assert_initial_wire(
                script.requests[0], text=request, state=_empty_state(), session_id=str(session.id)
            )
            assert_actual_audits(engine, script.requests, ("success",), session_id=str(session.id))

            (proposal,) = await sessions.list_composition_proposals(session.id)
            assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
            assert proposal.committed_state_id is None and proposal.base_state_id is None
            assert proposal.user_message_id == user.id
            assert proposal.tool_call_id == "common-tool-1"
            assert deep_thaw(proposal.arguments_json) == _pipeline(tmp_path, str(session.id))
            assert proposal.pipeline_metadata is not None
            assert proposal.pipeline_metadata.base == {"kind": "absent"}
            authority = await sessions.get_authoritative_pipeline_proposal(
                session_id=session.id, proposal_id=proposal.id
            )
            assert authority.row.status == "pending"
            assert authority.row.audit_event_id == authority.creation_event_id

            with engine.connect() as conn:
                assert conn.execute(select(composition_states_table)).all() == []
                assert conn.execute(select(blobs_table)).all() == []
                assert conn.execute(
                    select(proposal_events_table).where(
                        proposal_events_table.c.event_type == "proposal.rejected"
                    )
                ).all() == []
                audit_rows = conn.execute(
                    select(chat_messages_table.c.role, chat_messages_table.c.tool_calls)
                ).all()
            assert not any(calls and calls[0].get("_kind") == "audit" for _role, calls in audit_rows)
            await asyncio.wait_for(
                sessions.add_message(
                    session.id, "system", "lock probe", writer_principal="route_system_message"
                ),
                2,
            )
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(engine, "before_cursor_execute", park)

    asyncio.run(explicit_case())
```

`run_public_cancel_case` is complete code in the qualified authority companion.
It observes actual started P4, every original cancellation, the SQL disposition,
saved state, audit truth and actual primary/EARLY SDK prefix. Successful started
P4 may commit its graph before outer cancellation settles; the public auto path
has no intermediate proposal. The explicit case parks its actual proposal
insert and retains the one successful native call, strict initial request,
no fabricated generic audit, singleton canonical pending proposal and real
system-message write. It does not prove a registered creation ticket;
Task7 owns that direct finish-once guarantee.

- [ ] In `tests/integration/web/composer/test_common_mutation_authority_cancellation.py`,
  add this narrow post-cancel write probe inside the existing `observe_running`,
  after the three terminal snapshot assertions and before `return snapshot`.
  The actual operation drive has already been joined. Keep the surrounding
  finally release and every existing native/state/exception/status assertion:

```python
            if seam == "started_p4" and not fail_sql:
                assert len(sql_returns) == 1
                assert sql_returns[0].checkpoint_disposition == "committed"
                probe = await asyncio.wait_for(
                    async_client.patch(
                        f"/api/sessions/{sid}/composer/preferences",
                        json={"trust_mode": "auto_commit", "density_default": "high"},
                    ),
                    2,
                )
                assert probe.status_code == 200, probe.text
```

The authenticated same-session preference PATCH performs an actual write even
when values are unchanged. Its two-second bound proves the successful P4 join
released write locks; terminal reads and worker flags alone cannot prove that.
No helper argument, return value, extra app or controller is needed. Other
public helper cases keep their existing behavior.

- [ ] Replace the complete old repeated-cancel/rejection-failure definition,
  including its async marker, with this existing public companion invocation:

```python
def test_auto_commit_cancellation_preserves_p4_failure_and_repeated_originals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.web.composer.test_common_mutation_authority_cancellation import run_public_cancel_case

    run_public_cancel_case(tmp_path, monkeypatch, seam="started_p4", fail_sql=True, count=3)
```

Its actual P4 SQL fault and three exact outer cancellations remain reachable
through the original exception graph; failed P4 rolls back its graph while
earlier provider calls remain successful. Task7 retains the independent
proposal creation/rejection originals and both SQL outcomes. Add SQLAlchemy
`event` to P's existing imports if absent. Remove only imports rendered unused
by these two full replacements; retain shared fixtures, earlier ports,
terminal response helpers and dataclasses with later live consumers.

- [ ] Keep both existing structural parameters; replace only its body after
  signature with the following. Remove its private/sentinel helper imports
  only if unused elsewhere:

```python
    request = message
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    candidate = "I can explain the current draft without changing it."
    script = CommonSDK(request=request, candidate=candidate, steps=(Step("primary", content=candidate), Step("end")))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    common = AsyncMock(spec=composer._compose_loop, wraps=composer._compose_loop)
    monkeypatch.setattr(composer, "_compose_loop", common)
    before = state.to_dict()
    result = await composer.compose(request, [], state, session_id=str(session.id),
                                    user_id="planner-user", user_message_id=str(user.id))
    common.assert_awaited_once()
    script.assert_done()
    assert sdk.await_count == 2 and result.state == state and state.to_dict() == before
    assert result.message.startswith(candidate)
    assert await sessions.list_composition_proposals(session.id) == []
    assert_initial_wire(script.requests[0], text=request, state=state, session_id=str(session.id))
    _assert_no_pipeline_side_effects(engine)
    assert_actual_audits(engine, script.requests, ("success", "success"), session_id=str(session.id))
```

Rename its definition to `test_empty_and_existing_graph_use_same_actual_common_provider_loop`
with the same signature/decorators; the body proves both graphs use the real
common loop. Because the parameter's graph is not mutated and
has no outputs, it does not execute filesystem preflight; no path alteration
is needed to make this particular no-effects oracle pass.

- [ ] Replace the audit-failure unit with an actual pre-semantic checkpoint
  failure. P4 failure after a proposal is already created cannot prove that
  no proposal was created; this exact physical checkpoint port can:

```python
@pytest.mark.asyncio
async def test_common_required_provider_audit_failure_publishes_no_proposal_or_state(tmp_path, monkeypatch):
    request = "Build a CSV to JSONL pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    preference_transition = await sessions.update_composer_preferences(
        session.id, trust_mode="explicit_approve", density_default="high", actor="test",
    )
    assert preference_transition.current.trust_mode == "explicit_approve"
    assert preference_transition.current.density_default == "high"
    with engine.connect() as conn:
        events_before = [dict(row) for row in conn.execute(
            select(proposal_events_table).order_by(proposal_events_table.c.id),
        ).mappings()]
    (preference_event,) = events_before
    assert preference_event["event_type"] == "trust_mode.changed"
    assert preference_event["session_id"] == str(session.id)
    assert preference_event["proposal_id"] is None
    assert preference_event["actor"] == "test"
    assert preference_event["payload"] == {
        "trust_mode": preference_transition.current.trust_mode,
        "prior_trust_mode": preference_transition.prior.trust_mode,
        "density_default": preference_transition.current.density_default,
    }
    script = CommonSDK(request=request, candidate="", steps=(Step("primary", tool_name="set_pipeline",
        arguments={"pipeline": _pipeline(tmp_path, str(session.id))}),))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    original = sessions._insert_chat_message
    sql_failure = SQLAlchemyError("actual required native provider audit failure")
    fired = []
    def fail(conn, **kwargs):
        envelopes = kwargs["tool_calls"]
        if kwargs["role"] == "audit" and envelopes and envelopes[0].get("_kind") == "llm_call_audit":
            fired.append(sql_failure)
            raise sql_failure
        return original(conn, **kwargs)
    monkeypatch.setattr(sessions, "_insert_chat_message", fail)
    with pytest.raises(ComposerRequiredAuditPersistenceError) as caught:
        await composer.compose(request, [], _empty_state(), session_id=str(session.id),
                                user_id="planner-user", user_message_id=str(user.id))
    assert caught.value.__cause__ is sql_failure
    assert fired == [sql_failure]
    assert sdk.await_count == 1 and script.phases == []
    script.assert_done()
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert await sessions.list_composition_proposals(session.id) == []
    persisted = await sessions.get_messages(session.id, limit=None)
    durable_call_kinds = [envelope["_kind"] for row in persisted for envelope in (row.tool_calls or ())
        if envelope.get("_kind") in {"prompt_guard_audit", "llm_call_audit", "planner_attempt_audit"}]
    assert durable_call_kinds == ["prompt_guard_audit"]
    with engine.connect() as conn:
        events_after = [dict(row) for row in conn.execute(
            select(proposal_events_table).order_by(proposal_events_table.c.id),
        ).mappings()]
        assert events_after == events_before
        assert conn.execute(select(blobs_table)).all() == []
        assert conn.execute(select(composition_proposals_table)).all() == []
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "tool")).all() == []
```

The actual preference writer inserts its setup audit even for same-value
preferences. Preserve that complete transition-bound event row and compare the
entire event table after failure; the shared no-side-effects helper's blanket
event emptiness does not apply here. The failed native checkpoint rolls back
its LLM audit row; the preceding prompt-guard audit remains. Keep the independent
common-writer rollback/atomicity tests. Preserve the original SQL cause and
required-audit classification, without fabricating successful settlement.

- [ ] Keep `test_freeform_matching_provider_cancellation_persists_once_without_self_cause`
  unchanged. Replace the last private-route spy test with:

```python
@pytest.mark.asyncio
async def test_freeform_common_provider_timeout_has_no_pipeline_side_effects(tmp_path, monkeypatch):
    request = "Build the requested pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    original = TimeoutError("bounded physical timeout")
    script = CommonSDK(request=request, candidate="", steps=(Step("primary", failure=original),))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    common = AsyncMock(spec=composer._compose_loop, wraps=composer._compose_loop)
    monkeypatch.setattr(composer, "_compose_loop", common)
    with pytest.raises(ComposerConvergenceError) as caught:
        await composer.compose(request, [], _empty_state(), session_id=str(session.id),
                                user_id="planner-user", user_message_id=str(user.id))
    assert caught.value.budget_exhausted == "timeout" and caught.value.max_turns == 0
    common.assert_awaited_once()
    script.assert_done()
    assert sdk.await_count == 1 and script.phases == []
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert_actual_audits(engine, script.requests, ("timeout",), session_id=str(session.id))
    assert await sessions.list_composition_proposals(session.id) == []
    _assert_no_pipeline_side_effects(engine)
```

The two capability-manifest functions are replaced in Task8 at their retained
direct-library owner. No common planner-manifest contract is invented.

## Task 7 — Retain actual lower-level creation/rejection original custody

**Modify:** `tests/unit/web/sessions/test_pipeline_rejection_required.py`.
Preserve every existing fixture and definition, including immutable replay,
corruption/foreign/forged outcome, Stop/deadline and repeated-cancellation
companions. Add the complete parameterized definition below. Both telemetry
values and both successful/failing rejection SQL outcomes are required; the
successful `request_cancelled` rejection event must survive the old adapter's
retirement as well as the failed-cleanup originals.

Add `composition_states_table` to the existing session-model import. Other
module names below already exist; the remaining imports are local.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("telemetry_failure", [False, True])
@pytest.mark.parametrize(
    "rejection_fails",
    [False, True],
    ids=["rejection-commits", "rejection-rolls-back"],
)
async def test_creation_then_rejection_retains_actual_originals_at_nonzero_scope(
    operation_store, monkeypatch, telemetry_failure, rejection_fails
):
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
    from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkRole
    from elspeth.web.sessions import service as service_module
    from elspeth.web.sessions.pipeline_rejection import PipelineRejectionSQLResult
    from elspeth.web.sessions.pipeline_rejection_custody import (
        original_outcome_group,
        project_creation_handoff,
        reject_pipeline_with_required_custody,
    )
    from elspeth.web.sessions.pipeline_rejection_finish_once import PipelineCreationReturned

    engine, repository, _authority, service, sid = operation_store
    existing, running = await prepared_rejection(operation_store)
    context = running.session_operation_context
    old = existing.authority.row
    assert old.user_message_id is not None
    creation_owner = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    creation_binding = RequiredWorkBinding(creation_owner, 7, 12, RequiredWorkRole.TURN, running)
    creation, creation_projection = creation_binding.reserve_pair(
        RequiredWorkSource.PROPOSAL_CREATION_SQL,
        RequiredWorkSource.PROPOSAL_CREATION_PROJECTION,
    )
    plan = PipelinePlanResult(
        proposal=existing.authority.proposal,
        tool_call_id="lower-level-new-provider-call",
        custody_result="not_required",
        model_identifier="model",
        model_version="version",
        provider="test",
    )
    telemetry_original = RuntimeError("actual creation post-SQL telemetry failure")
    if telemetry_failure:
        class FailedCounter:
            def add(self, amount, attributes):
                raise telemetry_original

        monkeypatch.setattr(service_module, "_PIPELINE_PLANNER_COUNTER", FailedCounter())

    create_entered, create_release = threading.Event(), threading.Event()
    reject_entered, reject_release = threading.Event(), threading.Event()
    sql_original = OperationalError("actual rejection SQL failure", {}, RuntimeError("actual driver"))
    created_cancellations = []
    rejected_cancellations = []
    creation_outcomes = []
    rejection_outcomes = []
    creation_handoffs = []
    creation_projection_failures = []
    observed_rejection = []
    rejection_owners = []
    actual_finish = service_module.run_required_sql_finish_once

    async def observe_finish(ticket, func):
        outcome = await actual_finish(ticket, func)
        if ticket.key.source is RequiredWorkSource.PROPOSAL_CREATION_SQL:
            creation_outcomes.append((ticket, outcome))
            created_cancellations.extend(outcome.deferred_cancellations)
        if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL:
            rejection_outcomes.append((ticket, outcome))
            rejected_cancellations.extend(outcome.deferred_cancellations)
        return outcome

    monkeypatch.setattr(service_module, "run_required_sql_finish_once", observe_finish)

    def hold_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO composition_proposals"):
            create_entered.set()
            assert create_release.wait(10)
        if statement.startswith("UPDATE composition_proposals"):
            reject_entered.set()
            assert reject_release.wait(10)
            if rejection_fails:
                raise sql_original

    event.listen(engine, "before_cursor_execute", hold_sql)

    async def workflow():
        handoff = await service.create_pipeline_composition_proposal_finish_once(
            session_id=sid,
            plan=plan,
            summary=old.summary,
            rationale=old.rationale,
            affects=old.affects,
            arguments_redacted_json=RedactedPipelineArguments(old.arguments_redacted_json),
            actor="composer-web:user:alice",
            composer_model_identifier="model",
            composer_model_version="version",
            composer_provider="test",
            user_message_id=old.user_message_id,
            session_operation_context=context,
            required_work=creation,
            running=running,
        )
        creation_handoffs.append(handoff)
        row, failures, cancellations = project_creation_handoff(
            creation_binding, creation, creation_projection, handoff
        )
        creation_projection_failures.extend(failures)
        assert row is not None and row.status == "pending"
        assert row.id != old.id and row.tool_call_id == plan.tool_call_id
        assert row.session_id == sid and row.user_message_id == old.user_message_id
        actual = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
        assert actual.row.audit_event_id == actual.creation_event_id
        expected = PipelineRejectionExpected(
            actual,
            "request_cancelled",
            None,
            "system:auto_reject_request_cancelled",
            "alice",
            context,
            running,
        )
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(
                RequiredAuthorityKind.DURABLE_COMPOSE,
                context,
                durable_operation_id=running.claim.operation_id,
                claim_attempt=running.claim.attempt,
                proposal_id=str(row.id),
                tool_call_id=row.tool_call_id,
            )
        )
        rejection_owners.append(coordinator)
        binding = RequiredWorkBinding(coordinator, 7, 12, RequiredWorkRole.TURN, running)
        observed_rejection.append(expected)
        try:
            await reject_pipeline_with_required_custody(service, expected=expected, binding=binding)
        except BaseException as rejection:
            raise original_outcome_group(
                "Actual creation and cancellation cleanup originals",
                *cancellations,
                *failures,
                rejection,
            ) from rejection
        raise AssertionError("actual rejection completion lost its deferred cancellation")

    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return tuple(leaf for child in error.exceptions for leaf in leaves(child))
        return (error,)

    task = asyncio.create_task(workflow())
    try:
        assert await asyncio.to_thread(create_entered.wait, 10)
        task.cancel("initial creation cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        create_release.set()
        assert await asyncio.to_thread(reject_entered.wait, 10)
        task.cancel("later cleanup cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        reject_release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(task, 10)
        assert type(caught.value) is BaseExceptionGroup
        originals = leaves(caught.value)
        assert len(created_cancellations) == len(rejected_cancellations) == 1
        assert type(created_cancellations[0]) is type(rejected_cancellations[0]) is asyncio.CancelledError
        assert created_cancellations[0].args == ("initial creation cancellation",)
        assert rejected_cancellations[0].args == ("later cleanup cancellation",)
        projected_originals = tuple(
            leaf for failure in creation_projection_failures for leaf in leaves(failure)
        )
        expected_originals = (
            created_cancellations[0],
            *projected_originals,
            *((sql_original,) if rejection_fails else ()),
            rejected_cancellations[0],
        )
        assert len(originals) == len(expected_originals)
        assert all(actual is expected for actual, expected in zip(originals, expected_originals, strict=True))
        assert len({id(original) for original in originals}) == len(originals)

        ((creation_ticket, creation_outcome),) = creation_outcomes
        assert creation_ticket is creation
        assert type(creation_outcome) is RequiredSQLReturned
        (creation_handoff,) = creation_handoffs
        assert type(creation_handoff) is PipelineCreationReturned
        assert creation_handoff.sql_outcome is creation_outcome
        assert creation_outcome.value.tool_call_id == plan.tool_call_id
        assert creation_outcome.value.id != old.id
        if telemetry_failure:
            (post_sql_original,) = creation_handoff.post_sql_failures
            assert post_sql_original is telemetry_original
            assert any(original is telemetry_original for original in projected_originals)
        else:
            assert creation_handoff.post_sql_failures == ()
            assert creation_projection_failures == []
            assert len(originals) == (3 if rejection_fails else 2)

        creation_owner.assert_completed()
        assert len(creation_owner.tickets) == 2
        assert {ticket.key.source for ticket in creation_owner.tickets} == {
            RequiredWorkSource.PROPOSAL_CREATION_SQL,
            RequiredWorkSource.PROPOSAL_CREATION_PROJECTION,
        }
        (rejection_owner,) = rejection_owners
        rejection_owner.assert_completed()
        assert len(rejection_owner.tickets) == 2
        assert {ticket.key.source for ticket in rejection_owner.tickets} == {
            RequiredWorkSource.PROPOSAL_REJECTION_SQL,
            RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION,
        }
        for owner in (creation_owner, rejection_owner):
            for ticket in owner.tickets:
                assert ticket.complete
                assert ticket.key.transition_ordinal == 7 and ticket.key.semantic_ordinal == 12
                assert ticket.key.authority.context == context
                assert ticket.key.authority.durable_operation_id == running.claim.operation_id
                assert ticket.key.authority.claim_attempt == running.claim.attempt
        if telemetry_failure:
            receipts = creation_owner.failure_receipts()
            assert any(
                receipt.key.source is RequiredWorkSource.PROPOSAL_CREATION_PROJECTION
                and any(original is telemetry_original for original in leaves(receipt.original_root))
                for receipt in receipts
            )

        (expected,) = observed_rejection
        assert expected.reason == "request_cancelled" and expected.dispatch is None
        assert expected.actor == "system:auto_reject_request_cancelled"
        assert expected.actor_user_id == "alice"
        assert expected.context == context and expected.running is running
        assert rejection_owner.authority.proposal_id == str(expected.authority.row.id)
        assert rejection_owner.authority.tool_call_id == plan.tool_call_id
        ((rejection_ticket, rejection_outcome),) = rejection_outcomes
        assert rejection_ticket in rejection_owner.tickets
        assert rejection_ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL
        assert rejection_outcome.deferred_cancellations[0] is rejected_cancellations[0]

        with engine.connect() as conn:
            actual_row = conn.execute(
                select(composition_proposals_table).where(
                    composition_proposals_table.c.session_id == str(sid),
                    composition_proposals_table.c.id == str(expected.authority.row.id),
                    composition_proposals_table.c.tool_call_id == plan.tool_call_id,
                )
            ).mappings().one()
            terminal_rows = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.session_id == str(sid),
                    proposal_events_table.c.proposal_id == actual_row["id"],
                    proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")),
                )
            ).mappings().all()
            assert conn.execute(
                select(composition_states_table).where(composition_states_table.c.session_id == str(sid))
            ).all() == []
        assert actual_row["committed_state_id"] is None
        assert actual_row["user_message_id"] == str(old.user_message_id)
        reloaded = await asyncio.wait_for(
            service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=expected.authority.row.id),
            2,
        )
        assert reloaded.proposal == expected.authority.proposal
        assert reloaded.creation_event_id == expected.authority.creation_event_id
        if rejection_fails:
            assert type(rejection_outcome) is RequiredSQLRaised
            assert rejection_outcome.error is sql_original
            assert actual_row["status"] == "pending" and terminal_rows == []
            assert actual_row["audit_event_id"] == str(expected.authority.creation_event_id)
            assert reloaded.row == expected.authority.row
        else:
            assert type(rejection_outcome) is RequiredSQLReturned
            result = rejection_outcome.value
            assert type(result) is PipelineRejectionSQLResult
            assert result.expected is expected and result.transitioned is True
            assert result.record == reloaded.row
            assert actual_row["status"] == reloaded.row.status == "rejected"
            (terminal,) = terminal_rows
            expected_payload = {
                "schema": "pipeline_proposal_rejected.v1",
                "tool_call_id": plan.tool_call_id,
                "tool_name": "set_pipeline",
                "status": "rejected",
                "outcome": "failed",
                "reason_code": "request_cancelled",
                "draft_hash": plan.proposal.draft_hash,
                "dispatch": None,
            }
            assert terminal["event_type"] == result.event.event_type == "proposal.rejected"
            assert terminal["actor"] == result.event.actor == "system:auto_reject_request_cancelled"
            assert terminal["payload"] == result.event.payload == expected_payload
            assert terminal["id"] == str(result.event.id) == actual_row["audit_event_id"]
            assert terminal["id"] == str(reloaded.row.audit_event_id)
            assert terminal["proposal_id"] == str(result.record.id) == str(expected.authority.row.id)
            assert terminal["session_id"] == str(result.record.session_id) == str(sid)
            assert replace(
                reloaded.row,
                status=expected.authority.row.status,
                audit_event_id=expected.authority.row.audit_event_id,
                updated_at=expected.authority.row.updated_at,
            ) == expected.authority.row
    finally:
        create_release.set()
        reject_release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", hold_sql)
        repository.release(context)
```

The actual creation and rejection owners each have a complete nonempty SQL/
projection source pair at transition 7 / semantic 12, bound to the same real
context, durable operation and attempt. Rejection authority binds the newly
created proposal/tool IDs. The final group preserves every actual original
object and order, including telemetry failure and both delivered cancellations.

On SQL rollback, the new row remains pending with its actual creation-event
pointer and no terminal event. On SQL commit, the exact `request_cancelled`
event has status `rejected`, outcome `failed`, the original draft hash, null
dispatch and the actual terminal row/event pointer. Both dispositions must be
read back through canonical authority. Successful SQL still defers the actual
cancellation until owned projection completes.

Keep the independent three-creation-original 6/9 and three-rejection-original
4/7 tests, canonical validation-failure child 9/13, immutable reuse and all
existing companions. This finite direct workflow does not replace them or
claim a provider ran. Its local explicit-group projection is appropriate for
`original_outcome_group`; the public cancellation helper retains traversal
through group members and cause/context links. Retire no adapter-only caller
until this complete direct replacement is measured and independently reviewed.

## Task 8 — Direct library manifest and response-object evidence

**Modify:** `tests/integration/web/composer/test_freeform_pipeline_planner.py`
only the complete definitions of
`test_freeform_planner_manifest_mismatch_is_durable_before_failure` and
`test_freeform_manifest_mismatch_audit_write_defers_request_cancellation_exactly_once`,
plus their consumed direct helper and imports below.
**Modify:** `tests/unit/web/composer/test_pipeline_planner.py` only to add the
served-endpoint witness below. Preserve every existing definition in that file.

**Interfaces:** consume P's existing `_recipe_composer_context` and unchanged
`_assert_no_pipeline_side_effects`; consume the direct unit's existing
`_plan`, `_ScriptedCompletion` and `_response`. Produce the two renamed direct
integration definitions and the new direct response-object unit below. The
shared finite CommonSDK helper remains unchanged.

The supported direct planner owns capability manifests. `_plan` uses the
caller's real SQL engine, acquired COMPOSE context and authority, then creates
its provider accounting service on that same engine. It rebuilds a full
catalog policy internally; passing an honest `ToolContext` does not prove that
its caller's policy constrains this harness. These tests deliberately refuse
before proposal creation, filesystem preflight or graph publication.

- [ ] Merge these consumed names into P's existing imports. Keep all other
  imports that still have consumers. Do not add `dataclasses.replace`, delete
  terminal helpers or rewrite earlier ports:

```python
from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.pipeline_planner import PlannerCustodyConfig, PlannerOriginatingMessage
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.tools import ToolContext
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer.test_pipeline_planner import _ScriptedCompletion, _plan, _response
```

P already supplies `asyncio`, `json`, `threading`, `deepcopy`,
`Path`, `Any`, `pytest`, `LiteLLMAPIError`, `select`, `event`,
`ComposerLLMCallStatus`, `AuditIntegrityError`, `create_catalog_service`,
`chat_messages_table`, `quota_provider_attempts_table` and
`token_usage_ledger_table`. Merge with those imports rather than duplicate
statements. Merge `Callable` into P's existing `collections.abc` import. Add
this complete finite helper beside the existing recipe helper:

```python
async def _direct_manifest_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
    expected_status: ComposerLLMCallStatus,
    *,
    pause_insert: Callable[..., None] | None = None,
) -> tuple[BaseException, AuditIntegrityError]:
    import elspeth.web.composer.pipeline_planner as planner_module

    request = "Build the requested pipeline."
    engine, sessions, session, user, _composer = await _recipe_composer_context(
        tmp_path, monkeypatch, message=request
    )
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    tool_context = ToolContext(catalog=policy, plugin_snapshot=snapshot)
    origin = PlannerOriginatingMessage(
        session_id=str(session.id),
        message_id=str(user.id),
        content=request,
        user_id="planner-user",
    )
    manifests: list[Any] = []
    owned: list[list[dict[str, Any]]] = []
    owned_hashes: list[str] = []
    integrity_failures: list[AuditIntegrityError] = []
    real_builder = planner_module.build_planner_capability_manifest
    real_matcher = planner_module._assert_planner_call_matches_manifest

    def observe_manifest(**kwargs: Any) -> Any:
        manifest = real_builder(**kwargs)
        manifests.append(manifest)
        owned.append(kwargs["messages"])
        return manifest

    def observe_mismatch(call: Any, manifest: Any, recorder: Any) -> None:
        try:
            real_matcher(call, manifest, recorder)
        except AuditIntegrityError as failure:
            integrity_failures.append(failure)
            raise

    monkeypatch.setattr(planner_module, "build_planner_capability_manifest", observe_manifest)
    monkeypatch.setattr(planner_module, "_assert_planner_call_matches_manifest", observe_mismatch)

    class MutatingCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> Any:
            assert len(owned) == 1 and self.requests == []
            assert kwargs["messages"] is not owned[0]
            original_content = owned[0][0]["content"]
            kwargs["messages"][0]["content"] += "\nSDK detached mutation"
            assert owned[0][0]["content"] == original_content
            owned[0][0]["content"] += "\nactual owned manifest mutation"
            owned_hashes.append(stable_hash(owned[0]))
            return await super().__call__(**kwargs)

    completion = MutatingCompletion(outcome)
    recorder = BufferingRecorder()
    if pause_insert is not None:
        event.listen(engine, "before_cursor_execute", pause_insert)
    try:
        async with acquire_compose_context(sessions, session.id) as context:
            custody = PlannerCustodyConfig(
                data_dir=str(tmp_path),
                session_engine=engine,
                max_storage_per_session=1_000_000,
                secret_service=None,
                runtime_preflight=None,
                session_operation_context=context,
                session_operation_authority=sessions.session_operation_authority,
            )
            error: BaseException | None = None
            try:
                await _plan(
                    tmp_path=tmp_path,
                    tool_context=tool_context,
                    completion=completion,
                    recorder=recorder,
                    originating_message=origin,
                    custody_config=custody,
                )
            except BaseException as failure:
                error = failure
            assert error is not None, "controlled owned manifest mutation did not refuse"
            (integrity,) = integrity_failures
            assert type(integrity) is AuditIntegrityError
            assert str(integrity) == "planner call inputs changed after capability manifest construction"
            assert len(manifests) == len(completion.requests) == len(owned_hashes) == 1
            assert completion._responses == []
            (call,) = recorder.llm_calls
            assert call.status is expected_status
            assert call.call_id is not None
            assert call.planner_call_ordinal == 1
            assert call.messages_hash == owned_hashes[0]
            assert call.messages_hash != manifests[0].rendered_prompt_hash
            assert call.tools_spec_hash == manifests[0].effective_tool_hash
            physical = completion.requests[0]
            assert [row["role"] for row in physical["messages"]] == ["system", "user", "user"]
            assert json.loads(physical["messages"][2]["content"])["intent"] == request
            names = {tool["function"]["name"] for tool in physical["tools"]}
            assert "emit_pipeline_proposal" in names and "set_pipeline" not in names
            assert physical["messages"][0]["content"].endswith("\nSDK detached mutation")
            assert "actual owned manifest mutation" not in physical["messages"][0]["content"]

            persisted = await sessions.get_messages(session.id, limit=None)
            native = [
                (row, envelope)
                for row in persisted
                for envelope in (row.tool_calls or ())
                if envelope.get("_kind") == "llm_call_audit"
            ]
            ((native_row, public_envelope),) = native
            public_envelope = deep_thaw(public_envelope)
            assert native_row.role == "audit" and native_row.writer_principal == "compose_loop"
            assert public_envelope == llm_call_audit_envelope(call)
            public_call = public_envelope["call"]
            assert public_call["call_id"] == call.call_id
            assert public_call["status"] == expected_status.value
            assert public_call["messages_hash"] == call.messages_hash
            assert public_call["messages_hash"] != manifests[0].rendered_prompt_hash
            assert public_call["tools_spec_hash"] == call.tools_spec_hash == manifests[0].effective_tool_hash
            public_text = json.dumps(public_envelope, sort_keys=True)
            assert request not in public_text
            assert "SDK detached mutation" not in public_text
            assert "actual owned manifest mutation" not in public_text

            with engine.connect() as conn:
                attempt = dict(
                    conn.execute(
                        select(quota_provider_attempts_table).where(
                            quota_provider_attempts_table.c.session_id == str(session.id)
                        )
                    ).mappings().one()
                )
                ledger = dict(
                    conn.execute(
                        select(token_usage_ledger_table).where(
                            token_usage_ledger_table.c.session_id == str(session.id)
                        )
                    ).mappings().one()
                )
            assert attempt["attempt_id"] == call.call_id
            assert attempt["identity_id"] == ledger["identity_id"] == "planner-user"
            assert attempt["session_id"] == ledger["session_id"] == str(session.id)
            assert attempt["source"] == ledger["source"] == "composer"
            assert attempt["operation_id"] == context.fence.operation_id
            assert attempt["operation_epoch"] == context.fence.operation_epoch
            assert attempt["lease_token"] == context.fence.lease_token
            assert attempt["settled_at"] is not None
            assert attempt["ledger_entry_id"] == ledger["entry_id"]
            expected_model = call.model_requested if call.model_returned is None else call.model_returned
            assert ledger["model"] == expected_model
            assert ledger["prompt_tokens"] == call.prompt_tokens
            assert ledger["completion_tokens"] == call.completion_tokens
            if expected_status is ComposerLLMCallStatus.SUCCESS:
                assert call.provider_cost == 0.01
                assert call.prompt_tokens == 10 and call.completion_tokens == 5
            else:
                assert call.provider_cost is None and call.provider_cost_source == "not_available"
                assert call.model_returned is None
                assert call.prompt_tokens is None and call.completion_tokens is None

            private_requests = [
                (row, envelope)
                for row in persisted
                for envelope in (row.tool_calls or ())
                if envelope.get("_kind") == "composer_diagnostic_evidence"
                and envelope["evidence"]["status"] == "provider_request"
            ]
            ((private_row, private_envelope),) = private_requests
            assert private_row.role == "audit" and private_row.writer_principal == "compose_loop"
            evidence = ComposerDiagnosticEvidence.from_envelope(
                private_envelope,
                expected_session_id=str(session.id),
                expected_operation_id=context.fence.operation_id,
                expected_tool_call_id=call.call_id,
            )
            assert evidence.operation_epoch == context.fence.operation_epoch
            assert evidence.operation_epoch == attempt["operation_epoch"]
            assert evidence.tool_name == "composer_provider_context" and evidence.status == "provider_request"
            assert call.private_request is not None and call.private_request.canonical is not None
            assert evidence.arguments.content is not None
            assert json.loads(evidence.arguments.content) == json.loads(call.private_request.canonical)
            assert "SDK detached mutation" not in evidence.arguments.content
            assert "actual owned manifest mutation" not in evidence.arguments.content
            assert await sessions.list_composition_proposals(session.id) == []
            _assert_no_pipeline_side_effects(engine)
            return error, integrity
    finally:
        if pause_insert is not None:
            event.remove(engine, "before_cursor_execute", pause_insert)
```

The real builder and matcher run unchanged. `_ScriptedCompletion.__call__`
records the physical kwargs, consumes the single response and raises a supplied
exception by identity. The two mutations are independent: SDK-side mutation
cannot change the owned message; the following owned-input mutation must
change the actual audit hash and trigger the real matcher. The pre-dispatch
private request receipt remains its earlier snapshot. Do not assert either
snapshot equals the later, deliberately mutated bytes. Do not reuse the common
`assert_actual_audits` helper here: it requires common ordinal `None` and
unchanged physical/private hashes; this direct call has ordinal `1`.

The strict diagnostic decoder checks the actual pre-dispatch provider-request
receipt against the live context and actual attempt/call identity. No count of
received-response or provider-failure diagnostics is invented. Public metadata
and the private request receipt are tested separately; this is a bounded direct
fixture privacy check, not full HTTP/UI privacy qualification. This direct
setup does not update preferences, so the unchanged no-effects helper's empty
proposal-events assertion remains appropriate.

- [ ] Replace the complete original three-outcome manifest function,
  including its decorators, with this definition. Keep all three outcomes:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "expected_status"),
    (
        ("success", ComposerLLMCallStatus.SUCCESS),
        ("error", ComposerLLMCallStatus.API_ERROR),
        ("cancel", ComposerLLMCallStatus.CANCELLED),
    ),
)
async def test_direct_planner_manifest_mismatch_is_durable_before_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_outcome: str,
    expected_status: ComposerLLMCallStatus,
) -> None:
    original: BaseException | None = None
    if provider_outcome == "error":
        original = LiteLLMAPIError(
            status_code=503,
            message="provider unavailable",
            llm_provider="test-provider",
            model="anthropic/claude-planner",
        )
    elif provider_outcome == "cancel":
        original = asyncio.CancelledError("actual direct planner provider cancellation")
    response = _response(("emit_pipeline_proposal", {"pipeline": {}}))
    error, integrity = await _direct_manifest_case(
        tmp_path,
        monkeypatch,
        response if original is None else original,
        expected_status,
    )
    if original is None:
        assert error is integrity
    else:
        expected_type = ExceptionGroup if provider_outcome == "error" else BaseExceptionGroup
        assert type(error) is expected_type
        assert isinstance(error, BaseExceptionGroup)
        assert error.message == "Provider original body and audit construction outcomes"
        assert error.exceptions == (original, integrity)
```

The successful physical call refuses with the exact observed integrity object.
For API error and provider cancellation, both the original body exception and
that exact integrity object remain ordered members of the current producer's
exact group. Leaf membership alone does not preserve these oracles.

- [ ] Replace the complete original mismatch-audit cancellation function with
  this native checkpoint race. Observe the real delivered cancellation from
  its owner, delegate unchanged and compare the final exact group members:

```python
@pytest.mark.asyncio
async def test_direct_manifest_mismatch_checkpoint_defers_original_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    native_inserts: list[str] = []
    actual_cancellations: list[asyncio.CancelledError] = []
    real_raise = ProviderCallCustody.raise_deferred_cancellations

    def observe_deferred(
        owner: ProviderCallCustody,
        cancellations: tuple[asyncio.CancelledError, ...],
    ) -> None:
        actual_cancellations.extend(cancellations)
        real_raise(owner, cancellations)

    def pause(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        execution_context: Any,
        many: bool,
    ) -> None:
        if not statement.startswith("INSERT INTO chat_messages"):
            return
        (bound,) = execution_context.compiled_parameters
        envelopes = bound["tool_calls"]
        if bound["role"] != "audit" or not envelopes or envelopes[0].get("_kind") != "llm_call_audit":
            return
        assert not many and native_inserts == []
        native_inserts.append(envelopes[0]["call"]["call_id"])
        entered.set()
        assert release.wait(10), "actual native audit worker was not released"

    monkeypatch.setattr(ProviderCallCustody, "raise_deferred_cancellations", observe_deferred)
    task = asyncio.create_task(
        _direct_manifest_case(
            tmp_path,
            monkeypatch,
            _response(("emit_pipeline_proposal", {"pipeline": {}})),
            ComposerLLMCallStatus.SUCCESS,
            pause_insert=pause,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10), "actual native audit insert was not reached"
        task.cancel("request cancelled during actual native manifest checkpoint")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        error, integrity = await asyncio.wait_for(task, 10)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert len(native_inserts) == 1
    (cancellation,) = actual_cancellations
    assert type(cancellation) is asyncio.CancelledError
    assert cancellation.args == ("request cancelled during actual native manifest checkpoint",)
    assert type(error) is BaseExceptionGroup
    assert error.message == "Provider body and required settlement outcomes"
    assert error.exceptions == (integrity, cancellation)
```

The SQL barrier selects the actual native insert's original bound `tool_calls`
value after checking the INSERT and audit role. SQLAlchemy's current execution
context retains these original Python parameters before DBAPI JSON processing;
this is a finite observation of this real producer, not a parameter parser.
`entered` must fire, the task must remain incomplete until release, the required
worker must join and the helper must read the committed checkpoint before
returning. The helper always removes the listener, including acquire failure;
the test always releases and joins its task. Any timeout or extra native insert
fails the test. The full checkpoint/accounting/privacy assertions in the helper
also run in this race.

The old adapter's cancellation-with-integrity-cause shape is replaced by the
current provider owner's exact ordered group. The actual cancellation object
is observed before the real owner re-raises it; neither an injected replacement
nor a message-only match can satisfy the identity assertion.

- [ ] Add this definition to `tests/unit/web/composer/test_pipeline_planner.py`.
  Its existing imports already provide all names below. An actual external SDK
  `ModelResponse` permits the extra `provider` field; keep explicit `Usage.cost`
  and use `_ScriptedCompletion.requests` as the physical call trace:

```python
@pytest.mark.asyncio
async def test_direct_planner_response_object_retains_served_endpoint(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    completion = _ScriptedCompletion(
        ModelResponse(
            id="direct-served-endpoint",
            model="provider/planner-v1",
            provider="DeepInfra",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost=0.01),
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "direct-served-proposal",
                                "type": "function",
                                "function": {
                                    "name": "emit_pipeline_proposal",
                                    "arguments": json.dumps({"pipeline": _pipeline(tmp_path)}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        )
    )
    recorder = BufferingRecorder()
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)
    assert len(completion.requests) == 1 and completion._responses == []
    (call,) = recorder.llm_calls
    assert call.status is ComposerLLMCallStatus.SUCCESS
    assert call.provider_cost == 0.01
    assert call.provider_served == "DeepInfra"
    assert call.provider_request_id == "direct-served-endpoint"
```

This supplements the common actual persisted endpoint test. Keep the direct
unit's existing manifest equality, SDK detachment, owned-tool mutation,
three-outcome input-mutation and attached-call-suffix controls; its real-fence
accounting and missing-custody controls; and every existing endpoint omission,
primary, advisor and transport test. No helper or import outside this bounded
packet is retired without root's separate current reference proof.

- [ ] Root runs the three new definitions with ordinary serial pytest using
  the existing native invocation, both source roots, unique private logs and
  complete before/after capture from this plan. These are the exact new
  selectors; the first retains its three parameters:

```text
tests/integration/web/composer/test_freeform_pipeline_planner.py::test_direct_planner_manifest_mismatch_is_durable_before_failure
tests/integration/web/composer/test_freeform_pipeline_planner.py::test_direct_manifest_mismatch_checkpoint_defers_original_cancellation
tests/unit/web/composer/test_pipeline_planner.py::test_direct_planner_response_object_retains_served_endpoint
```

Expected: all selected cases pass with native exit `0`, after the forced
manifest refusal and cancellation assertions execute. A failed entered barrier,
constructor, native receipt, ownership or original-object assertion is RED;
retain the full failure and repair its actual cause. Do not weaken the oracle,
return a fabricated planner result, add a provider bypass or skip a definition.

- [ ] Retain or run the exact existing direct controls below under the same
  root-owned recipe, according to the current unchanged-source evidence. Root
  also runs the relevant existing whole-tree gates from Task9. No new runner,
  scanner or collection controller is needed:

```text
tests/unit/web/composer/test_pipeline_planner.py::test_real_planner_call_builds_manifest_from_exact_audited_inputs
tests/unit/web/composer/test_pipeline_planner.py::test_physical_sdk_mutation_cannot_change_planner_owned_inputs
tests/unit/web/composer/test_pipeline_planner.py::test_owned_planner_tool_mutation_after_manifest_is_an_integrity_failure
tests/unit/web/composer/test_pipeline_planner.py::test_owned_planner_input_mutation_is_audited_once_before_integrity_failure
tests/unit/web/composer/test_pipeline_planner.py::test_planner_fixture_uses_real_guard_fence_and_settled_provider_accounting
tests/unit/web/composer/test_pipeline_planner.py::test_planner_provider_accounting_does_not_supply_missing_inline_custody
tests/unit/web/composer/test_pipeline_planner.py::test_planner_physical_helper_refuses_missing_custody_even_with_live_fence
tests/unit/web/composer/test_pipeline_planner.py::test_planner_omits_endpoint_kwargs_when_unset
tests/unit/web/composer/test_pipeline_planner.py::test_planner_ordinary_calls_use_primary_endpoint
```

- [ ] Root reviews complete final owned-file diffs and native evidence, obtains
  fresh SPEC then separate Astra quality verdicts, and stages only the reviewed
  owned paths and consumed plan changes. Root retains all index/ref/object
  writes and the existing execution method. Passing this packet does not
  qualify PostgreSQL, the whole suite, CI, merge, a live provider or deployment.

## Task 9 — Complete migration ledger, RED controls and focused qualification

- [ ] **Before Task1 deletion**, create
  `tests/unit/web/composer/test_private_planner_retirement.py` with the complete
  ordinary pytest unit below. First complete and qualify Tasks2–8 and root's
  adapter-reference checks. This file is a new structural regression; a missing
  file, import error or collection failure does not satisfy its RED.

```python
"""Only the supported direct planner library remains installed."""

from pathlib import Path

from elspeth.web.composer import pipeline_planner


def test_private_facade_is_retired_but_direct_library_is_supported():
    planner_path = Path(pipeline_planner.__file__)
    assert not planner_path.with_name("planning_application.py").exists()
    assert callable(pipeline_planner.plan_pipeline)
```

- [ ] Use the existing environment/capacity/provenance setup under **Runnable
  focused verification and handback** below before these commands. On the
  otherwise qualified guard tree, run the new unit **while the adapter still
  exists**:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp="$COMPOSER_TEMP/retirement-red" tests/unit/web/composer/test_private_planner_retirement.py::test_private_facade_is_retired_but_direct_library_is_supported > "$COMPOSER_CHECKS/retirement-red.log" 2>&1
COMPOSER_EXIT=$?
printf '%s\n' "$COMPOSER_EXIT" > "$COMPOSER_CHECKS/retirement-red.exit"
cat "$COMPOSER_CHECKS/retirement-red.log"
test "$COMPOSER_EXIT" -eq 1
```

Require the completed native log to show the failure at exactly
`assert not planner_path.with_name("planning_application.py").exists()`.
Native exit1 alone is insufficient. Preserve that original RED; no custom
runner, plugin, scanner or collection controller is needed.

- [ ] Apply only Task1's root-released production deletions, then run the same
  structural unit for GREEN:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp="$COMPOSER_TEMP/retirement-green" tests/unit/web/composer/test_private_planner_retirement.py::test_private_facade_is_retired_but_direct_library_is_supported > "$COMPOSER_CHECKS/retirement-green.log" 2>&1
COMPOSER_EXIT=$?
printf '%s\n' "$COMPOSER_EXIT" > "$COMPOSER_CHECKS/retirement-green.exit"
cat "$COMPOSER_CHECKS/retirement-green.log"
test "$COMPOSER_EXIT" -eq 0
```

The GREEN must execute both assertions. This is a refactor existence/API pin;
it does not count as behavioral provider, admission, audit, custody or guard
proof. Run the retained direct-library/common-path modules and the complete
qualification below independently.

### Every original integration function has a disposition

The cases below are from the saved actual268-case registry, not a new count
derived from definitions. The table preserves every original function's intent;
new case counts must be measured by actual pytest collection after execution.
F and P mean the two named integration modules in Tasks5/6.

| Original exact function | Saved registered cases | Executable replacement/retained owner |
| --- | ---: | --- |
| F `test_provider_can_answer_without_graph_mutation_for_any_request_wording` |112| Retain reviewed complete body unchanged; exact common wire/context, no effects, raw candidate/receipt and real independent END. |
| F `test_complete_multi_clause_request_enters_empty_pipeline_planner` |20| Task5 `test_every_request_wording_uses_common_loop_and_audits_provider_timeout`, all original decorators. |
| F `test_send_message_freeform_planner_failure_is_translated` |4| Task5 exact table/setup/assertion replacements; all private-receipt mutation controls retained, cost503 unchanged. |
| F `test_recompose_freeform_planner_failure_is_translated` |1| Task5 malformed physical response and common error DTO; actual recompose custody/no replay/no effects retained. |
| F `test_freeform_progress_reason_stops_blaming_the_provider_for_planner_faults` |1| Retain entire direct-library mapping unit. |
| F `test_policy_detail_codes_decide_policy_blocked` |102| Retain all code/detail parameters and body. |
| F `test_every_reachable_failure_code_has_an_http_envelope` |1| Retain exact closed-vocabulary coverage. |
| F `test_freeform_policy_blocked_copy_blames_neither_provider_nor_operation_id` |1| Retain exact safe policy copy. |
| F `test_send_message_freeform_planner_decline_is_a_normal_assistant_message` |1| Task5 actual common cannot_complete/STOP_CONFIRMED, primary decline and distinct END; direct-library exhaustion remains separately covered. |
| F `test_send_message_ordinary_turn_natural_decline_is_a_normal_assistant_message` |2| Task5 common ADMIT PRIMARY/END or WITHHOLD PRIMARY/END/prose-repair/END; exact 2/4 physical calls, 1/2 independently bound END passes, private superseded/final withheld bytes/hash, canonical admitted segments and public/replay exclusion. |
| F `test_later_explicit_imperative_reaches_planner_and_auto_commits` |1| Task5 common provider `set_pipeline`/EARLY/prose/END, exact request and saved P4 state, intentionally no auto proposal. |
| F `test_freeform_auto_commit_surfaces_interpretation_reviews` |2| Task5 exact common-dialect replacements; full actual acceptance/cohort/amendment/provenance/attempt-ledger tail retained for both base states. |
| P `test_empty_build_stages_one_canonical_pipeline_proposal_for_both_trust_modes` |4| Task6 trust × base replacement, canonical pending base/hash versus truthful direct auto state. |
| P `test_planner_llm_call_audit_persists_the_served_endpoint` |1| Task6 actual persisted common endpoint plus Task8 direct response-object witness. |
| P `test_trust_mode_change_during_planning_revokes_auto_commit_authority` |2| Task6 both in-flight directions; authority A5 separately owns late worker/locked SQL ordering. |
| P `test_referential_empty_build_projects_authoritative_prior_user_requests` |1| Task6 persisted chronological human IDs, nominal shared context/assistant separation, exact actual common wire and canonical routing proposal. |
| P `test_cancellation_during_proposal_create_preserves_trust_mode_lifecycle_without_dispatch_or_state` |2| Task6 auto actual started-P4 companion and explicit actual proposal insert cancellation. |
| P `test_auto_commit_cancellation_survives_rejection_failure_and_repeated_cancel` |1| Task6 actual public P4 failure/all-originals test, Task7 actual lower-level creation+failed rejection original identity. |
| P `test_structural_entry_selects_planner_or_ordinary_compose_loop` |2| Task6 same actual common provider loop for both graphs, exact current state/request and SQL no effects. |
| P `test_planner_audit_failure_publishes_no_proposal_authority_or_state` |1| Task6 actual native provider checkpoint fault before semantic effects; existing common P4 atomicity tests retained separately. |
| P `test_freeform_matching_provider_cancellation_persists_once_without_self_cause` |1| Retain full body unchanged. |
| P `test_freeform_planner_manifest_mismatch_is_durable_before_failure` |3| Task8 direct actual manifest success/error/cancel and native SQL evidence. |
| P `test_freeform_manifest_mismatch_audit_write_defers_request_cancellation_exactly_once` |1| Task8 actual direct manifest refusal plus native provider checkpoint insert race, original cancellation and integrity retained. |
| P `test_freeform_compose_routes_to_the_planner_without_pipeline_side_effects` |1| Task6 actual common SDK timeout and four unchanged zero-effect tables. |

### Every retired mixed-unit oracle has a current owner

The deleted adapter cohort itself is intentionally gone. Do not preserve an
unused private audit writer merely to claim cross-writer parity. Retained
shared contracts are pinned at these exact surviving production test ports:

| Retired group | Current required owner/test |
| --- | --- |
| Private success/decline/cohort cancellation and `_plan_and_stage_empty` | `test_audit_cohort_settlement.py::test_provider_timeout_after_inline_tool_turn_persists_each_record_exactly_once`, `::test_cancel_mid_cohort_drains_the_shield_and_lands_the_whole_cohort`, `::test_cancel_then_mid_cohort_failure_lands_nothing_and_keeps_the_cancel`; actual public A5 P4 cancellation tests. |
| Private malformed executor-result hash writer | Task2 ports the original invocation, canary and exact `AuditIntegrityError` to `test_compose_loop_persistence.py::test_surviving_tool_writer_rejects_malformed_bound_content_hash`: complete real SQL no-write comparison on refusal, then a same-context/same-writer positive continuation with actual persisted public/private rows and returned binding. Its valid 64-character lower-case hexadecimal value is a syntactic projection control, not proof of executor graph-digest computation. |
| Private refused-prose writer | Existing `test_private_evidence_persistence.py::test_normal_private_evidence_requires_exact_bound_set_before_any_write`, `::test_private_insert_failure_rolls_back_assistant_and_tool`, `::test_private_cohort_exact_replay_deduplicates_and_conflict_fails`, `::test_private_replay_finds_and_refuses_tampered_outer_binding`, and F natural decline WITHHOLD. Preserve root's P4 fault repairs. |
| One-shot private candidate cohort | `test_compose_loop_persistence.py::test_private_evidence_keeps_received_ids_when_real_finalizer_rebinds_candidate`, `::test_private_evidence_keeps_valid_json_rejected_wire_envelope_and_actual_error`; `test_private_evidence_persistence.py::test_normal_tool_turn_retains_private_authored_content`, `::test_aborted_batch_after_prior_checkpoint_retains_original_inline_receipt_in_alternate_sql`. |
| Private graph-recovery handoff | `test_planner_graph_recovery.py::test_handoff_does_not_construct_or_capture_a_candidate`, `::test_first_call_handoff_does_not_need_discovery`, `::test_explicit_restore_returns_exact_park_identity`, `::test_mixed_recovery_batch_is_rejected_before_dispatch`. No common routing restoration. |
| Private provider-context projection | Same module's `test_rendered_prompt_parts_are_exact_and_capture_bounds_are_honest`, retained `test_nonempty_compose_sends_current_amended_parts_through_real_provider_boundary`, all direct-library context tests, F112 exact bounded-wire cases, and the qualified common/direct request-byte budget tests. Diagnostic-capture bounds alone do not prove physical provider admission. |
| Private preview-preflight callbacks/stage module | Retain every common pending-review/runtime preflight test, especially `test_missing_blob_metadata_rejects_composer_preflight_without_content_proof_or_state_commit`; Task5 real pending proposal acceptance/cohort tests. Direct planner preview/library tests remain. |
| Private required-control finalizer factory | `test_required_control_autowire.py::TestAutoWireIdempotence::test_every_no_op_path_is_identity_preserving` direct assertions; `test_autowire_and_set_pipeline_agree_on_empty_string_validation_failure` and all remaining real common candidate finalization/disclosure tests. |
| Private visible-tool/gap cohort | `test_visible_tool_event_persistence.py::test_actual_persist_reload_keeps_original_effective_and_long_selected_result`, `::test_visible_companion_rolls_back_with_its_actual_tool_cohort`, `::test_public_response_gap_has_actual_fence_and_call_binding`. |
| Historical private decline END budget/terminal | The old `test_actual_planner_decline_review_shares_call_budget_and_saved_terminal` is already superseded, not a current deletion selector. Preserve `test_run_composer_turn.py::test_actual_planner_decline_end_failure_preserves_audit_and_never_publishes`, Task4 actual common END request-rejection evidence, Task5 actual primary/stop/END with exact native counts, cost companion `test_actual_common_roles_share_one_settled_cost_policy` and actual primary/EARLY/END public cost tests; direct planner continuation budget tests unchanged. |
| Private creation cancellation/nonzero child rejection | Task7 `test_pipeline_rejection_required.py::test_creation_then_rejection_retains_actual_originals_at_nonzero_scope` owns real finish-once creation/rejection7/12 tickets, actual original objects, SQL truth and telemetry failure; retain `test_pipeline_rejection_callers.py::test_actual_canonical_failure_rejects_under_registered_child_and_keeps_body` and mounted manual required-child cancellation tests. |
| Private LLM-summary interleave/parity host | Task2 `test_llm_call_audit_summary.py::test_surviving_writer_uses_shared_summary` exercises the actual surviving writer for all finish-reason families beside unchanged literal JSON/key-order pins. Retain direct library `test_each_transient_api_retry_consumes_and_audits_a_wire_attempt` and `test_service_unavailable_on_second_planning_call_audits_each_retry`; those pin physical/semantic attempt ordering, not SQL interleave of the removed writer. |

The names in this ledger are source references, not new claims that those
tests passed on the final packet. Collect/run the real modules below.

### Actual regression controls

- [x] Exercise the current-request negative control inside the existing
  `test_every_request_wording_uses_common_loop_and_audits_provider_timeout`
  test in `tests/integration/web/composer/test_freeform_planner_failure_translation.py`.
  Use its existing `monkeypatch` fixture and finite `CommonSDK` boundary; do not
  create a plugin, runner, helper module or collection controller. Root freezes
  source writes for each native run and retains a complete pre-control copy.
  First run the unmodified full20 wording cases, preserving every parameter:

```bash
COMPOSER_TIMEOUT_FILE="tests/integration/web/composer/test_freeform_planner_failure_translation.py"
COMPOSER_TIMEOUT_TEST="$COMPOSER_TIMEOUT_FILE::test_every_request_wording_uses_common_loop_and_audits_provider_timeout"
COMPOSER_TIMEOUT_CASE="${COMPOSER_TIMEOUT_TEST}[explanation]"
cd "$COMPOSER_WORKTREE" && cp -- "$COMPOSER_TIMEOUT_FILE" "$COMPOSER_CHECKS/current-request-original.py"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 -v --tb=short -o 'pythonpath=src elspeth-lints/src' --basetemp="$COMPOSER_TEMP/current-wire-positive" "$COMPOSER_TIMEOUT_TEST" > "$COMPOSER_CHECKS/current-wire-positive.log" 2>&1
COMPOSER_EXIT=$?
printf '%s\n' "$COMPOSER_EXIT" > "$COMPOSER_CHECKS/current-wire-positive.exit"
cat "$COMPOSER_CHECKS/current-wire-positive.log"
test "$COMPOSER_EXIT" -eq 0
```

  Read the complete native result and require the actual full20 parameter cases
  to pass. The saved registry is not a substitute for this execution. After
  closing its source capture, temporarily replace this unique existing block
  in that one test only, using the editor:

```python
    assert original_timeout == 20.0
    assert type(original_limits[3]) is Decimal
    assert original_limits[4] == 2

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": message, "operation_id": operation_id, "state_id": None},
    )
```

  Replace it with this complete ordinary-fixture control; all later original
  HTTP, audit, cost, custody, privacy and no-effect assertions remain unchanged:

```python
    assert original_timeout == 20.0
    assert type(original_limits[3]) is Decimal
    assert original_limits[4] == 2

    original_builder = composer._build_messages
    corrupted_positions: list[int] = []
    wrong_current_request = "Controlled wrong current request"

    def corrupt_current_request(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        built = original_builder(*args, **kwargs)
        current = built[-1]
        assert current["role"] == "user" and current["content"] == message
        assert all(row["content"] != message for row in built[:-1] if row["role"] == "user")
        corrupted_positions.append(len(built) - 1)
        built[-1] = {**current, "content": wrong_current_request}
        return built

    with monkeypatch.context() as current_request_control:
        current_request_control.setattr(composer, "_build_messages", corrupt_current_request)
        settled = settle_sync(
            client,
            client.app,
            path=f"/api/sessions/{session_id}/messages",
            body={"content": message, "operation_id": operation_id, "state_id": None},
        )

    # Observe the real SDK capture before the unchanged HTTP-status checks.
    assert len(corrupted_positions) == 1
    sdk.assert_awaited_once()
    common.assert_awaited_once()
    actual = common.await_args
    assert actual is not None
    (request,) = script.requests
    assert request["messages"][corrupted_positions[0]]["role"] == "user"
    assert request["messages"][corrupted_positions[0]]["content"] == wrong_current_request
    assert_initial_wire(request, text=message, state=actual.args[2], session_id=session_id)
```

  The bound original builder is called with all original arguments. Its actual
  last row is the current human message before the service appends the two
  recovery/history rows. The control checks that boundary and the absence of
  an earlier duplicate before changing only that row's content. This is bound
  to the current raw-text builder; if a later builder frames that row as JSON,
  rebind the control from its real source before running it. A precondition
  failure before SDK entry is an invalid control, not its required RED.

  `CommonSDK` captures the real transmitted kwargs before checking the current
  request. Its assertion is caught by the real asynchronous operation worker;
  therefore the unchanged `assert_initial_wire` is also called on that untouched
  capture before the original HTTP-status assertion. No expected capture,
  private audit record, helper implementation or request argument is modified.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 -v --tb=short -o 'pythonpath=src elspeth-lints/src' --basetemp="$COMPOSER_TEMP/current-wire-negative" "$COMPOSER_TIMEOUT_CASE" > "$COMPOSER_CHECKS/current-wire-negative.log" 2>&1
COMPOSER_EXIT=$?
printf '%s\n' "$COMPOSER_EXIT" > "$COMPOSER_CHECKS/current-wire-negative.exit"
cat "$COMPOSER_CHECKS/current-wire-negative.log"
test "$COMPOSER_EXIT" -eq 1
```

  Require the native failure specifically at `assert_initial_wire` in
  `tests/helpers/composer_common_sdk.py`, on its current-user-content assertion
  `assert any(row["role"] == "user" and row["content"] == text for row in messages)`.
  The preceding checks must prove one actual SDK entry and that the changed
  builder row reached its captured wire position. A different exception, the
  SDK-entry checks, setup/collection failure, or a 500-versus-422 assertion does
  not qualify. Retain this RED and close its capture. The context manager has
  already restored the instance patch; pytest teardown/process exit removes
  the remaining runtime fixtures. Restore the exact original source block with
  the editor, then require complete-file equality to the pre-control copy:

```bash
cd "$COMPOSER_WORKTREE" && cmp -- "$COMPOSER_TIMEOUT_FILE" "$COMPOSER_CHECKS/current-request-original.py"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 -v --tb=short -o 'pythonpath=src elspeth-lints/src' --basetemp="$COMPOSER_TEMP/current-wire-restored" "$COMPOSER_TIMEOUT_CASE" > "$COMPOSER_CHECKS/current-wire-restored.log" 2>&1
COMPOSER_EXIT=$?
printf '%s\n' "$COMPOSER_EXIT" > "$COMPOSER_CHECKS/current-wire-restored.exit"
cat "$COMPOSER_CHECKS/current-wire-restored.log"
test "$COMPOSER_EXIT" -eq 0
```

  Require restored native exit0 before releasing the source freeze. The
  following copied-request hash control remains separate: it checks the audit
  assertion, while this control changes actual pre-SDK builder output.

- [x] Control `assert_actual_audits` against one actual completed two-call
  no-effects case, then temporarily change exactly one SDK-captured request's
  copied message text before invoking the helper in that test. Require native
  exit1 at its hash equality; restore and require exit0. This corrupts the
  expected physical evidence, not persisted SQL or production audit code:

```python
    script.requests[0]["messages"][-1]["content"] += "\ncontrolled expected-wire corruption"
    assert_actual_audits(engine, script.requests, ("success", "success"), session_id=str(session.id))
```

Use it only in a no-effects two-call test. Preserve the actual provider
checkpoint fault test's `fired == [sql_failure]` control independently.

Executed on 2026-10-22 with ordinary serial pytest: the unchanged 20
wording cases and actual two-call no-effects case passed (21 cases). The
pre-SDK current-request corruption failed at the unchanged current-request
assertion; exact source restoration passed. Independently corrupting the copied
SDK evidence failed at the audit hash equality; exact restoration passed.
These complete controls and restored sources do not establish final combined
integration, PostgreSQL, signing or release qualification.

- [ ] Run both actual trust flips and the qualified authority companion's
  held P4/preference lock-order tests. Its defined ceiling-current and late
  locked-predicate mutants must fail their behavioral tests; H2.3 cannot waive
  these because ordinary provider tests have the expected call counts.
- [ ] Run Task7 both telemetry parameters and qualified public repeated-cancel
  cases. Assert original object identity, actual completed tickets and SQL truth,
  not merely exception class/group length.
- [ ] Run unchanged cost-unavailable503 plus the companion's unknown/over,
  actual-role, request-byte/token, retry-original and failed-settlement tests.
  Its unknown-to-zero and charge-before-settlement mutants must fail. The new
  `CommonProviderCostRequest` is a typed view of actual detached fields; no
  H2.3 script removes real tools/messages/max-token fields to evade policy.

### Runnable focused verification and handback

- [ ] Root records the actual host/cwd/branch/HEAD and source ownership/capacity
  before execution. Use the existing interpreter. Do not install, rebuild,
  relink a venv or call a live provider. Record both package import roots at
  execution time; these commands do not run during plan writing.

```bash
COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)"
COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python"
COMPOSER_PRIMARY="$(git rev-parse --path-format=absolute --git-common-dir)"
COMPOSER_PRIMARY="${COMPOSER_PRIMARY%/.git}"
COMPOSER_CHECKS="$COMPOSER_WORKTREE/.claude/lanes/first-carry-20261021/h2-retirement-checks"
COMPOSER_TEMP="$COMPOSER_PRIMARY/.claude/h2-retirement-temp"
cd "$COMPOSER_WORKTREE" && mkdir -p "$COMPOSER_CHECKS" "$COMPOSER_TEMP"
export PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src"
export TMPDIR="$COMPOSER_TEMP"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

Both paths must resolve inside this implementation checkout. The short private
temporary directory remains inside the primary project, with the root's
concurrency permission; no external temporary directory is used.

- [ ] Measure actual collection and bind native logs to the settled source:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest --collect-only -q -n 0 tests/integration/web/composer/test_freeform_planner_failure_translation.py tests/integration/web/composer/test_freeform_pipeline_planner.py > "$COMPOSER_CHECKS/collection.log" 2>&1
COMPOSER_EXIT=$?
printf '%s\n' "$COMPOSER_EXIT" > "$COMPOSER_CHECKS/collection.exit"
cat "$COMPOSER_CHECKS/collection.log"
test "$COMPOSER_EXIT" -eq 0
```

Compare function identities and parameter coverage to the ledger. Any deliberate
parameter split adds real coverage; no reduction can silently disappear under a
generic collection-green claim. The old268 number describes the saved baseline,
not an expected new total.

- [ ] Run the complete affected focused modules with native exit capture:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp="$COMPOSER_TEMP/focused" tests/unit/web/composer/test_private_planner_retirement.py tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/composer/test_llm_call_audit_summary.py tests/unit/web/composer/test_provider_context_planner_integration.py tests/unit/web/composer/test_pipeline_planner.py tests/unit/web/composer/test_pipeline_planner_protocol_rejections.py tests/unit/web/composer/test_planner_graph_recovery.py tests/unit/web/composer/test_runtime_preflight_pending_review_verification.py tests/unit/web/composer/test_required_control_autowire.py tests/unit/web/composer/test_audit_cohort_settlement.py tests/unit/web/composer/test_compose_loop_persistence.py tests/unit/web/composer/test_private_evidence_persistence.py tests/unit/web/composer/test_visible_tool_event_persistence.py tests/unit/web/composer/test_advisor_checkpoint.py tests/unit/web/sessions/test_run_composer_turn.py tests/unit/web/sessions/test_pipeline_rejection_callers.py tests/unit/web/sessions/test_pipeline_rejection_required.py tests/integration/web/composer/test_freeform_planner_failure_translation.py tests/integration/web/composer/test_freeform_pipeline_planner.py tests/integration/web/composer/test_common_mutation_authority_cancellation.py > "$COMPOSER_CHECKS/focused.log" 2>&1
COMPOSER_EXIT=$?
printf '%s\n' "$COMPOSER_EXIT" > "$COMPOSER_CHECKS/focused.exit"
cat "$COMPOSER_CHECKS/focused.log"
test "$COMPOSER_EXIT" -eq 0
```

- [ ] Run Ruff check/format check on all touched Python files and both original
  integration modules; resolve unused imports rather than adding suppressions.
  Run affected package mypy under the repository's existing setup.
- [ ] Root runs affected whole-tree gates using `CONTRIBUTING.md`: attribute,
  soft-mapping census, masquerade, boundary tests, mock baseline and session
  writer/fingerprint gates can legitimately move when a file disappears.
  Regenerate pins using their actual producer and review only explained site
  removal. Do not manually edit HMAC metadata or claim keyless signed clearance.
- [ ] Root qualifies the final combined production tree with the canonical
  detached broad gate, including serial PostgreSQL for the guard SQL changes.
  Hash the actual measured inputs before/after; changed inputs invalidate a
  run. Record native completion exits and relevant RED controls. Existing
  earlier diagnostic/plan-review GREEN is not final qualification.
- [ ] Hand back exact changed paths/source binding and native results. Root
  alone stages/commits after current status, pathspec inspection and normal
  branch-safety check. Suggested implementation title:
  `refactor(composer): retire unreachable private planning facade`.
  No lane notes, logs or temporary plugins are staged. This plan-writing task
  makes no stage, commit, push, merge, signing or deployment claim.

Execution completes only when this actual retirement and every retained guard
contract pass on the combined tree. A fixture/model assertion, generic500,
unreached race event or missing actual audit remains a failure to resolve;
it is never grounds to delete a meaningful oracle.

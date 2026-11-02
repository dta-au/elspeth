"""An actual V4 PRIMARY-to-END outer source before checkpoint payment."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import advisor_checkpoint as advisor_module
from elspeth.web.composer import continuation_checkpoint_end_physical as physical_module
from elspeth.web.composer import continuation_primary as primary_module
from elspeth.web.composer.continuation import EndFrame
from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointUnboundRefusalTerminal
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    prepare_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_end_producer import build_checkpoint_end_input
from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_paid import (
    CheckpointPreparedSource,
    checkpoint_phase_source_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_unbound_refusal import read_v4_checkpoint_unbound_refusal_on_connection
from elspeth.web.sessions.continuation_end import _retain, enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt, PrimaryProviderResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.asyncio
async def test_actual_v4_end_review_has_one_unpaid_phase_source_before_child_admission(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    runtime, work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None and type(runtime.source.policy) is ContinuationAdmissionPolicyV4
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    try:

        async def primary_completion(**_kwargs):
            return _reply("This is the actual retained PRIMARY candidate awaiting its real END review.")

        monkeypatch.setattr(litellm, "acompletion", primary_completion)
        primary_failures: list[str] = []
        original_pre_sdk_failure = primary_module._pre_sdk_failure

        def observed_pre_sdk_failure(error, *, preparing):
            primary_failures.append(f"{type(error).__name__}: {str(error)[:200]}")
            return original_pre_sdk_failure(error, preparing=preparing)

        monkeypatch.setattr(primary_module, "_pre_sdk_failure", observed_pre_sdk_failure)
        phases = ContinuationLivePhases(
            service=runtime.materialization.service,
            authority=runtime.materialization.authority,
            work=work,
            provider_owner=runtime.bindings.provider_owner,
            lease=lease,
            progress=runtime.bindings.progress,
        )
        phases.bind_initial(runtime)
        primary = await run_continuation_primary(
            service=phases.service,
            authority=phases.authority,
            selected=runtime.source.selected,
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=work,
            provider_owner=runtime.bindings.provider_owner,
            timeout=runtime.source.policy.actions.primary.total,
        )
        if type(primary) is PrimaryNoSdkRefusalReceipt:
            with locked_session_transaction(store[0], str(store[4])) as conn:
                refusal = conn.execute(
                    select(chunks.c.payload).where(
                        chunks.c.session_id == str(store[4]),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == primary.result_chunk_key,
                    )
                ).scalar_one()
            pytest.fail(f"PRIMARY refused before END gate setup: {json.loads(refusal)['reason']}; causes={primary_failures}")
        assert type(primary) is PrimaryProviderResult
        phases.accept_primary(primary)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            current = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
        selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None
        assert type(entered.source.selected.stack.frames[-1]) is EndFrame
        current = await _retain(phases, entered.source.selected, "end_review_dispatched")
        with locked_session_transaction(store[0], str(store[4])) as conn:
            stored = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
            source = checkpoint_phase_source_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=stored
            )
            assert source is not None
            assert source.phase == "end" and source.kind == "end_review"
            assert source.current == stored and source.source == stored
            assert current.checkpoint_id == stored.checkpoint_id
            root = decode_continuation_snapshot(stored.payload_json)
            assert root.pending_settlement_ids == ()
            assert [
                (entry.effect_id, entry.disposition) for entry in root.effect_frontier if entry.effect_id == source.outer_effect_key
            ] == [(source.outer_effect_key, "dispatched")]
            original = conn.execute(
                select(effects.c.owner).where(
                    effects.c.session_id == str(store[4]),
                    effects.c.operation_id == running.claim.operation_id,
                    effects.c.effect_key == source.outer_effect_key,
                )
            ).scalar_one()
            savepoint = conn.begin_nested()
            try:
                changed = conn.execute(
                    update(effects)
                    .where(
                        effects.c.session_id == str(store[4]),
                        effects.c.operation_id == running.claim.operation_id,
                        effects.c.effect_key == source.outer_effect_key,
                    )
                    .values(owner="provider")
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match="unpaid original effect"):
                    checkpoint_phase_source_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=stored
                    )
            finally:
                savepoint.rollback()
            assert original == "composer_end"
            assert (
                checkpoint_phase_source_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=stored
                )
                == source
            )
    finally:
        await lease.close()
        work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_v4_end_gate_input_is_issued_and_prepared_before_physical_attempt(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    runtime, work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    try:

        async def primary_completion(**_kwargs):
            return _reply("The actual PRIMARY candidate must be reviewed before it can be published.")

        monkeypatch.setattr(litellm, "acompletion", primary_completion)
        primary_failures: list[str] = []
        original_pre_sdk_failure = primary_module._pre_sdk_failure

        def observed_pre_sdk_failure(error, *, preparing):
            primary_failures.append(f"{type(error).__name__}: {str(error)[:200]}")
            return original_pre_sdk_failure(error, preparing=preparing)

        monkeypatch.setattr(primary_module, "_pre_sdk_failure", observed_pre_sdk_failure)
        phases = ContinuationLivePhases(
            service=runtime.materialization.service,
            authority=runtime.materialization.authority,
            work=work,
            provider_owner=runtime.bindings.provider_owner,
            lease=lease,
            progress=runtime.bindings.progress,
        )
        phases.bind_initial(runtime)
        primary = await run_continuation_primary(
            service=phases.service,
            authority=phases.authority,
            selected=runtime.source.selected,
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=work,
            provider_owner=runtime.bindings.provider_owner,
            timeout=runtime.source.policy.actions.primary.total,
        )
        if type(primary) is PrimaryNoSdkRefusalReceipt:
            with locked_session_transaction(store[0], str(store[4])) as conn:
                refusal = conn.execute(
                    select(chunks.c.payload).where(
                        chunks.c.session_id == str(store[4]),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == primary.result_chunk_key,
                    )
                ).scalar_one()
            pytest.fail(f"PRIMARY refused before END gate setup: {json.loads(refusal)['reason']}; causes={primary_failures}")
        assert type(primary) is PrimaryProviderResult
        phases.accept_primary(primary)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            current = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
        selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None
        entered_selected = entered.source.selected

        prepared_sources: list[CheckpointPreparedSource] = []
        original_sources = []
        issued_permits = []
        original_issue = physical_module.observe_checkpoint_end_unbound_refusal

        def issue_original(source, *, failure):
            with pytest.raises(AuditIntegrityError, match="original source producer"):
                original_issue(replace(source), failure=failure)
            permit = original_issue(source, failure=failure)
            with pytest.raises(TypeError, match="private issuer"):
                replace(permit, _issue_key=object())
            issued_permits.append(permit)
            return permit

        def refuse_prepared_request(**_kwargs):
            raise ComposerContextUnsupported("prepared_request_bytes")

        monkeypatch.setattr(physical_module, "observe_checkpoint_end_unbound_refusal", issue_original)
        monkeypatch.setattr(advisor_module, "prepare_provider_input", refuse_prepared_request)

        async def intercept_original_end_gate(**arguments):
            from elspeth.web.composer.content_scope import composition_scope_for_operation

            phase_owner = arguments["checkpoint_phase_owner"]
            gate_locals = arguments["checkpoint_end_input"]
            assert type(phase_owner) is CheckpointPhaseOwner
            assert phase_owner.selected.stack.cursor == entered_selected.stack.cursor + 1
            captured = await capture_continuation_runtime_owners(
                phase_owner.runtime, selected=phase_owner.selected, inventory=phase_owner.inventory
            )
            build_checkpoint_end_input(phase_owner=phase_owner, gate_locals=gate_locals, captured=captured)
            with pytest.raises(AuditIntegrityError, match="original"):
                build_checkpoint_end_input(phase_owner=phase_owner, gate_locals=replace(gate_locals), captured=captured)
            source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
            physical_module.require_checkpoint_end_detached_source(source)
            original_detached_state = source.detached.live.state
            source.detached.live.state = replace(original_detached_state, version=original_detached_state.version + 1)
            try:
                with pytest.raises(AuditIntegrityError):
                    physical_module.require_checkpoint_end_detached_source(source)
            finally:
                source.detached.live.state = original_detached_state
            physical_module.require_checkpoint_end_detached_source(source)
            prepared_sources.append(source.prepared)
            original_sources.append(source)
            advisor = phases.service._advisor_checkpoint
            bindings = phase_owner.runtime.bindings
            content_scope = composition_scope_for_operation(
                phase_owner.runtime.live.state,
                session_id=bindings.session_id,
                session_operation_context=bindings.session_operation_context,
                session_operation_authority=phases.service._require_sessions_service().session_operation_authority,
            )
            request = advisor._build_checkpoint_arguments(
                phase="end",
                state=phase_owner.runtime.live.state,
                content_scope=content_scope,
                env_ref_names=phase_owner.runtime.env_ref_names,
                user_message=bindings.message,
                advisor_review_state=gate_locals.review_state,
                publication_review=gate_locals.publication_review,
            )
            await advisor._call_advisor_with_audit(
                request,
                recorder=phase_owner.runtime.live.recorder,
                provider_owner=phases.provider_owner,
                structured_output=True,
                _outcome=source.capture,
                checkpoint_end_source=source,
            )
            raise AssertionError("unbound END request reached provider result")

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", intercept_original_end_gate)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            physical_before = tuple(conn.execute(select(physical.c.attempt_id).where(physical.c.session_id == str(store[4]))).scalars())
        terminal_result = await execute_continuation_end_review(phases, entered_selected)
        assert type(terminal_result) is EndCheckpointUnboundRefusalTerminal
        terminal = terminal_result.verified
        assert len(prepared_sources) == len(original_sources) == len(issued_permits) == 1
        prepared_source = prepared_sources[0]
        assert prepared_source.phase == "end" and prepared_source.end_input_ref is not None
        assert terminal.prepared == prepared_source.prepared_selected
        assert terminal.error.http_status == 422
        with pytest.raises(AuditIntegrityError, match="original pre-admission boundary"):
            physical_module.observe_checkpoint_end_unbound_refusal(
                original_sources[0], failure=ComposerContextUnsupported("prepared_request_bytes")
            )
        original_failure = issued_permits[0].failure
        object.__setattr__(issued_permits[0], "failure", ComposerContextUnsupported("prepared_request_bytes"))
        try:
            with pytest.raises(AuditIntegrityError, match="original source or failure"):
                physical_module.require_original_checkpoint_unbound_refusal(issued_permits[0])
        finally:
            object.__setattr__(issued_permits[0], "failure", original_failure)
        physical_module.require_original_checkpoint_unbound_refusal(issued_permits[0])
        with locked_session_transaction(store[0], str(store[4])) as conn:
            stored = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
            assert stored == terminal.terminal
            assert prepared_source.prepared_selected.cursor == entered_selected.stack.cursor + 2
            assert decode_continuation_snapshot(stored.payload_json).pending_settlement_ids == ()
            assert (
                read_v4_checkpoint_unbound_refusal_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    names=original_sources[0].phase_owner.runtime.env_ref_names,
                )
                == terminal
            )
            assert conn.dialect.name == "sqlite"
            trigger_name = "trg_composer_async_operations_transition_guard"
            trigger_sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
            ).scalar_one()
            assert type(trigger_sql) is str
            savepoint = conn.begin_nested()
            try:
                conn.exec_driver_sql(f"DROP TRIGGER {trigger_name}")
                conn.execute(
                    update(jobs)
                    .where(jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id)
                    .values(continuation_policy_sha256="0" * 64)
                )
                with pytest.raises(AuditIntegrityError, match="selected root differs from its original persisted admission contract"):
                    read_v4_checkpoint_unbound_refusal_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                    )
            finally:
                savepoint.rollback()
            assert (
                conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)).scalar_one()
                == trigger_sql
            )
            blocked = conn.begin_nested()
            try:
                with pytest.raises(IntegrityError, match="immutable transition"):
                    conn.execute(
                        update(jobs)
                        .where(jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id)
                        .values(continuation_policy_sha256="0" * 64)
                    )
            finally:
                blocked.rollback()
            assert (
                tuple(conn.execute(select(physical.c.attempt_id).where(physical.c.session_id == str(store[4]))).scalars())
                == physical_before
            )
            with pytest.raises(AuditIntegrityError, match="current END guard inventory"):
                read_v4_checkpoint_unbound_refusal_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    names=original_sources[0].phase_owner.runtime.env_ref_names ^ frozenset({"foreign_guard_name"}),
                )
            original_attempt = conn.execute(
                select(physical).where(
                    physical.c.session_id == str(store[4]),
                    physical.c.source == "composer",
                    physical.c.operation_id == running.session_operation_context.fence.operation_id,
                    physical.c.operation_epoch == running.session_operation_context.fence.operation_epoch,
                )
            ).first()
            assert original_attempt is not None
            savepoint = conn.begin_nested()
            try:
                forged = dict(original_attempt._mapping)
                forged.update(attempt_id=str(uuid4()), settled_at=None, ledger_entry_id=None)
                conn.execute(insert(physical).values(**forged))
                with pytest.raises(AuditIntegrityError, match="orphan physical attempt"):
                    read_v4_checkpoint_unbound_refusal_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                    )
            finally:
                savepoint.rollback()
            assert (
                read_v4_checkpoint_unbound_refusal_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    names=original_sources[0].phase_owner.runtime.env_ref_names,
                )
                == terminal
            )
        with pytest.raises(AuditIntegrityError, match="current prepared source"):
            runtime.materialization.service._require_sessions_service().refuse_v4_checkpoint_unbound_source_sync(permit=issued_permits[0])
    finally:
        await lease.close()
        work.coordinator.assert_completed()

"""Actual first-slice V4 END terminal publication through one SQL transaction."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic
from uuid import UUID, uuid4

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer import continuation_checkpoint_end_complete_terminal as terminal_projection
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_checkpoint_end_complete_terminal import project_end_complete_terminal
from elspeth.web.composer.continuation_checkpoint_end_computation import compute_checkpoint_end_gate
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    checkpoint_end_physical_custody,
    observe_checkpoint_end_physical_return,
    prepare_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.state import CompositionState
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.composer_app_services import ComposerAppServices
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.composer_finalization import ComposerFinalizationInputs, ContinuationFinalizationBudget
from elspeth.web.sessions.composer_turn import _SEND_LABELS, ComposerTurnObservation, _discard_progress
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_terminal import persist_end_complete_terminal_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_owed import retain_v4_checkpoint_end_owed_on_connection
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_advisor_checkpoint import _checkpoint_reply, simple_state
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_composition_initialization as composition_fixture
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store


def test_original_absent_preflight_accepts_only_unchanged_valid_site_free_nonblob_graph(monkeypatch):
    graph = simple_state.__wrapped__().to_dict()
    graph["nodes"] = []
    graph["outputs"] = [{"name": "rows", "plugin": "csv", "options": {"path": "output.csv"}, "on_write_failure": "discard"}]
    state = CompositionState.from_dict(graph)
    check = terminal_projection._original_absent_preflight_is_qualified
    assert state.validate().is_valid and not terminal_projection.interpretation_sites(state)
    assert check(state, initial_version=state.version, last_preflight=None)
    assert not check(state, initial_version=state.version - 1, last_preflight=None)
    source = state.sources["source"]
    blob = replace(state, sources={"source": replace(source, options={**source.options, "blob_ref": "blob-bound"})})
    assert not check(blob, initial_version=blob.version, last_preflight=None)
    invalid = replace(state, outputs=(replace(state.outputs[0], name="unbound"),))
    assert not invalid.validate().is_valid
    assert not check(invalid, initial_version=invalid.version, last_preflight=None)
    with monkeypatch.context() as scoped:
        scoped.setattr(terminal_projection, "interpretation_sites", lambda _state: (object(),))
        assert not check(state, initial_version=state.version, last_preflight=None)


async def _original_finalization_inputs(phases, source, selected):
    """Use the same ordinary END bootstrap and live owners as its publisher."""
    from elspeth.web.composer.continuation import ComposeFrame

    service, runtime = phases.service, source.detached
    running = phases.work.running
    assert running is not None and type(selected.stack.frames[0]) is ComposeFrame
    owner = phases.work.coordinator.sql_deadline_owner
    assert owner is not None
    compose = selected.stack.frames[0]
    bootstrap = decode_continuation_bootstrap(next(chunk for chunk in selected.chunks if chunk.reference == compose.bootstrap))
    values = bootstrap_values(bootstrap)
    sessions = service._require_sessions_service()
    session = await sessions.get_session(UUID(selected.stack.logical.session_id))
    base = values.recovery_context.turn_start_state_id
    assert base is not None
    base_id = UUID(base)
    state_record = await sessions.get_state(base_id)
    bindings = runtime.bindings
    policy = service._policy_context
    from elspeth.web.composer.progress import ComposerProgressRegistry
    from elspeth.web.sessions.routes._helpers import _SessionComposeLockRegistry

    services = ComposerAppServices(
        session_service=sessions,
        composer_service=service,
        settings=service._settings,
        catalog_service=policy._catalog,
        operator_profile_registry=policy._profile_registry,
        scoped_secret_resolver=service._secret_service,
        session_engine=phases.authority._engine,
        interpretation_surfacing=service._interpretation_surfacing,
        plugin_snapshot_for_user_id=policy._snapshot_factory,
        progress_registry=ComposerProgressRegistry(),
        compose_locks=_SessionComposeLockRegistry(),
        composer_operation_authority=phases.authority,
    )
    return ComposerFinalizationInputs(
        service=sessions,
        session=session,
        recovery_context=values.recovery_context,
        lease=phases.lease,
        compose_base_state_id=base_id,
        running=running,
        observation=ComposerTurnObservation(required_work=phases.work.coordinator, compose_base_state_id=base_id),
        progress_sink=bindings.progress if bindings.progress is not None else _discard_progress,
        state=values.initial_state,
        chat_ingress={} if bindings.ingress is None else bindings.ingress,
        chat_ingress_inputs=() if bindings.chat_ingress_inputs is None else bindings.chat_ingress_inputs,
        prior_completion_gates_facts=values.completion_gates,
        state_record=state_record,
        budget_anchor=ContinuationFinalizationBudget(owner=owner),
        services=services,
        turn=bootstrap,
        labels=_SEND_LABELS,
        provider_binding=phases.work,
        settings=services.settings,
        plugin_snapshot=bindings.plugin_snapshot,
        profile_registry=services.operator_profile_registry,
        required_work=phases.work.coordinator,
    )


@pytest.mark.asyncio
async def test_actual_end_complete_flagged_terminal_preserves_full_finalizer_and_rejects_foreign_result(
    operation_store, tmp_path, monkeypatch
):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    original_ready = composition_fixture.ready_case

    async def bound_ready(*args, **kwargs):
        return await original_ready(*args, **{**kwargs, "bound_base": True})

    monkeypatch.setattr(composition_fixture, "ready_case", bound_ready)
    original_work = composition_fixture.inventory_work

    def timed_work(running):
        selected = authority.load_continuation_snapshot(running)
        root = decode_continuation_snapshot(selected.payload_json)
        admitted_policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        sampled = monotonic()
        with store[0].connect() as conn:
            observed = database_now(conn)
            deadline = conn.execute(
                select(segments.c.deadline_at).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                )
            ).scalar_one()
        assert deadline is not None
        timing = ContinuationSQLDeadlineOwner(
            running,
            policy=admitted_policy,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=continuation_test_settings().composer_async_drain_seconds,
            hard_expires_at=sampled + (restore_utc(deadline) - observed).total_seconds(),
        )
        return original_work(running, sql_deadline_owner=timing)

    monkeypatch.setattr(composition_fixture, "inventory_work", timed_work)
    original_state_data = initialization_fixture.CompositionStateData
    graph = simple_state.__wrapped__().to_dict()
    input_path = tmp_path / "input.csv"
    input_path.write_text("value\nready\n", encoding="utf-8")
    graph["sources"]["source"]["options"]["path"] = str(input_path)
    graph["nodes"] = []
    graph["outputs"] = [{"name": "rows", "plugin": "csv", "options": {"path": str(tmp_path / "output.csv")}, "on_write_failure": "discard"}]

    def seeded_state_data(*, metadata_, composer_meta):
        return original_state_data(
            sources=graph["sources"],
            nodes=graph["nodes"],
            edges=graph["edges"],
            outputs=graph["outputs"],
            is_valid=True,
            metadata_=metadata_,
            composer_meta=composer_meta,
        )

    monkeypatch.setattr(initialization_fixture, "CompositionStateData", seeded_state_data)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    runtime, work, _requests = await initialized_primary_input(
        store, tmp_path, monkeypatch, settings_overrides={"composer_advisor_checkpoint_max_passes": 1}
    )
    running = work.running
    assert running is not None
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    try:

        async def primary_completion(**_kwargs):
            return _reply("An actual PRIMARY candidate requires END review.")

        monkeypatch.setattr(litellm, "acompletion", primary_completion)
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
        assert type(primary) is PrimaryProviderResult
        phases.accept_primary(primary)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            current = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
        selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None
        phases.service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(_GuardClient(PromptGuardDecision("allow", "clean")))
        flagged = _checkpoint_reply("FLAGGED", findings="One requirement remains unmet.", publication_verdict="WITHHOLD")
        sdk_calls = []

        async def complete_checkpoint(**_kwargs):
            sdk_calls.append(1)
            return _reply(flagged)

        monkeypatch.setattr(litellm, "acompletion", complete_checkpoint)

        class CapturedTerminal(Exception):
            pass

        retained = []

        async def intercept_original_end_gate(**arguments):
            phase_owner = arguments["checkpoint_phase_owner"]
            gate_locals = arguments["checkpoint_end_input"]
            source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
            advisor = phases.service._advisor_checkpoint
            sessions = phases.service._require_sessions_service()
            bindings = phase_owner.runtime.bindings
            scope = composition_scope_for_operation(
                phase_owner.runtime.live.state,
                session_id=bindings.session_id,
                session_operation_context=bindings.session_operation_context,
                session_operation_authority=sessions.session_operation_authority,
            )
            request = advisor._build_checkpoint_arguments(
                phase="end",
                state=phase_owner.runtime.live.state,
                content_scope=scope,
                env_ref_names=phase_owner.runtime.env_ref_names,
                user_message=bindings.message,
                advisor_review_state=gate_locals.review_state,
                publication_review=gate_locals.publication_review,
            )
            guidance, _metadata = await advisor._call_advisor_with_audit(
                request,
                recorder=phase_owner.runtime.live.recorder,
                provider_owner=phases.provider_owner,
                structured_output=True,
                _outcome=source.capture,
                checkpoint_end_source=source,
            )
            assert guidance == flagged
            returned = observe_checkpoint_end_physical_return(source)
            custody = checkpoint_end_physical_custody(source)
            prepared = custody.continuation_checkpoint_v4
            admission = custody.continuation_checkpoint_v4_admission
            assert prepared is not None and admission is not None
            with locked_session_transaction(store[0], str(store[4])) as conn:
                owed = retain_v4_checkpoint_end_owed_on_connection(
                    conn, running=running, prepared=prepared, admission=admission, returned=returned
                )
            selected_owed = await load_selected_continuation(
                authority=phases.authority, running=running, selected=owed.owed_selected, required_work=work
            )
            computation = compute_checkpoint_end_gate(source=source, returned=returned)
            assert computation.action == "blocked_or_orphan_terminal"
            projected = project_end_complete_terminal(physical=source, returned=returned, computation=computation)
            inputs = await _original_finalization_inputs(phases, source, selected_owed)
            with locked_session_transaction(store[0], str(store[4])) as conn:
                outcome = await persist_end_complete_terminal_on_connection(
                    conn,
                    service=sessions,
                    physical=source,
                    prepared=prepared,
                    owed_admission=owed,
                    returned=returned,
                    projected=projected,
                    inputs=inputs,
                )
            retained.append((outcome, owed, projected, prepared))
            raise CapturedTerminal

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", intercept_original_end_gate)
        with pytest.raises(CapturedTerminal):
            await execute_continuation_end_review(phases, entered.source.selected)
        assert sdk_calls == [1] and len(retained) == 1
        outcome, owed, projected, prepared = retained[0]
        with locked_session_transaction(store[0], str(store[4])) as conn:
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == running.claim.operation_id)).one()
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == owed.original.attempt.attempt_id)).one()
            hold = conn.execute(select(holds).where(holds.c.effect_key == owed.original.effect_key)).one()
            ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
            assert parent.status == "completed" and parent.result_json is not None
            assert attempt.settled_at is not None and hold.state == "consumed" and ledger.entry_id == attempt.ledger_entry_id
            assert outcome.record.status == "completed" and outcome.batch.projected is projected
            assert outcome.record.result_json == parent.result_json
            from elspeth.web.sessions.continuation_checkpoint_end_complete_terminal_cold import (
                require_end_complete_terminal_postwrite_on_connection,
            )

            pass_sequence = conn.execute(
                select(messages.c.sequence_no).where(messages.c.id == outcome.batch.pass_audit.message_id)
            ).scalar_one()
            prefix_sequences = tuple(
                conn.execute(select(messages.c.sequence_no).where(messages.c.id == row.message_id)).scalar_one()
                for row in outcome.batch.prefix_rows
            )
            foreign_message = outcome.batch.response.message.model_copy(update={"id": str(uuid4())})
            foreign_response = outcome.batch.response.model_copy(update={"message": foreign_message})
            with pytest.raises(AuditIntegrityError):
                require_end_complete_terminal_postwrite_on_connection(
                    conn,
                    prepared=prepared,
                    batch=replace(outcome.batch, response=foreign_response),
                    record=outcome.record,
                    pass_sequence=pass_sequence,
                    prefix_sequences=prefix_sequences,
                )
    finally:
        await lease.close()
        work.coordinator.assert_completed()

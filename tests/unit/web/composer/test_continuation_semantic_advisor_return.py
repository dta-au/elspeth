"""Original iteration observations cannot be manufactured from equal DATA."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application
from elspeth.web.sessions.locking import locked_session_transaction
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


@pytest.mark.parametrize("kind", ["early", "return", "end_input"])
def test_unissued_iteration_observation_has_no_capture_authority(kind):
    from elspeth.web.composer.continuation_semantic_advisor_return import (
        ObservedIterationEarly,
        ObservedIterationEndInput,
        ObservedIterationReturn,
        require_original_iteration_early,
        require_original_iteration_end_input,
        require_original_iteration_return,
    )

    # Constructor access to a nominal DATA carrier does not issue original
    # live-call/capture custody. Refuse before consulting any supplied owner.
    if kind == "early":
        observed = ObservedIterationEarly()
        with pytest.raises(AuditIntegrityError, match=r"original.*observation"):
            require_original_iteration_early(observed, None, None, None)
    elif kind == "return":
        observed_return = ObservedIterationReturn()
        with pytest.raises(AuditIntegrityError, match=r"original.*observation"):
            require_original_iteration_return(observed_return, None, None, None)
    else:
        end_input = ObservedIterationEndInput()
        with pytest.raises(AuditIntegrityError, match=r"original.*observation"):
            require_original_iteration_end_input(end_input, None, None, pending_json="{}")


@pytest.mark.asyncio
async def test_original_classifier_preserves_real_continue_and_refuses_equal_wrapper(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_return import (
        observe_continuation_iteration_classification,
        require_original_iteration_classification,
    )
    from tests.unit.web.sessions.test_continuation_classifier_primary import _classifier_case

    # The established live harness issues actual provider-authored reads,
    # lands their P4 and begins a real timed classify stage. Its one bound
    # clock is shared with SQL/issuer; no classifier outcome is stubbed.
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, _running, runtime, _issuer, _prepared, sdk):
        selected = runtime.source.selected
        assert phases.finished_batch is not None
        dispatch, persist, _prestate = phases.finished_batch
        observed = await observe_continuation_iteration_classification(
            phases, runtime, selected=selected, dispatch=dispatch, persist=persist
        )
        actual = require_original_iteration_classification(observed, phases, runtime, selected=selected)
        assert actual is observed.outcome and actual.action == "continue" and actual.result is None
        assert len(sdk) == 1
        with pytest.raises(AuditIntegrityError, match="original issued classification observation"):
            require_original_iteration_classification(replace(observed), phases, runtime, selected=selected)
        with pytest.raises(AuditIntegrityError, match="original live source"):
            require_original_iteration_classification(observed, phases, runtime, selected=replace(selected))
    phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_registered_and_successor_sizing_use_one_original_unpaid_stage_bound(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_next_action import iteration_stage_action_policy
    from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, decode_continuation_policy
    from elspeth.web.sessions import continuation_iteration_return as stages
    from elspeth.web.sessions.continuation import _retained_successor_action
    from tests.unit.web.sessions.test_continuation_iteration_stages import _applied_case

    async with _applied_case(operation_store, tmp_path, monkeypatch) as (owner, running, selected, inventory, captured, sdk):
        ready = stages.retain_iteration_prepared(owner.authority, running, selected=selected, inventory=inventory, captured=captured)
        policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
        before = stored_rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            data = stages.read_iteration_stage_on_connection(conn, running, selected=ready)
            assert data is not None and data.stage == "classify_ready"
            registered = iteration_stage_action_policy(selected=ready, stage=data, policy=policy)
            queued = stages.read_iteration_allowance_data_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=ready
            )
            encoded, retained_hash = _retained_successor_action(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                snapshot=ready,
                next_action_json=None,
                policy_sha256=None,
            )
            assert registered == queued == policy.actions.primary
            assert ContinuationActionPolicy.model_validate_json(encoded, strict=True) == registered
            assert retained_hash == selected.stack.policy_hash
            with pytest.raises(AuditIntegrityError, match="successor allowance differs"):
                _retained_successor_action(
                    conn,
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    snapshot=ready,
                    next_action_json=policy.actions.session_sql.model_dump_json(),
                    policy_sha256=selected.stack.policy_hash,
                )
            old = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
            with pytest.raises(AuditIntegrityError, match="exact original stage source"):
                iteration_stage_action_policy(selected=ready, stage=replace(data, current=old), policy=policy)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_original_applied_p4_data_cannot_invent_a_classified_return_before_sdk(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.continuation_semantic_advisor import read_semantic_advisor_retained_inputs_on_connection

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (phases, running, primary, sdk):
        for _ in range(len(primary.outcome.completion.tool_batch.calls) + 1):
            await _advance(phases)
        assert phases.p4 is not None and type(phases.p4.application) is StoredP4Application
        selected = await load_selected_continuation(
            authority=phases.authority,
            running=running,
            selected=phases.p4.application.snapshot,
            required_work=phases.work,
        )
        await phases.current(selected)
        current = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
        before = stored_rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn,
            pytest.raises(AuditIntegrityError, match="iteration return lost its committed original effect/result"),
        ):
            read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=current)
        assert stored_rows(operation_store[0]) == before
        assert len(sdk) == 1
    phases.work.coordinator.assert_completed()

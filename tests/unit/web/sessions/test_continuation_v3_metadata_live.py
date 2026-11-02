"""A real metadata tail from original historical V3 PRIMARY custody.

Fresh V3 provider admission remains refused. These controls start with the
historical paid source and exercise the actual phase, SQL and cold owners.
"""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_live_phases as phases
from elspeth.web.composer import continuation_metadata_admission as admission
from elspeth.web.composer import continuation_metadata_preparation as preparation
from elspeth.web.composer import tool_batch
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_limits import ContinuationDocumentTooLarge
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.continuation import _v3_unpaid_terminal_reserve
from elspeth.web.sessions.continuation_driver import ContinuationSegmentHandedOver
from elspeth.web.sessions.continuation_next_iteration import is_applied_batch_cut
from elspeth.web.sessions.continuation_prepared_tail import prepared_append_chunks
from elspeth.web.sessions.continuation_tool_no_execution import ToolNoExecutionRefusal
from elspeth.web.sessions.models import approvals_table as approvals
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import interpretation_events_table as interpretations
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.service import MetadataPublicationUnavailable, SessionServiceImpl
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_v3_tool_historical_refusal import _rows
from tests.unit.web.sessions.test_continuation_v3_tool_refusal_driver import _historical_driver
from tests.unit.web.sessions.test_interpretation_events_table import _user_approved_row

operation_store = test_composer_operation_authority.operation_store


@asynccontextmanager
async def _metadata_case(operation_store, tmp_path, monkeypatch, *, tool_name="set_metadata"):
    current = ContinuationLivePhases.current
    async with _historical_driver(operation_store, tmp_path, monkeypatch, tool_name=tool_name) as (driver, values):
        # The refusal fixture pins current() to one selected head. This lane
        # deliberately walks beyond it using the ordinary source reader.
        monkeypatch.setattr(ContinuationLivePhases, "current", current)
        selected = values[3]
        policy = driver.sql_deadline.policy
        action = next_continuation_action_policy(selected=selected, policy=policy)
        clock = ContinuationActionClock(action)
        driver.sql_deadline.begin_action(action, started_at=clock.started_monotonic)
        driver.phases.bind_action_clock(clock)
        yield driver, values


def _state_rows(store):
    with store[0].connect() as conn:
        return tuple(conn.execute(select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version)).all())


def _forbid_external_dispatch(monkeypatch):
    entries = []

    async def forbidden(*args, **kwargs):
        entries.append((args, kwargs))
        raise AssertionError("pure metadata tail entered generic tool/provider dispatch")

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", forbidden)
    monkeypatch.setattr(phases, "_execute_continuation_call_live", forbidden)
    monkeypatch.setattr(litellm, "acompletion", forbidden)
    return entries


async def _execute(driver, selected):
    return await driver.service.advance_continuation_phase(
        owner=driver.phases,
        authority=driver.authority,
        selected=selected,
        work=driver.work,
        provider_owner=driver.phases.provider_owner,
    )


@pytest.mark.asyncio
async def test_historical_v3_metadata_publishes_all_prefixes_and_cold_restore_never_reexecutes(operation_store, tmp_path, monkeypatch):
    async with _metadata_case(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, _policy, running, selected, _inventory, *_ = values
        sid, operation = str(store[4]), running.claim.operation_id
        before, states_before = _rows(store[0], sid, operation), _state_rows(store)
        expected_published_version = 1 if not states_before else states_before[-1].version + 1
        original_tool_version = values[5].state.version
        external = _forbid_external_dispatch(monkeypatch)
        computations = []
        actual = preparation.prepare_set_metadata_result_value

        def compute(*args, **kwargs):
            result = actual(*args, **kwargs)
            computations.append(result)
            return result

        monkeypatch.setattr(preparation, "prepare_set_metadata_result_value", compute)
        assert await _execute(driver, selected) is None
        stored = driver.phases.latest_snapshot
        assert stored is not None
        assert driver.phases.runtime is None and driver.phases.primary is None
        driver.sql_deadline.begin_settlement()
        current_stored, current = await driver.selected()
        assert current_stored == stored and is_applied_batch_cut(current)
        assert not current.stack.pending_owner_results
        assert len(computations) == 1 and computations[0].success and not external
        after = _rows(store[0], sid, operation)
        for table in (physical, usage):
            assert after[table.name] == before[table.name]
        new_events = [row for row in after[events.name] if row not in before[events.name]]
        assert [row.tag for row in sorted(new_events, key=lambda row: row.sequence)] == [
            "may_dispatch",
            "checkpoint",
            "tool_prepared",
            "effect_committed",
            "checkpoint",
        ]
        assert current.stack.cursor == selected.stack.cursor + len(new_events)
        new_effects = [row for row in after[effects.name] if row not in before[effects.name]]
        assert len(new_effects) == 2 and all(row.disposition == "committed" for row in new_effects)
        states_after = _state_rows(store)
        assert len(states_after) == len(states_before) + 1
        assert states_after[-1].metadata_["data"]["name"] == "after"
        assert states_after[-1].version == expected_published_version
        assert computations[0].updated_state.version == original_tool_version + 1

        async def no_reexecution(*args, **kwargs):
            raise AssertionError("cold applied P4 repeated metadata preparation")

        monkeypatch.setattr(admission, "_prepare_detached", no_reexecution)
        cold = ContinuationLivePhases(
            driver.service,
            driver.authority,
            driver.work,
            ProviderInvocationOwner(service=store[3], required_work=driver.work),
            driver.lease,
        )
        restored, _ = await cold.current(current)
        assert restored.live.state.metadata.name == "after"
        assert restored.live.state.version == computations[0].updated_state.version
        assert restored.graph.batch is not None
        assert restored.live.state is restored.graph.batch.batch.state
        retained_result = restored.graph.batch.batch.tool_outcomes[0].response
        assert type(retained_result) is ToolResult
        assert restored.live.state.to_dict() == retained_result.updated_state.to_dict()
        assert restored.live.turn_evidence.effects.latest_verified_state.version == states_after[-1].version
        assert restored.live.turn_evidence.effects.latest_verified_state_id == str(states_after[-1].id)
        await cold.restore_finished_batch(current)
        assert cold.finished_batch is not None
        assert cold.finished_batch[0].tool_outcomes[0].call.id == values[7].tool_batch.calls[0].id
        assert _rows(store[0], sid, operation) == after and _state_rows(store) == states_after
        assert len(computations) == 1 and not external


@pytest.mark.asyncio
@pytest.mark.parametrize("short", (False, True), ids=("exact-fit", "one-byte-short"))
async def test_metadata_full_tail_capacity_counts_off_head_history_before_any_issue(operation_store, tmp_path, monkeypatch, short):
    async with _metadata_case(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, policy, running, selected, _inventory, *_ = values
        sid, operation = str(store[4]), running.claim.operation_id
        before, states_before = _rows(store[0], sid, operation), _state_rows(store)
        external = _forbid_external_dispatch(monkeypatch)
        actual_scope = admission.admit_prepared_complete_tail_on_connection
        actual_consume = admission.consume_metadata_transitions_on_connection
        entered, measured = [], []

        def consume(*args, **kwargs):
            entered.append(kwargs["plan"])
            return actual_consume(*args, **kwargs)

        @contextmanager
        def at_capacity(conn, *, original, prefixes):
            assert len(prefixes) == 5
            existing = {
                row.chunk_key: bytes(row.payload)
                for row in conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation))
            }
            final = prefixes[-1].append
            reserve = _v3_unpaid_terminal_reserve(
                conn,
                session_id=sid,
                operation_id=operation,
                post_root_json=final.snapshot_json,
                post_cursor=final.expected_cursor + len(final.events),
            )
            complete = dict(existing)
            for prefix in prefixes:
                for candidate in (*prefix.chunks, *prepared_append_chunks(prefix.append)):
                    if candidate.key in complete:
                        assert complete[candidate.key] == candidate.payload
                    complete[candidate.key] = candidate.payload
            for candidate in reserve:
                if candidate.key in complete:
                    assert complete[candidate.key] == candidate.payload
                complete[candidate.key] = candidate.payload
            demand = sum(len(payload) for payload in complete.values())
            ballast = policy.max_retained_payload_bytes - demand + int(short)
            assert ballast > 0
            # Explicit historical off-head payload, below the unchanged private
            # document limit per row. No admitted policy or trigger is changed.
            for index, offset in enumerate(range(0, ballast, 65_536)):
                payload = b"b" * min(65_536, ballast - offset)
                conn.execute(
                    insert(chunks).values(
                        session_id=sid,
                        operation_id=operation,
                        chunk_key=f"capacity-control:{index}",
                        codec="composer.capacity-control.v1",
                        payload=payload,
                        sha256=sha256(payload).hexdigest(),
                        byte_length=len(payload),
                    )
                )
            measured.append(demand + ballast)
            with actual_scope(conn, original=original, prefixes=prefixes):
                yield

        monkeypatch.setattr(admission, "admit_prepared_complete_tail_on_connection", at_capacity)
        monkeypatch.setattr(admission, "consume_metadata_transitions_on_connection", consume)
        result = await _execute(driver, selected)
        assert measured == [policy.max_retained_payload_bytes + int(short)]
        assert not external
        after = _rows(store[0], sid, operation)
        assert after[physical.name] == before[physical.name] and after[usage.name] == before[usage.name]
        if short:
            assert type(result) is ToolNoExecutionRefusal and not entered
            assert _state_rows(store) == states_before
            assert not any(row.chunk_key.startswith("capacity-control:") for row in after[chunks.name])
        else:
            assert result is None and len(entered) == 1
            assert _state_rows(store)[-1].metadata_["data"]["name"] == "after"
        assert not driver.work.coordinator.settlement_failure_receipts()


@pytest.mark.asyncio
async def test_unsupported_original_v3_read_tool_refuses_without_metadata_preparation(operation_store, tmp_path, monkeypatch):
    async with _metadata_case(operation_store, tmp_path, monkeypatch, tool_name="get_pipeline_state") as (driver, values):
        store, _policy, running, selected, _inventory, *_ = values
        sid, operation = str(store[4]), running.claim.operation_id
        before, states_before = _rows(store[0], sid, operation), _state_rows(store)
        external = _forbid_external_dispatch(monkeypatch)

        async def no_metadata_completion(*args, **kwargs):
            raise AssertionError("unsupported original tool entered metadata completion")

        monkeypatch.setattr(admission, "complete_prepared_metadata_tool", no_metadata_completion)
        result = await _execute(driver, selected)
        assert type(result) is ToolNoExecutionRefusal and not external
        after = _rows(store[0], sid, operation)
        assert after[physical.name] == before[physical.name] and after[usage.name] == before[usage.name]
        assert _state_rows(store) == states_before
        assert not driver.work.coordinator.settlement_failure_receipts()


@pytest.mark.asyncio
@pytest.mark.parametrize("review", ("none", "pending", "approval"))
async def test_metadata_locked_admission_preserves_existing_review_rows(operation_store, tmp_path, monkeypatch, review):
    actual_bootstrap = initialization_cases.bootstrap_case

    async def bound_bootstrap(store, **kwargs):
        # Use the fixture's real prior-state writer and request binding, rather
        # than planting a state after the original operation was admitted.
        return await actual_bootstrap(store, **{**kwargs, "bound_base": True})

    monkeypatch.setattr(initialization_cases, "bootstrap_case", bound_bootstrap)
    async with _metadata_case(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, _policy, running, selected, _inventory, *_ = values
        sid, operation = str(store[4]), running.claim.operation_id
        state_id = selected.stack.logical.base_state_id
        assert state_id is not None
        # These are pre-existing history/control rows. The original PRIMARY,
        # runtime graph and immutable admission tuple remain untouched.
        with store[0].begin() as conn:
            if review == "pending":
                conn.execute(
                    insert(interpretations).values(
                        _user_approved_row(
                            row_id=str(uuid4()),
                            session_id=sid,
                            state_id=state_id,
                        )
                    )
                )
            elif review == "approval":
                ensure_test_identity(conn, identity_id="metadata-approver")
                conn.execute(
                    insert(approvals).values(
                        approval_id=str(uuid4()),
                        session_id=sid,
                        state_id=state_id,
                        binding_json={},
                        requested_by_identity_id="alice",
                        approver_identity_id="metadata-approver",
                        requested_at=datetime.now(UTC),
                    )
                )

        def reviews():
            with store[0].connect() as conn:
                return tuple(
                    tuple(conn.execute(select(table).where(table.c.session_id == sid).order_by(*table.primary_key.columns)).all())
                    for table in (interpretations, approvals)
                )

        before, states_before, reviews_before = _rows(store[0], sid, operation), _state_rows(store), reviews()
        actual = SessionServiceImpl.prepare_compose_turn_publication_on_connection
        refusals = []

        def prepare(self, *args, **kwargs):
            try:
                return actual(self, *args, **kwargs)
            except MetadataPublicationUnavailable as error:
                assert "cannot retire pending reviews or approvals" in str(error)
                refusals.append(error)
                raise

        monkeypatch.setattr(SessionServiceImpl, "prepare_compose_turn_publication_on_connection", prepare)
        external = _forbid_external_dispatch(monkeypatch)
        result = await _execute(driver, selected)
        after = _rows(store[0], sid, operation)
        assert reviews() == reviews_before and not external
        assert after[physical.name] == before[physical.name] and after[usage.name] == before[usage.name]
        if review == "none":
            assert result is None and not refusals
            assert len(_state_rows(store)) == len(states_before) + 1
        else:
            assert type(result) is ToolNoExecutionRefusal and len(refusals) == 1
            assert _state_rows(store) == states_before
            assert all(row.disposition == "refused" for row in after[effects.name] if row not in before[effects.name])
        assert not driver.work.coordinator.settlement_failure_receipts()


@pytest.mark.asyncio
async def test_actual_driver_can_handover_after_metadata_commit_without_new_provider(operation_store, tmp_path, monkeypatch):
    current = ContinuationLivePhases.current
    async with _historical_driver(operation_store, tmp_path, monkeypatch, tool_name="set_metadata") as (driver, values):
        monkeypatch.setattr(ContinuationLivePhases, "current", current)
        store, _policy, running, _selected, _inventory, *_ = values
        before = _rows(store[0], str(store[4]), running.claim.operation_id)
        external = _forbid_external_dispatch(monkeypatch)
        actual, committed = ComposerServiceImpl.advance_continuation_phase, []

        async def advance(self, **kwargs):
            result = await actual(self, **kwargs)
            snapshot = kwargs["owner"].latest_snapshot
            assert result is None and snapshot is not None
            committed.append(snapshot)
            driver.worker.begin_drain()
            return result

        monkeypatch.setattr(ComposerServiceImpl, "advance_continuation_phase", advance)
        outcome = await driver.run(None)
        assert type(outcome) is ContinuationSegmentHandedOver
        assert len(committed) == 1 and not external
        after = _rows(store[0], str(store[4]), running.claim.operation_id)
        assert after[physical.name] == before[physical.name] and after[usage.name] == before[usage.name]
        assert _state_rows(store)[-1].metadata_["data"]["name"] == "after"


@pytest.mark.asyncio
async def test_metadata_publication_failure_rolls_back_original_tool_and_all_prefixes(operation_store, tmp_path, monkeypatch):
    async with _metadata_case(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, _policy, running, selected, _inventory, *_ = values
        sid, operation = str(store[4]), running.claim.operation_id
        before, states_before = _rows(store[0], sid, operation), _state_rows(store)
        external = _forbid_external_dispatch(monkeypatch)
        published = []
        actual = SessionServiceImpl.consume_compose_turn_publication_on_connection

        def fail_after_publication(self, *args, **kwargs):
            published.append(actual(self, *args, **kwargs))
            raise AuditIntegrityError("injected metadata publication rollback")

        monkeypatch.setattr(SessionServiceImpl, "consume_compose_turn_publication_on_connection", fail_after_publication)
        runtime, inventory = await driver.phases.current(selected)
        with pytest.raises(AuditIntegrityError, match="injected metadata publication rollback"):
            await admission.try_admit_metadata_tool(driver.phases, runtime=runtime, selected=selected, inventory=inventory)
        assert len(published) == 1 and not external
        assert _rows(store[0], sid, operation) == before and _state_rows(store) == states_before
        assert driver.phases.latest_snapshot is None


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("detached_document", "sql_evidence"))
async def test_metadata_preparation_bound_refusal_does_not_leave_a_required_sql_failure(operation_store, tmp_path, monkeypatch, site):
    async with _metadata_case(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, _policy, running, selected, _inventory, *_ = values
        sid, operation = str(store[4]), running.claim.operation_id
        before, states_before = _rows(store[0], sid, operation), _state_rows(store)
        external = _forbid_external_dispatch(monkeypatch)
        bounds = []

        async def detached_bound(*args, **kwargs):
            bounds.append(site)
            raise ContinuationDocumentTooLarge("private chunk exceeds the complete JSON admission bound")

        def evidence_bound(*args, **kwargs):
            bounds.append(site)
            raise ComposerContextUnsupported("retained_chunk_bytes")

        def no_issue(*args, **kwargs):
            raise AssertionError("metadata bound refusal reached the original tool issue writer")

        if site == "detached_document":
            monkeypatch.setattr(admission, "_prepare_detached", detached_bound)
        else:
            monkeypatch.setattr(admission, "prepare_p4_application_tail", evidence_bound)
        monkeypatch.setattr(admission, "consume_metadata_transitions_on_connection", no_issue)
        result = await _execute(driver, selected)
        assert type(result) is ToolNoExecutionRefusal and bounds == [site] and not external
        after = _rows(store[0], sid, operation)
        assert after[physical.name] == before[physical.name] and after[usage.name] == before[usage.name]
        assert _state_rows(store) == states_before
        assert not driver.work.coordinator.settlement_failure_receipts()


@pytest.mark.asyncio
async def test_metadata_records_committed_snapshot_before_cancelled_delivery(operation_store, tmp_path, monkeypatch):
    async with _metadata_case(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, _policy, _running, selected, _inventory, *_ = values
        external = _forbid_external_dispatch(monkeypatch)
        runtime, inventory = await driver.phases.current(selected)
        actual = admission._registered
        delivered = []

        async def cancel_delivery(**kwargs):
            result = await actual(**kwargs)
            assert driver.phases.latest_snapshot is not None
            assert driver.phases.runtime is None and driver.phases.primary is None
            delivered.append(result)
            raise asyncio.CancelledError("after metadata SQL commit before bridge delivery")

        monkeypatch.setattr(admission, "_registered", cancel_delivery)
        with pytest.raises(asyncio.CancelledError, match="after metadata SQL commit"):
            await admission.try_admit_metadata_tool(driver.phases, runtime=runtime, selected=selected, inventory=inventory)
        assert len(delivered) == 1 and not external
        stored, current = await driver.selected()
        assert stored == driver.phases.latest_snapshot and is_applied_batch_cut(current)
        assert _state_rows(store)[-1].metadata_["data"]["name"] == "after"


@pytest.mark.asyncio
async def test_normal_v2_metadata_uses_existing_tool_dispatch(operation_store, tmp_path, monkeypatch):
    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("normal-metadata", "set_metadata", {"patch": {"name": "normal-v2"}}),
    ) as (owner, _running, _primary, sdk):

        async def no_v3_bridge(*args, **kwargs):
            raise AssertionError("normal V2 entered V3 metadata admission")

        monkeypatch.setattr(admission, "try_admit_metadata_tool", no_v3_bridge)
        await _advance(owner)
        assert owner.runtime is not None and owner.runtime.live.state.metadata.name == "normal-v2"
        assert len(sdk) == 1

"""Current paid PRIMARY metadata: atomic publication, bounds and cancellation."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_metadata_admission as admission
from elspeth.web.composer import continuation_metadata_preparation as preparation
from elspeth.web.composer.continuation_limits import ContinuationDocumentTooLarge
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.continuation import continuation_unpaid_terminal_reserve_on_connection
from elspeth.web.sessions.continuation_driver import ContinuationSegmentHandedOver
from elspeth.web.sessions.continuation_next_iteration import is_applied_batch_cut
from elspeth.web.sessions.continuation_prepared_tail import prepared_append_chunks
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
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_tool_failure import _claimed_driver, _install_original_deadline, _unstarted_driver
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _forbid_execution, _v4_tool_case
from tests.unit.web.sessions.test_continuation_v4_metadata_live import operation_store as _operation_store
from tests.unit.web.sessions.test_interpretation_events_table import _user_approved_row

operation_store = _operation_store


def _state_rows(store):
    with store[0].connect() as conn:
        return tuple(conn.execute(select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version)).all())


@pytest.mark.asyncio
async def test_complete_prefixes_and_cold_graph_preserve_original_tool_result(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        selected = owner.runtime.source.selected
        runtime, inventory = await owner.current(selected)
        assert runtime.graph.batch is not None
        original_version = runtime.graph.batch.batch.state.version
        original_call = runtime.graph.batch.calls[0]
        before = dict(recovery_rows(store[0]))
        original = preparation.prepare_set_metadata_result_value
        computed = []

        def compute(*args, **kwargs):
            result = original(*args, **kwargs)
            computed.append(result)
            return result

        monkeypatch.setattr(preparation, "prepare_set_metadata_result_value", compute)
        external = _forbid_execution(monkeypatch)
        assert await admission.try_admit_metadata_tool(owner, runtime=runtime, selected=selected, inventory=inventory) is True
        assert len(computed) == 1 and computed[0].success and computed[0].updated_state.version == original_version + 1
        after = dict(recovery_rows(store[0]))
        new_events = [row for row in after[events.name] if row not in before[events.name]]
        assert [row.tag for row in sorted(new_events, key=lambda row: row.sequence)] == [
            "may_dispatch",
            "checkpoint",
            "tool_prepared",
            "effect_committed",
            "checkpoint",
        ]
        new_effects = [row for row in after[effects.name] if row not in before[effects.name]]
        assert len(new_effects) == 2 and all(row.disposition == "committed" for row in new_effects)
        for table in (physical, usage):
            assert after[table.name] == before[table.name]
        current = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        assert is_applied_batch_cut(current) and not current.stack.pending_owner_results
        assert current.stack.cursor == selected.stack.cursor + len(new_events)

        async def no_reexecution(*args, **kwargs):
            raise AssertionError("cold metadata graph repeated computation")

        monkeypatch.setattr(admission, "_prepare_detached", no_reexecution)
        cold = ContinuationLivePhases(
            owner.service, owner.authority, owner.work, ProviderInvocationOwner(service=store[3], required_work=owner.work), owner.lease
        )
        restored, _ = await cold.current(current)
        assert restored.graph.batch is not None and restored.live.state is restored.graph.batch.batch.state
        response = restored.graph.batch.batch.tool_outcomes[0].response
        assert type(response) is ToolResult and restored.live.state.to_dict() == response.updated_state.to_dict()
        published = _state_rows(store)[-1]
        assert restored.live.turn_evidence.effects.latest_verified_state.version == published.version
        assert restored.live.turn_evidence.effects.latest_verified_state_id == str(published.id)
        await cold.restore_finished_batch(current)
        assert cold.finished_batch is not None and cold.finished_batch[0].tool_outcomes[0].call.id == original_call.id
        assert dict(recovery_rows(store[0])) == after and len(computed) == len(sdk) == 1 and not external


@pytest.mark.asyncio
async def test_publication_failure_rolls_back_tool_and_all_prefixes(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, _running, sdk):
        selected = owner.runtime.source.selected
        runtime, inventory = await owner.current(selected)
        before = recovery_rows(store[0])
        before_snapshot = owner.latest_snapshot
        external = _forbid_execution(monkeypatch)
        original = SessionServiceImpl.consume_compose_turn_publication_on_connection
        published = []

        def fail(self, *args, **kwargs):
            published.append(original(self, *args, **kwargs))
            raise AuditIntegrityError("injected metadata publication rollback")

        monkeypatch.setattr(SessionServiceImpl, "consume_compose_turn_publication_on_connection", fail)
        with pytest.raises(AuditIntegrityError, match="injected metadata publication rollback"):
            await admission.try_admit_metadata_tool(owner, runtime=runtime, selected=selected, inventory=inventory)
        assert len(published) == 1 and not external and len(sdk) == 1
        assert recovery_rows(store[0]) == before and owner.latest_snapshot is before_snapshot


@pytest.mark.asyncio
async def test_committed_snapshot_precedes_cancelled_delivery(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        selected = owner.runtime.source.selected
        runtime, inventory = await owner.current(selected)
        external = _forbid_execution(monkeypatch)
        original = admission._registered
        delivered = []

        async def cancel(**kwargs):
            result = await original(**kwargs)
            assert owner.latest_snapshot is not None and owner.runtime is owner.primary is None
            delivered.append(result)
            raise asyncio.CancelledError("after metadata SQL commit before bridge delivery")

        monkeypatch.setattr(admission, "_registered", cancel)
        with pytest.raises(asyncio.CancelledError, match="after metadata SQL commit"):
            await admission.try_admit_metadata_tool(owner, runtime=runtime, selected=selected, inventory=inventory)
        assert len(delivered) == 1 and not external and len(sdk) == 1
        current = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        assert is_applied_batch_cut(current)
        assert _state_rows(store)[-1].metadata_["data"]["name"] == "V4 original model metadata"


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("detached_document", "sql_evidence"))
async def test_bound_refusal_preserves_sql_and_required_work(operation_store, tmp_path, monkeypatch, site):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, _running, sdk):
        selected = owner.runtime.source.selected
        runtime, inventory = await owner.current(selected)
        before = recovery_rows(store[0])
        external = _forbid_execution(monkeypatch)
        reached = []

        async def detached_bound(*args, **kwargs):
            reached.append(site)
            raise ContinuationDocumentTooLarge("private chunk exceeds the complete JSON admission bound")

        def evidence_bound(*args, **kwargs):
            reached.append(site)
            raise ComposerContextUnsupported("retained_chunk_bytes")

        def no_issue(*args, **kwargs):
            raise AssertionError("metadata bound refusal reached issue")

        if site == "detached_document":
            monkeypatch.setattr(admission, "_prepare_detached", detached_bound)
        else:
            monkeypatch.setattr(admission, "prepare_p4_application_tail", evidence_bound)
        monkeypatch.setattr(admission, "consume_metadata_transitions_on_connection", no_issue)
        assert await admission.try_admit_metadata_tool(owner, runtime=runtime, selected=selected, inventory=inventory) is False
        assert reached == [site] and not external and len(sdk) == 1
        assert recovery_rows(store[0]) == before and not owner.work.coordinator.settlement_failure_receipts()


@pytest.mark.asyncio
@pytest.mark.parametrize("short", (False, True), ids=("exact-fit", "one-byte-short"))
async def test_full_tail_capacity_counts_off_head_history_before_issue(operation_store, tmp_path, monkeypatch, short):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        selected = owner.runtime.source.selected
        runtime, inventory = await owner.current(selected)
        policy = runtime.source.policy
        sid, operation = str(store[4]), running.claim.operation_id
        before = dict(recovery_rows(store[0]))
        external = _forbid_execution(monkeypatch)
        original_scope, original_consume = (
            admission.admit_prepared_complete_tail_on_connection,
            admission.consume_metadata_transitions_on_connection,
        )
        measured, entered = [], []

        def consume(*args, **kwargs):
            entered.append(kwargs["plan"])
            return original_consume(*args, **kwargs)

        @contextmanager
        def at_capacity(conn, *, original, prefixes):
            assert len(prefixes) == 5
            complete = {
                row.chunk_key: bytes(row.payload)
                for row in conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation))
            }
            final = prefixes[-1].append
            reserve = continuation_unpaid_terminal_reserve_on_connection(
                conn,
                session_id=sid,
                operation_id=operation,
                post_root_json=final.snapshot_json,
                post_cursor=final.expected_cursor + len(final.events),
            )
            for prefix in prefixes:
                for candidate in (*prefix.chunks, *prepared_append_chunks(prefix.append)):
                    if candidate.key in complete:
                        assert complete[candidate.key] == candidate.payload
                    complete[candidate.key] = candidate.payload
            for candidate in reserve:
                if candidate.key in complete:
                    assert complete[candidate.key] == candidate.payload
                complete[candidate.key] = candidate.payload
            demand = sum(len(value) for value in complete.values())
            ballast = policy.max_retained_payload_bytes - demand + int(short)
            assert ballast > 0
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
            with original_scope(conn, original=original, prefixes=prefixes):
                yield

        monkeypatch.setattr(admission, "admit_prepared_complete_tail_on_connection", at_capacity)
        monkeypatch.setattr(admission, "consume_metadata_transitions_on_connection", consume)
        result = await admission.try_admit_metadata_tool(owner, runtime=runtime, selected=selected, inventory=inventory)
        assert measured == [policy.max_retained_payload_bytes + int(short)]
        assert result is (not short) and len(entered) == int(not short)
        after = dict(recovery_rows(store[0]))
        for table in (physical, usage):
            assert after[table.name] == before[table.name]
        if short:
            assert after == before
        else:
            assert _state_rows(store)[-1].metadata_["data"]["name"] == "V4 original model metadata"
        assert not external and len(sdk) == 1 and not owner.work.coordinator.settlement_failure_receipts()


@pytest.mark.asyncio
@pytest.mark.parametrize("review", ("none", "pending", "approval"))
async def test_locked_admission_preserves_existing_review_rows(operation_store, tmp_path, monkeypatch, review):
    original_bootstrap = initialization_cases.bootstrap_case

    async def bound_bootstrap(store, **kwargs):
        return await original_bootstrap(store, **{**kwargs, "bound_base": True})

    monkeypatch.setattr(initialization_cases, "bootstrap_case", bound_bootstrap)
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, _running, sdk):
        selected = owner.runtime.source.selected
        runtime, inventory = await owner.current(selected)
        state_id = selected.stack.logical.base_state_id
        assert state_id is not None
        sid = str(store[4])
        with store[0].begin() as conn:
            if review == "pending":
                conn.execute(insert(interpretations).values(_user_approved_row(row_id=str(uuid4()), session_id=sid, state_id=state_id)))
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
        before = dict(recovery_rows(store[0]))
        original = SessionServiceImpl.prepare_compose_turn_publication_on_connection
        refused = []

        def prepare(self, *args, **kwargs):
            try:
                return original(self, *args, **kwargs)
            except MetadataPublicationUnavailable as error:
                assert "cannot retire pending reviews or approvals" in str(error)
                refused.append(error)
                raise

        monkeypatch.setattr(SessionServiceImpl, "prepare_compose_turn_publication_on_connection", prepare)
        external = _forbid_execution(monkeypatch)
        result = await admission.try_admit_metadata_tool(owner, runtime=runtime, selected=selected, inventory=inventory)
        after = dict(recovery_rows(store[0]))
        for table in (interpretations, approvals, physical, usage):
            assert after[table.name] == before[table.name]
        assert result is (review == "none") and len(refused) == int(review != "none")
        if review != "none":
            assert after == before
        else:
            assert len(after[states.name]) == len(before[states.name]) + 1
        assert not external and len(sdk) == 1 and not owner.work.coordinator.settlement_failure_receipts()


@pytest.mark.asyncio
async def test_actual_driver_handover_after_metadata_commit_without_new_provider(operation_store, tmp_path, monkeypatch):
    _install_original_deadline(operation_store, monkeypatch)
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        driver = _unstarted_driver(store, owner, running)
        key = (running.claim.session_id, running.claim.operation_id)
        before = dict(recovery_rows(store[0]))
        external = _forbid_execution(monkeypatch)
        original, committed = ComposerServiceImpl.advance_continuation_phase, []

        async def advance(self, **kwargs):
            assert type(driver.worker._local_cancels[key]) is asyncio.Event
            result = await original(self, **kwargs)
            assert result is None and kwargs["owner"].latest_snapshot is not None
            committed.append(kwargs["owner"].latest_snapshot)
            driver.worker.begin_drain()
            return result

        monkeypatch.setattr(ComposerServiceImpl, "advance_continuation_phase", advance)
        async with _claimed_driver(driver):
            outcome = await driver.run(None)
            assert type(outcome) is ContinuationSegmentHandedOver
            assert len(committed) == 1 and not external and len(sdk) == 1
            after = dict(recovery_rows(store[0]))
            for table in (physical, usage):
                assert after[table.name] == before[table.name]
            assert _state_rows(store)[-1].metadata_["data"]["name"] == "V4 original model metadata"
        assert key not in driver.worker._local_cancels
        assert driver.progress_owner is not None and driver.progress_owner.unregistered
        assert not driver.progress_owner.contained_pending and driver.progress_owner.cleanup_failure is None

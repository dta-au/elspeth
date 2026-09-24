"""Generic Composer checkpoints preserve a carried guided proposal's anchor."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions._persist_payload import RedactedToolRow, StatePayload
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import guided_operations_table, session_operation_fences_table
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    CompositionStateRecord,
    SessionCompositionStateCreation,
    StaleComposeStateError,
)
from tests.integration.web.composer.guided.test_respond import _confirm_wiring, _review_wiring
from tests.integration.web.composer.guided.test_settlement_proposal_base_rebind import (
    _carried_proposal_anchor,
    _head_record,
    _seed_a_live_confirmation_admission,
)
from tests.integration.web.composer.guided.test_wrong_stage_intent import _stage_schema8_topology_intent_proposal
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_composer_proposal_authority import _create_ordinary_accept_proposal


def _checkpoint_data(record: CompositionStateRecord) -> CompositionStateData:
    return CompositionStateData(
        sources=record.sources,
        nodes=record.nodes,
        edges=record.edges,
        outputs=record.outputs,
        metadata_=record.metadata_,
        is_valid=record.is_valid,
        validation_errors=record.validation_errors,
        composer_meta=record.composer_meta,
    )


def _save_checkpoint(client: SyncASGITestClient, session_id: str, data: CompositionStateData, *, writer: str = "save"):
    service = client.app.state.session_service
    expected_state_id = _head_record(client, session_id).id

    async def save():
        ordinary_proposal = (
            await _create_ordinary_accept_proposal(service, session_id=UUID(session_id), base_state_id=expected_state_id)
            if writer == "proposal"
            else None
        )
        lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=UUID(session_id),
            operation_kind=SessionOperationKind.PROPOSAL if ordinary_proposal is not None else SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        try:
            if ordinary_proposal is not None:
                committed = await service.accept_composition_proposal(
                    session_id=UUID(session_id),
                    proposal_id=ordinary_proposal.id,
                    expected_current_state_id=expected_state_id,
                    state=data,
                    actor="user:alice",
                    session_operation_context=lease.context,
                )
                assert committed.status == "committed"
                return await service.get_current_state(UUID(session_id))
            if writer == "turn":
                await service.persist_compose_turn_async(
                    session_id=session_id,
                    assistant_content="Retain the reviewed graph.",
                    redacted_assistant_tool_calls=({"id": "unchanged-tool", "function": {"name": "set_pipeline"}},),
                    redacted_tool_rows=(
                        RedactedToolRow(
                            tool_call_id="unchanged-tool",
                            content='{"ok": true}',
                            composition_state_payload=StatePayload(data=data, derived_from_state_id=None),
                        ),
                    ),
                    parent_composition_state_id=str(expected_state_id),
                    expected_current_state_id=str(expected_state_id),
                    writer_principal="compose_loop",
                    plugin_crash_pending=False,
                    session_operation_context=lease.context,
                )
                return await service.get_current_state(UUID(session_id))
            if writer == "response":
                result = await service.commit_composition_response(
                    session_id=UUID(session_id),
                    expected_current_state_id=expected_state_id,
                    state=data,
                    assistant_content="The proposal is unchanged.",
                    raw_content=None,
                    session_operation_context=lease.context,
                )
                return result.state
            if writer == "interpretations":
                return await service.save_composition_state_with_interpretations(
                    UUID(session_id), data, provenance="post_compose", interpretations=(), session_operation_context=lease.context
                )
            if writer == "repository":
                return service.session_operation_authority.mutate(
                    lease.context,
                    lambda transaction: transaction.composition_states.append_state(
                        SessionCompositionStateCreation(
                            id=uuid4(), data=data, provenance="post_compose", created_at=datetime.now(UTC), derived_from_state_id=None
                        )
                    ),
                )
            operation_context = lease.context
            if writer == "wrong_kind":
                operation_context = replace(operation_context, operation_kind=SessionOperationKind.PROPOSAL)
            elif writer == "wrong_token":
                operation_context = replace(operation_context, fence=replace(operation_context.fence, lease_token=str(uuid4())))
            elif writer == "expired":
                with client.app.state.session_engine.begin() as connection:
                    connection.execute(
                        update(session_operation_fences_table)
                        .where(session_operation_fences_table.c.session_id == session_id)
                        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
            return await service.save_composition_state(
                UUID(session_id),
                data,
                provenance="post_compose",
                session_operation_context=operation_context,
            )
        finally:
            await lease.close()

    return asyncio.run(save())


@pytest.mark.parametrize("writer", ["save", "response", "interpretations", "proposal"])
def test_same_content_compose_checkpoint_moves_pending_anchor(
    composer_test_client: SyncASGITestClient,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    client = composer_test_client
    session_id, _, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    after = _save_checkpoint(client, session_id, _checkpoint_data(before), writer=writer)
    assert after.id != before.id
    assert composition_content_hash(state_from_record(before)) == composition_content_hash(state_from_record(after))
    assert _carried_proposal_anchor(client, session_id).state_id == after.id
    read = client.get(f"/api/sessions/{session_id}/guided")
    assert read.status_code == 200
    assert read.json()["next_turn"]["payload"]["draft_hash"] == staged["next_turn"]["payload"]["draft_hash"]
    assert state_from_record(after).guided_session == state_from_record(before).guided_session
    events = asyncio.run(client.app.state.session_service.list_proposal_events(UUID(session_id)))
    rebases = [event for event in events if event.event_type == "proposal.rebased"]
    assert len(rebases) == 1
    assert rebases[0].payload["reason_code"] == ("ordinary_proposal_checkpoint" if writer == "proposal" else "compose_checkpoint")
    assert rebases[0].actor == ("user:alice" if writer == "proposal" else "compose_loop")
    second = _save_checkpoint(client, session_id, _checkpoint_data(after), writer="save" if writer == "proposal" else writer)
    assert _carried_proposal_anchor(client, session_id).state_id == second.id
    reviewed = _review_wiring(client, session_id)
    assert reviewed["guided_session"]["step"] == "step_4_wire"


@pytest.mark.parametrize("writer", ["save", "response", "interpretations", "proposal"])
def test_changed_composition_cannot_rebase_pending_review(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    client = composer_test_client
    session_id, _, _ = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    metadata = deep_thaw(before.metadata_)
    metadata["description"] = "A different executable authoring request"
    changed = replace(_checkpoint_data(before), metadata_=metadata)
    with pytest.raises(AuditIntegrityError, match="composition content changed"):
        _save_checkpoint(client, session_id, changed, writer=writer)
    assert _head_record(client, session_id).id == before.id
    assert _carried_proposal_anchor(client, session_id).state_id == before.id


@pytest.mark.parametrize("mutation", ["clear", "replace", "facts"])
def test_compose_cannot_change_pending_guided_authority(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    client = composer_test_client
    session_id, _, _ = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    metadata = deep_thaw(before.composer_meta)
    if mutation == "clear":
        metadata["guided_session"] = None
    elif mutation == "replace":
        metadata["guided_session"]["active_proposal"]["proposal_id"] = str(uuid4())
    else:
        source = next(iter(metadata["guided_session"]["reviewed_sources"].values()))
        source["options"]["delimiter"] = "|"
    with pytest.raises(AuditIntegrityError):
        _save_checkpoint(client, session_id, replace(_checkpoint_data(before), composer_meta=metadata))
    assert _head_record(client, session_id).id == before.id
    assert _carried_proposal_anchor(client, session_id).state_id == before.id


def test_raw_repository_append_cannot_strand_pending_review(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, _, _ = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    with pytest.raises(AuditIntegrityError, match="atomic lifecycle settlement"):
        _save_checkpoint(client, session_id, _checkpoint_data(before), writer="repository")
    assert _head_record(client, session_id).id == before.id


@pytest.mark.parametrize(
    "composer_test_client",
    [pytest.param("sqlite", id="sqlite"), pytest.param("postgres", id="postgres", marks=pytest.mark.testcontainer)],
    indirect=True,
)
def test_compose_checkpoint_refuses_live_confirmation_and_allows_expired_admission(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, _, _ = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    operation_id = _seed_a_live_confirmation_admission(client, session_id)
    with pytest.raises(StaleComposeStateError, match="live guided confirmation"):
        _save_checkpoint(client, session_id, _checkpoint_data(before))
    assert _head_record(client, session_id).id == before.id
    with client.app.state.session_engine.begin() as connection:
        connection.execute(
            update(guided_operations_table)
            .where(guided_operations_table.c.operation_id == operation_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    after = _save_checkpoint(client, session_id, _checkpoint_data(before))
    assert _carried_proposal_anchor(client, session_id).state_id == after.id
    with client.app.state.session_engine.begin() as connection:
        admission = connection.execute(
            select(guided_operations_table.c.proposal_id).where(guided_operations_table.c.operation_id == operation_id)
        ).scalar_one()
    assert admission is not None, "COMPOSE checkpoint must not mutate guided operation custody"


@pytest.mark.parametrize("writer", ["save", "response", "interpretations", "turn", "proposal"])
def test_failed_rebase_rolls_back_checkpoint(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    client = composer_test_client
    session_id, _, _ = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    before_messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id)))
    append_rebase = service_module._append_verified_guided_proposal_rebase

    def fail_after_rebase(*args: object, **kwargs: object) -> None:
        append_rebase(*args, **kwargs)
        raise RuntimeError("injected rebase persistence failure")

    monkeypatch.setattr(service_module, "_append_verified_guided_proposal_rebase", fail_after_rebase)
    with pytest.raises(RuntimeError, match="injected rebase"):
        _save_checkpoint(client, session_id, _checkpoint_data(before), writer=writer)
    assert _head_record(client, session_id).id == before.id
    assert _carried_proposal_anchor(client, session_id).state_id == before.id
    events = asyncio.run(client.app.state.session_service.list_proposal_events(UUID(session_id)))
    assert not [event for event in events if event.event_type == "proposal.rebased"]
    after_messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id)))
    assert [message.id for message in after_messages] == [message.id for message in before_messages]


def test_tool_checkpoint_preserves_anchor_and_transcript_lineage(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, _, _ = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    after = _save_checkpoint(client, session_id, _checkpoint_data(before), writer="turn")
    assert after.derived_from_state_id == before.id
    assert _carried_proposal_anchor(client, session_id).state_id == after.id
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id)))
    tool = next(message for message in messages if message.tool_call_id == "unchanged-tool")
    assert tool.composition_state_id == after.id
    assert tool.parent_assistant_id is not None


@pytest.mark.parametrize("fault", ["wrong_kind", "wrong_token", "expired"])
def test_pending_checkpoint_requires_exact_live_compose_fence(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    client = composer_test_client
    session_id, _, _ = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    with pytest.raises(SessionOperationFenceLost):
        _save_checkpoint(client, session_id, _checkpoint_data(before), writer=fault)
    assert _head_record(client, session_id).id == before.id
    assert _carried_proposal_anchor(client, session_id).state_id == before.id


@pytest.mark.parametrize("composer_test_client", [pytest.param("postgres", marks=pytest.mark.testcontainer)], indirect=True)
def test_postgres_confirmation_contends_with_live_checkpoint_save(
    composer_test_client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, _, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    staged = _review_wiring(client, session_id)
    before = _head_record(client, session_id)
    save_entered = Event()
    allow_save = Event()
    parse_pending = service_module.pending_guided_checkpoint

    def pause_after_compose_lease(metadata: object):
        if current_thread().name.startswith("checkpoint-writer"):
            save_entered.set()
            assert allow_save.wait(timeout=30), "test did not release the checkpoint writer"
        return parse_pending(metadata)

    monkeypatch.setattr(service_module, "pending_guided_checkpoint", pause_after_compose_lease)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkpoint-writer") as executor:
        future = executor.submit(_save_checkpoint, client, session_id, _checkpoint_data(before))
        try:
            assert save_entered.wait(timeout=15), "checkpoint writer did not acquire its COMPOSE lease"
            turn = staged["next_turn"]
            response = client.post(
                f"/api/sessions/{session_id}/guided/respond",
                json={
                    "operation_id": str(uuid4()),
                    "turn_token": turn["turn_token"],
                    "proposal_id": turn["payload"]["proposal_id"],
                    "draft_hash": turn["payload"]["draft_hash"],
                    "chosen": ["confirm_wiring"],
                },
            )
            assert response.status_code == 409, response.json()
            assert _head_record(client, session_id).id == before.id
            assert _carried_proposal_anchor(client, session_id).state_id == before.id
        finally:
            allow_save.set()
        after = future.result(timeout=15)
    assert _carried_proposal_anchor(client, session_id).state_id == after.id
    accepted = _confirm_wiring(client, session_id)
    assert accepted["composition_state"]["id"] != str(after.id)
    guided = state_from_record(_head_record(client, session_id)).guided_session
    assert guided is not None
    assert guided.active_proposal is None


@pytest.mark.parametrize("route", ["messages", "recompose"])
def test_messages_same_content_checkpoint_keeps_guided_review_readable(
    composer_test_client: SyncASGITestClient,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    client = composer_test_client
    session_id, _, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)

    async def compose(_message: str, _history: object, state: CompositionState, **_kwargs: object) -> ComposerResult:
        return ComposerResult(message="The reviewed proposal is unchanged.", state=replace(state, version=state.version + 1))

    monkeypatch.setattr(client.app.state.composer_service, "compose", compose, raising=False)
    if route == "recompose":
        service = client.app.state.session_service

        async def add_user_message() -> None:
            lease = await SessionOperationLease.acquire(
                service.session_operation_authority,
                session_id=UUID(session_id),
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=service.session_operation_owner_instance_id,
                lease_seconds=service.session_operation_lease_seconds,
            )
            try:
                await service.add_message(
                    UUID(session_id),
                    "user",
                    "Explain this proposal without changing it.",
                    composition_state_id=before.id,
                    writer_principal="route_user_message",
                    session_operation_context=lease.context,
                )
            finally:
                await lease.close()

        asyncio.run(add_user_message())
    response = client.post(f"/api/sessions/{session_id}/{route}", json={"content": "Explain this proposal without changing it."})
    assert response.status_code == 200, response.json()
    after = _head_record(client, session_id)
    assert after.id != before.id
    assert _carried_proposal_anchor(client, session_id).state_id == after.id
    read = client.get(f"/api/sessions/{session_id}/guided")
    assert read.status_code == 200
    assert read.json()["next_turn"]["payload"]["draft_hash"] == staged["next_turn"]["payload"]["draft_hash"]

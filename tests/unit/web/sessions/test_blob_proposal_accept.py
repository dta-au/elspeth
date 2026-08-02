from __future__ import annotations

import asyncio
import importlib
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretBytes
from sqlalchemy import select, update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs.protocol import BlobRecord
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import proposal_blob_effect_receipts_table
from elspeth.web.sessions.protocol import (
    CompositionProposalRecord,
    CompositionStateData,
    CompositionStateProvenance,
    CompositionStateRecord,
)
from elspeth.web.sessions.routes import create_session_router
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


class _ExecutionServiceFake:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def cleanup_session_lock(self, session_id: str) -> None:
        self._locks.pop(session_id, None)


def _make_app(tmp_path: Path, user_id: str = "alice") -> tuple[FastAPI, SessionServiceImpl]:
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    telemetry = build_sessions_telemetry()
    service = SessionServiceImpl(
        engine,
        telemetry=telemetry,
        log=structlog.get_logger("test"),
    )

    app = FastAPI()
    identity = UserIdentity(user_id=user_id, username=user_id)

    async def mock_user() -> UserIdentity:
        return identity

    app.dependency_overrides[get_current_user] = mock_user
    app.state.session_service = service
    app.state.session_engine = engine
    app.state.sessions_telemetry = telemetry
    app.state.settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    app.state.catalog_service = catalog
    app.state.operator_profile_registry = MagicMock(spec=OperatorProfileRegistry)
    app.state.plugin_snapshot_factory = lambda _user: snapshot
    app.state.composer_service = None
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    app.state.execution_service = _ExecutionServiceFake()
    app.state.composer_progress_registry = ComposerProgressRegistry(
        engine=engine,
        session_operation_authority=service.session_operation_authority,
    )
    app.state.scoped_secret_resolver = None
    app.include_router(create_session_router())
    return app, service


def _patch_route_execute_tool(monkeypatch: pytest.MonkeyPatch, wrapper_factory) -> None:
    for module_name in (
        "elspeth.web.sessions.routes.composer",
        "elspeth.web.sessions.routes.composer.proposals",
    ):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        original = getattr(module, "execute_tool", None)
        if original is None:
            continue
        monkeypatch.setattr(module, "execute_tool", wrapper_factory(original))
        return
    raise AssertionError("could not locate composer proposal route execute_tool binding")


async def _create_test_blob(
    service: SessionServiceImpl,
    blob_service: BlobServiceImpl,
    session_id: UUID,
    *,
    filename: str,
    content: bytes,
) -> BlobRecord:
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        return await blob_service.create_blob(
            session_id,
            filename=filename,
            content=content,
            mime_type="text/plain",
            created_by="user",
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)


async def _create_test_proposal(
    service: SessionServiceImpl,
    *,
    session_id: UUID,
    **kwargs: Any,
) -> CompositionProposalRecord:
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        return await service.create_composition_proposal(
            session_id=session_id,
            session_operation_context=context,
            **kwargs,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)


async def _save_composition_state(
    service: SessionServiceImpl,
    session_id: UUID,
    state: CompositionStateData,
    *,
    provenance: CompositionStateProvenance,
) -> CompositionStateRecord:
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        return await service.save_composition_state(
            session_id,
            state,
            provenance=provenance,
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)


def test_accept_update_blob_proposal_commits_without_composition_state_delta(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    blob_service = BlobServiceImpl(
        service._engine,
        tmp_path,
        session_operation_authority=service.session_operation_authority,
    )
    client = TestClient(app)
    session = asyncio.run(service.create_session("alice", "Blob approval", "local"))
    session_id = session.id
    state_record = asyncio.run(
        _save_composition_state(
            service,
            session_id,
            CompositionStateData(metadata_={"name": "Blob approval", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
    )
    user_message = asyncio.run(
        service.add_message(
            session_id,
            "user",
            "Please update the report blob with the approved text.",
            writer_principal="route_user_message",
        )
    )
    blob = asyncio.run(
        _create_test_blob(
            service,
            blob_service,
            session_id,
            filename="report.txt",
            content=b"original content",
        )
    )
    arguments = {"blob_id": str(blob.id), "content": "approved content"}
    proposal = asyncio.run(
        _create_test_proposal(
            service,
            session_id=session_id,
            tool_call_id="call_update_blob",
            tool_name="update_blob",
            summary="Update the report blob.",
            rationale="Requested by the current composer turn.",
            affects=("blob",),
            arguments_json=arguments,
            arguments_redacted_json={"blob_id": str(blob.id), "content": "<redacted>"},
            base_state_id=state_record.id,
            actor="composer-web:user:alice",
            user_message_id=user_message.id,
            composer_model_identifier="openai/gpt-5-mini",
            composer_model_version="gpt-5-mini-2026-05-01",
            composer_provider="openai",
            composer_skill_hash="sha256:composer-skill",
            tool_arguments_hash=stable_hash(arguments),
        )
    )

    response = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "committed"
    assert body["committed_state_id"] == str(state_record.id)
    assert Path(blob.storage_path).read_text(encoding="utf-8") == "approved content"
    persisted = asyncio.run(service.get_current_state(session_id))
    assert persisted is not None
    assert persisted.id == state_record.id


def test_accept_update_blob_proposal_remains_blocked_by_second_pending_reference(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    blob_service = BlobServiceImpl(
        service._engine,
        tmp_path,
        session_operation_authority=service.session_operation_authority,
    )
    client = TestClient(app)
    session = asyncio.run(service.create_session("alice", "Blob approval conflict", "local"))
    session_id = session.id
    state_record = asyncio.run(
        _save_composition_state(
            service,
            session_id,
            CompositionStateData(metadata_={"name": "Blob approval conflict", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
    )
    user_message = asyncio.run(
        service.add_message(
            session_id,
            "user",
            "Please update the report blob after I choose one proposal.",
            writer_principal="route_user_message",
        )
    )
    blob = asyncio.run(
        _create_test_blob(
            service,
            blob_service,
            session_id,
            filename="report.txt",
            content=b"original content",
        )
    )

    async def create_update_proposal(*, tool_call_id: str, content: str) -> CompositionProposalRecord:
        arguments = {"blob_id": str(blob.id), "content": content}
        return await _create_test_proposal(
            service,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name="update_blob",
            summary="Update the report blob.",
            rationale="Requested by the current composer turn.",
            affects=("blob",),
            arguments_json=arguments,
            arguments_redacted_json={"blob_id": str(blob.id), "content": "<redacted>"},
            base_state_id=state_record.id,
            actor="composer-web:user:alice",
            user_message_id=user_message.id,
            composer_model_identifier="openai/gpt-5-mini",
            composer_model_version="gpt-5-mini-2026-05-01",
            composer_provider="openai",
            composer_skill_hash="sha256:composer-skill",
            tool_arguments_hash=stable_hash(arguments),
        )

    accepted_candidate = asyncio.run(create_update_proposal(tool_call_id="call_update_blob_first", content="first content"))
    retaining_proposal = asyncio.run(create_update_proposal(tool_call_id="call_update_blob_second", content="second content"))

    response = client.post(f"/api/sessions/{session_id}/proposals/{accepted_candidate.id}/accept")

    assert response.status_code == 422, response.text
    assert str(retaining_proposal.id) in response.text
    assert Path(blob.storage_path).read_text(encoding="utf-8") == "original content"
    proposals = asyncio.run(service.list_composition_proposals(session_id))
    assert {proposal.id: proposal.status for proposal in proposals} == {
        accepted_candidate.id: "rejected",
        retaining_proposal.id: "pending",
    }


def test_accept_delete_blob_proposal_commits_without_composition_state_delta(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    blob_service = BlobServiceImpl(
        service._engine,
        tmp_path,
        session_operation_authority=service.session_operation_authority,
    )
    client = TestClient(app)
    session = asyncio.run(service.create_session("alice", "Blob deletion approval", "local"))
    session_id = session.id
    state_record = asyncio.run(
        _save_composition_state(
            service,
            session_id,
            CompositionStateData(metadata_={"name": "Blob deletion approval", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
    )
    blob = asyncio.run(
        _create_test_blob(
            service,
            blob_service,
            session_id,
            filename="obsolete.txt",
            content=b"obsolete content",
        )
    )
    arguments = {"blob_id": str(blob.id)}
    proposal = asyncio.run(
        _create_test_proposal(
            service,
            session_id=session_id,
            tool_call_id="call_delete_blob",
            tool_name="delete_blob",
            summary="Delete the obsolete blob.",
            rationale="Requested by the current composer turn.",
            affects=("blob",),
            arguments_json=arguments,
            arguments_redacted_json=arguments,
            base_state_id=state_record.id,
            actor="composer-web:user:alice",
            tool_arguments_hash=stable_hash(arguments),
            user_message_id=None,
            composer_model_identifier="openai/gpt-5-mini",
            composer_model_version="gpt-5-mini-2026-05-01",
            composer_provider="openai",
            composer_skill_hash="sha256:composer-skill",
        )
    )

    response = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "committed"
    assert body["committed_state_id"] == str(state_record.id)
    assert not Path(blob.storage_path).exists()
    persisted = asyncio.run(service.get_current_state(session_id))
    assert persisted is not None
    assert persisted.id == state_record.id


@pytest.mark.parametrize(
    ("tool_name", "tampered_receipt_field", "advance_state_head"),
    [
        ("update_blob", None, False),
        ("delete_blob", None, False),
        ("update_blob", "arguments_hash", False),
        ("delete_blob", "result_blob_snapshot_hash", False),
        ("update_blob", None, True),
        ("delete_blob", None, True),
    ],
)
def test_retry_after_blob_effect_before_proposal_settlement_does_not_reexecute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    tampered_receipt_field: str | None,
    advance_state_head: bool,
) -> None:
    app, service = _make_app(tmp_path)
    blob_service = BlobServiceImpl(
        service._engine,
        tmp_path,
        session_operation_authority=service.session_operation_authority,
    )
    client = TestClient(app)
    session = asyncio.run(service.create_session("alice", "Blob approval recovery", "local"))
    session_id = session.id
    state_record = asyncio.run(
        _save_composition_state(
            service,
            session_id,
            CompositionStateData(metadata_={"name": "Blob approval recovery", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
    )
    blob = asyncio.run(
        _create_test_blob(
            service,
            blob_service,
            session_id,
            filename="report.txt",
            content=b"original content",
        )
    )
    user_message_id = None
    arguments: dict[str, str] = {"blob_id": str(blob.id)}
    if tool_name == "update_blob":
        user_message = asyncio.run(
            service.add_message(
                session_id,
                "user",
                "Please update the report blob with the approved text.",
                writer_principal="route_user_message",
            )
        )
        user_message_id = user_message.id
        arguments["content"] = "approved content"
    proposal = asyncio.run(
        _create_test_proposal(
            service,
            session_id=session_id,
            tool_call_id=f"call_{tool_name}_recovery",
            tool_name=tool_name,
            summary=f"Apply approved {tool_name} mutation.",
            rationale="Requested by the current composer turn.",
            affects=("blob",),
            arguments_json=arguments,
            arguments_redacted_json=arguments,
            base_state_id=state_record.id,
            actor="composer-web:user:alice",
            user_message_id=user_message_id,
            composer_model_identifier="openai/gpt-5-mini",
            composer_model_version="gpt-5-mini-2026-05-01",
            composer_provider="openai",
            composer_skill_hash="sha256:composer-skill",
            tool_arguments_hash=stable_hash(arguments),
        )
    )

    execution_calls = 0

    def wrapper_factory(original):
        def counted_execute_tool(*args, **kwargs):
            nonlocal execution_calls
            execution_calls += 1
            return original(*args, **kwargs)

        return counted_execute_tool

    _patch_route_execute_tool(monkeypatch, wrapper_factory)
    original_accept = service.accept_composition_proposal
    settlement_calls = 0

    async def fail_first_settlement(**kwargs):
        nonlocal settlement_calls
        settlement_calls += 1
        if settlement_calls == 1:
            raise RuntimeError("injected failure after blob effect before proposal settlement")
        return await original_accept(**kwargs)

    monkeypatch.setattr(service, "accept_composition_proposal", fail_first_settlement)

    with pytest.raises(RuntimeError, match="injected failure after blob effect"):
        client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")

    proposals_after_fault = asyncio.run(service.list_composition_proposals(session_id))
    assert [item.status for item in proposals_after_fault] == ["pending"]
    if tool_name == "update_blob":
        assert Path(blob.storage_path).read_text(encoding="utf-8") == "approved content"
    else:
        assert not Path(blob.storage_path).exists()

    with service._engine.begin() as conn:
        receipt = conn.execute(
            select(proposal_blob_effect_receipts_table).where(proposal_blob_effect_receipts_table.c.proposal_id == str(proposal.id))
        ).one()
        assert receipt.session_id == str(session_id)
        assert receipt.tool_name == tool_name
        assert receipt.blob_id == str(blob.id)
        assert receipt.arguments_hash == stable_hash(arguments)
        assert receipt.result_blob_snapshot["id"] == str(blob.id)
        assert receipt.accepted_event_id is None
        assert receipt.accepted_at is None
        if tampered_receipt_field is not None:
            conn.execute(
                update(proposal_blob_effect_receipts_table)
                .where(proposal_blob_effect_receipts_table.c.proposal_id == str(proposal.id))
                .values({tampered_receipt_field: "f" * 64})
            )

    if tampered_receipt_field is not None:
        with pytest.raises(AuditIntegrityError, match="blob effect receipt"):
            client.post(
                f"/api/sessions/{session_id}/proposals/{proposal.id}/reject",
                json={"reason": "must fail closed on corrupt receipt"},
            )
        with pytest.raises(AuditIntegrityError, match="blob effect receipt"):
            client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")
        assert execution_calls == 1
        return

    advanced_state = None
    if advance_state_head:
        advanced_state = asyncio.run(
            _save_composition_state(
                service,
                session_id,
                CompositionStateData(metadata_={"name": "independently advanced", "description": ""}, is_valid=True),
                provenance="session_seed",
            )
        )

    reject_response = client.post(
        f"/api/sessions/{session_id}/proposals/{proposal.id}/reject",
        json={"reason": "operator changed mind after the applied effect"},
    )

    assert reject_response.status_code == 409, reject_response.text

    retry_response = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")

    assert retry_response.status_code == 200, retry_response.text
    assert retry_response.json()["status"] == "committed"
    if advanced_state is not None:
        assert retry_response.json()["committed_state_id"] == str(advanced_state.id)
        assert asyncio.run(service.get_current_state(session_id)) == advanced_state
    assert execution_calls == 1
    with service._engine.connect() as conn:
        settled_receipt = conn.execute(
            select(proposal_blob_effect_receipts_table).where(proposal_blob_effect_receipts_table.c.proposal_id == str(proposal.id))
        ).one()
    assert settled_receipt.accepted_event_id == retry_response.json()["audit_event_id"]
    assert settled_receipt.accepted_at is not None

    duplicate_response = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")

    assert duplicate_response.status_code == 200, duplicate_response.text
    assert duplicate_response.json() == retry_response.json()
    assert execution_calls == 1


@pytest.mark.asyncio
async def test_accept_update_blob_proposal_serializes_against_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = _make_app(tmp_path)
    blob_service = BlobServiceImpl(
        service._engine,
        tmp_path,
        session_operation_authority=service.session_operation_authority,
    )
    session = await service.create_session("alice", "Blob approval race", "local")
    session_id = session.id
    state_record = await _save_composition_state(
        service,
        session_id,
        CompositionStateData(metadata_={"name": "Blob approval race", "description": ""}, is_valid=True),
        provenance="session_seed",
    )
    user_message = await service.add_message(
        session_id,
        "user",
        "Please update the report blob with the approved text.",
        writer_principal="route_user_message",
    )
    blob = await _create_test_blob(
        service,
        blob_service,
        session_id,
        filename="report.txt",
        content=b"original content",
    )
    arguments = {"blob_id": str(blob.id), "content": "approved content"}
    proposal = await _create_test_proposal(
        service,
        session_id=session_id,
        tool_call_id="call_update_blob_race",
        tool_name="update_blob",
        summary="Update the report blob.",
        rationale="Requested by the current composer turn.",
        affects=("blob",),
        arguments_json=arguments,
        arguments_redacted_json={"blob_id": str(blob.id), "content": "<redacted>"},
        base_state_id=state_record.id,
        actor="composer-web:user:alice",
        user_message_id=user_message.id,
        composer_model_identifier="openai/gpt-5-mini",
        composer_model_version="gpt-5-mini-2026-05-01",
        composer_provider="openai",
        composer_skill_hash="sha256:composer-skill",
        tool_arguments_hash=stable_hash(arguments),
    )
    entered_tool = threading.Event()
    release_tool = threading.Event()
    observed_tool_authority: dict[str, Any] = {}

    def wrapper_factory(original):
        def gated_execute_tool(*args, **kwargs):
            observed_tool_authority.update(
                authority=kwargs.get("session_operation_authority"),
                context=kwargs.get("session_operation_context"),
                accepting_proposal_id=kwargs.get("_accepting_proposal_id"),
            )
            entered_tool.set()
            if not release_tool.wait(timeout=5):
                raise TimeoutError("test timed out waiting to release execute_tool")
            return original(*args, **kwargs)

        return gated_execute_tool

    _patch_route_execute_tool(monkeypatch, wrapper_factory)

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            accept_task = asyncio.create_task(client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept"))
            assert await asyncio.to_thread(entered_tool.wait, 5)

            reject_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session_id}/proposals/{proposal.id}/reject",
                    json={"reason": "operator changed mind"},
                )
            )
            await asyncio.sleep(0.05)
            assert not reject_task.done()

            release_tool.set()
            accept_response, reject_response = await asyncio.gather(accept_task, reject_task)
    finally:
        release_tool.set()

    assert accept_response.status_code == 200
    assert reject_response.status_code == 409
    assert observed_tool_authority["authority"] is service.session_operation_authority
    assert observed_tool_authority["context"].operation_kind is SessionOperationKind.PROPOSAL
    assert observed_tool_authority["context"].fence.session_id == str(session_id)
    assert observed_tool_authority["accepting_proposal_id"] == proposal.id
    assert Path(blob.storage_path).read_text(encoding="utf-8") == "approved content"
    proposals = await service.list_composition_proposals(session_id)
    assert [item.status for item in proposals] == ["committed"]
    events = await service.list_proposal_events(session_id)
    assert [event.event_type for event in events] == [
        "proposal.created",
        "proposal.accepted",
    ]


@pytest.mark.asyncio
async def test_cancelled_accept_update_blob_proposal_still_terminalizes_before_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = _make_app(tmp_path)
    blob_service = BlobServiceImpl(
        service._engine,
        tmp_path,
        session_operation_authority=service.session_operation_authority,
    )
    session = await service.create_session("alice", "Blob approval cancellation", "local")
    session_id = session.id
    state_record = await _save_composition_state(
        service,
        session_id,
        CompositionStateData(metadata_={"name": "Blob approval cancellation", "description": ""}, is_valid=True),
        provenance="session_seed",
    )
    user_message = await service.add_message(
        session_id,
        "user",
        "Please update the report blob with the approved text.",
        writer_principal="route_user_message",
    )
    blob = await _create_test_blob(
        service,
        blob_service,
        session_id,
        filename="report.txt",
        content=b"original content",
    )
    arguments = {"blob_id": str(blob.id), "content": "approved content"}
    proposal = await _create_test_proposal(
        service,
        session_id=session_id,
        tool_call_id="call_update_blob_cancel",
        tool_name="update_blob",
        summary="Update the report blob.",
        rationale="Requested by the current composer turn.",
        affects=("blob",),
        arguments_json=arguments,
        arguments_redacted_json={"blob_id": str(blob.id), "content": "<redacted>"},
        base_state_id=state_record.id,
        actor="composer-web:user:alice",
        user_message_id=user_message.id,
        composer_model_identifier="openai/gpt-5-mini",
        composer_model_version="gpt-5-mini-2026-05-01",
        composer_provider="openai",
        composer_skill_hash="sha256:composer-skill",
        tool_arguments_hash=stable_hash(arguments),
    )
    entered_tool = threading.Event()
    release_tool = threading.Event()

    def wrapper_factory(original):
        def gated_execute_tool(*args, **kwargs):
            entered_tool.set()
            if not release_tool.wait(timeout=5):
                raise TimeoutError("test timed out waiting to release execute_tool")
            return original(*args, **kwargs)

        return gated_execute_tool

    _patch_route_execute_tool(monkeypatch, wrapper_factory)

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            accept_task = asyncio.create_task(client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept"))
            assert await asyncio.to_thread(entered_tool.wait, 5)
            accept_task.cancel()

            reject_task = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session_id}/proposals/{proposal.id}/reject",
                    json={"reason": "operator changed mind"},
                )
            )
            await asyncio.sleep(0.05)
            assert not reject_task.done()

            release_tool.set()
            with pytest.raises(asyncio.CancelledError):
                await accept_task
            reject_response = await reject_task
    finally:
        release_tool.set()

    assert reject_response.status_code == 409
    assert Path(blob.storage_path).read_text(encoding="utf-8") == "approved content"
    proposals = await service.list_composition_proposals(session_id)
    assert [item.status for item in proposals] == ["committed"]
    events = await service.list_proposal_events(session_id)
    assert [event.event_type for event in events] == [
        "proposal.created",
        "proposal.accepted",
    ]

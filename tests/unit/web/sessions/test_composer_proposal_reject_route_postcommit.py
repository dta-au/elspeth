from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from structlog.testing import capture_logs

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.sessions.protocol import CompositionProposalRecord
from elspeth.web.sessions.routes.composer import proposals as proposal_routes


def _create_ordinary_proposal(
    test_client: TestClient,
    *,
    tool_name: str = "set_pipeline",
    arguments_json: dict[str, object] | None = None,
) -> tuple[dict[str, Any], CompositionProposalRecord]:
    session = test_client.post("/api/sessions", json={"title": "Ordinary proposal reject"}).json()
    session_id = UUID(session["id"])
    app = cast(FastAPI, test_client.app)
    service = app.state.session_service

    async def create() -> object:
        context = await service._run_sync(
            lambda: service.session_operation_authority.acquire(
                session_id=session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=service.session_operation_owner_instance_id,
                lease_seconds=service.session_operation_lease_seconds,
            )
        )
        try:
            arguments = arguments_json or {"sources": {}, "nodes": [], "edges": [], "outputs": []}
            return await service.create_composition_proposal(
                session_id=session_id,
                tool_call_id="call_route_reject",
                tool_name=tool_name,
                summary="Reject this proposal.",
                rationale="Requested by the user.",
                affects=("graph",),
                arguments_json=arguments,
                arguments_redacted_json=arguments,
                base_state_id=None,
                actor="composer-web:user-alice",
                session_operation_context=context,
            )
        finally:
            await service._run_sync(service.session_operation_authority.release, context)

    return session, asyncio.run(create())


def test_ordinary_reject_route_passes_exact_proposal_context(test_client: TestClient, monkeypatch) -> None:
    session, proposal = _create_ordinary_proposal(test_client)
    app = cast(FastAPI, test_client.app)
    service = app.state.session_service
    original_reject = type(service).reject_composition_proposal
    seen_kinds: list[SessionOperationKind] = []

    async def observe_context(self: object, **kwargs: object) -> object:
        context = cast(SessionOperationContext, kwargs["session_operation_context"])
        seen_kinds.append(context.operation_kind)
        return await original_reject(self, **kwargs)

    monkeypatch.setattr(type(service), "reject_composition_proposal", observe_context)
    response = test_client.post(f"/api/sessions/{session['id']}/proposals/{proposal.id}/reject", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert seen_kinds == [SessionOperationKind.PROPOSAL]
    events = test_client.get(f"/api/sessions/{session['id']}/proposal-events").json()
    assert {event["event_type"] for event in events} == {"proposal.created", "proposal.rejected"}
    rejected_event = next(event for event in events if event["event_type"] == "proposal.rejected")
    assert response.json()["audit_event_id"] == rejected_event["id"]


def test_committed_ordinary_reject_logs_cleanup_fault_without_masking_success(test_client: TestClient, monkeypatch) -> None:
    session, proposal = _create_ordinary_proposal(test_client)
    original_close = SessionOperationLease.close
    secret = "postgresql://operator:secret@example.invalid/elspeth"  # secret-scan: allow-this-line

    async def close_then_fail(lease: SessionOperationLease) -> None:
        await original_close(lease)
        raise RuntimeError(secret)

    monkeypatch.setattr(SessionOperationLease, "close", close_then_fail)
    with capture_logs() as logs:
        response = test_client.post(f"/api/sessions/{session['id']}/proposals/{proposal.id}/reject", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert secret not in json.dumps(logs)
    assert {
        "event": "composer_proposal_reject_postcommit_cleanup_failed",
        "session_id": session["id"],
        "exc_class": "RuntimeError",
        "log_level": "error",
    } in logs


def test_committed_ordinary_accept_logs_cleanup_fault_without_masking_success(test_client: TestClient, monkeypatch) -> None:
    session, proposal = _create_ordinary_proposal(
        test_client,
        tool_name="set_metadata",
        arguments_json={"patch": {"name": "Accepted proposal"}},
    )
    app = cast(FastAPI, test_client.app)
    catalog = create_catalog_service()
    app.state.catalog_service = catalog
    app.state.operator_profile_registry = MagicMock(spec=OperatorProfileRegistry)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    app.state.plugin_snapshot_factory = lambda _user: snapshot
    app.state.scoped_secret_resolver = None
    original_close = SessionOperationLease.close
    secret = "postgresql://operator:secret@example.invalid/elspeth"  # secret-scan: allow-this-line

    async def close_then_fail(lease: SessionOperationLease) -> None:
        await original_close(lease)
        raise RuntimeError(secret)

    monkeypatch.setattr(SessionOperationLease, "close", close_then_fail)
    with capture_logs() as logs:
        response = test_client.post(f"/api/sessions/{session['id']}/proposals/{proposal.id}/accept")

    assert response.status_code == 200
    assert response.json()["status"] == "committed"
    assert secret not in json.dumps(logs)
    assert {
        "event": "composer_proposal_accept_postcommit_cleanup_failed",
        "session_id": session["id"],
        "exc_class": "RuntimeError",
        "log_level": "error",
    } in logs


def test_ordinary_accept_route_passes_exact_proposal_context_and_expected_head(test_client: TestClient, monkeypatch) -> None:
    session, proposal = _create_ordinary_proposal(
        test_client,
        tool_name="set_metadata",
        arguments_json={"patch": {"name": "Exact proposal context"}},
    )
    app = cast(FastAPI, test_client.app)
    catalog = create_catalog_service()
    app.state.catalog_service = catalog
    app.state.operator_profile_registry = MagicMock(spec=OperatorProfileRegistry)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    app.state.plugin_snapshot_factory = lambda _user: snapshot
    app.state.scoped_secret_resolver = None
    service = app.state.session_service
    original_accept = type(service).accept_composition_proposal
    observed: list[tuple[SessionOperationKind, object, object]] = []

    async def observe_context(self: object, **kwargs: object) -> object:
        context = cast(SessionOperationContext, kwargs["session_operation_context"])
        observed.append((context.operation_kind, kwargs["expected_current_state_id"], kwargs["state"]))
        return await original_accept(self, **kwargs)

    monkeypatch.setattr(type(service), "accept_composition_proposal", observe_context)
    response = test_client.post(f"/api/sessions/{session['id']}/proposals/{proposal.id}/accept")

    assert response.status_code == 200
    assert response.json()["status"] == "committed"
    assert len(observed) == 1
    operation_kind, expected_current_state_id, accepted_state = observed[0]
    assert operation_kind is SessionOperationKind.PROPOSAL
    assert expected_current_state_id is None
    assert accepted_state is not None


@pytest.mark.asyncio
async def test_cancellation_after_ordinary_accept_commit_drains_lease_cleanup(
    test_client: TestClient,
    monkeypatch,
) -> None:
    session, proposal = await asyncio.to_thread(
        _create_ordinary_proposal,
        test_client,
        tool_name="set_metadata",
        arguments_json={"patch": {"name": "Cancelled durable acceptance"}},
    )
    app = cast(FastAPI, test_client.app)
    catalog = create_catalog_service()
    app.state.catalog_service = catalog
    app.state.operator_profile_registry = MagicMock(spec=OperatorProfileRegistry)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    app.state.plugin_snapshot_factory = lambda _user: snapshot
    app.state.scoped_secret_resolver = None
    service = app.state.session_service
    original_accept = type(service).accept_composition_proposal
    original_close = SessionOperationLease.close
    close_started = asyncio.Event()
    close_finished = asyncio.Event()
    request_task = asyncio.current_task()
    assert request_task is not None

    async def accept_then_cancel(self: object, **kwargs: object) -> object:
        committed = await original_accept(self, **kwargs)
        asyncio.get_running_loop().call_soon(request_task.cancel)
        return committed

    async def observed_close(lease: SessionOperationLease) -> None:
        close_started.set()
        try:
            await original_close(lease)
        finally:
            close_finished.set()

    monkeypatch.setattr(type(service), "accept_composition_proposal", accept_then_cancel)
    monkeypatch.setattr(SessionOperationLease, "close", observed_close)
    request = Request({"type": "http", "app": app})

    with pytest.raises(asyncio.CancelledError):
        await proposal_routes.accept_composition_proposal(
            UUID(session["id"]),
            proposal.id,
            request,
            None,
            UserIdentity(user_id="alice", username="alice"),
        )

    assert close_started.is_set()
    assert close_finished.is_set()
    persisted = await service.get_authoritative_composition_proposal(
        session_id=UUID(session["id"]),
        proposal_id=proposal.id,
        reviewed_facts=None,
    )
    assert persisted.row.status == "committed"


def test_validation_failure_auto_reject_cleanup_fault_cannot_mask_422(test_client: TestClient, monkeypatch) -> None:
    session, proposal = _create_ordinary_proposal(
        test_client,
        arguments_json={
            "sources": {
                "primary": {
                    "plugin": "definitely_missing",
                    "on_success": "rows",
                    "on_validation_failure": "discard",
                    "options": {},
                }
            },
            "nodes": [],
            "edges": [],
            "outputs": [],
        },
    )
    original_close = SessionOperationLease.close
    app = cast(FastAPI, test_client.app)
    service = app.state.session_service
    original_reject = type(service).reject_composition_proposal
    seen_kinds: list[SessionOperationKind] = []
    catalog = create_catalog_service()
    app.state.catalog_service = catalog
    app.state.operator_profile_registry = MagicMock(spec=OperatorProfileRegistry)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    app.state.plugin_snapshot_factory = lambda _user: snapshot

    async def observe_context(self: object, **kwargs: object) -> object:
        context = cast(SessionOperationContext, kwargs["session_operation_context"])
        seen_kinds.append(context.operation_kind)
        return await original_reject(self, **kwargs)

    async def close_then_fail(lease: SessionOperationLease) -> None:
        await original_close(lease)
        raise RuntimeError("sensitive auto-reject cleanup failure")

    monkeypatch.setattr(type(service), "reject_composition_proposal", observe_context)
    monkeypatch.setattr(SessionOperationLease, "close", close_then_fail)
    response = test_client.post(f"/api/sessions/{session['id']}/proposals/{proposal.id}/accept")

    assert response.status_code == 422
    assert response.json()["detail"]["error_type"] == "proposal_validation_failed"
    assert seen_kinds == [SessionOperationKind.PROPOSAL]
    proposals = test_client.get(f"/api/sessions/{session['id']}/proposals").json()
    assert proposals[0]["status"] == "rejected"

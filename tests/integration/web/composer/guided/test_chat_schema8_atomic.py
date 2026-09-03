"""Schema-8 atomic CHAT route contracts."""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, text

from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
    InterpretationSource,
)
from elspeth.contracts.composer_llm_audit import ComposerChatTurnStatus, ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.guided.chat_solver import Step1SourceChatResolution
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SinkResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.sessions._guided_step_chat import (
    GuidedStepChatOnlyResult,
    Step1SourcePluginReselectedResult,
    Step1SourceResolvedResult,
    Step2SinkResolvedResult,
    StepChatResult,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import guided_operations_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes._helpers import _initial_composition_state_with_guided_session
from elspeth.web.sessions.routes.composer import guided as guided_route
from elspeth.web.sessions.routes.composer.guided_chat_atomic import GuidedChatProviderOutcome
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.schemas import GuidedChatRequest
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.integration.web.composer.guided.test_respond import TestStep2IntraStep as _Step2Journey
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


@pytest.fixture
def file_composer_test_client(composer_test_client: TestClient, tmp_path: Path) -> Iterator[TestClient]:
    """Rebind the minimal app to file SQLite for real multi-connection races."""
    engine = create_session_engine(f"sqlite:///{tmp_path / 'chat-races.db'}")
    initialize_session_schema(engine)
    composer_test_client.app.state.session_engine = engine
    composer_test_client.app.state.session_service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.guided.chat.races"),
    )
    try:
        yield composer_test_client
    finally:
        engine.dispose()


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"title": "schema-8 chat"})
    assert response.status_code == 201, response.json()
    session_id = response.json()["id"]
    start = client.post(
        f"/api/sessions/{session_id}/guided/start",
        json={
            "profile": "live",
            "intent": "Begin this guided chat session.",
            "operation_id": str(uuid4()),
        },
    )
    assert start.status_code == 200, start.json()
    return session_id


def _chat_body(turn: dict, *, operation_id: str | None = None, message: str = "Use CSV") -> dict[str, str]:
    return {
        "operation_id": operation_id or str(uuid4()),
        "turn_token": turn["turn_token"],
        "message": message,
    }


def _chat_operation_count(client: TestClient, session_id: str) -> int:
    with client.app.state.session_engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count())
                .select_from(guided_operations_table)
                .where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.kind == "guided_chat",
                )
            ).scalar_one()
        )


def test_step_2_singular_sink_resolution_maps_to_the_live_transition() -> None:
    from elspeth.web.sessions.routes.composer.guided_chat_atomic import _transition_request

    body = GuidedChatRequest.model_validate(
        {
            "operation_id": str(uuid4()),
            "turn_token": "a" * 64,
            "message": "Use JSON",
        },
        strict=True,
    )
    sink = SinkResolved(
        outputs=(
            SinkOutputResolved(
                name="result",
                plugin="json",
                options={"path": "out.jsonl"},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        )
    )

    request = _transition_request(
        body=body,
        guided=SimpleNamespace(step=GuidedStep.STEP_2_SINK, active_edit_target=None),
        current_turn={"type": "single_select", "step_index": 1, "payload": {}},
        source_resolution=None,
        sink_resolution=sink,
    )

    assert request is not None
    assert request.chosen == ["json"]


def _choose_source(client: TestClient, session_id: str, turn: dict, plugin: str = "csv") -> dict:
    response = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": turn["turn_token"],
            "chosen": [plugin],
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def _source_resolution() -> Step1SourceChatResolution:
    return Step1SourceChatResolution(
        assistant_message="I prepared the CSV source.",
        plugin="csv",
        filename="source.csv",
        mime_type="text/csv",
        content="name,value\nalice,1\n",
        options={"schema": {"mode": "observed"}},
        observed_columns=("name", "value"),
        sample_rows=({"name": "alice", "value": "1"},),
        on_validation_failure="discard",
    )


async def _resolved_source_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
    resolution = _source_resolution()
    return Step1SourceResolvedResult(
        chat=StepChatResult(
            assistant_message=resolution.assistant_message,
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=1,
            error_class=None,
        ),
        resolution=resolution,
        deferred_actions=(),
    )


async def _reselected_json_source_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
    return Step1SourcePluginReselectedResult(
        chat=StepChatResult(
            assistant_message="I changed the source type to JSON and kept the uploaded file ready.",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=1,
            error_class=None,
        ),
        plugin="json",
    )


def _persist_guided(client: TestClient, session_id: str, guided: GuidedSession) -> None:
    state = replace(_initial_composition_state_with_guided_session(), guided_session=guided)
    state_dict = state.to_dict()
    asyncio.run(
        client.app.state.session_service.save_composition_state(
            UUID(session_id),
            CompositionStateData(
                sources=state_dict["sources"],
                nodes=state_dict["nodes"],
                edges=state_dict["edges"],
                outputs=state_dict["outputs"],
                metadata_=state_dict["metadata"],
                is_valid=False,
                composer_meta={"guided_session": guided.to_dict()},
            ),
            provenance="session_seed",
        )
    )


async def _advisory_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
    return GuidedStepChatOnlyResult(
        chat=StepChatResult(
            assistant_message="Review the current source choices.",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=1,
            error_class=None,
        ),
    )


def _record_test_llm_call(
    recorder: object,
    *,
    marker: str,
    status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS,
) -> None:
    now = datetime.now(UTC)
    call = ComposerLLMCall(
        model_requested="test/guided-chat",
        model_returned="test/guided-chat" if status is ComposerLLMCallStatus.SUCCESS else None,
        status=status,
        prompt_tokens=1 if status is ComposerLLMCallStatus.SUCCESS else None,
        completion_tokens=1 if status is ComposerLLMCallStatus.SUCCESS else None,
        total_tokens=2 if status is ComposerLLMCallStatus.SUCCESS else None,
        latency_ms=1,
        provider_request_id=None,
        messages_hash=stable_hash({"marker": marker}),
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=now,
        finished_at=now,
        error_class=None if status is ComposerLLMCallStatus.SUCCESS else "ProviderFailure",
        error_message=None if status is ComposerLLMCallStatus.SUCCESS else f"secret-{marker}",
        temperature=0.0,
        seed=42,
    )
    recorder.record_llm_call(call)  # type: ignore[attr-defined]


def _llm_audit_calls(client: TestClient, session_id: str) -> list[dict[str, object]]:
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    return [
        envelope["call"] for message in messages for envelope in (message.tool_calls or ()) if envelope.get("_kind") == "llm_call_audit"
    ]


def _assert_one_llm_failure_audit(client: TestClient, session_id: str, *, marker: str) -> None:
    calls = _llm_audit_calls(client, session_id)
    assert len(calls) == 1, calls
    assert calls[0]["messages_hash"] == stable_hash({"marker": marker})
    assert calls[0]["error_message"] is None


def test_advisory_chat_settles_once_and_exact_replay_ignores_mutable_provider_and_policy(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.sessions.routes.composer import guided_chat_atomic

    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert first.status_code == 200, first.json()
    first_json = first.json()
    assert first_json["assistant_message"] == "Review the current source choices."
    assert first_json["assistant_message_kind"] == "assistant"
    assert first_json["next_turn"]["turn_token"] == turn["turn_token"]
    assert [item["role"] for item in first_json["guided_session"]["chat_history"][-2:]] == ["user", "assistant"]
    assert _chat_operation_count(composer_test_client, session_id) == 1

    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("replay called provider")),
        raising=False,
    )
    monkeypatch.setattr(
        guided_chat_atomic,
        "_request_plugin_policy_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replay consulted mutable policy")),
    )
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert replay.status_code == 200, replay.json()
    assert replay.json() == first_json
    assert _chat_operation_count(composer_test_client, session_id) == 1


def test_chat_pair_binds_its_occurrence_and_retry_authority_is_occurrence_bound(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """elspeth-ea80e34fdc: retry authority is occurrence-bound, not content-based.

    The persisted user chat turn records the turn_token it was submitted
    under, the wire projection carries it to the frontend Retry affordance,
    a retry under the SAME still-current occurrence succeeds, and a retry
    under a superseded occurrence draws the ordinary stale-turn 409 without
    reaching the provider — historical prose can never ride the newest token.
    """
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(turn))

    assert first.status_code == 200, first.json()
    history = first.json()["guided_session"]["chat_history"]
    assert [item["role"] for item in history[-2:]] == ["user", "assistant"]
    assert history[-2]["turn_token"] == turn["turn_token"]
    assert history[-1]["turn_token"] is None

    # The reload projection (what a restored frontend renders Retry from)
    # carries the persisted occurrence verbatim.
    reloaded = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()
    assert reloaded["guided_session"]["chat_history"][-2]["turn_token"] == turn["turn_token"]

    # Same-occurrence retry: the advisory exchange left the turn unanswered,
    # so resending the RECORDED token as a fresh operation succeeds.
    retry = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(turn))
    assert retry.status_code == 200, retry.json()

    # Advance the occurrence through the wizard: the turn token rotates.
    schema_turn = _choose_source(composer_test_client, session_id, turn)["next_turn"]
    assert schema_turn["turn_token"] != turn["turn_token"]

    # Stale retry: the recorded token no longer identifies the current
    # unanswered turn — rejected before any provider call.
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("stale retry called provider")),
        raising=False,
    )
    stale = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(turn))
    assert stale.status_code == 409, stale.json()
    assert stale.json()["detail"] == "turn_token does not identify the current unanswered turn."


def test_reused_operation_id_with_different_message_conflicts_without_provider(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)
    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    assert first.status_code == 200, first.json()
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("conflict called provider")),
        raising=False,
    )

    conflict = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json={**body, "message": "Use JSON instead"},
    )

    assert conflict.status_code == 409, conflict.json()
    assert conflict.json()["detail"] == "Operation id is already bound to a different request."
    assert _chat_operation_count(composer_test_client, session_id) == 1


def test_schema8_chat_rejects_step3_without_current_turn_before_provider_or_reservation(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    _persist_guided(composer_test_client, session_id, GuidedSession(step=GuidedStep.STEP_3_TRANSFORMS))
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unsupported stage called provider")),
        raising=False,
    )

    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json={"operation_id": str(uuid4()), "turn_token": "a" * 64, "message": "Build transforms"},
    )

    assert response.status_code == 409, response.json()
    assert response.json()["detail"] == {
        "code": "guided_chat_stage_unsupported",
        "detail": "Schema-8 CHAT is not available for step_3_transforms.",
    }
    assert _chat_operation_count(composer_test_client, session_id) == 0


def test_chat_route_has_no_legacy_decoders_direct_writers_or_chain_solver() -> None:
    from elspeth.web.sessions.routes.composer import guided_chat_atomic

    source = inspect.getsource(guided_route.post_guided_chat)
    implementation = inspect.getsource(guided_chat_atomic.post_guided_chat_schema8)
    tree = ast.parse(source + "\n" + implementation)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "body.step_index" not in implementation
    assert "handle_step_1_source" not in names
    assert "handle_step_2_sink" not in names
    assert "solve_chain" not in names
    assert "save_composition_state" not in attributes
    assert "settle_guided_state_operation" in attributes


def test_expired_invalid_attempt_fails_preflight_without_attempt_bump_or_provider(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.sessions.guided_operations import guided_operation_request_hash
    from elspeth.web.sessions.protocol import GuidedOperationClaimed
    from elspeth.web.sessions.schemas import GuidedChatRequest

    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body({"turn_token": "0" * 64})
    request_model = GuidedChatRequest.model_validate(body, strict=True)
    service = composer_test_client.app.state.session_service
    claim = asyncio.run(
        service.reserve_guided_operation(
            session_id=UUID(session_id),
            operation_id=body["operation_id"],
            kind="guided_chat",
            request_hash=guided_operation_request_hash(
                session_id=UUID(session_id),
                kind="guided_chat",
                request=request_model,
            ),
            actor="composer_route",
            lease_seconds=300,
        )
    )
    assert isinstance(claim, GuidedOperationClaimed)
    with composer_test_client.app.state.session_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE guided_operations SET lease_expires_at = :expired WHERE session_id = :session_id AND operation_id = :operation_id"
            ),
            {
                "expired": datetime.now(UTC) - timedelta(seconds=1),
                "session_id": session_id,
                "operation_id": body["operation_id"],
            },
        )
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("invalid expired operation called provider")),
        raising=False,
    )

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert response.status_code == 409, response.json()
    assert response.json()["detail"] == "turn_token does not identify the current unanswered turn."
    assert body["turn_token"] != turn["turn_token"]
    with composer_test_client.app.state.session_engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == body["operation_id"],
                )
            )
            .mappings()
            .one()
        )
    assert operation["attempt"] == 1
    assert operation["status"] == "in_progress"


def test_schema_form_source_resolution_is_advisory_without_blob_mutation_and_replays_exactly(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    schema_turn = _choose_source(composer_test_client, session_id, initial_turn)["next_turn"]
    body = _chat_body(schema_turn)
    state_before = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["composition_state"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_source_provider, raising=False)
    reserve = AsyncMock(
        spec=composer_test_client.app.state.blob_service.reserve_inline_custody,
        side_effect=AssertionError("advisory Chat attempted blob custody"),
    )
    delete = AsyncMock(
        spec=composer_test_client.app.state.blob_service.delete_blob,
        side_effect=AssertionError("advisory Chat attempted blob deletion"),
    )
    monkeypatch.setattr(composer_test_client.app.state.blob_service, "reserve_inline_custody", reserve)
    monkeypatch.setattr(composer_test_client.app.state.blob_service, "delete_blob", delete)

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert first.status_code == 200, first.json()
    first_json = first.json()
    assert first_json["assistant_message"] == (
        "I did not apply generated source content. Review the current source form and submit it through the wizard controls."
    )
    assert first_json["assistant_message_kind"] == "synthetic_failure"
    assert first_json["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert first_json["next_turn"]["turn_token"] == schema_turn["turn_token"]
    assert first_json["next_turn"]["payload"] == schema_turn["payload"]
    for key in ("sources", "nodes", "edges", "outputs", "metadata"):
        assert first_json["composition_state"][key] == state_before[key]
    assert asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id))) == []
    reserve.assert_not_awaited()
    delete.assert_not_awaited()
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("replay called provider")),
        raising=False,
    )
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert replay.status_code == 200, replay.json()
    assert replay.json() == first_json
    assert asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id))) == []
    reserve.assert_not_awaited()
    delete.assert_not_awaited()


def test_schema_form_uploaded_source_type_mismatch_is_acknowledged_without_provider(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    schema_turn = _choose_source(composer_test_client, session_id, initial_turn, plugin="text")["next_turn"]
    uploaded = asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "MOCK_DATA.json",
            b'[{"name":"alice","value":1}]\n',
            "application/json",
            created_by="user",
        )
    )

    async def provider_must_not_run(**_kwargs: object) -> GuidedChatProviderOutcome:
        raise AssertionError("uploaded source mismatch called provider")

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", provider_must_not_run, raising=False)

    request_body = _chat_body(
        schema_turn,
        message='I\'ve uploaded "MOCK_DATA.json"; please use it as the pipeline input.',
    )
    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=request_body)

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert 'I received "MOCK_DATA.json"' in body["assistant_message"]
    assert "JSON" in body["assistant_message"]
    assert "Text" in body["assistant_message"]
    assert "still uploaded" in body["assistant_message"]
    assert body["next_turn"]["turn_token"] == schema_turn["turn_token"]
    assert body["next_turn"]["payload"] == schema_turn["payload"]
    blobs = asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert [blob.id for blob in blobs] == [uploaded.id]
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=request_body)
    assert replay.status_code == 200, replay.json()
    assert replay.json() == body
    assert _chat_operation_count(composer_test_client, session_id) == 1


def test_matching_uploaded_source_missing_required_failure_policy_raises(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    uploaded = asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "orders.csv",
            b"order_id,total\n1,10\n",
            "text/csv",
            created_by="user",
        )
    )

    def missing_policy_prefill(_plugin: str, *, inspection_facts: object | None = None) -> dict[str, object]:
        assert inspection_facts is not None
        return {
            "path": f"blob:{uploaded.id}",
            "schema": {"mode": "observed"},
        }

    monkeypatch.setattr(guided_route, "build_step_1_source_prefill", missing_policy_prefill)

    with pytest.raises(InvariantError, match="source prefill is missing required on_validation_failure"):
        asyncio.run(
            guided_route._source_from_latest_uploaded_blob_for_step_1_chat(
                message='I\'ve uploaded "orders.csv"; please use it as the pipeline input.',
                plugin_hint="csv",
                blob_service=composer_test_client.app.state.blob_service,
                session_id=UUID(session_id),
            )
        )


def test_matching_uploaded_source_missing_required_path_raises(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "orders.csv",
            b"order_id,total\n1,10\n",
            "text/csv",
            created_by="user",
        )
    )

    def missing_path_prefill(_plugin: str, *, inspection_facts: object | None = None) -> dict[str, object]:
        assert inspection_facts is not None
        return {
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }

    monkeypatch.setattr(guided_route, "build_step_1_source_prefill", missing_path_prefill)

    with pytest.raises(InvariantError, match="matching source prefill is missing required path"):
        asyncio.run(
            guided_route._source_from_latest_uploaded_blob_for_step_1_chat(
                message='I\'ve uploaded "orders.csv"; please use it as the pipeline input.',
                plugin_hint="csv",
                blob_service=composer_test_client.app.state.blob_service,
                session_id=UUID(session_id),
            )
        )


@pytest.mark.parametrize("malformed_policy", [None, 0, ""])
def test_matching_uploaded_source_malformed_failure_policy_raises(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    malformed_policy: object,
) -> None:
    session_id = _create_session(composer_test_client)
    uploaded = asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "orders.csv",
            b"order_id,total\n1,10\n",
            "text/csv",
            created_by="user",
        )
    )

    def malformed_policy_prefill(_plugin: str, *, inspection_facts: object | None = None) -> dict[str, object]:
        assert inspection_facts is not None
        return {
            "path": f"blob:{uploaded.id}",
            "schema": {"mode": "observed"},
            "on_validation_failure": malformed_policy,
        }

    monkeypatch.setattr(guided_route, "build_step_1_source_prefill", malformed_policy_prefill)

    with pytest.raises(InvariantError, match="source prefill on_validation_failure must be a non-empty exact str"):
        asyncio.run(
            guided_route._source_from_latest_uploaded_blob_for_step_1_chat(
                message='I\'ve uploaded "orders.csv"; please use it as the pipeline input.',
                plugin_hint="csv",
                blob_service=composer_test_client.app.state.blob_service,
                session_id=UUID(session_id),
            )
        )


@pytest.mark.parametrize(
    ("prefill", "expected_field"),
    [
        (
            {
                "path": None,
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            },
            "path",
        ),
        (
            {
                "path": 0,
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            },
            "path",
        ),
        (
            {
                "path": "",
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            },
            "path",
        ),
        (
            {
                "path": "blob:authoritative",
                "on_validation_failure": "discard",
            },
            "schema",
        ),
        (
            {
                "path": "blob:authoritative",
                "schema": None,
                "on_validation_failure": "discard",
            },
            "schema",
        ),
    ],
)
def test_matching_uploaded_source_malformed_prefill_contract_raises(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    prefill: dict[str, object],
    expected_field: str,
) -> None:
    session_id = _create_session(composer_test_client)
    asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "orders.csv",
            b"order_id,total\n1,10\n",
            "text/csv",
            created_by="user",
        )
    )

    def malformed_prefill(_plugin: str, *, inspection_facts: object | None = None) -> dict[str, object]:
        assert inspection_facts is not None
        return dict(prefill)

    monkeypatch.setattr(guided_route, "build_step_1_source_prefill", malformed_prefill)

    with pytest.raises(InvariantError, match=expected_field):
        asyncio.run(
            guided_route._source_from_latest_uploaded_blob_for_step_1_chat(
                message='I\'ve uploaded "orders.csv"; please use it as the pipeline input.',
                plugin_hint="csv",
                blob_service=composer_test_client.app.state.blob_service,
                session_id=UUID(session_id),
            )
        )


def test_schema_form_source_plugin_reselection_rebuilds_form_and_preserves_ready_upload(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    chosen = _choose_source(composer_test_client, session_id, initial_turn, plugin="text")
    schema_turn = chosen["next_turn"]
    record_before = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record_before is not None
    pending_before = record_before.composer_meta["guided_session"]["pending_source_intents"]
    assert len(pending_before) == 1
    stable_id = next(iter(pending_before))
    uploaded = asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "MOCK_DATA.json",
            b'[{"name":"alice","value":1}]\n',
            "application/json",
            created_by="user",
        )
    )
    newer_mismatched_upload = asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "NEWER_DATA.csv",
            b"name,value\nbob,2\n",
            "text/csv",
            created_by="user",
        )
    )
    provider_calls = 0

    async def provider(**kwargs: object) -> GuidedChatProviderOutcome:
        nonlocal provider_calls
        provider_calls += 1
        return await _reselected_json_source_provider(**kwargs)

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", provider, raising=False)
    request_body = _chat_body(
        schema_turn,
        message="This uploaded source is JSON, not text. Change the source type and keep the file.",
    )

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=request_body)

    assert first.status_code == 200, first.json()
    body = first.json()
    assert body["assistant_message_kind"] == "assistant"
    assert body["assistant_message"] == "I changed the source type to JSON and kept the uploaded file ready."
    next_turn = body["next_turn"]
    assert next_turn["type"] == "schema_form"
    assert next_turn["turn_token"] != schema_turn["turn_token"]
    assert next_turn["payload"]["plugin"] == "json"
    assert next_turn["payload"]["prefilled"]["path"] == f"blob:{uploaded.id}"
    record_after = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record_after is not None
    pending_after = record_after.composer_meta["guided_session"]["pending_source_intents"]
    assert list(pending_after) == [stable_id]
    assert pending_after[stable_id]["phase"] == "plugin_options"
    assert pending_after[stable_id]["plugin"] == "json"
    assert pending_after[stable_id]["inspection_facts"]["redacted_identity"]["blob_id"] == str(uploaded.id)
    assert body["guided_session"]["history"][-2]["response_hash"] is not None
    assert body["guided_session"]["history"][-2]["summary"] == "Pending source plugin reselected through guided chat."
    assert body["guided_session"]["history"][-1]["response_hash"] is None
    blobs = asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert [blob.id for blob in blobs] == [newer_mismatched_upload.id, uploaded.id]
    assert provider_calls == 1

    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("replay called provider")),
        raising=False,
    )
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=request_body)
    assert replay.status_code == 200, replay.json()
    assert replay.json() == body
    assert provider_calls == 1


def test_same_operation_concurrent_callers_join_one_provider_result_outside_compose_lock(
    file_composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = file_composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    initial_versions = asyncio.run(client.app.state.session_service.get_state_versions(UUID(session_id)))
    provider_calls = 0

    async def race() -> list[object]:
        nonlocal provider_calls
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()

        async def controlled_provider(**kwargs: object) -> GuidedChatProviderOutcome:
            nonlocal provider_calls
            provider_calls += 1
            compose_lock = await client.app.state.session_compose_lock_registry.get_lock(str(kwargs["session_id"]))
            assert not compose_lock.locked(), "provider work must run outside the per-session compose lock"
            provider_started.set()
            await release_provider.wait()
            return await _advisory_provider()

        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", controlled_provider, raising=False)
        async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as async_client:
            winner = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/chat", json=body))
            await asyncio.wait_for(provider_started.wait(), timeout=3)
            joiner = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/chat", json=body))
            await asyncio.sleep(0)
            release_provider.set()
            return list(await asyncio.wait_for(asyncio.gather(winner, joiner), timeout=5))

    winner_response, joined_response = asyncio.run(race())

    assert winner_response.status_code == joined_response.status_code == 200
    assert winner_response.json() == joined_response.json()
    assert provider_calls == 1
    assert _chat_operation_count(client, session_id) == 1
    assert len(asyncio.run(client.app.state.session_service.get_state_versions(UUID(session_id)))) == len(initial_versions) + 1


def test_provider_failure_persists_one_redacted_llm_audit_with_terminal_failure(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    marker = "chat-provider-failure"

    async def failed_provider(**kwargs: object) -> GuidedChatProviderOutcome:
        _record_test_llm_call(
            kwargs["recorder"],
            marker=marker,
            status=ComposerLLMCallStatus.API_ERROR,
        )
        raise RuntimeError(f"secret-{marker}")

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", failed_provider, raising=False)
    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert first.status_code == replay.status_code == 500
    assert first.json() == replay.json()
    assert first.json()["detail"]["failure_code"] == "operation_failed"
    assert f"secret-{marker}" not in first.text
    _assert_one_llm_failure_audit(composer_test_client, session_id, marker=marker)


def test_post_provider_transition_rejection_persists_one_llm_audit(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    marker = "chat-transition-rejection"

    async def audited_provider(**kwargs: object) -> GuidedChatProviderOutcome:
        _record_test_llm_call(kwargs["recorder"], marker=marker)
        return await _resolved_source_provider()

    def reject_transition(*_args: object, **_kwargs: object) -> None:
        raise AuditIntegrityError("injected transition rejection")

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", audited_provider, raising=False)
    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", reject_transition)
    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(turn),
    )

    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    _assert_one_llm_failure_audit(composer_test_client, session_id, marker=marker)


def test_non_admission_http_exception_fails_closed_instead_of_settling_as_admission_rejection(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 stage guard transiting the shared transition helper is not a sink
    admission rejection: it must fail the operation closed, never settle as a
    token-consuming 200 labelled SinkAdmissionRejected."""
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    marker = "chat-non-admission-http"

    async def audited_provider(**kwargs: object) -> GuidedChatProviderOutcome:
        _record_test_llm_call(kwargs["recorder"], marker=marker)
        return await _resolved_source_provider()

    def reject_transition(*_args: object, **_kwargs: object) -> None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "guided_respond_stage_unsupported",
                "detail": "Schema-8 RESPOND is not available for step_3_transforms.",
            },
        )

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", audited_provider, raising=False)
    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", reject_transition)
    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(turn),
    )

    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "operation_failed"
    _assert_one_llm_failure_audit(composer_test_client, session_id, marker=marker)


def test_sink_admission_rejection_settles_as_degraded_turn_with_reason(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed sink-admission rejection still settles as the safe not-applied
    200 with the admission reason in the assistant message."""
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    reason = "Output option 'path': '/etc/passwd' is outside this deployment's allowed output locations."

    def reject_transition(*_args: object, **_kwargs: object) -> None:
        raise guided_route.SinkAdmissionRejectedError(reason)

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_source_provider, raising=False)
    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", reject_transition)
    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(turn),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert reason in body["assistant_message"]
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert body["next_turn"]["turn_token"] == turn["turn_token"]


def test_settlement_failure_rolls_back_chat_state_but_persists_failure_evidence_and_replays_typed_failure(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    service = composer_test_client.app.state.session_service
    initial_versions = asyncio.run(service.get_state_versions(UUID(session_id)))
    initial_messages = asyncio.run(service.get_messages(UUID(session_id), limit=None))
    secret_canary = "/private/operator/chat-settlement-secret.csv"
    marker = "chat-settlement-failure"

    async def audited_provider(**kwargs: object) -> GuidedChatProviderOutcome:
        _record_test_llm_call(kwargs["recorder"], marker=marker)
        return await _advisory_provider()

    async def fail_settlement(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(secret_canary)

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", audited_provider, raising=False)
    monkeypatch.setattr(service, "settle_guided_state_operation", fail_settlement)
    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("terminal replay called provider")),
        raising=False,
    )
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert first.status_code == replay.status_code == 500
    assert replay.json() == first.json()
    assert first.json()["detail"]["failure_code"] == "operation_failed"
    assert secret_canary not in first.text
    assert asyncio.run(service.get_state_versions(UUID(session_id))) == initial_versions
    # The failed success-settlement had already buffered both the provider
    # call and the completed chat-turn projection. Both belong to the closed
    # failure cohort; neither state nor the user/assistant message pair does.
    assert len(asyncio.run(service.get_messages(UUID(session_id), limit=None))) == len(initial_messages) + 2
    with composer_test_client.app.state.session_engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == body["operation_id"],
                )
            )
            .mappings()
            .one()
        )
    assert operation["status"] == "failed"
    assert operation["result_state_id"] is None
    assert operation["response_hash"] is None
    assert secret_canary not in str(dict(operation))
    _assert_one_llm_failure_audit(composer_test_client, session_id, marker=marker)


def test_provider_head_drift_fails_closed_without_settling_chat(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    service = composer_test_client.app.state.session_service
    initial_versions = asyncio.run(service.get_state_versions(UUID(session_id)))
    initial_messages = asyncio.run(service.get_messages(UUID(session_id), limit=None))
    marker = "chat-stale-head"

    async def drifting_provider(**kwargs: object) -> GuidedChatProviderOutcome:
        _record_test_llm_call(kwargs["recorder"], marker=marker)
        state = kwargs["state"]
        state_dict = state.to_dict()  # type: ignore[union-attr]
        await service.save_composition_state(
            UUID(session_id),
            CompositionStateData(
                sources=state_dict["sources"],
                nodes=state_dict["nodes"],
                edges=state_dict["edges"],
                outputs=state_dict["outputs"],
                metadata_=state_dict["metadata"],
                is_valid=False,
                composer_meta={"guided_session": state.guided_session.to_dict()},  # type: ignore[union-attr]
            ),
            provenance="session_seed",
        )
        return await _advisory_provider()

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", drifting_provider, raising=False)
    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert response.status_code == replay.status_code == 409
    assert response.json() == replay.json()
    assert response.json()["detail"]["failure_code"] == "stale_conflict"
    assert len(asyncio.run(service.get_state_versions(UUID(session_id)))) == len(initial_versions) + 1
    assert len(asyncio.run(service.get_messages(UUID(session_id), limit=None))) == len(initial_messages) + 1
    _assert_one_llm_failure_audit(composer_test_client, session_id, marker=marker)


def test_exact_replay_fails_closed_when_current_turn_payload_is_tampered(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    initial_versions = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)
    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    assert first.status_code == 200
    store = composer_test_client.app.state.payload_store

    monkeypatch.setattr(type(store), "retrieve", lambda _self, _content_hash: b"{}")
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("tampered replay called provider")),
        raising=False,
    )

    with pytest.raises(AuditIntegrityError, match="bytes do not match"):
        composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert _chat_operation_count(composer_test_client, session_id) == 1
    assert (
        len(asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))) == len(initial_versions) + 1
    )


def test_expired_operation_takeover_fences_stale_worker_and_both_join_winner(
    file_composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = file_composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _chat_body(turn)
    service = client.app.state.session_service
    initial_versions = asyncio.run(service.get_state_versions(UUID(session_id)))
    engine = client.app.state.session_engine
    provider_calls = 0

    async def race() -> list[object]:
        nonlocal provider_calls
        stale_provider_started = asyncio.Event()
        release_stale_provider = asyncio.Event()
        takeover_provider_started = asyncio.Event()

        async def controlled_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                stale_provider_started.set()
                await release_stale_provider.wait()
            else:
                takeover_provider_started.set()
            return await _advisory_provider()

        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", controlled_provider, raising=False)
        async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as async_client:
            stale = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/chat", json=body))
            await asyncio.wait_for(stale_provider_started.wait(), timeout=3)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE guided_operations SET lease_expires_at = :expired "
                        "WHERE session_id = :session_id AND operation_id = :operation_id"
                    ),
                    {
                        "expired": datetime.now(UTC) - timedelta(seconds=1),
                        "session_id": session_id,
                        "operation_id": body["operation_id"],
                    },
                )
            winner = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/chat", json=body))
            await asyncio.wait_for(takeover_provider_started.wait(), timeout=3)
            winner_response = await asyncio.wait_for(winner, timeout=3)
            release_stale_provider.set()
            stale_response = await asyncio.wait_for(stale, timeout=3)
            return [stale_response, winner_response]

    stale_response, winner_response = asyncio.run(race())

    assert stale_response.status_code == winner_response.status_code == 200
    assert stale_response.json() == winner_response.json()
    assert provider_calls == 2
    with engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == body["operation_id"],
                )
            )
            .mappings()
            .one()
        )
    assert operation["status"] == "completed"
    assert operation["attempt"] == 2
    assert len(asyncio.run(service.get_state_versions(UUID(session_id)))) == len(initial_versions) + 1


def test_single_select_inline_source_resolution_materializes_blob_and_prefills_schema_form(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline resolve_source content with no uploaded blob becomes a session blob.

    The plugin-selection transition must carry inspection facts derived from
    the materialized bytes so the next turn is a blob-backed, continuable
    schema form instead of the bare ``options: null`` stall.
    """
    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    assert initial_turn["type"] == "single_select"
    body = _chat_body(initial_turn, message="The rows are name,value pairs; create the source inline.")
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_source_provider, raising=False)

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=body)

    assert first.status_code == 200, first.json()
    first_json = first.json()
    assert first_json["assistant_message"] == "I prepared the CSV source."
    assert first_json["assistant_message_kind"] == "assistant"

    blobs = asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert len(blobs) == 1
    blob = blobs[0]
    assert blob.filename == "source.csv"
    assert blob.mime_type == "text/csv"
    assert blob.created_by == "assistant"
    assert blob.status == "ready"
    content = asyncio.run(composer_test_client.app.state.blob_service.read_blob_content(blob.id))
    assert content == b"name,value\nalice,1\n"

    next_turn = first_json["next_turn"]
    assert next_turn["type"] == "schema_form"
    prefilled = next_turn["payload"]["prefilled"]
    assert prefilled["path"] == f"blob:{blob.id}"
    assert prefilled["on_validation_failure"] == "discard"
    assert prefilled["schema"]["mode"] in {"flexible", "observed"}

    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    persisted_guided = record.composer_meta["guided_session"]
    intent = next(iter(persisted_guided["pending_source_intents"].values()))
    assert intent["phase"] == "plugin_options"
    assert intent["plugin"] == "csv"
    assert intent["inspection_facts"] is not None


def test_inline_source_walks_prefilled_form_through_inspection_to_resolved_source(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The materialized inline blob drives the wizard to a reviewed source."""
    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_source_provider, raising=False)
    chat = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(initial_turn))
    assert chat.status_code == 200, chat.json()
    schema_turn = chat.json()["next_turn"]
    assert schema_turn["type"] == "schema_form"
    prefilled = schema_turn["payload"]["prefilled"]
    assert prefilled["path"].startswith("blob:")

    form = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": schema_turn["turn_token"],
            "edited_values": {
                "plugin": "csv",
                "options": {
                    "path": prefilled["path"],
                    "schema": prefilled["schema"],
                    "on_validation_failure": prefilled["on_validation_failure"],
                },
            },
        },
    )
    assert form.status_code == 200, form.json()
    inspect_turn = form.json()["next_turn"]
    assert inspect_turn["type"] == "inspect_and_confirm"

    confirm = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": inspect_turn["turn_token"],
            "edited_values": {"columns": ["name", "value"]},
        },
    )
    assert confirm.status_code == 200, confirm.json()

    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    persisted_guided = record.composer_meta["guided_session"]
    assert persisted_guided["pending_source_intents"] == {}
    source = next(iter(persisted_guided["reviewed_sources"].values()))
    assert source["plugin"] == "csv"
    assert tuple(source["observed_columns"]) == ("name", "value")
    assert source["options"]["path"] == prefilled["path"]


async def _resolved_sink_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
    sink = SinkResolved(
        outputs=(
            SinkOutputResolved(
                name="result",
                plugin="json",
                # collision_policy/mode are part of the solver's file-sink
                # contract and of the chat-time deployment admission the
                # prefill lane now runs before staging.
                options={
                    "path": "out.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "fail_if_exists",
                },
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        )
    )
    return Step2SinkResolvedResult(
        chat=StepChatResult(
            assistant_message="I set up the JSON sink.",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=1,
            error_class=None,
        ),
        sink=sink,
        deferred_actions=(),
    )


def test_sink_resolution_prefills_schema_form_from_chat_options(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sink resolution's options must survive plugin selection as prefill.

    One chat message per stage is the tutorial contract: after the resolution
    answers the sink single_select, the schema form must render with the
    resolution's options (path included) so the wizard's Continue is live —
    not the bare ``path: Not set`` stall.
    """
    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_source_provider, raising=False)
    chat = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(initial_turn))
    schema_turn = chat.json()["next_turn"]
    prefilled = schema_turn["payload"]["prefilled"]
    form = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": schema_turn["turn_token"],
            "edited_values": {
                "plugin": "csv",
                "options": {
                    "path": prefilled["path"],
                    "schema": prefilled["schema"],
                    "on_validation_failure": prefilled["on_validation_failure"],
                },
            },
        },
    )
    inspect_turn = form.json()["next_turn"]
    confirm = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": inspect_turn["turn_token"],
            "edited_values": {"columns": ["name", "value"]},
        },
    )
    review_turn = confirm.json()["next_turn"]
    assert review_turn["type"] == "review_components"
    finish = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": review_turn["turn_token"],
            "component_action": {"action": "finish", "component_kind": "source"},
        },
    )
    assert finish.status_code == 200, finish.json()
    sink_select_turn = finish.json()["next_turn"]
    assert sink_select_turn["type"] == "single_select"

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_sink_provider, raising=False)
    sink_chat = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(sink_select_turn, message="Save the results to a JSON file."),
    )

    assert sink_chat.status_code == 200, sink_chat.json()
    sink_form_turn = sink_chat.json()["next_turn"]
    assert sink_form_turn["type"] == "schema_form"
    sink_prefilled = sink_form_turn["payload"]["prefilled"]
    assert sink_prefilled["path"] == "out.json"
    assert sink_prefilled["schema"]["mode"] == "observed"
    assert sink_prefilled["on_write_failure"] == "discard"
    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    persisted_guided = record.composer_meta["guided_session"]
    assert next(iter(persisted_guided["pending_output_intents"].values()))["name"] == "result"


def test_invalid_sink_prefill_never_reaches_operator_logs(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    session_id = _create_session(composer_test_client)
    sink_state = _Step2Journey()._drive_to_step_2_single_select(composer_test_client, session_id)
    sink_turn = sink_state["next_turn"]
    canary = "raw-model-config-canary-7f3a9d"

    async def invalid_sink_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
        sink = SinkResolved(
            outputs=(
                SinkOutputResolved(
                    name="result",
                    plugin="json",
                    options={
                        "path": "out.json",
                        "schema": {"mode": "observed"},
                        canary: "untrusted",
                    },
                    required_fields=(),
                    schema_mode="observed",
                    on_write_failure="discard",
                ),
            )
        )
        return Step2SinkResolvedResult(
            chat=StepChatResult(
                assistant_message="I set up the JSON sink.",
                status=ComposerChatTurnStatus.SUCCESS,
                latency_ms=1,
                error_class=None,
            ),
            sink=sink,
            deferred_actions=(),
        )

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", invalid_sink_provider, raising=False)
    with capture_logs() as logs:
        response = composer_test_client.post(
            f"/api/sessions/{session_id}/guided/chat",
            json=_chat_body(sink_turn, message="Save the results."),
        )

    assert response.status_code == 200, response.json()
    rejection = next(entry for entry in logs if entry["event"] == "guided.step_2_sink_prefill_config_rejected")
    assert rejection["rejection_code"] == "invalid_sink_configuration"
    assert rejection["exc_class"] == "PluginConfigError"
    assert "plugin" not in rejection
    assert "error_detail" not in rejection
    assert canary not in repr(logs)
    # inv-f1 D4 / incidental 2: the rejected prefill was deliberately NOT
    # applied — the wire reason must say so, not blame provider availability.
    body = response.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"


def test_inadmissible_sink_prefill_degrades_at_chat_time_instead_of_staging(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat-authored SINGLE_SELECT sink prefill runs the same deployment
    admission as schema-form answers (elspeth-ef92db3e16 prefill lane): an
    out-of-allowlist output path degrades at chat time with the admission
    reason, instead of staging as server-held form prefill and surfacing
    later at the user's form POST as a 400 blaming their submission.
    """
    from structlog.testing import capture_logs

    session_id = _create_session(composer_test_client)
    sink_state = _Step2Journey()._drive_to_step_2_single_select(composer_test_client, session_id)
    sink_turn = sink_state["next_turn"]
    disallowed = {
        "path": "/etc/elspeth-prefill-admission-canary.json",
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "fail_if_exists",
    }
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _sink_form_answer_provider(disallowed),
        raising=False,
    )
    with capture_logs() as logs:
        response = composer_test_client.post(
            f"/api/sessions/{session_id}/guided/chat",
            json=_chat_body(sink_turn, message="Save the results to a JSON file."),
        )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert "allowed output locations" in body["assistant_message"]
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    # Nothing was staged: the plugin-selection turn is re-presented under the
    # same token and no output intent advanced to its options phase.
    assert body["next_turn"]["type"] == "single_select"
    assert body["next_turn"]["turn_token"] == sink_turn["turn_token"]
    rejection = next(entry for entry in logs if entry["event"] == "guided.step_2_sink_prefill_admission_rejected")
    assert "allowed output locations" in rejection["detail"]
    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    guided_after = record.composer_meta["guided_session"]
    assert all(intent["phase"] == "plugin_selection" for intent in guided_after["pending_output_intents"].values())
    assert guided_after["reviewed_outputs"] == {}


def test_underspecified_sink_prefill_degrades_at_chat_time_instead_of_staging(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file-sink prefill missing an explicit collision policy is rejected by
    the same chat-time admission as the out-of-allowlist path, so the solver's
    collision_policy/mode contract is backstopped for every other producer of
    a sink resolution.
    """
    session_id = _create_session(composer_test_client)
    sink_state = _Step2Journey()._drive_to_step_2_single_select(composer_test_client, session_id)
    sink_turn = sink_state["next_turn"]
    underspecified = {
        "path": "results.json",
        "schema": {"mode": "observed"},
    }
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _sink_form_answer_provider(underspecified),
        raising=False,
    )
    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(sink_turn, message="Save the results to a JSON file."),
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert body["next_turn"]["type"] == "single_select"
    assert body["next_turn"]["turn_token"] == sink_turn["turn_token"]


def _sink_form_answer_provider(options: dict[str, object]):
    """Provider whose resolution answers the sink schema form with ``options``."""

    async def provider(**_kwargs: object) -> GuidedChatProviderOutcome:
        sink = SinkResolved(
            outputs=(
                SinkOutputResolved(
                    name="result",
                    plugin="json",
                    options=options,
                    required_fields=(),
                    schema_mode="observed",
                    on_write_failure="discard",
                ),
            )
        )
        return Step2SinkResolvedResult(
            chat=StepChatResult(
                assistant_message="I filled in the JSON sink settings.",
                status=ComposerChatTurnStatus.SUCCESS,
                latency_ms=1,
                error_class=None,
            ),
            sink=sink,
            deferred_actions=(),
        )

    return provider


def _drive_chat_to_sink_schema_form(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object] | Callable[[str, Path], dict[str, object]],
) -> tuple[str, dict]:
    """Create a session, reach the Step-2 sink schema form via chat.

    The plugin-selection step stages an admissible prefill — the SINGLE_SELECT
    lane now runs the deployment admission before staging, so an inadmissible
    ``options`` set can only be exercised as the *form answer*, which is what
    the provider is swapped to before returning. Session-dependent option
    sets (own-session and cross-session path vectors) pass a callable that
    receives the created session id and the deployment data_dir.
    """
    session_id = _create_session(client)
    if callable(options):
        options = options(session_id, Path(client.app.state.settings.data_dir))
    sink_state = _Step2Journey()._drive_to_step_2_single_select(client, session_id)
    sink_turn = sink_state["next_turn"]
    admissible_prefill: dict[str, object] = {
        "path": "results.json",
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "fail_if_exists",
    }
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _sink_form_answer_provider(admissible_prefill),
        raising=False,
    )
    select_chat = client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(sink_turn, message="Save the results to a JSON file."),
    )
    assert select_chat.status_code == 200, select_chat.json()
    form_turn = select_chat.json()["next_turn"]
    assert form_turn["type"] == "schema_form"
    monkeypatch.setattr(
        guided_route,
        "_run_guided_chat_provider_attempt",
        _sink_form_answer_provider(options),
        raising=False,
    )
    return session_id, form_turn


def test_chat_sink_form_answer_rejects_path_outside_allowed_directories(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat-driven sink form answers pass the manual form's deployment
    admission (elspeth-ef92db3e16): an LLM-authored absolute output path
    outside the deployment's allowed sink directories must degrade to a
    safe not-applied response instead of entering reviewed authority.
    """
    disallowed = {
        "path": "/etc/elspeth-admission-canary.json",
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "fail_if_exists",
    }
    session_id, form_turn = _drive_chat_to_sink_schema_form(composer_test_client, monkeypatch, disallowed)

    form_chat = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form_turn, message="Yes, apply those settings."),
    )

    assert form_chat.status_code == 200, form_chat.json()
    body = form_chat.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    # The rejected options were NOT applied: the form turn is re-presented,
    # the intent never advances past its form phase, and nothing reaches
    # reviewed authority. (The disallowed path appears only in the degrade
    # message's verbatim explanation — the staged prefill is admissible by
    # construction, since the SINGLE_SELECT lane now admission-gates it.)
    assert body["next_turn"]["type"] == "schema_form"
    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    guided_after = record.composer_meta["guided_session"]
    intent = next(iter(guided_after["pending_output_intents"].values()))
    assert intent["phase"] == "plugin_options"
    assert guided_after["reviewed_outputs"] == {}


def test_chat_sink_form_answer_requires_explicit_collision_policy(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity with the manual form: a file-sink answer missing an explicit
    collision policy is rejected at the same admission boundary, so an
    under-specified LLM option set cannot become a reviewed fact that
    planning later dies on (elspeth-ef92db3e16).
    """
    underspecified = {
        "path": "results.json",
        "schema": {"mode": "observed"},
    }
    session_id, form_turn = _drive_chat_to_sink_schema_form(composer_test_client, monkeypatch, underspecified)

    form_chat = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form_turn, message="Yes, apply those settings."),
    )

    assert form_chat.status_code == 200, form_chat.json()
    body = form_chat.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert body["next_turn"]["type"] == "schema_form"
    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    guided_after = record.composer_meta["guided_session"]
    intent = next(iter(guided_after["pending_output_intents"].values()))
    assert intent["phase"] == "plugin_options"
    assert guided_after["reviewed_outputs"] == {}


_FOREIGN_SESSION_ID = "99999999-9999-4999-8999-999999999999"


def _traversal_sink_options(session_id: str, data_dir: Path) -> dict[str, object]:
    return {
        "path": "reports/../../../escape.jsonl",
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "fail_if_exists",
    }


def _foreign_outputs_sink_options(session_id: str, data_dir: Path) -> dict[str, object]:
    # Explicitly session-scoped FOREIGN relative path: resolution must keep it
    # foreign (never adopt it under the caller's directory) so the allowlist
    # rejects it.
    return {
        "path": f"outputs/{_FOREIGN_SESSION_ID}/hijack.jsonl",
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "fail_if_exists",
    }


def _foreign_blob_sink_options(session_id: str, data_dir: Path) -> dict[str, object]:
    return {
        "path": str(data_dir / "blobs" / _FOREIGN_SESSION_ID / "hijack.jsonl"),
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "fail_if_exists",
    }


@pytest.mark.parametrize(
    "vector",
    [_traversal_sink_options, _foreign_outputs_sink_options, _foreign_blob_sink_options],
    ids=["dot-dot-traversal", "cross-session-outputs-relative", "cross-session-blob-absolute"],
)
def test_chat_sink_form_answer_rejects_traversal_and_cross_session_paths(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    vector: Callable[[str, Path], dict[str, object]],
) -> None:
    """Deployment sink admission on the CHAT lane (elspeth-ef92db3e16):
    traversal and cross-session paths in an LLM-authored form answer must
    degrade to a safe not-applied response instead of entering reviewed
    authority."""
    session_id, form_turn = _drive_chat_to_sink_schema_form(composer_test_client, monkeypatch, vector)

    form_chat = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form_turn, message="Yes, apply those settings."),
    )

    assert form_chat.status_code == 200, form_chat.json()
    body = form_chat.json()
    assert body["assistant_message_kind"] == "synthetic_failure"
    assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert body["next_turn"]["type"] == "schema_form"
    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    guided_after = record.composer_meta["guided_session"]
    intent = next(iter(guided_after["pending_output_intents"].values()))
    assert intent["phase"] == "plugin_options"
    assert guided_after["reviewed_outputs"] == {}


def test_chat_sink_form_answer_admits_own_session_blob_path(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's own ``blobs/<session_id>`` subtree is inside
    ``allowed_sink_directories``: the chat lane must ADMIT it — the
    over-rejection guard for elspeth-ef92db3e16's admission move."""

    def own_blob_options(session_id: str, data_dir: Path) -> dict[str, object]:
        blob_dir = data_dir / "blobs" / session_id
        blob_dir.mkdir(parents=True, exist_ok=True)
        return {
            "path": str(blob_dir / "derived.jsonl"),
            "schema": {"mode": "observed"},
            "mode": "write",
            "collision_policy": "auto_increment",
        }

    session_id, form_turn = _drive_chat_to_sink_schema_form(composer_test_client, monkeypatch, own_blob_options)

    form_chat = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/chat",
        json=_chat_body(form_turn, message="Yes, apply those settings."),
    )

    assert form_chat.status_code == 200, form_chat.json()
    body = form_chat.json()
    assert body["assistant_message_kind"] != "synthetic_failure"
    assert body["next_turn"]["type"] == "multi_select_with_custom"
    record = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    guided_after = record.composer_meta["guided_session"]
    intent = next(iter(guided_after["pending_output_intents"].values()))
    assert intent["options"] is not None
    assert intent["options"]["path"].endswith("derived.jsonl")


def test_inline_source_defers_to_existing_ready_uploaded_blob(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uploaded blob stays authoritative; inline content is not stored."""
    session_id = _create_session(composer_test_client)
    uploaded = asyncio.run(
        composer_test_client.app.state.blob_service.create_blob(
            UUID(session_id),
            "uploaded.csv",
            b"name,value\nuploaded,9\n",
            "text/csv",
            created_by="user",
        )
    )
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_source_provider, raising=False)

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(initial_turn))

    assert first.status_code == 200, first.json()
    blobs = asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id)))
    assert [blob.id for blob in blobs] == [uploaded.id]
    next_turn = first.json()["next_turn"]
    assert next_turn["type"] == "schema_form"
    assert next_turn["payload"]["prefilled"]["path"] == f"blob:{uploaded.id}"


def test_inline_source_unencodable_content_settles_as_advisory_without_blob(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lone-surrogate content from the provider must not 500 the turn."""
    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    async def surrogate_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
        resolution = replace(_source_resolution(), content="name,value\n\ud800,1\n")
        return Step1SourceResolvedResult(
            chat=StepChatResult(
                assistant_message=resolution.assistant_message,
                status=ComposerChatTurnStatus.SUCCESS,
                latency_ms=1,
                error_class=None,
            ),
            resolution=resolution,
            deferred_actions=(),
        )

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", surrogate_provider, raising=False)

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(initial_turn))

    assert first.status_code == 200, first.json()
    first_json = first.json()
    assert first_json["assistant_message_kind"] == "synthetic_failure"
    assert first_json["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert first_json["next_turn"]["turn_token"] == initial_turn["turn_token"]
    assert asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id))) == []


def test_inline_source_quota_failure_settles_as_advisory_without_blob(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quota-rejected inline source must not pretend it was applied."""
    from elspeth.web.blobs.service import BlobQuotaExceededError

    session_id = _create_session(composer_test_client)
    initial_turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _resolved_source_provider, raising=False)
    quota = AsyncMock(
        spec=composer_test_client.app.state.blob_service.create_blob,
        side_effect=BlobQuotaExceededError(session_id, current_bytes=10, limit_bytes=10),
    )
    monkeypatch.setattr(composer_test_client.app.state.blob_service, "create_blob", quota)

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/chat", json=_chat_body(initial_turn))

    assert first.status_code == 200, first.json()
    first_json = first.json()
    assert first_json["assistant_message_kind"] == "synthetic_failure"
    assert first_json["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "not_applied"
    assert first_json["next_turn"]["turn_token"] == initial_turn["turn_token"]
    assert asyncio.run(composer_test_client.app.state.blob_service.list_blobs(UUID(session_id))) == []


# ---------------------------------------------------------------------------
# A completed guided session keeps its conversation (elspeth-986801d218)
# ---------------------------------------------------------------------------

_COMMITTED_SOURCE_ID = "00000000-0000-4000-8000-000000000a01"
_COMMITTED_NODE_ID = "00000000-0000-4000-8000-000000000a02"
_COMMITTED_OUTPUT_ID = "00000000-0000-4000-8000-000000000a03"
_COMMITTED_SOURCE_2_ID = "00000000-0000-4000-8000-000000000a05"
_COMMITTED_OUTPUT_2_ID = "00000000-0000-4000-8000-000000000a06"
_COMMITTED_PROPOSAL_ID = "00000000-0000-4000-8000-000000000a04"
_COMMITTED_DRAFT_HASH = "b" * 64
_COMMITTED_PIPELINE_YAML = "sources:\n  primary:\n    plugin: csv\n"
_PROMPT_TEMPLATE_CANARY = "PROMPT_TEMPLATE_CANARY_do_not_leak"
_SOURCE_PATH_CANARY = "/var/lib/elspeth/SOURCE_PATH_CANARY.csv"


# The one authored sink schema in this module that projects a NON-EMPTY
# ``business_schema``. Every other fixture authors observed mode, whose four
# empty lists compare equal under ANY asymmetry between the gate's two halves —
# so the admitting direction of that newly compared fact had no coverage at all
# (RT-4). Both authored field forms are here: the round-trip dict and the
# ``"name: type"`` string grammar.
_DECLARED_OUTPUT_OPTIONS: dict[str, object] = {
    "path": "out.jsonl",
    "schema": {
        "mode": "declared",
        "fields": [{"name": "name", "type": "str", "required": True, "nullable": False}, "amount: int"],
        "guaranteed_fields": ["name"],
        "required_fields": ["name"],
    },
}
# What ``emitters._wire_schema`` freezes for those options — both forms
# normalised to the round-trip dict. Written out rather than re-derived through
# the same helper, so the frozen half of the gate is a RECORD here and not a
# second call to the code under test.
_DECLARED_BUSINESS_SCHEMA: dict[str, object] = {
    "mode": "declared",
    "fields": [
        {"name": "name", "type": "str", "required": True, "nullable": False},
        {"name": "amount", "type": "int", "required": True, "nullable": False},
    ],
    "guaranteed_fields": ["name"],
    "required_fields": ["name"],
}


def _committed_pipeline_state(
    *,
    node_options: dict[str, object] | None = None,
    node_plugin: str = "passthrough",
    output_options: dict[str, object] | None = None,
) -> object:
    """The head composition a confirmed guided build leaves behind.

    Deliberately PLURAL on both ends. ``guided_structure_projection`` labels
    components by their position in ``state.sources`` / ``state.outputs``, so a
    single-source fixture would never notice a key-order change failing to
    round-trip through ``save_composition_state`` → ``state_from_record`` →
    ``CompositionState.from_dict`` — and that failure mode is a permanent false
    409 on every multi-source completed session, not a visible crash.
    """
    from elspeth.web.composer.state import CompositionState

    return CompositionState.from_dict(
        {
            "version": 1,
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "shaped",
                    "options": {"path": _SOURCE_PATH_CANARY, "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
                "secondary": {
                    "plugin": "csv",
                    "on_success": "archive",
                    "options": {"path": "/var/lib/elspeth/reference.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
            },
            "nodes": [
                {
                    "id": "copy",
                    "node_type": "transform",
                    "plugin": node_plugin,
                    "input": "shaped",
                    "on_success": "cleaned",
                    "on_error": "discard",
                    "options": dict(node_options or {}),
                }
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": output_options if output_options is not None else {"path": "out.jsonl", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                },
                {
                    "name": "archive",
                    "plugin": "json",
                    "options": {"path": "archive.jsonl", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                },
            ],
            "metadata": {"name": "committed", "description": None},
        }
    )


def _committed_wire_payload(
    *,
    node_plugin: str = "passthrough",
    node_options_summary: list[dict[str, str]] | None = None,
    business_schema: dict[str, object] | None = None,
) -> dict[str, object]:
    """The frozen CONFIRM_WIRING record the state above was confirmed from.

    ``node_plugin`` must track the state fixture's node plugin: the committed
    chat's admission gate compares that pair, so a payload naming a plugin the
    head does not carry is drift, not a fixture variation. ``business_schema``
    tracks the first output's authored options for the same reason — it is what
    ``emitters._wire_schema`` would have frozen from them.
    """

    def _cardinality(input_: str, output: str) -> dict[str, object]:
        return {"input": input_, "output": output, "expected_output_count": None}

    return {
        "proposal_id": _COMMITTED_PROPOSAL_ID,
        "draft_hash": _COMMITTED_DRAFT_HASH,
        "sources": [
            {
                "stable_id": _COMMITTED_SOURCE_ID,
                "label": "source-1",
                "plugin": "csv",
                "on_validation_failure": "discard",
                "guaranteed_fields": ["name"],
                "row_cardinality": _cardinality("none", "zero_or_many"),
            },
            {
                "stable_id": _COMMITTED_SOURCE_2_ID,
                "label": "source-2",
                "plugin": "csv",
                "on_validation_failure": "discard",
                "guaranteed_fields": ["name"],
                "row_cardinality": _cardinality("none", "zero_or_many"),
            },
        ],
        "nodes": [
            {
                "stable_id": _COMMITTED_NODE_ID,
                "label": "node-1",
                "node_type": "transform",
                "plugin": node_plugin,
                "behavior": {"kind": "transform"},
                "required_fields": ["name"],
                "guaranteed_fields": ["name"],
                "row_cardinality": _cardinality("one", "one"),
                "structured_output_fields": [],
                "node_options_summary": list(node_options_summary or ()),
            }
        ],
        "outputs": [
            {
                "stable_id": _COMMITTED_OUTPUT_ID,
                "label": "output-1",
                "plugin": "json",
                "on_write_failure": "discard",
                "required_fields": ["name"],
                "business_schema": (
                    business_schema
                    if business_schema is not None
                    else {
                        "mode": "observed",
                        "fields": [],
                        "guaranteed_fields": [],
                        "required_fields": [],
                    }
                ),
            },
            {
                "stable_id": _COMMITTED_OUTPUT_2_ID,
                "label": "output-2",
                "plugin": "json",
                "on_write_failure": "discard",
                "required_fields": ["name"],
                "business_schema": {
                    "mode": "observed",
                    "fields": [],
                    "guaranteed_fields": [],
                    "required_fields": [],
                },
            },
        ],
        # Order matters: this is the exact sequence the shared topology
        # derivation emits (every source's success then failure route, then
        # every node's, then every output's write-failure route).
        "connections": [
            {
                "stable_id": "00000000-0000-4000-8000-000000000a11",
                "from_endpoint": {"kind": "source", "stable_id": _COMMITTED_SOURCE_ID},
                "to_endpoint": {"kind": "node", "stable_id": _COMMITTED_NODE_ID},
                "flow": {"kind": "source_success", "branch": None},
                "schema_contract": None,
            },
            {
                "stable_id": "00000000-0000-4000-8000-000000000a12",
                "from_endpoint": {"kind": "source", "stable_id": _COMMITTED_SOURCE_ID},
                "to_endpoint": {"kind": "discard"},
                "flow": {"kind": "source_validation_failure"},
                "schema_contract": None,
            },
            {
                "stable_id": "00000000-0000-4000-8000-000000000a16",
                "from_endpoint": {"kind": "source", "stable_id": _COMMITTED_SOURCE_2_ID},
                "to_endpoint": {"kind": "output", "stable_id": _COMMITTED_OUTPUT_2_ID},
                "flow": {"kind": "source_success", "branch": None},
                "schema_contract": None,
            },
            {
                "stable_id": "00000000-0000-4000-8000-000000000a17",
                "from_endpoint": {"kind": "source", "stable_id": _COMMITTED_SOURCE_2_ID},
                "to_endpoint": {"kind": "discard"},
                "flow": {"kind": "source_validation_failure"},
                "schema_contract": None,
            },
            {
                "stable_id": "00000000-0000-4000-8000-000000000a13",
                "from_endpoint": {"kind": "node", "stable_id": _COMMITTED_NODE_ID},
                "to_endpoint": {"kind": "output", "stable_id": _COMMITTED_OUTPUT_ID},
                "flow": {"kind": "node_success", "branch": None},
                "schema_contract": None,
            },
            {
                "stable_id": "00000000-0000-4000-8000-000000000a14",
                "from_endpoint": {"kind": "node", "stable_id": _COMMITTED_NODE_ID},
                "to_endpoint": {"kind": "discard"},
                "flow": {"kind": "node_error"},
                "schema_contract": None,
            },
            {
                "stable_id": "00000000-0000-4000-8000-000000000a15",
                "from_endpoint": {"kind": "output", "stable_id": _COMMITTED_OUTPUT_ID},
                "to_endpoint": {"kind": "discard"},
                "flow": {"kind": "output_write_failure"},
                "schema_contract": None,
            },
            {
                "stable_id": "00000000-0000-4000-8000-000000000a18",
                "from_endpoint": {"kind": "output", "stable_id": _COMMITTED_OUTPUT_2_ID},
                "to_endpoint": {"kind": "discard"},
                "flow": {"kind": "output_write_failure"},
                "schema_contract": None,
            },
        ],
        "semantic_contracts": [],
        "warnings": [],
        "blockers": [],
        "can_confirm": True,
    }


_REVIEW_TERM = "llm_prompt_template:copy"
_REVIEW_DRAFT = "Summarise {{ row.name }} in one line."


def _reviewable_llm_node_options() -> dict[str, object]:
    """Committed ``copy`` node options carrying ONE pending prompt-template review.

    Deliberately profile-bound and free of ``required_input_fields``: with
    those two facts the composition validates CLEAN once the review is
    resolved (measured 2026-09-03 — ``provider``/``model``/``api_key`` are not
    authorable on a profile-bound node, and a ``required_input_fields`` the
    observed-mode csv sources cannot guarantee is a schema-contract error).
    That is what lets an Accept move the head's verdict, which is the whole
    discriminator of ``test_accepting_a_review_then_chatting_carries_the_new_verdict``.

    The staged ``interpretation_requirements`` row is not decoration: both
    ``create_pending_interpretation_event`` and the supersession sweep derive
    the review's content identity through it, so a node without it cannot
    carry a pending card at all.
    """

    return {
        "profile": "task-role",
        "prompt_template": _REVIEW_DRAFT,
        "schema": {"mode": "observed"},
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "pt",
                "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
                "user_term": _REVIEW_TERM,
                "status": "pending",
                "draft": _REVIEW_DRAFT,
                "event_id": None,
                "accepted_value": None,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            },
        ],
    }


def _seed_reviewable_completed_session(client: TestClient, session_id: str, *, is_valid: bool) -> tuple[str, InterpretationEventRecord]:
    """Seed a confirmed build whose llm node still has one pending review card.

    Returns the confirmation hash the completed chat channel binds to and the
    pending event, minted through the production writer boundary
    (``create_pending_interpretation_event``) so the row carries the real
    ``user_approved`` source the supersession sweep filters on.
    """

    token = _seed_completed_session(
        client,
        session_id,
        state=_committed_pipeline_state(node_plugin="llm", node_options=_reviewable_llm_node_options()),
        wire_payload=_committed_wire_payload(node_plugin="llm"),
        is_valid=is_valid,
    )
    service = client.app.state.session_service
    head = asyncio.run(service.get_current_state(UUID(session_id)))
    assert head is not None
    event = asyncio.run(
        service.create_pending_interpretation_event(
            session_id=UUID(session_id),
            composition_state_id=head.id,
            affected_node_id="copy",
            tool_call_id=f"backend_auto_surface:{uuid4()}",
            user_term=_REVIEW_TERM,
            kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
            llm_draft=_REVIEW_DRAFT,
            model_identifier="anthropic/claude-sonnet-4.6",
            model_version="4.6",
            provider="anthropic",
            composer_skill_hash="0" * 64,
        )
    )
    assert event.choice is InterpretationChoice.PENDING
    # The load-bearing precondition for the sweep this fixture exercises:
    # ``_supersede_dead_site_pending_interpretation_events`` examines ONLY
    # ``user_approved`` pending rows, so a row minted with any other source
    # would make the survival assertion pass for a reason unrelated to the
    # settlement.
    assert event.interpretation_source is InterpretationSource.USER_APPROVED
    return token, event


def _accept_review(client: TestClient, session_id: str, event_id: UUID) -> Response:
    """Accept a pending interpretation card the way the review surface does."""

    return client.post(
        f"/api/sessions/{session_id}/interpretations/{event_id}/resolve",
        json={"choice": "accepted_as_drafted"},
    )


def _interpretation_choices(client: TestClient, session_id: str) -> list[InterpretationChoice]:
    events = asyncio.run(client.app.state.session_service.list_interpretation_events(UUID(session_id), status="all"))
    return [event.choice for event in events]


def _seed_completed_session(
    client: TestClient,
    session_id: str,
    *,
    state: object | None = None,
    wire_payload: dict[str, object] | None = None,
    is_valid: bool = True,
    terminal_kind: str = "completed",
    chat_history: tuple[object, ...] = (),
) -> str:
    """Persist a settled guided build and return its confirmation hash."""
    from elspeth.web.composer.guided.protocol import GuidedStep as _GuidedStep
    from elspeth.web.composer.guided.protocol import TurnType as _TurnType
    from elspeth.web.composer.guided.state_machine import TerminalKind, TerminalReason, TerminalState, TurnRecord
    from elspeth.web.sessions.guided_payloads import prepare_guided_json_payload
    from elspeth.web.sessions.guided_replay import guided_completed_chat_token

    payload = wire_payload if wire_payload is not None else _committed_wire_payload()
    prepared = prepare_guided_json_payload(
        client.app.state.payload_store,
        purpose="turn",
        payload=payload,
    )
    confirmation = prepare_guided_json_payload(
        client.app.state.payload_store,
        purpose="turn_response",
        payload={
            "action": "confirm_wiring",
            "proposal_id": _COMMITTED_PROPOSAL_ID,
            "draft_hash": _COMMITTED_DRAFT_HASH,
        },
    )
    completed = replace(
        GuidedSession(step=_GuidedStep.STEP_4_WIRE),
        # The reviewed-component maps a confirmed build actually carries. An
        # empty pair here is a shape production cannot reach — ``_build_projection``
        # cross-checks ``state.sources`` against ``guided.reviewed_sources`` for
        # every id in ``source_order`` before a CONFIRM_WIRING payload can exist
        # — and it made this class's redaction assertions vacuous: with both maps
        # empty, ``_current_source``/``_current_sink`` returned None and no code
        # path could have carried the storage-path canary into the context at all.
        source_order=(_COMMITTED_SOURCE_ID, _COMMITTED_SOURCE_2_ID),
        reviewed_sources={
            _COMMITTED_SOURCE_ID: SourceResolved(
                name="primary",
                plugin="csv",
                options={"path": _SOURCE_PATH_CANARY, "schema": {"mode": "observed"}},
                observed_columns=("name",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
            _COMMITTED_SOURCE_2_ID: SourceResolved(
                name="secondary",
                plugin="csv",
                options={"path": "/var/lib/elspeth/reference.csv", "schema": {"mode": "observed"}},
                observed_columns=("name",),
                sample_rows=(),
                on_validation_failure="discard",
            ),
        },
        output_order=(_COMMITTED_OUTPUT_ID, _COMMITTED_OUTPUT_2_ID),
        reviewed_outputs={
            _COMMITTED_OUTPUT_ID: SinkOutputResolved(
                name="cleaned",
                plugin="json",
                options={"path": "out.jsonl", "schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            ),
            _COMMITTED_OUTPUT_2_ID: SinkOutputResolved(
                name="archive",
                plugin="json",
                options={"path": "archive.jsonl", "schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            ),
        },
        history=(
            TurnRecord(
                step=_GuidedStep.STEP_4_WIRE,
                turn_type=_TurnType.CONFIRM_WIRING,
                payload_hash=prepared.payload_id,
                response_hash=confirmation.payload_id,
                emitter="server",
                summary="Guided pipeline wiring confirmed.",
            ),
        ),
        chat_history=chat_history,
        chat_turn_seq=(chat_history[-1].seq + 1 if chat_history else 0),
        terminal=(
            TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml=_COMMITTED_PIPELINE_YAML)
            if terminal_kind == "completed"
            else TerminalState(
                kind=TerminalKind.EXITED_TO_FREEFORM,
                reason=TerminalReason.USER_PRESSED_EXIT,
                pipeline_yaml=None,
            )
        ),
    )
    head = replace(state if state is not None else _committed_pipeline_state(), guided_session=completed)
    state_dict = head.to_dict()
    asyncio.run(
        client.app.state.session_service.save_composition_state(
            UUID(session_id),
            CompositionStateData(
                sources=state_dict["sources"],
                nodes=state_dict["nodes"],
                edges=state_dict["edges"],
                outputs=state_dict["outputs"],
                metadata_=state_dict["metadata"],
                is_valid=is_valid,
                validation_errors=None if is_valid else ["guided_composition_invalid"],
                composer_meta={"guided_session": completed.to_dict()},
            ),
            provenance="session_seed",
        )
    )
    if terminal_kind != "completed":
        return "0" * 64
    return guided_completed_chat_token(completed)


def _state_versions(client: TestClient, session_id: str) -> object:
    return asyncio.run(client.app.state.session_service.get_state_versions(UUID(session_id)))


def _planner_attempt_audits(client: TestClient, session_id: str) -> list[dict[str, object]]:
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    return [
        envelope["attempt"]
        for message in messages
        for envelope in (message.tool_calls or ())
        if envelope.get("_kind") == "planner_attempt_audit"
    ]


def _chat_turn_audits(client: TestClient, session_id: str) -> list[dict[str, object]]:
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    return [
        envelope["turn"] for message in messages for envelope in (message.tool_calls or ()) if envelope.get("_kind") == "chat_turn_audit"
    ]


def _guided_audit_invocation_names(client: TestClient, session_id: str) -> list[str]:
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    return [
        invocation["tool_name"]
        for message in messages
        for tool_call in (message.tool_calls or ())
        for invocation in (tool_call.get("invocation") or {},)
        if "tool_name" in invocation
    ]


class TestCompletedSessionChat:
    """A confirmed pipeline keeps its conversation (elspeth-986801d218).

    After **Confirm wiring** there is no unanswered turn, so the ordinary
    ``turn_token`` binding does not exist. The channel is bound to the
    confirmation that closed the build instead, the answer is advisory over the
    frozen wire record, and NOTHING about the committed pipeline may move: the
    tests below assert admission, the exact provider shape, and — case by case
    — every way the request must be refused instead.
    """

    @staticmethod
    def _capture_provider(monkeypatch: pytest.MonkeyPatch, *, reply: str) -> list[dict[str, object]]:
        """Run the REAL provider path, capturing the assembled request.

        The class's other tests stub `_run_guided_chat_provider_attempt`, which
        skips context assembly entirely — so every assertion about what the
        model is told has to come through this seam.
        """
        from elspeth.web.composer.guided import chat_solver

        captured: list[dict[str, object]] = []

        async def capture(**kwargs: object) -> object:
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply, tool_calls=None))])

        monkeypatch.setattr(chat_solver, "_litellm_acompletion", capture)
        return captured

    @staticmethod
    def _completed_chat(
        client: TestClient,
        session_id: str,
        token: str,
        *,
        message: str = "What does node-1 do?",
        operation_id: str | None = None,
    ) -> object:
        return client.post(
            f"/api/sessions/{session_id}/guided/chat",
            json={
                "operation_id": operation_id or str(uuid4()),
                "turn_token": token,
                "message": message,
            },
        )

    def test_completed_chat_answers_over_the_frozen_wire_record(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from elspeth.web.composer.guided import chat_solver
        from elspeth.web.composer.guided.prompts import load_step_chat_skill

        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(composer_test_client, session_id)
        before = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
        assert before is not None
        captured = self._capture_provider(monkeypatch, reply="node-1 copies every row through unchanged.")

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["assistant_message"] == "node-1 copies every row through unchanged."
        assert body["assistant_message_kind"] == "assistant"
        # The build stays finished: no turn to answer, terminal echoed.
        assert body["next_turn"] is None
        assert body["terminal"]["kind"] == "completed"
        assert body["terminal"]["pipeline_yaml"] == _COMMITTED_PIPELINE_YAML
        # Exactly one provider call, advisory (no tools).
        assert len(captured) == 1
        assert captured[0].get("tools") is None
        messages = captured[0]["messages"]
        assert messages[0]["content"] == load_step_chat_skill(GuidedStep.STEP_4_WIRE)
        assert messages[1]["content"] == chat_solver._ADVISORY_NO_TOOLS_ADDENDUM
        context = "\n".join(str(message["content"]) for message in messages)
        assert "The guided build is FINISHED" in context
        assert "Saved build instructions were all resolved at confirmation" in context
        # No verdict of its own, and no on-screen control named: the head's
        # validity is not in this context (the frozen review counts are), and
        # the backend cannot see which controls the surface is showing — the
        # tutorial dwell hides the freeform-editor button the opener used to
        # name as the only way out.
        assert "Committed validation:" not in context
        assert "Open freeform editor" not in context
        assert "never state whether the pipeline is valid or ready to run" in context
        # Scoped to the block this arm BUILDS: `is_valid` is a common enough
        # word that asserting it over the whole prompt would red this test for
        # an unrelated skill edit, pointing nowhere near the cause.
        committed_block = next(str(message["content"]) for message in messages if "The guided build is FINISHED" in str(message["content"]))
        assert "is_valid" not in committed_block
        # The committed graph is described from the frozen record — ALL of it.
        # The fixture is deliberately plural on both ends; an assertion that
        # names only the first source would pass off a context that dropped
        # every component after it.
        for alias in ("source-1", "source-2", "node-1", "output-1", "output-2"):
            assert alias in context
        # No "Applied source/output" projection: it names at most one source,
        # so on this two-source build it either under-describes the pipeline
        # or renders "none yet." above a graph listing both.
        assert "none yet." not in context
        # Never the yaml, the storage path, or authored option values. The
        # session now CARRIES that path in `reviewed_sources`, so this is a
        # live check on the committed arm's suppression rather than an
        # assertion about a field the fixture never populated.
        assert _COMMITTED_PIPELINE_YAML not in context
        assert _SOURCE_PATH_CANARY not in context
        # Transcript grows by exactly the pair, at the wire step, bound to the
        # confirmation the session settled on.
        chat_history = body["guided_session"]["chat_history"]
        assert [turn["role"] for turn in chat_history] == ["user", "assistant"]
        assert [turn["step"] for turn in chat_history] == ["step_4_wire", "step_4_wire"]
        assert chat_history[0]["turn_token"] == token
        # The committed pipeline itself is untouched.
        after = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
        assert after is not None
        assert (after.sources, after.nodes, after.outputs, after.metadata_) == (
            before.sources,
            before.nodes,
            before.outputs,
            before.metadata_,
        )
        assert body["composition_state"]["is_valid"] is True
        # One advisory provider call, attributed to no planner attempt: a
        # question about a settled build never re-plans it.
        assert len(_llm_audit_calls(composer_test_client, session_id)) == 1
        assert _planner_attempt_audits(composer_test_client, session_id) == []
        chat_turn_audits = _chat_turn_audits(composer_test_client, session_id)
        assert len(chat_turn_audits) == 1
        assert chat_turn_audits[0]["step"] == "step_4_wire"
        assert chat_turn_audits[0]["turn_token"] == token

    def test_completed_chat_emits_one_llm_call_and_no_turn_occurrence(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The wire turn was emitted and answered long before this question."""

        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(composer_test_client, session_id)
        emitted_before = _guided_audit_invocation_names(composer_test_client, session_id).count("guided_turn_emitted")

        async def _reply(**_kwargs: object) -> GuidedChatProviderOutcome:
            return GuidedStepChatOnlyResult(
                chat=StepChatResult(
                    assistant_message="It writes JSON to your output.",
                    status=ComposerChatTurnStatus.SUCCESS,
                    latency_ms=1,
                    error_class=None,
                )
            )

        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _reply, raising=False)

        assert self._completed_chat(composer_test_client, session_id, token).status_code == 200

        names = _guided_audit_invocation_names(composer_test_client, session_id)
        assert names.count("guided_turn_emitted") == emitted_before
        assert names.count("guided_turn_answered") == 0
        assert names.count("guided_step_advanced") == 0
        assert _chat_operation_count(composer_test_client, session_id) == 1

    def test_completed_chat_replays_exactly_by_operation_id(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(composer_test_client, session_id)
        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)
        operation_id = str(uuid4())

        first = self._completed_chat(composer_test_client, session_id, token, operation_id=operation_id)
        assert first.status_code == 200, first.json()

        monkeypatch.setattr(
            guided_route,
            "_run_guided_chat_provider_attempt",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("replay called provider")),
            raising=False,
        )
        second = self._completed_chat(composer_test_client, session_id, token, operation_id=operation_id)

        assert second.status_code == 200, second.json()
        assert second.json() == first.json()
        assert _chat_operation_count(composer_test_client, session_id) == 1

    def test_second_question_reuses_the_same_token(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The token is the confirmation hash, so it survives every answer."""

        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(composer_test_client, session_id)
        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)

        assert self._completed_chat(composer_test_client, session_id, token).status_code == 200
        second = self._completed_chat(composer_test_client, session_id, token, message="And the output?")

        assert second.status_code == 200, second.json()
        assert len(second.json()["guided_session"]["chat_history"]) == 4

    def test_completed_chat_carries_the_head_validity_forward(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A question must not silently re-validate a Review-required build.

        Seeded invalid on purpose, and asserted on BOTH sides of the same
        response: the head record the client renders ("Review required", Run
        refused) and the context the model is handed. Those were two different
        authorities — the context stated the frozen wire payload's
        ``can_confirm`` as ``is_valid`` — so in exactly this state the model
        was told the build was valid with zero errors while the user's screen
        said the opposite (review round 1, 2026-09-03).
        """

        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(composer_test_client, session_id, is_valid=False)
        captured = self._capture_provider(monkeypatch, reply="It writes JSON to your output.")

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 200, response.json()
        assert response.json()["composition_state"]["is_valid"] is False
        assert response.json()["composition_state"]["validation_errors"] == ["guided_composition_invalid"]
        assert len(captured) == 1
        messages = captured[0]["messages"]
        context = "\n".join(str(message["content"]) for message in messages)
        assert "Committed validation:" not in context
        assert "never state whether the pipeline is valid or ready to run" in context
        # Scoped to the committed block itself — see the sibling test.
        committed_block = next(str(message["content"]) for message in messages if "The guided build is FINISHED" in str(message["content"]))
        assert "is_valid" not in committed_block

    def test_authored_settings_are_not_republished_from_the_frozen_record(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Structure is bound to the live head; authored option values are not.

        The admission gate compares (kind, label, plugin, node_type) and the
        connection endpoints — so an interpretation Accept that rewrites an
        llm node's ``prompt_template`` is admitted BY DESIGN. The frozen wire
        record's ``node_options_summary`` still carries the pre-Accept text,
        and republishing it would have the model explain the pipeline by a
        prompt that is no longer in it.
        """

        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(
            composer_test_client,
            session_id,
            # The post-Accept head: same structure, different prompt text.
            state=_committed_pipeline_state(node_plugin="llm", node_options={"prompt_template": "Summarise the row."}),
            wire_payload=_committed_wire_payload(
                node_plugin="llm",
                node_options_summary=[{"key": "prompt_template", "value": _PROMPT_TEMPLATE_CANARY}],
            ),
        )
        captured = self._capture_provider(monkeypatch, reply="node-1 asks a model about each row.")

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 200, response.json()
        assert len(captured) == 1
        context = "\n".join(str(message["content"]) for message in captured[0]["messages"])
        assert _PROMPT_TEMPLATE_CANARY not in context
        # Withheld, and said to be withheld — an unexplained gap invites the
        # model to fill it from the pre-commit conversation.
        assert "so are the authored settings behind each component" in context
        # The structure itself still describes the node.
        assert "node-1" in context

    def test_wrong_token_is_refused_before_any_reservation_or_provider_call(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session_id = _create_session(composer_test_client)
        _seed_completed_session(composer_test_client, session_id)
        versions_before = _state_versions(composer_test_client, session_id)
        monkeypatch.setattr(
            guided_route,
            "_run_guided_chat_provider_attempt",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("refused request called the provider")),
            raising=False,
        )

        response = self._completed_chat(composer_test_client, session_id, "f" * 64)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"] == "turn_token does not identify the confirmed pipeline."
        assert _chat_operation_count(composer_test_client, session_id) == 0
        assert _state_versions(composer_test_client, session_id) == versions_before

    def test_exited_to_freeform_keeps_the_verbatim_terminal_refusal(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exit closes the channel; only /guided/reenter reopens it."""

        session_id = _create_session(composer_test_client)
        _seed_completed_session(composer_test_client, session_id, terminal_kind="exited_to_freeform")
        monkeypatch.setattr(
            guided_route,
            "_run_guided_chat_provider_attempt",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("refused request called the provider")),
            raising=False,
        )

        response = self._completed_chat(composer_test_client, session_id, "f" * 64)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"] == "Guided session is already terminal."
        assert _chat_operation_count(composer_test_client, session_id) == 0

    def test_structure_drift_under_a_completed_session_is_refused(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A freeform compose can rewire the head; the answer must not lie.

        Explaining the frozen wire record would describe a graph that is no
        longer there, so the request is refused rather than answered.
        """

        session_id = _create_session(composer_test_client)
        drifted = _committed_pipeline_state()
        drifted = replace(drifted, nodes=(*drifted.nodes, replace(drifted.nodes[0], id="injected")))
        token = _seed_completed_session(composer_test_client, session_id, state=drifted)
        versions_before = _state_versions(composer_test_client, session_id)
        monkeypatch.setattr(
            guided_route,
            "_run_guided_chat_provider_attempt",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("refused request called the provider")),
            raising=False,
        )

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"]["code"] == "guided_chat_committed_graph_changed"
        assert _chat_operation_count(composer_test_client, session_id) == 0
        assert _state_versions(composer_test_client, session_id) == versions_before

    def test_unprojectable_head_is_refused_with_409_not_500(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A head that cannot be projected at all is still just drift."""

        session_id = _create_session(composer_test_client)
        orphaned = _committed_pipeline_state()
        orphaned = replace(
            orphaned,
            nodes=(replace(orphaned.nodes[0], on_success="nothing-consumes-this"),),
        )
        token = _seed_completed_session(composer_test_client, session_id, state=orphaned)
        monkeypatch.setattr(
            guided_route,
            "_run_guided_chat_provider_attempt",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("refused request called the provider")),
            raising=False,
        )

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"]["code"] == "guided_chat_committed_graph_changed"

    def test_prompt_template_patch_still_admits_the_conversation(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An interpretation Accept patches node options, never structure."""

        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(
            composer_test_client,
            session_id,
            state=_committed_pipeline_state(node_options={"prompt_template": _PROMPT_TEMPLATE_CANARY}),
        )
        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 200, response.json()

    def test_a_time_qualified_transform_cardinality_does_not_refuse_the_chat(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The route half of the published/compared partition, admitting side.

        A TRANSFORM node's ``row_cardinality`` is frozen from the LOWERED
        executable state, so the gate cannot re-derive it and the chat context
        publishes it as ``row_cardinality_at_confirmation`` instead. A frozen
        record whose cardinality no longer matches what the head would lower to
        must therefore still be answered — refusing it would 409 completed
        sessions with no drift at all.
        """

        session_id = _create_session(composer_test_client)
        payload = _committed_wire_payload()
        node = cast(list[dict[str, Any]], payload["nodes"])[0]
        node["row_cardinality"] = {"input": "one", "output": "zero_or_many", "expected_output_count": None}
        token = _seed_completed_session(composer_test_client, session_id, wire_payload=payload)
        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 200, response.json()

    def test_a_declared_output_schema_still_admits_the_conversation(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The admitting side of the newly compared ``business_schema`` (RT-4).

        Every other completed-session fixture here authors an observed-mode
        sink, whose projected ``business_schema`` is four EMPTY lists — and
        empty lists compare equal under any asymmetry between the gate's two
        halves. A projection/mirror disagreement on a declared schema would
        therefore refuse every completed-session chat for every pipeline that
        declares output fields, with no drift at all, while the refusing test
        below and the rest of this suite stayed green. This is that class of
        pipeline, unchanged, and it must still be answered.
        """

        assert _DECLARED_BUSINESS_SCHEMA["fields"], "the fixture must carry real fields or it proves nothing"
        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(
            composer_test_client,
            session_id,
            state=_committed_pipeline_state(output_options=_DECLARED_OUTPUT_OPTIONS),
            wire_payload=_committed_wire_payload(business_schema=_DECLARED_BUSINESS_SCHEMA),
        )
        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 200, response.json()

    @pytest.mark.parametrize(
        ("component", "key", "value"),
        [
            pytest.param(
                "nodes",
                "structured_output_fields",
                [{"query": "q1", "field": "q1_verdict", "type": "string", "enum_values": ["yes", "no"]}],
                id="structured-output-fields",
            ),
            pytest.param(
                "outputs",
                "business_schema",
                {"mode": "declared", "fields": [], "guaranteed_fields": [], "required_fields": ["name"]},
                id="business-schema",
            ),
        ],
    )
    def test_an_options_derived_published_fact_going_stale_is_refused(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        component: str,
        key: str,
        value: object,
    ) -> None:
        """The route half of the partition, refusing side (RT-2).

        ``structured_output_fields`` and ``business_schema`` are published to
        the model on a committed build and are PURE functions of head options
        with no lowering dependency, so a ``patch_node_options`` /
        ``patch_output_options`` write can move them while every endpoint and
        behavior stays identical. Before they joined the gate, the model was
        handed the frozen value as current fact.
        """

        session_id = _create_session(composer_test_client)
        payload = _committed_wire_payload()
        cast(list[dict[str, Any]], payload[component])[0][key] = value
        token = _seed_completed_session(composer_test_client, session_id, wire_payload=payload)
        versions_before = _state_versions(composer_test_client, session_id)
        monkeypatch.setattr(
            guided_route,
            "_run_guided_chat_provider_attempt",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("refused request called the provider")),
            raising=False,
        )

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"]["code"] == "guided_chat_committed_graph_changed"
        assert _chat_operation_count(composer_test_client, session_id) == 0
        assert _state_versions(composer_test_client, session_id) == versions_before

    def test_transcript_cap_is_refused_with_409_before_the_provider(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A full transcript is a capacity limit, not a server invariant break."""
        from elspeth.web.composer.guided.protocol import ChatRole as _ChatRole
        from elspeth.web.composer.guided.protocol import ChatTurn as _ChatTurn
        from elspeth.web.composer.guided.state_machine import GUIDED_MAX_CHAT_TURNS

        session_id = _create_session(composer_test_client)
        full = tuple(
            _ChatTurn(
                role=_ChatRole.USER if index % 2 == 0 else _ChatRole.ASSISTANT,
                content="x",
                seq=index,
                step=GuidedStep.STEP_4_WIRE,
                ts_iso="2026-09-03T00:00:00+00:00",
                assistant_message_kind=None if index % 2 == 0 else "assistant",
                turn_token=None,
            )
            for index in range(GUIDED_MAX_CHAT_TURNS - 1)
        )
        token = _seed_completed_session(composer_test_client, session_id, chat_history=full)
        monkeypatch.setattr(
            guided_route,
            "_run_guided_chat_provider_attempt",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("refused request called the provider")),
            raising=False,
        )

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"]["code"] == "guided_chat_history_full"
        assert _chat_operation_count(composer_test_client, session_id) == 0

    def test_synthetic_failure_reply_leaves_the_committed_build_untouched(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rejected reply is still a transcript turn, never a state change."""

        session_id = _create_session(composer_test_client)
        token = _seed_completed_session(composer_test_client, session_id)
        before = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
        assert before is not None

        async def _leaked(**_kwargs: object) -> GuidedChatProviderOutcome:
            return GuidedStepChatOnlyResult(
                chat=StepChatResult(
                    assistant_message="I couldn't finish that reply. Press Retry.",
                    status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                    latency_ms=1,
                    error_class="AssistantScaffoldLeakError",
                )
            )

        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _leaked, raising=False)

        response = self._completed_chat(composer_test_client, session_id, token)

        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["assistant_message_kind"] == "synthetic_failure"
        assert body["next_turn"] is None
        assert body["terminal"]["kind"] == "completed"
        after = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
        assert after is not None
        assert (after.sources, after.nodes, after.outputs) == (before.sources, before.nodes, before.outputs)
        assert body["guided_session"]["chat_history"][-1]["synthetic_failure_reason"] == "quality_guard"

    def test_accepting_a_review_then_chatting_carries_the_new_verdict(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An Accept's verdict must survive the question that follows it.

        The end-to-end sibling the two nearby tests only approximate:
        ``test_prompt_template_patch_still_admits_the_conversation`` SEEDS the
        post-Accept shape rather than reaching it, and
        ``test_completed_chat_carries_the_head_validity_forward`` never leaves
        the seeded verdict at all. Here the ``interpretation_resolve`` writer
        authors the head — resolving the review is what makes the build valid
        — and the completed chat that follows must persist THAT row's verdict.

        What it catches: a terminal settlement that takes its validity from
        anywhere but the live head at settlement time — the row the guided
        build was seeded with, the pre-provider frozen snapshot, or the guided
        session's own idea of the build. Every one of those reports
        ``is_valid=False`` here, so asking a question would flip a "Pipeline
        ready" surface back to "Review required" and shut the Run gate on a
        build nothing changed.

        The fixture is asserted to DISCRIMINATE (seeded verdict != post-Accept
        verdict) so it reds loudly if the composition ever drifts into
        agreeing, instead of going quietly vacuous.

        Not a re-derivation pin, and deliberately does not claim to be. This
        harness wires no runtime preflight — the only stage the interpretation
        writer's validator and ``_guided_persisted_validity``'s catalog
        validator do not share — so both return the same verdict for this
        state (measured 2026-09-03), and a settlement that re-derived rather
        than carried would still pass here. Staleness is what this proves.
        """

        session_id = _create_session(composer_test_client)
        seeded_is_valid = False
        token, event = _seed_reviewable_completed_session(composer_test_client, session_id, is_valid=seeded_is_valid)

        accepted = _accept_review(composer_test_client, session_id, event.id)
        assert accepted.status_code == 200, accepted.json()

        service = composer_test_client.app.state.session_service
        post_accept = asyncio.run(service.get_current_state(UUID(session_id)))
        assert post_accept is not None
        # The discrimination check: without a moved verdict this test proves
        # nothing about which authority the settlement read.
        assert post_accept.is_valid is not seeded_is_valid
        assert post_accept.is_valid is True
        assert post_accept.validation_errors is None

        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)
        response = self._completed_chat(composer_test_client, session_id, token, message="What does node-1 do now?")

        assert response.status_code == 200, response.json()
        composition = response.json()["composition_state"]
        assert composition["is_valid"] is post_accept.is_valid
        assert composition["validation_errors"] is None
        # And the row the settlement actually wrote, not just the echo: a new
        # version carrying the Accept's verdict over identical content.
        after = asyncio.run(service.get_current_state(UUID(session_id)))
        assert after is not None
        assert after.version > post_accept.version
        assert after.is_valid is post_accept.is_valid
        assert after.validation_errors == post_accept.validation_errors
        assert (after.sources, after.nodes, after.outputs, after.metadata_) == (
            post_accept.sources,
            post_accept.nodes,
            post_accept.outputs,
            post_accept.metadata_,
        )

    def test_a_pending_review_survives_a_completed_chat_and_still_accepts(
        self,
        composer_test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Questions must not retire the review cards the build is waiting on.

        Every composition-state writer runs
        ``_supersede_dead_site_pending_interpretation_events`` inside its own
        INSERT, terminally retiring any pending ``user_approved`` row whose
        review site the new head extinguished. A completed chat writes a new
        ``composition_states`` version per question, so that sweep runs per
        question too — and the only reason the card survives is that the
        terminal arm's authored content is byte-identical to the head.

        What it catches: a terminal settlement that writes anything but the
        head's exact content — dropping the node's ``options``, re-lowering
        the profile, normalising ``interpretation_requirements`` away. The
        sweep would then find the site extinguished and flip the row to
        SUPERSEDED, after which the user's Accept comes back 409
        ``interpretation_already_resolved``: a zombie card whose Run gate
        never clears (elspeth-d73139155a).

        Both halves are asserted because either alone is weak — the count says
        the row is still there, the Accept says it is still RESOLVABLE — and
        two questions are asked because a settlement that corrupted the site
        only on a second write would pass a single-question check.
        """

        session_id = _create_session(composer_test_client)
        token, event = _seed_reviewable_completed_session(composer_test_client, session_id, is_valid=False)
        monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider, raising=False)

        assert self._completed_chat(composer_test_client, session_id, token).status_code == 200
        assert self._completed_chat(composer_test_client, session_id, token, message="And the outputs?").status_code == 200

        assert _interpretation_choices(composer_test_client, session_id) == [InterpretationChoice.PENDING]
        accepted = _accept_review(composer_test_client, session_id, event.id)
        assert accepted.status_code == 200, accepted.json()
        assert accepted.json()["event"]["choice"] == InterpretationChoice.ACCEPTED_AS_DRAFTED.value
        assert _interpretation_choices(composer_test_client, session_id) == [InterpretationChoice.ACCEPTED_AS_DRAFTED]
        # The Accept did what it exists to do — the review is resolved on the
        # node, not merely marked resolved on the event row.
        after = asyncio.run(composer_test_client.app.state.session_service.get_current_state(UUID(session_id)))
        assert after is not None
        assert after.nodes is not None
        requirements = next(node["options"][INTERPRETATION_REQUIREMENTS_KEY] for node in after.nodes if node["id"] == "copy")
        assert [requirement["status"] for requirement in requirements] == ["resolved"]

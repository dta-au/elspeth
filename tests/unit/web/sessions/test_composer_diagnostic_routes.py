"""Private Composer evidence has an explicit, attributed read boundary."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_record_audit_grade_view import _get


def _evidence(session_id: UUID, *, call_id: str = "call_private") -> dict[str, Any]:
    return build_private_tool_evidence(
        session_id=str(session_id),
        operation_id=str(uuid4()),
        operation_epoch=1,
        tool_call_id=call_id,
        tool_name="patch_node_options",
        status="success",
        arguments_canonical=canonical_json({"node_id": "classify", "options": {"prompt_template": "Keep the full ordinary reference."}}),
        result_canonical=canonical_json({"success": False, "message": "Please clarify the reference category."}),
    ).to_envelope()


@pytest.mark.asyncio
async def test_diagnostic_evidence_read_is_explicit_and_attributed(test_client: SyncASGITestClient) -> None:
    service = test_client.app.state.session_service
    session = await service.create_session("alice", "Diagnostic evidence", "local")

    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence?limit=5&offset=0")

    assert response.status_code == 200
    assert response.json() == []
    accesses = service.list_audit_access_log(session_id=str(session.id))
    assert len(accesses) == 1
    assert accesses[0].request_path == f"/api/sessions/{session.id}/diagnostic-evidence"
    assert accesses[0].query_args == {"limit": "5", "offset": "0"}


@pytest.mark.asyncio
async def test_diagnostic_evidence_refuses_another_sessions_owner(test_client: SyncASGITestClient) -> None:
    service = test_client.app.state.session_service
    with test_client.app.state.phase3_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="bob")
    session = await service.create_session("bob", "Private diagnostics", "local")

    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence")

    assert response.status_code == 404
    assert service.list_audit_access_log(session_id=str(session.id)) == []


@pytest.mark.asyncio
async def test_diagnostic_evidence_refuses_disclosure_without_access_receipt(
    test_client: SyncASGITestClient,
    inject_audit_access_log_write_failure,
) -> None:
    service = test_client.app.state.session_service
    session = await service.create_session("alice", "Diagnostic evidence", "local")
    inject_audit_access_log_write_failure(service)

    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence", raise_app_exceptions=False)

    assert response.status_code == 500
    assert response.json()["error_type"] == "audit_access_log_write_failed"


@pytest.mark.asyncio
async def test_private_evidence_roundtrips_but_all_generic_views_and_history_exclude_it(test_client: SyncASGITestClient) -> None:
    service = test_client.app.state.session_service
    session = await service.create_session("alice", "Diagnostic evidence", "local")
    await service.add_message(session.id, "user", "Build the requested pipeline.", writer_principal="route_user_message")
    evidence = _evidence(session.id)
    private = await service.add_message(
        session.id, "audit", "Private evidence recorded.", tool_calls=(evidence,), writer_principal="compose_loop"
    )

    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence")

    assert response.status_code == 200
    row = response.json()[0]
    assert row["message_id"] == str(private.id)
    assert row["created_at"]
    assert json.loads(row["evidence"]["arguments"]["content"])["options"]["prompt_template"] == "Keep the full ordinary reference."
    assert json.loads(row["evidence"]["result"]["content"])["message"] == "Please clarify the reference category."
    all_flags = "include_tool_rows=true&include_llm_audit=true&include_raw_content=true&include_rejection_reasons=true"
    for query in ("", f"?{all_flags}"):
        generic = await _get(test_client, f"/api/sessions/{session.id}/messages{query}")
        assert generic.status_code == 200
        assert [message["content"] for message in generic.json()] == ["Build the requested pipeline."]
    history = _composer_chat_history(await service.get_messages(session.id, limit=None))
    assert len(history) == 1
    assert history[0]["content"] == "Build the requested pipeline."


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["session", "operation", "retained_content", "envelope_hash", "role", "mixed", "writer"])
async def test_private_evidence_read_refuses_corrupt_or_cross_bound_records(test_client: SyncASGITestClient, corruption: str) -> None:
    service = test_client.app.state.session_service
    session = await service.create_session("alice", "Diagnostic evidence", "local")
    evidence = _evidence(uuid4() if corruption == "session" else session.id)
    if corruption == "operation":
        evidence["operation_id"] = str(uuid4())
    elif corruption == "retained_content":
        evidence["evidence"]["arguments"]["content"] = '{"changed":true}'
    elif corruption == "envelope_hash":
        evidence["evidence_sha256"] = "0" * 64
    await service.add_message(
        session.id,
        "assistant" if corruption == "role" else "audit",
        "Private evidence recorded.",
        tool_calls=(evidence, {"_kind": "llm_call_audit", "call": {}}) if corruption == "mixed" else (evidence,),
        writer_principal="admin_tool" if corruption == "writer" else "compose_loop",
    )

    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence", raise_app_exceptions=False)

    assert response.status_code == 500
    assert "Keep the full ordinary reference." not in response.text
    assert "Please clarify the reference category." not in response.text
    if corruption in {"role", "mixed"}:
        generic = await _get(test_client, f"/api/sessions/{session.id}/messages?include_llm_audit=true", raise_app_exceptions=False)
        assert generic.status_code == 500
    if corruption in {"role", "mixed", "writer"}:
        with pytest.raises(AuditIntegrityError):
            _composer_chat_history(await service.get_messages(session.id, limit=None))


@pytest.mark.asyncio
async def test_private_evidence_pagination_ignores_conversation_rows(test_client: SyncASGITestClient) -> None:
    service = test_client.app.state.session_service
    session = await service.create_session("alice", "Evidence pagination", "local")
    for index in range(2):
        await service.add_message(session.id, "user", f"Message {index}", writer_principal="route_user_message")
        await service.add_message(
            session.id,
            "audit",
            "Private evidence recorded.",
            tool_calls=(_evidence(session.id, call_id=f"call_{index}"),),
            writer_principal="compose_loop",
        )

    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence?offset=1&limit=1")

    assert response.status_code == 200
    assert [row["evidence"]["tool_call_id"] for row in response.json()] == ["call_1"]
    conversation = await _get(test_client, f"/api/sessions/{session.id}/messages?offset=1&limit=1")
    assert conversation.status_code == 200
    assert [row["content"] for row in conversation.json()] == ["Message 1"]


@pytest.mark.asyncio
async def test_private_read_pages_storage_and_stops_before_unrequested_suffix(test_client: SyncASGITestClient, monkeypatch) -> None:
    service = test_client.app.state.session_service
    session = await service.create_session("alice", "Bounded evidence read", "local")
    for index in range(3):
        await service.add_message(session.id, "user", f"Message {index}", writer_principal="route_user_message")
        evidence = _evidence(session.id, call_id=f"call_{index}")
        if index == 2:
            evidence["evidence_sha256"] = "0" * 64
        await service.add_message(
            session.id, "audit", "Private evidence recorded.", tool_calls=(evidence,), writer_principal="compose_loop"
        )
    original = service.get_messages
    reads: list[tuple[int | None, int]] = []

    async def bounded_get_messages(session_id: UUID, limit: int | None = 100, offset: int = 0):
        reads.append((limit, offset))
        assert limit == 1, "Private reads must not materialize the entire session"
        return await original(session_id, limit=limit, offset=offset)

    monkeypatch.setattr(service, "get_messages", bounded_get_messages)

    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence?offset=1&limit=1")

    assert response.status_code == 200
    assert [row["evidence"]["tool_call_id"] for row in response.json()] == ["call_1"]
    assert reads == [(1, 0), (1, 1), (1, 2), (1, 3)]
    corrupt_page = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence?offset=2&limit=1", raise_app_exceptions=False)
    assert corrupt_page.status_code == 500


@pytest.mark.asyncio
async def test_private_read_refuses_oversized_page(test_client: SyncASGITestClient) -> None:
    service = test_client.app.state.session_service
    session = await service.create_session("alice", "Bounded evidence page", "local")
    response = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence?limit=6")
    assert response.status_code == 422

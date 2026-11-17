"""Owner saves approve exactly the bytes committed by blob replacement."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.web.blobs import routes
from elspeth.web.blobs.service import content_hash
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.interpretation_state import (
    InterpretationReviewPending,
    materialize_state_for_execution,
    prompt_review_draft_from_options,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import blobs_table, composition_states_table, interpretation_events_table, prompt_file_approvals_table
from elspeth.web.sessions.prompt_file_approval import PromptApprovalTrigger, _blob_record, _read_custody_bytes
from elspeth.web.sessions.prompt_file_review import PromptFileReviewStale
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.state_envelope import unwrap_state_column
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs.test_routes import TestIDORProtection as _IDORFixture
from tests.unit.web.blobs.test_routes import _create_session, _make_app, _upload_blob
from tests.unit.web.composer import test_blob_inline_tools as blob_tools


def _bound(
    client: SyncASGITestClient,
    session_id: str,
    blob_id: str,
    content_sha256: str,
    service,
    *,
    mixed: bool = False,
    shared: bool = False,
    encoding: str | None = None,
) -> None:
    marker: dict[str, str] = {"mode": "inline_content", "blob_ref": blob_id, "sha256": content_sha256}
    if encoding is not None:
        marker["encoding"] = encoding
    nodes = [{"id": "first", "plugin": "llm", "options": {"prompt_template": marker}}]
    if shared:
        nodes.append({"id": "second", "plugin": "llm", "options": {"system_prompt": marker}})
    data = CompositionStateData(
        nodes=nodes,
        sources={"source": {"options": {"blob_ref": blob_id}}} if mixed else None,
        is_valid=True,
    )
    asyncio.run(service.save_composition_state(UUID(session_id), data, provenance="session_seed"))


def _save(client: SyncASGITestClient, session_id: str, blob_id: str, content: str, expected: str | None):
    return client.put(
        f"/api/sessions/{session_id}/blobs/{blob_id}/prompt-text",
        json={"content": content, "expected_sha256": expected},
    )


def test_owner_save_records_its_binding_inside_metadata_transaction(tmp_path) -> None:
    client, _service, blob_service, session_id, blob, _second, _events = _live_prompt_setup(tmp_path)
    response = _save(client, session_id, blob["id"], "after\n", blob["content_hash"])
    assert response.status_code == 200, response.text
    assert response.json() == {"changed": True, "content_sha256": content_hash(b"after\n")}
    stored = client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content")
    assert stored.content == b"after\n"
    assert stored.headers["X-Content-SHA256"] == content_hash(stored.content)
    with blob_service._engine.connect() as conn:
        approval = conn.execute(select(prompt_file_approvals_table)).one()
        nodes = conn.execute(
            select(composition_states_table.c.nodes)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(composition_states_table.c.version.desc())
            .limit(1)
        ).scalar_one()
    assert unwrap_state_column(nodes)[0]["options"]["prompt_template"]["sha256"] == approval.content_sha256
    assert (approval.node_id, approval.field_path, approval.trigger, approval.content_sha256) == (
        "first",
        "prompt_template",
        PromptApprovalTrigger.EDITOR_SAVE.value,
        content_hash(b"after\n"),
    )


@pytest.mark.parametrize(
    ("bound", "code"),
    [
        ({"shared": True}, "prompt_file_shared_binding"),
        ({"encoding": "latin-1"}, "prompt_file_encoding_unsupported"),
        ({"encoding": "utf-16"}, "prompt_file_encoding_unsupported"),
        ({"encoding": "utf-8-sig"}, "prompt_file_encoding_unsupported"),
    ],
)
def test_save_refuses_what_the_user_cannot_see_or_would_not_run(tmp_path, bound, code) -> None:
    """A save approves only the one prompt on screen, read as the UTF-8 text the user typed.

    Shared: a second binding (here another node's system prompt, as an assistant re-point would
    leave it) must keep its own review. Encoding: a latin-1/utf-16 prompt would decode different
    text from the saved UTF-8 bytes while the approved hash still matched.
    """
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before\n", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service, **bound)
    response = _save(client, session_id, blob["id"], "caf\u00e9\n", blob["content_hash"])

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == code
    with service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []
    assert client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before\n"


def test_save_refuses_node_with_two_prompt_files(tmp_path: Path) -> None:
    client, service, blob_service, session_id, first, _other, _events = _live_prompt_setup(tmp_path)
    second = _upload_blob(client, session_id, b"second", "second.txt", "text/plain")
    current = asyncio.run(service.get_current_state(UUID(session_id)))
    assert current is not None
    data = state_from_record(current).to_dict()
    data["nodes"][0]["options"]["system_prompt"] = {"blob_ref": second["id"], "mode": "inline_content", "sha256": second["content_hash"]}
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=data["sources"],
                    nodes=data["nodes"],
                    edges=data["edges"],
                    outputs=data["outputs"],
                    metadata_=data["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
    before = asyncio.run(service.get_current_state(UUID(session_id)))
    assert before is not None
    response = _save(client, session_id, first["id"], "edited", first["content_hash"])
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "prompt_file_node_multi_binding"
    assert client.get(f"/api/sessions/{session_id}/blobs/{first['id']}/content").content == b"first before"
    with blob_service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []
        assert conn.execute(select(blobs_table.c.content_hash).where(blobs_table.c.id == first["id"])).scalar_one() == first["content_hash"]
    after = asyncio.run(service.get_current_state(UUID(session_id)))
    assert after is not None and after.id == before.id


@pytest.mark.parametrize("body_extra", [{}, {"expected_sha256": None}])
def test_save_without_expected_hash_is_refused_and_writes_nothing(tmp_path, body_extra) -> None:
    """No blind overwrite: the client must name the content it started editing."""
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before\n", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    response = client.put(
        f"/api/sessions/{session_id}/blobs/{blob['id']}/prompt-text",
        json={"content": "after\n", **body_extra},
    )

    assert response.status_code == 422, response.text
    with service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []
    assert client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before\n"


def test_failed_approval_rolls_back_bytes_and_approval_row(tmp_path, monkeypatch) -> None:
    app, service, blob_service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    with blob_service._engine.begin() as conn:
        conn.execute(text("CREATE TABLE prompt_save_probe (hash TEXT NOT NULL)"))

    def fail_after_write(conn, request, *, now):
        conn.execute(text("INSERT INTO prompt_save_probe (hash) VALUES (:hash)"), {"hash": request.content_sha256})
        raise RuntimeError("approval failed")

    # Fault injection proves the byte write and approval share one rollback boundary.
    monkeypatch.setattr(routes, "record_prompt_file_approval", fail_after_write, raising=False)
    with pytest.raises(RuntimeError, match="approval failed"):
        _save(client, session_id, blob["id"], "after", blob["content_hash"])
    with blob_service._engine.begin() as conn:
        assert conn.execute(select(blobs_table.c.content_hash).where(blobs_table.c.id == blob["id"])).scalar_one() == blob["content_hash"]
        assert conn.execute(text("SELECT COUNT(*) FROM prompt_save_probe")).scalar_one() == 0
        assert conn.execute(
            select(composition_states_table.c.version).where(composition_states_table.c.session_id == session_id)
        ).scalars().all() == [1]
    assert client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before"


def test_stale_and_identical_saves_do_not_approve(tmp_path) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    stale = _save(client, session_id, blob["id"], "after", "f" * 64)
    assert stale.status_code == 409
    assert stale.json()["detail"]["error_code"] == "prompt_file_stale"
    same = _save(client, session_id, blob["id"], "before", blob["content_hash"])
    assert same.status_code == 200
    assert same.json()["changed"] is False
    with service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []


def test_mixed_use_and_binary_refuse(tmp_path) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service, mixed=True)
    response = _save(client, session_id, blob["id"], "after", blob["content_hash"])
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "prompt_file_mixed_use"
    binary = _upload_blob(client, session_id, b"%PDF-1.4\n", "prompt.pdf", "application/pdf")
    response = _save(client, session_id, binary["id"], "after", binary["content_hash"])
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "prompt_file_not_text"
    with service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []


def test_oversized_text_refuses_without_approval(tmp_path) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    response = _save(client, session_id, blob["id"], "x" * (256 * 1024 + 1), blob["content_hash"])
    assert response.status_code == 413
    assert response.json()["detail"]["error_code"] == "prompt_file_too_large"
    assert client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before"
    with service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table)).all() == []


def test_non_owner_cannot_save_or_approve(tmp_path) -> None:
    alice, bob = _IDORFixture()._make_two_session_app(tmp_path)
    session_id = _create_session(alice)
    blob = _upload_blob(alice, session_id, b"before", "prompt.txt", "text/plain")
    response = _save(bob, session_id, blob["id"], "after", blob["content_hash"])
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error_code"] == "prompt_file_not_found"
    assert alice.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before"


def _live_prompt_setup(
    tmp_path: Path, *, two_nodes: bool = False
) -> tuple[Any, Any, Any, str, dict[str, Any], dict[str, Any] | None, dict[str, UUID]]:
    app, service, blob_service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    first = _upload_blob(client, session_id, b"first before", "first.txt", "text/plain")
    second = _upload_blob(client, session_id, b"second before", "second.txt", "text/plain") if two_nodes else None
    engine = blob_service._engine

    def read_blob(_context: Any, blob_id: UUID) -> tuple[Any, bytes]:
        with engine.connect() as conn:
            record = _blob_record(conn, UUID(session_id), blob_id)
        return record, _read_custody_bytes(record)

    service._inline_blob_read = read_blob
    state = blob_tools._inline_ref_state().to_dict()
    nodes = state["nodes"]
    nodes[0]["id"] = "first"
    bindings = [(nodes[0], first)]
    if second is not None:
        node_b = deepcopy(nodes[0])
        node_b["id"] = "second"
        nodes.append(node_b)
        bindings.append((node_b, second))
    for node, blob in bindings:
        options = dict(node["options"])
        options["prompt_template"] = {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}
        node["options"] = _options_with_default_prompt_template_review(node_id=node["id"], plugin="llm", options=options)
    events: dict[str, UUID] = {}
    with fenced_operation_context(engine, session_id) as operation:
        saved = asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=state["sources"],
                    nodes=nodes,
                    edges=state["edges"],
                    outputs=state["outputs"],
                    metadata_=state["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
        for node, _blob in bindings:
            draft = prompt_review_draft_from_options(node["options"])
            assert draft is not None
            event = asyncio.run(
                service.create_pending_interpretation_event(
                    session_id=UUID(session_id),
                    composition_state_id=saved.id,
                    affected_node_id=node["id"],
                    tool_call_id=f"test-{node['id']}",
                    user_term=f"llm_prompt_template:{node['id']}",
                    kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
                    llm_draft=draft,
                    model_identifier="test-model",
                    model_version="1",
                    provider="test-provider",
                    composer_skill_hash="a" * 64,
                    session_operation_context=operation,
                )
            )
            events[node["id"]] = event.id
    return client, service, blob_service, session_id, first, second, events


def _admit(service: Any, blob_service: Any, session_id: str) -> tuple[Any, ...]:
    """Exercise the real execution-service final admission check against live services."""
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    execution = ExecutionServiceImpl.__new__(ExecutionServiceImpl)
    execution._blob_service = blob_service
    execution._session_service = service
    execution._call_async = asyncio.run
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        return execution._assert_state_prompt_file_approvals(
            state_from_record(record), session_id=UUID(session_id), session_operation_context=operation
        )


def _review_state(service: Any, session_id: str) -> tuple[dict[str, str], Any]:
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    state = state_from_record(record)
    requirements = {
        node.id: next(
            requirement["status"]
            for requirement in node.options["interpretation_requirements"]
            if requirement["kind"] == "llm_prompt_template"
        )
        for node in state.nodes
        if node.plugin == "llm"
    }
    return requirements, materialize_state_for_execution(state)


def _approve_card(service: Any, blob_service: Any, session_id: str, event_id: UUID) -> None:
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        resolved, _state = asyncio.run(
            service.resolve_interpretation_event(
                session_id=UUID(session_id),
                event_id=event_id,
                choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
                amended_value=None,
                actor="user:alice",
                session_operation_context=operation,
            )
        )
    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED


def test_save_admits_then_agent_edit_requires_review(tmp_path: Path) -> None:
    client, service, blob_service, session_id, first, _second, events = _live_prompt_setup(tmp_path)
    assert events["first"]
    assert _review_state(service, session_id)[0]["first"] == "pending"
    saved = _save(client, session_id, first["id"], "owner-edited prompt", first["content_hash"])
    assert saved.status_code == 200, saved.text
    requirements, materialized = _review_state(service, session_id)
    assert requirements["first"] == "resolved"
    assert not isinstance(materialized, InterpretationReviewPending) or all(
        site.kind is not InterpretationKind.LLM_PROMPT_TEMPLATE for site in materialized.sites
    )
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]

    new_blob = _upload_blob(client, session_id, b"agent replacement prompt", "replacement.txt", "text/plain")
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    state = state_from_record(record)
    from tests.unit.web.composer.test_blob_inline_tools import _catalog, execute_tool

    with fenced_operation_context(blob_service._engine, session_id) as operation:
        refused = execute_tool(
            "update_blob",
            {"blob_id": first["id"], "content": "agent overwrite"},
            state,
            _catalog(),
            data_dir=str(tmp_path),
            session_engine=blob_service._engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=service.session_operation_authority,
            user_message_id="agent-message",
            user_message_content="agent overwrite",
        )
        assert refused.success is False
        assert refused.validation.errors[0].error_code == "blob_bound_update"

        wired = execute_tool(
            "wire_blob_inline_ref",
            {"field_path": "node:first.options.prompt_template", "blob_id": new_blob["id"]},
            state,
            _catalog(),
            data_dir=str(tmp_path),
            session_engine=blob_service._engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=service.session_operation_authority,
        )
        assert wired.success, wired.to_dict()
        next_node = wired.updated_state.nodes[0]
        next_state = wired.updated_state.to_dict()
        saved_state = asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=next_state["sources"],
                    nodes=next_state["nodes"],
                    edges=next_state["edges"],
                    outputs=next_state["outputs"],
                    metadata_=next_state["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
        draft = prompt_review_draft_from_options(next_node.options)
        assert draft is not None
        new_event = asyncio.run(
            service.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=saved_state.id,
                affected_node_id="first",
                tool_call_id="agent-repoint",
                user_term="llm_prompt_template:first",
                kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
                llm_draft=draft,
                model_identifier="test-model",
                model_version="1",
                provider="test-provider",
                composer_skill_hash="a" * 64,
                session_operation_context=operation,
            )
        )
    with blob_service._engine.connect() as conn:
        pending = conn.execute(
            select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == str(new_event.id))
        ).scalar_one()
    assert pending == "pending"
    with pytest.raises(PromptFileReviewStale) as refusal:
        _admit(service, blob_service, session_id)
    assert refusal.value.error_code == "prompt_file_review_stale"
    _approve_card(service, blob_service, session_id, new_event.id)
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def test_approved_unbound_blob_can_be_deleted_with_evidence_retained(tmp_path: Path) -> None:
    client, service, blob_service, session_id, first, _second, _events = _live_prompt_setup(tmp_path)
    assert _save(client, session_id, first["id"], "approved edit", first["content_hash"]).status_code == 200
    replacement = _upload_blob(client, session_id, b"replacement", "replacement.txt", "text/plain")
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    state = state_from_record(record)
    from tests.unit.web.composer.test_blob_inline_tools import _catalog, execute_tool

    with fenced_operation_context(blob_service._engine, session_id) as operation:
        wired = execute_tool(
            "wire_blob_inline_ref",
            {"field_path": "node:first.options.prompt_template", "blob_id": replacement["id"]},
            state,
            _catalog(),
            data_dir=str(tmp_path),
            session_engine=blob_service._engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=service.session_operation_authority,
        )
        assert wired.success, wired.to_dict()
        next_state = wired.updated_state.to_dict()
        asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=next_state["sources"],
                    nodes=next_state["nodes"],
                    edges=next_state["edges"],
                    outputs=next_state["outputs"],
                    metadata_=next_state["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
        deleted = execute_tool(
            "delete_blob",
            {"blob_id": first["id"]},
            wired.updated_state,
            _catalog(),
            data_dir=str(tmp_path),
            session_engine=blob_service._engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=service.session_operation_authority,
        )
        assert deleted.success, deleted.to_dict()
    with blob_service._engine.connect() as conn:
        assert conn.execute(select(blobs_table.c.id).where(blobs_table.c.id == first["id"])).one_or_none() is None
        rows = conn.execute(select(prompt_file_approvals_table).where(prompt_file_approvals_table.c.blob_id == first["id"])).all()
    assert len(rows) == 1 and rows[0].content_sha256 == content_hash(b"approved edit")


def test_editor_save_resolves_only_its_node_card(tmp_path: Path) -> None:
    client, service, blob_service, session_id, first, second, events = _live_prompt_setup(tmp_path, two_nodes=True)
    assert second is not None
    response = _save(client, session_id, first["id"], "first owner edit", first["content_hash"])
    assert response.status_code == 200, response.text
    with blob_service._engine.connect() as conn:
        rows = conn.execute(select(prompt_file_approvals_table).where(prompt_file_approvals_table.c.session_id == session_id)).all()
        cards = conn.execute(
            select(interpretation_events_table.c.affected_node_id, interpretation_events_table.c.choice).where(
                interpretation_events_table.c.id.in_([str(events["first"]), str(events["second"])]),
            )
        ).all()
    assert [(row.node_id, row.field_path, row.trigger) for row in rows] == [("first", "prompt_template", "editor_save")]
    assert dict(cards) == {"first": "superseded", "second": "pending"}
    requirements, materialized = _review_state(service, session_id)
    assert requirements == {"first": "resolved", "second": "pending"}
    assert isinstance(materialized, InterpretationReviewPending)
    assert [(site.component_id, site.kind) for site in materialized.sites if site.kind is InterpretationKind.LLM_PROMPT_TEMPLATE] == [
        ("second", InterpretationKind.LLM_PROMPT_TEMPLATE)
    ]
    with pytest.raises(PromptFileReviewStale) as refusal:
        _admit(service, blob_service, session_id)
    assert refusal.value.error_code == "prompt_file_review_stale"
    assert "second" in str(refusal.value)
    assert "first" not in str(refusal.value)


def test_editor_save_keeps_card_pending_for_inline_query(tmp_path: Path) -> None:
    client, service, blob_service, session_id, first, _second, events = _live_prompt_setup(tmp_path)
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    data = state_from_record(record).to_dict()
    data["nodes"][0]["options"]["queries"] = {"q": {"input_fields": {}, "template": "inline query"}}
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=data["sources"],
                    nodes=data["nodes"],
                    edges=data["edges"],
                    outputs=data["outputs"],
                    metadata_=data["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
    response = _save(client, session_id, first["id"], "edited file", first["content_hash"])
    assert response.status_code == 200, response.text
    with blob_service._engine.connect() as conn:
        assert conn.execute(select(prompt_file_approvals_table.c.trigger)).scalar_one() == "editor_save"
        assert (
            conn.execute(
                select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == str(events["first"]))
            ).scalar_one()
            == "pending"
        )
    assert _review_state(service, session_id)[0]["first"] == "pending"


def test_same_id_replacement_refuses_until_new_card_approval(tmp_path: Path) -> None:
    _client, service, blob_service, session_id, first, _second, events = _live_prompt_setup(tmp_path)
    _approve_card(service, blob_service, session_id, events["first"])
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]

    replacement = b"replacement under same blob id"
    replacement_hash = content_hash(replacement)
    with blob_service._engine.begin() as conn:
        row = conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == first["id"])).one()
        Path(row.storage_path).write_bytes(replacement)
        conn.execute(
            blobs_table.update()
            .where(blobs_table.c.id == first["id"])
            .values(
                content_hash=replacement_hash,
                size_bytes=len(replacement),
            )
        )
    # The fixture models a same-ID replacement and its marker repin. It has
    # no public mutation event, so the old approval must still fail admission.
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    state = state_from_record(record).to_dict()
    node = state["nodes"][0]
    options = dict(node["options"])
    options["prompt_template"] = {"blob_ref": first["id"], "mode": "inline_content", "sha256": replacement_hash}
    node["options"] = _options_with_default_prompt_template_review(node_id="first", plugin="llm", options=options)
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        saved = asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=state["sources"],
                    nodes=state["nodes"],
                    edges=state["edges"],
                    outputs=state["outputs"],
                    metadata_=state["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
    with pytest.raises(PromptFileReviewStale) as refusal:
        _admit(service, blob_service, session_id)
    assert refusal.value.error_code == "prompt_file_review_stale"
    assert "first" in str(refusal.value)
    with blob_service._engine.connect() as conn:
        current_card = conn.execute(
            select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == str(events["first"]))
        ).scalar_one()
    assert current_card == "accepted_as_drafted"

    # Out-of-band fixture writes do not reopen a card; stage one explicitly
    # before exercising the real card approval path.
    with fenced_operation_context(blob_service._engine, session_id) as operation:
        draft = prompt_review_draft_from_options(node["options"])
        assert draft is not None
        event = asyncio.run(
            service.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=saved.id,
                affected_node_id="first",
                tool_call_id="same-id-replacement",
                user_term="llm_prompt_template:first",
                kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
                llm_draft=draft,
                model_identifier="test-model",
                model_version="1",
                provider="test-provider",
                composer_skill_hash="a" * 64,
                session_operation_context=operation,
            )
        )
    with pytest.raises(PromptFileReviewStale):
        _admit(service, blob_service, session_id)
    _approve_card(service, blob_service, session_id, event.id)
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def test_one_approval_query_distinguishes_card_and_editor_save(tmp_path: Path) -> None:
    client, service, blob_service, session_id, first, _second, events = _live_prompt_setup(tmp_path)
    _approve_card(service, blob_service, session_id, events["first"])
    saved = _save(client, session_id, first["id"], "owner-edited prompt", first["content_hash"])
    assert saved.status_code == 200, saved.text

    query = text("""
        SELECT trigger, content_sha256, actor_user_id, approved_at
        FROM prompt_file_approvals
        WHERE session_id = :session_id AND node_id = :node_id AND field_path = :field_path
        ORDER BY approved_at, id
    """)
    with blob_service._engine.connect() as conn:
        rows = conn.execute(
            query,
            {
                "session_id": session_id,
                "node_id": "first",
                "field_path": "prompt_template",
            },
        ).all()
    assert [(row.trigger, row.content_sha256, row.actor_user_id) for row in rows] == [
        ("card", first["content_hash"], "alice"),
        ("editor_save", saved.json()["content_sha256"], "alice"),
    ]
    assert all(datetime.fromisoformat(str(row.approved_at)) for row in rows)

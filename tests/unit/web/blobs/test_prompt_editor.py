"""Owner saves approve exactly the bytes committed by blob replacement."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select, text

from elspeth.web.blobs import routes
from elspeth.web.blobs.service import content_hash
from elspeth.web.sessions.models import blobs_table, composition_states_table
from elspeth.web.sessions.prompt_file_approval import PromptApprovalTrigger
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.state_envelope import unwrap_state_column
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs.test_routes import TestIDORProtection as _IDORFixture
from tests.unit.web.blobs.test_routes import _create_session, _make_app, _upload_blob


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


def test_owner_save_records_its_binding_inside_metadata_transaction(tmp_path, monkeypatch) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before\n", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    calls = []

    def record(conn, request, *, now):
        stored = conn.execute(select(blobs_table.c.content_hash).where(blobs_table.c.id == blob["id"])).scalar_one()
        assert stored == content_hash(b"after\n")
        nodes = conn.execute(
            select(composition_states_table.c.nodes)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(composition_states_table.c.version.desc())
            .limit(1)
        ).scalar_one()
        assert unwrap_state_column(nodes)[0]["options"]["prompt_template"]["sha256"] == stored
        assert conn.in_transaction()
        calls.append((request, now))

    monkeypatch.setattr(routes, "record_prompt_file_approval", record, raising=False)
    response = _save(client, session_id, blob["id"], "after\n", blob["content_hash"])
    assert response.status_code == 200, response.text
    assert response.json() == {"changed": True, "content_sha256": content_hash(b"after\n")}
    stored = client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content")
    assert stored.content == b"after\n"
    assert stored.headers["X-Content-SHA256"] == content_hash(stored.content)
    assert [(call.node_id, call.field_path) for call, _ in calls] == [("first", "prompt_template")]
    assert all(call.trigger is PromptApprovalTrigger.EDITOR_SAVE and call.content_sha256 == content_hash(b"after\n") for call, _ in calls)


@pytest.mark.parametrize(
    ("bound", "code"),
    [
        ({"shared": True}, "prompt_file_shared_binding"),
        ({"encoding": "latin-1"}, "prompt_file_encoding_unsupported"),
        ({"encoding": "utf-16"}, "prompt_file_encoding_unsupported"),
    ],
)
def test_save_refuses_what_the_user_cannot_see_or_would_not_run(tmp_path, monkeypatch, bound, code) -> None:
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
    calls = []
    monkeypatch.setattr(routes, "record_prompt_file_approval", lambda conn, request, *, now: calls.append(request), raising=False)

    response = _save(client, session_id, blob["id"], "caf\u00e9\n", blob["content_hash"])

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == code
    assert calls == []
    assert client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before\n"


@pytest.mark.parametrize("body_extra", [{}, {"expected_sha256": None}])
def test_save_without_expected_hash_is_refused_and_writes_nothing(tmp_path, monkeypatch, body_extra) -> None:
    """No blind overwrite: the client must name the content it started editing."""
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before\n", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    calls = []
    monkeypatch.setattr(routes, "record_prompt_file_approval", lambda conn, request, *, now: calls.append(request), raising=False)

    response = client.put(
        f"/api/sessions/{session_id}/blobs/{blob['id']}/prompt-text",
        json={"content": "after\n", **body_extra},
    )

    assert response.status_code == 422, response.text
    assert calls == []
    assert client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before\n"


def test_utf8_sig_binding_saves(tmp_path, monkeypatch) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before\n", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service, encoding="utf-8-sig")
    monkeypatch.setattr(routes, "record_prompt_file_approval", lambda conn, request, *, now: None, raising=False)

    assert _save(client, session_id, blob["id"], "after\n", blob["content_hash"]).status_code == 200


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


def test_stale_and_identical_saves_do_not_approve(tmp_path, monkeypatch) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    calls = []
    monkeypatch.setattr(routes, "record_prompt_file_approval", lambda *args, **kwargs: calls.append(args), raising=False)
    stale = _save(client, session_id, blob["id"], "after", "f" * 64)
    assert stale.status_code == 409
    assert stale.json()["detail"]["error_code"] == "prompt_file_stale"
    same = _save(client, session_id, blob["id"], "before", blob["content_hash"])
    assert same.status_code == 200
    assert same.json()["changed"] is False
    assert calls == []


def test_mixed_use_and_binary_refuse(tmp_path, monkeypatch) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service, mixed=True)
    calls = []
    monkeypatch.setattr(routes, "record_prompt_file_approval", lambda *args, **kwargs: calls.append(args), raising=False)
    response = _save(client, session_id, blob["id"], "after", blob["content_hash"])
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "prompt_file_mixed_use"
    binary = _upload_blob(client, session_id, b"%PDF-1.4\n", "prompt.pdf", "application/pdf")
    response = _save(client, session_id, binary["id"], "after", binary["content_hash"])
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "prompt_file_not_text"
    assert calls == []


def test_oversized_text_refuses_without_approval(tmp_path, monkeypatch) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "prompt.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service)
    calls = []
    monkeypatch.setattr(routes, "record_prompt_file_approval", lambda *args, **kwargs: calls.append(args), raising=False)
    response = _save(client, session_id, blob["id"], "x" * (256 * 1024 + 1), blob["content_hash"])
    assert response.status_code == 413
    assert response.json()["detail"]["error_code"] == "prompt_file_too_large"
    assert client.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before"
    assert calls == []


def test_non_owner_cannot_save_or_approve(tmp_path, monkeypatch) -> None:
    alice, bob = _IDORFixture()._make_two_session_app(tmp_path)
    session_id = _create_session(alice)
    blob = _upload_blob(alice, session_id, b"before", "prompt.txt", "text/plain")
    calls = []
    monkeypatch.setattr(routes, "record_prompt_file_approval", lambda *args, **kwargs: calls.append(args), raising=False)
    response = _save(bob, session_id, blob["id"], "after", blob["content_hash"])
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error_code"] == "prompt_file_not_found"
    assert alice.get(f"/api/sessions/{session_id}/blobs/{blob['id']}/content").content == b"before"
    assert calls == []


@pytest.mark.skip(reason="needs R2 record_prompt_file_approval; unskip on rebase")
def test_save_admits_then_agent_edit_requires_review() -> None:
    """R2 integration: bind, save, admit, agent update, refuse, approve, admit."""
    raise AssertionError("R2 admission fixture must be connected on rebase")

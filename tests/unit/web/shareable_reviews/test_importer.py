"""Import a shared pipeline and its frozen files into a new session.

Tom (``alice``) shares a link with import allowed; Chris (``bob``) imports it
through ``POST /api/sessions/shared/{token}/import``. Everything runs through
the real app: the share service, the payload store, the blob service, the
session service and the ordinary YAML seeder (the library-fork seam).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from structlog.testing import capture_logs

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.payload_store import IntegrityError
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.yaml_importer import MAX_RUNTIME_YAML_IMPORT_CHARS
from elspeth.web.config import WebSettings
from elspeth.web.plugin_policy.availability import RequestPluginSnapshotFactory
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.models import blobs_table, sessions_table
from elspeth.web.sessions.protocol import CompositionStateData, SessionNotFoundError
from elspeth.web.shareable_reviews import importer as importer_module
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.integration.web.conftest import _lifespan_test_client, _save_composition_state_with_compose_authority
from tests.unit.web._sync_asgi_client import SyncASGITestClient

_TOM = "alice"
_CHRIS = "bob"
# Carries a ``region`` column so the same file can also serve as the
# reference table (the one-file-bound-twice case).
_CSV_A = b"id,name,region\n1,Ada,North\n"
_CSV_REF = b"id,region\n1,North\n"
_DOC_B = b"first document\n"
_DOC_C = b"second document\n"
_INLINE_PATH = "node:lookup.options.tables.t.reference_content"
# The import refusal bodies the frontend's tests replay through the client parser.
_FRONTEND_REFUSAL_FIXTURE = Path(__file__).resolve().parents[4] / "src/elspeth/web/frontend/src/test/fixtures/shared-import-refusals.json"


@dataclass
class _Actor:
    identity_id: str = _TOM


@pytest.fixture
def actor() -> _Actor:
    return _Actor()


@pytest.fixture
def client(tmp_path: Path, actor: _Actor) -> Iterator[SyncASGITestClient]:
    from elspeth.web.app import create_app

    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough", "source:blob_rows"),
    )
    app: FastAPI = create_app(settings=settings)
    for identity_id in (_TOM, _CHRIS):
        wire_test_pipeline_user_authority(
            app,
            identity_id=identity_id,
            provider=settings.auth_provider,
            engine=app.state.session_engine,
        )

    async def _current_user() -> UserIdentity:
        return UserIdentity(user_id=actor.identity_id, username=f"{actor.identity_id}-name")

    app.dependency_overrides[get_current_user] = _current_user
    with _lifespan_test_client(app) as test_client:
        yield test_client


# ── Tom's side: build and share a graph through the real routes ─────────


def _new_session(client: SyncASGITestClient, actor: _Actor, owner: str) -> UUID:
    actor.identity_id = owner
    created = client.post("/api/sessions", json={"title": "Tom's workspace"})
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"])


def _upload(client: SyncASGITestClient, session_id: UUID, filename: str, content: bytes, mime: str) -> dict[str, Any]:
    uploaded = client.post(f"/api/sessions/{session_id}/blobs", files={"file": (filename, content, mime)})
    assert uploaded.status_code == 201, uploaded.text
    blob = uploaded.json()
    with client.app.state.session_engine.connect() as conn:
        blob["storage_path"] = conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob["id"])).scalar_one()
    return blob


def _save_state(client: SyncASGITestClient, session_id: UUID, state: dict[str, Any]) -> None:
    asyncio.run(
        _save_composition_state_with_compose_authority(
            client.app.state.session_service,
            session_id,
            CompositionStateData(
                sources=state["sources"],
                nodes=state["nodes"],
                edges=[],
                outputs=state["outputs"],
                metadata_=state["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
    )


def _marker(blob: dict[str, Any]) -> dict[str, str]:
    return {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}


def _rows_entry(blob: dict[str, Any]) -> dict[str, Any]:
    return {
        "blob_id": blob["id"],
        "payload_ref": blob["content_hash"],
        "filename": blob["filename"],
        "mime_type": blob["mime_type"],
        "size_bytes": blob["size_bytes"],
    }


def _graph(
    client: SyncASGITestClient,
    session_id: UUID,
    *,
    name: str,
    csv_blob: dict[str, Any],
    reference_blob: dict[str, Any],
    row_blobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A csv source + reference_join node, optionally with a blob_rows source."""
    outputs_dir = client.app.state.settings.data_dir / "outputs" / str(session_id)
    blobs_dir = client.app.state.settings.data_dir / "blobs" / str(session_id)
    sources: dict[str, Any] = {
        "customers": {
            "plugin": "csv",
            "on_success": "lookup_in",
            "on_validation_failure": "discard",
            "options": {"path": csv_blob["storage_path"], "blob_ref": csv_blob["id"], "schema": {"mode": "observed"}},
        }
    }
    outputs: list[dict[str, Any]] = [
        {
            "name": "main",
            "plugin": "json",
            "options": {"path": str(outputs_dir / "main.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            "on_write_failure": "discard",
        }
    ]
    if row_blobs is not None:
        sources["docs"] = {
            "plugin": "blob_rows",
            "on_success": "docs_out",
            "on_validation_failure": "discard",
            "options": {"blobs": [_rows_entry(blob) for blob in row_blobs], "schema": {"mode": "observed"}},
        }
        outputs.append(
            {
                "name": "docs_out",
                "plugin": "json",
                "options": {"path": str(blobs_dir / "out" / "docs.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        )
    return {
        "metadata": {"name": name, "description": ""},
        "sources": sources,
        "nodes": [
            {
                "id": "lookup",
                "node_type": "transform",
                "plugin": "reference_join",
                "input": "lookup_in",
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "tables": {"t": {"reference_format": "csv", "reference_content": _marker(reference_blob)}},
                    "lookups": [
                        {"table": "t", "key_field": "id", "reference_key_name": "id", "output": {"region": "ref['region']"}},
                    ],
                },
            }
        ],
        "outputs": outputs,
    }


def _share(client: SyncASGITestClient, session_id: UUID, *, allow_import: bool = True) -> dict[str, Any]:
    body: dict[str, object] = {"allow_import": allow_import}
    if allow_import:
        preview = client.get(f"/api/sessions/{session_id}/shareable-files")
        assert preview.status_code == 200, preview.text
        body["file_manifest_fingerprint"] = preview.json()["manifest_fingerprint"]
    marked = client.post(f"/api/sessions/{session_id}/mark-ready-for-review", json=body)
    assert marked.status_code == 200, marked.text
    return marked.json()


@dataclass
class _Shared:
    session_id: UUID
    token: str
    payload_digest: str
    blobs: dict[str, dict[str, Any]]


def _tom_shares(
    client: SyncASGITestClient,
    actor: _Actor,
    *,
    name: str = "Customer regions",
    with_rows: bool = False,
    same_file_twice: bool = False,
    allow_import: bool = True,
) -> _Shared:
    session_id = _new_session(client, actor, _TOM)
    blobs = {"a": _upload(client, session_id, "customers.csv", _CSV_A, "text/csv")}
    if same_file_twice:
        blobs["ref"] = blobs["a"]
    else:
        blobs["ref"] = _upload(client, session_id, "regions.csv", _CSV_REF, "text/csv")
    row_blobs = None
    if with_rows:
        blobs["b"] = _upload(client, session_id, "b.txt", _DOC_B, "text/plain")
        blobs["c"] = _upload(client, session_id, "c.txt", _DOC_C, "text/plain")
        row_blobs = [blobs["b"], blobs["c"]]
    _save_state(
        client,
        session_id,
        _graph(client, session_id, name=name, csv_blob=blobs["a"], reference_blob=blobs["ref"], row_blobs=row_blobs),
    )
    shared = _share(client, session_id, allow_import=allow_import)
    return _Shared(session_id=session_id, token=shared["token"], payload_digest=shared["payload_digest"], blobs=blobs)


# ── Chris's side ────────────────────────────────────────────────────────


def _import(client: SyncASGITestClient, actor: _Actor, token: str, *, as_user: str = _CHRIS) -> Any:
    actor.identity_id = as_user
    return client.post(f"/api/sessions/shared/{token}/import")


def _session_ids(client: SyncASGITestClient, actor: _Actor, owner: str) -> set[str]:
    """The sessions ``owner`` sees (archived sessions are not listed)."""
    actor.identity_id = owner
    listed = client.get("/api/sessions")
    assert listed.status_code == 200, listed.text
    return {session["id"] for session in listed.json()}


@pytest.fixture
def created_sessions(client: SyncASGITestClient, monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    """Ids of every session the session service creates from here on."""
    service = client.app.state.session_service
    original = service.create_session
    created: list[UUID] = []

    async def _create_session(user_id: str, title: str, auth_provider_type: str) -> Any:
        record = await original(user_id, title, auth_provider_type)
        created.append(record.id)
        return record

    monkeypatch.setattr(service, "create_session", _create_session)
    return created


def _stored_session_ids(client: SyncASGITestClient, owner: str) -> set[str]:
    """Every session row ``owner`` has, archived ones included."""
    with client.app.state.session_engine.connect() as conn:
        return set(conn.execute(select(sessions_table.c.id).where(sessions_table.c.user_id == owner)).scalars())


def _assert_one_session_created_and_archived(
    client: SyncASGITestClient, actor: _Actor, created: list[UUID], *, listed_before: set[str]
) -> None:
    assert len(created) == 1
    with pytest.raises(SessionNotFoundError):
        asyncio.run(client.app.state.session_service.get_session(created[0]))
    assert _session_ids(client, actor, _CHRIS) == listed_before


def _assert_matches_frontend_fixture(response: Any, case: str) -> None:
    """The refusal body is byte-for-byte the one the import page's tests replay.

    ``SharedInspectView.test.tsx`` feeds each recorded body through the real
    client parser, so a change to the server's shape fails here, naming the
    fixture, instead of leaving the frontend tests green against a body the
    server no longer sends. Only per-request values are normalised: the
    request id and the plugin-snapshot fingerprint.
    """
    recorded = json.loads(_FRONTEND_REFUSAL_FIXTURE.read_text(encoding="utf-8"))[case]
    body = response.json()
    detail = body["detail"]
    if isinstance(detail, dict):
        for volatile in ("request_id", "snapshot_fingerprint"):
            if volatile in detail:
                assert isinstance(detail[volatile], str)
                assert detail[volatile]
                detail[volatile] = recorded["body"]["detail"][volatile]
    assert {"status": response.status_code, "statusText": response.reason_phrase, "body": body} == recorded


def _session_blobs(client: SyncASGITestClient, session_id: str) -> list[dict[str, Any]]:
    with client.app.state.session_engine.connect() as conn:
        rows = conn.execute(select(blobs_table).where(blobs_table.c.session_id == session_id)).mappings().all()
    return [dict(row) for row in rows]


def _current_state(client: SyncASGITestClient, session_id: str) -> Any:
    record = asyncio.run(client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    return record


def test_import_copies_files_and_rebinds_every_binding(client: SyncASGITestClient, actor: _Actor) -> None:
    shared = _tom_shares(client, actor, with_rows=True)

    response = _import(client, actor, shared.token)

    assert response.status_code == 201, response.text
    body = response.json()
    new_session_id = body["session_id"]
    assert new_session_id != str(shared.session_id)
    session = asyncio.run(client.app.state.session_service.get_session(UUID(new_session_id)))
    assert (session.user_id, session.title) == (_CHRIS, "Copy of Customer regions")

    copies = {row["content_hash"]: row for row in _session_blobs(client, new_session_id)}
    originals = {key: blob["content_hash"] for key, blob in shared.blobs.items()}
    assert set(copies) == set(originals.values())
    for key, content_hash in originals.items():
        assert copies[content_hash]["id"] != shared.blobs[key]["id"]
        assert copies[content_hash]["creation_modality"] == "verbatim"

    record = _current_state(client, new_session_id)
    assert str(record.id) == body["state_id"]
    customers = record.sources["customers"]["options"]
    assert customers["blob_ref"] == copies[originals["a"]]["id"]
    assert customers["path"] == copies[originals["a"]]["storage_path"]
    assert [entry["blob_id"] for entry in record.sources["docs"]["options"]["blobs"]] == [
        copies[originals["b"]]["id"],
        copies[originals["c"]]["id"],
    ]
    assert record.nodes[0]["options"]["tables"]["t"]["reference_content"] == {
        "blob_ref": copies[originals["ref"]]["id"],
        "mode": "inline_content",
        "sha256": originals["ref"],
    }
    assert deep_thaw(record.composer_meta["shared_import"]) == {
        "payload_digest": shared.payload_digest,
        "shared_by_user_id": _TOM,
        "shared_by_username": f"{_TOM}-name",
        "shared_from_session_id": str(shared.session_id),
        "shared_from_state_id": str(_current_state(client, str(shared.session_id)).id),
        "files": sorted(
            ({"sha256": content_hash, "blob_id": row["id"]} for content_hash, row in copies.items()),
            key=lambda item: item["sha256"],
        ),
    }
    serialized = json.dumps(
        {"sources": record.sources, "nodes": record.nodes, "outputs": record.outputs},
        default=str,
    )
    assert str(shared.session_id) not in serialized
    for blob in shared.blobs.values():
        assert blob["id"] not in serialized
        assert blob["storage_path"] not in serialized


def test_import_rebases_sink_paths_under_the_recipients_session(client: SyncASGITestClient, actor: _Actor) -> None:
    from elspeth.web.composer.tools._common import _validate_sink_path

    shared = _tom_shares(client, actor, with_rows=True)

    response = _import(client, actor, shared.token)

    assert response.status_code == 201, response.text
    new_session_id = response.json()["session_id"]
    data_dir = client.app.state.settings.data_dir.resolve()
    record = _current_state(client, new_session_id)
    outputs = {output["name"]: deep_thaw(output["options"]) for output in record.outputs}
    assert outputs["main"]["path"] == str(data_dir / "outputs" / new_session_id / "main.jsonl")
    assert outputs["docs_out"]["path"] == str(data_dir / "blobs" / new_session_id / "out" / "docs.jsonl")
    for options in outputs.values():
        # The re-authored path passes the composer's sink-path guard for the
        # recipient's session, unchanged.
        assert _validate_sink_path(options, str(data_dir), session_id=new_session_id) is None
    serialized = json.dumps({"sources": record.sources, "nodes": record.nodes, "outputs": record.outputs}, default=str)
    assert str(shared.session_id) not in serialized
    # Chris's copy validates as it stands: no sink path needs setting by hand.
    validated = client.post(f"/api/sessions/{new_session_id}/validate")
    assert validated.status_code == 200, validated.text
    assert validated.json()["is_valid"] is True, validated.json()["errors"]


@pytest.mark.parametrize(
    "relative_path",
    [
        "../escape.jsonl",
        "/etc/passwd",
        "",
        "nested/../../x",
        pytest.param("sk-" + "a" * 24 + ".jsonl", id="credential-shaped"),
        pytest.param("a\nINFO fake log line.jsonl", id="control-character"),
        pytest.param("a" * 1025, id="longer-than-1024"),
    ],
)
def test_import_tampered_sink_path_is_an_integrity_error_and_creates_no_session(
    client: SyncASGITestClient,
    actor: _Actor,
    created_sessions: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    """The snapshot is server-written; a sink path that would leave the recipient's folder is corruption."""
    shared = _tom_shares(client, actor)
    service = client.app.state.shareable_review_service
    original_load = service.load_snapshot

    async def _load_snapshot(*, token: str) -> Any:
        payload, snapshot = await original_load(token=token)
        assert snapshot.sink_paths, "the shared graph must carry a sink path to tamper with"
        tampered = replace(snapshot.sink_paths[0], relative_path=relative_path)
        return payload, replace(snapshot, sink_paths=(tampered, *snapshot.sink_paths[1:]))

    monkeypatch.setattr(service, "load_snapshot", _load_snapshot)
    created_sessions.clear()
    stored_before = _stored_session_ids(client, _CHRIS)

    with capture_logs() as logs:
        response = _import(client, actor, shared.token)

    # The app's fail-closed AuditIntegrityError handler answers 500.
    assert response.status_code == 500, response.text
    assert response.json()["error_type"] == "audit_integrity_error"
    refusals = [log["message"] for log in logs if log["event"] == "http_audit_integrity_error"]
    assert len(refusals) == 1
    assert refusals[0].startswith("share manifest sink path for output 'main'")
    assert created_sessions == []
    assert _stored_session_ids(client, _CHRIS) == stored_before


def test_import_one_file_bound_twice_creates_one_blob(client: SyncASGITestClient, actor: _Actor) -> None:
    shared = _tom_shares(client, actor, same_file_twice=True)

    response = _import(client, actor, shared.token)

    assert response.status_code == 201, response.text
    copies = _session_blobs(client, response.json()["session_id"])
    assert len(copies) == 1
    record = _current_state(client, response.json()["session_id"])
    assert record.sources["customers"]["options"]["blob_ref"] == copies[0]["id"]
    assert record.nodes[0]["options"]["tables"]["t"]["reference_content"]["blob_ref"] == copies[0]["id"]
    assert deep_thaw(record.composer_meta["shared_import"]["files"]) == [{"sha256": copies[0]["content_hash"], "blob_id": copies[0]["id"]}]


def test_import_after_sharer_deletes_file_still_succeeds(client: SyncASGITestClient, actor: _Actor) -> None:
    shared = _tom_shares(client, actor)
    actor.identity_id = _TOM
    asyncio.run(client.app.state.session_service.archive_session(shared.session_id))

    response = _import(client, actor, shared.token)

    assert response.status_code == 201, response.text
    copies = {row["content_hash"] for row in _session_blobs(client, response.json()["session_id"])}
    assert copies == {shared.blobs["a"]["content_hash"], shared.blobs["ref"]["content_hash"]}


def test_sharer_can_import_own_link(client: SyncASGITestClient, actor: _Actor) -> None:
    shared = _tom_shares(client, actor)

    response = _import(client, actor, shared.token, as_user=_TOM)

    assert response.status_code == 201, response.text
    new_session_id = response.json()["session_id"]
    assert new_session_id != str(shared.session_id)
    session = asyncio.run(client.app.state.session_service.get_session(UUID(new_session_id)))
    assert session.user_id == _TOM


def test_import_view_only_link_is_403_and_creates_no_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID]
) -> None:
    shared = _tom_shares(client, actor, allow_import=False)
    created_sessions.clear()
    stored_before = _stored_session_ids(client, _CHRIS)

    response = _import(client, actor, shared.token)

    assert response.status_code == 403, response.text
    assert created_sessions == []
    assert _stored_session_ids(client, _CHRIS) == stored_before


def test_import_missing_frozen_file_is_404_and_creates_no_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID]
) -> None:
    shared = _tom_shares(client, actor)
    assert client.app.state.payload_store.delete(shared.blobs["ref"]["content_hash"])
    created_sessions.clear()

    response = _import(client, actor, shared.token)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Shared snapshot is no longer available; ask the sender for a fresh link"
    assert created_sessions == []


def test_import_corrupt_frozen_file_raises_and_creates_no_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _tom_shares(client, actor)
    payload_store = client.app.state.payload_store
    original_retrieve = payload_store.retrieve
    corrupt = shared.blobs["ref"]["content_hash"]

    def _retrieve(content_hash: str) -> bytes:
        if content_hash == corrupt:
            raise IntegrityError(f"Payload integrity check failed: expected {content_hash}")
        return original_retrieve(content_hash)

    monkeypatch.setattr(payload_store, "retrieve", _retrieve)
    created_sessions.clear()

    with pytest.raises(IntegrityError):
        _import(client, actor, shared.token)

    assert created_sessions == []


def test_import_oversized_yaml_is_refused_and_creates_no_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public YAML over the import cap is refused before any session exists."""
    shared = _tom_shares(client, actor)
    oversized = "#" * (MAX_RUNTIME_YAML_IMPORT_CHARS + 1)
    monkeypatch.setattr(importer_module, "generate_public_yaml", lambda _state: oversized)
    created_sessions.clear()
    stored_before = _stored_session_ids(client, _CHRIS)

    response = _import(client, actor, shared.token)

    assert response.status_code == 422, response.text
    _assert_matches_frontend_fixture(response, "yaml_too_large")
    assert created_sessions == []
    assert _stored_session_ids(client, _CHRIS) == stored_before


def test_import_invalid_token_is_401(client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID]) -> None:
    response = _import(client, actor, "not-a-real-token")

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Invalid or expired share token"
    assert created_sessions == []


def test_import_without_seeder_is_503_and_creates_no_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _tom_shares(client, actor)
    monkeypatch.delattr(client.app.state, "library_state_seeder")
    created_sessions.clear()

    response = _import(client, actor, shared.token)

    assert response.status_code == 503, response.text
    assert created_sessions == []


def test_import_over_quota_archives_the_new_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _tom_shares(client, actor)
    listed_before = _session_ids(client, actor, _CHRIS)
    monkeypatch.setattr(client.app.state.blob_service, "_max_storage_per_session", 1)
    created_sessions.clear()

    response = _import(client, actor, shared.token)

    assert response.status_code == 413, response.text
    _assert_one_session_created_and_archived(client, actor, created_sessions, listed_before=listed_before)


def test_import_copy_hash_mismatch_archives_the_new_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: a copy whose content hash differs from the manifest is an integrity anomaly."""
    shared = _tom_shares(client, actor)
    payload_store = client.app.state.payload_store
    original_retrieve = payload_store.retrieve
    tampered = shared.blobs["ref"]["content_hash"]

    def _retrieve(content_hash: str) -> bytes:
        content = original_retrieve(content_hash)
        return content + b"2,South\n" if content_hash == tampered else content

    monkeypatch.setattr(payload_store, "retrieve", _retrieve)
    listed_before = _session_ids(client, actor, _CHRIS)
    created_sessions.clear()

    response = _import(client, actor, shared.token)

    # The app's AuditIntegrityError handler answers 500.
    assert response.status_code == 500, response.text
    _assert_one_session_created_and_archived(client, actor, created_sessions, listed_before=listed_before)


class _FactoryWithout:
    """The app's plugin snapshot factory with one plugin made unavailable.

    Keeps both entry points of ``RequestPluginSnapshotFactory``: routes call
    it with a user, and the app's composer worker calls ``for_user_id`` on
    every scan. A bare function would serve the routes but kill the worker.
    """

    def __init__(self, wrapped: RequestPluginSnapshotFactory, removed: PluginId) -> None:
        self._wrapped = wrapped
        self._removed = removed

    def __call__(self, user: UserIdentity) -> PluginAvailabilitySnapshot:
        return self.for_user_id(user.user_id)

    def for_user_id(self, user_id: str) -> PluginAvailabilitySnapshot:
        snapshot = self._wrapped.for_user_id(user_id)
        return replace(snapshot, available=snapshot.available - {self._removed})


def test_import_seed_failure_archives_the_new_session(
    client: SyncASGITestClient, actor: _Actor, created_sessions: list[UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _tom_shares(client, actor, with_rows=True)
    listed_before = _session_ids(client, actor, _CHRIS)
    created_sessions.clear()
    blob_rows = PluginId("source", "blob_rows")
    monkeypatch.setattr(
        client.app.state,
        "plugin_snapshot_factory",
        _FactoryWithout(client.app.state.plugin_snapshot_factory, blob_rows),
    )
    # The app's composer worker resolves the factory by principal id on every
    # scan; a stand-in it cannot use kills the worker and drains the app.
    assert blob_rows not in composer_app_services(client.app).plugin_snapshot_for_user_id(_CHRIS).available

    response = _import(client, actor, shared.token)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["plugin_id"] == str(blob_rows)
    _assert_matches_frontend_fixture(response, "plugin_policy")
    _assert_one_session_created_and_archived(client, actor, created_sessions, listed_before=listed_before)


def test_import_blank_pipeline_name_uses_fallback_title(client: SyncASGITestClient, actor: _Actor) -> None:
    shared = _tom_shares(client, actor, name="  ")

    response = _import(client, actor, shared.token)

    assert response.status_code == 201, response.text
    session = asyncio.run(client.app.state.session_service.get_session(UUID(response.json()["session_id"])))
    assert session.title == "Copy of shared pipeline"

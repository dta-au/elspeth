"""YAML export validates uploaded prompt bytes under its existing lease."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import (
    ValidationResult,
    _make_app,
    _ready_blob_record,
    _save_test_composition_state,
)
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines

from elspeth.contracts.blobs import BlobNotFoundError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes import _helpers
from elspeth.web.sessions.routes.composer import state as state_routes


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["ready", "missing", "stale_hash", "not_ready", "oversized", "corrupt_bytes", "foreign_session"])
async def test_export_uses_scoped_verified_prompt_snapshot(tmp_path: Path, monkeypatch, condition: str) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "File prompts", "local")
    blob_id = uuid4()
    content = "Original prompt\r\n{{ row.text }}\nEnd é".encode()
    digest = hashlib.sha256(content).hexdigest()
    record = replace(
        _ready_blob_record(blob_id=blob_id, session_id=session.id, storage_path="inputs/prompt.txt"),
        mime_type="text/plain",
        content_hash=digest,
        size_bytes=len(content),
    )
    if condition == "stale_hash":
        record = replace(record, content_hash="a" * 64)
    elif condition == "not_ready":
        record = replace(record, status="uploading")
    elif condition == "oversized":
        record = replace(record, size_bytes=256 * 1024 + 1)
    elif condition == "foreign_session":
        record = replace(record, session_id=uuid4())
    contexts = []
    reads = []

    def metadata(selector, context):
        assert selector == blob_id
        assert context.fence.session_id == str(session.id)
        assert context.operation_kind is SessionOperationKind.COMPOSE
        contexts.append(context)
        if condition == "missing":
            raise BlobNotFoundError(str(selector))
        return record

    def read(selector, context):
        assert selector == blob_id
        contexts.append(context)
        reads.append(selector)
        return record, b"Changed bytes" if condition == "corrupt_bytes" else content

    app.state.blob_service = SimpleNamespace(get_blob_sync=metadata, read_blob_content_sync=read)
    marker = {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": digest}
    await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(
            source={"plugin": "csv", "on_success": "rows", "on_validation_failure": "discard", "options": {"schema": {"mode": "observed"}}},
            nodes=[
                {
                    "id": "assess",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "rows",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": {"system_prompt": marker, "queries": [{"name": "score", "template": marker}]},
                }
            ],
            outputs=[
                {
                    "name": "main",
                    "plugin": "json",
                    "options": {"path": "outputs/out.json", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
            metadata_={"name": "File prompts", "description": ""},
            is_valid=True,
        ),
        provenance="session_seed",
    )

    async def preflight(_state, **kwargs):
        getter = kwargs.get("blob_get_content")
        if getter is None:
            return ValidationResult(is_valid=False, checks=[], errors=[])
        try:
            found, data = getter(blob_id)
        except KeyError:
            return ValidationResult(is_valid=False, checks=[], errors=[])
        assert found == record and data == content
        return ValidationResult(is_valid=True, checks=[], errors=[])

    monkeypatch.setattr(state_routes, "_runtime_preflight_for_state", preflight)
    response = SyncASGITestClient(app, raise_server_exceptions=False).get(f"/api/sessions/{session.id}/state/yaml")
    if condition == "ready":
        assert response.status_code == 200, response.text
        assert reads == [blob_id]
        assert contexts
        assert "blob_ref" not in response.json()["yaml"]
        assert str(blob_id) not in response.text and content.decode() not in response.text
    elif condition in {"corrupt_bytes", "foreign_session"}:
        assert response.status_code == 500
        assert reads == ([blob_id] if condition == "corrupt_bytes" else [])
    else:
        assert response.status_code == 409
        assert reads == []
    if contexts:
        assert all(context is contexts[0] for context in contexts)


@pytest.mark.asyncio
async def test_export_preflight_forwards_verified_metadata_and_content(tmp_path: Path, monkeypatch) -> None:
    app, _service = _make_app(tmp_path)
    blob_id, session_id = uuid4(), uuid4()
    content = b"Exact text"
    record = replace(
        _ready_blob_record(blob_id=blob_id, session_id=session_id, storage_path="inputs/prompt.txt"),
        mime_type="text/plain",
        content_hash=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )

    def getter(selector):
        if selector != blob_id:
            raise KeyError(selector)
        return record, content

    def validate(_state, _settings, _generator, **kwargs):
        assert kwargs["blob_get_metadata"](blob_id) == record
        assert kwargs["blob_get_metadata"](uuid4()) is None
        assert kwargs["blob_get_content"](blob_id) == (record, content)
        return ValidationResult(is_valid=True, checks=[], errors=[])

    monkeypatch.setattr(_helpers, "validate_pipeline", validate)
    result = await _helpers._runtime_preflight_for_state(
        CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="File prompts"), version=1),
        settings=app.state.settings,
        secret_service=None,
        user_id=None,
        session_id=session_id,
        plugin_snapshot=app.state.plugin_snapshot_factory.for_user_id("alice"),
        profile_registry=app.state.operator_profile_registry,
        catalog=app.state.catalog_service,
        blob_get_content=getter,
    )
    assert result.is_valid

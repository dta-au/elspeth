"""Tests for the PLURAL authoritative blob binding (elspeth-0c6a343921).

``set_source_from_blobs`` accepts blob IDs only; every persisted ``blobs``
entry field is resolved from the session's authoritative records. The whole
list is validated atomically — one malformed, missing, duplicated, foreign,
non-ready, or inconsistent member fails the complete call with NO
composition mutation — and binary blobs are never UTF-8 decoded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import insert
from sqlalchemy.pool import StaticPool

from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools.sources import _execute_set_source_from_blobs
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

from .test_tools import _mock_catalog, _trained_tool_context


def _catalog_with_blob_rows():
    from elspeth.web.catalog.schemas import PluginSummary

    catalog = _mock_catalog()
    catalog.list_sources.return_value = [
        *catalog.list_sources.return_value,
        PluginSummary(name="blob_rows", description="Blob custody rows", plugin_type="source", config_fields=[]),
    ]
    return catalog


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 16


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


@pytest.fixture
def harness(tmp_path):
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    blob_service = BlobServiceImpl(engine, tmp_path)
    session_id = str(uuid4())
    from datetime import UTC, datetime

    with engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="alice",
                auth_provider_type="local",
                title="plural binding",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return engine, blob_service, session_id


def _create_ready_blob(harness, *, content: bytes, filename: str, mime_type: str) -> str:
    import asyncio

    _, blob_service, session_id = harness
    from uuid import UUID

    record = asyncio.run(
        blob_service.create_blob(
            session_id=UUID(session_id),
            filename=filename,
            content=content,
            mime_type=mime_type,  # type: ignore[arg-type]
            created_by="user",
            source_description="uploaded",
        )
    )
    return str(record.id)


def _first_error(result) -> str:
    return result.validation.errors[0].message if result.validation and result.validation.errors else ""


def _run(harness, arguments: dict[str, Any], state: CompositionState | None = None):
    engine, _, session_id = harness
    context = _trained_tool_context(_catalog_with_blob_rows(), session_engine=engine, session_id=session_id)
    return _execute_set_source_from_blobs(arguments, state or _empty_state(), context)


class TestSetSourceFromBlobs:
    def test_binds_ready_blobs_in_order_with_authoritative_fields(self, harness) -> None:
        import hashlib

        png_id = _create_ready_blob(harness, content=_PNG, filename="page-1.png", mime_type="image/png")
        jpg_id = _create_ready_blob(harness, content=_JPEG, filename="page-2.jpg", mime_type="image/jpeg")

        result = _run(harness, {"blob_ids": [jpg_id, png_id], "on_success": "documents"})

        assert result.success, _first_error(result)
        source = result.updated_state.sources["source"]
        assert source.plugin == "blob_rows"
        assert source.on_success == "documents"
        entries = source.options["blobs"]
        assert [entry["blob_id"] for entry in entries] == [jpg_id, png_id]
        assert entries[0] == {
            "blob_id": jpg_id,
            "payload_ref": hashlib.sha256(_JPEG).hexdigest(),
            "filename": "page-2.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": len(_JPEG),
        }
        assert source.options["schema"] == {"mode": "observed"}
        # Bounded audit payload only — never storage paths or content.
        payloads = result.data["source_blobs"]
        assert [p["blob_id"] for p in payloads] == [jpg_id, png_id]
        assert all("storage_path" not in p for p in payloads)

    def test_missing_blob_fails_whole_call_without_mutation(self, harness) -> None:
        png_id = _create_ready_blob(harness, content=_PNG, filename="p.png", mime_type="image/png")
        state = _empty_state()

        result = _run(harness, {"blob_ids": [png_id, str(uuid4())], "on_success": "docs"}, state)

        assert not result.success
        assert "not found" in _first_error(result)
        assert result.updated_state is state or result.updated_state.sources == {}

    def test_foreign_session_blob_reads_as_missing(self, harness, tmp_path) -> None:
        engine, blob_service, _ = harness
        from datetime import UTC, datetime

        other_session = str(uuid4())
        with engine.begin() as conn:
            conn.execute(
                insert(sessions_table).values(
                    id=other_session,
                    user_id="mallory",
                    auth_provider_type="local",
                    title="other",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        import asyncio
        from uuid import UUID

        foreign = asyncio.run(
            blob_service.create_blob(
                session_id=UUID(other_session),
                filename="secret.png",
                content=_PNG,
                mime_type="image/png",  # type: ignore[arg-type]
                created_by="user",
            )
        )

        result = _run(harness, {"blob_ids": [str(foreign.id)], "on_success": "docs"})

        assert not result.success
        assert "not found" in _first_error(result)
        # The failure must not disclose the foreign blob's metadata.
        assert "secret.png" not in repr(result.data)

    def test_pending_blob_rejected(self, harness) -> None:
        _, blob_service, session_id = harness
        import asyncio
        from uuid import UUID

        pending = asyncio.run(
            blob_service.create_pending_blob(
                session_id=UUID(session_id),
                filename="out.csv",
                mime_type="text/csv",
                created_by="pipeline",
            )
        )

        result = _run(harness, {"blob_ids": [str(pending.id)], "on_success": "docs"})

        assert not result.success
        assert "not ready" in _first_error(result)

    def test_duplicate_request_ids_rejected(self, harness) -> None:
        png_id = _create_ready_blob(harness, content=_PNG, filename="p.png", mime_type="image/png")

        result = _run(harness, {"blob_ids": [png_id, png_id], "on_success": "docs"})

        assert not result.success
        assert "Duplicate blob id" in _first_error(result)

    def test_duplicate_content_across_blobs_rejected(self, harness) -> None:
        first = _create_ready_blob(harness, content=_PNG, filename="a.png", mime_type="image/png")
        second = _create_ready_blob(harness, content=_PNG, filename="b.png", mime_type="image/png")

        result = _run(harness, {"blob_ids": [first, second], "on_success": "docs"})

        assert not result.success
        assert "same content" in _first_error(result)

    def test_caller_supplied_blobs_option_rejected(self, harness) -> None:
        png_id = _create_ready_blob(harness, content=_PNG, filename="p.png", mime_type="image/png")

        result = _run(
            harness,
            {
                "blob_ids": [png_id],
                "on_success": "docs",
                "options": {"blobs": [{"blob_id": "forged", "payload_ref": "a" * 64}]},
            },
        )

        assert not result.success
        assert "do not author" in _first_error(result)

    def test_llm_authored_blob_rejected_fail_closed(self, harness) -> None:
        png_id = _create_ready_blob(harness, content=_PNG, filename="p.png", mime_type="image/png")

        real_get = None
        from elspeth.web.composer.tools import sources as sources_module

        real_get = sources_module._sync_get_blob

        def _llm_authored(engine, blob_id, session_id=None):
            record = real_get(engine, blob_id, session_id)
            if record is not None:
                record = dict(record)
                record["creation_modality"] = "llm_generated"
            return record

        with patch.object(sources_module, "_sync_get_blob", side_effect=_llm_authored):
            result = _run(harness, {"blob_ids": [png_id], "on_success": "docs"})

        assert not result.success
        assert "LLM-authored" in _first_error(result)
        assert "set_source_from_blob" in _first_error(result)

    def test_malformed_uuid_rejected(self, harness) -> None:
        result = _run(harness, {"blob_ids": ["not-a-uuid"], "on_success": "docs"})
        assert not result.success

    def test_empty_blob_ids_is_an_argument_error(self, harness) -> None:
        with pytest.raises(ToolArgumentError):
            _run(harness, {"blob_ids": [], "on_success": "docs"})

    def test_named_source_binding(self, harness) -> None:
        png_id = _create_ready_blob(harness, content=_PNG, filename="p.png", mime_type="image/png")

        result = _run(
            harness,
            {"blob_ids": [png_id], "on_success": "docs", "source_name": "documents"},
        )

        assert result.success, _first_error(result)
        assert "documents" in result.updated_state.sources

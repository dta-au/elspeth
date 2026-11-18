"""``SessionInlineBlobSnapshot.content`` refuses by type, never by lookup failure."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobNotFoundError, BlobRecord, BlobStateError, InlineBlobNotAdmittedError
from elspeth.contracts.enums import CreationModality
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.sessions.inline_blob_preflight import (
    InlinePreflightState,
    SessionInlineBlobSnapshot,
    prepare_session_inline_blob_snapshot,
)


def _record(blob_id: UUID, session_id: UUID, content: bytes) -> BlobRecord:
    return BlobRecord(
        id=blob_id,
        session_id=session_id,
        filename="prompt.txt",
        mime_type="text/plain",
        size_bytes=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        storage_path="inline-prompt.txt",
        created_at=datetime.now(UTC),
        created_by="user",
        source_description=None,
        status="ready",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


def _state(*bindings: tuple[UUID, bytes]) -> InlinePreflightState:
    nodes = [
        {
            "id": f"n{index}",
            "options": {
                "prompt_template": {
                    "blob_ref": str(blob_id),
                    "mode": "inline_content",
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            },
        }
        for index, (blob_id, content) in enumerate(bindings)
    ]
    return InlinePreflightState(sources={}, nodes=nodes, outputs=[])


def _snapshot(session_id: UUID, blobs: dict[UUID, bytes], *, missing: frozenset[UUID] = frozenset()) -> SessionInlineBlobSnapshot:
    def read_blob(blob_id: UUID) -> tuple[BlobRecord, bytes]:
        if blob_id in missing:
            raise BlobNotFoundError(str(blob_id))
        return _record(blob_id, session_id, blobs[blob_id]), blobs[blob_id]

    snapshot = prepare_session_inline_blob_snapshot(_state(*blobs.items()), session_id=session_id, read_blob=read_blob)
    assert snapshot is not None
    return snapshot


def test_admitted_blob_returns_its_verified_pair() -> None:
    session_id, blob_id = uuid4(), uuid4()
    snapshot = _snapshot(session_id, {blob_id: b"prompt"})
    record, content = snapshot.content(blob_id)
    assert (record.id, content) == (blob_id, b"prompt")


def test_unreadable_blob_refuses_as_unverified() -> None:
    session_id, blob_id = uuid4(), uuid4()
    snapshot = _snapshot(session_id, {blob_id: b"prompt"}, missing=frozenset({blob_id}))
    with pytest.raises(InlineBlobNotAdmittedError) as refusal:
        snapshot.content(blob_id)
    assert refusal.value.reason == "unverified"
    assert refusal.value.limit_bytes is None
    assert refusal.value.blob_id == str(blob_id)


def test_blob_outside_the_snapshot_refuses_as_unverified() -> None:
    session_id = uuid4()
    snapshot = _snapshot(session_id, {uuid4(): b"prompt"})
    with pytest.raises(InlineBlobNotAdmittedError) as refusal:
        snapshot.content(uuid4())
    assert refusal.value.reason == "unverified"


def test_oversized_blob_refuses_naming_the_per_file_cap() -> None:
    session_id, blob_id = uuid4(), uuid4()
    snapshot = _snapshot(session_id, {blob_id: b"x" * (BLOB_INLINE_PER_REF_BYTE_CAP + 1)})
    with pytest.raises(InlineBlobNotAdmittedError) as refusal:
        snapshot.content(blob_id)
    assert refusal.value.reason == "oversized"
    assert refusal.value.limit_bytes == BLOB_INLINE_PER_REF_BYTE_CAP
    assert str(BLOB_INLINE_PER_REF_BYTE_CAP) in str(refusal.value)


def test_blobs_over_the_aggregate_cap_refuse_naming_it() -> None:
    session_id = uuid4()
    blobs = {
        uuid4(): b"x" * BLOB_INLINE_PER_REF_BYTE_CAP for _ in range(BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP + 1)
    }
    snapshot = _snapshot(session_id, blobs)
    refused = [blob_id for blob_id in blobs if blob_id not in snapshot.records]
    assert len(refused) == 1
    with pytest.raises(InlineBlobNotAdmittedError) as refusal:
        snapshot.content(refused[0])
    assert refusal.value.reason == "aggregate_oversized"
    assert refusal.value.limit_bytes == BLOB_INLINE_AGGREGATE_BYTE_CAP


def test_refusal_is_a_blob_state_error_for_existing_not_ready_consumers() -> None:
    session_id, blob_id = uuid4(), uuid4()
    snapshot = _snapshot(session_id, {blob_id: b"prompt"}, missing=frozenset({blob_id}))
    with pytest.raises(BlobStateError):
        snapshot.content(blob_id)


def test_refusal_reason_and_limit_must_agree() -> None:
    with pytest.raises(ValueError, match="limit_bytes"):
        InlineBlobNotAdmittedError("b", reason="unverified", limit_bytes=1)
    with pytest.raises(ValueError, match="limit_bytes"):
        InlineBlobNotAdmittedError("b", reason="oversized", limit_bytes=None)

"""Integration contracts for custody-safe canonical pipeline proposals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import delete

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash
from elspeth.web.blobs.service import content_hash
from elspeth.web.composer.pipeline_custody import (
    PipelineCustodyPreparation,
    finalize_pipeline_custody_on_connection,
    inline_custody_audit_projection,
    prepare_pipeline_custody,
    verify_finalized_pipeline_custody,
)
from elspeth.web.composer.tools.blobs import _PreparedBlobCreate
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


def _prepared(
    tmp_path: Path,
    *,
    content: bytes = b"private-inline-value\n42\n",
    session_id: str | None = None,
    created_from_message_id: str | None = None,
) -> _PreparedBlobCreate:
    session_id = session_id or str(uuid4())
    blob_id = str(uuid4())
    return _PreparedBlobCreate(
        blob_id=blob_id,
        filename="candidate.csv",
        mime_type="text/csv",
        content_bytes=content,
        content_hash=content_hash(content),
        storage_path=tmp_path / "blobs" / session_id / f"{blob_id}_candidate.csv",
        description="reviewed candidate",
        creation_modality=CreationModality.LLM_GENERATED,
        created_from_message_id=created_from_message_id or str(uuid4()),
        creating_model_identifier="composer-model",
        creating_model_version="composer-version",
        creating_provider="provider",
        creating_composer_skill_hash="a" * 64,
        creating_arguments_hash="b" * 64,
    )


def _arguments(content: str = "private-inline-value\n42\n") -> dict[str, object]:
    return {
        "source": {
            "plugin": "csv",
            "options": {"schema": {"mode": "observed"}},
            "on_success": "main",
            "on_validation_failure": "discard",
            "inline_blob": {
                "filename": "candidate.csv",
                "mime_type": "text/csv",
                "content": content,
                "description": "reviewed candidate",
            },
        },
        "nodes": [],
        "edges": [],
        "outputs": [],
        "metadata": {"name": "custody proposal"},
    }


def test_prepare_pipeline_custody_is_pure_and_hashes_only_safe_arguments(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    arguments = _arguments()
    session_id = prepared.storage_path.parent.name

    custody = prepare_pipeline_custody(arguments, prepared, session_id=session_id, max_storage_per_session=500 * 1024 * 1024)

    source = custody.arguments["source"]
    assert isinstance(source, Mapping)
    assert "inline_blob" not in source
    assert source["blob_id"] == str(custody.blob_id)
    assert custody.max_storage_per_session == 500 * 1024 * 1024, "the plan-time ceiling must be stamped on the preparation"
    assert custody.request.creating_arguments_hash == stable_hash(custody.arguments)
    assert custody.request.content == prepared.content_bytes
    assert not prepared.storage_path.parent.exists()
    assert "private-inline-value" not in repr(prepared)
    assert "private-inline-value" not in repr(custody.request)


def test_audit_projection_recursively_removes_inline_content_from_malformed_and_named_shapes() -> None:
    secret = "redacted inline test value"
    arguments = _arguments(secret)
    arguments["sources"] = {
        "named": {
            "inline_blob": {
                "content": {"malformed": [secret]},
                "filename": "named.csv",
                "mime_type": "text/csv",
            }
        }
    }

    projected = inline_custody_audit_projection(arguments)

    assert secret not in repr(projected)
    assert projected["source"]["inline_blob"] == "[redacted inline content held for custody]"
    assert projected["sources"]["named"]["inline_blob"] == "[redacted inline content held for custody]"


def test_finalize_refuses_ceiling_divergent_from_plan_time(tmp_path: Path) -> None:
    """The settle-time quota must equal the plan-time quota, or nothing settles.

    The guard fires before any filesystem or DB I/O, so no connection is
    consumed — a divergent ceiling can never be enforced, not even
    transiently.
    """
    from typing import cast

    from sqlalchemy import Connection

    prepared = _prepared(tmp_path)
    arguments = _arguments()
    custody = prepare_pipeline_custody(
        arguments,
        prepared,
        session_id=prepared.storage_path.parent.name,
        max_storage_per_session=500 * 1024 * 1024,
    )

    with pytest.raises(AuditIntegrityError, match="diverges from the plan-time ceiling"):
        finalize_pipeline_custody_on_connection(
            custody,
            conn=cast(Connection, object()),  # guard fires before the connection is touched
            data_dir=tmp_path,
            max_storage_per_session=1024,
            write_fence=None,
        )


@pytest.mark.asyncio
async def test_failed_commit_reconciliation_preserves_a_committed_inline_stage(tmp_path: Path) -> None:
    """A commit error is ambiguous: a durable row must win over cleanup."""
    from elspeth.web.composer import pipeline_custody as pipeline_custody_module

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session = await sessions.create_session("test-user", "Ambiguous inline commit", "local")
    message = await sessions.add_message(
        session.id,
        "user",
        "Create the inline source.",
        writer_principal="route_user_message",
    )
    session_id = str(session.id)
    prepared = _prepared(tmp_path, session_id=session_id, created_from_message_id=str(message.id))
    custody = prepare_pipeline_custody(
        _arguments(),
        prepared,
        session_id=session_id,
        max_storage_per_session=500 * 1024 * 1024,
    )
    with engine.begin() as conn:
        publication = finalize_pipeline_custody_on_connection(
            custody,
            conn=conn,
            data_dir=tmp_path,
            max_storage_per_session=500 * 1024 * 1024,
            write_fence=None,
        )

    assert not publication.storage.exists()
    assert publication.staging.exists()
    primary = RuntimeError("commit outcome unknown")
    pipeline_custody_module.reconcile_pipeline_custody_after_transaction_failure(
        engine,
        publication,
        primary_exc=primary,
    )

    assert publication.storage.read_bytes() == custody.request.content
    assert not publication.staging.exists()


@pytest.mark.asyncio
async def test_failed_commit_reconciliation_removes_stage_when_committed_row_was_deleted(tmp_path: Path) -> None:
    """A concurrent committed delete leaves no authority for staged bytes."""
    from elspeth.web.composer import pipeline_custody as pipeline_custody_module

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session = await sessions.create_session("test-user", "Deleted inline commit", "local")
    message = await sessions.add_message(
        session.id,
        "user",
        "Create the inline source.",
        writer_principal="route_user_message",
    )
    session_id = str(session.id)
    prepared = _prepared(tmp_path, session_id=session_id, created_from_message_id=str(message.id))
    custody = prepare_pipeline_custody(
        _arguments(),
        prepared,
        session_id=session_id,
        max_storage_per_session=500 * 1024 * 1024,
    )
    with engine.begin() as conn:
        publication = finalize_pipeline_custody_on_connection(
            custody,
            conn=conn,
            data_dir=tmp_path,
            max_storage_per_session=500 * 1024 * 1024,
            write_fence=None,
        )
    publication = replace(publication, cleanup_after_rollback=False)
    with engine.begin() as conn:
        conn.execute(delete(blobs_table).where(blobs_table.c.id == str(custody.blob_id)))

    assert publication.staging.exists()
    primary = RuntimeError("commit outcome unknown")
    pipeline_custody_module.reconcile_pipeline_custody_after_transaction_failure(
        engine,
        publication,
        primary_exc=primary,
    )

    assert not publication.storage.exists()
    assert not publication.staging.exists()


def test_preparation_rejects_non_positive_or_inexact_ceiling(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    for bad_ceiling in (0, -1, True, 10.5):
        with pytest.raises(AuditIntegrityError, match="positive exact integer"):
            prepare_pipeline_custody(
                _arguments(),
                prepared,
                session_id=prepared.storage_path.parent.name,
                max_storage_per_session=bad_ceiling,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize(
    "inline_value",
    [
        "private scalar value",
        ["private list value", {"nested": "private nested list value"}],
        {"content": "private content value", "unknown": {"payload": "private unknown value"}},
    ],
)
def test_audit_projection_replaces_every_inline_blob_value_wholesale(inline_value: object) -> None:
    arguments = {
        "source": {"inline_blob": inline_value},
        "sources": {"named": {"inline_blob": inline_value}},
    }

    projected = inline_custody_audit_projection(arguments)

    marker = "[redacted inline content held for custody]"
    assert projected["source"]["inline_blob"] == marker
    assert projected["sources"]["named"]["inline_blob"] == marker
    assert "private" not in repr(projected)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda arguments: arguments["source"]["options"].update({"blob_ref": str(uuid4())}),
        lambda arguments: arguments.update({"sources": {"named": {"inline_blob": {"content": "second secret"}}}}),
    ],
)
def test_prepare_pipeline_custody_rejects_manual_or_residual_blob_authority(tmp_path: Path, mutate) -> None:
    prepared = _prepared(tmp_path)
    arguments = _arguments()
    mutate(arguments)

    with pytest.raises(AuditIntegrityError):
        prepare_pipeline_custody(
            arguments, prepared, session_id=prepared.storage_path.parent.name, max_storage_per_session=500 * 1024 * 1024
        )

    assert not prepared.storage_path.parent.exists()


@pytest.mark.parametrize(
    ("arguments_patch", "prepared_patch"),
    [
        ({"content": "different private content"}, {}),
        ({"mime_type": "text/plain"}, {}),
        ({"filename": "different.csv"}, {}),
        ({"description": "different description"}, {}),
        ({}, {"content_hash": "f" * 64}),
    ],
)
def test_prepare_pipeline_custody_rejects_candidate_argument_seam_mismatch_without_values(
    tmp_path: Path,
    arguments_patch: dict[str, str],
    prepared_patch: dict[str, str],
) -> None:
    prepared = replace(_prepared(tmp_path), **prepared_patch)
    arguments = _arguments()
    arguments["source"]["inline_blob"].update(arguments_patch)

    with pytest.raises(AuditIntegrityError) as exc_info:
        prepare_pipeline_custody(
            arguments, prepared, session_id=prepared.storage_path.parent.name, max_storage_per_session=500 * 1024 * 1024
        )

    assert "different private content" not in str(exc_info.value)
    assert "different description" not in str(exc_info.value)
    assert not prepared.storage_path.parent.exists()


_CUSTODY_CONTENT = b"private-inline-value\n42\n"


def _settled(tmp_path: Path) -> tuple[PipelineCustodyPreparation, BlobRecord]:
    """Prepare one custody plan and the exact settled row it promises.

    Expected values are written out literally rather than re-derived through
    ``_normalized_inline_custody_fields``: a record built by the same helper the
    verifier calls would agree with itself no matter what either one did.  Only
    ``creating_arguments_hash`` comes from the preparation, because
    ``prepare_pipeline_custody`` deliberately rebinds it to the hash of the
    custody-safe arguments and no literal can stand in for that.
    """
    prepared = _prepared(tmp_path, content=_CUSTODY_CONTENT)
    session_id = prepared.storage_path.parent.name
    custody = prepare_pipeline_custody(
        _arguments(),
        prepared,
        session_id=session_id,
        max_storage_per_session=500 * 1024 * 1024,
    )
    storage = tmp_path.expanduser().resolve() / "blobs" / session_id / f"{custody.blob_id}_candidate.csv"
    storage.parent.mkdir(parents=True, exist_ok=True)
    storage.write_bytes(_CUSTODY_CONTENT)
    record = BlobRecord(
        id=custody.blob_id,
        session_id=custody.request.session_id,
        filename="candidate.csv",
        mime_type="text/csv",
        size_bytes=len(_CUSTODY_CONTENT),
        content_hash=content_hash(_CUSTODY_CONTENT),
        storage_path=str(storage),
        created_at=datetime.now(UTC),
        created_by="assistant",
        source_description="reviewed candidate",
        status="ready",
        creation_modality=CreationModality.LLM_GENERATED,
        created_from_message_id=prepared.created_from_message_id,
        creating_model_identifier="composer-model",
        creating_model_version="composer-version",
        creating_provider="provider",
        creating_composer_skill_hash="a" * 64,
        creating_arguments_hash=custody.request.creating_arguments_hash,
    )
    return custody, record


def test_verify_accepts_the_row_the_preparation_promised(tmp_path: Path) -> None:
    custody, record = _settled(tmp_path)

    verify_finalized_pipeline_custody(custody, record, data_dir=tmp_path)


def test_verify_accepts_a_naive_created_at(tmp_path: Path) -> None:
    """``created_at`` is checked for shape only, and deliberately so.

    The column is ``DateTime(timezone=True)`` and the write stamps an aware
    value, but the SQLite round-trip returns it naive.  Asserting awareness here
    would raise on every inline custody finalization under SQLite, so this pins
    the narrowing against a well-meaning future tightening.
    """
    custody, record = _settled(tmp_path)

    verify_finalized_pipeline_custody(custody, replace(record, created_at=datetime.now()), data_dir=tmp_path)  # noqa: DTZ005


def test_verify_rejects_a_value_equal_but_differently_typed_size(tmp_path: Path) -> None:
    """``24.0 == 24`` is True; the settled row still carried the wrong type."""
    custody, record = _settled(tmp_path)
    widened = replace(record, size_bytes=float(len(_CUSTODY_CONTENT)))

    assert widened.size_bytes == record.size_bytes
    with pytest.raises(AuditIntegrityError, match="mismatched size_bytes"):
        verify_finalized_pipeline_custody(custody, widened, data_dir=tmp_path)


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("filename", "attacker.csv"),
        ("mime_type", "text/plain"),
        ("content_hash", "f" * 64),
        ("created_by", "user"),
        ("source_description", "quietly relabelled"),
        ("status", "pending"),
        ("creation_modality", CreationModality.VERBATIM),
        ("creating_model_identifier", "other-model"),
        ("creating_provider", "other-provider"),
        ("creating_composer_skill_hash", "c" * 64),
        ("creating_arguments_hash", "d" * 64),
    ],
)
def test_verify_rejects_every_tampered_provenance_field(tmp_path: Path, field_name: str, tampered_value: object) -> None:
    custody, record = _settled(tmp_path)

    with pytest.raises(AuditIntegrityError, match=f"mismatched {field_name}"):
        verify_finalized_pipeline_custody(custody, replace(record, **{field_name: tampered_value}), data_dir=tmp_path)


def test_verify_rejects_a_row_pointing_at_another_blobs_storage(tmp_path: Path) -> None:
    custody, record = _settled(tmp_path)
    elsewhere = tmp_path / "blobs" / "elsewhere.csv"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_bytes(_CUSTODY_CONTENT)

    with pytest.raises(AuditIntegrityError, match="mismatched storage_path"):
        verify_finalized_pipeline_custody(custody, replace(record, storage_path=str(elsewhere)), data_dir=tmp_path)


def test_verify_rejects_a_missing_storage_file(tmp_path: Path) -> None:
    custody, record = _settled(tmp_path)
    Path(record.storage_path).unlink()

    with pytest.raises(AuditIntegrityError, match="missing storage file"):
        verify_finalized_pipeline_custody(custody, record, data_dir=tmp_path)


def test_verify_rejects_storage_bytes_that_drifted_from_the_authorized_content(tmp_path: Path) -> None:
    custody, record = _settled(tmp_path)
    Path(record.storage_path).write_bytes(b"substituted-after-the-row-was-written\n")

    with pytest.raises(AuditIntegrityError, match="mismatched storage bytes"):
        verify_finalized_pipeline_custody(custody, record, data_dir=tmp_path)


def test_verify_rejects_a_symbolic_link_planted_at_the_storage_path(tmp_path: Path) -> None:
    custody, record = _settled(tmp_path)
    settled_path = Path(record.storage_path)
    target = tmp_path / "attacker-controlled.csv"
    target.write_bytes(_CUSTODY_CONTENT)
    settled_path.unlink()
    settled_path.symlink_to(target)

    with pytest.raises(AuditIntegrityError, match="symbolic-link storage path"):
        verify_finalized_pipeline_custody(custody, record, data_dir=tmp_path)

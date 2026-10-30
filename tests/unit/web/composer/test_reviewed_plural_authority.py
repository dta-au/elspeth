"""Private plural reuse must revalidate every persisted attachment record.

The current incremental binder supplies the custody entries; the real session
service persists them before the current owned-state authority API reads them.
Later-member mutations change database metadata only. These controls make no
claim about physical attachment-byte integrity or new public selector grammar.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import Engine, delete, insert, select, update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.pipeline_proposal import (
    owned_composition_state_authority,
    owned_composition_state_execution_arguments,
)
from elspeth.web.composer.reviewed_source_authority import resolve_owned_composition_source_authority
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools._common import ReviewedSourceAuthority
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, chat_messages_table, sessions_table
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

from .test_set_source_from_blobs import _JPEG, _PNG, _create_ready_blob
from .test_tools import execute_tool


@dataclass(frozen=True)
class _AuthorityHarness:
    engine: Engine
    sessions: FencedSessionServiceHarness
    blobs: BlobServiceImpl
    catalog: CatalogService
    session_id: UUID
    data_dir: Path


@dataclass(frozen=True)
class _PersistedPlural:
    state: CompositionState
    record: CompositionStateRecord
    blob_ids: tuple[str, str]


@pytest.fixture
def authority_harness(tmp_path: Path) -> Iterator[_AuthorityHarness]:
    engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    session_id = uuid4()
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        conn.execute(
            insert(sessions_table).values(
                id=str(session_id),
                user_id="alice",
                auth_provider_type="local",
                title="Reviewed plural authority",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    sessions = FencedSessionServiceHarness(
        engine, data_dir=tmp_path, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.reviewed_plural_authority")
    )
    try:
        yield _AuthorityHarness(engine, sessions, BlobServiceImpl(engine, tmp_path), create_catalog_service(), session_id, tmp_path)
    finally:
        engine.dispose()


def _create_blob(harness: _AuthorityHarness, *, content: bytes, filename: str, mime_type: str) -> str:
    return _create_ready_blob(
        (harness.engine, harness.blobs, str(harness.session_id)), content=content, filename=filename, mime_type=mime_type
    )


def _draft() -> CompositionState:
    return CompositionState(
        sources={},
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="documents",
                plugin="json",
                options={
                    "path": "outputs/documents.jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                    "format": "jsonl",
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Reviewed attachment records"),
        version=1,
    )


def _bind(harness: _AuthorityHarness, state: CompositionState, blob_ids: list[str], *, source_name: str = "scans") -> ToolResult:
    return execute_tool(
        "set_source_from_blobs",
        {"source_name": source_name, "blob_ids": blob_ids, "on_success": "documents", "on_validation_failure": "discard"},
        state,
        harness.catalog,
        session_engine=harness.engine,
        session_id=str(harness.session_id),
        user_id="alice",
        data_dir=str(harness.data_dir),
    )


def _persist(
    harness: _AuthorityHarness, state: CompositionState, *, is_valid: bool = True
) -> tuple[CompositionState, CompositionStateRecord]:
    payload = state.to_dict()
    saved = asyncio.run(
        harness.sessions.save_composition_state(
            harness.session_id,
            CompositionStateData(
                sources=payload["sources"],
                nodes=payload["nodes"],
                edges=payload["edges"],
                outputs=payload["outputs"],
                metadata_=payload["metadata"],
                is_valid=is_valid,
                validation_errors=(),
            ),
            provenance="session_seed",
        )
    )
    current = asyncio.run(harness.sessions.get_current_state(harness.session_id))
    assert current is not None and current.id == saved.id
    restored = state_from_record(current)
    assert restored.sources == state.sources
    assert (restored.nodes, restored.edges, restored.outputs, restored.metadata) == (
        state.nodes,
        state.edges,
        state.outputs,
        state.metadata,
    )
    return restored, current


def _seed_persisted_plural(harness: _AuthorityHarness) -> _PersistedPlural:
    first = _create_blob(harness, content=_PNG, filename="page-1.png", mime_type="image/png")
    later = _create_blob(harness, content=_JPEG, filename="page-2.jpg", mime_type="image/jpeg")
    bound = _bind(harness, _draft(), [first, later])
    assert bound.success, bound.validation.errors
    assert bound.validation.is_valid, bound.validation.errors
    state, record = _persist(harness, bound.updated_state)
    assert deep_thaw(state.sources["scans"].options["blobs"]) == [
        {
            "blob_id": first,
            "payload_ref": hashlib.sha256(_PNG).hexdigest(),
            "filename": "page-1.png",
            "mime_type": "image/png",
            "size_bytes": len(_PNG),
        },
        {
            "blob_id": later,
            "payload_ref": hashlib.sha256(_JPEG).hexdigest(),
            "filename": "page-2.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": len(_JPEG),
        },
    ]
    return _PersistedPlural(state, record, (first, later))


def _resolve(harness: _AuthorityHarness, state: CompositionState, *, user_id: str = "alice") -> ReviewedSourceAuthority:
    authority = resolve_owned_composition_source_authority(
        engine=harness.engine, session_id=str(harness.session_id), user_id=user_id, state=state
    )
    assert type(authority) is ReviewedSourceAuthority
    assert authority.session_id == str(harness.session_id)
    return authority


def _private_reuse(harness: _AuthorityHarness, state: CompositionState, authority: ReviewedSourceAuthority) -> ToolResult:
    arguments = dict(owned_composition_state_execution_arguments(owned_composition_state_authority(state), version=state.version))
    return execute_tool(
        "set_pipeline",
        arguments,
        state,
        harness.catalog,
        session_engine=harness.engine,
        session_id=str(harness.session_id),
        user_id="alice",
        data_dir=str(harness.data_dir),
        reviewed_source_authority=authority,
    )


def _blob_row(harness: _AuthorityHarness, blob_id: str) -> dict[str, Any] | None:
    with harness.engine.connect() as conn:
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).mappings().one_or_none()
    return None if row is None else dict(row)


def _assert_checkpoint_unchanged(harness: _AuthorityHarness, seed: _PersistedPlural) -> None:
    current = asyncio.run(harness.sessions.get_current_state(harness.session_id))
    assert current is not None
    assert (current.id, current.version) == (seed.record.id, seed.record.version)
    assert state_from_record(current) == seed.state


def _mutate_later_record(harness: _AuthorityHarness, blob_id: str, perturbation: str) -> None:
    changes: dict[str, Any]
    with harness.engine.begin() as conn:
        if perturbation == "missing":
            assert conn.execute(delete(blobs_table).where(blobs_table.c.id == blob_id)).rowcount == 1
            return
        if perturbation in {"foreign_session_same_user", "foreign_session_other_user"}:
            other_session = str(uuid4())
            other_user = "alice" if perturbation == "foreign_session_same_user" else "mallory"
            ensure_test_identity(conn, identity_id=other_user)
            conn.execute(
                insert(sessions_table).values(
                    id=other_session,
                    user_id=other_user,
                    auth_provider_type="local",
                    title="Another attachment owner",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            changes = {"session_id": other_session}
        elif perturbation in {"llm_generated", "disambiguated", "llm_generated_then_amended"}:
            message_id = str(uuid4())
            conn.execute(
                insert(chat_messages_table).values(
                    id=message_id,
                    session_id=str(harness.session_id),
                    role="user",
                    content="Generate this attachment.",
                    raw_content=None,
                    tool_calls=None,
                    tool_call_id=None,
                    sequence_no=1,
                    writer_principal="route_user_message",
                    created_at=datetime.now(UTC),
                    composition_state_id=None,
                    parent_assistant_id=None,
                )
            )
            changes = {
                "creation_modality": perturbation,
                "created_from_message_id": message_id,
                "creating_model_identifier": "offline-fixture-model",
                "creating_model_version": "offline-fixture-v1",
                "creating_provider": "offline-fixture-provider",
                "creating_composer_skill_hash": hashlib.sha256(b"fixture skill").hexdigest(),
                "creating_arguments_hash": hashlib.sha256(b"fixture arguments").hexdigest(),
            }
        else:
            changes = {
                "payload_ref": {"content_hash": hashlib.sha256(b"changed current record").hexdigest()},
                "filename": {"filename": "renamed-page-2.jpg"},
                "mime_type": {"mime_type": "image/png"},
                "size_bytes": {"size_bytes": len(_JPEG) + 1},
                "not_ready_pending": {"status": "pending"},
                "not_ready_error": {"status": "error"},
            }[perturbation]
        before = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).mappings().one()
        assert conn.execute(update(blobs_table).where(blobs_table.c.id == blob_id).values(**changes)).rowcount == 1
        after = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).mappings().one()
        assert {key for key in before if before[key] != after[key]} == set(changes)


def test_unchanged_persisted_plural_entries_authorize_private_reuse(authority_harness: _AuthorityHarness) -> None:
    """Dropping or refreshing valid reviewed entries must lose the exact reuse result."""
    seed = _seed_persisted_plural(authority_harness)
    authority = _resolve(authority_harness, seed.state)

    result = _private_reuse(authority_harness, seed.state, authority)

    assert result.success, result.validation.errors
    assert result.updated_state.version == seed.state.version + 1
    assert set(result.updated_state.sources) == {"scans"}
    assert result.updated_state.sources["scans"] == seed.state.sources["scans"]
    assert [entry["blob_id"] for entry in result.updated_state.sources["scans"].options["blobs"]] == list(seed.blob_ids)
    _assert_checkpoint_unchanged(authority_harness, seed)


@pytest.mark.parametrize(
    "perturbation",
    (
        "payload_ref",
        "filename",
        "mime_type",
        "size_bytes",
        "foreign_session_same_user",
        "foreign_session_other_user",
        "not_ready_pending",
        "not_ready_error",
        "llm_generated",
        "disambiguated",
        "llm_generated_then_amended",
        "missing",
    ),
)
def test_reviewed_plural_entry_revalidation(authority_harness: _AuthorityHarness, perturbation: str) -> None:
    """Skipping the later entry or its changed current property must lose refusal."""
    seed = _seed_persisted_plural(authority_harness)
    _resolve(authority_harness, seed.state)
    first, later = seed.blob_ids
    original_first = _blob_row(authority_harness, first)
    original_later = _blob_row(authority_harness, later)
    assert original_first is not None and original_later is not None

    _mutate_later_record(authority_harness, later, perturbation)
    assert _blob_row(authority_harness, first) == original_first
    assert _blob_row(authority_harness, later) != original_later

    with pytest.raises(AuditIntegrityError, match="reviewed plural"):
        _resolve(authority_harness, seed.state)

    _assert_checkpoint_unchanged(authority_harness, seed)
    with authority_harness.engine.begin() as conn:
        if perturbation == "missing":
            conn.execute(insert(blobs_table).values(**original_later))
        else:
            conn.execute(update(blobs_table).where(blobs_table.c.id == later).values(**original_later))
    _resolve(authority_harness, seed.state)
    _assert_checkpoint_unchanged(authority_harness, seed)


def test_one_attachment_may_be_reused_across_reviewed_roots(authority_harness: _AuthorityHarness) -> None:
    """Moving duplicate detection across root boundaries must reject this valid reuse."""
    seed = _seed_persisted_plural(authority_harness)
    first, later = seed.blob_ids
    bound = _bind(authority_harness, seed.state, [later, first], source_name="reused_scans")
    assert bound.success, bound.validation.errors
    assert bound.validation.is_valid, bound.validation.errors
    state, _record = _persist(authority_harness, bound.updated_state)

    authority = _resolve(authority_harness, state)
    result = _private_reuse(authority_harness, state, authority)

    assert result.success, result.validation.errors
    assert result.updated_state.version == state.version + 1
    assert set(result.updated_state.sources) == {"scans", "reused_scans"}
    assert [entry["blob_id"] for entry in result.updated_state.sources["scans"].options["blobs"]] == [first, later]
    assert [entry["blob_id"] for entry in result.updated_state.sources["reused_scans"].options["blobs"]] == [later, first]


@pytest.mark.parametrize("duplication", ("identity", "content"))
def test_duplicate_attachment_is_refused_within_one_incremental_list(authority_harness: _AuthorityHarness, duplication: str) -> None:
    """Dropping either list-local duplicate check must apply an invalid new root."""
    seed = _seed_persisted_plural(authority_harness)
    first, _later = seed.blob_ids
    duplicate = first
    if duplication == "content":
        duplicate = _create_blob(authority_harness, content=_PNG, filename="same-document.png", mime_type="image/png")
        assert duplicate != first

    result = _bind(authority_harness, seed.state, [first, duplicate], source_name="duplicates")

    assert result.success is False
    assert result.updated_state is seed.state
    assert [error.error_code for error in result.validation.errors] == ["source_blob_duplicate"]
    _assert_checkpoint_unchanged(authority_harness, seed)


def test_private_plural_authority_refuses_a_different_requesting_user(authority_harness: _AuthorityHarness) -> None:
    """Removing the existing current-user check must grant foreign private custody."""
    seed = _seed_persisted_plural(authority_harness)
    _resolve(authority_harness, seed.state)

    with pytest.raises(AuditIntegrityError, match="current user session"):
        _resolve(authority_harness, seed.state, user_id="mallory")

    _resolve(authority_harness, seed.state)
    _assert_checkpoint_unchanged(authority_harness, seed)


def _persist_corrupted_options(harness: _AuthorityHarness, seed: _PersistedPlural, options: dict[str, Any]) -> _PersistedPlural:
    source = replace(seed.state.sources["scans"], options=options)
    corrupted = seed.state.with_named_source("scans", source)
    state, record = _persist(harness, corrupted, is_valid=False)
    return _PersistedPlural(state, record, seed.blob_ids)


@pytest.mark.parametrize("corruption", ("missing", "not_a_list", "empty"))
def test_malformed_persisted_plural_list_is_an_integrity_error(authority_harness: _AuthorityHarness, corruption: str) -> None:
    """Ignoring absent or malformed blob_rows custody must grant invalid authority."""
    seed = _seed_persisted_plural(authority_harness)
    _resolve(authority_harness, seed.state)
    options = deep_thaw(seed.state.sources["scans"].options)
    if corruption == "missing":
        del options["blobs"]
    else:
        options["blobs"] = "not-a-list" if corruption == "not_a_list" else []
    corrupted = _persist_corrupted_options(authority_harness, seed, options)

    with pytest.raises(AuditIntegrityError, match="reviewed plural blob custody"):
        _resolve(authority_harness, corrupted.state)

    _assert_checkpoint_unchanged(authority_harness, corrupted)


@pytest.mark.parametrize("index", (pytest.param(0, id="first"), pytest.param(1, id="later")))
@pytest.mark.parametrize("corruption", ("not_a_mapping", "blob_id", "payload_ref", "size_type", "unexpected_field"))
def test_every_persisted_plural_entry_uses_the_owned_contract(authority_harness: _AuthorityHarness, index: int, corruption: str) -> None:
    """Skipping either entry or coercing corrupt stored facts must grant authority."""
    seed = _seed_persisted_plural(authority_harness)
    _resolve(authority_harness, seed.state)
    options = deep_thaw(seed.state.sources["scans"].options)
    entries = options["blobs"]
    if corruption == "not_a_mapping":
        entries[index] = "not-an-entry"
    elif corruption == "blob_id":
        entries[index]["blob_id"] = "not-a-uuid"
    elif corruption == "payload_ref":
        entries[index]["payload_ref"] = "not-a-hash"
    elif corruption == "size_type":
        entries[index]["size_bytes"] = str(entries[index]["size_bytes"])
    else:
        entries[index]["unexpected"] = "not-an-owned-field"
    corrupted = _persist_corrupted_options(authority_harness, seed, options)
    assert corrupted.state.sources["scans"].options["blobs"][1 - index] == seed.state.sources["scans"].options["blobs"][1 - index]

    with pytest.raises(AuditIntegrityError, match=r"reviewed plural blob custody.*malformed entry"):
        _resolve(authority_harness, corrupted.state)

    _assert_checkpoint_unchanged(authority_harness, corrupted)


@pytest.mark.parametrize("duplication", ("identity", "content"))
def test_duplicate_persisted_plural_custody_is_an_integrity_error(authority_harness: _AuthorityHarness, duplication: str) -> None:
    """Dropping per-list uniqueness must grant custody the binder could not issue."""
    seed = _seed_persisted_plural(authority_harness)
    _resolve(authority_harness, seed.state)
    options = deep_thaw(seed.state.sources["scans"].options)
    entries = options["blobs"]
    if duplication == "identity":
        entries[1] = dict(entries[0])
    else:
        # Keep each recorded entry equal to its live row so only list-local
        # content uniqueness can refuse this corrupt binding. This is a
        # metadata control; physical file integrity is outside this seam.
        entries[1]["payload_ref"] = entries[0]["payload_ref"]
        with authority_harness.engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == seed.blob_ids[1]).values(content_hash=entries[0]["payload_ref"]))
    corrupted = _persist_corrupted_options(authority_harness, seed, options)

    with pytest.raises(AuditIntegrityError, match=r"reviewed plural blob custody.*repeats an attachment"):
        _resolve(authority_harness, corrupted.state)

    _assert_checkpoint_unchanged(authority_harness, corrupted)


def test_reviewed_plural_custody_cannot_also_claim_scalar_identity(authority_harness: _AuthorityHarness) -> None:
    """Ignoring conflicting scalar custody must authorize an impossible plural root."""
    seed = _seed_persisted_plural(authority_harness)
    _resolve(authority_harness, seed.state)
    options = deep_thaw(seed.state.sources["scans"].options)
    options["blob_ref"] = seed.blob_ids[0]
    corrupted = _persist_corrupted_options(authority_harness, seed, options)

    with pytest.raises(AuditIntegrityError, match=r"reviewed plural blob custody.*scalar"):
        _resolve(authority_harness, corrupted.state)

    _assert_checkpoint_unchanged(authority_harness, corrupted)


def test_owned_rootless_authority_preserves_the_current_owner_check(authority_harness: _AuthorityHarness) -> None:
    """Requiring plural custody for every rootless draft must refuse valid ownership."""
    state, record = _persist(authority_harness, _draft(), is_valid=False)
    authority = _resolve(authority_harness, state)
    assert authority.reviewed_sources == {}

    with pytest.raises(AuditIntegrityError, match="current user session"):
        _resolve(authority_harness, state, user_id="mallory")

    current = asyncio.run(authority_harness.sessions.get_current_state(authority_harness.session_id))
    assert current is not None and (current.id, current.version) == (record.id, record.version)
    assert state_from_record(current) == state

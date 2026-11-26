"""What the next blob operation does with a deletion a dead process left half-done.

Every blob create, read and delete first reconciles the session's unfinished
deletions (``BlobServiceImpl._reconcile_blob_deletions_locked``). That step
either puts a blob's bytes back or destroys them, so each test here strands a
real deletion at one committed phase, starts a fresh service the way a restart
would, and checks what a *different* operation leaves behind:

* a deletion that had not committed is undone: the blob reads back with its
  exact bytes;
* a deletion that had committed is finished: the bytes are gone;
* a read-only operation never destroys bytes and never retires an obligation;
* evidence that does not match what the deletion recorded stops the
  operation and leaves every byte and the obligation where they were.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import select

from elspeth.contracts.blobs import BlobDeletionPlan, BlobIntegrityError, BlobNotFoundError, BlobRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table
from elspeth.web.sessions.protocol import SessionOperationMutationTransaction
from tests.helpers.session_fences import fenced_operation_context, seed_live_compose_context
from tests.unit.web.blobs import test_service as service_fixtures

blob_service = service_fixtures.blob_service
compose_context = service_fixtures.compose_context
db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id

_CONTENT = b"bytes the deletion recorded"
_Phase = Literal["intent", "staged", "purge_pending"]
_UNCOMMITTED: tuple[_Phase, ...] = ("intent", "staged")
# Both are Tier-1 refusals: the deletion ledger's own evidence check and the
# stored-hash check on the blob's bytes. Which one fires first is not the point.
_INTEGRITY_REFUSALS = (AuditIntegrityError, BlobIntegrityError)


async def _strand_deletion(
    phase: _Phase,
    *,
    blob_service: BlobServiceImpl,
    session_id: UUID,
    compose_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> BlobRecord:
    """Create a blob, then stop its deletion right after ``phase`` commits.

    The commit's acknowledgement is lost and the database refuses every later
    observation, so the deleting operation can neither finish nor undo its
    work: what a process that died at that point leaves behind.
    """
    record = await blob_service.create_blob(session_id, "doomed.csv", _CONTENT, "text/csv", session_operation_context=compose_context)
    authority = blob_service._session_operation_authority
    original_mutate = authority.mutate
    original_cas = authority.compare_and_swap
    committed = False

    def lose_ack[T](context: SessionOperationContext, mutation: Callable[[SessionOperationMutationTransaction], T]) -> T:
        nonlocal committed
        if committed:
            raise ConnectionError("database unavailable after the process lost its acknowledgement")
        result = original_mutate(context, mutation)
        if isinstance(result, BlobDeletionPlan) and result.phase == phase:
            committed = True
            raise ConnectionError("deletion commit acknowledgement lost")
        return result

    def refuse_observation(context: SessionOperationContext) -> None:
        if committed:
            raise ConnectionError("database unavailable after the process lost its acknowledgement")
        original_cas(context)

    monkeypatch.setattr(authority, "mutate", lose_ack)
    monkeypatch.setattr(authority, "compare_and_swap", refuse_observation)
    with pytest.raises(ConnectionError):
        await blob_service.delete_blob(record.id, session_operation_context=compose_context)
    assert committed, "the deletion must reach the phase under test"
    return record


def _obligation_phases(db_engine) -> list[str]:
    with db_engine.connect() as connection:
        return list(connection.execute(select(blob_deletion_cleanups_table.c.phase)).scalars())


def _blob_ids(db_engine) -> list[str]:
    with db_engine.connect() as connection:
        return list(connection.execute(select(blobs_table.c.id)).scalars())


def _tombstone(db_engine) -> Path:
    with db_engine.connect() as connection:
        return Path(connection.execute(select(blob_deletion_cleanups_table.c.tombstone_path)).scalar_one())


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", _UNCOMMITTED)
async def test_uncommitted_deletion_is_undone_by_the_next_create(
    phase: _Phase,
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await _strand_deletion(
        phase, blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )
    assert _obligation_phases(db_engine) == [phase]

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    other = await restarted.create_blob(session_id, "other.csv", b"unrelated", "text/csv", session_operation_context=successor)

    assert _obligation_phases(db_engine) == []
    assert sorted(_blob_ids(db_engine)) == sorted([str(record.id), str(other.id)])
    # Checked on disk before any read: a read has its own healing step, and
    # the create's reconciliation must already have put the bytes back.
    assert Path(record.storage_path).read_bytes() == _CONTENT
    assert sorted(path.name for path in Path(record.storage_path).parent.iterdir()) == sorted(
        [Path(record.storage_path).name, Path(other.storage_path).name]
    )
    assert await restarted.read_blob_content(record.id, session_operation_context=successor) == _CONTENT


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", _UNCOMMITTED)
async def test_read_only_operation_restores_bytes_but_leaves_the_obligation_for_a_writer(
    phase: _Phase,
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read can put bytes back so it can serve them; retiring the obligation is a write it may not make."""
    record = await _strand_deletion(
        phase, blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )

    restarted = BlobServiceImpl(db_engine, tmp_path)
    with fenced_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.BLOB_READ) as reader:
        assert await restarted.read_blob_content(record.id, session_operation_context=reader) == _CONTENT

    assert Path(record.storage_path).read_bytes() == _CONTENT
    assert _obligation_phases(db_engine) == [phase]
    assert _blob_ids(db_engine) == [str(record.id)]


@pytest.mark.asyncio
async def test_committed_deletion_is_finished_by_the_next_create(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await _strand_deletion(
        "purge_pending", blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )
    tombstone = _tombstone(db_engine)
    assert tombstone.read_bytes() == _CONTENT
    assert _blob_ids(db_engine) == []

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    other = await restarted.create_blob(session_id, "other.csv", b"unrelated", "text/csv", session_operation_context=successor)

    assert _obligation_phases(db_engine) == []
    assert _blob_ids(db_engine) == [str(other.id)]
    assert [path.name for path in Path(record.storage_path).parent.iterdir()] == [Path(other.storage_path).name]


@pytest.mark.asyncio
async def test_committed_deletion_is_finished_even_when_the_bytes_are_back_at_the_canonical_path(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is already gone, so bytes at the canonical path belong to nothing and must not survive."""
    record = await _strand_deletion(
        "purge_pending", blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )
    storage = Path(record.storage_path)
    _tombstone(db_engine).rename(storage)

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    other = await restarted.create_blob(session_id, "other.csv", b"unrelated", "text/csv", session_operation_context=successor)

    assert _obligation_phases(db_engine) == []
    assert [path.name for path in storage.parent.iterdir()] == [Path(other.storage_path).name]


@pytest.mark.asyncio
async def test_read_only_operation_leaves_a_committed_deletion_untouched(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await _strand_deletion(
        "purge_pending", blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )
    tombstone = _tombstone(db_engine)

    restarted = BlobServiceImpl(db_engine, tmp_path)
    # The blob's row is already gone, so the read finds nothing; what matters
    # is that reconciliation ran first and destroyed nothing.
    with (
        fenced_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.BLOB_READ) as reader,
        pytest.raises(BlobNotFoundError),
    ):
        await restarted.read_blob_content(record.id, session_operation_context=reader)

    assert tombstone.read_bytes() == _CONTENT
    assert _obligation_phases(db_engine) == ["purge_pending"]


def _replace_canonical_bytes(record: BlobRecord, tombstone: Path) -> None:
    Path(record.storage_path).write_bytes(b"not the bytes the deletion recorded")
    if tombstone.exists():
        tombstone.unlink()


def _keep_both_copies(record: BlobRecord, tombstone: Path) -> None:
    storage = Path(record.storage_path)
    source, target = (storage, tombstone) if storage.exists() else (tombstone, storage)
    target.write_bytes(source.read_bytes())


def _lose_every_copy(record: BlobRecord, tombstone: Path) -> None:
    for path in (Path(record.storage_path), tombstone):
        if path.exists():
            path.unlink()


@pytest.mark.asyncio
async def test_staged_deletion_whose_set_aside_bytes_changed_is_not_restored(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undoing a deletion puts back the recorded bytes or nothing: altered set-aside bytes never become the blob."""
    record = await _strand_deletion(
        "staged", blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )
    tombstone = _tombstone(db_engine)
    tombstone.write_bytes(b"not the bytes the deletion recorded")

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    with pytest.raises(_INTEGRITY_REFUSALS):
        await restarted.create_blob(session_id, "other.csv", b"unrelated", "text/csv", session_operation_context=successor)

    assert not Path(record.storage_path).exists()
    assert tombstone.read_bytes() == b"not the bytes the deletion recorded"
    assert _obligation_phases(db_engine) == ["staged"]
    assert _blob_ids(db_engine) == [str(record.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", _UNCOMMITTED)
@pytest.mark.parametrize(
    "damage",
    [_replace_canonical_bytes, _keep_both_copies, _lose_every_copy],
    ids=["different-bytes", "both-copies-present", "no-copy-left"],
)
async def test_uncommitted_deletion_with_unexpected_evidence_stops_the_next_operation(
    phase: _Phase,
    damage: Callable[[BlobRecord, Path], None],
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery never guesses: evidence that differs from the recorded deletion is left exactly as found."""
    record = await _strand_deletion(
        phase, blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )
    damage(record, _tombstone(db_engine))
    session_dir = Path(record.storage_path).parent
    before = {path.name: path.read_bytes() for path in session_dir.iterdir()}

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    with pytest.raises(_INTEGRITY_REFUSALS):
        await restarted.create_blob(session_id, "other.csv", b"unrelated", "text/csv", session_operation_context=successor)

    assert {path.name: path.read_bytes() for path in session_dir.iterdir()} == before
    assert _obligation_phases(db_engine) == [phase]
    assert _blob_ids(db_engine) == [str(record.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [_replace_canonical_bytes, _keep_both_copies], ids=["different-bytes", "both-copies-present"])
async def test_committed_deletion_with_unexpected_canonical_bytes_destroys_nothing(
    damage: Callable[[BlobRecord, Path], None],
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bytes the deletion never recorded are not this deletion's to destroy."""
    record = await _strand_deletion(
        "purge_pending", blob_service=blob_service, session_id=session_id, compose_context=compose_context, monkeypatch=monkeypatch
    )
    damage(record, _tombstone(db_engine))
    session_dir = Path(record.storage_path).parent
    before = {path.name: path.read_bytes() for path in session_dir.iterdir()}

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    with pytest.raises(_INTEGRITY_REFUSALS):
        await restarted.create_blob(session_id, "other.csv", b"unrelated", "text/csv", session_operation_context=successor)

    assert {path.name: path.read_bytes() for path in session_dir.iterdir()} == before
    assert _obligation_phases(db_engine) == ["purge_pending"]

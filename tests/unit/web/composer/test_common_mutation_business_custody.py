"""Automatic business writes use the locked mode and filesystem custody."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.blobs import BlobDeletionPlan, BlobRecord, BlobReplacementPlan
from elspeth.contracts.composer_mutation import ComposerAutoMutationRefused, ComposerMutationCeiling
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs import replacement as replacement_module
from elspeth.web.blobs import service as blob_module
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.service import BlobServiceImpl, _persist_blob_content, content_hash
from elspeth.web.composer.tools.blobs import _persist_prepared_blob_create, _prepare_blob_create
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions import service as session_module
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.locking import _blob_custody_session_lock
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blob_replacement_cleanups_table,
    blobs_table,
    sessions_table,
)
from tests.unit.web.coordination import test_composer_operation_authority as operation_fixtures
from tests.unit.web.coordination.test_composer_operation_authority import (
    cancelled,
    start,
)

operation_store = operation_fixtures.operation_store


async def _mode(store, mode):
    await store[3].update_composer_preferences(store[4], trust_mode=mode, density_default="high", actor="user:alice")


def _rows(engine, sid, tables):
    with engine.connect() as conn:
        return tuple(
            tuple(conn.execute(select(table).where(table.c.session_id == str(sid)).order_by(*table.primary_key.columns)).all())
            for table in tables
        )


def _snapshot(engine, sid, data_dir):
    rows = _rows(engine, sid, (blobs_table, blob_deletion_cleanups_table, blob_replacement_cleanups_table))
    directory = Path(data_dir) / "blobs" / str(sid)
    files = tuple((str(path.relative_to(directory)), path.read_bytes()) for path in sorted(directory.rglob("*")) if path.is_file())
    return rows, files


def _create(engine, repository, data_dir, context, ceiling, blob_id):
    return _persist_blob_content(
        engine=engine,
        data_dir=Path(data_dir),
        max_storage_per_session=1_000_000,
        blob_id=blob_id,
        session_id=context.fence.session_id,
        filename="custody.csv",
        content=b"value\n43\n",
        mime_type="text/csv",
        created_by="user",
        source_description=None,
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
        idempotent=False,
        session_operation_context=context,
        session_operation_authority=repository,
        auto_mutation=ceiling,
    )


def _effect(engine, repository, data_dir, context, ceiling, blob_id, action):
    if action == "create":
        return _create(engine, repository, data_dir, context, ceiling, blob_id)
    if action == "replace":
        old = repository.mutate(context, lambda tx: tx.blobs.read_blob(blob_id=blob_id))
        content = b"value\n43\n"
        return BlobReplacementCoordinator(engine=engine, data_dir=Path(data_dir), session_operation_authority=repository).replace_blob(
            expected=old,
            replacement=replace(old, size_bytes=len(content), content_hash=content_hash(content)),
            content=content,
            context=context,
            max_storage_per_session=1_000_000,
            accepting_proposal_id=None,
            auto_mutation=ceiling,
        )
    assert action == "delete"
    blobs = BlobServiceImpl(engine, Path(data_dir), session_operation_authority=repository)
    with _blob_custody_session_lock(engine, context.fence.session_id):
        return blobs._delete_blob_with_ledger(blob_id=blob_id, context=context, auto_mutation=ceiling)


@pytest.mark.asyncio
async def test_preferences_acquire_custody_before_session_sql(operation_store, monkeypatch):
    service = operation_store[3]
    entered = []
    original_custody = session_module._blob_custody_session_lock
    original_begin = service._session_process_locked_begin

    @contextmanager
    def custody(*args, **kwargs):
        with original_custody(*args, **kwargs) as held:
            entered.append("custody")
            yield held

    @contextmanager
    def begin(*args, **kwargs):
        with original_begin(*args, **kwargs) as conn:
            entered.append("session")
            yield conn

    monkeypatch.setattr(session_module, "_blob_custody_session_lock", custody)
    monkeypatch.setattr(service, "_session_process_locked_begin", begin)
    await _mode(operation_store, "auto_commit")
    assert entered == ["custody", "session"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,current",
    [
        ("auto_commit", "auto_commit"),
        ("explicit_approve", "auto_commit"),
        ("auto_commit", "explicit_approve"),
        ("explicit_approve", "explicit_approve"),
    ],
)
async def test_business_callback_requires_initial_and_current_auto(operation_store, initial, current):
    engine, repository, _authority, _service, sid = operation_store
    await _mode(operation_store, current)
    _, running = start(operation_store)
    context = running.session_operation_context
    ceiling = ComposerMutationCeiling(str(sid), "synthetic-business-request", initial)
    with engine.connect() as conn:
        before = conn.execute(select(sessions_table).where(sessions_table.c.id == str(sid))).one()
    entered = []

    def business(tx):
        entered.append(True)
        tx.session.set_title(title="guarded business", updated_at=tx.database_now)

    try:
        if initial == current == "auto_commit":
            repository.mutate(context, business, auto_mutation=ceiling)
            assert entered == [True]
            with engine.connect() as conn:
                assert (
                    conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == str(sid))).scalar_one() == "guarded business"
                )
        else:
            with pytest.raises(ComposerAutoMutationRefused) as refused:
                repository.mutate(context, business, auto_mutation=ceiling)
            assert refused.value.revocation.ceiling is ceiling
            assert refused.value.revocation.current_trust_mode == current
            assert entered == []
            with engine.connect() as conn:
                assert conn.execute(select(sessions_table).where(sessions_table.c.id == str(sid))).one() == before
        # Ordinary audit/manual/proposal callers retain their original mode-independent default.
        repository.mutate(context, lambda tx: tx.session.set_title(title="ordinary default", updated_at=tx.database_now))
        renewed = repository.renew(context, lease_seconds=30)
        assert renewed.fence == context.fence
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["foreign_session", "non_compose", "not_owned", "corrupt_current"])
async def test_business_guard_refuses_wrong_or_corrupt_authority_without_callback(operation_store, bad):
    engine, repository, _authority, _service, sid = operation_store
    await _mode(operation_store, "auto_commit")
    _, running = start(operation_store)
    context = running.session_operation_context
    ceiling = ComposerMutationCeiling(str(uuid4()) if bad == "foreign_session" else str(sid), "synthetic-business-request", "auto_commit")
    if bad == "non_compose":
        repository.release(context)
        context = repository.acquire(
            session_id=sid, operation_kind=SessionOperationKind.PROPOSAL, owner_instance_id="test-owner", lease_seconds=30
        )
    if bad == "corrupt_current":
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.exec_driver_sql("ALTER TABLE sessions DROP CONSTRAINT ck_sessions_trust_mode")
            else:
                conn.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(trust_mode="corrupt"))
    with engine.connect() as conn:
        before = conn.execute(select(sessions_table).where(sessions_table.c.id == str(sid))).one()
    entered = []
    try:
        with pytest.raises(TypeError if bad == "not_owned" else AuditIntegrityError):
            repository.mutate(context, lambda tx: entered.append(tx), auto_mutation=object() if bad == "not_owned" else ceiling)
        assert entered == []
        with engine.connect() as conn:
            assert conn.execute(select(sessions_table).where(sessions_table.c.id == str(sid))).one() == before
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_existing_durable_stop_is_checked_before_mutation_mode(operation_store):
    _engine, repository, authority, _service, sid = operation_store
    record, running = start(operation_store)
    context = running.session_operation_context
    authority.request_cancel(session_id=sid, operation_id=record.operation_id, cancelled_failure=cancelled)
    ceiling = ComposerMutationCeiling(str(sid), "synthetic-stopped-request", "explicit_approve")
    entered = []
    try:
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            repository.mutate(context, lambda tx: entered.append(tx), auto_mutation=ceiling)
        assert entered == []
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("current,guarded", [("auto_commit", True), ("explicit_approve", True), ("explicit_approve", False)])
async def test_prepared_blob_helper_forwards_optional_business_ceiling(operation_store, tmp_path, current, guarded):
    engine, repository, _authority, _service, sid = operation_store
    await _mode(operation_store, current)
    _, running = start(operation_store)
    context = running.session_operation_context
    data_dir = tmp_path / "actual-prepared-custody"
    prepared = _prepare_blob_create(
        filename="custody.csv",
        mime_type="text/csv",
        content="value\n43\n",
        data_dir=str(data_dir),
        session_id=str(sid),
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
    )
    ceiling = ComposerMutationCeiling(str(sid), "synthetic-prepared-request", "auto_commit") if guarded else None
    before = _snapshot(engine, sid, data_dir)
    try:
        if guarded and current == "explicit_approve":
            with pytest.raises(ComposerAutoMutationRefused):
                _persist_prepared_blob_create(
                    prepared,
                    session_engine=engine,
                    session_id=str(sid),
                    session_operation_context=context,
                    session_operation_authority=repository,
                    auto_mutation=ceiling,
                )
            assert _snapshot(engine, sid, data_dir) == before
        else:
            assert (
                _persist_prepared_blob_create(
                    prepared,
                    session_engine=engine,
                    session_id=str(sid),
                    session_operation_context=context,
                    session_operation_authority=repository,
                    auto_mutation=ceiling,
                )
                is None
            )
            after = _snapshot(engine, sid, data_dir)
            assert len(after[0][0]) == len(after[1]) == 1
            assert after[1][0][1] == b"value\n43\n"
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "replace", "delete"])
@pytest.mark.parametrize("current", ["auto_commit", "explicit_approve"])
async def test_real_blob_business_drivers_preserve_rows_and_bytes_on_refusal(operation_store, tmp_path, monkeypatch, action, current):
    engine, repository, _authority, _service, sid = operation_store
    await _mode(operation_store, current)
    _, running = start(operation_store)
    context = running.session_operation_context
    data_dir = tmp_path / "actual-blob-business"
    service = BlobServiceImpl(engine, data_dir, session_operation_authority=repository)
    blob_id = uuid4()
    if action != "create":
        seeded = await service.create_blob(sid, "custody.csv", b"value\n42\n", "text/csv", session_operation_context=context)
        blob_id = seeded.id
    before = _snapshot(engine, sid, data_dir)
    ceiling = ComposerMutationCeiling(str(sid), "synthetic-blob-request", "auto_commit")
    entered = []
    original_write = blob_module._atomic_write_blob
    original_replace_write = replacement_module._atomic_write_blob
    original_delete = BlobServiceImpl._stage_blob_deletion_plan

    def write(*args, **kwargs):
        entered.append("create")
        return original_write(*args, **kwargs)

    def replace_write(*args, **kwargs):
        entered.append("replace")
        return original_replace_write(*args, **kwargs)

    def delete(self, **kwargs):
        entered.append("delete")
        return original_delete(self, **kwargs)

    monkeypatch.setattr(blob_module, "_atomic_write_blob", write)
    monkeypatch.setattr(replacement_module, "_atomic_write_blob", replace_write)
    monkeypatch.setattr(BlobServiceImpl, "_stage_blob_deletion_plan", delete)
    try:
        if current == "explicit_approve":
            with pytest.raises(ComposerAutoMutationRefused):
                _effect(engine, repository, data_dir, context, ceiling, blob_id, action)
            assert _snapshot(engine, sid, data_dir) == before
            assert entered == [], "Refused business intent entered its filesystem phase"
        else:
            _effect(engine, repository, data_dir, context, ceiling, blob_id, action)
            assert entered == [action]
            after = _snapshot(engine, sid, data_dir)
            assert after[0][1:] == ((), ())
            if action == "delete":
                assert after == (((), (), ()), ())
            else:
                assert len(after[0][0]) == len(after[1]) == 1
                assert after[1][0][1] == b"value\n43\n"
                assert after[0][0][0].status == "ready"
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,after_phase", [("create", "pending"), ("replace", "intent"), ("replace", "swap_pending"), ("delete", "staged")]
)
async def test_each_blob_business_transition_reads_current_mode_in_its_transaction(
    operation_store,
    tmp_path,
    monkeypatch,
    action,
    after_phase,
):
    """A controlled SQL mode change isolates each later business transition."""
    engine, repository, _authority, _service, sid = operation_store
    await _mode(operation_store, "auto_commit")
    _, running = start(operation_store)
    context = running.session_operation_context
    ceiling = ComposerMutationCeiling(str(sid), "synthetic-transition-request", "auto_commit")
    data_dir = tmp_path / "actual-transition-guard"
    blobs = BlobServiceImpl(engine, data_dir, session_operation_authority=repository)
    blob_id = uuid4()
    if action != "create":
        blob_id = (await blobs.create_blob(sid, "custody.csv", b"value\n42\n", "text/csv", session_operation_context=context)).id
    before = _snapshot(engine, sid, data_dir)
    changed = []
    advanced = []
    original_mutate = repository.mutate

    def change_after_committed_phase(actual_context, callback, *, auto_mutation=None):
        value = original_mutate(actual_context, callback, auto_mutation=auto_mutation)
        if changed and type(value) is BlobReplacementPlan and value.phase != after_phase:
            advanced.append(value.phase)
        selected = (
            (action == "create" and type(value) is BlobRecord and value.status == after_phase)
            or (action == "replace" and type(value) is BlobReplacementPlan and value.phase == after_phase)
            or (action == "delete" and type(value) is BlobDeletionPlan and value.phase == after_phase)
        )
        if selected and not changed:
            # Deliberate test-only direct SQL bypass isolates the next locked
            # business predicate; real preference writers cannot cross custody.
            with engine.begin() as conn:
                conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(trust_mode="explicit_approve"))
            changed.append(value)
        return value

    monkeypatch.setattr(repository, "mutate", change_after_committed_phase)
    try:
        with pytest.raises(ComposerAutoMutationRefused):
            _effect(engine, repository, data_dir, context, ceiling, blob_id, action)
        assert len(changed) == 1
        assert advanced == [], "A later business transition committed after the current-mode change"
        assert _snapshot(engine, sid, data_dir) == before
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "replace", "delete"])
async def test_guarded_blob_commit_ack_loss_preserves_actual_committed_result(operation_store, tmp_path, monkeypatch, action):
    engine, repository, _authority, _service, sid = operation_store
    await _mode(operation_store, "auto_commit")
    _, running = start(operation_store)
    context = running.session_operation_context
    data_dir = tmp_path / "actual-uncertain-commit"
    service = BlobServiceImpl(engine, data_dir, session_operation_authority=repository)
    blob_id = uuid4()
    if action != "create":
        blob_id = (await service.create_blob(sid, "custody.csv", b"value\n42\n", "text/csv", session_operation_context=context)).id
    ceiling = ComposerMutationCeiling(str(sid), "synthetic-ack-request", "auto_commit")
    original_mutate = repository.mutate
    lost = []

    def lose_ack(actual_context, callback, *, auto_mutation=None):
        value = original_mutate(actual_context, callback, auto_mutation=auto_mutation)
        selected = (
            (action == "create" and type(value) is BlobRecord and value.status == "ready")
            or (action == "replace" and type(value) is BlobReplacementPlan and value.phase == "purge_pending")
            or (action == "delete" and type(value) is BlobDeletionPlan and value.phase == "purge_pending")
        )
        if selected and not lost:
            assert auto_mutation is ceiling
            lost.append(value)
            raise ConnectionError("actual committed business acknowledgement lost")
        return value

    monkeypatch.setattr(repository, "mutate", lose_ack)
    try:
        _effect(engine, repository, data_dir, context, ceiling, blob_id, action)
        assert len(lost) == 1
        after = _snapshot(engine, sid, data_dir)
        assert after[0][1:] == ((), ())
        if action == "delete":
            assert after == (((), (), ()), ())
        else:
            assert len(after[0][0]) == len(after[1]) == 1
            assert after[0][0][0].status == "ready"
            assert after[1][0][1] == b"value\n43\n"
    finally:
        repository.release(context)


def _custody_child(database_url, data_dir, context, ceiling, blob_id, action, entered, release, results):
    """A fresh process owns the actual DB engine and actual filesystem phase."""
    engine = create_session_engine(database_url)
    repository = (
        PostgresSessionOperationRepository(engine) if engine.dialect.name == "postgresql" else SQLiteLocalSessionOperationAuthority(engine)
    )
    original_write = blob_module._atomic_write_blob
    original_replace_write = replacement_module._atomic_write_blob
    original_delete = BlobServiceImpl._commit_blob_deletion_plan

    def pause():
        entered.set()
        assert release.wait(15), "Parent did not release actual filesystem custody"

    def write(*args, **kwargs):
        pause()
        return original_write(*args, **kwargs)

    def replace_write(*args, **kwargs):
        pause()
        return original_replace_write(*args, **kwargs)

    def delete(self, **kwargs):
        pause()
        return original_delete(self, **kwargs)

    blob_module._atomic_write_blob = write
    replacement_module._atomic_write_blob = replace_write
    BlobServiceImpl._commit_blob_deletion_plan = delete
    try:
        _effect(engine, repository, data_dir, context, ceiling, UUID(blob_id), action)
    except ComposerAutoMutationRefused as refused:
        results.put(("refused", refused.revocation.current_trust_mode))
    except BaseException as error:
        results.put(("failed", type(error).__name__))
        raise
    else:
        results.put(("committed", None))
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "replace", "delete"])
@pytest.mark.parametrize("first", ["preferences", "blob"])
async def test_separate_process_blob_custody_excludes_preference_downgrade(operation_store, tmp_path, monkeypatch, action, first):
    engine, repository, _authority, service, sid = operation_store
    await _mode(operation_store, "auto_commit")
    _, running = start(operation_store)
    context = running.session_operation_context
    ceiling = ComposerMutationCeiling(str(sid), "synthetic-process-request", "auto_commit")
    data_dir = tmp_path / "actual-process-custody"
    blob_id = uuid4()
    if action != "create":
        blobs = BlobServiceImpl(engine, data_dir, session_operation_authority=repository)
        blob_id = (await blobs.create_blob(sid, "custody.csv", b"value\n42\n", "text/csv", session_operation_context=context)).id
    before = _snapshot(engine, sid, data_dir)
    process_context = multiprocessing.get_context("spawn")
    entered, release = process_context.Event(), process_context.Event()
    results = process_context.Queue()
    attempted = threading.Event()
    original_run_sync = service._run_sync

    async def observe_worker(func, *args, **kwargs):
        def entered_worker():
            attempted.set()
            return func(*args, **kwargs)

        return await original_run_sync(entered_worker)

    monkeypatch.setattr(service, "_run_sync", observe_worker)
    preference = None
    process = process_context.Process(
        target=_custody_child,
        args=(
            engine.url.render_as_string(hide_password=False),
            str(data_dir),
            context,
            ceiling,
            str(blob_id),
            action,
            entered,
            release,
            results,
        ),
    )
    try:
        if first == "preferences":
            await _mode(operation_store, "explicit_approve")
            release.set()
        process.start()
        if first == "blob":
            assert await asyncio.to_thread(entered.wait, 10), "Child did not enter actual filesystem phase"
            preference = asyncio.create_task(_mode(operation_store, "explicit_approve"))
            assert await asyncio.to_thread(attempted.wait, 10), "Preference worker was not submitted"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(preference), 0.2)
            assert (await service.get_composer_preferences(sid)).trust_mode == "auto_commit"
            # PostgreSQL has distinct advisory classes. SQLite's existing file
            # custody intentionally shares its process session exclusion.
            if engine.dialect.name == "postgresql":
                repository.renew(context, lease_seconds=30)
            release.set()
        await asyncio.to_thread(process.join, 20)
        assert not process.is_alive()
        assert process.exitcode == 0
        outcome = results.get(timeout=2)
        if preference is not None:
            await preference
        assert (await service.get_composer_preferences(sid)).trust_mode == "explicit_approve"
        after = _snapshot(engine, sid, data_dir)
        if first == "preferences":
            assert outcome == ("refused", "explicit_approve")
            assert not entered.is_set(), "Refused business intent reached filesystem work"
            assert after == before
        else:
            assert outcome == ("committed", None)
            assert after[0][1:] == ((), ())
            if action == "delete":
                assert after == (((), (), ()), ())
            else:
                assert len(after[0][0]) == len(after[1]) == 1
                assert after[1][0][1] == b"value\n43\n"
    finally:
        release.set()
        if process.pid is not None:
            await asyncio.to_thread(process.join, 20)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        if preference is not None:
            await asyncio.gather(preference, return_exceptions=True)
        results.close()
        results.join_thread()
        repository.release(context)

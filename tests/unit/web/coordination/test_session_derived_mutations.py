"""Custody proofs for fenced mutations over Sessions-derived tables."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, insert, select

from elspeth.contracts.blobs import blob_record_snapshot_hash
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import (
    blob_inline_resolutions_table,
    blob_run_links_table,
    blobs_table,
    composition_states_table,
    proposal_blob_effect_receipts_table,
    run_events_table,
    runs_table,
)


def _create(authority: SQLiteLocalSessionOperationAuthority, *, title: str):
    return authority.create_session_with_initial_fence(
        user_id="alice",
        title=title,
        auth_provider_type="local",
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )


def _seed_run_and_blob(
    engine: Engine,
    *,
    session_id: UUID,
    content_hash: str,
    size_bytes: int,
) -> tuple[UUID, UUID]:
    now = datetime.now(UTC)
    state_id = uuid4()
    run_id = uuid4()
    blob_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id),
                session_id=str(session_id),
                version=1,
                source=None,
                sources=None,
                nodes=None,
                edges=None,
                outputs=None,
                metadata_=None,
                is_valid=True,
                validation_errors=None,
                composer_meta=None,
                created_at=now,
                derived_from_state_id=None,
                provenance="session_seed",
            )
        )
        conn.execute(
            insert(runs_table).values(
                id=str(run_id),
                session_id=str(session_id),
                state_id=str(state_id),
                status="pending",
                started_at=now,
                finished_at=None,
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )
        )
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(session_id),
                filename=f"{blob_id}.txt",
                mime_type="text/plain",
                size_bytes=size_bytes,
                content_hash=content_hash,
                storage_path=f"/tmp/{blob_id}.txt",
                created_at=now,
                created_by="user",
                source_description=None,
                status="ready",
                creation_modality="verbatim",
            )
        )
    return run_id, blob_id


def _acquire(authority: SQLiteLocalSessionOperationAuthority, *, session_id: UUID):
    return authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )


def test_derived_mutations_reject_foreign_parents_and_raw_execute(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    owned = _create(authority, title="owned")
    foreign = _create(authority, title="foreign")
    _owned_run, owned_blob = _seed_run_and_blob(engine, session_id=owned.id, content_hash="a" * 64, size_bytes=3)
    foreign_run, foreign_blob = _seed_run_and_blob(engine, session_id=foreign.id, content_hash="b" * 64, size_bytes=5)
    fence = _acquire(authority, session_id=owned.id)

    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.runs.append_run_event(
                run_id=foreign_run,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"phase": "forbidden"},
            ),
        )
    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.blobs.insert_blob_run_link(
                blob_id=foreign_blob,
                run_id=_owned_run,
                direction="input",
            ),
        )
    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.blobs.insert_blob_run_link(
                blob_id=owned_blob,
                run_id=foreign_run,
                direction="input",
            ),
        )

    def assert_no_raw_execute(transaction) -> None:
        assert not hasattr(transaction, "execute")
        assert not hasattr(transaction.runs, "execute")

    authority.mutate(fence, assert_no_raw_execute)

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(blob_run_links_table)).scalar_one() == 0


def test_compose_prepares_durable_blob_replacement_intent(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="durable replacement")
    run_id, blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    with engine.begin() as conn:
        conn.execute(runs_table.update().where(runs_table.c.id == str(run_id)).values(status="completed"))
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    expected = authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement = replace(expected, size_bytes=7, content_hash="b" * 64)

    plan = authority.mutate(
        context,
        lambda transaction: transaction.blobs.prepare_blob_replacement(
            replacement_id=uuid4(),
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.replacement-stage",
            backup_path=f"{expected.storage_path}.replacement-backup",
            max_storage_per_session=100,
            accepting_proposal_id=None,
        ),
    )

    assert plan.phase == "intent"
    assert plan.old_blob == expected
    assert plan.replacement_blob == replacement
    assert plan.operation_id == context.fence.operation_id
    assert plan.operation_epoch == context.fence.operation_epoch
    assert plan.operation_kind is SessionOperationKind.COMPOSE


def test_non_proposal_blob_mutations_cannot_inherit_proposal_retention_exclusion(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="non-proposal retention exclusion")
    run_id, blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    with engine.begin() as conn:
        conn.execute(runs_table.update().where(runs_table.c.id == str(run_id)).values(status="completed"))
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    expected = authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement = replace(expected, size_bytes=7, content_hash="b" * 64)
    asserted_proposal_id = uuid4()

    with pytest.raises(AuditIntegrityError, match="non-proposal blob replacement cannot exclude proposal retention"):
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.prepare_blob_replacement(
                replacement_id=uuid4(),
                expected=expected,
                replacement=replacement,
                staging_path=f"{expected.storage_path}.replacement-stage",
                backup_path=f"{expected.storage_path}.replacement-backup",
                max_storage_per_session=100,
                accepting_proposal_id=asserted_proposal_id,
            ),
        )
    with pytest.raises(AuditIntegrityError, match="non-proposal blob deletion cannot exclude proposal retention"):
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.prepare_blob_deletion(
                blob_id=blob_id,
                tombstone_path=f"{expected.storage_path}.delete",
                blob_snapshot_hash=blob_record_snapshot_hash(expected),
                expected_file_present=True,
                expected_file_size=expected.size_bytes,
                expected_file_hash=expected.content_hash,
                accepting_proposal_id=asserted_proposal_id,
            ),
        )


def test_blob_replacement_ledger_commits_exact_metadata_and_retires(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="replacement lifecycle")
    run_id, blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    with engine.begin() as conn:
        conn.execute(runs_table.update().where(runs_table.c.id == str(run_id)).values(status="completed"))
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    expected = authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement = replace(expected, size_bytes=7, content_hash="b" * 64)
    replacement_id = uuid4()
    plan = authority.mutate(
        context,
        lambda transaction: transaction.blobs.prepare_blob_replacement(
            replacement_id=replacement_id,
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.{replacement_id}.stage",
            backup_path=f"{expected.storage_path}.{replacement_id}.backup",
            max_storage_per_session=100,
            accepting_proposal_id=None,
        ),
    )

    assert (
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.read_blob_replacement(blob_id=blob_id),
        )
        == plan
    )
    assert authority.mutate(context, lambda transaction: transaction.blobs.list_blob_replacements()) == (plan,)
    staged = authority.mutate(
        context,
        lambda transaction: transaction.blobs.mark_blob_replacement_staged(plan=plan),
    )
    assert staged.phase == "swap_pending"
    committed = authority.mutate(
        context,
        lambda transaction: transaction.blobs.commit_blob_replacement(
            plan=staged,
            max_storage_per_session=100,
            accepting_proposal_id=None,
        ),
    )
    assert committed.phase == "purge_pending"
    assert authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id)) == replacement
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(proposal_blob_effect_receipts_table)).scalar_one() == 0
    assert (
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.retire_blob_replacement(plan=committed),
        )
        is True
    )
    assert (
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.read_blob_replacement(blob_id=blob_id),
        )
        is None
    )


def test_blob_deletion_and_replacement_ledgers_exclude_each_other_and_invocations(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="replacement exclusion")
    run_id, blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    with engine.begin() as conn:
        conn.execute(runs_table.update().where(runs_table.c.id == str(run_id)).values(status="completed"))
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    expected = authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement = replace(expected, size_bytes=7, content_hash="b" * 64)
    replacement_id = uuid4()
    plan = authority.mutate(
        context,
        lambda transaction: transaction.blobs.prepare_blob_replacement(
            replacement_id=replacement_id,
            expected=expected,
            replacement=replacement,
            staging_path=f"{expected.storage_path}.{replacement_id}.stage",
            backup_path=f"{expected.storage_path}.{replacement_id}.backup",
            max_storage_per_session=100,
            accepting_proposal_id=None,
        ),
    )
    with pytest.raises(AuditIntegrityError, match="different blob replacement invocation"):
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.prepare_blob_replacement(
                replacement_id=uuid4(),
                expected=expected,
                replacement=replacement,
                staging_path=f"{expected.storage_path}.other.stage",
                backup_path=f"{expected.storage_path}.other.backup",
                max_storage_per_session=100,
                accepting_proposal_id=None,
            ),
        )
    with pytest.raises(AuditIntegrityError, match="replacement is in progress"):
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.prepare_blob_deletion(
                blob_id=blob_id,
                tombstone_path=f"{expected.storage_path}.delete",
                blob_snapshot_hash=blob_record_snapshot_hash(expected),
                expected_file_present=True,
                expected_file_size=expected.size_bytes,
                expected_file_hash=expected.content_hash,
                accepting_proposal_id=None,
            ),
        )
    assert (
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.abort_blob_replacement(plan=plan),
        )
        is True
    )
    deletion = authority.mutate(
        context,
        lambda transaction: transaction.blobs.prepare_blob_deletion(
            blob_id=blob_id,
            tombstone_path=f"{expected.storage_path}.delete",
            blob_snapshot_hash=blob_record_snapshot_hash(expected),
            expected_file_present=True,
            expected_file_size=expected.size_bytes,
            expected_file_hash=expected.content_hash,
            accepting_proposal_id=None,
        ),
    )
    with pytest.raises(AuditIntegrityError, match="deletion is in progress"):
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.prepare_blob_replacement(
                replacement_id=uuid4(),
                expected=expected,
                replacement=replacement,
                staging_path=f"{expected.storage_path}.third.stage",
                backup_path=f"{expected.storage_path}.third.backup",
                max_storage_per_session=100,
                accepting_proposal_id=None,
            ),
        )
    assert (
        authority.mutate(
            context,
            lambda transaction: transaction.blobs.abort_blob_deletion(plan=deletion),
        )
        is True
    )


def test_ready_blob_replacement_rejects_wrong_kind_and_quota_without_row_delta(engine: Engine) -> None:
    from elspeth.contracts.blobs import BlobQuotaExceededError

    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="replace guards")
    run_id, blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    with engine.begin() as conn:
        conn.execute(runs_table.update().where(runs_table.c.id == str(run_id)).values(status="completed"))

    execute_context = _acquire(authority, session_id=session.id)
    expected = authority.mutate(execute_context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    replacement = replace(expected, size_bytes=7, content_hash="b" * 64)
    with pytest.raises(AuditIntegrityError, match="operation kind"):
        authority.mutate(
            execute_context,
            lambda transaction: transaction.blobs.prepare_blob_replacement(
                replacement_id=uuid4(),
                expected=expected,
                replacement=replacement,
                staging_path=f"{expected.storage_path}.wrong-kind.stage",
                backup_path=f"{expected.storage_path}.wrong-kind.backup",
                max_storage_per_session=100,
                accepting_proposal_id=None,
            ),
        )
    authority.release(execute_context)

    compose_context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="sqlite-owner",
        lease_seconds=30,
    )
    with pytest.raises(BlobQuotaExceededError):
        authority.mutate(
            compose_context,
            lambda transaction: transaction.blobs.prepare_blob_replacement(
                replacement_id=uuid4(),
                expected=expected,
                replacement=replacement,
                staging_path=f"{expected.storage_path}.quota.stage",
                backup_path=f"{expected.storage_path}.quota.backup",
                max_storage_per_session=6,
                accepting_proposal_id=None,
            ),
        )
    observed = authority.mutate(compose_context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))
    assert observed == expected


def test_inline_resolution_batch_rolls_back_when_one_blob_is_foreign(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    owned = _create(authority, title="owned")
    foreign = _create(authority, title="foreign")
    owned_run, owned_blob = _seed_run_and_blob(engine, session_id=owned.id, content_hash="a" * 64, size_bytes=3)
    _foreign_run, foreign_blob = _seed_run_and_blob(engine, session_id=foreign.id, content_hash="b" * 64, size_bytes=5)
    resolutions = (
        ResolvedBlobContent(
            field_path="source.options.first",
            blob_id=owned_blob,
            content_hash="a" * 64,
            byte_length=3,
            mime_type="text/plain",
            encoding="utf-8",
        ),
        ResolvedBlobContent(
            field_path="source.options.second",
            blob_id=foreign_blob,
            content_hash="b" * 64,
            byte_length=5,
            mime_type="text/plain",
            encoding="utf-8",
        ),
    )
    fence = _acquire(authority, session_id=owned.id)

    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(
            fence,
            lambda transaction: transaction.blobs.insert_blob_inline_resolutions(
                run_id=owned_run,
                attempt=1,
                resolutions=resolutions,
                resolved_at=datetime.now(UTC),
            ),
        )

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(blob_inline_resolutions_table)).scalar_one() == 0


def test_stale_event_writer_consumes_no_sequence(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="events")
    run_id, _blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    stale = _acquire(authority, session_id=session.id)
    first = authority.mutate(
        stale,
        lambda transaction: transaction.runs.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="progress",
            data={"step": 1},
        ),
    )
    authority.release(stale)
    current = _acquire(authority, session_id=session.id)

    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(
            stale,
            lambda transaction: transaction.runs.append_run_event(
                run_id=run_id,
                timestamp=datetime.now(UTC),
                event_type="progress",
                data={"step": "stale"},
            ),
        )
    second = authority.mutate(
        current,
        lambda transaction: transaction.runs.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="completed",
            data={"step": 2},
        ),
    )
    replay = authority.mutate(current, lambda transaction: transaction.runs.list_run_events_after(run_id=run_id, after_sequence=0))

    assert (first.sequence, second.sequence) == (1, 2)
    assert tuple(event.sequence for event in replay) == (1, 2)
    assert tuple(event.data["step"] for event in replay) == (1, 2)


def test_run_event_replay_rejects_hidden_nonpositive_sequence(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="invalid sequence")
    run_id, _blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        try:
            conn.execute(
                insert(run_events_table).values(
                    id=str(uuid4()),
                    run_id=str(run_id),
                    sequence=0,
                    timestamp=datetime.now(UTC),
                    event_type="progress",
                    data={"hidden": True},
                )
            )
        finally:
            conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    fence = _acquire(authority, session_id=session.id)

    with pytest.raises(AuditIntegrityError, match="sequence"):
        authority.mutate(fence, lambda transaction: transaction.runs.list_run_events_after(run_id=run_id, after_sequence=0))


def test_run_event_replay_rejects_noncontiguous_sequence(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="sequence gap")
    run_id, _blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    with engine.begin() as conn:
        conn.execute(
            insert(run_events_table),
            (
                {
                    "id": str(uuid4()),
                    "run_id": str(run_id),
                    "sequence": 1,
                    "timestamp": datetime.now(UTC),
                    "event_type": "progress",
                    "data": {"step": 1},
                },
                {
                    "id": str(uuid4()),
                    "run_id": str(run_id),
                    "sequence": 3,
                    "timestamp": datetime.now(UTC),
                    "event_type": "completed",
                    "data": {"step": 3},
                },
            ),
        )
    fence = _acquire(authority, session_id=session.id)

    with pytest.raises(AuditIntegrityError, match="sequence"):
        authority.mutate(fence, lambda transaction: transaction.runs.list_run_events_after(run_id=run_id, after_sequence=0))


def test_run_event_append_rejects_naive_timestamp_before_write(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="naive timestamp")
    run_id, _blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    fence = _acquire(authority, session_id=session.id)

    with pytest.raises(ValueError, match="timezone-aware"):
        authority.mutate(
            fence,
            lambda transaction: transaction.runs.append_run_event(
                run_id=run_id,
                timestamp=datetime.now(UTC).replace(tzinfo=None),
                event_type="progress",
                data={},
            ),
        )
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


def test_run_event_immediate_and_replay_records_are_canonical_and_deeply_immutable(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = _create(authority, title="canonical event")
    run_id, _blob_id = _seed_run_and_blob(engine, session_id=session.id, content_hash="a" * 64, size_bytes=3)
    fence = _acquire(authority, session_id=session.id)
    payload = {"nested": {"steps": [1, 2]}}
    non_utc = datetime.now(timezone(timedelta(hours=9)))

    immediate = authority.mutate(
        fence,
        lambda transaction: transaction.runs.append_run_event(
            run_id=run_id,
            timestamp=non_utc,
            event_type="progress",
            data=payload,
        ),
    )
    replay = authority.mutate(
        fence,
        lambda transaction: transaction.runs.list_run_events_after(run_id=run_id, after_sequence=0),
    )

    assert replay == (immediate,)
    assert immediate.timestamp.tzinfo is UTC
    with pytest.raises(TypeError):
        immediate.data["nested"]["steps"][0] = 9  # type: ignore[index]
    payload["nested"]["steps"][0] = 9
    assert immediate.data["nested"]["steps"] == (1, 2)


def test_output_reads_fail_closed_on_cross_session_link(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    owned = _create(authority, title="owned")
    foreign = _create(authority, title="foreign")
    owned_run, owned_blob = _seed_run_and_blob(engine, session_id=owned.id, content_hash="a" * 64, size_bytes=3)
    _foreign_run, foreign_blob = _seed_run_and_blob(engine, session_id=foreign.id, content_hash="b" * 64, size_bytes=5)
    fence = _acquire(authority, session_id=owned.id)
    inserted = authority.mutate(
        fence,
        lambda transaction: transaction.blobs.insert_blob_run_link(blob_id=owned_blob, run_id=owned_run, direction="output"),
    )
    duplicate = authority.mutate(
        fence,
        lambda transaction: transaction.blobs.insert_blob_run_link(blob_id=owned_blob, run_id=owned_run, direction="output"),
    )
    links = authority.mutate(fence, lambda transaction: transaction.blobs.list_blob_run_links(blob_id=owned_blob))
    with engine.begin() as conn:
        conn.execute(insert(blob_run_links_table).values(blob_id=str(foreign_blob), run_id=str(owned_run), direction="output"))

    with pytest.raises(SessionDerivedCustodyError, match="derived record is unavailable"):
        authority.mutate(fence, lambda transaction: transaction.blobs.list_run_output_blobs(run_id=owned_run))

    assert inserted is True
    assert duplicate is False
    assert [(link.blob_id, link.run_id, link.direction) for link in links] == [(owned_blob, owned_run, "output")]

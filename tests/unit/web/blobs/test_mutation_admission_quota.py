"""Admission shares live storage accounting and its real refusal audit writer."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.blobs import StorageAccountingUnavailableError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl, _atomic_write_blob, content_hash
from elspeth.web.composer.tools.blobs import admit_blob_mutation
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.models import output_storage_reservations_table, quota_policies_table
from tests.helpers.session_fences import seed_live_compose_context, seed_live_operation_context
from tests.unit.web.auth.test_audit import _durable_recorder, _durable_rows, _metadata
from tests.unit.web.blobs import test_mutation_admission as admission
from tests.unit.web.blobs import test_service as service_fixtures

db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id
compose_context = service_fixtures.compose_context
admission_message = admission.admission_message


def _policies(engine, *, cap: int | None, ceiling: int | None, revoked=False):
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for name, identity, bound in (("identity", "test-user", cap), ("container", None, ceiling)):
            if bound is not None:
                connection.execute(
                    quota_policies_table.insert().values(
                        policy_id=name,
                        identity_id=identity,
                        tokens_per_day=1000,
                        storage_bytes=bound,
                        set_by_actor="operator",
                        set_at=now,
                        revoked_at=now if revoked and identity is not None else None,
                    )
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["session", "identity", "container", "both", "container-only", "none"])
async def test_independent_admission_bounds_keep_truthful_durable_quota_evidence(db_engine, session_id, compose_context, tmp_path, binding):
    recorder, url = _durable_recorder(tmp_path)
    authority = SQLiteLocalSessionOperationAuthority(db_engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    service = BlobServiceImpl(db_engine, tmp_path, session_operation_authority=authority)
    record = await service.create_blob(session_id, "quota.csv", b"old", "text/csv", session_operation_context=compose_context)
    _policies(
        db_engine,
        cap=5 if binding in {"identity", "both"} else None if binding == "container-only" else 1000,
        ceiling=5 if binding in {"container", "container-only", "both"} else 1000,
    )
    context = replace(
        admission._context(service, db_engine, compose_context, tmp_path),
        user_message_content="larger",
        max_blob_storage_per_session_bytes=5 if binding == "session" else 1000,
    )
    before = admission._resources(db_engine, tmp_path / "blobs")
    result = admit_blob_mutation("update_blob", {"blob_id": str(record.id), "content": "larger"}, admission._empty_state(), context)
    assert (result is None) is (binding == "none")
    assert admission._resources(db_engine, tmp_path / "blobs") == before
    rows = _durable_rows(url)
    if binding not in {"session", "none"}:
        assert len(rows) == 1
        payload = _metadata(rows[0])
        assert (payload["operation"], payload["usage"], payload["scope"], payload["limit"]) == (
            "blob_replacement",
            3,
            "identity" if binding in {"identity", "both"} else "container",
            5,
        )
        assert payload["cap"] == (None if binding == "container-only" else 5 if binding in {"identity", "both"} else 1000)
        assert payload["ceiling"] == (5 if binding in {"container", "container-only", "both"} else 1000)
    else:
        assert rows == []
    recorder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["revoked", "unavailable"])
@pytest.mark.parametrize(
    "content,session_cap,success", [("new", 1000, True), ("x", 1000, True), ("new", 2, False), ("x", 0, False), ("larger", 1000, None)]
)
async def test_nonpositive_growth_bypasses_policy_measurement_but_never_session_projection(
    db_engine, session_id, compose_context, tmp_path, policy, content, session_cap, success
):
    service = BlobServiceImpl(db_engine, tmp_path)
    record = await service.create_blob(session_id, "quota.csv", b"old", "text/csv", session_operation_context=compose_context)
    _policies(db_engine, cap=1, ceiling=1, revoked=policy == "revoked")
    context = replace(
        admission._context(service, db_engine, compose_context, tmp_path),
        user_message_content=content,
        max_blob_storage_per_session_bytes=session_cap,
    )
    measured = []

    def unavailable(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "quota_policies" in statement:
            measured.append(statement)
            raise OperationalError(statement, {}, RuntimeError("policy unavailable"))

    before = admission._resources(db_engine, tmp_path)
    if policy == "unavailable":
        event.listen(db_engine, "before_cursor_execute", unavailable)
    try:
        if success is None:
            with pytest.raises(StorageAccountingUnavailableError):
                admit_blob_mutation("update_blob", {"blob_id": str(record.id), "content": content}, admission._empty_state(), context)
        else:
            result = admit_blob_mutation("update_blob", {"blob_id": str(record.id), "content": content}, admission._empty_state(), context)
            assert (result is None) is success
    finally:
        if policy == "unavailable":
            event.remove(db_engine, "before_cursor_execute", unavailable)
    assert bool(measured) is (success is None and policy == "unavailable")
    assert admission._resources(db_engine, tmp_path) == before
    # Delete introduces no positive-growth accounting policy gate.
    assert admit_blob_mutation("delete_blob", {"blob_id": str(record.id)}, admission._empty_state(), context) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("coverage", ["unmatched", "partial", "full"])
@pytest.mark.parametrize("binding", ["session", "identity", "container"])
async def test_output_reservation_excess_is_charged_once_by_every_admission_bound(
    db_engine, session_id, compose_context, tmp_path, coverage, binding
):
    recorder, url = _durable_recorder(tmp_path)
    authority = SQLiteLocalSessionOperationAuthority(db_engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    service = BlobServiceImpl(db_engine, tmp_path, session_operation_authority=authority)
    run_id = await service_fixtures._seed_active_run(
        db_engine, session_id, session_operation_context=compose_context, source=service_fixtures._RUN_SOURCE, status="completed"
    )
    execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    record = service_fixtures._pending_output_record(service, session_id, "quota.csv")

    def reserve_output(transaction):
        transaction.blobs.reserve_pending_output_blob(record=record)
        transaction.blobs.insert_blob_run_link(blob_id=record.id, run_id=UUID(run_id), direction="output")

    authority.mutate(execute, reserve_output)
    reservation_size = {"unmatched": 5, "partial": 5, "full": 3}[coverage]
    authority.mutate(
        execute,
        lambda transaction: transaction.blobs.reserve_run_output_storage(
            run_id=UUID(run_id),
            target_path=str(tmp_path / "unmatched.csv") if coverage == "unmatched" else record.storage_path,
            size_bytes=reservation_size,
            max_storage_per_session=1000,
        ),
    )
    _atomic_write_blob(Path(record.storage_path), b"old", write_guard=lambda: authority.compare_and_swap(execute))
    record = authority.mutate(
        execute,
        lambda transaction: transaction.blobs.mark_run_output_blob_ready(
            run_id=UUID(run_id),
            blob_id=record.id,
            size_bytes=3,
            content_hash=content_hash(b"old"),
            max_storage_per_session=1000,
        ),
    )
    operation = seed_live_compose_context(db_engine, session_id)
    _policies(db_engine, cap=6 if binding == "identity" else 1000, ceiling=6 if binding == "container" else 1000)
    context = replace(
        admission._context(service, db_engine, operation, tmp_path),
        user_message_content="larger",
        max_blob_storage_per_session_bytes=6 if binding == "session" else 1000,
    )
    before = admission._resources(db_engine, tmp_path / "blobs")
    result = admit_blob_mutation("update_blob", {"blob_id": str(record.id), "content": "larger"}, admission._empty_state(), context)
    assert (result is None) is (coverage == "full")
    assert admission._resources(db_engine, tmp_path / "blobs") == before
    with db_engine.connect() as connection:
        assert connection.execute(select(output_storage_reservations_table.c.size_bytes)).scalar_one() == reservation_size
    if binding != "session" and coverage != "full":
        rows = _durable_rows(url)
        assert len(rows) == 1
        assert _metadata(rows[0])["usage"] == (8 if coverage == "unmatched" else 5)
    recorder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_owner", [False, True])
async def test_admission_accounts_for_other_sessions_and_other_identities(db_engine, session_id, compose_context, tmp_path, foreign_owner):
    from uuid import uuid4

    from elspeth.web.sessions.models import sessions_table
    from tests.fixtures.identities import ensure_test_identity

    recorder, url = _durable_recorder(tmp_path)
    authority = SQLiteLocalSessionOperationAuthority(db_engine, quota_exceeded_recorder=recorder.record_quota_exceeded)
    service = BlobServiceImpl(db_engine, tmp_path, session_operation_authority=authority)
    record = await service.create_blob(session_id, "quota.csv", b"old", "text/csv", session_operation_context=compose_context)
    other_session = uuid4()
    owner = "other-owner" if foreign_owner else "test-user"
    with db_engine.begin() as connection:
        ensure_test_identity(connection, identity_id=owner)
        connection.execute(
            sessions_table.insert().values(
                id=str(other_session),
                user_id=owner,
                auth_provider_type="local",
                title="other retained bytes",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    other_operation = seed_live_compose_context(db_engine, other_session)
    await service.create_blob(other_session, "elsewhere.csv", b"old", "text/csv", session_operation_context=other_operation)
    _policies(db_engine, cap=1000 if foreign_owner else 8, ceiling=8 if foreign_owner else 1000)
    context = replace(admission._context(service, db_engine, compose_context, tmp_path), user_message_content="larger")
    before = admission._resources(db_engine, tmp_path / "blobs")
    assert (
        admit_blob_mutation("update_blob", {"blob_id": str(record.id), "content": "larger"}, admission._empty_state(), context) is not None
    )
    assert admission._resources(db_engine, tmp_path / "blobs") == before
    (row,) = _durable_rows(url)
    payload = _metadata(row)
    assert (payload["usage"], payload["scope"], payload["limit"]) == (6, "container" if foreign_owner else "identity", 8)
    recorder.close()

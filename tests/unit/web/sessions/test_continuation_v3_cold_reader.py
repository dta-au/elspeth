"""Cold V3 chunk reads must use the original admission allowance before payload transfer."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ContinuationProviderCostPolicyV2
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.sessions.continuation import initialize_continuation, load_snapshot, put_chunk, read_chunk
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_frames import initial_stack
from tests.unit.web.sessions.test_continuation_schema import continuation_unadmitted_parent, insert_continuation_parent
from tests.unit.web.sessions.test_continuation_v3_central_capacity import _initialize, _limit, _set_v3_contract, _sized_root

pytest_plugins = ("tests.unit.web.sessions.test_continuation_schema",)
continuation_parent = continuation_unadmitted_parent


def _replace_chunk_payload(conn, *, session_id: str, operation_id: str, key: str, payload: bytes) -> None:
    conn.execute(
        update(chunks)
        .where(chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
        .values(payload=payload, byte_length=len(payload), sha256=sha256(payload).hexdigest())
    )


def test_v3_cold_root_projects_payload_conditionally_at_original_allowance(continuation_parent):
    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
        _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)

    statements: list[str] = []

    def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("select") and "composer_continuation_chunks" in statement and "payload" in statement:
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with engine.connect() as conn:
            selected = load_snapshot(conn, session_id=sid, operation_id=operation_id)
        assert selected.payload_json == encoded  # positive control: an exact-fit root is projected
        assert any("case" in statement and "length" in statement for statement in statements), statements
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def test_v3_cold_root_refuses_actual_oversize_before_decode(continuation_parent, monkeypatch):
    import elspeth.web.composer.continuation_codec as codec

    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
        _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
    # The JSON and its embedded hash-bound policy are still semantically valid.
    # Only the actual retained bytes exceed the original allowance.
    encoded_bytes = encoded.encode("utf-8")
    oversized = encoded_bytes + b" " * (_limit(root) + 1 - len(encoded_bytes))
    assert len(oversized) == _limit(root) + 1
    assert decode_continuation_snapshot(oversized.decode("utf-8")) == root
    exact_limit = oversized[:-1]
    with engine.begin() as conn:
        _replace_chunk_payload(conn, session_id=sid, operation_id=operation_id, key="snapshot:initial-checkpoint", payload=exact_limit)
    with engine.connect() as conn:
        assert load_snapshot(conn, session_id=sid, operation_id=operation_id).payload_json == exact_limit.decode("utf-8")
    with engine.begin() as conn:
        _replace_chunk_payload(conn, session_id=sid, operation_id=operation_id, key="snapshot:initial-checkpoint", payload=oversized)

    decoded: list[str] = []
    original = codec.decode_continuation_snapshot

    def observe_decode(payload: str):
        decoded.append(payload)
        return original(payload)

    monkeypatch.setattr(codec, "decode_continuation_snapshot", observe_decode)
    with engine.connect() as conn, pytest.raises(AuditIntegrityError):
        load_snapshot(conn, session_id=sid, operation_id=operation_id)
    assert decoded == [], "an oversized cold root must be refused before JSON decode"


def test_v3_cold_event_chunk_uses_same_original_allowance(continuation_parent):
    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
        _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
        raw = b"x" * (_limit(root) + 1)
        conn.execute(
            insert(chunks).values(
                session_id=sid,
                operation_id=operation_id,
                chunk_key="event:1",
                codec="event.v2",
                payload=raw,
                sha256=sha256(raw).hexdigest(),
                byte_length=len(raw),
            )
        )
    with engine.connect() as conn, pytest.raises(AuditIntegrityError):
        read_chunk(conn, session_id=sid, operation_id=operation_id, key="event:1")


@pytest.mark.parametrize("path", ("collision", "overflow"))
def test_v3_writer_history_reads_conditionally_project_corrupt_oversize(continuation_parent, path):
    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
        _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
        raw = b"x" * (_limit(root) + 1)
        conn.execute(
            insert(chunks).values(
                session_id=sid,
                operation_id=operation_id,
                chunk_key="historical",
                codec="event.v2",
                payload=raw,
                sha256=sha256(raw).hexdigest(),
                byte_length=len(raw),
            )
        )
    statements: list[str] = []

    def names_historical(parameters: object) -> bool:
        if isinstance(parameters, Mapping):
            return "historical" in parameters.values()
        if isinstance(parameters, (tuple, list)):
            return "historical" in parameters
        return False

    def observe(_connection, _cursor, statement, parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("select") and "composer_continuation_chunks" in statement and names_historical(parameters):
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        # Controlled negative: the observer must identify this exact-key raw
        # SELECT as unguarded, then discard it before the production path.
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(chunks.c.payload).where(
                        chunks.c.session_id == sid,
                        chunks.c.operation_id == operation_id,
                        chunks.c.chunk_key == "historical",
                    )
                ).scalar_one()
                == raw
            )
        assert len(statements) == 1 and "case" not in statements[0], statements
        statements.clear()

        with engine.begin() as conn, pytest.raises(AuditIntegrityError):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation_id,
                key="historical" if path == "collision" else "fresh",
                payload=b"{}",
                codec="event.v2",
            )
        assert statements and all("case" in statement and "length" in statement for statement in statements), statements
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def test_v3_parent_contract_refuses_missing_discriminator(continuation_parent):
    engine, sid, operation_id = continuation_parent
    root, _encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id).values(continuation_policy_version=None)
        )


@pytest.mark.parametrize("mutation", ("version", "hash", "allowance", "legacy_downgrade"))
def test_v3_parent_policy_tuple_is_immutable(continuation_parent, mutation):
    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
        _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
        original = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    changes = {
        "version": {"continuation_policy_version": "composer.continuation.policy.v2"},
        "hash": {"continuation_policy_sha256": "f" * 64 if root.policy_hash != "f" * 64 else "e" * 64},
        "allowance": {"continuation_retained_payload_limit_bytes": original.continuation_retained_payload_limit_bytes + 1},
        # This satisfies the legacy CHECK; only immutability should reject it.
        "legacy_downgrade": {
            "continuation_policy_version": "composer.continuation.policy.v2",
            "continuation_policy_sha256": None,
            "continuation_retained_payload_limit_bytes": None,
        },
    }[mutation]
    with pytest.raises(IntegrityError, match="composer operation immutable transition"), engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id).values(**changes))
    with engine.begin() as conn:
        unchanged = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
        assert unchanged == original
        # Positive control: identical policy values and an ordinary monotonic
        # parent update are permitted by the same installed trigger.
        conn.execute(
            update(jobs)
            .where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)
            .values(
                continuation_policy_version=original.continuation_policy_version,
                continuation_policy_sha256=original.continuation_policy_sha256,
                continuation_retained_payload_limit_bytes=original.continuation_retained_payload_limit_bytes,
                status_revision=original.status_revision + 1,
            )
        )
        assert load_snapshot(conn, session_id=sid, operation_id=operation_id).payload_json == encoded


@pytest.mark.parametrize("version", ("composer.continuation.policy.v1", "composer.continuation.policy.v2"))
def test_legacy_parent_policy_version_is_immutable(continuation_parent, version):
    engine, sid, operation_id = continuation_parent
    with engine.begin() as conn:
        insert_continuation_parent(conn, sid, operation_id, policy_version=version)
    other = "composer.continuation.policy.v2" if version.endswith("v1") else "composer.continuation.policy.v1"
    with pytest.raises(IntegrityError, match="composer operation immutable transition"), engine.begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id).values(continuation_policy_version=other)
        )
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(jobs.c.continuation_policy_version).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)
            ).scalar_one()
            == version
        )


def test_v1_cold_root_keeps_legacy_read_contract(continuation_parent):
    engine, sid, operation_id = continuation_parent
    original = initial_stack()
    v2 = continuation_policy_from_settings(continuation_test_settings())
    policy = ContinuationAdmissionPolicy(segment=v2.segment, actions=v2.actions)
    root = original.model_copy(
        update={
            "logical": original.logical.model_copy(update={"session_id": sid, "operation_id": operation_id}),
            "mutation_ceiling": original.mutation_ceiling.model_copy(update={"session_id": sid}),
            "policy_hash": policy.policy_hash,
            "admission_policy_json": policy.model_dump_json(),
        }
    )
    encoded = encode_continuation_snapshot(root)
    with engine.begin() as conn:
        insert_continuation_parent(conn, sid, operation_id)
        initialize_continuation(
            conn,
            session_id=sid,
            operation_id=operation_id,
            checkpoint_id="initial-checkpoint",
            snapshot_json=encoded,
            action_json=policy.actions.session_sql.model_dump_json(),
            policy_sha256=policy.policy_hash,
        )
    with engine.connect() as conn:
        assert load_snapshot(conn, session_id=sid, operation_id=operation_id).payload_json == encoded


def test_v2_cold_root_keeps_legacy_read_contract(continuation_parent):
    engine, sid, operation_id = continuation_parent
    original = initial_stack()
    policy = continuation_policy_from_settings(continuation_test_settings())
    root = original.model_copy(
        update={
            "logical": original.logical.model_copy(update={"session_id": sid, "operation_id": operation_id}),
            "mutation_ceiling": original.mutation_ceiling.model_copy(update={"session_id": sid}),
            "provider_cost_policy": ContinuationProviderCostPolicyV2(
                applicability="continuation_v2",
                lifecycle_version=2,
                accounting_owner="physical_audit_and_token_ledger",
                admission_policy_hash=policy.policy_hash,
            ),
            "policy_hash": policy.policy_hash,
            "admission_policy_json": policy.model_dump_json(),
        }
    )
    encoded = encode_continuation_snapshot(root)
    with engine.begin() as conn:
        insert_continuation_parent(conn, sid, operation_id, policy_version="composer.continuation.policy.v2")
        initialize_continuation(
            conn,
            session_id=sid,
            operation_id=operation_id,
            checkpoint_id="initial-checkpoint",
            snapshot_json=encoded,
            action_json=policy.actions.session_sql.model_dump_json(),
            policy_sha256=policy.policy_hash,
        )
    with engine.connect() as conn:
        assert load_snapshot(conn, session_id=sid, operation_id=operation_id).payload_json == encoded

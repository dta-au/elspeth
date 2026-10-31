"""Original private SQL bytes and independent unchanged continuation policies."""

from hashlib import sha256

import pytest
from sqlalchemy import LargeBinary, text, update
from sqlalchemy.types import TypeDecorator

from elspeth.contracts.bounded_serialization import bounded_canonical_json_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import bounded_json, continuation_codec, continuation_evidence, continuation_limits, continuation_retention
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.sessions.continuation import put_chunk, read_chunk, read_private_chunk
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation

operation_store = _operation_store


def _source(store, payload):
    engine, _repository, _authority, _service, sid = store
    record, _seed, _action = admit_continuation(store)
    reference = PrivateChunkRef(codec="composer.state.v1", digest=sha256(payload).hexdigest(), byte_length=len(payload))
    key = f"value:{reference.codec}:{reference.digest}"
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key=key, payload=payload, codec=reference.codec)
    return engine, str(sid), record.operation_id, reference, key


def test_continuation_policy_owners_are_explicit_and_independent(monkeypatch):
    assert continuation_limits.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES == 1_048_576
    assert continuation_limits.CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES == 1_048_576
    original_input = bounded_json.JSON_MAX_TOTAL_UTF8_BYTES
    original_pair = continuation_evidence.CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
    monkeypatch.setattr(continuation_retention, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", 16)
    assert continuation_retention.preflight_retained_json("é", reason="retained_chunk_bytes") == 4
    with pytest.raises(AuditIntegrityError, match="actual byte limit"):
        continuation_retention.require_retention_capacity(bounded_canonical_json_size("é", limit=15), reason="retained_chunk_bytes")
    with pytest.raises(ComposerContextUnsupported):
        continuation_retention.preflight_retained_json("a" * 15, reason="retained_chunk_bytes")
    assert original_input == bounded_json.JSON_MAX_TOTAL_UTF8_BYTES
    assert original_pair == continuation_evidence.CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
    monkeypatch.setattr(continuation_evidence, "CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES", original_pair - 1)
    assert continuation_codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES == 1_048_576
    assert continuation_retention.preflight_retained_json("a" * 14, reason="retained_chunk_bytes") == 16


@pytest.mark.parametrize("extra", (0, 1))
def test_private_document_codec_exact_boundary_and_one_byte_excess(extra):
    bound = continuation_limits.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    # Genuine canonical UTF8: byte length differs from Python character length.
    canonical = '"' + "é" * ((bound - 2) // 2) + "x" * extra + '"'
    assert len(canonical.encode("utf-8")) == bound + extra
    if extra:
        with pytest.raises(AuditIntegrityError, match="complete JSON admission bound"):
            encode_private_chunk(codec="composer.response.v1", canonical=canonical)
    else:
        assert encode_private_chunk(codec="composer.response.v1", canonical=canonical).canonical is canonical


@pytest.mark.parametrize("size", (4, 1_048_576))
def test_private_sql_supported_actual_bytes(operation_store, size):
    payload = b'"' + "é".encode() * ((size - 2) // 2) + b'"'
    engine, sid, oid, reference, _key = _source(operation_store, payload)
    with engine.connect() as conn:
        actual = read_private_chunk(conn, session_id=sid, operation_id=oid, reference=reference)
    assert actual == payload and type(actual) is bytes


class _ObservedPrivateBytes(TypeDecorator):
    """Observe one real projected driver value; never replace or reread it."""

    impl = LargeBinary
    cache_ok = False

    def __init__(self, observations):
        super().__init__()
        self.observations = observations

    def process_result_value(self, value, dialect):
        self.observations.append((type(value).__name__, len(value) if type(value) is bytes else None))
        return value


def test_private_sql_does_not_return_excess_payload_with_false_small_reference(operation_store, monkeypatch):
    bound = continuation_limits.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    payload = b'"' + b"x" * (bound - 1) + b'"'
    engine, sid, oid, reference, key = _source(operation_store, payload)
    counterfeit = reference.model_copy(update={"byte_length": 2})
    positive_payload = b'{"supported":true}'
    positive = PrivateChunkRef(codec=reference.codec, digest=sha256(positive_payload).hexdigest(), byte_length=len(positive_payload))
    with engine.begin() as conn:
        put_chunk(
            conn,
            session_id=sid,
            operation_id=oid,
            key=f"value:{positive.codec}:{positive.digest}",
            payload=positive_payload,
            codec=positive.codec,
        )
    projected = []
    monkeypatch.setattr(chunks.c.payload, "type", _ObservedPrivateBytes(projected))
    with engine.connect() as conn:
        assert read_private_chunk(conn, session_id=sid, operation_id=oid, reference=positive) == positive_payload
        assert projected == [("bytes", len(positive_payload))]
        projected.clear()
        with pytest.raises(AuditIntegrityError, match=r"private.*(bound|binding)"):
            read_private_chunk(conn, session_id=sid, operation_id=oid, reference=counterfeit)
        assert projected == [("NoneType", None)]
        # Root/snapshot reads deliberately retain their original uncapped API.
        assert read_chunk(conn, session_id=sid, operation_id=oid, key=key) == payload
        # The generic cached whole-table query is outside this CASE observer.


def test_private_sql_suppresses_actual_excess_with_deliberately_corrupt_small_metadata(operation_store, monkeypatch):
    bound = continuation_limits.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    body = '"' + "é" * ((bound - 2) // 2) + 'x"'
    payload = body.encode("utf-8")
    assert len(payload) == bound + 1 and len(body) < bound
    engine, sid, oid, reference, key = _source(operation_store, payload)
    projected = []
    counterfeit = reference.model_copy(update={"byte_length": len(body)})
    monkeypatch.setattr(chunks.c.payload, "type", _ObservedPrivateBytes(projected))
    predicate = (chunks.c.session_id == sid) & (chunks.c.operation_id == oid) & (chunks.c.chunk_key == key)
    with engine.begin() as conn:
        dropped = False
        try:
            if conn.dialect.name == "postgresql":
                assert conn.dialect.driver == "psycopg2"
                # Explicit corruption ONLY in this disposable fixture database;
                # every other constraint remains active. Repaired in finally.
                conn.exec_driver_sql("ALTER TABLE composer_continuation_chunks DROP CONSTRAINT ck_composer_chunks_exact_bytes")
                dropped = True
                conn.execute(update(chunks).where(predicate).values(byte_length=len(body)))
            else:
                assert conn.dialect.name == "sqlite"
                # A corrupt TEXT storage class passes SQLite's existing
                # character-length CHECK. No CHECK or FK is disabled here.
                conn.execute(
                    text(
                        "UPDATE composer_continuation_chunks SET payload=:body, byte_length=:length WHERE session_id=:sid AND operation_id=:oid AND chunk_key=:key"
                    ),
                    {"body": body, "length": len(body), "sid": sid, "oid": oid, "key": key},
                )
            with pytest.raises(AuditIntegrityError, match=r"private.*(bound|binding)"):
                read_private_chunk(conn, session_id=sid, operation_id=oid, reference=counterfeit)
            assert projected == [("NoneType", None)]
        finally:
            conn.execute(update(chunks).where(predicate).values(payload=payload, byte_length=len(payload)))
            if dropped:
                conn.exec_driver_sql(
                    "ALTER TABLE composer_continuation_chunks ADD CONSTRAINT ck_composer_chunks_exact_bytes CHECK (octet_length(payload) = byte_length)"
                )


@pytest.mark.parametrize("consumer", ("family", "application"))
def test_private_sql_consumers_keep_strict_utf8_error_and_original_cause(operation_store, consumer):
    from elspeth.web.composer.continuation_message_families import _actual_chunk
    from elspeth.web.sessions.continuation_p4_application_store import _private_chunk_on_connection
    from tests.unit.web.sessions.test_continuation_cold_restore import start_continuation

    payload = b'"\xff"'
    engine, sid, oid, reference, _key = _source(operation_store, payload)
    running = start_continuation(operation_store[1], operation_store[2])
    assert running.claim.operation_id == oid and str(running.claim.session_id) == sid
    reader = _actual_chunk if consumer == "family" else _private_chunk_on_connection
    try:
        with engine.connect() as conn, pytest.raises(AuditIntegrityError, match="strict UTF-8") as caught:
            reader(conn, running, reference)
        assert type(caught.value.__cause__) is UnicodeDecodeError
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.parametrize("fault", ("codec", "hash", "reference_length", "key"))
def test_private_sql_refuses_one_binding_fault_after_actual_positive(operation_store, fault):
    payload = '{"é":1}'.encode()
    engine, sid, oid, reference, key = _source(operation_store, payload)
    with engine.begin() as conn:
        assert read_private_chunk(conn, session_id=sid, operation_id=oid, reference=reference) == payload
        if fault == "codec":
            conn.execute(
                update(chunks)
                .where(chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == key)
                .values(codec="composer.response.v1")
            )
        elif fault == "hash":
            conn.execute(
                update(chunks)
                .where(chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == key)
                .values(sha256="a" * 64)
            )
        elif fault == "reference_length":
            reference = reference.model_copy(update={"byte_length": reference.byte_length - 1})
        else:
            reference = reference.model_copy(update={"digest": "b" * 64})
        with pytest.raises(AuditIntegrityError, match=r"private.*binding"):
            read_private_chunk(conn, session_id=sid, operation_id=oid, reference=reference)


def test_actual_evidence_uses_pair_policy_independently_of_document_policy(monkeypatch):
    from elspeth.web.composer.continuation_evidence import admit_turn_evidence
    from tests.unit.web.composer.test_continuation_evidence_recorder_reference import _pair_candidate

    value, recorder, original = _pair_candidate()
    positive = admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(recorder,))
    assert positive.source_chunk == original and positive.recorder_chunk is recorder
    bound = continuation_limits.CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
    monkeypatch.setattr(continuation_evidence, "CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES", bound - 1)
    with pytest.raises(AuditIntegrityError, match="pair bound"):
        admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(recorder,))
    monkeypatch.setattr(continuation_evidence, "CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES", bound)
    monkeypatch.setattr(continuation_evidence, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", recorder.reference.byte_length - 1)
    with pytest.raises(AuditIntegrityError, match=r"recorder exceeds.*document bound"):
        admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(recorder,))
    assert bounded_json.JSON_MAX_TOTAL_UTF8_BYTES == 1_048_576


def test_private_driver_observer_preserves_actual_return_without_payload_export():
    observations = []
    observer = _ObservedPrivateBytes(observations)
    original = b"synthetic-observer-positive"
    assert observer.process_result_value(original, None) is original
    assert observations == [("bytes", len(original))]
    assert observer.process_result_value(None, None) is None
    assert observations == [("bytes", len(original)), ("NoneType", None)]

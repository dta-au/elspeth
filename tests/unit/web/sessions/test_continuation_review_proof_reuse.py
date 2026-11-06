"""A verified Review source may be reused only in its original SQL transaction."""

from __future__ import annotations

import json
from hashlib import sha256

import litellm
import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EndFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_end_values import continuation_candidate_id, decode_end_candidate
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_semantic_actions import _require_same_review_transaction, install_semantic_action_on_connection
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture

operation_store = semantic_fixture.operation_store


def test_review_proof_reuse_requires_same_connection_transaction_snapshot_and_effect(tmp_path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'review-proof.db'}")
    source = StoredContinuationSnapshot("{}", 3, "original-checkpoint")
    changed = StoredContinuationSnapshot("{}", 3, "substituted-checkpoint")
    with engine.connect() as conn:
        with conn.begin():
            transaction = conn.get_transaction()
            assert transaction is not None
            _require_same_review_transaction(conn, conn, transaction, source, source, "provider:1:0", "provider:1:0")
            with pytest.raises(AuditIntegrityError, match="original current transaction and source"):
                _require_same_review_transaction(conn, conn, transaction, source, changed, "provider:1:0", "provider:1:0")
            with pytest.raises(AuditIntegrityError, match="original current transaction and source"):
                _require_same_review_transaction(conn, conn, transaction, source, source, "provider:1:0", "provider:1:1")
            with engine.connect() as other, pytest.raises(AuditIntegrityError, match="original current transaction and source"):
                _require_same_review_transaction(other, conn, transaction, source, source, "provider:1:0", "provider:1:0")
        with conn.begin(), pytest.raises(AuditIntegrityError, match="original current transaction and source"):
            _require_same_review_transaction(conn, conn, transaction, source, source, "provider:1:0", "provider:1:0")
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["bridge", "candidate"])
async def test_immediate_review_reuse_rejects_corrupt_new_bridge_or_candidate(
    operation_store, tmp_path, monkeypatch, corruption: str
) -> None:
    async with semantic_fixture._semantic_case(operation_store, tmp_path, monkeypatch, end_candidate=True) as case:
        original = case["runtime"].source.selected.stack
        end = original.frames[-1]
        assert type(end) is EndFrame and end.candidate is not None
        candidate = decode_end_candidate(
            next(chunk for chunk in case["runtime"].source.selected.chunks if chunk.reference == end.candidate)
        )
        answer = semantic_fixture._answer(case["request"])
        prescribed = answer["prescribed_action"]
        prescribed.pop("instruction")
        prescribed.update(
            kind="review",
            candidate_ref=continuation_candidate_id(candidate),
            evidence_binding=case["request"].source.coverage_binding,
        )

        async def completion(**_kwargs):
            return semantic_fixture._reply(json.dumps(answer))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await semantic_fixture._run_paid_semantic(case, monkeypatch)
        sid, operation_id = str(operation_store[4]), case["running"].claim.operation_id
        effect_key = prepared.binding.effect_key
        with locked_session_transaction(operation_store[0], sid) as conn:
            installed = install_semantic_action_on_connection(conn, case["running"], selected=retained.snapshot, effect_key=effect_key)
            original_append = semantic_sql.append_complete_snapshot

            def corrupt_after_append(connection, **kwargs):
                consumed = original_append(connection, **kwargs)
                if corruption == "bridge":
                    key = f"semantic-review-source:{effect_key}"
                else:
                    frame = decode_continuation_snapshot(consumed.payload_json).frames[-1]
                    assert type(frame) is EndFrame and frame.candidate is not None
                    key = f"value:{frame.candidate.codec}:{frame.candidate.digest}"
                changed = b"corrupt-review-source"
                row = connection.execute(
                    update(chunks)
                    .where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
                    .values(payload=changed, byte_length=len(changed), sha256=sha256(changed).hexdigest())
                )
                assert row.rowcount == 1
                return consumed

            with monkeypatch.context() as local_patch:
                local_patch.setattr(semantic_sql, "append_complete_snapshot", corrupt_after_append)
                with pytest.raises(AuditIntegrityError):
                    semantic_sql.consume_semantic_advisor_review_on_connection(
                        conn, case["running"], selected=installed, effect_key=effect_key
                    )
            assert load_snapshot(conn, session_id=sid, operation_id=operation_id) == installed

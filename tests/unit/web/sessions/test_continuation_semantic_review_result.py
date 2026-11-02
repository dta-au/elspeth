"""Cold wire controls for the actual shared END Review result."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.continuation import load_snapshot, put_chunk, read_chunk
from elspeth.web.sessions.continuation_semantic_review_result import (
    RetainedSemanticReviewResult,
    decode_semantic_review_result,
    encode_semantic_review_result,
    read_original_semantic_review_result_on_connection,
    retain_semantic_review_result_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture

operation_store = shared_fixture.operation_store


def _actual_result() -> RetainedSemanticReviewResult:
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    return RetainedSemanticReviewResult(
        result=ComposerResult(
            message="Provider's actual review answer.",
            state=state,
            persisted_assistant_message_id="original-assistant-row",
            persisted_assistant_content="Provider's actual review answer.",
        ),
        publication_binding="a" * 64,
    )


def test_semantic_review_result_complete_cold_roundtrip() -> None:
    original = _actual_result()
    encoded = encode_semantic_review_result(original)
    assert decode_semantic_review_result(encoded) == original
    assert json.loads(encoded)["publication_binding"] == "a" * 64


def test_semantic_review_result_preserves_populated_audit_draft() -> None:
    original = _actual_result()
    populated = RetainedSemanticReviewResult(
        result=replace(
            original.result,
            recovery_audit_drafts=(
                AuditMessageDraft(
                    role="audit",
                    content="Actual private review cohort.",
                    tool_calls=({"type": "review_receipt", "source": {"kind": "provider", "ordinal": 1}},),
                ),
            ),
        ),
        publication_binding=original.publication_binding,
    )
    assert decode_semantic_review_result(encode_semantic_review_result(populated)) == populated


@pytest.mark.parametrize("field", ["state_json", "publication_binding", "unknown_field"])
def test_semantic_review_result_rejects_cold_field_substitution(field: str) -> None:
    encoded = encode_semantic_review_result(_actual_result())
    payload = json.loads(encoded)
    payload[field] = "substituted"
    changed = canonical_json(payload)
    with pytest.raises((AuditIntegrityError, ValueError)):
        decode_semantic_review_result(changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("clean", "blocked"))
async def test_actual_paid_review_result_commits_full_gate_and_cold_refuses_substitution(
    operation_store, tmp_path, monkeypatch, record_property, outcome: str
) -> None:
    async with shared_fixture.actual_shared_end_case(operation_store, tmp_path, monkeypatch, outcome=outcome) as case:
        execution = case["execution"]
        captured = case["captured"]
        assert captured is not None
        actual_wire = encode_semantic_review_result(RetainedSemanticReviewResult(execution.public_result, execution.publication_binding))
        record_property("full_result_utf8_bytes", len(actual_wire.encode("utf-8")))
        record_property("actual_llm_call_count", len(execution.public_result.llm_calls))
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        owner_key = case["owner_effect_key"]
        with locked_session_transaction(operation_store[0], sid) as conn:
            dispatched = load_snapshot(conn, session_id=sid, operation_id=operation)
            retained = retain_semantic_review_result_on_connection(
                conn,
                running,
                selected=dispatched,
                semantic_effect_key=case["semantic_effect_key"],
                execution=execution,
                captured=captured,
            )
            cold = read_original_semantic_review_result_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=operation,
                review_effect_key=owner_key,
            )
            assert cold.completed == retained
            assert cold.result == execution.public_result
            assert cold.outcome == outcome
            assert cold.action_key == case["admission"].action_key
            assert cold.semantic_effect_key == case["semantic_effect_key"]
            assert cold.publication_binding == (execution.publication_binding if outcome == "clean" else None)
            end = decode_continuation_snapshot(retained.payload_json).frames[-1]
            assert end.phase == ("publication_ready" if outcome == "clean" else "repair_needed")
            assert end.publication_binding == cold.publication_binding
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == owner_key)).one()
            assert effect.disposition == "committed" and effect.receipt_key == owner_key and effect.result_chunk_key is not None
            savepoint = conn.begin_nested()
            try:
                altered = json.loads(read_chunk(conn, session_id=sid, operation_id=operation, key=effect.result_chunk_key))
                assert altered["action_key"] != "0" * 64
                altered["action_key"] = "0" * 64
                altered_canonical = canonical_json(altered)
                altered_key = "semantic-review-result:" + sha256(altered_canonical.encode("utf-8")).hexdigest()
                put_chunk(
                    conn,
                    session_id=sid,
                    operation_id=operation,
                    key=altered_key,
                    payload=altered_canonical.encode("utf-8"),
                    codec="composer.semantic-review-result.v1",
                )
                changed = conn.execute(
                    update(effects)
                    .where(effects.c.session_id == sid, effects.c.effect_key == owner_key)
                    .values(result_chunk_key=altered_key)
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError):
                    read_original_semantic_review_result_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=operation,
                        review_effect_key=owner_key,
                    )
            finally:
                savepoint.rollback()

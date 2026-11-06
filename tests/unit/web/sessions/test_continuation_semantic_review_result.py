"""Cold wire controls for the actual shared END Review result."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from pydantic_core import PydanticSerializationError
from sqlalchemy import insert, select, update

from elspeth.contracts.composer_provider_context import PrivateProviderContext
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointCompleteReview
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.composer.continuation_end_values import decode_end_candidate
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.continuation import load_snapshot, put_chunk, read_chunk
from elspeth.web.sessions.continuation_semantic_actions import read_original_semantic_review_for_advisor_on_connection
from elspeth.web.sessions.continuation_semantic_review_result import (
    RetainedSemanticReviewResult,
    decode_semantic_review_result,
    encode_semantic_review_provider_input,
    encode_semantic_review_result,
    locate_original_semantic_review_result_on_connection,
    read_original_semantic_review_result_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.unit.web.composer.test_continuation_physical_outcomes import private_record
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


@pytest.mark.parametrize("field", ("reasoning_details", "thinking_blocks"))
def test_complete_review_serializes_each_frozen_provider_metadata_field(field) -> None:
    call = private_record()
    if field == "reasoning_details":
        assert call.reasoning_details
        call = replace(call, thinking_blocks=None)
    else:
        assert call.thinking_blocks
        call = replace(call, reasoning_details=None)
    original = _actual_result()
    original = replace(original, result=replace(original.result, llm_calls=(call,)))
    encoded = encode_semantic_review_result(original)
    restored = decode_semantic_review_result(encoded)
    assert restored == original
    assert encode_semantic_review_result(restored) == encoded
    (restored_call,) = restored.result.llm_calls
    assert restored_call.private_request == call.private_request
    assert restored_call.private_failure == call.private_failure
    assert restored_call.private_diagnostics == call.private_diagnostics
    assert json.loads(encoded)["llm_calls"][0][field]


@pytest.mark.parametrize("invalid", [object(), float("nan"), float("inf")], ids=["unknown-object", "nan", "infinity"])
def test_complete_review_owned_serializer_refuses_unknown_and_nonfinite_metadata(invalid) -> None:
    call = replace(private_record(), reasoning_details={"invalid": invalid})
    original = _actual_result()
    original = replace(original, result=replace(original.result, llm_calls=(call,)))
    with pytest.raises((AuditIntegrityError, PydanticSerializationError), match=r"owned JSON|finite JSON"):
        encode_semantic_review_result(original)


def _review_with_large_private_request() -> RetainedSemanticReviewResult:
    original = _actual_result()
    request = canonical_json({"messages": [{"role": "user", "content": "Private retained review context café. " * 512}]})
    assert len(request.encode("utf-8")) > 10_000
    call = replace(private_record(), private_request=PrivateProviderContext("retained", request))
    return replace(
        original,
        result=replace(
            original.result,
            llm_calls=(call,),
            raw_assistant_content="The complete original model reply.",
            recovery_audit_drafts=(
                AuditMessageDraft(role="audit", content="Retained review cohort", tool_calls=({"kind": "review", "ordinal": 1},)),
            ),
            repair_turns_used=2,
        ),
    )


def test_semantic_review_provider_data_projects_public_calls_without_changing_stored_result() -> None:
    original = _review_with_large_private_request()
    stored = encode_semantic_review_result(original)
    complete = json.loads(stored)
    cold = decode_semantic_review_result(stored)
    paired_stored, provider_json = encode_semantic_review_provider_input(original)
    assert paired_stored == stored
    projected = json.loads(provider_json)
    assert type(projected) is dict
    assert projected["llm_calls"] == [call.to_dict() for call in cold.result.llm_calls]
    assert set(projected) == set(complete)
    assert {key: value for key, value in projected.items() if key != "llm_calls"} == {
        key: value for key, value in complete.items() if key != "llm_calls"
    }
    (full_call,) = complete["llm_calls"]
    (public_call,) = projected["llm_calls"]
    assert full_call["private_failure"] is not None and full_call["private_diagnostics"]
    assert len(full_call["private_request"]["canonical"].encode("utf-8")) > 10_000
    assert {"private_failure", "private_diagnostics", "private_request"}.isdisjoint(public_call)
    assert len(canonical_json(projected).encode("utf-8")) < len(stored.encode("utf-8")) - 10_000
    assert decode_semantic_review_result(stored) == cold == original
    assert encode_semantic_review_result(cold) == stored
    # The public prompt projection is not a replacement retained result codec.
    with pytest.raises(AuditIntegrityError, match="canonical original bytes"):
        decode_semantic_review_result(canonical_json(projected))


def test_semantic_review_cold_admission_refuses_corrupt_private_audit_before_typed_projection() -> None:
    stored = encode_semantic_review_result(_review_with_large_private_request())
    payload = json.loads(stored)
    assert type(payload["llm_calls"][0]["private_request"]["canonical"]) is str
    payload["llm_calls"][0]["private_request"]["canonical"] = 17
    corrupted = canonical_json(payload)
    with pytest.raises(AuditIntegrityError, match="closed original fields"):
        decode_semantic_review_result(corrupted)
    # Only actual decoded DATA can reach the paired encoder from a cold read.
    original = decode_semantic_review_result(stored)
    paired_stored, provider_json = encode_semantic_review_provider_input(original)
    assert paired_stored == stored and json.loads(provider_json)["llm_calls"]


def test_semantic_review_provider_input_reuses_its_actual_cold_proof_and_rebinds_changed_result(monkeypatch) -> None:
    from elspeth.web.sessions import continuation_semantic_review_result as review_codec

    original_decoder = review_codec.decode_semantic_review_result
    decoded = []

    def observe_decode(encoded):
        result = original_decoder(encoded)
        decoded.append((encoded, result))
        return result

    monkeypatch.setattr(review_codec, "decode_semantic_review_result", observe_decode)
    original = _review_with_large_private_request()
    stored, provider_json = review_codec.encode_semantic_review_provider_input(original)
    assert decoded == [(stored, original)]
    assert json.loads(provider_json)["llm_calls"] == [call.to_dict() for call in original.result.llm_calls]
    # Consumers parse the immutable view without reconstructing the full result.
    from elspeth.web.composer.continuation_semantic_advisor import semantic_retained_json

    assert semantic_retained_json(provider_json) == json.loads(provider_json)
    assert decoded == [(stored, original)]
    changed = replace(original, result=replace(original.result, message="Another actual Review outcome."))
    changed_stored, changed_provider_json = review_codec.encode_semantic_review_provider_input(changed)
    assert decoded[-1] == (changed_stored, changed) and len(decoded) == 2
    assert changed_stored != stored and changed_provider_json != provider_json
    assert json.loads(changed_provider_json)["message"] == changed.result.message
    assert original_decoder(stored) == original


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
        assert type(execution) is EndCheckpointCompleteReview
        review = execution.review
        actual_wire = encode_semantic_review_result(RetainedSemanticReviewResult(review.result, review.publication_binding))
        paired_wire, provider_json = encode_semantic_review_provider_input(
            RetainedSemanticReviewResult(review.result, review.publication_binding)
        )
        assert paired_wire == actual_wire
        projected = json.loads(provider_json)
        assert review.result.llm_calls
        assert type(projected) is dict
        assert projected["llm_calls"] == [call.to_dict() for call in review.result.llm_calls]
        assert {key: value for key, value in projected.items() if key != "llm_calls"} == {
            key: value for key, value in json.loads(actual_wire).items() if key != "llm_calls"
        }
        record_property("full_result_utf8_bytes", len(actual_wire.encode("utf-8")))
        record_property("actual_llm_call_count", len(review.result.llm_calls))
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        owner_key = case["owner_effect_key"]
        with locked_session_transaction(operation_store[0], sid) as conn:
            retained = load_snapshot(conn, session_id=sid, operation_id=operation)
            assert retained == review.completed
            cold = read_original_semantic_review_result_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=operation,
                review_effect_key=owner_key,
            )
            assert cold.completed == retained
            assert cold.result == review.result
            assert cold.outcome == outcome
            assert cold.action_key == case["admission"].action_key
            assert cold.semantic_effect_key == case["semantic_effect_key"]
            assert cold.publication_binding == (review.publication_binding if outcome == "clean" else None)
            end = decode_continuation_snapshot(retained.payload_json).frames[-1]
            assert end.phase == ("publication_ready" if outcome == "clean" else "repair_needed")
            assert end.publication_binding == cold.publication_binding
            candidate_ref = end.candidate
            candidate = decode_end_candidate(
                PrivateChunk(
                    candidate_ref,
                    read_chunk(
                        conn,
                        session_id=sid,
                        operation_id=operation,
                        key=f"value:{candidate_ref.codec}:{candidate_ref.digest}",
                    ).decode("utf-8", errors="strict"),
                )
            )
            located = read_original_semantic_review_for_advisor_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, original_candidate=candidate
            )
            assert located == cold
            assert located.original_source == candidate.semantic_source
            assert located.original_decision_json == candidate.semantic_decision_json
            assert candidate.semantic_source is not None
            foreign_source = candidate.semantic_source.model_copy(update={"effect_key": "foreign-semantic-source"})
            with pytest.raises(AuditIntegrityError):
                read_original_semantic_review_for_advisor_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=operation,
                    original_candidate=candidate.model_copy(update={"semantic_source": foreign_source}),
                )
            assert candidate.response.digest != "0" * 64
            with pytest.raises(AuditIntegrityError):
                locate_original_semantic_review_result_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=operation,
                    semantic_effect_key=case["semantic_effect_key"],
                    response_digest="0" * 64,
                )
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == owner_key)).one()
            assert effect.disposition == "committed" and effect.receipt_key == owner_key and effect.result_chunk_key is not None
            duplicate = conn.begin_nested()
            try:
                duplicate_key = "end:review:" + "f" * 64
                assert duplicate_key != owner_key
                duplicate_commit = json.loads(read_chunk(conn, session_id=sid, operation_id=operation, key=effect.result_chunk_key))
                duplicate_commit["owner_effect_key"] = duplicate_key
                duplicate_canonical = canonical_json(duplicate_commit)
                duplicate_chunk_key = "semantic-review-result:" + sha256(duplicate_canonical.encode("utf-8")).hexdigest()
                put_chunk(
                    conn,
                    session_id=sid,
                    operation_id=operation,
                    key=duplicate_chunk_key,
                    payload=duplicate_canonical.encode("utf-8"),
                    codec="composer.semantic-review-result.v2",
                )
                duplicate_effect = dict(effect._mapping)
                duplicate_effect.update(effect_key=duplicate_key, receipt_key=duplicate_key, result_chunk_key=duplicate_chunk_key)
                conn.execute(insert(effects).values(**duplicate_effect))
                with pytest.raises(AuditIntegrityError):
                    read_original_semantic_review_for_advisor_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, original_candidate=candidate
                    )
            finally:
                duplicate.rollback()
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
                    codec="composer.semantic-review-result.v2",
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
                with pytest.raises(AuditIntegrityError):
                    read_original_semantic_review_for_advisor_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, original_candidate=candidate
                    )
            finally:
                savepoint.rollback()

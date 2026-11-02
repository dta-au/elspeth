"""Neutral result keeps all audit values through one immutable recorder source."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
from elspeth.web.composer.continuation_result_codec import (
    RetainedComposerResult,
    decode_retained_composer_result,
    encode_retained_composer_result,
)
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions.continuation_semantic_review_result import encode_semantic_review_result
from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot
from tests.unit.web.sessions.test_continuation_semantic_review_result import _actual_result


def _result(calls=()) -> RetainedComposerResult:
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    return RetainedComposerResult(
        result=ComposerResult(message="Actual provider result.", state=state, llm_calls=calls),
        publication_binding=None,
    )


def test_neutral_result_selects_exact_ordered_original_recorder_calls_without_embedding_private_requests() -> None:
    snapshot = _snapshot(3)
    recorder = encode_recorder_snapshot(snapshot)
    original = _result((snapshot.llm_calls[0], snapshot.llm_calls[2]))
    encoded = encode_retained_composer_result(original, recorder_source=recorder)
    assert decode_retained_composer_result(encoded, recorder_source=recorder) == original
    wire = json.loads(encoded)
    assert wire["llm_call_indices"] == [0, 2]
    assert wire["tool_invocation_indices"] == []
    assert wire["recorder"] == recorder.reference.model_dump(mode="json")
    assert "private_request" not in encoded
    assert "llm_calls" not in wire and "tool_invocations" not in wire


def test_neutral_result_rejects_foreign_call_and_recorder_source() -> None:
    snapshot = _snapshot(3)
    recorder = encode_recorder_snapshot(snapshot)
    original = _result((snapshot.llm_calls[0], snapshot.llm_calls[2]))
    encoded = encode_retained_composer_result(original, recorder_source=recorder)
    foreign = encode_recorder_snapshot(replace(snapshot, llm_calls=snapshot.llm_calls[:2]))
    with pytest.raises(AuditIntegrityError):
        decode_retained_composer_result(encoded, recorder_source=foreign)
    with pytest.raises(AuditIntegrityError, match="original recorder source"):
        encode_retained_composer_result(_result((replace(snapshot.llm_calls[2], call_id="foreign"),)), recorder_source=recorder)


@pytest.mark.parametrize("indices", ([True], [-1], [2, 1], [0, 0], [3]))
def test_neutral_result_rejects_nonoriginal_call_selection(indices) -> None:
    snapshot = _snapshot(3)
    recorder = encode_recorder_snapshot(snapshot)
    encoded = encode_retained_composer_result(_result((snapshot.llm_calls[0],)), recorder_source=recorder)
    changed = json.loads(encoded)
    changed["llm_call_indices"] = indices
    with pytest.raises((AuditIntegrityError, ValueError)):
        decode_retained_composer_result(canonical_json(changed), recorder_source=recorder)


def test_neutral_result_remains_bounded_when_original_recorder_has_many_private_requests() -> None:
    snapshot = _snapshot(20)
    recorder = encode_recorder_snapshot(snapshot)
    original = _result(snapshot.llm_calls)
    encoded = encode_retained_composer_result(original, recorder_source=recorder)
    assert len(encoded.encode("utf-8")) < len(recorder.canonical.encode("utf-8"))
    assert decode_retained_composer_result(encoded, recorder_source=recorder) == original


def test_legacy_review_v1_stays_separate_from_neutral_result_v1() -> None:
    review = encode_semantic_review_result(_actual_result())
    recorder = encode_recorder_snapshot(_snapshot(0))
    with pytest.raises(AuditIntegrityError):
        decode_retained_composer_result(review, recorder_source=recorder)

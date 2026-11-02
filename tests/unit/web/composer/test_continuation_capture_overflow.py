"""Original representation observations preserve existing refusal contracts."""

from contextlib import contextmanager

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_capture_overflow import ContinuationMandatoryCaptureOverflow
from elspeth.web.composer.continuation_codec import encode_mandatory_private_chunk, encode_private_chunk
from elspeth.web.composer.continuation_evidence import admit_turn_evidence
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_evidence_recorder_reference import _pair_candidate


@contextmanager
def _ordinary_context():
    yield


def test_original_private_text_witness_keeps_exact_unicode_size_and_outer_exception():
    limit = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    fitting = canonical_json("x" * (limit - 2))
    assert encode_private_chunk(codec="composer.messages.v1", canonical=fitting).reference.byte_length == limit
    original = canonical_json("界" * (limit // 3))
    expected = len(original.encode("utf-8"))
    assert expected > limit
    with pytest.raises(AuditIntegrityError) as caught, _ordinary_context():
        encode_private_chunk(codec="composer.messages.v1", canonical=original)
    assert type(caught.value) is AuditIntegrityError
    assert str(caught.value) == "private chunk exceeds the complete JSON admission bound"
    cause = caught.value.__cause__
    assert type(cause) is ContinuationMandatoryCaptureOverflow
    witness = cause.witness
    assert (witness.unit, witness.codec) == ("private_chunk", "composer.messages.v1")
    assert (witness.observed_bytes, witness.limit_bytes, witness.size_is_exact) == (expected, limit, True)
    assert original not in str(cause)
    # Ordinary exception traceback mutation must still work through a context.
    with pytest.raises(ContinuationMandatoryCaptureOverflow) as propagated, _ordinary_context():
        raise cause
    assert propagated.value is cause


def test_original_mandatory_private_preflight_reports_only_a_lower_bound():
    limit = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    assert encode_mandatory_private_chunk(codec="composer.messages.v1", value={"ok": True}).canonical == '{"ok":true}'
    with pytest.raises(ComposerContextUnsupported) as caught:
        encode_mandatory_private_chunk(codec="composer.messages.v1", value={"body": "x" * (limit + 100)})
    ordinary = ComposerContextUnsupported("retained_chunk_bytes")
    assert type(caught.value) is type(ordinary)
    assert caught.value.reason == ordinary.reason
    assert str(caught.value) == str(ordinary)
    assert caught.value.to_payload() == ordinary.to_payload()
    cause = caught.value.__cause__
    assert type(cause) is ContinuationMandatoryCaptureOverflow
    assert (cause.witness.observed_bytes, cause.witness.limit_bytes, cause.witness.size_is_exact) == (limit + 1, limit, False)
    assert (cause.witness.unit, cause.witness.codec) == ("private_chunk", "composer.messages.v1")


def test_original_evidence_pair_witness_names_exact_pair_and_preserves_public_refusal():
    fitting, recorder, _ = _pair_candidate()
    admit_turn_evidence(fitting, env_ref_names=frozenset(), source_chunks=(recorder,), mandatory_retention=True)
    original, recorder, evidence = _pair_candidate(over=1)
    expected = len(evidence.canonical.encode("utf-8")) + len(recorder.canonical.encode("utf-8"))
    with pytest.raises(ComposerContextUnsupported) as caught:
        admit_turn_evidence(original, env_ref_names=frozenset(), source_chunks=(recorder,), mandatory_retention=True)
    ordinary = ComposerContextUnsupported("retained_chunk_bytes")
    assert type(caught.value) is type(ordinary)
    assert caught.value.reason == ordinary.reason
    assert str(caught.value) == str(ordinary)
    assert caught.value.to_payload() == ordinary.to_payload()
    cause = caught.value.__cause__
    assert type(cause) is ContinuationMandatoryCaptureOverflow
    assert (cause.witness.unit, cause.witness.codec) == ("evidence_pair", "composer.evidence.v2")
    assert (cause.witness.observed_bytes, cause.witness.limit_bytes, cause.witness.size_is_exact) == (
        expected,
        CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
        True,
    )
    with pytest.raises(AuditIntegrityError, match="pair bound") as stored:
        admit_turn_evidence(original, env_ref_names=frozenset(), source_chunks=(recorder,))
    assert stored.value.__cause__ is None


def test_invalid_original_values_and_credentials_do_not_acquire_overflow_witnesses():
    with pytest.raises(AuditIntegrityError) as malformed:
        encode_private_chunk(codec="composer.messages.v1", canonical="not json")
    assert malformed.value.__cause__ is None
    value, recorder, _ = _pair_candidate(over=1, credential=True)
    with pytest.raises(CredentialMaterialRefused) as credentials:
        admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(recorder,), mandatory_retention=True)
    assert type(credentials.value.__cause__) is not ContinuationMandatoryCaptureOverflow


def test_saturated_private_measurement_still_validates_the_complete_original_tail():
    with pytest.raises(ValueError, match="NaN") as malformed:
        encode_mandatory_private_chunk(
            codec="composer.messages.v1",
            value={"prefix": "x" * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 100), "tail": float("nan")},
        )
    assert type(malformed.value.__cause__) is not ContinuationMandatoryCaptureOverflow

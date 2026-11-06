"""Original size evidence alone never grants checkpoint paid authority."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_capture_overflow import ContinuationMandatoryCaptureOverflow
from elspeth.web.composer.continuation_checkpoint_projection import (
    ProjectedEarlyCheckpointOverflow,
    _require_capture_witness,
    require_original_early_checkpoint_overflow,
)
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES


@pytest.fixture
def original_witness():
    canonical = '{"original":"' + "x" * CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + '"}'
    with pytest.raises(AuditIntegrityError) as raised:
        encode_private_chunk(codec="composer.audit-buffer.v2", canonical=canonical)
    cause = raised.value.__cause__
    assert type(cause) is ContinuationMandatoryCaptureOverflow
    witness = cause.witness
    assert witness.observed_bytes == len(canonical.encode("utf-8"))
    assert witness.size_is_exact
    _require_capture_witness(witness)
    return witness


@pytest.mark.parametrize(
    "change",
    (
        {"codec": "composer.response.v1"},
        {"unit": "evidence_pair"},
        {"limit_bytes": CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1},
        {"observed_bytes": CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES},
        {"observed_bytes": True},
        {"size_is_exact": False},
    ),
)
def test_capture_witness_checks_original_unit_and_exact_size_semantics(original_witness, change):
    with pytest.raises(AuditIntegrityError):
        _require_capture_witness(replace(original_witness, **change))


def test_genuine_encoder_witness_is_not_original_paid_capture_authority(original_witness):
    forged = ProjectedEarlyCheckpointOverflow(original_witness)
    with pytest.raises(AuditIntegrityError, match="original returned-call capture boundary"):
        require_original_early_checkpoint_overflow(forged, prepared=None, admission=None, returned=None)

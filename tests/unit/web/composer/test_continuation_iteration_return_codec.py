"""A classifier return keeps its full result and original terminal DATA."""

import json
from dataclasses import replace
from typing import Literal

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
from elspeth.web.composer.continuation_iteration_return_codec import (
    IterationReturnEnvelope,
    decode_iteration_return_envelope,
    encode_iteration_return_envelope,
)
from elspeth.web.composer.continuation_result_codec import RetainedComposerResult, encode_retained_composer_result
from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot
from tests.unit.web.composer.test_continuation_result_codec import _result


def _returned(*, calls: int = 2, source_kind: Literal["classifier_primary", "source_proof_stop"] = "classifier_primary"):
    snapshot = _snapshot(calls)
    recorder = encode_recorder_snapshot(snapshot)
    retained = _result(snapshot.llm_calls)
    primary = source_kind == "classifier_primary"
    if primary:
        retained = RetainedComposerResult(retained.result, "c" * 64)
    result_json = encode_retained_composer_result(retained, recorder_source=recorder)
    envelope = IterationReturnEnvelope(
        retained=retained,
        result_json=result_json,
        source_kind=source_kind,
        terminal_kind="model_end" if primary else "owned_refusal",
        end_disposition="clean" if primary else "blocked",
        primary_effect_key="provider:0:2" if primary else None,
        primary_input_sha256="a" * 64 if primary else None,
        primary_response=PrivateChunkRef(codec="composer.response.v1", digest="b" * 64, byte_length=113) if primary else None,
        end_effect_key="end:original" if primary else None,
        pending_stage_key="original-pending-end" if primary else None,
        end_pass_message_id="original-model-pass" if primary else None,
        end_pass_index=1 if primary else None,
    )
    return envelope, recorder


def test_return_envelope_keeps_complete_original_result_and_primary_end_locators() -> None:
    value, recorder = _returned(calls=3)
    encoded = encode_iteration_return_envelope(value, recorder_source=recorder)
    assert decode_iteration_return_envelope(encoded, recorder_source=recorder) == value
    wire = json.loads(encoded)
    assert wire["source_kind"] == "classifier_primary"
    assert wire["primary_effect_key"] == "provider:0:2"
    assert wire["end_pass_message_id"] == "original-model-pass"
    assert "private_request" not in encoded


def test_source_stop_return_retains_owned_refusal_without_invented_primary() -> None:
    value, recorder = _returned(source_kind="source_proof_stop")
    encoded = encode_iteration_return_envelope(value, recorder_source=recorder)
    assert decode_iteration_return_envelope(encoded, recorder_source=recorder) == value
    wire = json.loads(encoded)
    assert wire["primary_effect_key"] is None
    assert wire["primary_response"] is None
    assert wire["end_pass_message_id"] is None


@pytest.mark.parametrize(
    "changed",
    (
        {"source_kind": "source_proof_stop"},
        {"primary_effect_key": None},
        {"end_pass_message_id": None},
        {"pending_stage_key": None},
        {"terminal_kind": "owned_refusal"},
    ),
)
def test_return_envelope_refuses_partial_or_reclassified_clean_stamp(changed) -> None:
    value, recorder = _returned()
    wire = json.loads(encode_iteration_return_envelope(value, recorder_source=recorder))
    wire.update(changed)
    with pytest.raises(AuditIntegrityError, match="closed original terminal source"):
        decode_iteration_return_envelope(canonical_json(wire), recorder_source=recorder)


def test_return_envelope_refuses_substituted_complete_result_and_foreign_recorder() -> None:
    value, recorder = _returned()
    with pytest.raises(AuditIntegrityError, match="original complete result bytes"):
        encode_iteration_return_envelope(replace(value, result_json="{}"), recorder_source=recorder)
    encoded = encode_iteration_return_envelope(value, recorder_source=recorder)
    foreign = encode_recorder_snapshot(_snapshot(1))
    with pytest.raises(AuditIntegrityError):
        decode_iteration_return_envelope(encoded, recorder_source=foreign)
    with pytest.raises(AuditIntegrityError):
        decode_iteration_return_envelope(value.result_json, recorder_source=recorder)
    unbound = _result(value.retained.result.llm_calls)
    unbound_json = encode_retained_composer_result(unbound, recorder_source=recorder)
    with pytest.raises(AuditIntegrityError, match="model publication binding"):
        encode_iteration_return_envelope(replace(value, retained=unbound, result_json=unbound_json), recorder_source=recorder)

"""Size observations for the actual complete provider retention projections."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import (
    encode_continuation_response,
    encode_private_chunk,
    measure_continuation_response_size,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_provider_outcomes import encode_physical_audit_call, measure_physical_audit_call_size
from elspeth.web.composer.continuation_title_values import (
    TitleProviderResultSnapshot,
    capture_title_provider_result,
    measure_title_provider_result_size,
)
from tests.unit.web.composer.test_continuation_title_values import title_result_inputs

_LIMIT = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES


def test_complete_measures_match_all_three_normal_success_encodings() -> None:
    values = title_result_inputs()
    captured = capture_title_provider_result(**values)

    assert measure_physical_audit_call_size(values["audit_call"]).admitted_bytes == len(
        encode_physical_audit_call(values["audit_call"]).encode("utf-8")
    )
    assert measure_continuation_response_size(values["completion"], audit_call=values["audit_call"]).admitted_bytes == len(
        captured.snapshot.response_canonical.encode("utf-8")
    )
    assert measure_title_provider_result_size(captured.snapshot).admitted_bytes == captured.chunk.reference.byte_length


def test_physical_audit_excess_is_positive_and_not_inferred_from_a_small_field() -> None:
    values = title_result_inputs()
    call = replace(values["audit_call"], reasoning_content="é" * 600_000)

    measured = measure_physical_audit_call_size(call)
    assert measured.limit == _LIMIT
    assert measured.admitted_bytes is None
    assert len(encode_physical_audit_call(call).encode("utf-8")) > _LIMIT


def test_response_one_byte_excess_is_counted_after_complete_projection() -> None:
    values = title_result_inputs()
    completion = values["completion"]
    empty = replace(completion, message=replace(completion.message, content=""))
    empty_size = measure_continuation_response_size(empty).admitted_bytes
    assert empty_size is not None
    assert empty_size == len(encode_continuation_response(empty).canonical.encode("utf-8"))
    one_over = replace(completion, message=replace(completion.message, content="x" * (_LIMIT - empty_size + 1)))

    measured = measure_continuation_response_size(one_over)
    assert measured.admitted_bytes is None
    with pytest.raises(AuditIntegrityError, match="private chunk exceeds the complete JSON admission bound"):
        encode_continuation_response(one_over)


def test_title_outer_excess_counts_second_level_escaping_when_inner_fits() -> None:
    values = title_result_inputs()
    captured = capture_title_provider_result(**values)
    completion = values["completion"]
    escaped = replace(completion, message=replace(completion.message, content="\\" * 350_000))
    response_measure = measure_continuation_response_size(escaped, audit_call=values["audit_call"])
    assert response_measure.admitted_bytes is not None and response_measure.admitted_bytes < _LIMIT
    response = encode_continuation_response(escaped, audit_call=values["audit_call"])
    snapshot = TitleProviderResultSnapshot(
        logical=captured.snapshot.logical,
        actor_user_id=captured.snapshot.actor_user_id,
        effect_key=captured.snapshot.effect_key,
        input_sha256=captured.snapshot.input_sha256,
        creator_sequence=captured.snapshot.creator_sequence,
        creator_generation=captured.snapshot.creator_generation,
        source=captured.snapshot.source,
        response_reference=response.reference,
        response_canonical=response.canonical,
        audit_drafts_json=captured.snapshot.audit_drafts_json,
    )

    outer_measure = measure_title_provider_result_size(snapshot)
    assert outer_measure.admitted_bytes is None
    assert len(canonical_json(snapshot.model_dump(mode="json")).encode("utf-8")) > _LIMIT
    with pytest.raises(AuditIntegrityError, match="private chunk exceeds the complete JSON admission bound"):
        encode_private_chunk(codec="composer.title-provider-result.v1", canonical=canonical_json(snapshot.model_dump(mode="json")))


def test_measures_reject_non_nominal_inputs_instead_of_classifying_them_as_size() -> None:
    with pytest.raises(TypeError, match="nominal record"):
        measure_physical_audit_call_size(object())
    with pytest.raises(TypeError, match="admitted completion"):
        measure_continuation_response_size(object())
    with pytest.raises(TypeError, match="nominal snapshot"):
        measure_title_provider_result_size(object())

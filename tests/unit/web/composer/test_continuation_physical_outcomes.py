import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from elspeth.contracts.composer_llm_audit import PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_codec import (
    decode_retained_physical_audit_call,
    encode_continuation_response,
)
from elspeth.web.composer.continuation_provider_outcomes import (
    decode_physical_audit_call,
    encode_physical_audit_call,
    require_successful_response_audit,
)
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from tests.unit.web.composer.test_continuation_response import completion


def actual_record():
    received = completion()
    call = build_llm_call_record(
        model_requested="actual-test-model",
        messages=[{"role": "user", "content": "Keep the complete native and usage tail café"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns() - 17_000_000,
        temperature=0.0,
        seed=7,
        response_metadata=received.provider_metadata,
    )
    return replace(call, call_id=str(UUID(int=41))), received.provider_metadata


def test_complete_actual_record_survives_json_without_rebuilding_clocks():
    call, metadata = actual_record()
    canonical = encode_physical_audit_call(call)
    restored = decode_physical_audit_call(canonical)
    assert encode_physical_audit_call(restored) == canonical
    assert restored.latency_ms == call.latency_ms >= 17
    assert restored.started_at == call.started_at and restored.finished_at == call.finished_at
    assert restored.messages_hash == call.messages_hash and restored.call_id == call.call_id
    assert restored.temperature == 0.0 and restored.seed == 7
    require_successful_response_audit(restored, metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at)


def private_record():
    from elspeth.contracts.composer_audit import PrivateFailureFacts, PrivateReceivedDiagnostic
    from elspeth.contracts.composer_provider_context import PrivateProviderContext

    call, _metadata = actual_record()
    return replace(
        call,
        status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
        error_class="MalformedProviderResponse",
        error_message="Retained malformed café result",
        private_failure=PrivateFailureFacts(canonical_json({"stage": "response_admission", "detail": "Complete café tail"})),
        private_diagnostics=(
            PrivateReceivedDiagnostic(
                "actual-evidence",
                "actual-call",
                "set_metadata",
                canonical_json({"patch": {"description": "Exact café"}}),
                canonical_json({"refused": True}),
                '{"patch":{"description":"Exact café"}}',
            ),
        ),
        private_request=PrivateProviderContext(
            "retained", canonical_json({"messages": [{"role": "user", "content": "Exact retained café input"}]})
        ),
    )


def test_complete_private_failure_diagnostic_and_request_fields_survive():
    call = private_record()
    restored = decode_physical_audit_call(encode_physical_audit_call(call))
    assert restored == call
    assert restored.private_failure == call.private_failure
    assert restored.private_diagnostics == call.private_diagnostics
    assert restored.private_request == call.private_request


@pytest.mark.parametrize("field", ("private_failure", "private_diagnostics", "private_request"))
def test_omitted_private_audit_channel_cannot_default_to_an_empty_record(field):
    value = json.loads(encode_physical_audit_call(private_record()))
    del value[field]
    with pytest.raises(AuditIntegrityError, match=r"omitted|owned complete contract"):
        decode_physical_audit_call(canonical_json(value))


@pytest.mark.parametrize(
    "field,value",
    (
        ("call_id", str(UUID(int=42))),
        ("model_returned", "another-model"),
        ("provider_request_id", "another-provider-request"),
        ("finish_reason", "another-finish"),
        ("prompt_tokens", 991),
        ("completion_tokens", 992),
        ("total_tokens", 1983),
        ("cached_prompt_tokens", 993),
        ("reasoning_tokens", 994),
        ("reasoning_content", "Changed full native reasoning tail"),
    ),
)
def test_success_audit_must_match_its_actual_physical_response(field, value):
    call, metadata = actual_record()
    altered = replace(call, **{field: value})
    with pytest.raises(AuditIntegrityError, match="actual physical"):
        require_successful_response_audit(
            altered, metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
        )


def test_finish_and_status_are_actual_received_evidence():
    call, metadata = actual_record()
    for altered in (
        replace(call, finished_at=call.finished_at + timedelta(seconds=1)),
        replace(call, status=ComposerLLMCallStatus.TIMEOUT, error_class="TimeoutError", error_message="timed out"),
    ):
        with pytest.raises(AuditIntegrityError, match="actual physical"):
            require_successful_response_audit(
                altered, metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
            )


def test_codec_refuses_omitted_unknown_or_noncanonical_fields():
    call, _metadata = actual_record()
    canonical = encode_physical_audit_call(call)
    source = json.loads(canonical)
    omitted = {key: value for key, value in source.items() if key != "latency_ms"}
    for changed in (canonical_json(omitted), canonical_json(source | {"invented": True}), json.dumps(source, indent=2)):
        with pytest.raises(AuditIntegrityError):
            decode_physical_audit_call(changed)


def test_restored_historical_call_is_once_only_and_conflicting_bytes_refuse():
    call, _metadata = actual_record()
    recorder = BufferingRecorder()
    recorder.restore_received_llm_call(call)
    recorder.restore_received_llm_call(decode_physical_audit_call(encode_physical_audit_call(call)))
    assert recorder.llm_calls == (call,)
    with pytest.raises(AuditIntegrityError, match="different complete"):
        recorder.restore_received_llm_call(replace(call, latency_ms=call.latency_ms + 1))
    assert recorder.llm_calls == (call,)


def test_complete_current_cost_and_allowance_fields_survive_without_renewal():
    call, _metadata = actual_record()
    measured = replace(
        call,
        pricing_model="catalog/original-model",
        provider_cost=0.004,
        provider_cost_source=PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST,
        max_completion_tokens_requested=512,
        planner_policy_hash="b" * 64,
        planner_call_ordinal=3,
    )
    cold = decode_physical_audit_call(encode_physical_audit_call(measured))
    assert cold == measured
    assert cold.pricing_model == "catalog/original-model"
    assert cold.provider_cost == 0.004
    assert cold.provider_cost_source == PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST
    assert cold.max_completion_tokens_requested == 512
    assert cold.planner_policy_hash == "b" * 64
    assert cold.planner_call_ordinal == 3


def test_response_requires_complete_audit_only_when_physical_audit_is_requested():
    call, _metadata = actual_record()
    received = completion()
    pure = encode_continuation_response(received)
    with pytest.raises(AuditIntegrityError, match="actual complete physical audit"):
        decode_retained_physical_audit_call(pure)
    retained = encode_continuation_response(received, audit_call=call)
    assert decode_retained_physical_audit_call(retained) == call

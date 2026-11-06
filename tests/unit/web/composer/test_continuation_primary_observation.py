"""Source controls for the dormant PRIMARY paid-result observation."""

import sys
from dataclasses import replace

import pytest

from elspeth.contracts.bounded_serialization import bounded_canonical_json_size, utf8_size
from elspeth.contracts.composer_audit import PrivateReceivedDiagnostic
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot, encode_recorder_snapshot, recorder_snapshot_json
from elspeth.web.composer.continuation_codec import encode_continuation_response, measure_continuation_response_size
from elspeth.web.composer.continuation_evidence import decode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)
from elspeth.web.composer.continuation_primary_observation import (
    PrimaryCompleteObservation,
    PrimaryOverflowObservation,
    observe_primary_provider_result,
    observe_v4_primary_provider_result,
)
from elspeth.web.composer.continuation_provider_outcomes import measure_physical_audit_call_size
from elspeth.web.composer.continuation_retention import retained_model_json_view
from elspeth.web.composer.provider_gateway import _require_no_credential_material_in_completion
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness, V4ProviderSdkReturn
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence, snapshot
from tests.unit.web.composer.test_continuation_evidence_recorder_reference import _pair_candidate
from tests.unit.web.composer.test_continuation_title_values import title_result_inputs
from tests.unit.web.composer.test_turn_effects import CONTEXT


def _case() -> dict:
    owner, messages, live_recorder, logical, _ = rich_evidence()
    previous_evidence = snapshot(owner, messages, live_recorder, logical)
    values = title_result_inputs()
    call = values["audit_call"]
    prior = decode_recorder_snapshot(previous_evidence.recorder_chunk)
    return {
        "outcome": _CallModelOutcome(values["completion"], call),
        "recorder": replace(prior, llm_calls=(*prior.llm_calls, call)),
        "previous_evidence": previous_evidence,
        "context": CONTEXT,
        "env_ref_names": frozenset(),
    }


def _with_call(case: dict, call) -> None:
    completion = case["outcome"].completion
    case["outcome"] = _CallModelOutcome(completion, call)
    prior = decode_recorder_snapshot(case["previous_evidence"].recorder_chunk)
    case["recorder"] = replace(prior, llm_calls=(*prior.llm_calls, call))


def _response_excess_case() -> dict:
    case = _case()
    completion = case["outcome"].completion
    metadata = replace(completion.provider_metadata, reasoning_content="x" * 230_000, reasoning_details=[{"text": "y" * 230_000}])
    call = case["outcome"].audit_call
    assert call is not None
    _with_call(case, replace(call, reasoning_content=metadata.reasoning_content, reasoning_details=metadata.reasoning_details))
    case["outcome"] = _CallModelOutcome(
        replace(completion, message=replace(completion.message, content="z" * 230_000), provider_metadata=metadata),
        case["outcome"].audit_call,
    )
    assert measure_physical_audit_call_size(case["outcome"].audit_call).admitted_bytes is not None
    assert measure_continuation_response_size(case["outcome"].completion, audit_call=case["outcome"].audit_call).exceeded
    return case


def test_complete_primary_observation_preserves_all_normal_chunks() -> None:
    case = _case()
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryCompleteObservation)
    assert observed.outcome is case["outcome"]
    assert observed.response == encode_continuation_response(case["outcome"].completion, audit_call=case["outcome"].audit_call)
    expected = append_primary_call_to_evidence(
        case["previous_evidence"],
        call=case["outcome"].audit_call,
        recorder=case["recorder"],
        env_ref_names=frozenset(),
    )
    assert observed.recorder == expected.recorder_chunk
    assert observed.evidence == expected.source_chunk


@pytest.mark.parametrize("excess", (False, True))
def test_v4_live_observer_refuses_unissued_completion_before_size_classification(excess: bool) -> None:
    case = _response_excess_case() if excess else _case()
    with pytest.raises(AuditIntegrityError, match="original complete SDK return"):
        observe_v4_primary_provider_result(**case)


def test_v4_live_observer_refuses_forged_equal_metadata_witness() -> None:
    case = _case()
    outcome = case["outcome"]
    call = outcome.audit_call
    assert call is not None and call.call_id is not None
    metadata = outcome.completion.provider_metadata
    witness = V4ProviderResultCompleteness(
        V4ProviderSdkReturn(call.call_id, 1), metadata, outcome.completion.message.content, outcome.completion.tool_batch.calls
    )
    case["outcome"] = _CallModelOutcome(replace(outcome.completion, provider_metadata=replace(metadata, v4_completeness=witness)), call)
    with pytest.raises(AuditIntegrityError, match="original issued metadata capture"):
        observe_v4_primary_provider_result(**case)


def test_v4_live_observer_refuses_witness_for_another_physical_attempt() -> None:
    case = _case()
    outcome = case["outcome"]
    call = outcome.audit_call
    assert call is not None and call.call_id is not None
    metadata = outcome.completion.provider_metadata
    witness = V4ProviderResultCompleteness(
        V4ProviderSdkReturn("another-physical-attempt", 1),
        metadata,
        outcome.completion.message.content,
        outcome.completion.tool_batch.calls,
    )
    case["outcome"] = _CallModelOutcome(replace(outcome.completion, provider_metadata=replace(metadata, v4_completeness=witness)), call)
    with pytest.raises(AuditIntegrityError, match="original complete SDK return"):
        observe_v4_primary_provider_result(**case)


def test_response_excess_after_fitting_physical_audit() -> None:
    case = _response_excess_case()
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryOverflowObservation)
    assert (observed.representation, observed.document_codec) == ("response", "composer.response.v1")
    assert observed.observed_bytes == CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1


@pytest.mark.parametrize(
    ("returned", "requested", "expected_status", "expected_model"),
    (
        ("returned-model", "requested-model", "observed", "returned-model"),
        (None, "requested-model", "observed", "requested-model"),
        ("r" * 256, "requested-model", "observed", "r" * 256),
        ("r" * 257, "requested-model", "unavailable", None),
    ),
)
def test_response_excess_preserves_actual_model_or_declines_ledger_projection(
    returned: str | None, requested: str, expected_status: str, expected_model: str | None
) -> None:
    case = _response_excess_case()
    completion = case["outcome"].completion
    metadata = replace(completion.provider_metadata, model_returned=returned)
    call = case["outcome"].audit_call
    assert call is not None
    _with_call(case, replace(call, model_returned=returned, model_requested=requested))
    case["outcome"] = _CallModelOutcome(replace(completion, provider_metadata=metadata), case["outcome"].audit_call)
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryOverflowObservation)
    assert observed.representation == "response"
    assert (observed.usage.status, observed.usage.model) == (expected_status, expected_model)


@pytest.mark.parametrize(
    ("prompt_tokens", "expected_status"),
    (
        (None, "observed"),
        (0, "observed"),
        (2**31 - 1, "observed"),
        (2**31, "unavailable"),
    ),
)
def test_response_excess_preserves_unknown_zero_and_token_bounds(prompt_tokens: int | None, expected_status: str) -> None:
    case = _response_excess_case()
    completion = case["outcome"].completion
    usage = replace(
        completion.provider_metadata.usage,
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        reported_total=None,
        cached_prompt_tokens=0 if prompt_tokens is not None else None,
        reasoning_tokens=0 if prompt_tokens == 0 else None,
    )
    metadata = replace(completion.provider_metadata, usage=usage)
    call = case["outcome"].audit_call
    assert call is not None
    _with_call(
        case,
        replace(
            call,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        ),
    )
    case["outcome"] = _CallModelOutcome(replace(completion, provider_metadata=metadata), case["outcome"].audit_call)
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryOverflowObservation)
    assert observed.representation == "response"
    assert observed.usage.status == expected_status
    if expected_status == "observed":
        assert (observed.usage.prompt_tokens, observed.usage.completion_tokens) == (prompt_tokens, 0)
        assert observed.usage.cached_prompt_tokens == (0 if prompt_tokens is not None else None)
        assert observed.usage.reasoning_tokens == (0 if prompt_tokens == 0 else None)
    else:
        assert (observed.usage.model, observed.usage.prompt_tokens, observed.usage.cached_prompt_tokens) == (None, None, None)


@pytest.mark.parametrize(
    ("amount", "expected"),
    (
        (float.fromhex("0x0.0000000000001p-1022"), "5e-324"),
        (sys.float_info.max, "1.7976931348623157e308"),
    ),
)
def test_response_excess_retains_finite_binary64_cost_extremes(amount: float, expected: str) -> None:
    case = _response_excess_case()
    completion = case["outcome"].completion
    metadata = replace(completion.provider_metadata, provider_cost=amount, provider_cost_source="response_usage.cost")
    call = case["outcome"].audit_call
    assert call is not None
    _with_call(case, replace(call, provider_cost=amount, provider_cost_source="response_usage.cost"))
    case["outcome"] = _CallModelOutcome(replace(completion, provider_metadata=metadata), case["outcome"].audit_call)
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryOverflowObservation)
    assert observed.representation == "response"
    assert (observed.cost.status, observed.cost.amount, observed.cost.source) == ("known", expected, "response_usage.cost")


def test_recorder_excess_after_fitting_physical_audit_and_response() -> None:
    case = _case()
    _snapshot, audit, source = _pair_candidate()
    previous = decode_turn_evidence(source, env_ref_names=frozenset(), source_chunks=(audit,))
    case["previous_evidence"] = previous
    call = case["outcome"].audit_call
    assert call is not None
    diagnostic = canonical_json({"payload": "x" * 220_000})
    receipts = tuple(
        PrivateReceivedDiagnostic(
            f"recorder-growth-{index}", f"recorder-call-{index}", "list_sources", diagnostic, '{"status":"received"}', None
        )
        for index in range(4)
    )
    call = replace(call, private_diagnostics=(*call.private_diagnostics, *receipts))
    _with_call(case, call)
    assert measure_physical_audit_call_size(call).admitted_bytes is not None
    assert measure_continuation_response_size(case["outcome"].completion, audit_call=call).admitted_bytes is not None
    assert utf8_size(recorder_snapshot_json(case["recorder"]), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) > (
        CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    )
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryOverflowObservation)
    assert (observed.representation, observed.document_codec) == ("recorder", "composer.audit-buffer.v2")
    assert observed.observed_bytes == CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1


@pytest.mark.parametrize(
    ("amount", "source", "expected_status", "expected_amount"),
    (
        (None, "not_available", "unknown", None),
        (0.0, "response_usage.cost", "known", "0"),
        (0.25, "response_usage.cost", "known", "0.25"),
    ),
)
def test_valid_paid_physical_audit_excess_has_honest_cost(
    amount: float | None, source: str, expected_status: str, expected_amount: str | None
) -> None:
    case = _case()
    call = case["outcome"].audit_call
    assert call is not None
    diagnostic = canonical_json({"payload": "x" * 220_000})
    receipts = tuple(
        PrivateReceivedDiagnostic(f"diagnostic-{index}", f"call-{index}", "list_sources", diagnostic, '{"status":"received"}', None)
        for index in range(5)
    )
    metadata = replace(case["outcome"].completion.provider_metadata, provider_cost=amount, provider_cost_source=source)
    completion = replace(case["outcome"].completion, provider_metadata=metadata)
    call = replace(call, private_diagnostics=receipts, provider_cost=amount, provider_cost_source=source)
    case["outcome"] = _CallModelOutcome(completion, call)
    prior = decode_recorder_snapshot(case["previous_evidence"].recorder_chunk)
    case["recorder"] = replace(prior, llm_calls=(*prior.llm_calls, call))
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryOverflowObservation)
    assert (observed.representation, observed.document_codec) == ("physical_audit", "composer.physical-audit.v1")
    assert (observed.measurement, observed.observed_bytes, observed.limit_bytes) == (
        "at_least",
        CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1,
        CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    assert (observed.cost.status, observed.cost.amount, observed.cost.source) == (expected_status, expected_amount, source)
    assert diagnostic not in repr(observed)


def test_invalid_response_shape_wins_over_large_valid_audit() -> None:
    case = _case()
    call = case["outcome"].audit_call
    assert call is not None
    diagnostic = canonical_json({"payload": "x" * 220_000})
    receipts = tuple(
        PrivateReceivedDiagnostic(f"diagnostic-{index}", f"call-{index}", "list_sources", diagnostic, '{"status":"received"}', None)
        for index in range(5)
    )
    _with_call(case, replace(call, private_diagnostics=receipts))
    completion = case["outcome"].completion
    bad = replace(completion, tool_batch=replace(completion.tool_batch, call_ids=frozenset({"foreign"})))
    case["outcome"] = _CallModelOutcome(bad, case["outcome"].audit_call)
    with pytest.raises(AuditIntegrityError, match="inconsistent tool identities"):
        observe_primary_provider_result(**case)


def test_failed_physical_audit_wins_over_large_projection() -> None:
    case = _case()
    call = case["outcome"].audit_call
    assert call is not None
    diagnostic = canonical_json({"payload": "x" * 220_000})
    receipts = tuple(
        PrivateReceivedDiagnostic(f"diagnostic-{index}", f"call-{index}", "list_sources", diagnostic, '{"status":"received"}', None)
        for index in range(5)
    )
    failed = replace(
        call,
        status=ComposerLLMCallStatus.API_ERROR,
        error_class="ProviderError",
        error_message="The physical provider call failed",
        private_diagnostics=receipts,
    )
    assert measure_physical_audit_call_size(failed).exceeded
    _with_call(case, failed)
    with pytest.raises(AuditIntegrityError, match="changed its actual physical identity"):
        observe_primary_provider_result(**case)


def test_valid_original_pair_at_limit_exposes_only_new_pair_excess() -> None:
    case = _case()
    _snapshot, audit, source = _pair_candidate()
    previous = decode_turn_evidence(source, env_ref_names=frozenset(), source_chunks=(audit,))
    call = case["outcome"].audit_call
    assert call is not None
    prior = decode_recorder_snapshot(previous.recorder_chunk)
    case["previous_evidence"] = previous
    case["recorder"] = replace(prior, llm_calls=(*prior.llm_calls, call))
    original_pair = source.reference.byte_length + audit.reference.byte_length
    assert source.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    assert audit.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    assert original_pair == CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
    new_recorder = encode_recorder_snapshot(case["recorder"])
    new_evidence = previous.snapshot.model_copy(update={"recorder": new_recorder.reference})
    new_evidence_size = bounded_canonical_json_size(
        retained_model_json_view(new_evidence), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    ).admitted_bytes
    assert new_evidence_size is not None
    assert new_recorder.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    assert new_evidence_size + new_recorder.reference.byte_length > CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
    observed = observe_primary_provider_result(**case)
    assert isinstance(observed, PrimaryOverflowObservation)
    assert (observed.representation, observed.document_codec) == ("evidence_pair", "composer.evidence.v2")
    assert observed.observed_bytes == observed.limit_bytes + 1
    assert observed.cost.status == "unknown"


@pytest.mark.parametrize("large", (False, True))
def test_audit_only_credential_refuses_before_any_excess(large: bool) -> None:
    case = _case()
    call = case["outcome"].audit_call
    assert call is not None
    if large:
        diagnostic = canonical_json({"payload": "x" * 220_000})
        receipts = tuple(
            PrivateReceivedDiagnostic(f"diagnostic-{index}", f"call-{index}", "list_sources", diagnostic, '{"status":"received"}', None)
            for index in range(5)
        )
        call = replace(call, private_diagnostics=receipts)
    call = replace(call, model_requested="postgresql://example:secret@db.example/database")  # secret-scan: allow-this-line
    _with_call(case, call)
    with pytest.raises(CredentialMaterialRefused):
        observe_primary_provider_result(**case)


def test_completion_credential_refuses_before_size() -> None:
    case = _case()
    completion = case["outcome"].completion
    bad = replace(
        completion,
        message=replace(completion.message, content="postgresql://example:secret@db.example/database"),  # secret-scan: allow-this-line
    )
    case["outcome"] = _CallModelOutcome(bad, case["outcome"].audit_call)
    with pytest.raises(CredentialMaterialRefused):
        observe_primary_provider_result(**case)
    with pytest.raises(CredentialMaterialRefused):
        _require_no_credential_material_in_completion(bad, surface="composer_primary_response")

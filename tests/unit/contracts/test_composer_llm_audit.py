"""Unit tests for the composer LLM-call audit L0 contract."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime

import pytest

from elspeth.contracts.composer_llm_audit import (
    PROVIDER_COST_SOURCE_HIDDEN_PARAMS_RESPONSE_COST,
    ComposerLLMCall,
    ComposerLLMCallRecorder,
    ComposerLLMCallStatus,
)
from elspeth.core.canonical import stable_hash


def _make_call(**overrides: object) -> ComposerLLMCall:
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "assistant", "content": "Prior turn"},
        {"role": "user", "content": "Current turn"},
    ]
    t = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    defaults: dict[str, object] = {
        "model_requested": "openrouter/openai/gpt-5.5",
        "model_returned": "openai/gpt-5.5-2026-05-01",
        "status": ComposerLLMCallStatus.SUCCESS,
        "prompt_tokens": 17,
        "completion_tokens": 5,
        "total_tokens": 22,
        "latency_ms": 123,
        "provider_request_id": "chatcmpl-safe-scalar",
        "messages_hash": stable_hash(messages),
        "tools_spec_hash": stable_hash([{"type": "function", "function": {"name": "set_source"}}]),
        "declared_tool_names": ("set_source",),
        "started_at": t,
        "finished_at": t,
        "error_class": None,
        "error_message": None,
        "temperature": 0.0,
        "seed": 42,
    }
    defaults.update(overrides)
    return ComposerLLMCall(**defaults)  # type: ignore[arg-type]


def test_status_strenum_values() -> None:
    assert ComposerLLMCallStatus.SUCCESS.value == "success"
    assert ComposerLLMCallStatus.TIMEOUT.value == "timeout"
    assert ComposerLLMCallStatus.API_ERROR.value == "api_error"
    assert ComposerLLMCallStatus.AUTH_ERROR.value == "auth_error"
    assert ComposerLLMCallStatus.BAD_REQUEST_ERROR.value == "bad_request_error"
    assert ComposerLLMCallStatus.MALFORMED_RESPONSE.value == "malformed_response"
    assert ComposerLLMCallStatus.CANCELLED.value == "cancelled"


def test_to_dict_serializes_enum_and_datetimes() -> None:
    call = _make_call()

    payload = call.to_dict()

    assert payload["status"] == "success"
    assert isinstance(payload["started_at"], str)
    assert isinstance(payload["finished_at"], str)
    assert payload["declared_tool_names"] == ["set_source"]


@pytest.mark.parametrize(
    ("declared_tool_names", "exc_type", "match"),
    [
        (["set_source"], TypeError, "declared_tool_names must be tuple"),
        (("",), ValueError, "declared_tool_names entries must be non-empty"),
        (("set_source", "set_source"), ValueError, "declared_tool_names entries must be unique"),
        (("set-source",), ValueError, "declared_tool_names entries must be tool names"),
    ],
)
def test_declared_tool_names_are_closed_structural_metadata(
    declared_tool_names: object,
    exc_type: type[Exception],
    match: str,
) -> None:
    with pytest.raises(exc_type, match=match):
        _make_call(declared_tool_names=declared_tool_names)


def test_token_none_values_are_preserved() -> None:
    call = _make_call(prompt_tokens=None, completion_tokens=None, total_tokens=None)

    payload = call.to_dict()

    assert payload["prompt_tokens"] is None
    assert payload["completion_tokens"] is None
    assert payload["total_tokens"] is None


def test_provider_cost_fields_are_serialized_without_fabricating_cost() -> None:
    call = _make_call(provider_cost=0.0037, provider_cost_source="response_usage.cost")

    payload = call.to_dict()

    assert payload["provider_cost"] == 0.0037
    assert payload["provider_cost_source"] == "response_usage.cost"


def test_pricing_identity_is_retained_in_serialized_audit() -> None:
    call = _make_call(model_requested="openai/operator-datazone", pricing_model="azure/gpt-4o")
    payload = call.to_dict()
    assert payload["model_requested"] == "openai/operator-datazone"
    assert payload["pricing_model"] == "azure/gpt-4o"


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_pricing_identity_cannot_be_blank(value: str) -> None:
    with pytest.raises(ValueError, match="pricing_model"):
        _make_call(pricing_model=value)


def test_private_provider_cost_source_is_serialized_with_provenance() -> None:
    call = _make_call(
        provider_cost=0.01234,
        provider_cost_source=PROVIDER_COST_SOURCE_HIDDEN_PARAMS_RESPONSE_COST,
    )

    payload = call.to_dict()

    assert payload["provider_cost"] == 0.01234
    assert payload["provider_cost_source"] == "_hidden_params.response_cost"


def test_model_drift_preserves_requested_and_returned_models() -> None:
    call = _make_call(model_requested="anthropic/claude-sonnet-4.5", model_returned="anthropic/claude-sonnet-4.5-20260501")

    payload = call.to_dict()

    assert payload["model_requested"] == "anthropic/claude-sonnet-4.5"
    assert payload["model_returned"] == "anthropic/claude-sonnet-4.5-20260501"


def test_messages_hash_is_hash_of_full_request_messages_array() -> None:
    full_messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "assistant", "content": "Prior turn"},
        {"role": "user", "content": "Current turn"},
    ]
    without_history = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Current turn"},
    ]

    call = _make_call(messages_hash=stable_hash(full_messages))

    assert call.messages_hash == stable_hash(full_messages)
    assert call.messages_hash != stable_hash(without_history)


def test_error_fields_are_safe_class_name_payloads() -> None:
    call = _make_call(
        status=ComposerLLMCallStatus.AUTH_ERROR,
        model_returned=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        provider_request_id=None,
        error_class="AuthenticationError",
        error_message="AuthenticationError",
    )

    payload = call.to_dict()

    assert payload["status"] == "auth_error"
    assert payload["error_class"] == "AuthenticationError"
    assert payload["error_message"] == "AuthenticationError"


def test_frozen_dataclass_blocks_mutation() -> None:
    call = _make_call()

    with pytest.raises(dataclasses.FrozenInstanceError):
        call.model_requested = "different"  # type: ignore[misc]


def test_l0_module_has_no_upward_imports() -> None:
    import elspeth.contracts.composer_llm_audit as audit

    forbidden_prefixes = ("elspeth.core", "elspeth.engine", "elspeth.plugins", "elspeth.web", "elspeth.cli")
    for ref in audit.__dict__.values():
        # Classes and functions carry their own ``__module__``; for any other
        # value the name resolves through its type, which is what the old
        # sentinel ``getattr`` was already reading. Narrow to the two cases and
        # read the owned attribute directly.
        ref_module = ref.__module__ if isinstance(ref, type) or inspect.isroutine(ref) else type(ref).__module__
        for prefix in forbidden_prefixes:
            assert not ref_module.startswith(prefix), f"composer_llm_audit imports {ref!r} from forbidden module {ref_module}"


def test_cache_token_fields_default_to_none() -> None:
    """Cache token fields default to None — absence is evidence, not zero.

    An absent value stays None rather than being coerced to zero — absence is
    evidence, and a fabricated zero is indistinguishable from a measured zero.
    Per elspeth-4e79436719 §Bug C: a
    missing provider cache statistic must NOT be coerced to zero. The
    audit row distinguishes "no cache reported" from "cache reported
    zero hits" — only the latter is a real provider claim.
    """
    call = _make_call()
    payload = call.to_dict()
    assert call.cached_prompt_tokens is None
    assert call.cache_creation_input_tokens is None
    assert call.cache_read_input_tokens is None
    assert payload["cached_prompt_tokens"] is None
    assert payload["cache_creation_input_tokens"] is None
    assert payload["cache_read_input_tokens"] is None


def test_cache_token_fields_round_trip_when_known() -> None:
    """Cache fields persist through to_dict for both provider shapes."""
    call = _make_call(
        cached_prompt_tokens=1024,
        cache_creation_input_tokens=500,
        cache_read_input_tokens=900,
    )
    payload = call.to_dict()
    assert payload["cached_prompt_tokens"] == 1024
    assert payload["cache_creation_input_tokens"] == 500
    assert payload["cache_read_input_tokens"] == 900


def test_provider_reasoning_fields_round_trip_when_reported() -> None:
    """Provider-supplied reasoning artifacts are retained on the hidden audit sidecar.

    These fields are intentionally separate from normal assistant message
    content: they exist so operators can diagnose tool-call/config failures
    against provider metadata without exposing hidden reasoning in the user
    chat transcript.
    """
    reasoning_details = [
        {"type": "reasoning.text", "text": "checked available pipeline tools"},
        {"type": "reasoning.signature", "signature": "opaque-provider-signature"},
    ]
    thinking_blocks = [{"type": "thinking", "thinking": "provider-supplied thinking block"}]

    call = _make_call(
        reasoning_tokens=12,
        reasoning_content="provider supplied reasoning text",
        reasoning_details=reasoning_details,
        thinking_blocks=thinking_blocks,
    )

    payload = call.to_dict()

    assert payload["reasoning_tokens"] == 12
    assert payload["reasoning_content"] == "provider supplied reasoning text"
    assert payload["reasoning_details"] == reasoning_details
    assert payload["thinking_blocks"] == thinking_blocks


def test_provider_reasoning_fields_default_to_none() -> None:
    call = _make_call()
    payload = call.to_dict()

    assert call.reasoning_tokens is None
    assert call.reasoning_content is None
    assert call.reasoning_details is None
    assert call.thinking_blocks is None
    assert payload["reasoning_tokens"] is None
    assert payload["reasoning_content"] is None
    assert payload["reasoning_details"] is None
    assert payload["thinking_blocks"] is None


def test_composer_llm_call_records_temperature_and_seed() -> None:
    """Configured temperature and seed round-trip through to_dict()."""
    call = _make_call(temperature=0.0, seed=42)

    payload = call.to_dict()

    assert call.temperature == 0.0
    assert call.seed == 42
    assert payload["temperature"] == 0.0
    assert payload["seed"] == 42


def test_temperature_accepts_none_for_omitted_request_parameter() -> None:
    call = _make_call(temperature=None)

    payload = call.to_dict()

    assert call.temperature is None
    assert payload["temperature"] is None


def test_composer_llm_call_allows_seed_none_when_provider_omits_it() -> None:
    """Unsupported provider params are omitted; audit records the actual request shape."""
    call = _make_call(model_requested="anthropic/claude-3-5-sonnet-20241022", seed=None)

    payload = call.to_dict()

    assert call.temperature == 0.0
    assert call.seed is None
    assert payload["seed"] is None


@pytest.mark.parametrize(
    ("overrides", "exc_type", "match"),
    [
        ({"status": "success"}, TypeError, "status must be ComposerLLMCallStatus"),
        ({"prompt_tokens": True}, TypeError, "prompt_tokens must be int"),
        ({"completion_tokens": -1}, ValueError, "completion_tokens must be >= 0"),
        ({"total_tokens": 1.5}, TypeError, "total_tokens must be int"),
        ({"latency_ms": -1}, ValueError, "latency_ms must be >= 0"),
        ({"seed": 1.5}, TypeError, "seed must be int"),
        ({"temperature": float("inf")}, ValueError, "temperature must be finite"),
        ({"model_requested": ""}, ValueError, "model_requested must be non-empty"),
        ({"model_returned": ""}, ValueError, "model_returned must be non-empty"),
        ({"messages_hash": ""}, ValueError, "messages_hash must be non-empty"),
        ({"tools_spec_hash": ""}, ValueError, "tools_spec_hash must be non-empty"),
        ({"provider_request_id": ""}, ValueError, "provider_request_id must be non-empty"),
        ({"started_at": "2026-05-04T12:00:00Z"}, TypeError, "started_at must be datetime"),
        ({"finished_at": "2026-05-04T12:00:00Z"}, TypeError, "finished_at must be datetime"),
        ({"finished_at": datetime(2026, 5, 4, 11, 59, 59, tzinfo=UTC)}, ValueError, "finished_at must be >= started_at"),
        ({"error_class": "TimeoutError"}, ValueError, "SUCCESS calls must not include error_class or error_message"),
        (
            {"status": ComposerLLMCallStatus.TIMEOUT, "error_class": None, "error_message": "TimeoutError"},
            ValueError,
            "non-success calls must include error_class and error_message",
        ),
    ],
)
def test_composer_llm_call_rejects_invalid_audit_shape(
    overrides: dict[str, object],
    exc_type: type[Exception],
    match: str,
) -> None:
    with pytest.raises(exc_type, match=match):
        _make_call(**overrides)


def test_recorder_protocol_runtime_check() -> None:
    class _StubRecorder:
        def record_llm_call(self, call: ComposerLLMCall) -> None:
            return

        def resolve_session(self, session_id: str) -> None:
            return

    rec: ComposerLLMCallRecorder = _StubRecorder()
    rec.record_llm_call(_make_call())
    rec.resolve_session("abc")

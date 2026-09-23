"""OpenRouter's served-endpoint name on composer LLM audit rows (``provider_served``).

OpenRouter routes one model id across many endpoints and names the endpoint that
served each completion in the response's top-level ``provider`` field. Strict
tool contracts (plan ``2026-09-23-composer-strict-tool-contracts.md`` S0/S1) are
judged per served endpoint, so the name must reach the persisted audit row.

Measured on LiteLLM 1.102.0 with a loopback recorder (S0 "Verify first"): a
canned OpenRouter-shaped response carrying ``"provider": "X"`` reaches the
object the composer admits, on both the ``openrouter/`` route and an ``openai/``
route with a custom ``api_base``; a response without the key yields absence.

Three disciplines are pinned here:

- **Tier-3 admission, bounded.** The value is provider-authored. Only a closed
  provider-name shape is recorded verbatim; any other present value is recorded
  as the fixed ``unrecognised`` token, never as provider bytes. Absence stays
  ``None``.
- **Real provider objects.** The happy path uses genuine
  ``litellm.types.utils.ModelResponse`` objects (ADR-032).
- **End-to-end to storage.** ``_LLM_CALL_PUBLIC_AUDIT_FIELDS`` is a closed
  whitelist; a field left out of it is silently dropped at every drain.
"""

from __future__ import annotations

import time
from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

import pytest
from litellm.types.utils import Choices, Message, ModelResponse

import elspeth.web.composer.audit as composer_audit
from elspeth.contracts.composer_llm_audit import (
    PROVIDER_SERVED_UNRECOGNISED,
    ComposerLLMCall,
    ComposerLLMCallStatus,
)
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import admit_llm_provider_metadata, build_llm_call_record
from elspeth.web.sessions.guided_audit import prepare_guided_audit_rows

# Every ``provider_name`` in the OpenRouter endpoints API for the three models
# the plan measured (deepseek/deepseek-v4.1-flash, z-ai/glm-5.3, openai/gpt-5.5),
# fetched 2026-09-23 (lane evidence ``providers-evidence/ep_*.json``). This is
# the known-positive corpus for the closed shape.
_MEASURED_OPENROUTER_PROVIDER_NAMES = (
    "AkashML",
    "Alibaba",
    "Amazon Bedrock",
    "AtlasCloud",
    "Azure",
    "Baidu",
    "BaseTen",
    "Cloudflare",
    "CoreWeave",
    "Crusoe",
    "Decart",
    "DeepInfra",
    "DeepSeek",
    "DigitalOcean",
    "Fireworks",
    "Friendli",
    "GMICloud",
    "Inceptron",
    "InferenceNet",
    "Io Net",
    "Makora",
    "Mistral",
    "Modal",
    "Morph",
    "Novita",
    "OpenAI",
    "OpenInference",
    "Parasail",
    "Phala",
    "Reka",
    "Relace",
    "Sail Research",
    "SiliconFlow",
    "StreamLake",
    "Together",
    "Venice",
    "Wafer",
    "Z.AI",
)

# Present values outside the closed shape. None of these may reach the audit
# row as provider bytes.
_OUT_OF_SHAPE_VALUES: tuple[Any, ...] = (
    "A" * 65,
    "<script>alert(1)</script>",
    "Deep\nInfra",
    " DeepInfra",
    "DeepInfra ",
    "sk-live/secret=value",
    "https://evil.example/x",
    42,
    {"name": "DeepInfra"},
    ["DeepInfra"],
)


def _real_response(**extra: Any) -> ModelResponse:
    return ModelResponse(
        id="req-provider-served",
        model="deepseek/deepseek-v4.1-flash",
        choices=[Choices(finish_reason="stop", index=0, message=Message(content="ok", role="assistant"))],
        **extra,
    )


def _record_from_response(response: Any) -> ComposerLLMCall:
    """The pipeline-planner success path: ``build_llm_call_record(response=...)``."""
    return build_llm_call_record(
        model_requested="openrouter/deepseek/deepseek-v4.1-flash",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=response,
    )


def _record_from_admitted_metadata(response: Any) -> ComposerLLMCall:
    """The compose-loop path: metadata admitted once, then recorded."""
    metadata = admit_llm_provider_metadata(response, choice=None, message=None)
    return build_llm_call_record(
        model_requested="openrouter/deepseek/deepseek-v4.1-flash",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response_metadata=metadata,
    )


_RECORD_PATHS = (
    pytest.param(_record_from_response, id="response-branch"),
    pytest.param(_record_from_admitted_metadata, id="admitted-metadata-branch"),
)


def _envelope_call(call: ComposerLLMCall) -> dict[str, object]:
    """The ``call`` projection of a persisted LLM audit envelope."""
    payload = llm_call_audit_envelope(call)["call"]
    assert isinstance(payload, dict)
    return payload


def _llm_call(**overrides: Any) -> ComposerLLMCall:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "model_requested": "openrouter/deepseek/deepseek-v4.1-flash",
        "model_returned": "deepseek/deepseek-v4.1-flash",
        "status": ComposerLLMCallStatus.SUCCESS,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "latency_ms": 5,
        "provider_request_id": "req-1",
        "messages_hash": "a" * 64,
        "tools_spec_hash": None,
        "declared_tool_names": (),
        "started_at": now,
        "finished_at": now,
        "error_class": None,
        "error_message": None,
        "temperature": None,
        "seed": None,
    }
    values.update(overrides)
    return ComposerLLMCall(**values)


class TestDeclaredFields:
    def test_llm_call_declares_provider_served(self) -> None:
        assert "provider_served" in {field.name for field in fields(ComposerLLMCall)}

    def test_admitted_provider_metadata_declares_provider_served(self) -> None:
        assert "provider_served" in {field.name for field in fields(_AdmittedLLMProviderMetadata)}

    def test_public_audit_whitelist_carries_provider_served(self) -> None:
        assert "provider_served" in composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS


class TestAdmission:
    @pytest.mark.parametrize("record_path", _RECORD_PATHS)
    @pytest.mark.parametrize("provider_name", _MEASURED_OPENROUTER_PROVIDER_NAMES)
    def test_measured_openrouter_provider_names_are_recorded_verbatim(self, record_path: Any, provider_name: str) -> None:
        record = record_path(_real_response(provider=provider_name))

        assert record.provider_served == provider_name

    @pytest.mark.parametrize("record_path", _RECORD_PATHS)
    @pytest.mark.parametrize("value", _OUT_OF_SHAPE_VALUES, ids=lambda value: type(value).__name__)
    def test_out_of_shape_values_record_the_fixed_token(self, record_path: Any, value: Any) -> None:
        record = record_path(_real_response(provider=value))

        assert record.provider_served == PROVIDER_SERVED_UNRECOGNISED
        assert record.provider_served == "unrecognised"

    @pytest.mark.parametrize("record_path", _RECORD_PATHS)
    @pytest.mark.parametrize("value", ("", "   "), ids=["empty", "blank"])
    def test_blank_provider_is_absence(self, record_path: Any, value: str) -> None:
        record = record_path(_real_response(provider=value))

        assert record.provider_served is None

    @pytest.mark.parametrize("record_path", _RECORD_PATHS)
    def test_absent_provider_stays_none(self, record_path: Any) -> None:
        record = record_path(_real_response())

        assert record.provider_served is None

    def test_no_response_records_absence(self) -> None:
        assert _record_from_response(None).provider_served is None


class TestContract:
    @pytest.mark.parametrize("value", ("DeepInfra", "Z.AI", PROVIDER_SERVED_UNRECOGNISED, None))
    def test_contract_accepts_the_closed_shape_and_the_token(self, value: str | None) -> None:
        assert _llm_call(provider_served=value).provider_served == value

    @pytest.mark.parametrize("value", ("<script>", "A" * 65, "Deep\nInfra", " DeepInfra", ""))
    def test_contract_rejects_provider_bytes_outside_the_shape(self, value: str) -> None:
        with pytest.raises(ValueError, match="provider_served"):
            _llm_call(provider_served=value)

    def test_contract_rejects_non_string(self) -> None:
        with pytest.raises(TypeError, match="provider_served"):
            _llm_call(provider_served=42)


class TestSurvivesToThePersistedProjection:
    def test_public_audit_envelope_exposes_provider_served(self) -> None:
        call_payload = _envelope_call(_record_from_response(_real_response(provider="DeepInfra")))

        assert call_payload["provider_served"] == "DeepInfra"

    def test_absent_provider_served_persists_as_null_not_omitted(self) -> None:
        call_payload = _envelope_call(_llm_call())

        assert "provider_served" in call_payload
        assert call_payload["provider_served"] is None

    def test_mutation_control_dropping_the_whitelist_entry_loses_the_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The instrument above must go red when the whitelist entry is removed."""
        mutated = tuple(field for field in composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS if field != "provider_served")
        assert len(mutated) == len(composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS) - 1
        monkeypatch.setattr(composer_audit, "_LLM_CALL_PUBLIC_AUDIT_FIELDS", mutated)

        call_payload = _envelope_call(_llm_call(provider_served="DeepInfra"))

        assert "provider_served" not in call_payload

    def test_guided_failure_row_preserves_provider_served(self) -> None:
        rows = prepare_guided_audit_rows(
            invocations=(),
            llm_calls=(
                _llm_call(
                    provider_served="DeepInfra",
                    status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
                    error_class="MalformedResponse",
                    error_message="truncated mid tool call",
                ),
            ),
            chat_turns=(),
        )

        (row,) = rows
        assert row.envelope["call"]["provider_served"] == "DeepInfra"

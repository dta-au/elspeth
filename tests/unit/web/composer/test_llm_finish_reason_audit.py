"""Provider finish-reason capture on composer LLM audit rows.

The composer records ``choices[0].finish_reason`` verbatim so an operator can
tell a completed answer (``stop``) from one the provider truncated (``length``)
or refused (``content_filter``). Before this evidence existed, all three looked
identical in the audit trail.

Two disciplines are pinned here:

- **Real provider objects.** The happy path is exercised against genuine
  ``litellm.types.utils.ModelResponse`` / ``Choices`` objects, not hand-written
  fakes. ADR-032 records why: a typed fake satisfies boundary readers that a
  real pydantic ``extra="allow"`` vendor object fails, so a green suite over
  fakes is not evidence the boundary admits real traffic.
- **End-to-end to storage.** A field on the dataclass is worthless if the
  persistence projection drops it. ``_LLM_CALL_PUBLIC_AUDIT_FIELDS`` in
  ``web/composer/audit.py`` is an explicit whitelist, so the envelope every
  drain site persists through is asserted directly.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.sessions.guided_audit import prepare_guided_audit_rows

# Values litellm's ``Choices`` Literal accepts and passes through unchanged.
# ``tool_calls`` is the one that matters most for truncation forensics: a turn
# cut off mid-tool-call is the failure this evidence exists to expose.
_LITELLM_ACCEPTED_FINISH_REASONS = ("stop", "length", "tool_calls", "content_filter")


def _real_response(finish_reason: str) -> ModelResponse:
    """Build a genuine litellm response object carrying ``finish_reason``."""
    return ModelResponse(
        id="req-finish-reason",
        model="returned-model",
        choices=[
            Choices(
                finish_reason=finish_reason,
                index=0,
                message=Message(content="partial answer", role="assistant"),
            )
        ],
    )


def _record(response: Any) -> ComposerLLMCall:
    return build_llm_call_record(
        model_requested="requested-model",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=response,
    )


class TestRealProviderResponses:
    """Extraction against genuine litellm SDK objects (ADR-032 testing rule)."""

    def test_real_litellm_response_carries_finish_reason_onto_the_record(self) -> None:
        record = _record(_real_response("length"))

        assert record.finish_reason == "length"

    @pytest.mark.parametrize("finish_reason", _LITELLM_ACCEPTED_FINISH_REASONS)
    def test_known_finish_reasons_round_trip_verbatim(self, finish_reason: str) -> None:
        record = _record(_real_response(finish_reason))

        assert record.finish_reason == finish_reason

    def test_truncated_and_completed_calls_are_now_distinguishable(self) -> None:
        """The whole point: two otherwise-identical audit rows must differ."""
        completed = _record(_real_response("stop"))
        truncated = _record(_real_response("length"))

        assert completed.status is truncated.status
        assert completed.finish_reason != truncated.finish_reason


class TestVerbatimCapture:
    """No normalisation: an unrecognised provider term survives intact.

    ``litellm.types.utils.Choices`` types ``finish_reason`` as a ``Literal``
    and its validator rewrites anything outside that set to ``"stop"`` (it
    logs ``Unmapped finish_reason ..., defaulting to 'stop'``). An
    unrecognised value therefore cannot be expressed through a real
    ``Choices`` object at all, so the no-normalisation guarantee is asserted
    through the module's other first-class provider shape: the Mapping-shaped
    response, which ``_provider_field`` handles explicitly and which
    ``token_usage_from_response`` already branches on.
    """

    def test_unrecognised_provider_value_is_not_mapped_or_bucketed(self) -> None:
        record = _record({"choices": [{"finish_reason": "provider_specific_halt"}]})

        assert record.finish_reason == "provider_specific_halt"

    def test_case_and_spelling_are_preserved_exactly(self) -> None:
        record = _record({"choices": [{"finish_reason": "MAX_TOKENS"}]})

        assert record.finish_reason == "MAX_TOKENS"


class TestAbsenceIsNotFabricated:
    """Absent stays ``None`` — never ``""``, never a default value."""

    def test_no_response_yields_none(self) -> None:
        assert _record(None).finish_reason is None

    def test_response_without_the_field_yields_none(self) -> None:
        assert _record({"choices": [{"message": {"content": "hi"}}]}).finish_reason is None

    def test_empty_choices_yields_none(self) -> None:
        assert _record({"choices": []}).finish_reason is None

    def test_explicit_null_finish_reason_yields_none(self) -> None:
        assert _record({"choices": [{"finish_reason": None}]}).finish_reason is None

    def test_blank_finish_reason_is_absence_not_an_empty_string(self) -> None:
        record = _record({"choices": [{"finish_reason": "   "}]})

        assert record.finish_reason is None
        assert record.finish_reason != ""

    def test_non_string_finish_reason_is_treated_as_absent(self) -> None:
        assert _record({"choices": [{"finish_reason": 7}]}).finish_reason is None


class TestContractValidation:
    def test_empty_finish_reason_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="finish_reason must be non-empty"):
            _llm_call(finish_reason="")

    def test_non_string_finish_reason_is_rejected_at_construction(self) -> None:
        with pytest.raises(TypeError, match="finish_reason must be str"):
            _llm_call(finish_reason=object())

    def test_finish_reason_defaults_to_none(self) -> None:
        assert _llm_call().finish_reason is None


def _llm_call(
    *,
    finish_reason: Any = None,
    status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS,
    error_class: str | None = None,
    error_message: str | None = None,
) -> ComposerLLMCall:
    now = datetime.now(UTC)
    return ComposerLLMCall(
        model_requested="requested-model",
        model_returned="returned-model",
        status=status,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        latency_ms=5,
        provider_request_id="req-1",
        messages_hash="a" * 64,
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=now,
        finished_at=now,
        error_class=error_class,
        error_message=error_message,
        temperature=None,
        seed=None,
        finish_reason=finish_reason,
    )


class TestSurvivesToThePersistedProjection:
    """``_LLM_CALL_PUBLIC_AUDIT_FIELDS`` is an explicit whitelist.

    Every drain site — ``routes/_helpers._persist_llm_calls``,
    ``composer/service``, and ``sessions/guided_audit`` — persists through
    ``llm_call_audit_envelope``, so pinning the envelope covers all three.
    """

    def test_to_dict_carries_the_raw_value(self) -> None:
        assert _llm_call(finish_reason="length").to_dict()["finish_reason"] == "length"

    @pytest.mark.parametrize("finish_reason", (*_LITELLM_ACCEPTED_FINISH_REASONS, "provider_specific_halt"))
    def test_public_audit_envelope_exposes_finish_reason(self, finish_reason: str) -> None:
        envelope = llm_call_audit_envelope(_llm_call(finish_reason=finish_reason))

        assert envelope["call"]["finish_reason"] == finish_reason  # type: ignore[index]

    def test_absent_finish_reason_persists_as_null_not_omitted(self) -> None:
        call_payload = llm_call_audit_envelope(_llm_call())["call"]

        assert "finish_reason" in call_payload  # type: ignore[operator]
        assert call_payload["finish_reason"] is None  # type: ignore[index]

    def test_end_to_end_from_real_response_to_persisted_envelope(self) -> None:
        record = _record(_real_response("content_filter"))

        envelope = llm_call_audit_envelope(record)

        assert envelope["call"]["finish_reason"] == "content_filter"  # type: ignore[index]

    def test_guided_audit_row_preserves_finish_reason(self) -> None:
        rows = prepare_guided_audit_rows(
            invocations=(),
            llm_calls=(_llm_call(finish_reason="length"),),
            chat_turns=(),
        )

        (row,) = rows
        assert row.kind == "llm"
        assert row.envelope["call"]["finish_reason"] == "length"

    def test_guided_failure_redaction_does_not_strip_finish_reason(self) -> None:
        """The non-success branch rebuilds the payload and nulls named keys.

        That rewrite is the one place ``finish_reason`` could be dropped by
        construction rather than by the whitelist — and truncation evidence
        matters most precisely on the calls that did not succeed.
        """
        rows = prepare_guided_audit_rows(
            invocations=(),
            llm_calls=(
                _llm_call(
                    finish_reason="length",
                    status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
                    error_class="MalformedResponse",
                    error_message="truncated mid tool call",
                ),
            ),
            chat_turns=(),
        )

        (row,) = rows
        assert row.envelope["call"]["finish_reason"] == "length"
        assert row.envelope["call"]["error_message"] is None

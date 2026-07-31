"""Bounds on provider-authored strings reaching composer LLM audit rows.

ELSPETH lets an operator point the composer at any OpenAI-compatible
endpoint. The failure this guards is not a hostile provider but a
malfunctioning one — a buggy proxy, a mis-translated upstream, an error blob
returned where a short token belongs. Those strings land in
``chat_messages.tool_calls`` (stored) and, through
``llm_call_audit_summary``, in the message-list view (rendered).

Three properties are pinned here:

- **A working endpoint is untouched.** Every real value passes through
  byte-identical, in both projections.
- **A malfunctioning endpoint degrades, it does not destroy.** An oversized
  value is truncated and the audit row still exists — truncating rather than
  rejecting is the point: rejecting would discard the evidence that the
  endpoint misbehaved.
- **Truncation is detectable.** The in-band ``<truncated:{n}>`` suffix
  travels with the value into every projection, so a reader never sees a cut
  value that merely looks short.

Testing asymmetry — read before "fixing" a test
------------------------------------------------
Oversized ``finish_reason`` cases MUST go through the Mapping-shaped
response, not a real ``litellm`` ``Choices``: ``Choices`` types
``finish_reason`` as a ``Literal`` and its validator silently rewrites
anything outside that set to ``"stop"``, so an oversized value cannot be
expressed through a real ``Choices`` object at all. ``ModelResponse.model``
carries no such Literal, so oversized ``model_returned`` cases DO use a real
provider object. Both shapes are first-class inputs to
``_provider_field``; the split is about what each object can express, not
about avoiding real objects (ADR-032).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.web.composer.audit import llm_call_audit_envelope, llm_call_audit_summary
from elspeth.web.composer.llm_response_parsing import (
    PROVIDER_STRING_TRUNCATION_MARKER,
    build_llm_call_record,
)

# The bounds under test, restated here as the test's own expectation rather
# than imported from the module — importing the private constant would make
# the test agree with the implementation by construction.
FINISH_REASON_LIMIT = 128
MODEL_LIMIT = 256
# ``provider_request_id`` shares the identifier bound with ``model_returned``.
REQUEST_ID_LIMIT = 256

# Termination tokens real providers emit. OpenAI-family (``stop``,
# ``length``, ``tool_calls``, ``content_filter``), Anthropic (``end_turn``,
# ``max_tokens``, ``stop_sequence``, ``tool_use``), Bedrock
# (``CONTENT_FILTERED``) and Gemini — whose ``FINISH_REASON_UNSPECIFIED`` and
# ``MALFORMED_FUNCTION_CALL`` are the longest values any provider is known to
# emit, and are still a fifth of the bound.
REAL_FINISH_REASONS = (
    "stop",
    "length",
    "tool_calls",
    "content_filter",
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "CONTENT_FILTERED",
    "MAX_TOKENS",
    "RECITATION",
    "SAFETY",
    "MALFORMED_FUNCTION_CALL",
    "FINISH_REASON_UNSPECIFIED",
)

# Values ``litellm``'s ``Choices`` Literal accepts and passes through intact,
# so they can be exercised against a genuine provider object.
LITELLM_ACCEPTED_FINISH_REASONS = ("stop", "length", "tool_calls", "content_filter")

# Legitimately long model identifiers. These are the reason the model bound
# is 256 rather than the token bound: an operator pointing at Bedrock or
# Vertex gets identifiers in this shape on every single call.
LONG_BUT_LEGITIMATE_MODELS = (
    "anthropic/claude-sonnet-4-6",
    "meta-llama/llama-3.1-70b-instruct",
    "accounts/fireworks/models/llama-v3p1-405b-instruct",
    "arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "projects/elspeth-acceptance-project/locations/australia-southeast1/publishers/anthropic/models/claude-3-5-sonnet@20240620",
)


def _real_response(
    *,
    finish_reason: str = "stop",
    model: str = "returned-model",
    response_id: str = "req-bounded-strings",
) -> ModelResponse:
    """A genuine litellm response object (ADR-032: real objects at the boundary)."""
    return ModelResponse(
        id=response_id,
        model=model,
        choices=[
            Choices(
                finish_reason=finish_reason,
                index=0,
                message=Message(content="an answer", role="assistant"),
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


def _envelope_call(record: ComposerLLMCall) -> dict[str, Any]:
    call = llm_call_audit_envelope(record)["call"]
    assert isinstance(call, dict)
    return call


def _summary(record: ComposerLLMCall) -> dict[str, Any]:
    parsed = json.loads(llm_call_audit_summary(record))
    assert isinstance(parsed, dict)
    return parsed


class TestWellBehavedEndpointIsUntouched:
    """A normal value must be byte-identical, before and after this bound."""

    @pytest.mark.parametrize("finish_reason", REAL_FINISH_REASONS)
    def test_real_finish_reasons_are_recorded_verbatim(self, finish_reason: str) -> None:
        record = _record({"choices": [{"finish_reason": finish_reason}]})

        assert record.finish_reason == finish_reason
        assert PROVIDER_STRING_TRUNCATION_MARKER not in str(record.finish_reason)

    @pytest.mark.parametrize("finish_reason", LITELLM_ACCEPTED_FINISH_REASONS)
    def test_real_provider_object_finish_reason_survives_to_the_envelope(self, finish_reason: str) -> None:
        record = _record(_real_response(finish_reason=finish_reason))

        assert _envelope_call(record)["finish_reason"] == finish_reason

    @pytest.mark.parametrize("finish_reason", ("length", "content_filter"))
    def test_abnormal_finish_reasons_reach_the_rendered_summary_verbatim(self, finish_reason: str) -> None:
        """The rendered projection carries the abnormal ones unchanged."""
        record = _record(_real_response(finish_reason=finish_reason))

        assert _summary(record)["finish_reason"] == finish_reason

    @pytest.mark.parametrize("finish_reason", ("stop", "tool_calls"))
    def test_routine_finish_reasons_stay_out_of_the_summary(self, finish_reason: str) -> None:
        """Not a regression: ``_ROUTINE_FINISH_REASONS`` omits these by design.

        The envelope still carries them; only the human-facing line drops
        them so a badge does not appear on every row.
        """
        record = _record(_real_response(finish_reason=finish_reason))

        assert _envelope_call(record)["finish_reason"] == finish_reason
        assert "finish_reason" not in _summary(record)

    @pytest.mark.parametrize("model", LONG_BUT_LEGITIMATE_MODELS)
    def test_long_but_legitimate_model_identifiers_are_not_cut(self, model: str) -> None:
        record = _record(_real_response(model=model))

        assert record.model_returned == model
        assert _envelope_call(record)["model_returned"] == model
        assert _summary(record)["model_returned"] == model


class TestBoundaries:
    """Exactly at the limit passes; one character over is cut."""

    def test_finish_reason_exactly_at_the_limit_is_untouched(self) -> None:
        value = "f" * FINISH_REASON_LIMIT

        record = _record({"choices": [{"finish_reason": value}]})

        assert record.finish_reason == value

    def test_finish_reason_one_over_the_limit_is_truncated(self) -> None:
        value = "f" * (FINISH_REASON_LIMIT + 1)

        record = _record({"choices": [{"finish_reason": value}]})

        assert record.finish_reason != value
        assert record.finish_reason == f"{'f' * FINISH_REASON_LIMIT}<truncated:{FINISH_REASON_LIMIT + 1}>"

    def test_model_exactly_at_the_limit_is_untouched(self) -> None:
        value = "m" * MODEL_LIMIT

        record = _record(_real_response(model=value))

        assert record.model_returned == value

    def test_model_one_over_the_limit_is_truncated(self) -> None:
        value = "m" * (MODEL_LIMIT + 1)

        record = _record(_real_response(model=value))

        assert record.model_returned != value
        assert record.model_returned == f"{'m' * MODEL_LIMIT}<truncated:{MODEL_LIMIT + 1}>"

    def test_request_id_exactly_at_the_limit_is_untouched(self) -> None:
        value = "r" * REQUEST_ID_LIMIT

        record = _record(_real_response(response_id=value))

        assert record.provider_request_id == value

    def test_request_id_one_over_the_limit_is_truncated(self) -> None:
        value = "r" * (REQUEST_ID_LIMIT + 1)

        record = _record(_real_response(response_id=value))

        assert record.provider_request_id != value
        assert record.provider_request_id == f"{'r' * REQUEST_ID_LIMIT}<truncated:{REQUEST_ID_LIMIT + 1}>"


class TestOversizedValueDegradesGracefully:
    """Truncate, keep the row, make the anomaly legible."""

    def test_oversized_finish_reason_still_produces_an_audit_row(self) -> None:
        """Rejecting would discard the evidence the endpoint misbehaved."""
        record = _record({"choices": [{"finish_reason": "x" * 50_000}]})

        assert isinstance(record, ComposerLLMCall)
        assert record.status is ComposerLLMCallStatus.SUCCESS
        assert record.messages_hash

    def test_oversized_finish_reason_is_detectably_truncated(self) -> None:
        record = _record({"choices": [{"finish_reason": "x" * 50_000}]})

        assert record.finish_reason is not None
        assert PROVIDER_STRING_TRUNCATION_MARKER in record.finish_reason

    def test_the_original_length_is_recorded_in_the_marker(self) -> None:
        """ "Sent 200 characters" and "sent 50 KB" are different diagnoses."""
        record = _record({"choices": [{"finish_reason": "x" * 50_000}]})

        assert str(record.finish_reason).endswith("<truncated:50000>")

    def test_a_provider_error_blob_is_bounded_not_stored_whole(self) -> None:
        """The realistic shape: an error payload where a token belongs."""
        blob = json.dumps({"error": {"message": "upstream unavailable " * 500, "code": 502}})

        record = _record({"choices": [{"finish_reason": blob}]})

        assert record.finish_reason is not None
        assert len(record.finish_reason) < len(blob)
        assert len(record.finish_reason) <= FINISH_REASON_LIMIT + len(f"<truncated:{len(blob)}>")

    def test_oversized_model_still_satisfies_the_contract(self) -> None:
        """The bounded value must remain a legal ``model_returned``."""
        record = _record(_real_response(model="m" * 5_000))

        assert record.model_returned is not None
        assert PROVIDER_STRING_TRUNCATION_MARKER in record.model_returned

    def test_oversized_leading_whitespace_model_does_not_destroy_the_row(self) -> None:
        """Load-bearing: the truncated remnant must survive the contract.

        Both extraction sites admit a value on ``value.strip()`` being
        truthy but truncate the *unstripped* value, so a value that is
        mostly leading whitespace slices down to pure blanks. The record
        survives only because the marker is appended after the slice and is
        itself non-blank — ``_require_non_empty_str`` would otherwise raise
        and destroy the audit row on exactly the path the design forbids.
        Moving the signal to a companion field, or making it strippable,
        silently reintroduces that raise.
        """
        record = _record(_real_response(model=" " * 300 + "x"))

        assert record.model_returned is not None
        assert record.model_returned.strip()
        assert record.model_returned.endswith("<truncated:301>")

    def test_oversized_leading_whitespace_finish_reason_does_not_destroy_the_row(self) -> None:
        """Sibling of the model case — the same slice-to-blank hazard."""
        record = _record({"choices": [{"finish_reason": " " * 200 + "x"}]})

        assert record.finish_reason is not None
        assert record.finish_reason.strip()
        assert record.finish_reason.endswith("<truncated:201>")


class TestBoundedOnceAndInheritedByEveryProjection:
    """One bound at the capture point — projections do not re-truncate."""

    def test_truncated_finish_reason_is_identical_in_both_projections(self) -> None:
        record = _record({"choices": [{"finish_reason": "x" * 50_000}]})

        assert _envelope_call(record)["finish_reason"] == record.finish_reason
        assert _summary(record)["finish_reason"] == record.finish_reason

    def test_truncated_model_is_identical_in_both_projections(self) -> None:
        record = _record(_real_response(model="m" * 5_000))

        assert _envelope_call(record)["model_returned"] == record.model_returned
        assert _summary(record)["model_returned"] == record.model_returned

    def test_the_rendered_summary_stays_small_for_a_broken_endpoint(self) -> None:
        """The whole point: unbounded text must not reach a rendered column."""
        record = _record(
            {
                "choices": [{"finish_reason": "x" * 200_000}],
                "model": "m" * 200_000,
            }
        )

        assert len(llm_call_audit_summary(record)) < 1_000

    def test_a_truncated_marker_survives_json_serialisation(self) -> None:
        record = _record({"choices": [{"finish_reason": "x" * 400}]})

        round_tripped = json.loads(json.dumps(llm_call_audit_envelope(record)))

        assert round_tripped["call"]["finish_reason"] == record.finish_reason


class TestUnexpectedTypesDoNotCrashTheRecorder:
    """A malfunctioning endpoint sends wrong types, not just long strings."""

    @pytest.mark.parametrize("finish_reason", (7, 3.5, True, [], {}, None, ("stop",)))
    def test_non_string_finish_reason_is_absence_not_a_crash(self, finish_reason: Any) -> None:
        record = _record({"choices": [{"finish_reason": finish_reason}]})

        assert record.finish_reason is None

    @pytest.mark.parametrize("model", (7, 3.5, True, [], {}, None, "", "   "))
    def test_non_string_or_blank_model_is_absence_not_a_crash(self, model: Any) -> None:
        record = _record({"choices": [], "model": model})

        assert record.model_returned is None

    def test_a_response_that_is_not_an_object_at_all_does_not_crash(self) -> None:
        record = _record(42)

        assert record.finish_reason is None
        assert record.model_returned is None
        assert record.provider_request_id is None


class TestProviderRequestIdTruncatesRatherThanDisappears:
    """An over-long correlation id must stay visible as an over-long id.

    This bound used to be enforced by DROPPING the value, which made an
    endpoint emitting a broken id indistinguishable from one emitting no id
    at all — the same lost-evidence class already fixed for ``finish_reason``
    and ``model_returned``. Truncate-and-signal keeps the distinction.
    """

    @pytest.mark.parametrize(
        "request_id",
        (
            "chatcmpl-9x2Kf7QwLmNpRtVbYc3ZaHdEg",
            "req_011CQ8vT5nYzKpMwRsXbLfGh",
            "msg_01ABcDeFgHiJkLmNoPqRsTuV",
            "01234567-89ab-cdef-0123-456789abcdef",
        ),
    )
    def test_real_request_ids_are_recorded_verbatim(self, request_id: str) -> None:
        record = _record(_real_response(response_id=request_id))

        assert record.provider_request_id == request_id
        assert PROVIDER_STRING_TRUNCATION_MARKER not in str(record.provider_request_id)

    def test_an_absent_request_id_is_still_none(self) -> None:
        """Absence must remain expressible — it is half of the distinction."""
        record = _record({"choices": [], "model": "returned-model"})

        assert record.provider_request_id is None

    def test_an_oversized_request_id_is_not_mistaken_for_absence(self) -> None:
        """The regression this closes: over-long used to read as "no id sent"."""
        record = _record(_real_response(response_id="r" * 5_000))

        assert record.provider_request_id is not None
        assert PROVIDER_STRING_TRUNCATION_MARKER in record.provider_request_id

    def test_the_original_length_is_recorded_in_the_marker(self) -> None:
        record = _record(_real_response(response_id="r" * 5_000))

        assert str(record.provider_request_id).endswith("<truncated:5000>")

    def test_an_oversized_request_id_still_produces_an_audit_row(self) -> None:
        record = _record(_real_response(response_id="r" * 50_000))

        assert isinstance(record, ComposerLLMCall)
        assert record.status is ComposerLLMCallStatus.SUCCESS
        assert record.messages_hash

    def test_an_oversized_request_id_does_not_fall_through_to_request_id(self) -> None:
        """Load-bearing: selection happens BEFORE bounding, not through it.

        ``id`` is preferred over ``request_id``, so the first non-empty
        string wins and is then capped. If the length test were folded back
        into the acceptance predicate, an over-long ``id`` would fall through
        and the row would record a clean ``request_id`` from an endpoint that
        had just emitted a broken id — the anomaly would vanish exactly as it
        did before this fix.
        """
        record = _record({"id": "i" * 5_000, "request_id": "req-fallback", "choices": []})

        assert record.provider_request_id is not None
        assert record.provider_request_id.startswith("i" * REQUEST_ID_LIMIT)
        assert record.provider_request_id.endswith("<truncated:5000>")

    def test_request_id_is_still_the_fallback_when_id_is_absent(self) -> None:
        """The preference order itself is unchanged by this fix."""
        record = _record({"request_id": "req-fallback", "choices": []})

        assert record.provider_request_id == "req-fallback"

    def test_an_oversized_whitespace_request_id_does_not_destroy_the_row(self) -> None:
        """Sibling of the model/finish_reason slice-to-blank hazard.

        The value is truncated unstripped, so a mostly-blank id slices down
        to pure whitespace; the record survives only because the non-blank
        marker is appended after the slice. ``_require_non_empty_str`` would
        otherwise raise and destroy the audit row.
        """
        record = _record({"id": " " * 300 + "x", "choices": []})

        assert record.provider_request_id is not None
        assert record.provider_request_id.strip()
        assert record.provider_request_id.endswith("<truncated:301>")

    def test_a_truncated_request_id_reaches_the_envelope_unchanged(self) -> None:
        record = _record(_real_response(response_id="r" * 5_000))

        assert _envelope_call(record)["provider_request_id"] == record.provider_request_id

    @pytest.mark.parametrize("request_id", (7, 3.5, [], {}, None, "", "   ", "\t\n"))
    def test_non_string_or_blank_request_id_is_absence_not_a_crash(self, request_id: Any) -> None:
        """A blank id must be absence — admitting it raised and lost the row.

        ``_require_non_empty_str`` rejects a whitespace-only string, so a
        short blank id admitted on mere truthiness reached the contract and
        raised, destroying the audit row on exactly the path this bound
        exists to keep alive. Blank is now absence, as it already was for
        ``model_returned``.
        """
        record = _record({"id": request_id, "choices": []})

        assert record.provider_request_id is None

    def test_a_blank_id_falls_through_to_a_usable_request_id(self) -> None:
        """Blank is absence, so the preference order continues past it."""
        record = _record({"id": "   ", "request_id": "req-fallback", "choices": []})

        assert record.provider_request_id == "req-fallback"

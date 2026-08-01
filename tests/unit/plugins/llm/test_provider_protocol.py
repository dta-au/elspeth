# tests/unit/plugins/llm/test_provider_protocol.py
"""Tests for LLMProvider protocol and DTOs."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from enum import StrEnum

import pytest

from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.transforms.llm.provider import (
    FinishReason,
    FinishReasonFailure,
    LLMAuditParent,
    LLMProvider,
    LLMQueryResult,
    UnrecognizedFinishReason,
    classify_finish_reason_failure,
    parse_finish_reason,
)


class _IdentifierEnum(StrEnum):
    VALUE = "identifier-value"


class _FormattingString(str):
    def __str__(self) -> str:
        return "formatted-differently"


def test_llm_audit_parent_accepts_row_and_operation_forms() -> None:
    row = LLMAuditParent.for_row(state_id="state-1", token_id="token-1")
    operation = LLMAuditParent.for_operation(operation_id="operation-1")

    assert row.client_kwargs() == {
        "state_id": "state-1",
        "token_id": "token-1",
        "operation_id": None,
    }
    assert operation.client_kwargs() == {
        "state_id": None,
        "token_id": None,
        "operation_id": "operation-1",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"state_id": "state-1"},
        {"token_id": "token-1"},
        {"operation_id": "operation-1", "state_id": "state-1", "token_id": "token-1"},
        {"operation_id": " "},
    ],
)
def test_llm_audit_parent_rejects_invalid_parentage(kwargs: dict[str, str]) -> None:
    with pytest.raises((TypeError, ValueError)):
        LLMAuditParent(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"state_id": 1, "token_id": "token-1"}, "state_id"),
        ({"state_id": "state-1", "token_id": 1}, "token_id"),
        ({"operation_id": 1}, "operation_id"),
    ],
)
def test_llm_audit_parent_rejects_non_string_ids_with_type_error(
    kwargs: dict[str, object],
    field_name: str,
) -> None:
    with pytest.raises(TypeError, match=rf"{field_name} must be a string"):
        LLMAuditParent(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [_IdentifierEnum.VALUE, _FormattingString("identifier-value")])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"state_id": "state-1", "token_id": "token-1"},
        {"operation_id": "operation-1"},
    ],
)
def test_llm_audit_parent_rejects_string_subclasses_before_identity_use(
    value: str,
    kwargs: dict[str, str],
) -> None:
    field_name = "operation_id" if "operation_id" in kwargs else "state_id"
    kwargs[field_name] = value

    with pytest.raises(TypeError, match=rf"{field_name} must be a string"):
        LLMAuditParent(**kwargs)


@pytest.mark.parametrize("finish_reason", [FinishReason.STOP, None])
def test_finish_reason_classifier_accepts_success_forms(finish_reason: FinishReason | None) -> None:
    assert classify_finish_reason_failure(finish_reason) is None


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        (
            FinishReason.LENGTH,
            FinishReasonFailure(
                reason="response_truncated",
                finish_reason="length",
                error_message="Response truncated (finish_reason=length)",
            ),
        ),
        (
            FinishReason.CONTENT_FILTER,
            FinishReasonFailure(
                reason="content_filtered",
                finish_reason="content_filter",
                error_message="Response blocked by provider content filter",
            ),
        ),
        (
            FinishReason.TOOL_CALLS,
            FinishReasonFailure(
                reason="unexpected_finish_reason",
                finish_reason="tool_calls",
                error_message="Unexpected finish reason: tool_calls",
            ),
        ),
        (
            UnrecognizedFinishReason("safety_filter"),
            FinishReasonFailure(
                reason="unexpected_finish_reason",
                finish_reason="safety_filter",
                error_message="Unexpected finish reason: safety_filter",
            ),
        ),
    ],
)
def test_finish_reason_classifier_returns_provider_neutral_failure(
    finish_reason: FinishReason | UnrecognizedFinishReason,
    expected: FinishReasonFailure,
) -> None:
    assert classify_finish_reason_failure(finish_reason) == expected


class TestLLMQueryResult:
    """Tests for the LLMQueryResult frozen dataclass."""

    def test_is_frozen(self) -> None:
        result = LLMQueryResult(
            content="hello",
            usage=TokenUsage.known(10, 5),
            model="gpt-4o",
        )
        with pytest.raises(FrozenInstanceError):
            result.content = "modified"  # type: ignore[misc]

    def test_fields(self) -> None:
        usage = TokenUsage.known(10, 5)
        result = LLMQueryResult(
            content="hello",
            usage=usage,
            model="gpt-4o",
            finish_reason=FinishReason.STOP,
        )
        assert result.content == "hello"
        assert result.usage is usage
        assert result.model == "gpt-4o"
        assert result.finish_reason == FinishReason.STOP
        # raw_response is NOT on LLMQueryResult
        assert not hasattr(result, "raw_response")

    def test_post_init_rejects_empty_content(self) -> None:
        with pytest.raises(ValueError, match="content must be non-empty"):
            LLMQueryResult(
                content="",
                usage=TokenUsage.unknown(),
                model="gpt-4o",
            )

    def test_post_init_rejects_whitespace_content(self) -> None:
        with pytest.raises(ValueError, match="content must be non-empty"):
            LLMQueryResult(
                content="   ",
                usage=TokenUsage.unknown(),
                model="gpt-4o",
            )

    def test_post_init_rejects_empty_model(self) -> None:
        with pytest.raises(ValueError, match="model must be non-empty"):
            LLMQueryResult(
                content="hello",
                usage=TokenUsage.unknown(),
                model="",
            )

    def test_post_init_rejects_whitespace_model(self) -> None:
        with pytest.raises(ValueError, match="model must be non-empty"):
            LLMQueryResult(
                content="hello",
                usage=TokenUsage.unknown(),
                model="   ",
            )

    def test_finish_reason_defaults_to_none(self) -> None:
        result = LLMQueryResult(
            content="hello",
            usage=TokenUsage.unknown(),
            model="gpt-4o",
        )
        assert result.finish_reason is None

    def test_post_init_rejects_wrong_usage_type(self) -> None:
        """usage must be a TokenUsage instance, not a dict or other type.

        Bug: elspeth-42cb31ce6f. Without runtime validation, a caller
        passing a dict or None for usage succeeds at construction and
        explodes later when the transform accesses .prompt_tokens.
        """
        with pytest.raises(TypeError, match="usage"):
            LLMQueryResult(
                content="hello",
                usage={"prompt_tokens": 10, "completion_tokens": 5},  # type: ignore[arg-type]
                model="gpt-4o",
            )

    def test_post_init_rejects_none_usage(self) -> None:
        """usage=None must be rejected — TokenUsage.unknown() exists for that."""
        with pytest.raises(TypeError, match="usage"):
            LLMQueryResult(
                content="hello",
                usage=None,  # type: ignore[arg-type]
                model="gpt-4o",
            )

    def test_post_init_rejects_wrong_finish_reason_type(self) -> None:
        """finish_reason must be ParsedFinishReason, not a raw string.

        Bug: elspeth-42cb31ce6f. A raw string like "stop" bypasses the
        FinishReason enum and UnrecognizedFinishReason sentinel.
        """
        with pytest.raises(TypeError, match="finish_reason"):
            LLMQueryResult(
                content="hello",
                usage=TokenUsage.unknown(),
                model="gpt-4o",
                finish_reason="stop",  # type: ignore[arg-type]  # deliberate: tests rejection of raw string (must use FinishReason.STOP)
            )


class TestFinishReason:
    """Tests for FinishReason StrEnum."""

    def test_enum_values(self) -> None:
        assert FinishReason.STOP.value == "stop"
        assert FinishReason.LENGTH.value == "length"
        assert FinishReason.CONTENT_FILTER.value == "content_filter"
        assert FinishReason.TOOL_CALLS.value == "tool_calls"

    def test_from_string(self) -> None:
        assert FinishReason("stop") is FinishReason.STOP
        assert FinishReason("length") is FinishReason.LENGTH


class TestParseFinishReason:
    """Tests for parse_finish_reason helper."""

    def test_none_returns_none(self) -> None:
        assert parse_finish_reason(None) is None

    def test_valid_stop(self) -> None:
        assert parse_finish_reason("stop") is FinishReason.STOP

    def test_valid_length(self) -> None:
        assert parse_finish_reason("length") is FinishReason.LENGTH

    def test_valid_content_filter(self) -> None:
        assert parse_finish_reason("content_filter") is FinishReason.CONTENT_FILTER

    def test_valid_tool_calls(self) -> None:
        assert parse_finish_reason("tool_calls") is FinishReason.TOOL_CALLS

    def test_unknown_returns_unrecognized_sentinel(self) -> None:
        result = parse_finish_reason("end_turn")
        assert isinstance(result, UnrecognizedFinishReason)
        assert result.raw == "end_turn"

    def test_empty_string_returns_unrecognized_sentinel(self) -> None:
        result = parse_finish_reason("")
        assert isinstance(result, UnrecognizedFinishReason)
        assert result.raw == ""


class TestLLMProviderProtocol:
    """Tests for the LLMProvider protocol."""

    def test_is_runtime_checkable(self) -> None:
        # Verify @runtime_checkable allows isinstance checks.
        # Without the decorator, isinstance() would raise TypeError.
        assert not isinstance(object(), LLMProvider)

    def test_mock_provider_satisfies_protocol(self) -> None:
        class MockProvider:
            def execute_query(
                self,
                messages: list[dict[str, str]],
                *,
                model: str,
                temperature: float,
                max_tokens: int | None,
                audit_parent: LLMAuditParent,
            ) -> LLMQueryResult:
                del audit_parent
                return LLMQueryResult(
                    content="test",
                    usage=TokenUsage.unknown(),
                    model=model,
                )

            def runtime_preflight(self, *, operation_id: str, model: str) -> None:
                del operation_id, model

            def close(self) -> None:
                pass

        provider = MockProvider()
        assert isinstance(provider, LLMProvider)

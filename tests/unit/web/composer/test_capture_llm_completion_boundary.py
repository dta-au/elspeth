"""Boundary honesty tests for ``_capture_composer_llm_completion_fields``.

The function is the declared Tier-3 parse boundary for the raw provider
completion object (``@trust_boundary(source_param="response")``): malformed
shapes raise ``_MalformedLLMResponseError`` carrying only already-admitted
provider facts; nothing is coerced or fabricated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from elspeth.web.composer.service import (
    _capture_composer_llm_completion_fields,
    _MalformedLLMResponseError,
)


def test_malformed_choices_raises_malformed_llm_response_error() -> None:
    response = SimpleNamespace(choices="not-a-sequence")
    with pytest.raises(_MalformedLLMResponseError):
        _capture_composer_llm_completion_fields(response)


def test_missing_choices_raises() -> None:
    with pytest.raises(_MalformedLLMResponseError):
        _capture_composer_llm_completion_fields(SimpleNamespace())


def test_empty_choices_raises() -> None:
    with pytest.raises(_MalformedLLMResponseError):
        _capture_composer_llm_completion_fields(SimpleNamespace(choices=[]))


def test_choice_without_message_raises() -> None:
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])
    with pytest.raises(_MalformedLLMResponseError):
        _capture_composer_llm_completion_fields(response)


def test_non_string_content_raises() -> None:
    message = SimpleNamespace(content=42, tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    with pytest.raises(_MalformedLLMResponseError):
        _capture_composer_llm_completion_fields(response)


def test_non_sequence_tool_calls_raises() -> None:
    message = SimpleNamespace(content=None, tool_calls="bad")
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    with pytest.raises(_MalformedLLMResponseError):
        _capture_composer_llm_completion_fields(response)


def test_well_formed_response_is_admitted() -> None:
    message = SimpleNamespace(content="hello", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    admitted, tool_calls, _metadata = _capture_composer_llm_completion_fields(response)
    assert admitted.content == "hello"
    assert tool_calls == ()

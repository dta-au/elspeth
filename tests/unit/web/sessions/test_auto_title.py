from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from litellm import ModelResponse

from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.telemetry import _FakeCounter


class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []

    async def update_session_title(self, session_id: object, title: str) -> None:
        self.updates.append((session_id, title))


def _completion(content: str | None) -> ModelResponse:
    return ModelResponse(choices=[{"index": 0, "message": {"role": "assistant", "content": content}}])


def _completion_with_finish(content: str, finish_reason: object) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


def _completion_without_finish(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


async def _run_auto_title(monkeypatch, response: object) -> tuple[_TitleService, object, object]:
    """Drive maybe_auto_title_session against a canned completion.

    Returns (service, failed_counter, rejected_counter) for assertions.
    """
    failed = _FakeCounter()
    rejected = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", failed)
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_REJECTED_COUNTER", rejected)

    async def _canned(**_kwargs: object) -> object:
        return response

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _canned)
    service = _TitleService()
    await _auto_title.maybe_auto_title_session(
        service=service,
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )
    return service, failed, rejected


# The exact completion shape from live acceptance 2026-08-04
# (elspeth-308d1e0831): the model ignored the naming instruction and began
# answering the user's message, provider-truncated at max_tokens.
_LIVE_LEAK_COMPLETION = "# CSV Classification Pipeline\n\n```python\nimport anthropic\nimport pandas as pd\n"


@pytest.mark.asyncio
async def test_auto_title_rejects_the_live_leaked_runaway_completion(monkeypatch) -> None:
    service, failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(_LIVE_LEAK_COMPLETION, "length"))
    assert service.updates == []
    assert failed.calls == []
    assert rejected.calls == [(1, {"rejection_class": "character", "finish_reason": "length"}, None)]


@pytest.mark.asyncio
async def test_auto_title_gate_rejects_runaway_even_without_finish_reason(monkeypatch) -> None:
    """Design invariant: the shape gate alone must stop the leak.

    finish_reason is advisory telemetry — litellm defaults an omitted
    field to "stop", so admission can never key on it.
    """
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_without_finish(_LIVE_LEAK_COMPLETION))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "character", "finish_reason": "absent"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "separator",
    ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\t"],
    ids=["line-sep", "para-sep", "nel", "vertical-tab", "form-feed", "tab"],
)
async def test_auto_title_rejects_non_space_whitespace(monkeypatch, separator: str) -> None:
    """Unicode line breaks the old collapse silently welded into spaces."""
    service, _failed, rejected = await _run_auto_title(
        monkeypatch, _completion_with_finish(f"CSV Pipeline{separator}import anthropic", "stop")
    )
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "character", "finish_reason": "stop"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "`CSV Pipeline`",
        "**CSV Pipeline**",
        "[CSV Pipeline](http://example.com)",
        "> CSV Pipeline",
        "1. CSV Pipeline",
        "Here's a title: CSV Pipeline",
        "I can't help with that.",
        "\x1b[31mRed Title",
        "Zero​Width Title",
        "Data ‮ Pipeline",
        "Null\x00Byte Title",
    ],
    ids=[
        "inline-code",
        "bold",
        "link",
        "blockquote",
        "list-numbered",
        "preamble-colon",
        "refusal-trailing-period",
        "ansi-escape",
        "zero-width",
        "rtl-override",
        "null-byte",
    ],
)
async def test_auto_title_rejects_non_title_characters(monkeypatch, content: str) -> None:
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(content, "stop"))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "character", "finish_reason": "stop"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "Fix AKIA1234567890ABCDEF Key Error",  # secret-scan: allow-this-line
        "Fix 123-45-6789 Payroll Issue",
    ],
    ids=["aws-access-key", "ssn"],
)
async def test_auto_title_rejects_credential_shaped_titles(monkeypatch, content: str) -> None:
    """Credential shapes pass the character allowlist — the layered check
    exists precisely because letters/digits/hyphens are title-legal."""
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(content, "stop"))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "credential", "finish_reason": "stop"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["Pipeline", "One Two Three Four Five Six Seven Eight Nine"],
    ids=["one-word", "nine-words"],
)
async def test_auto_title_rejects_out_of_bounds_word_counts(monkeypatch, content: str) -> None:
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(content, "stop"))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "word_count", "finish_reason": "stop"}, None)]


@pytest.mark.asyncio
async def test_auto_title_rejects_over_length_instead_of_truncating(monkeypatch) -> None:
    """Truncation is what laundered the live leak into a plausible record."""
    content = "Comprehensive Multiregional Classification Pipeline Architecture Documentation"
    assert len(content) > 60
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(content, "stop"))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "length", "finish_reason": "stop"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["CSV Pipeline -", "- CSV Pipeline"],
    ids=["trailing-hyphen", "leading-list-dash"],
)
async def test_auto_title_rejects_edge_punctuation(monkeypatch, content: str) -> None:
    """Allowlisted characters are still rejected at the title's edges —
    a leading "- " is a markdown list marker, not a title."""
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(content, "stop"))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "edge_punctuation", "finish_reason": "stop"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "   "], ids=["empty", "whitespace-only"])
async def test_auto_title_counts_empty_completions_as_rejections(monkeypatch, content: str) -> None:
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(content, "stop"))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "empty", "finish_reason": "stop"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("CSV Classification Pipeline", "CSV Classification Pipeline"),
        ('"CSV Classification Pipeline"', "CSV Classification Pipeline"),
        ("Data — Cleanup Pipeline", "Data — Cleanup Pipeline"),
        ("User\u2019s Data Pipeline", "User\u2019s Data Pipeline"),
        ("Анализ Данных CSV", "Анализ Данных CSV"),
        ("Extract & Classify Reviews", "Extract & Classify Reviews"),
        ("My  Title", "My Title"),
        ("Retry Config (Round 2)", "Retry Config (Round 2)"),
    ],
    ids=[
        "plain",
        "surrounding-quotes-stripped",
        "em-dash",
        "curly-apostrophe",
        "cyrillic",
        "ampersand",
        "double-space-collapsed",
        "parenthesized",
    ],
)
async def test_auto_title_accepts_title_shaped_completions(monkeypatch, content: str, expected: str) -> None:
    service, failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(content, "stop"))
    assert [title for _sid, title in service.updates] == [expected]
    assert failed.calls == []
    assert rejected.calls == []


@pytest.mark.asyncio
async def test_auto_title_labels_openrouter_error_finish_reason(monkeypatch) -> None:
    """ "error" is OpenRouter's fifth normalized value — it must label the
    rejection rather than collapse into "other"."""
    service, _failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish(_LIVE_LEAK_COMPLETION, "error"))
    assert service.updates == []
    assert rejected.calls == [(1, {"rejection_class": "character", "finish_reason": "error"}, None)]


@pytest.mark.asyncio
async def test_auto_title_outbound_call_redacts_fences_and_truncates(monkeypatch) -> None:
    """The naming call must never ship raw first-message content: secrets
    are redacted before truncation, the excerpt is bounded, and the user
    turn is fenced as untrusted data."""
    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return _completion("Useful Pipeline")

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _capture)
    service = _TitleService()
    secret = "AKIA" + "A" * 16
    await _auto_title.maybe_auto_title_session(
        service=service,
        session_id=uuid4(),
        user_message=f"Use key {secret} to build a CSV pipeline. " + "x" * 5000,
        model="openai/test",
        temperature=None,
        seed=None,
    )
    messages = captured["messages"]
    assert messages[0]["role"] == "system"
    user_content = messages[1]["content"]
    assert secret not in user_content
    assert "<redacted-sensitive:aws_access_key>" in user_content
    assert user_content.count(_auto_title._AUTO_TITLE_EXCERPT_FENCE) == 2
    assert len(user_content) < _auto_title._AUTO_TITLE_EXCERPT_CHAR_LIMIT + 300
    assert captured["max_tokens"] == 40
    assert [title for _sid, title in service.updates] == ["Useful Pipeline"]


@pytest.mark.asyncio
async def test_auto_title_rejects_non_string_finish_reason_as_malformed(monkeypatch) -> None:
    service, failed, rejected = await _run_auto_title(monkeypatch, _completion_with_finish("CSV Pipeline", 7))
    assert service.updates == []
    assert rejected.calls == []
    assert failed.calls == [(1, {"exception_class": "MalformedResponseError"}, None)]


@pytest.mark.asyncio
async def test_auto_title_timeout_records_telemetry_and_returns(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _raise_timeout(**_kwargs: object) -> object:
        raise TimeoutError("title generation timed out")

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _raise_timeout)
    service = _TitleService()

    await _auto_title.maybe_auto_title_session(
        service=service,
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )

    assert service.updates == []
    assert counter.calls == [(1, {"exception_class": "TimeoutError"}, None)]


@pytest.mark.asyncio
async def test_auto_title_malformed_provider_response_records_telemetry_and_returns(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _malformed_response(**_kwargs: object) -> object:
        return ModelResponse(choices=[])

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _malformed_response)
    service = _TitleService()

    await _auto_title.maybe_auto_title_session(
        service=service,
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )

    assert service.updates == []
    assert counter.calls == [(1, {"exception_class": "MalformedResponseError"}, None)]


@pytest.mark.asyncio
async def test_auto_title_null_provider_content_is_an_explicit_no_title(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _null_content(**_kwargs: object) -> object:
        return _completion(None)

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _null_content)
    service = _TitleService()

    await _auto_title.maybe_auto_title_session(
        service=service,
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )

    assert service.updates == []
    assert counter.calls == []


@pytest.mark.asyncio
async def test_auto_title_rejects_non_string_provider_content(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _wrong_content_type(**_kwargs: object) -> object:
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=7))])

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _wrong_content_type)
    service = _TitleService()

    await _auto_title.maybe_auto_title_session(
        service=service,
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )

    assert service.updates == []
    assert counter.calls == [(1, {"exception_class": "MalformedResponseError"}, None)]


@pytest.mark.asyncio
async def test_auto_title_programmer_error_propagates(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _raise_programmer_error(**_kwargs: object) -> object:
        raise TypeError("signature drift")

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _raise_programmer_error)

    with pytest.raises(TypeError, match="signature drift"):
        await _auto_title.maybe_auto_title_session(
            service=_TitleService(),
            session_id=uuid4(),
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )

    assert counter.calls == []


@pytest.mark.asyncio
async def test_auto_title_title_write_failure_propagates(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _completion_response(**_kwargs: object) -> object:
        return _completion("Useful Pipeline")

    class _FailingService(_TitleService):
        async def update_session_title(self, session_id: object, title: str) -> None:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _completion_response)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await _auto_title.maybe_auto_title_session(
            service=_FailingService(),
            session_id=uuid4(),
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )

    assert counter.calls == []

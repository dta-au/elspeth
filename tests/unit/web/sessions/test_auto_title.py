from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import litellm
import pytest
from litellm import ModelResponse
from litellm.exceptions import BadGatewayError, ServiceUnavailableError
from litellm.exceptions import Timeout as LiteLLMTimeout
from sqlalchemy import insert, select

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer import llm_response_parsing
from elspeth.web.composer.prompt_guard import PromptGuardRefused
from elspeth.web.coordination import quota_authority
from elspeth.web.coordination.quota_authority import ProviderAttempt, TokenUsageEntry
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.models import chat_messages_table, token_usage_ledger_table
from elspeth.web.sessions.telemetry import _FakeCounter
from tests.helpers.auto_title import RecordingTitleService as _TitleService
from tests.helpers.auto_title import call_auto_title

_DISPATCHED_AT = datetime(2026, 9, 15, 23, 59, 59, tzinfo=UTC)


class _TitleClock:
    @staticmethod
    def now(tz: object) -> datetime:
        assert tz is UTC
        return _DISPATCHED_AT


@pytest.fixture(autouse=True)
def _freeze_dispatch_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_auto_title, "datetime", _TitleClock)
    monkeypatch.setattr(llm_response_parsing, "datetime", _TitleClock)
    monkeypatch.setattr(quota_authority, "database_now", lambda _connection: _DISPATCHED_AT)


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

    monkeypatch.setattr(litellm, "acompletion", _canned)
    service = _TitleService()
    await call_auto_title(
        service=service,
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )
    return service, failed, rejected


@pytest.mark.asyncio
async def test_auto_title_required_guard_override_refuses_before_admission(monkeypatch) -> None:
    calls = []

    async def must_not_dispatch(**kwargs):
        calls.append(kwargs)
        raise AssertionError("Required guard refusal must precede physical SDK dispatch")

    monkeypatch.setattr(litellm, "acompletion", must_not_dispatch)
    service = _TitleService()
    with pytest.raises(PromptGuardRefused) as caught:
        await call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
            prompt_guard_mode="required",
        )
    assert caught.value.disposition == "unavailable"
    assert calls == service.attempts == service.updates == service.usage == []
    with service._engine.connect() as connection:
        rows = connection.execute(select(chat_messages_table)).all()
    assert len(rows) == 1
    check = rows[0].tool_calls[0]["check"]
    assert (check["role"], check["mode"], check["disposition"]) == ("composer", "required", "unavailable")


@pytest.mark.asyncio
async def test_recording_title_usage_does_not_hide_unlinked_ledger_rows(monkeypatch) -> None:
    service, _failed, _rejected = await _run_auto_title(monkeypatch, _completion("Useful Pipeline"))
    assert len(service.usage) == 1
    assert service.usage[0][2][0].call_id == service.attempts[0].attempt_id
    with service._engine.begin() as connection:
        connection.execute(
            insert(token_usage_ledger_table).values(
                entry_id=str(uuid4()),
                identity_id="title-user",
                source="auto_title",
                session_id=str(service.session_id),
                run_id=None,
                model="controlled/title",
                prompt_tokens=1,
                completion_tokens=1,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                recorded_at=_DISPATCHED_AT,
            )
        )
    with pytest.raises(AssertionError, match="Title ledger rows must retain their actual provider attempt"):
        _ = service.usage


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

    monkeypatch.setattr(litellm, "acompletion", _capture)
    service = _TitleService()
    secret = "AKIA" + "A" * 16
    await call_auto_title(
        service=service,
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
async def test_auto_title_threads_exact_compose_context_to_title_write(monkeypatch) -> None:
    observed: list[tuple[object, str, SessionOperationContext]] = []

    class _FencedTitleService(_TitleService):
        async def before_title_write(self, session_id, title, context) -> None:
            observed.append((session_id, title, context))

    async def _successful_completion(**_kwargs: object) -> object:
        return _completion("Fenced title")

    monkeypatch.setattr(litellm, "acompletion", _successful_completion)

    service = _FencedTitleService()
    await call_auto_title(
        service=service,
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )

    assert observed == [(service.session_id, "Fenced title", service.last_context)]


@pytest.mark.asyncio
async def test_auto_title_timeout_records_telemetry_and_returns(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _raise_timeout(**_kwargs: object) -> object:
        raise TimeoutError("title generation timed out")

    monkeypatch.setattr(litellm, "acompletion", _raise_timeout)
    service = _TitleService()

    await call_auto_title(
        service=service,
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

    monkeypatch.setattr(litellm, "acompletion", _malformed_response)
    service = _TitleService()

    await call_auto_title(
        service=service,
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

    monkeypatch.setattr(litellm, "acompletion", _null_content)
    service = _TitleService()

    await call_auto_title(
        service=service,
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

    monkeypatch.setattr(litellm, "acompletion", _wrong_content_type)
    service = _TitleService()

    await call_auto_title(
        service=service,
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

    monkeypatch.setattr(litellm, "acompletion", _raise_programmer_error)

    service = _TitleService()
    with pytest.raises(TypeError, match="signature drift"):
        await call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )

    assert counter.calls == []
    assert service.updates == []
    assert len(service.usage) == 1
    entry = service.usage[0][2][0]
    assert entry.call_id == service.attempts[0].attempt_id
    assert entry.prompt_tokens is None
    assert entry.completion_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadError])
async def test_auto_title_raw_transport_failure_settles_without_replacing_default_title(monkeypatch, error_type) -> None:
    async def failing_provider(**_kwargs: object) -> object:
        raise error_type("private upstream response")

    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)
    monkeypatch.setattr(litellm, "acompletion", failing_provider)
    service = _TitleService()
    await call_auto_title(
        service=service,
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )

    assert service.updates == []
    assert counter.calls == [(1, {"exception_class": "TransportError"}, None)]
    assert len(service.usage) == 1
    entry = service.usage[0][2][0]
    assert entry.call_id == service.attempts[0].attempt_id
    assert entry.prompt_tokens is None
    assert entry.completion_tokens is None


@pytest.mark.asyncio
async def test_auto_title_first_party_http_error_after_response_is_not_provider_weather(monkeypatch) -> None:
    class _BrokenResponse:
        @property
        def choices(self) -> object:
            raise httpx.ConnectError("local adapter bug")

    async def response_from_provider(**_kwargs: object) -> object:
        return _BrokenResponse()

    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)
    monkeypatch.setattr(litellm, "acompletion", response_from_provider)
    service = _TitleService()
    with pytest.raises(httpx.ConnectError, match="local adapter bug"):
        await call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    assert counter.calls == []
    assert len(service.usage) == 1


@pytest.mark.asyncio
async def test_auto_title_provider_failure_does_not_hide_failed_terminal_settlement(monkeypatch) -> None:
    provider_failure = httpx.ConnectError("private upstream response")
    settlement_failure = RuntimeError("ledger unavailable")

    async def failing_provider(**_kwargs: object) -> object:
        raise provider_failure

    class _FailingSettlementService(_TitleService):
        async def before_settlement(self) -> None:
            raise settlement_failure

    monkeypatch.setattr(litellm, "acompletion", failing_provider)
    service = _FailingSettlementService()
    with pytest.raises(ExceptionGroup) as caught:
        await call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    provider, settlement = caught.value.exceptions
    assert type(provider) is _auto_title._AutoTitleProviderTransportError
    assert provider.__cause__ is provider_failure
    assert type(settlement) is ComposerOwnedSettlementFailure
    assert settlement.__cause__ is settlement_failure
    assert service.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
async def test_auto_title_title_write_failure_propagates(monkeypatch, error_type) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _completion_response(**_kwargs: object) -> object:
        return _completion("Useful Pipeline")

    write_failure = error_type("database unavailable")

    class _FailingService(_TitleService):
        async def before_title_write(self, session_id, title, context) -> None:
            raise write_failure

    monkeypatch.setattr(litellm, "acompletion", _completion_response)

    service = _FailingService()
    with pytest.raises(ComposerOwnedSettlementFailure) as caught:
        await call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )

    assert caught.value.__cause__ is write_failure
    assert service.updates == []
    assert len(service.usage) == 1
    assert counter.calls == []


@pytest.mark.asyncio
async def test_auto_title_cancellation_propagates_after_accounting(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)
    cancellation = asyncio.CancelledError()

    async def cancelled_provider(**kwargs: object) -> object:
        raise cancellation

    monkeypatch.setattr(litellm, "acompletion", cancelled_provider)
    with pytest.raises(asyncio.CancelledError) as caught:
        await call_auto_title(
            service=_TitleService(),
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    assert caught.value is cancellation
    assert counter.calls == [(1, {"exception_class": "CancelledError"}, None)]


@pytest.mark.asyncio
async def test_auto_title_repeated_cancellation_finishes_settlement_and_preserves_original(monkeypatch) -> None:
    original = asyncio.CancelledError("provider cancellation")
    settlement_started = asyncio.Event()
    release_settlement = asyncio.Event()

    async def cancelled_provider(**_kwargs: object) -> object:
        raise original

    class _BlockingSettlementService(_TitleService):
        async def before_settlement(self) -> None:
            settlement_started.set()
            await release_settlement.wait()

    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", _FakeCounter())
    monkeypatch.setattr(litellm, "acompletion", cancelled_provider)
    service = _BlockingSettlementService()
    task = asyncio.create_task(
        call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    )
    await asyncio.wait_for(settlement_started.wait(), timeout=1)
    task.cancel()
    release_settlement.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await asyncio.wait_for(task, timeout=1)
    first, repeated = caught.value.exceptions
    assert first is original
    assert type(repeated) is asyncio.CancelledError and repeated.args == ()
    assert len(service.usage) == 1
    assert service.usage[0][2][0].call_id == service.attempts[0].attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement_failure", [RuntimeError("write failed"), AuditIntegrityError("audit write failed")])
async def test_auto_title_provider_cancellation_preserves_failure_priority_during_settlement(
    monkeypatch, settlement_failure: Exception
) -> None:
    original = asyncio.CancelledError("provider cancellation")
    settlement_started = asyncio.Event()
    release_settlement = asyncio.Event()

    async def cancelled_provider(**_kwargs: object) -> object:
        raise original

    class _FailingSettlementService(_TitleService):
        async def before_settlement(self) -> None:
            settlement_started.set()
            await release_settlement.wait()
            raise settlement_failure

    monkeypatch.setattr(litellm, "acompletion", cancelled_provider)
    service = _FailingSettlementService()
    task = asyncio.create_task(
        call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    )
    await asyncio.wait_for(settlement_started.wait(), timeout=1)
    task.cancel("second cancellation")
    release_settlement.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await asyncio.wait_for(task, timeout=1)
    first, settlement_outcomes = caught.value.exceptions
    assert first is original
    assert type(settlement_outcomes) is BaseExceptionGroup
    retained_failure, repeated = settlement_outcomes.exceptions
    if isinstance(settlement_failure, AuditIntegrityError):
        assert retained_failure is settlement_failure
    else:
        assert type(retained_failure) is ComposerOwnedSettlementFailure
        assert retained_failure.__cause__ is settlement_failure
    assert type(repeated) is asyncio.CancelledError and repeated.args == ("second cancellation",)
    assert service.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cancelled", [False, True])
async def test_auto_title_child_settlement_self_cancellation_fails_closed(monkeypatch, provider_cancelled: bool) -> None:
    original = asyncio.CancelledError("provider cancellation")
    child_cancellation = asyncio.CancelledError("settlement stopped itself")

    async def provider(**_kwargs: object) -> object:
        if provider_cancelled:
            raise original
        return _completion("Useful Pipeline")

    class _SelfCancellingSettlementService(_TitleService):
        async def before_settlement(self) -> None:
            raise child_cancellation

    monkeypatch.setattr(litellm, "acompletion", provider)
    service = _SelfCancellingSettlementService()
    with pytest.raises(BaseExceptionGroup) as caught:
        await call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    if provider_cancelled:
        first, settlement_outcomes = caught.value.exceptions
        assert first is original
        assert type(settlement_outcomes) is BaseExceptionGroup
    else:
        settlement_outcomes = caught.value
    integrity, retained_child, shield_cancellation = settlement_outcomes.exceptions
    assert type(integrity) is AuditIntegrityError
    assert str(integrity) == "Required provider child lacks an actual delivered outcome"
    assert integrity.__cause__ is child_cancellation
    assert retained_child is child_cancellation
    assert type(shield_cancellation) is asyncio.CancelledError and shield_cancellation.args == ()
    assert service.usage == []


@pytest.mark.asyncio
async def test_auto_title_cancellation_during_success_settlement_finishes_accounting(monkeypatch) -> None:
    settlement_started = asyncio.Event()
    release_settlement = asyncio.Event()

    async def successful_provider(**_kwargs: object) -> object:
        return _completion("Useful Pipeline")

    class _BlockingSettlementService(_TitleService):
        async def before_settlement(self) -> None:
            settlement_started.set()
            await release_settlement.wait()

    monkeypatch.setattr(litellm, "acompletion", successful_provider)
    service = _BlockingSettlementService()
    task = asyncio.create_task(
        call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    )
    await asyncio.wait_for(settlement_started.wait(), timeout=1)
    task.cancel()
    release_settlement.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert len(service.usage) == 1
    assert service.usage[0][2][0].call_id == service.attempts[0].attempt_id
    assert service.updates == []


@pytest.mark.asyncio
async def test_auto_title_cancellation_during_admission_closes_undispatched_attempt(monkeypatch) -> None:
    admission_started = asyncio.Event()
    release_admission = asyncio.Event()
    closure_started = asyncio.Event()
    release_closure = asyncio.Event()
    provider_calls = 0

    async def provider(**_kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return _completion("Useful Pipeline")

    class _BlockingAdmissionService(_TitleService):
        async def after_admission(self, attempt: ProviderAttempt) -> None:
            admission_started.set()
            await release_admission.wait()

        async def before_undispatched_cancellation(self, attempt_id: str, context: SessionOperationContext) -> None:
            assert context is self.last_context
            closure_started.set()
            await release_closure.wait()

    monkeypatch.setattr(litellm, "acompletion", provider)
    service = _BlockingAdmissionService()
    task = asyncio.create_task(
        call_auto_title(
            service=service,
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    )
    await asyncio.wait_for(admission_started.wait(), timeout=1)
    task.cancel("admission cancellation")
    release_admission.set()
    await asyncio.wait_for(closure_started.wait(), timeout=1)
    task.cancel("second cancellation")
    release_closure.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await asyncio.wait_for(task, timeout=1)
    first, repeated = caught.value.exceptions
    assert type(first) is asyncio.CancelledError and first.args == ("admission cancellation",)
    assert type(repeated) is asyncio.CancelledError and repeated.args == ("second cancellation",)
    assert service.cancelled_attempts == [service.attempts[0].attempt_id]
    assert provider_calls == 0
    assert len(service.usage) == 1
    entry = service.usage[0][2][0]
    assert entry.call_id == service.attempts[0].attempt_id
    assert (entry.prompt_tokens, entry.completion_tokens) == (0, 0)


@pytest.mark.asyncio
async def test_auto_title_cancellation_wins_over_concurrent_admission_refusal(monkeypatch) -> None:
    admission_started = asyncio.Event()
    release_admission = asyncio.Event()
    provider_calls = 0
    refusal = ChargeableAdmissionRefused(
        ChargeableAdmissionDecision(
            refusal_reason=AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
            evidence=AdmissionPolicyEvidence(
                identity_policy_id="test-policy", quota_disposition=QuotaDisposition.ACCOUNTING_UNAVAILABLE, secret_wiring_hash="a" * 64
            ),
        )
    )

    async def provider(**_kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return _completion("Useful Pipeline")

    class _BlockingRefusalService(_TitleService):
        async def before_admission(self) -> None:
            admission_started.set()
            await release_admission.wait()
            raise refusal

    monkeypatch.setattr(litellm, "acompletion", provider)
    task = asyncio.create_task(
        call_auto_title(
            service=_BlockingRefusalService(),
            user_message="Build a CSV pipeline",
            model="openai/test",
            temperature=None,
            seed=None,
        )
    )
    await asyncio.wait_for(admission_started.wait(), timeout=1)
    task.cancel("caller cancellation")
    await asyncio.sleep(0)
    release_admission.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await asyncio.wait_for(task, timeout=1)
    retained_refusal, cancellation = caught.value.exceptions
    assert retained_refusal is refusal
    assert type(cancellation) is asyncio.CancelledError and cancellation.args == ("caller cancellation",)
    assert provider_calls == 0


# ── Task I1: the auto-title token-ledger adapter ──────────────────────────


@pytest.mark.asyncio
async def test_auto_title_charges_a_returned_completion_even_when_the_title_is_rejected(monkeypatch) -> None:
    """R14 (sso-design.md:1161): a completion the gate discards was still spent."""
    response = ModelResponse(
        model="openai/returned",
        choices=[{"index": 0, "message": {"role": "assistant", "content": _LIVE_LEAK_COMPLETION}}],
        usage={"prompt_tokens": 21, "completion_tokens": 4, "total_tokens": 25},
    )
    service, _failed, _rejected = await _run_auto_title(monkeypatch, response)
    assert service.updates == []
    assert service.usage == [
        (
            "auto_title",
            None,
            (
                TokenUsageEntry(
                    model="openai/returned",
                    prompt_tokens=21,
                    completion_tokens=4,
                    cached_prompt_tokens=None,
                    reasoning_tokens=None,
                    recorded_at=_DISPATCHED_AT,
                    call_id=service.attempts[0].attempt_id,
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_auto_title_charges_an_unreported_completion_as_unknown_never_zero(monkeypatch) -> None:
    service, _failed, _rejected = await _run_auto_title(monkeypatch, _completion_without_finish("Useful Pipeline Title"))
    assert service.updates != []
    assert service.usage == [
        (
            "auto_title",
            None,
            (
                TokenUsageEntry(
                    model="openai/test",
                    prompt_tokens=None,
                    completion_tokens=None,
                    cached_prompt_tokens=None,
                    reasoning_tokens=None,
                    recorded_at=_DISPATCHED_AT,
                    call_id=service.attempts[0].attempt_id,
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_auto_title_provider_timeout_charges_unknown_usage(monkeypatch) -> None:
    async def _raise_timeout(**_kwargs: object) -> object:
        raise TimeoutError("title generation timed out")

    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", _FakeCounter())
    monkeypatch.setattr(litellm, "acompletion", _raise_timeout)
    service = _TitleService()
    await call_auto_title(
        service=service,
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )
    assert service.usage == [
        (
            "auto_title",
            None,
            (
                TokenUsageEntry(
                    model="openai/test",
                    prompt_tokens=None,
                    completion_tokens=None,
                    cached_prompt_tokens=None,
                    reasoning_tokens=None,
                    recorded_at=_DISPATCHED_AT,
                    call_id=service.attempts[0].attempt_id,
                ),
            ),
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [BadGatewayError, ServiceUnavailableError, LiteLLMTimeout])
async def test_auto_title_gateway_failure_settles_unknown_usage_without_title(monkeypatch, error_type) -> None:
    error = error_type(message="private upstream response", llm_provider="test", model="test/model")

    async def failing_provider(**_kwargs: object) -> object:
        raise error

    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)
    monkeypatch.setattr(litellm, "acompletion", failing_provider)
    service = _TitleService()
    await call_auto_title(
        service=service,
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
    )
    assert service.updates == []
    assert counter.calls == [(1, {"exception_class": error_type.__name__}, None)]
    assert len(service.usage) == 1
    entry = service.usage[0][2][0]
    assert entry.call_id == service.attempts[0].attempt_id
    assert entry.prompt_tokens is None
    assert entry.completion_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [False, True])
async def test_auto_title_malformed_response_preserves_reported_or_unknown_usage(monkeypatch, reported: bool) -> None:
    """Malformed response does not prove no billable work happened."""
    response = (
        ModelResponse(choices=[], usage={"prompt_tokens": 9, "completion_tokens": 0, "total_tokens": 9})
        if reported
        else ModelResponse(choices=[])
    )
    service, failed, _rejected = await _run_auto_title(monkeypatch, response)
    assert failed.calls == [(1, {"exception_class": "MalformedResponseError"}, None)]
    expected = TokenUsageEntry(
        model="openai/test",
        prompt_tokens=9 if reported else None,
        completion_tokens=0 if reported else None,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        recorded_at=_DISPATCHED_AT,
        call_id=service.attempts[0].attempt_id,
    )
    assert service.usage == [("auto_title", None, (expected,))]

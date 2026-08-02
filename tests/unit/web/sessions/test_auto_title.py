from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from litellm import ModelResponse

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.telemetry import _FakeCounter


class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []

    async def update_session_title(
        self,
        session_id: object,
        title: str,
        *,
        session_operation_context: SessionOperationContext,
    ) -> None:
        del session_operation_context
        self.updates.append((session_id, title))


def _compose_context(session_id: object) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=str(session_id),
            operation_id="auto-title-operation",
            lease_token="auto-title-token",
            operation_epoch=2,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


_TEST_CONTEXT = _compose_context(uuid4())


def _completion(content: str | None) -> ModelResponse:
    return ModelResponse(choices=[{"index": 0, "message": {"role": "assistant", "content": content}}])


@pytest.mark.asyncio
async def test_auto_title_threads_exact_compose_context_to_title_write(monkeypatch) -> None:
    session_id = uuid4()
    context = _compose_context(session_id)
    observed: list[tuple[object, str, SessionOperationContext]] = []

    class _FencedTitleService:
        async def update_session_title(
            self,
            session_id: object,
            title: str,
            *,
            session_operation_context: SessionOperationContext,
        ) -> None:
            observed.append((session_id, title, session_operation_context))

    async def _successful_completion(**_kwargs: object) -> object:
        return _completion("Fenced title")

    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _successful_completion)

    await _auto_title.maybe_auto_title_session(
        service=_FencedTitleService(),  # type: ignore[arg-type]
        session_id=session_id,
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
        session_operation_context=context,
    )

    assert observed == [(session_id, "Fenced title", context)]


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
        session_operation_context=_TEST_CONTEXT,
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
        session_operation_context=_TEST_CONTEXT,
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
        session_operation_context=_TEST_CONTEXT,
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
        session_operation_context=_TEST_CONTEXT,
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
            session_operation_context=_TEST_CONTEXT,
        )

    assert counter.calls == []


@pytest.mark.asyncio
async def test_auto_title_title_write_failure_propagates(monkeypatch) -> None:
    counter = _FakeCounter()
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", counter)

    async def _completion_response(**_kwargs: object) -> object:
        return _completion("Useful Pipeline")

    class _FailingService(_TitleService):
        async def update_session_title(
            self,
            session_id: object,
            title: str,
            *,
            session_operation_context: SessionOperationContext,
        ) -> None:
            del session_id, title, session_operation_context
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
            session_operation_context=_TEST_CONTEXT,
        )

    assert counter.calls == []

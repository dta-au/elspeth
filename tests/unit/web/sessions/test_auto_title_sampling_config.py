"""Auto-title uses caller-threaded composer sampling."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import elspeth.web.sessions._auto_title as at
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind

_TEST_CONTEXT = SessionOperationContext(
    fence=SessionOperationFence(
        session_id="auto-title-sampling-session",
        operation_id="auto-title-sampling-operation",
        lease_token="auto-title-sampling-token",
        operation_epoch=2,
    ),
    operation_kind=SessionOperationKind.COMPOSE,
)


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


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.asyncio
async def test_auto_title_omits_sampling_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(at, "_litellm_acompletion", fake_acompletion)
    service = _TitleService()

    await at.maybe_auto_title_session(
        service=service,
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="gpt-5",
        temperature=None,
        seed=None,
        session_operation_context=_TEST_CONTEXT,
    )

    assert "temperature" not in captured
    assert "seed" not in captured
    assert service.updates[0][1] == "My Title"


@pytest.mark.asyncio
async def test_auto_title_sends_configured_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(at, "_litellm_acompletion", fake_acompletion)

    await at.maybe_auto_title_session(
        service=_TitleService(),
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="gpt-4o",
        temperature=0.0,
        seed=42,
        session_operation_context=_TEST_CONTEXT,
    )

    assert captured["temperature"] == 0.0
    assert captured["seed"] == 42

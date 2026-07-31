"""Auto-title threads the PRIMARY-role endpoint affordance (Phase 3 Task 2).

Auto-title always uses the primary composer role (see
sessions/routes/messages.py:272) — there is no advisor parameter on
``maybe_auto_title_session`` at all, so the role asymmetry is structural.
Mirrors the structure of test_auto_title_sampling_config.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import elspeth.web.sessions._auto_title as at

_SENTINEL_CREDENTIAL = "sk-auto-title-endpoint-affordance-sentinel"  # secret-scan: allow-this-line


class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []

    async def update_session_title(self, session_id: object, title: str) -> None:
        self.updates.append((session_id, title))


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.asyncio
async def test_auto_title_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(at, "_litellm_acompletion", fake_acompletion)

    await at.maybe_auto_title_session(
        service=_TitleService(),
        session_id=uuid4(),
        user_message="Build a CSV pipeline",
        model="gpt-5",
        temperature=None,
        seed=None,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


@pytest.mark.asyncio
async def test_auto_title_sends_configured_primary_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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
        temperature=None,
        seed=None,
        api_base="https://primary-gateway.example.test/v1",
        api_key=_SENTINEL_CREDENTIAL,
    )

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL

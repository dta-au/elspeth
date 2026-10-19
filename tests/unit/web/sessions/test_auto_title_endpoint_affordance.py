"""Auto-title threads the primary Composer endpoint through real custody."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from tests.helpers.auto_title import RecordingTitleService, call_auto_title

_SENTINEL_CREDENTIAL = "sk-auto-title-endpoint-affordance-sentinel"  # secret-scan: allow-this-line


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.asyncio
async def test_auto_title_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    service = RecordingTitleService()
    await call_auto_title(
        service=service,
        user_message="Build a CSV pipeline",
        model="gpt-5",
        temperature=None,
        seed=None,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured
    assert service.updates == [(service.session_id, "My Title")]


@pytest.mark.asyncio
async def test_auto_title_sends_configured_primary_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    service = RecordingTitleService()
    await call_auto_title(
        service=service,
        user_message="Build a CSV pipeline",
        model="gpt-4o",
        temperature=None,
        seed=None,
        api_base="https://primary-gateway.example.test/v1",
        api_key=_SENTINEL_CREDENTIAL,
    )

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL
    assert service.updates == [(service.session_id, "My Title")]

"""Auto-title uses caller-threaded sampling through real fenced custody."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from tests.helpers.auto_title import RecordingTitleService, call_auto_title


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.asyncio
async def test_auto_title_omits_sampling_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert "temperature" not in captured
    assert "seed" not in captured
    assert service.updates == [(service.session_id, "My Title")]


@pytest.mark.asyncio
async def test_auto_title_sends_configured_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
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
        temperature=0.0,
        seed=42,
    )

    assert captured["temperature"] == 0.0
    assert captured["seed"] == 42
    assert service.updates == [(service.session_id, "My Title")]

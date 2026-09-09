"""The composer battery's currency (spec Decision 8) rests on two server facts: the advisor call carries no
``tools`` (so its audit row has a null ``tools_spec_hash``) and runs on ``composer_advisor_model`` (so the
scorer's bucket-by-model is sound). ``composer_advisor_model != composer_model`` is already pinned by
tests/unit/web/test_config.py ("composer_advisor_model must differ from composer_model")."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import elspeth.web.composer.service as svc
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings


def _service(tmp_path: Path) -> ComposerServiceImpl:
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="anthropic/claude-sonnet-5",
        composer_advisor_model="anthropic/claude-opus-4-8",
    )
    return ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        message = type("Message", (), {"tool_calls": None, "content": "advice"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)
    return captured


@pytest.mark.asyncio
async def test_advisor_call_sends_no_tools_and_uses_the_advisor_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = _capture(monkeypatch)
    await _service(tmp_path)._call_advisor_with_audit(
        {"trigger": "reactive", "problem_summary": "stuck", "recent_errors": [], "attempted_actions": []}, recorder=None
    )
    assert "tools" not in captured and captured["model"] == "anthropic/claude-opus-4-8"

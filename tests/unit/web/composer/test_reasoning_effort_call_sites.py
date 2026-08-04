"""Reasoning-effort wiring at the composer's non-planner call sites (elspeth-dc459d438e).

Mirrors test_endpoint_affordance.py: same fixtures, same fake-response
shape, same call sites (_call_llm, _call_text_llm,
_call_advisor_with_audit). Planner phase mapping is covered in
test_pipeline_planner.py; auto-title exclusion is proven by
tests/unit/web/sessions/test_auto_title.py remaining untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import elspeth.web.composer.service as svc
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings


def _settings(data_dir: Path, **overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": data_dir,
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    values.update(overrides)
    return WebSettings(**values)


def _service(tmp_path: Path, **settings_overrides: Any) -> ComposerServiceImpl:
    return ComposerServiceImpl.for_trained_operator(
        catalog=MagicMock(spec=CatalogService),
        settings=_settings(tmp_path, **settings_overrides),
    )


def _response(content: str = "reply") -> Any:
    message = type("Message", (), {"tool_calls": None, "content": content})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response()

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)
    return captured


@pytest.mark.asyncio
async def test_tool_loop_call_carries_the_discovery_knob(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = _capture(monkeypatch)
    await _service(tmp_path)._call_llm([{"role": "user", "content": "hi"}], tools=[])
    assert captured["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_text_call_carries_the_discovery_knob(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = _capture(monkeypatch)
    await _service(tmp_path, composer_discovery_reasoning_effort="medium")._call_text_llm([{"role": "user", "content": "hi"}])
    assert captured["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_none_opt_out_leaves_calls_unhinted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = _capture(monkeypatch)
    await _service(tmp_path, composer_discovery_reasoning_effort="none")._call_llm([{"role": "user", "content": "hi"}], tools=[])
    assert "reasoning_effort" not in captured
    assert "reasoning" not in captured


@pytest.mark.asyncio
async def test_openrouter_primary_model_gets_the_native_reasoning_object(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = _capture(monkeypatch)
    await _service(tmp_path, composer_model="openrouter/anthropic/claude-sonnet-5")._call_llm([{"role": "user", "content": "hi"}], tools=[])
    assert captured["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advisor_model", "expected_key", "expected_value"),
    [
        ("openrouter/anthropic/claude-opus-4-8", "reasoning", {"effort": "medium"}),
        ("bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0", "reasoning_effort", "medium"),
    ],
    ids=["openrouter", "bedrock"],
)
async def test_advisor_call_carries_the_advisor_knob(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    advisor_model: str,
    expected_key: str,
    expected_value: object,
) -> None:
    captured = _capture(monkeypatch)
    await _service(tmp_path, composer_advisor_model=advisor_model)._call_advisor_with_audit(
        {
            "trigger": "reactive",
            "problem_summary": "stuck",
            "recent_errors": [],
            "attempted_actions": [],
        },
        recorder=None,
    )
    assert captured[expected_key] == expected_value

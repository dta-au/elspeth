"""Phase 3 Task 2: Composer's settable OpenAI-compatible endpoint.

Per-role affordance (primary + advisor). Mirrors the structure of
test_llm_sampling_config.py: same fixtures, same fake-response shape, same
call sites (_call_llm, _call_text_llm, _call_advisor_with_audit). The
no-regression guarantee (byte-identical kwargs when unset) is asserted, not
assumed — every "omits" test below checks BOTH keys are absent, not just that
the call succeeded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from litellm.exceptions import APIError as LiteLLMAPIError

import elspeth.web.composer.service as svc
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings

_SENTINEL_CREDENTIAL = "sk-endpoint-affordance-sentinel-do-not-leak"  # secret-scan: allow-this-line


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


# --- _call_llm (primary role) -----------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response()

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(tmp_path)._call_llm([{"role": "user", "content": "hi"}], [])

    assert "api_base" not in captured
    assert "api_key" not in captured
    # Byte-identical no-regression guarantee: the full kwargs dict is exactly
    # what pre-affordance code sent for this call, no more, no less.
    assert captured == {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}], "tools": []}


@pytest.mark.asyncio
async def test_call_llm_sends_configured_primary_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response()

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(
        tmp_path,
        composer_endpoint_base_url="https://primary-gateway.example.test/v1",
        composer_endpoint_api_key=_SENTINEL_CREDENTIAL,
    )._call_llm([{"role": "user", "content": "hi"}], [])

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


@pytest.mark.asyncio
async def test_call_llm_does_not_use_advisor_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Role asymmetry: the primary call must never pick up the advisor's endpoint."""
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response()

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(
        tmp_path,
        composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
        composer_advisor_endpoint_api_key="advisor-only-secret",
    )._call_llm([{"role": "user", "content": "hi"}], [])

    assert "api_base" not in captured
    assert "api_key" not in captured


# --- _call_text_llm (primary role) ------------------------------------------


@pytest.mark.asyncio
async def test_text_llm_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response("text")

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(tmp_path)._call_text_llm([{"role": "user", "content": "hi"}])

    assert "api_base" not in captured
    assert "api_key" not in captured
    assert captured == {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_text_llm_sends_configured_primary_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response("text")

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(
        tmp_path,
        composer_endpoint_base_url="http://127.0.0.1:8787/v1",
        composer_endpoint_api_key=_SENTINEL_CREDENTIAL,
    )._call_text_llm([{"role": "user", "content": "hi"}])

    assert captured["api_base"] == "http://127.0.0.1:8787/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


# --- _call_advisor_with_audit (advisor role) --------------------------------


@pytest.mark.asyncio
async def test_advisor_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response("advice")

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(tmp_path)._call_advisor_with_audit(
        {
            "trigger": "reactive",
            "problem_summary": "stuck",
            "recent_errors": [],
            "attempted_actions": [],
        },
        recorder=None,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


@pytest.mark.asyncio
async def test_advisor_sends_configured_advisor_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response("advice")

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(
        tmp_path,
        composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
        composer_advisor_endpoint_api_key=_SENTINEL_CREDENTIAL,
    )._call_advisor_with_audit(
        {
            "trigger": "reactive",
            "problem_summary": "stuck",
            "recent_errors": [],
            "attempted_actions": [],
        },
        recorder=None,
    )

    assert captured["api_base"] == "https://advisor-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL


@pytest.mark.asyncio
async def test_advisor_does_not_use_primary_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Role asymmetry: the advisor call must never pick up the primary's endpoint."""
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _response("advice")

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    await _service(
        tmp_path,
        composer_endpoint_base_url="https://primary-gateway.example.test/v1",
        composer_endpoint_api_key="primary-only-secret",
    )._call_advisor_with_audit(
        {
            "trigger": "reactive",
            "problem_summary": "stuck",
            "recent_errors": [],
            "attempted_actions": [],
        },
        recorder=None,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


# --- Credential never leaks into logs, exceptions, or the audit record -----


@pytest.mark.asyncio
async def test_advisor_credential_never_appears_in_audit_record_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drive a FAILURE path (not just success) — _call_advisor_with_audit
    records a ComposerLLMCall in the ``finally`` block on every status,
    so this is where a credential leak into the audit trail would show up.
    """

    async def fake_acompletion(**kwargs: Any) -> Any:
        raise LiteLLMAPIError(
            status_code=500,
            message=f"upstream failure near api_key={kwargs.get('api_key')}",
            llm_provider="test",
            model=kwargs["model"],
        )

    monkeypatch.setattr(svc, "_litellm_acompletion", fake_acompletion)

    recorder = BufferingRecorder()
    with pytest.raises(LiteLLMAPIError) as excinfo:
        await _service(
            tmp_path,
            composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
            composer_advisor_endpoint_api_key=_SENTINEL_CREDENTIAL,
        )._call_advisor_with_audit(
            {
                "trigger": "reactive",
                "problem_summary": "stuck",
                "recent_errors": [],
                "attempted_actions": [],
            },
            recorder=recorder,
        )

    # The audit record's error_class/error_message are class-name-only by
    # design (see _call_advisor_with_audit's except clauses) — the sentinel
    # must not appear anywhere in the recorded ComposerLLMCall.
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    for field_value in (call.error_class, call.error_message, call.model_requested, call.model_returned):
        assert _SENTINEL_CREDENTIAL not in str(field_value)
    # The upstream provider exception's own message DOES carry the sentinel
    # in this synthetic test (a real provider would never echo the bearer
    # back) — assert our code doesn't launder it anywhere further: it never
    # reaches error_class (always type(exc).__name__) or error_message
    # (always "APIError" for this branch), which the loop above just proved.
    assert excinfo.value.status_code == 500

"""Boot probe for operator-set composer sampling config."""

from __future__ import annotations

import httpx
import pytest

import elspeth.web.composer.boot_probe as bp


@pytest.mark.asyncio
async def test_probe_raises_boot_config_error_on_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.exceptions import BadRequestError

    async def fake_acompletion(**_kwargs: object) -> object:
        raise BadRequestError(
            message="Invalid value for 'temperature'.",
            model="gpt-5",
            llm_provider="openai",
        )

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    with pytest.raises(bp.ComposerBootConfigError, match="gpt-5"):
        await bp.probe_composer_config(model="gpt-5", temperature=0.0, seed=None)


@pytest.mark.asyncio
async def test_probe_fatal_on_seed_bad_request_without_phrase_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.exceptions import BadRequestError

    async def fake_acompletion(**_kwargs: object) -> object:
        raise BadRequestError(
            message="Invalid value for 'seed'.",
            model="gpt-5",
            llm_provider="openai",
        )

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    with pytest.raises(bp.ComposerBootConfigError):
        await bp.probe_composer_config(model="gpt-5", temperature=None, seed=99999999999)


@pytest.mark.asyncio
async def test_probe_passes_through_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    assert await bp.probe_composer_config(model="gpt-4o", temperature=0.0, seed=42) is True


@pytest.mark.asyncio
async def test_bedrock_probe_uses_default_aws_chain_without_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs: object) -> object:
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)
    model = "bedrock/global.anthropic.claude-sonnet-4-6"

    assert await bp.probe_composer_config(model=model, temperature=None, seed=None) is True
    assert captured == [
        {
            "model": model,
            "messages": [{"role": "user", "content": "This is a composer boot-time configuration smoke test. Please reply with ok."}],
            "max_tokens": 16,
        }
    ]


@pytest.mark.asyncio
async def test_probe_is_graceful_on_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_kwargs: object) -> object:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    assert await bp.probe_composer_config(model="gpt-4o", temperature=0.0, seed=42) is False


@pytest.mark.asyncio
async def test_probe_is_graceful_on_litellm_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.exceptions import InternalServerError

    async def fake_acompletion(**_kwargs: object) -> object:
        raise InternalServerError(
            message="Missing credentials.",
            model="gpt-4o",
            llm_provider="openai",
        )

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    assert await bp.probe_composer_config(model="gpt-4o", temperature=0.0, seed=42) is False


@pytest.mark.asyncio
async def test_probe_propagates_programmer_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_kwargs: object) -> object:
        raise TypeError("signature drift")

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    with pytest.raises(TypeError, match="signature drift"):
        await bp.probe_composer_config(model="gpt-4o", temperature=0.0, seed=42)


# --- Endpoint affordance (Phase 3 Task 2) -----------------------------------
# The boot probe must hit the SAME endpoint the real calls will use for that
# role — a probe that silently validated against the provider default while
# the real traffic goes to a misconfigured custom endpoint would defeat the
# entire point of probing at boot.


@pytest.mark.asyncio
async def test_probe_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs: object) -> object:
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    assert await bp.probe_composer_config(model="gpt-4o", temperature=None, seed=None) is True

    assert "api_base" not in captured[0]
    assert "api_key" not in captured[0]
    assert captured[0] == {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "This is a composer boot-time configuration smoke test. Please reply with ok."}],
        "max_tokens": 16,
    }


@pytest.mark.asyncio
async def test_probe_sends_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs: object) -> object:
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    assert (
        await bp.probe_composer_config(
            model="gpt-4o",
            temperature=0.0,
            seed=42,
            api_base="https://gateway.example.test/v1",
            api_key="probe-bearer-token",  # secret-scan: allow-this-line
        )
        is True
    )

    assert captured[0]["api_base"] == "https://gateway.example.test/v1"
    assert captured[0]["api_key"] == "probe-bearer-token"  # secret-scan: allow-this-line

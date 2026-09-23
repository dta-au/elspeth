"""Boot probe for operator-set composer sampling config."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import elspeth.web.composer.boot_probe as bp

_CLEAN = '{"verdict":"CLEAN","category":"other","steps":[],"findings":"","note":null}'


@pytest.mark.asyncio
async def test_advisor_probe_uses_production_request_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.advisor_request import build_advisor_request_options

    calls: list[dict[str, object]] = []

    async def complete(*, on_provider_dispatch: object = None, **kwargs: object) -> object:
        assert on_provider_dispatch is None
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=_CLEAN, tool_calls=None))])

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    assert await bp.probe_composer_config(
        role="advisor",
        model="openrouter/anthropic/claude-sonnet-5",
        temperature=0.2,
        seed=42,
        max_tokens=8192,
        reasoning_effort="low",
        api_base="https://openrouter.ai/api/v1",
        api_key="test-token",
    )
    expected = build_advisor_request_options(
        model="openrouter/anthropic/claude-sonnet-5",
        temperature=0.2,
        seed=42,
        max_tokens=8192,
        reasoning_effort="low",
        api_base="https://openrouter.ai/api/v1",
        api_key="test-token",
        structured_output=True,
    )
    request = calls[0]
    messages = request.pop("messages")
    assert request == expected
    assert isinstance(messages, list)
    prompt = messages[0]["content"]
    assert all(field not in json.dumps(messages).lower() for field in ("verdict", "findings", "note", "steps", "category"))
    assert "reply with ok" in prompt.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "ok",
        f"```json\n{_CLEAN}\n```",
        "",
        '{"verdict":',
        '{"verdict":"FLAGGED","verdict":"CLEAN","category":"other","steps":[],"findings":"","note":null}',
        None,
        4,
    ],
    ids=["prose", "fenced-json", "empty", "truncated", "duplicate", "reasoning-only", "wrong-type"],
)
async def test_advisor_probe_rejects_nonconforming_content(monkeypatch: pytest.MonkeyPatch, content: object) -> None:
    async def complete(**_kwargs: object) -> object:
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    with pytest.raises(bp.ComposerBootConfigError, match=r"advisor.*probe-model.*structured-output"):
        await bp.probe_composer_config(role="advisor", model="probe-model", temperature=None, seed=None, max_tokens=4096)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(),
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices="invalid"),
        SimpleNamespace(choices=[SimpleNamespace()]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=_CLEAN, tool_calls=[object()]))]),
    ],
    ids=["missing-choices", "empty-choices", "wrong-choices", "missing-message", "tool-call"],
)
async def test_advisor_probe_rejects_malformed_provider_response(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    async def complete(**_kwargs: object) -> object:
        return response

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    with pytest.raises(bp.ComposerBootConfigError, match="structured-output"):
        await bp.probe_composer_config(role="advisor", model="probe-model", temperature=None, seed=None, max_tokens=4096)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "model", "temperature", "seed", "reasoning_effort", "expected_presence"),
    [
        ("planner", "probe-model", None, None, None, (False, False, False, False, False)),
        ("planner", "probe-model", 0.0, 7, "low", (True, True, False, False, False)),
        ("advisor", "probe-model", None, None, "low", (False, False, False, True, False)),
        ("advisor", "openrouter/probe-model", 0.0, 7, "low", (True, True, True, True, True)),
        ("advisor", "azure/probe-model", None, None, "low", (False, False, True, True, False)),
    ],
)
async def test_probe_rejection_reports_only_sent_option_presence(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    model: str,
    temperature: float | None,
    seed: int | None,
    reasoning_effort: str | None,
    expected_presence: tuple[bool, bool, bool, bool, bool],
) -> None:
    from litellm.exceptions import BadRequestError

    provider_error = BadRequestError(message="SENSITIVE_PROVIDER_TEXT", model=model, llm_provider="openai")

    async def complete(**_kwargs: object) -> object:
        raise provider_error

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    with pytest.raises(bp.ComposerBootConfigError) as caught:
        await bp.probe_composer_config(
            role=role,
            model=model,
            temperature=temperature,
            seed=seed,
            reasoning_effort=reasoning_effort,
            max_tokens=4096,
        )
    text = str(caught.value)
    assert "SENSITIVE_PROVIDER_TEXT" not in text
    assert text.startswith(f"composer {role} boot request rejected by {model}:")
    for field, present in zip(
        ("temperature", "seed", "reasoning_effort", "response_format", "provider_routing"), expected_presence, strict=True
    ):
        assert f"{field}_present={present}" in text
    assert caught.value.__cause__ is provider_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        _CLEAN,
        json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": "ok"}),
        json.dumps({"verdict": "CLEAN", "category": "other", "steps": ["step"], "findings": "", "note": None}),
        json.dumps({"verdict": "FLAGGED", "category": "other", "steps": [], "findings": " ", "note": None}),
    ],
    ids=["accepted-clean", "clean-note", "clean-steps", "flagged-empty"],
)
async def test_advisor_probe_accepts_schema_valid_without_requiring_checkpoint_semantics(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    async def complete(**_kwargs: object) -> object:
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    try:
        accepted = await bp.probe_composer_config(role="advisor", model="probe-model", temperature=None, seed=None, max_tokens=4096)
    except bp.ComposerBootConfigError:
        accepted = False
    assert accepted, "boot must accept schema-valid output even when checkpoint semantics reject it"


@pytest.mark.asyncio
async def test_advisor_probe_timeout_remains_nonfatal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def complete(**_kwargs: object) -> object:
        raise TimeoutError

    monkeypatch.setattr(bp, "_litellm_acompletion", complete)
    assert not await bp.probe_composer_config(role="advisor", model="probe-model", temperature=None, seed=None, max_tokens=4096)


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
        await bp.probe_composer_config(role="planner", model="gpt-5", temperature=0.0, seed=None)


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
        await bp.probe_composer_config(role="planner", model="gpt-5", temperature=None, seed=99999999999)


@pytest.mark.asyncio
async def test_probe_passes_through_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    assert await bp.probe_composer_config(role="planner", model="gpt-4o", temperature=0.0, seed=42) is True


@pytest.mark.asyncio
async def test_bedrock_probe_uses_default_aws_chain_without_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    async def fake_acompletion(*, on_provider_dispatch: object = None, **kwargs: object) -> object:
        assert on_provider_dispatch is None
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)
    model = "bedrock/global.anthropic.claude-sonnet-4-6"

    assert await bp.probe_composer_config(role="planner", model=model, temperature=None, seed=None) is True
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

    assert await bp.probe_composer_config(role="planner", model="gpt-4o", temperature=0.0, seed=42) is False


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

    assert await bp.probe_composer_config(role="planner", model="gpt-4o", temperature=0.0, seed=42) is False


@pytest.mark.asyncio
async def test_probe_propagates_programmer_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_kwargs: object) -> object:
        raise TypeError("signature drift")

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    with pytest.raises(TypeError, match="signature drift"):
        await bp.probe_composer_config(role="planner", model="gpt-4o", temperature=0.0, seed=42)


# --- Endpoint affordance (Phase 3 Task 2) -----------------------------------
# The boot probe must hit the SAME endpoint the real calls will use for that
# role — a probe that silently validated against the provider default while
# the real traffic goes to a misconfigured custom endpoint would defeat the
# entire point of probing at boot.


@pytest.mark.asyncio
async def test_probe_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []

    async def fake_acompletion(*, on_provider_dispatch: object = None, **kwargs: object) -> object:
        assert on_provider_dispatch is None
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(bp, "_litellm_acompletion", fake_acompletion)

    assert await bp.probe_composer_config(role="planner", model="gpt-4o", temperature=None, seed=None) is True

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
            role="planner",
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

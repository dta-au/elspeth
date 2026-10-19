"""Operator configuration for the dedicated Azure authoring guard."""

from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from elspeth.web.config import WebSettings, settings_from_env


def _required() -> dict[str, object]:
    return {
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }


def _guard() -> dict[str, object]:
    return {
        "adapter": "azure_prompt_shields",
        "endpoint": "https://guard.example.test",
        "subscription_key": "fixture-guard-credential",
        "timeout_seconds": 5.0,
    }


@pytest.fixture(autouse=True)
def isolated_web_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("ELSPETH_WEB__"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    for name, value in _required().items():
        if name == "shareable_link_signing_key":
            monkeypatch.setenv("ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY", "0" * 64)
        else:
            monkeypatch.setenv(f"ELSPETH_WEB__{name.upper()}", str(value))


def test_guard_fields_default_to_required_independent_unconfigured_mounts() -> None:
    assert {"composer_prompt_guard", "composer_advisor_prompt_guard", "composer_prompt_guard_mode"} <= WebSettings.model_fields.keys()
    settings = WebSettings.model_validate(_required())
    assert settings.composer_prompt_guard is None
    assert settings.composer_advisor_prompt_guard is None
    assert settings.composer_prompt_guard_mode == "required"


def test_guard_pointer_and_credential_do_not_inherit_model_configuration() -> None:
    settings = WebSettings.model_validate({**_required(), "composer_prompt_guard": _guard()})
    assert settings.composer_prompt_guard is not None
    assert settings.composer_prompt_guard.endpoint == "https://guard.example.test"
    assert settings.composer_prompt_guard.subscription_key.get_secret_value() == "fixture-guard-credential"
    assert settings.composer_advisor_prompt_guard is None
    assert "fixture-guard-credential" not in repr(settings)


@pytest.mark.parametrize("role_field", ["composer_prompt_guard", "composer_advisor_prompt_guard"])
def test_guard_environment_object_and_null_are_independent(role_field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env_name = f"ELSPETH_WEB__{role_field.upper()}"
    monkeypatch.setenv(env_name, json.dumps(_guard()))
    configured = settings_from_env().model_dump()
    assert configured[role_field] is not None
    monkeypatch.setenv(env_name, "null")
    assert settings_from_env().model_dump()[role_field] is None


@pytest.mark.parametrize("mode", ["required", "debug-when-unconfigured"])
def test_guard_mode_environment_closed_values(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE", mode)
    assert settings_from_env().composer_prompt_guard_mode == mode


@pytest.mark.parametrize("mode", ["", "debug", "disabled", "null", "false"])
def test_guard_mode_rejects_implicit_disable(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE", mode)
    with pytest.raises((ValidationError, RuntimeError)):
        settings_from_env()


@pytest.mark.parametrize(
    "changes",
    [
        {"adapter": "aws_bedrock"},
        {"endpoint": "http://guard.example.test"},
        {"endpoint": "https://user:password@guard.example.test"},
        {"endpoint": "https://guard.example.test/api"},
        {"endpoint": "https://guard.example.test?key=private"},
        {"endpoint": "https://guard.example.test#private"},
        {"endpoint": " https://guard.example.test"},
        {"endpoint": "https://guard.example.test:invalid"},
        {"endpoint": "https://guard.example.test\n"},
        {"subscription_key": ""},
        {"subscription_key": " "},
        {"subscription_key": "non-ascii-\u00e9"},
        {"subscription_key": "x" * 1025},
        {"subscription_key": "private\x00credential"},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
        {"unknown": "private"},
    ],
)
def test_guard_config_rejects_invalid_or_unsupported_operator_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WebSettings.model_validate({**_required(), "composer_prompt_guard": {**_guard(), **changes}})


def test_guard_environment_errors_never_echo_credential_or_provider_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ELSPETH_WEB__COMPOSER_PROMPT_GUARD",
        json.dumps({**_guard(), "subscription_key": "seeded-private-credential", "endpoint": "private-invalid-endpoint"}),
    )
    with pytest.raises(RuntimeError) as caught:
        settings_from_env()
    assert "seeded-private-credential" not in str(caught.value)
    assert "private-invalid-endpoint" not in str(caught.value)


def test_guard_environment_errors_do_not_echo_unknown_nested_key_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_PROMPT_GUARD", json.dumps({**_guard(), "seeded-private-key-name": "value"}))
    with pytest.raises(RuntimeError) as caught:
        settings_from_env()
    assert "seeded-private-key-name" not in str(caught.value)


@pytest.mark.parametrize("raw", ["[]", '"private"', "false", "invalid-private-json"])
def test_guard_environment_requires_object_or_explicit_null(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_PROMPT_GUARD", raw)
    with pytest.raises(RuntimeError) as caught:
        settings_from_env()
    assert "invalid-private-json" not in str(caught.value)


def test_existing_required_object_environment_still_rejects_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__PLUGIN_PREFERENCES", "null")
    with pytest.raises(RuntimeError, match="valid JSON object"):
        settings_from_env()

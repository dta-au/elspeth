"""Power Automate credentials always require HMAC audit fingerprints."""

from copy import deepcopy
from typing import Any

import pytest

from elspeth.core.config import (
    ElspethSettings,
    SecretFingerprintError,
    _fingerprint_config_for_audit,
    resolve_config,
    sanitize_node_config_for_audit,
)
from elspeth.core.security import secret_fingerprint


@pytest.fixture(params=["sas", "service_principal"])
def secret_options(request: pytest.FixtureRequest) -> dict[str, Any]:
    if request.param == "sas":
        auth = {"method": "sas", "trigger_url_secret": "https://flow.example.test/invoke?sig=recognizable-test-signature"}
    else:
        auth = {"method": "service_principal", "tenant_id": "test-tenant", "client_id": "test-client", "client_secret": "test-secret"}
    return {"allowed_origin": "https://flow.example.test", "auth": auth, "query": {"table": "orders"}}


def test_node_refuses_dev_raw_secrets_without_key(secret_options: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")

    with pytest.raises(SecretFingerprintError, match="ELSPETH_FINGERPRINT_KEY"):
        sanitize_node_config_for_audit(secret_options, plugin_name="power_automate")


@pytest.mark.parametrize("section", ["sources", "sinks"])
def test_settings_refuse_dev_raw_secrets_without_key(secret_options: dict[str, Any], section: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    config = {section: {"flow": {"plugin": "power_automate", "options": secret_options}}}

    with pytest.raises(SecretFingerprintError, match="ELSPETH_FINGERPRINT_KEY"):
        _fingerprint_config_for_audit(config)


@pytest.mark.parametrize("section", ["sources", "sinks"])
def test_resolve_config_refuses_dev_raw_secrets_without_key(
    secret_options: dict[str, Any], section: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    sources = {"primary": {"plugin": "csv", "on_success": "output", "options": {}}}
    sinks = {"output": {"plugin": "csv", "on_write_failure": "discard", "options": {}}}
    component = sources["primary"] if section == "sources" else sinks["output"]
    component["plugin"] = "power_automate"
    component["options"] = secret_options
    settings = ElspethSettings(sources=sources, sinks=sinks)

    with pytest.raises(SecretFingerprintError, match="ELSPETH_FINGERPRINT_KEY"):
        resolve_config(settings)


def test_safe_node_shape_is_detached_and_idempotent(secret_options: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "power-automate-redaction-test-key")
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    original = deepcopy(secret_options)
    secret_name = "trigger_url_secret" if secret_options["auth"]["method"] == "sas" else "client_secret"
    expected = deepcopy(original)
    expected["auth"][f"{secret_name}_fingerprint"] = secret_fingerprint(expected["auth"].pop(secret_name))

    safe = sanitize_node_config_for_audit(secret_options, plugin_name="power_automate")

    assert safe == expected
    assert secret_options == original
    assert safe is not secret_options
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    assert sanitize_node_config_for_audit(safe, plugin_name="power_automate") == expected
    secret_options["query"]["table"] = "mutated"
    assert safe == expected


@pytest.mark.parametrize("section", ["sources", "sinks"])
def test_settings_fingerprint_exactly_matches_node_config(
    secret_options: dict[str, Any], section: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "power-automate-redaction-test-key")
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    config = {section: {"flow": {"plugin": "power_automate", "options": secret_options}}}
    original = deepcopy(config)
    expected = sanitize_node_config_for_audit(secret_options, plugin_name="power_automate")

    safe = _fingerprint_config_for_audit(config)

    assert safe[section]["flow"]["options"] == expected
    assert config == original
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    assert _fingerprint_config_for_audit(safe) == safe


def test_managed_identity_needs_no_fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)
    options = {"auth": {"method": "managed_identity", "client_id": "user-assigned-identity"}}

    assert sanitize_node_config_for_audit(options, plugin_name="power_automate") == options
    config = {"sources": {"flow": {"plugin": "power_automate", "options": options}}}
    assert _fingerprint_config_for_audit(config) == config


def test_other_plugins_keep_existing_dev_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    options = {"auth": {"client_secret": "generic-plugin-development-secret"}}  # secret-scan: allow-this-line

    assert sanitize_node_config_for_audit(options, plugin_name="generic") == options
    config = {section: {"flow": {"plugin": "generic", "options": options}} for section in ("sources", "sinks")}
    assert _fingerprint_config_for_audit(config) == config

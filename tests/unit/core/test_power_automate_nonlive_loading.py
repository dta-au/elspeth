"""Offline Power Automate construction admits identity before credential effects."""

from copy import deepcopy

import pytest

from elspeth.contracts.enums import RunMode
from elspeth.core.canonical import stable_hash


def _safe_options():
    return {
        "auth": {"method": "sas_url", "trigger_url_secret_fingerprint": "a" * 64},
        "allowed_origin": "https://example.com",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }


def _archive():
    from elspeth.plugins.infrastructure.power_automate_nonlive import (
        ArchivedPowerAutomateOptions,
        ArchivedPowerAutomateSourceConfig,
        PowerAutomateArchive,
    )

    options = _safe_options()
    source = ArchivedPowerAutomateOptions(
        "source", "input", "old-run", options, stable_hash(options), ArchivedPowerAutomateSourceConfig.model_validate(options)
    )
    return PowerAutomateArchive("old-run", {"sources": {"input": {"plugin": "power_automate", "options": options}}}, {"input": source}, {})


def _raw():
    options = _safe_options()
    options["auth"] = {"method": "sas_url", "trigger_url_secret": "${UNSET_FLOW}"}
    return {"run_mode": "replay", "replay_from": "old-run", "sources": {"input": {"plugin": "power_automate", "options": options}}}


def test_projects_before_env_resolution(monkeypatch):
    from elspeth.plugins.infrastructure.power_automate_nonlive import project_power_automate_nonlive

    monkeypatch.delenv("UNSET_FLOW", raising=False)
    raw = _raw()
    projected, context = project_power_automate_nonlive(raw, _archive())
    assert projected["sources"]["input"]["options"] == _safe_options()
    assert raw == _raw()
    assert context.mode is RunMode.REPLAY
    assert context.source_credentials == {}


@pytest.mark.parametrize("credential", ["literal", "${UNSET_FLOW:-default}", "prefix${UNSET_FLOW}"])
def test_refuses_nonexact_secret_locator(credential):
    from elspeth.plugins.infrastructure.power_automate_nonlive import project_power_automate_nonlive

    raw = _raw()
    raw["sources"]["input"]["options"]["auth"]["trigger_url_secret"] = credential
    with pytest.raises(ValueError, match="credential_locator"):
        project_power_automate_nonlive(raw, _archive())


def test_effective_defaults_preserve_archived_authored_shape():
    from elspeth.plugins.infrastructure.power_automate_nonlive import project_power_automate_nonlive

    raw = _raw()
    raw["sources"]["input"]["options"]["page_size"] = 100
    projected, _ = project_power_automate_nonlive(raw, _archive())
    assert "page_size" not in projected["sources"]["input"]["options"]


@pytest.mark.parametrize(
    "change",
    [
        {"page_size": 7},
        {"max_pages": 7},
        {"max_rows": 7},
        {"timeout_seconds": 50},
        {"max_request_body_bytes": 2048},
        {"max_response_body_bytes": 2048},
        {"snapshot_for_resume": True},
        {"snapshot_id": "different"},
        {"query": {"region": "changed"}},
        {"allowed_origin": "https://example.org"},
        {"field_mapping": {"id": "record_id"}},
        {"on_validation_failure": "quarantine"},
        {"schema": {"mode": "flexible", "fields": ["id: str"]}},
    ],
)
def test_nonsecret_drift_refused(change):
    from elspeth.plugins.infrastructure.power_automate_nonlive import project_power_automate_nonlive

    raw = _raw()
    raw["sources"]["input"]["options"].update(change)
    with pytest.raises(ValueError, match="options_differ"):
        project_power_automate_nonlive(raw, _archive())


def test_verify_defers_only_source_credential(monkeypatch):
    from elspeth.plugins.infrastructure.power_automate_nonlive import project_power_automate_nonlive

    monkeypatch.delenv("UNSET_FLOW", raising=False)
    raw = _raw()
    raw["run_mode"] = "verify"
    _, context = project_power_automate_nonlive(raw, _archive())
    assert context.source_credentials["input"].env_name == "UNSET_FLOW"


def test_archived_auth_refuses_live_secret():
    from elspeth.plugins.infrastructure.power_automate_nonlive import ArchivedPowerAutomateSourceConfig

    options = deepcopy(_safe_options())
    options["auth"] = {"method": "sas_url", "trigger_url_secret": "raw"}
    with pytest.raises(ValueError):
        ArchivedPowerAutomateSourceConfig.model_validate(options)


def test_deferred_credential_checks_hmac_without_exception_context(monkeypatch):
    from elspeth.contracts.security import secret_fingerprint
    from elspeth.plugins.infrastructure.power_automate_nonlive import DeferredPowerAutomateCredential

    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-hmac-key")
    credential = DeferredPowerAutomateCredential("VERIFY_FLOW", secret_fingerprint("expected-value"))
    monkeypatch.setenv("VERIFY_FLOW", "expected-value")
    assert credential.resolve().get_secret_value() == "expected-value"
    monkeypatch.setenv("VERIFY_FLOW", "different-sensitive-value")
    with pytest.raises(ValueError, match="credential_refused") as failure:
        credential.resolve()
    assert failure.value.__context__ is None
    assert "different-sensitive-value" not in str(failure.value)
    monkeypatch.delenv("VERIFY_FLOW")
    with pytest.raises(ValueError, match="credential_refused") as missing:
        credential.resolve()
    assert missing.value.__context__ is None


def test_verify_sink_has_no_deferred_credentials():
    from elspeth.plugins.infrastructure.power_automate_nonlive import (
        ArchivedPowerAutomateOptions,
        ArchivedPowerAutomateSinkConfig,
        PowerAutomateArchive,
        project_power_automate_nonlive,
    )

    base = _archive()
    options = {
        "auth": {"method": "sas_url", "trigger_url_secret_fingerprint": "b" * 64},
        "allowed_origin": "https://example.com",
        "fields": ["id"],
        "schema": {"mode": "flexible", "fields": ["id: str"]},
    }
    sink = ArchivedPowerAutomateOptions(
        "sink", "output", "old-run", options, stable_hash(options), ArchivedPowerAutomateSinkConfig.model_validate(options)
    )
    archive = PowerAutomateArchive("old-run", base.settings, base.sources, {"output": sink})
    raw = _raw()
    raw["run_mode"] = "verify"
    sink_options = deepcopy(options)
    sink_options["auth"] = {"method": "sas_url", "trigger_url_secret": "${UNUSED_SINK}"}
    raw["sinks"] = {"output": {"plugin": "power_automate", "options": sink_options}}
    _, context = project_power_automate_nonlive(raw, archive)
    assert set(context.source_credentials) == {"input"}
    assert "output" not in context.source_credentials


@pytest.mark.parametrize("loader", ["dict", "yaml", "file"])
def test_standard_loader_requires_archive_before_env_expansion(monkeypatch, tmp_path, loader):
    import yaml

    import elspeth.config_loading as loading

    def forbidden(_):
        raise AssertionError("env expansion reached")

    monkeypatch.setattr(loading, "_expand_env_vars", forbidden)
    raw = _raw()
    with pytest.raises(ValueError, match="requires admitted archive context"):
        if loader == "dict":
            loading.load_settings_from_config_dict(raw, expand_env_vars=True)
        elif loader == "yaml":
            loading.load_settings_from_yaml_string(yaml.safe_dump(raw), expand_env_vars=True)
        else:
            path = tmp_path / "settings.yaml"
            path.write_text(yaml.safe_dump(raw))
            loading.load_settings(path)

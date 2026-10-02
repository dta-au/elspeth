from __future__ import annotations

import os
from dataclasses import replace

import pytest
from pydantic import ValidationError
from tests.unit.web.plugin_policy.test_compiler import _isolated_manager_with_llm_source, _settings

from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml
from elspeth.web.config import settings_from_env
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.plugin_policy.validation import validate_plugin_policy
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy, SecretWiringRule


def test_power_automate_origin_policy_defaults_to_deny() -> None:
    settings = _settings()
    assert settings.power_automate_allowed_origins == ()
    assert RuntimeWebPluginConfig.from_settings(settings).power_automate_allowed_origins == ()


def test_origin_policy_is_normalized_and_set_like_for_hashing() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(power_automate_allowed_origins=("https://FLOW.example:443", "https://second.example"))
    )
    assert runtime.power_automate_allowed_origins == ("https://flow.example", "https://second.example")
    manager = _isolated_manager_with_llm_source()
    first = compile_web_plugin_policy(registry=manager, settings=runtime)
    reordered = compile_web_plugin_policy(
        registry=manager, settings=replace(runtime, power_automate_allowed_origins=tuple(reversed(runtime.power_automate_allowed_origins)))
    )
    changed = compile_web_plugin_policy(registry=manager, settings=replace(runtime, power_automate_allowed_origins=()))
    assert first.power_automate_allowed_origins == runtime.power_automate_allowed_origins
    assert first.policy_hash == reordered.policy_hash
    assert first.policy_hash != changed.policy_hash


@pytest.mark.parametrize(
    "origin",
    [
        "http://flow.example",
        "https://*.example",
        "https://flow.example:444",
        "https://flow.example/path",
        "https://flow.example?sig=secret",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://[::1]",
    ],
)
def test_operator_origins_refuse_wildcards_paths_and_private_bypasses(origin: str) -> None:
    with pytest.raises(ValidationError):
        _settings(power_automate_allowed_origins=(origin,))


def test_power_automate_origins_decode_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Supply required settings explicitly; CI has no operator .env file.
    for name in tuple(os.environ):
        if name.startswith("ELSPETH_WEB__"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS", "4")
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS", "4")
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE", "20")
    monkeypatch.setenv("ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("ELSPETH_WEB__POWER_AUTOMATE_ALLOWED_ORIGINS", '["https://FLOW.example:443"]')
    assert settings_from_env().power_automate_allowed_origins == ("https://flow.example",)


class _NoCatalogRead:
    def get_schema(self, plugin_type: str, name: str) -> None:
        raise AssertionError("origin refusal must precede schema or secret resolution")


def _snapshot(origins: tuple[str, ...], *, policy_hash: str = "a" * 64) -> PluginAvailabilitySnapshot:
    return PluginAvailabilitySnapshot.create(
        policy_hash=policy_hash,
        principal_scope="local:test",
        available=frozenset({PluginId("source", "power_automate"), PluginId("sink", "power_automate")}),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
        power_automate_allowed_origins=origins,
    )


def _state(origin: object) -> CompositionState:
    options = {"allowed_origin": origin, "auth": {"method": "sas", "trigger_url_secret": {"secret_ref": "FLOW_URL"}}}
    return CompositionState(
        source=SourceSpec(plugin="power_automate", options=options, on_success="out", on_validation_failure="discard"),
        nodes=(),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="power_automate", options=options, on_write_failure="fail"),),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.mark.parametrize("origin", ["https://flow.example", "https://attacker.example", None, {"secret_ref": "FLOW_URL"}])
def test_both_source_and_sink_origins_are_checked_before_resolution(origin: object) -> None:
    state = _state(origin)
    result = validate_plugin_policy(state, snapshot=_snapshot(()), profile_registry=None, catalog=_NoCatalogRead())
    denied = [finding for finding in result.findings if finding.error_code == "power_automate_origin_not_allowed"]
    assert [(finding.component_type, finding.component_id) for finding in denied] == [("source", "source"), ("sink", "out")]
    assert result.executable_state == state


def test_allowed_origin_and_secret_wiring_are_independent() -> None:
    wiring = SecretWiringPolicy((SecretWiringRule("FLOW_URL", "source", "power_automate", "auth.trigger_url_secret"),))
    assert wiring.authorizes(secret_name="FLOW_URL", component_type="source", plugin="power_automate", option_key="auth.trigger_url_secret")
    refused = validate_plugin_policy(
        _state("https://flow.example"), snapshot=_snapshot(()), profile_registry=None, catalog=_NoCatalogRead()
    )
    assert any(finding.error_code == "power_automate_origin_not_allowed" for finding in refused.findings)
    assert not SecretWiringPolicy(()).authorizes(
        secret_name="FLOW_URL", component_type="source", plugin="power_automate", option_key="auth.trigger_url_secret"
    )


def test_origin_policy_changes_snapshot_authority() -> None:
    first = _snapshot(("https://flow.example",))
    changed = _snapshot(("https://second.example",))
    assert first.snapshot_hash != changed.snapshot_hash
    assert first.power_automate_allowed_origins == ("https://flow.example",)


class _SchemaCatalog:
    def get_schema(self, plugin_type: str, name: str) -> PluginSchemaInfo:
        return PluginSchemaInfo(name=name, plugin_type=plugin_type, description="test", json_schema={}, knob_schema={})

    def list_sources(self) -> list[PluginSummary]:
        return [PluginSummary(name="power_automate", plugin_type="source", description="test", config_fields=[])]

    def list_sinks(self) -> list[PluginSummary]:
        return [PluginSummary(name="power_automate", plugin_type="sink", description="test", config_fields=[])]

    def list_transforms(self) -> list[PluginSummary]:
        return []


class _NoSecretInventory:
    def has_ref(self, principal: str, name: str) -> bool:
        raise AssertionError("origin policy and managed identity discovery require no secret reads")

    def has_server_ref(self, name: str) -> bool:
        raise AssertionError("no secret reads")

    def has_user_ref(self, principal: str, name: str) -> bool:
        raise AssertionError("no secret reads")

    def server_generation(self, name: str) -> str | None:
        raise AssertionError("no secret reads")

    def user_generation(self, principal: str, name: str) -> str | None:
        raise AssertionError("no secret reads")


@pytest.mark.parametrize("origins", [(), ("https://flow.example",)])
def test_origin_allowlist_narrows_source_and_sink_discovery_without_secret_reads(origins: tuple[str, ...]) -> None:
    settings = RuntimeWebPluginConfig.from_settings(_settings(power_automate_allowed_origins=origins))
    ids = frozenset({PluginId("source", "power_automate"), PluginId("sink", "power_automate")})
    policy = WebPluginPolicy.create(
        required=frozenset(),
        configured_optional=ids,
        preferences=(),
        control_modes=(),
        plugin_code_identities=(),
        power_automate_allowed_origins=settings.power_automate_allowed_origins,
    )
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=_SchemaCatalog(),
        profiles=OperatorProfileRegistry(policy=policy, settings=settings),
        principal_scope="local:test",
        secret_inventory=_NoSecretInventory(),
        generation_key=b"test-origin-generation-key",
    )
    assert snapshot.power_automate_allowed_origins == origins
    assert snapshot.available == (ids if origins else frozenset())
    assert [(item.plugin_id, item.reason) for item in snapshot.unavailable] == (
        [] if origins else [(plugin_id, PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING) for plugin_id in sorted(ids)]
    )


@pytest.mark.parametrize("origins", [(), ("https://flow.example",)])
def test_real_power_automate_catalog_origin_admission_is_credential_free(origins: tuple[str, ...]) -> None:
    from elspeth.plugins.sinks.power_automate import PowerAutomateSink
    from elspeth.plugins.sources.power_automate import PowerAutomateSource
    from elspeth.web.catalog.service import CatalogServiceImpl

    manager = _isolated_manager_with_llm_source()
    assert manager.get_source_by_name("power_automate") is PowerAutomateSource
    assert manager.get_sink_by_name("power_automate") is PowerAutomateSink
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(plugin_allowlist=("source:power_automate", "sink:power_automate"), power_automate_allowed_origins=origins)
    )
    policy = compile_web_plugin_policy(registry=manager, settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = CatalogServiceImpl(manager)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:test",
        secret_inventory=_NoSecretInventory(),
        generation_key=b"test-origin-generation-key",
    )
    ids = {PluginId("source", "power_automate"), PluginId("sink", "power_automate")}
    assert ids <= snapshot.available if origins else ids.isdisjoint(snapshot.available)
    result = validate_plugin_policy(_state("https://flow.example"), snapshot=snapshot, profile_registry=profiles, catalog=catalog)
    assert not result.findings if origins else any(finding.error_code == "power_automate_origin_not_allowed" for finding in result.findings)


def test_approved_normalized_source_and_sink_origin_admitted_without_secret_resolution() -> None:
    state = _state("https://FLOW.example:443")
    result = validate_plugin_policy(state, snapshot=_snapshot(("https://flow.example",)), profile_registry=None, catalog=_SchemaCatalog())
    assert result.findings == ()
    assert result.executable_state == state


@pytest.mark.parametrize("origins,denied", [((), True), (("https://flow.example",), False)])
def test_yaml_and_composer_use_same_origin_policy(origins: tuple[str, ...], denied: bool) -> None:
    imported = composition_state_from_runtime_yaml("""
sources:
  default:
    plugin: power_automate
    on_success: out
    options:
      allowed_origin: https://flow.example
      auth: {method: sas, trigger_url_secret: {secret_ref: FLOW_URL}}
      on_validation_failure: discard
sinks:
  out:
    plugin: power_automate
    on_write_failure: fail
    options:
      allowed_origin: https://flow.example
      auth: {method: sas, trigger_url_secret: {secret_ref: FLOW_URL}}
""")
    for state in (_state("https://flow.example"), imported):
        result = validate_plugin_policy(state, snapshot=_snapshot(origins), profile_registry=None, catalog=_SchemaCatalog())
        assert bool(result.findings) is denied
        assert {finding.error_code for finding in result.findings} == ({"power_automate_origin_not_allowed"} if denied else set())


def test_policy_change_invalidates_old_execution_admission_before_secrets() -> None:
    from elspeth.web.execution.envelope import (
        EnvelopeRecoveryReason,
        ExecutionEnvelopeRefused,
        capture_execution_envelope,
        restore_execution_envelope,
    )
    from elspeth.web.execution.protocol import FrozenRunSettings

    manager = _isolated_manager_with_llm_source()
    runtime = RuntimeWebPluginConfig.from_settings(_settings(power_automate_allowed_origins=("https://flow.example",)))
    policy = compile_web_plugin_policy(registry=manager, settings=runtime)
    changed_policy = compile_web_plugin_policy(registry=manager, settings=replace(runtime, power_automate_allowed_origins=()))
    original = _snapshot(policy.power_automate_allowed_origins, policy_hash=policy.policy_hash)
    current = _snapshot(changed_policy.power_automate_allowed_origins, policy_hash=changed_policy.policy_hash)
    envelope = capture_execution_envelope(
        FrozenRunSettings(original, {}, {}),
        user_id="test",
        auth_provider_type="local",
        resolver=None,
        env_ref_names=frozenset(),
        implementation_fingerprint="c" * 64,
        deployment_generation="test",
    )
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        restore_execution_envelope(
            envelope.to_json(),
            current_snapshot=current,
            user_id="test",
            auth_provider_type="local",
            resolver=None,
            implementation_fingerprint="c" * 64,
            deployment_generation="test",
        )
    assert caught.value.reason is EnvelopeRecoveryReason.POLICY_CHANGED


def test_detached_interpretation_retains_and_rechecks_origin_authority() -> None:
    from tests.unit.web.sessions.test_interpretation_validation_inputs import _context

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.interpretation_validation import build_interpretation_validation_inputs

    catalog, profiles, original = _context()
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=original.policy_hash,
        principal_scope=original.principal_scope,
        available=original.available,
        unavailable=original.unavailable,
        selected=original.selected,
        usable_profile_aliases=original.usable_profile_aliases,
        selected_profile_aliases=original.selected_profile_aliases,
        control_modes=original.control_modes,
        binding_generation_fingerprint=original.binding_generation_fingerprint,
        power_automate_allowed_origins=("https://flow.example",),
    )
    detached = build_interpretation_validation_inputs(
        profile_aware=True, plugin_snapshot=snapshot, profile_registry=profiles, catalog=catalog
    )
    assert detached.plugin_snapshot == snapshot
    with pytest.raises(AuditIntegrityError, match="invalid canonical authority"):
        build_interpretation_validation_inputs(
            profile_aware=True,
            plugin_snapshot=replace(snapshot, power_automate_allowed_origins=()),
            profile_registry=profiles,
            catalog=catalog,
        )


def test_audit_evidence_retains_only_normalized_approved_origins() -> None:
    from elspeth.web.execution.service import _build_web_plugin_policy_evidence

    snapshot = _snapshot(("https://flow.example",))
    evidence = _build_web_plugin_policy_evidence(snapshot=snapshot, policy=None)
    assert evidence.power_automate_allowed_origins == ("https://flow.example",)
    for origin in ("https://flow.example:443", "https://FLOW.example", "https://flow.example/read?sig=private"):
        with pytest.raises(ValueError, match="normalized HTTPS443"):
            replace(evidence, power_automate_allowed_origins=(origin,))

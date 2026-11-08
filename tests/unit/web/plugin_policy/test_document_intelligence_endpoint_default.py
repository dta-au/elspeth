"""Effective ADI endpoints are owned config, not ambient plugin fallbacks."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.unit.web.execution.test_execution_envelope import SecretStore
from tests.unit.web.plugin_policy.test_availability import _Inventory, _settings
from tests.unit.web.test_config import _REQUIRED_WEB_ENV

from elspeth.config_loading import load_settings, load_settings_from_yaml_string
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem
from elspeth.core.config import resolve_config
from elspeth.core.document_intelligence_config import materialize_document_intelligence_endpoint
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.transforms.azure.document_intelligence import AzureDocumentIntelligence, AzureDocumentIntelligenceConfig
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.yaml_generator import generate_pipeline_dict
from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml
from elspeth.web.config import settings_from_env
from elspeth.web.execution.envelope import (
    EnvelopeRecoveryReason,
    ExecutionEnvelopeRefused,
    ExecutionRecoveryAuthority,
    build_run_execution_input,
    capture_execution_envelope,
    restore_run_execution_input,
)
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.retained_inputs import retain_execution_inputs
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.plugin_policy.validation import validate_authored_composition_state, validate_plugin_policy
from elspeth.web.sessions.interpretation_validation import (
    build_interpretation_validation_inputs,
    validate_composition_state_with_interpretation_inputs,
)

DEFAULT = "https://operator.cognitiveservices.azure.com"
OVERRIDE = "https://explicit.cognitiveservices.azure.com"
ROTATED = "https://rotated.cognitiveservices.azure.com"
ADI = PluginId("transform", "azure_document_intelligence")
KEY = "AZURE_DOCUMENT_INTELLIGENCE_KEY"
OPTIONS = {
    "api_key": {"secret_ref": KEY},
    "model_id": "prebuilt-layout",
    "source_mode": "url",
    "source_field": "doc_url",
    "content_field": "di_content",
    "schema": {"mode": "observed"},
}


def test_endpoint_default_preserves_heterogeneous_values_and_detaches_outer_mapping():
    nested = {"items": [1, False, None]}
    options = {"nested": nested, "count": 7, "enabled": False}
    effective = materialize_document_intelligence_endpoint(options, DEFAULT)
    assert effective["endpoint"] == DEFAULT
    assert effective["nested"] is nested
    assert effective["count"] == 7
    assert effective["enabled"] is False
    assert "endpoint" not in options
    effective["count"] = 9
    assert options["count"] == 7

    explicit = {**options, "endpoint": None}
    preserved = materialize_document_intelligence_endpoint(explicit, DEFAULT)
    assert preserved["endpoint"] is None
    assert preserved["nested"] is nested
    preserved["endpoint"] = OVERRIDE
    assert explicit["endpoint"] is None


def context(endpoint, *, inventory=None, principal="local:alice"):
    settings = _settings(plugin_allowlist=(str(ADI),), azure_document_intelligence_endpoint=endpoint)
    manager = get_shared_plugin_manager()
    catalog = CatalogServiceImpl(manager)
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=manager, settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=registry,
        principal_scope=principal,
        secret_inventory=inventory if inventory is not None else _Inventory(server=frozenset({KEY})),
        generation_key=b"adi-test-generation",
    )
    return catalog, registry, snapshot


def state(options):
    return CompositionState.from_dict(
        {
            "sources": {},
            "nodes": [
                {
                    "id": "adi",
                    "node_type": "transform",
                    "plugin": ADI.name,
                    "input": "documents",
                    "on_success": "results",
                    "on_error": "discard",
                    "options": options,
                }
            ],
            "edges": [],
            "outputs": [],
            "metadata": {"name": "ADI", "description": ""},
            "version": 1,
        }
    )


def pipeline(options, endpoint=None):
    raw = yaml.safe_load((Path(__file__).resolve().parents[4] / "examples/transform_pipeline/settings.yaml").read_text())
    raw["transforms"][0]["plugin"] = ADI.name
    raw["transforms"][0]["options"] = deepcopy(options)
    if endpoint is not None:
        raw["azure_document_intelligence_endpoint"] = endpoint
    return raw


@pytest.mark.parametrize("explicit", [False, True])
def test_yaml_composer_and_runtime_use_same_materialized_endpoint(explicit):
    options = {**OPTIONS, **({"endpoint": OVERRIDE} if explicit else {})}
    expected = OVERRIDE if explicit else DEFAULT
    catalog, registry, snapshot = context(DEFAULT)
    authored = state(options)
    result = validate_plugin_policy(authored, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert result.findings == ()
    effective = result.executable_state.nodes[0].options
    assert effective["endpoint"] == expected
    assert ("endpoint" in authored.nodes[0].options) is explicit
    assert effective["api_key"] == options["api_key"]
    loaded = load_settings_from_yaml_string(yaml.safe_dump(pipeline(options, DEFAULT)))
    assert loaded.transforms[0].options["endpoint"] == expected
    assert resolve_config(loaded)["transforms"][0]["options"]["endpoint"] == expected
    # Credential resolution is represented by an explicit fake test key, not
    # an operator key default or a provider call.
    plugin = AzureDocumentIntelligence({**loaded.transforms[0].options, "api_key": "test-key"})
    assert plugin._endpoint == expected


def test_cli_environment_override_changes_default_but_not_explicit_node(tmp_path, monkeypatch):
    monkeypatch.setenv("ELSPETH_AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", ROTATED)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(pipeline(OPTIONS, DEFAULT)))
    assert load_settings(path).transforms[0].options["endpoint"] == ROTATED
    path.write_text(yaml.safe_dump(pipeline({**OPTIONS, "endpoint": OVERRIDE}, DEFAULT)))
    assert load_settings(path).transforms[0].options["endpoint"] == OVERRIDE


def test_missing_both_is_still_a_plugin_configuration_error(monkeypatch):
    monkeypatch.setenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", DEFAULT)
    catalog, registry, snapshot = context(None)
    result = validate_plugin_policy(state(OPTIONS), snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert "endpoint" not in result.executable_state.nodes[0].options
    with pytest.raises(PluginConfigError, match="endpoint"):
        AzureDocumentIntelligenceConfig.from_dict({**OPTIONS, "api_key": "test-key"})


@pytest.mark.parametrize("invalid", [None, "", "http://operator.cognitiveservices.azure.com", "https://example.com"])
def test_explicit_invalid_endpoint_is_not_replaced(invalid):
    catalog, registry, snapshot = context(DEFAULT)
    options = {**OPTIONS, "endpoint": invalid}
    result = validate_plugin_policy(state(options), snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert result.executable_state.nodes[0].options["endpoint"] == invalid
    loaded = load_settings_from_yaml_string(yaml.safe_dump(pipeline(options, DEFAULT)))
    assert loaded.transforms[0].options["endpoint"] == invalid
    with pytest.raises(PluginConfigError, match="endpoint"):
        AzureDocumentIntelligenceConfig.from_dict({**loaded.transforms[0].options, "api_key": "test-key"})


def test_changed_default_changes_snapshot_and_preserves_captured_occurrence():
    catalog, registry, snapshot = context(DEFAULT)
    captured = build_interpretation_validation_inputs(
        profile_aware=True, plugin_snapshot=snapshot, profile_registry=registry, catalog=catalog
    )
    other_catalog, other_registry, other_snapshot = context(ROTATED)
    assert snapshot.snapshot_hash != other_snapshot.snapshot_hash
    original = state(OPTIONS)
    first = validate_plugin_policy(original, snapshot=snapshot, profile_registry=captured, catalog=captured)
    second = validate_plugin_policy(original, snapshot=other_snapshot, profile_registry=other_registry, catalog=other_catalog)
    assert first.executable_state.nodes[0].options["endpoint"] == DEFAULT
    assert second.executable_state.nodes[0].options["endpoint"] == ROTATED
    # A previously admitted effective value is explicit and must not rotate.
    retained = validate_plugin_policy(
        first.executable_state, snapshot=other_snapshot, profile_registry=other_registry, catalog=other_catalog
    )
    assert retained.executable_state.nodes[0].options["endpoint"] == DEFAULT


def test_default_does_not_supply_credentials_or_broaden_principal_access():
    _, _, missing = context(DEFAULT, inventory=_Inventory())
    assert ADI not in missing.available
    inventory = _Inventory(users={"local:alice": frozenset({KEY})})
    _, _, alice = context(DEFAULT, inventory=inventory)
    _, _, bob = context(DEFAULT, inventory=inventory, principal="local:bob")
    assert ADI in alice.available
    assert ADI not in bob.available
    inventory.users["local:alice"] = frozenset()
    _, _, revoked = context(DEFAULT, inventory=inventory)
    assert ADI not in revoked.available


def test_discovery_projects_default_without_mutating_full_catalog():
    catalog, registry, _ = context(DEFAULT)
    full = catalog.get_schema("transform", ADI.name)
    projected = registry.public_schema(ADI, full, available_aliases=())
    assert "endpoint" in full.json_schema["required"]
    assert "endpoint" not in projected.json_schema["required"]
    assert projected.json_schema["properties"]["endpoint"]["default"] == DEFAULT
    assert projected.secret_requirements == full.secret_requirements
    assert projected.web_config_authority == full.web_config_authority
    knob = next(item for item in projected.knob_schema["fields"] if item["name"] == "endpoint")
    assert knob["required"] is False
    assert knob["default"] == DEFAULT


@pytest.mark.parametrize("invalid", ["", "https://example.com", "https://operator.cognitiveservices.azure.com/path"])
def test_invalid_operator_default_is_rejected_at_both_settings_boundaries(invalid):
    with pytest.raises(ValueError, match="endpoint"):
        _settings(azure_document_intelligence_endpoint=invalid)
    with pytest.raises(ValueError, match="endpoint"):
        load_settings_from_yaml_string(yaml.safe_dump(pipeline(OPTIONS, invalid)))


def test_explicit_endpoint_does_not_require_operator_default():
    catalog, registry, snapshot = context(None)
    authored = state({**OPTIONS, "endpoint": OVERRIDE})
    result = validate_plugin_policy(authored, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert result.findings == ()
    assert result.executable_state.nodes[0].options["endpoint"] == OVERRIDE


def test_trained_operator_detachment_retains_endpoint_default_without_changing_authority():
    catalog, registry, _ = context(DEFAULT)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    captured = build_interpretation_validation_inputs(
        profile_aware=True, plugin_snapshot=snapshot, profile_registry=registry, catalog=catalog
    )
    result = validate_plugin_policy(state(OPTIONS), snapshot=snapshot, profile_registry=captured, catalog=captured)
    assert result.executable_state.nodes[0].options["endpoint"] == DEFAULT
    assert captured.plugin_snapshot == snapshot


@pytest.mark.parametrize("endpoint", [DEFAULT, ROTATED])
def test_actual_web_environment_loader_reads_operator_endpoint(monkeypatch, endpoint):
    for name, value in _REQUIRED_WEB_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ELSPETH_WEB__AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", endpoint)
    settings = settings_from_env()
    assert settings.azure_document_intelligence_endpoint == endpoint
    assert RuntimeWebPluginConfig.from_settings(settings).azure_document_intelligence_endpoint == endpoint


@pytest.mark.parametrize("invalid", ["", "http://operator.cognitiveservices.azure.com", "https://example.com"])
def test_actual_web_environment_loader_refuses_invalid_default(monkeypatch, invalid):
    for name, value in _REQUIRED_WEB_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ELSPETH_WEB__AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", invalid)
    with pytest.raises(ValidationError, match="azure_document_intelligence_endpoint"):
        settings_from_env()


def test_policy_view_same_owner_cache_reprojects_rotated_endpoint():
    catalog, registry, snapshot = context(DEFAULT)
    rotated_settings = _settings(plugin_allowlist=(str(ADI),), azure_document_intelligence_endpoint=ROTATED)
    runtime = RuntimeWebPluginConfig.from_settings(rotated_settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    rotated_registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    rotated_snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=rotated_registry,
        principal_scope="local:alice",
        secret_inventory=_Inventory(server=frozenset({KEY})),
        generation_key=b"adi-test-generation",
    )
    assert snapshot.snapshot_hash != rotated_snapshot.snapshot_hash
    caches = ComposerSessionCacheRegistry()
    with caches.borrow(SessionCacheKey("local:alice", "adi-default-rotation")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        first = PolicyCatalogView(catalog, snapshot, registry).with_analysis(analysis)
        second = PolicyCatalogView(catalog, rotated_snapshot, rotated_registry).with_analysis(analysis)
        assert first.projection_revision != second.projection_revision
        first_schema = first.get_schema("transform", ADI.name)
        first_summary = next(item for item in first.list_transforms() if item.name == ADI.name)
        hits = caches.snapshot().hits
        assert first.get_schema("transform", ADI.name) == first_schema
        assert first.list_transforms() == first.list_transforms()
        assert caches.snapshot().hits > hits
        second_schema = second.get_schema("transform", ADI.name)
        second_summary = next(item for item in second.list_transforms() if item.name == ADI.name)
        assert first_schema.json_schema["properties"]["endpoint"]["default"] == DEFAULT
        assert second_schema.json_schema["properties"]["endpoint"]["default"] == ROTATED
        assert next(item for item in first_summary.config_fields if item.name == "endpoint").default == DEFAULT
        assert next(item for item in second_summary.config_fields if item.name == "endpoint").default == ROTATED
        assert first_schema.secret_requirements == second_schema.secret_requirements
        assert first_summary.secret_requirements == second_summary.secret_requirements
        assert first.get_schema("transform", ADI.name) == first_schema


def valid_state(options, source_path=None):
    raw = pipeline(options)
    raw.pop("landscape")
    raw["transforms"] = [raw["transforms"][0]]
    raw["transforms"][0]["on_success"] = "output"
    if source_path is not None:
        raw["sources"]["primary"]["options"]["path"] = str(source_path)
    return composition_state_from_runtime_yaml(yaml.safe_dump(raw))


@pytest.mark.parametrize("trained", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_full_authored_and_detached_validation_accept_same_effective_graph(trained, explicit):
    catalog, registry, normal = context(DEFAULT)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog) if trained else normal
    authored = valid_state({**OPTIONS, **({"endpoint": OVERRIDE} if explicit else {})})
    captured = build_interpretation_validation_inputs(
        profile_aware=True, plugin_snapshot=snapshot, profile_registry=registry, catalog=catalog
    )
    direct = validate_authored_composition_state(authored, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    detached = validate_composition_state_with_interpretation_inputs(authored, captured)
    assert direct.validation.is_valid, direct.validation.errors
    assert detached.validation.is_valid, detached.validation.errors
    assert direct.executable_state == detached.executable_state
    assert direct.executable_state.nodes[0].options["endpoint"] == (OVERRIDE if explicit else DEFAULT)
    assert direct.authored_state == detached.authored_state == authored
    assert ("endpoint" in authored.nodes[0].options) is explicit


@pytest.mark.parametrize("trained", [False, True])
@pytest.mark.parametrize("invalid", [None, "", "https://example.com"])
def test_full_authored_and_detached_validation_refuse_explicit_invalid_endpoint(trained, invalid):
    catalog, registry, normal = context(DEFAULT)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog) if trained else normal
    authored = valid_state({**OPTIONS, "endpoint": invalid})
    captured = build_interpretation_validation_inputs(
        profile_aware=True, plugin_snapshot=snapshot, profile_registry=registry, catalog=catalog
    )
    direct = validate_authored_composition_state(authored, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    detached = validate_composition_state_with_interpretation_inputs(authored, captured)
    assert not direct.validation.is_valid
    assert not detached.validation.is_valid
    assert direct.executable_state.nodes[0].options["endpoint"] == invalid
    assert detached.executable_state.nodes[0].options["endpoint"] == invalid
    assert any("endpoint" in str(error) for error in direct.validation.errors)
    assert any("endpoint" in str(error) for error in detached.validation.errors)


class AdiSecrets(SecretStore):
    def __init__(self):
        self.secret = ResolvedSecret(KEY, "private-adi-test-material", "server", "a" * 64)

    def resolve(self, user_id, name):
        return self.secret if name == KEY else None

    def list_refs(self, user_id):
        return [SecretInventoryItem(KEY, "server", self.secret is not None)]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("default", EnvelopeRecoveryReason.BINDING_CHANGED),
        ("policy", EnvelopeRecoveryReason.POLICY_CHANGED),
        ("credential", EnvelopeRecoveryReason.SECRET_VERSION_CHANGED),
        ("missing_credential", EnvelopeRecoveryReason.SECRET_VERSION_UNAVAILABLE),
    ],
)
def test_actual_retained_envelope_preserves_endpoint_or_refuses_changed_authority(tmp_path, change, reason):
    source = tmp_path / "documents.csv"
    source.write_text("doc_url\nhttps://example.com/document.pdf\n")
    catalog, registry, snapshot = context(DEFAULT)
    authored = valid_state(OPTIONS, source)
    admitted = validate_authored_composition_state(authored, snapshot=snapshot, profile_registry=registry, catalog=catalog)
    assert admitted.validation.is_valid, admitted.validation.errors
    generated = generate_pipeline_dict(admitted.executable_state)
    audit_generated = generate_pipeline_dict(authored)
    assert "endpoint" not in audit_generated["transforms"][0]["options"]
    assert generated["transforms"][0]["options"]["endpoint"] == DEFAULT
    frozen, retained = retain_execution_inputs(FrozenRunSettings(snapshot, generated, audit_generated), root=tmp_path / "retained")
    store = AdiSecrets()
    envelope = capture_execution_envelope(
        frozen,
        user_id="alice",
        auth_provider_type="local",
        resolver=store,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
        retained_inputs=retained,
    )
    execution_input = build_run_execution_input(envelope, frozen, "d" * 64, "image-1", retained)
    before = envelope.to_json()
    assert "private-adi-test-material" not in before
    restored = restore_run_execution_input(
        execution_input,
        current_authority=lambda: ExecutionRecoveryAuthority(snapshot, "d" * 64, "image-1"),
        user_id="alice",
        auth_provider_type="local",
        resolver=store,
    )
    assert restored.settings.executable_config["transforms"][0]["options"]["endpoint"] == DEFAULT
    assert "endpoint" not in restored.settings.audit_safe_config["transforms"][0]["options"]
    assert restored.settings.executable_config == frozen.executable_config
    assert restored.settings.audit_safe_config == frozen.audit_safe_config
    current = snapshot
    if change == "default":
        _, _, current = context(ROTATED)
    elif change == "policy":
        current = replace(snapshot, policy_hash="f" * 64)
    elif change == "credential":
        store.secret = ResolvedSecret(KEY, "rotated-adi-test-material", "server", "e" * 64)
    elif change == "missing_credential":
        store.secret = None
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        restore_run_execution_input(
            execution_input,
            current_authority=lambda: ExecutionRecoveryAuthority(current, "d" * 64, "image-1"),
            user_id="alice",
            auth_provider_type="local",
            resolver=store,
        )
    assert caught.value.reason is reason
    assert envelope.to_json() == before
    assert frozen.executable_config["transforms"][0]["options"]["endpoint"] == DEFAULT
    assert "endpoint" not in frozen.audit_safe_config["transforms"][0]["options"]
    assert restored.settings.executable_config == frozen.executable_config
    assert restored.settings.audit_safe_config == frozen.audit_safe_config

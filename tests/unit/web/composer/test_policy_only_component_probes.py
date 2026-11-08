"""Option probes preserve full diagnostics without synthetic graph walks."""

import pytest

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolContext, _prevalidate_source_for_context, _prevalidate_transform_for_context
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.validation import PluginPolicyValidationResult
from tests.unit.web.plugin_policy.test_availability import _Inventory
from tests.unit.web.plugin_policy.test_document_intelligence_endpoint_default import DEFAULT, OPTIONS, ROTATED, context, valid_state


class FullValidationProbeView(PolicyCatalogView):
    """Control reproduces the old probe's complete validation boundary."""

    def project_component_policy(self, state):
        result = self.validate_composition_state(state)
        return PluginPolicyValidationResult(result.executable_state, result.policy_findings)


@pytest.mark.parametrize("trained", [False, True])
@pytest.mark.parametrize("credential", [True, False])
@pytest.mark.parametrize("case", ["default", "invalid_endpoint", "bad_source_mode", "unknown_provider", "source", "source_conflict"])
def test_probe_diagnostics_match_full_validation(trained, credential, case, monkeypatch):
    kwargs = {} if credential else {"inventory": _Inventory(server=frozenset())}
    catalog, registry, snapshot = context(DEFAULT, **kwargs)
    if trained:
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    legacy = FullValidationProbeView(catalog, snapshot, registry)
    optimized = PolicyCatalogView(catalog, snapshot, registry)
    if case in {"source", "source_conflict"}:
        options = {"path": "input.csv", "schema": {"mode": "observed"}}
        if case == "source_conflict":
            options["on_validation_failure"] = "quarantine"

        def probe(view):
            return _prevalidate_source_for_context(ToolContext(catalog=view, plugin_snapshot=view.snapshot), "csv", options, "discard")
    else:
        options = dict(OPTIONS)
        plugin = "azure_document_intelligence"
        if case == "invalid_endpoint":
            options["endpoint"] = "https://example.com"
        elif case == "bad_source_mode":
            options["source_mode"] = "not-a-mode"
        elif case == "unknown_provider":
            plugin = "llm"
            options = {"provider": "anthropic", "model": "test", "prompt_template": "test", "schema": {"mode": "observed"}}

        def probe(view):
            return _prevalidate_transform_for_context(ToolContext(catalog=view, plugin_snapshot=view.snapshot), plugin, options)

    expected = probe(legacy)

    def forbidden_graph_walk(self):
        raise AssertionError("synthetic option probe ran structural graph validation")

    monkeypatch.setattr(CompositionState, "validate", forbidden_graph_walk)
    assert probe(optimized) == expected
    if case == "invalid_endpoint":
        assert expected is not None
    if case == "default" and credential:
        assert expected is None


def test_projection_rechecks_default_rotation_and_final_graph_still_validates(monkeypatch):
    catalog, registry, _ = context(DEFAULT)
    rotated_catalog, rotated_registry, _ = context(ROTATED)
    trained = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    first = PolicyCatalogView(catalog, trained, registry)
    second = PolicyCatalogView(rotated_catalog, trained, rotated_registry)
    authored = valid_state(OPTIONS)
    assert first.project_component_policy(authored).executable_state.nodes[0].options["endpoint"] == DEFAULT
    assert second.project_component_policy(authored).executable_state.nodes[0].options["endpoint"] == ROTATED
    assert "endpoint" not in authored.nodes[0].options
    calls = []
    original = CompositionState.validate

    def observe(state):
        calls.append(state)
        return original(state)

    monkeypatch.setattr(CompositionState, "validate", observe)
    result = second.validate_composition_state(authored)
    assert result.validation.is_valid
    assert len(calls) == 1
    assert calls[0].nodes[0].options["endpoint"] == ROTATED


def authorized_view(*, profile_present=True, legacy=False):
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.catalog.service import CatalogServiceImpl
    from elspeth.web.plugin_policy.availability import build_plugin_snapshot
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
    from tests.unit.web.plugin_policy.test_availability import _settings

    settings = _settings(
        plugin_allowlist=("source:csv", "transform:llm"),
        llm_profiles={"task": {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0"}}
        if profile_present
        else {},
        default_llm_profile="task" if profile_present else None,
    )
    manager = get_shared_plugin_manager()
    catalog = CatalogServiceImpl(manager)
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=manager, settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=registry,
        principal_scope="local:alice",
        secret_inventory=_Inventory(),
        generation_key=b"probe-policy-control",
    )
    cls = FullValidationProbeView if legacy else PolicyCatalogView
    return cls(catalog, snapshot, registry)


@pytest.mark.parametrize("subject", ["csv", "llm"])
def test_authorized_success_proves_old_walk_before_optimized_no_walk(subject, monkeypatch):
    from elspeth.web.plugin_policy.models import PluginId

    legacy = authorized_view(legacy=True)
    optimized = authorized_view()
    plugin_id = PluginId("source" if subject == "csv" else "transform", subject)
    assert plugin_id in legacy.snapshot.available
    assert plugin_id in optimized.snapshot.available
    options = (
        {"path": "input.csv", "schema": {"mode": "observed"}}
        if subject == "csv"
        else {"profile": "task", "prompt_template": "{{ row.text }}", "required_input_fields": ["text"], "schema": {"mode": "observed"}}
    )

    def probe(view):
        ctx = ToolContext(catalog=view, plugin_snapshot=view.snapshot)
        if subject == "csv":
            return _prevalidate_source_for_context(ctx, subject, options, "discard")
        return _prevalidate_transform_for_context(ctx, subject, options)

    calls = []
    original = CompositionState.validate

    def observe(state):
        calls.append(state)
        return original(state)

    monkeypatch.setattr(CompositionState, "validate", observe)
    expected = probe(legacy)
    assert expected is None
    assert len(calls) > 0
    calls.clear()
    assert probe(optimized) == expected
    assert calls == []


@pytest.mark.parametrize("change", ["alias", "private_option", "profile_removed"])
def test_configured_profile_current_refusals_preserve_exact_diagnostics(change):
    options = {"profile": "task", "prompt_template": "{{ row.text }}", "required_input_fields": ["text"], "schema": {"mode": "observed"}}
    initial = authorized_view()
    ctx = ToolContext(catalog=initial, plugin_snapshot=initial.snapshot)
    assert _prevalidate_transform_for_context(ctx, "llm", options) is None
    if change == "alias":
        options["profile"] = "unavailable"
    elif change == "private_option":
        options["model"] = "unapproved-model"
    kwargs = {"profile_present": change != "profile_removed"}
    legacy = authorized_view(legacy=True, **kwargs)
    optimized = authorized_view(**kwargs)
    if change == "profile_removed":
        assert optimized.snapshot.snapshot_hash != initial.snapshot.snapshot_hash
    expected = _prevalidate_transform_for_context(ToolContext(catalog=legacy, plugin_snapshot=legacy.snapshot), "llm", options)
    actual = _prevalidate_transform_for_context(ToolContext(catalog=optimized, plugin_snapshot=optimized.snapshot), "llm", options)
    assert expected is not None
    assert actual == expected


def test_optional_adi_current_policy_removal_preserves_exact_diagnostics():
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.catalog.service import CatalogServiceImpl
    from elspeth.web.plugin_policy.availability import build_plugin_snapshot
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.models import PluginId, PluginUnavailableReason
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
    from tests.unit.web.plugin_policy.test_availability import _settings
    from tests.unit.web.plugin_policy.test_document_intelligence_endpoint_default import KEY

    plugin_id = PluginId("transform", "azure_document_intelligence")
    manager = get_shared_plugin_manager()
    catalog = CatalogServiceImpl(manager)
    inventory = _Inventory(server=frozenset({KEY}))

    def view(allowed, *, legacy=False):
        settings = _settings(plugin_allowlist=(str(plugin_id),) if allowed else (), azure_document_intelligence_endpoint=DEFAULT)
        runtime = RuntimeWebPluginConfig.from_settings(settings)
        policy = compile_web_plugin_policy(registry=manager, settings=runtime)
        registry = OperatorProfileRegistry(policy=policy, settings=runtime)
        snapshot = build_plugin_snapshot(
            policy=policy,
            catalog=catalog,
            profiles=registry,
            principal_scope="local:alice",
            secret_inventory=inventory,
            generation_key=b"probe-policy-control",
        )
        cls = FullValidationProbeView if legacy else PolicyCatalogView
        return cls(catalog, snapshot, registry)

    initial = view(True)
    assert plugin_id in initial.snapshot.available
    assert inventory.has_server_ref(KEY)
    assert (
        _prevalidate_transform_for_context(ToolContext(catalog=initial, plugin_snapshot=initial.snapshot), plugin_id.name, OPTIONS) is None
    )
    legacy = view(False, legacy=True)
    optimized = view(False)
    assert plugin_id not in legacy.snapshot.available
    assert plugin_id not in optimized.snapshot.available
    assert optimized.snapshot.snapshot_hash != initial.snapshot.snapshot_hash
    expected = _prevalidate_transform_for_context(ToolContext(catalog=legacy, plugin_snapshot=legacy.snapshot), plugin_id.name, OPTIONS)
    actual = _prevalidate_transform_for_context(ToolContext(catalog=optimized, plugin_snapshot=optimized.snapshot), plugin_id.name, OPTIONS)
    assert expected is not None
    assert expected[1] == PluginUnavailableReason.NOT_AUTHORIZED.value
    assert actual == expected

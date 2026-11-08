"""Session discovery retains public payloads without retaining invocation state."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import BaseModel, ConfigDict, SecretBytes, StrictInt

from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager, get_shared_plugin_manager
from elspeth.plugins.transforms.passthrough import PassThrough
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.discovery_cache import CachedDiscoveryPayload
from elspeth.web.composer.discovery_session import execute_cached_discovery
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolContext, ToolResult, _registry
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context
from elspeth.web.composer.tools.declarations import DiscoveryCacheScope
from elspeth.web.config import WebSettings
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from tests.fixtures.composer_session_cache import count_composer_work, make_small_composer_state


@pytest.fixture(scope="module")
def catalog() -> CatalogServiceImpl:
    return CatalogServiceImpl(get_shared_plugin_manager())


def _view(catalog: CatalogServiceImpl, analysis: ComposerAnalysis) -> PolicyCatalogView:
    return PolicyCatalogView.for_trained_operator(catalog, PluginAvailabilitySnapshot.for_trained_operator(catalog), analysis=analysis)


@pytest.mark.parametrize("enabled", [True, False])
def test_new_operation_reuses_payload_but_rebuilds_current_envelope(catalog: CatalogServiceImpl, enabled: bool) -> None:
    registry = ComposerSessionCacheRegistry(enabled=enabled)
    state = make_small_composer_state()
    changed = replace(state, outputs=(), version=state.version)
    with registry.borrow(SessionCacheKey("principal", "session")) as lease:
        view = _view(catalog, ComposerAnalysis(lease.cache))
        context = ToolContext(catalog=view, plugin_snapshot=view.snapshot)
        calls: list[CompositionState] = []

        def execute(current: CompositionState) -> ToolResult:
            calls.append(current)
            return execute_discovery_tool_with_context("get_expression_grammar", {}, current, context)

        first = execute_cached_discovery("get_expression_grammar", {}, state, view, partial(execute, state))
        warm = execute_cached_discovery("get_expression_grammar", {}, changed, view, partial(execute, changed))
        assert first.cache_hit is False
        assert warm.cache_hit is enabled
        assert len(calls) == (1 if enabled else 2)
        assert first.admitted.response is not None and warm.admitted.response is not None
        assert first.admitted.response.to_wire() == warm.admitted.response.to_wire()
        assert warm.admitted.result.updated_state is changed
        assert warm.admitted.result.validation == view.validate_composition_state(changed).validation
        assert not warm.admitted.result.validation.is_valid


def test_model_catalog_is_operation_only_and_admits_integral_float(catalog: CatalogServiceImpl) -> None:
    registry = ComposerSessionCacheRegistry()
    state = make_small_composer_state()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        view = _view(catalog, ComposerAnalysis(lease.cache))
        context = ToolContext(catalog=view, plugin_snapshot=view.snapshot)
        calls = 0

        def compute() -> ToolResult:
            nonlocal calls
            calls += 1
            return execute_discovery_tool_with_context("list_models", {"limit": 1.0}, state, context)

        operation: dict[str, CachedDiscoveryPayload] = {}
        first = execute_cached_discovery("list_models", {"limit": 1.0}, state, view, compute, operation)
        repeated = execute_cached_discovery("list_models", {"limit": 1}, state, view, compute, operation)
        next_operation = execute_cached_discovery("list_models", {"limit": 1.0}, state, view, compute, {})
        assert first.admitted.result.success
        assert not first.cache_hit and repeated.cache_hit and not next_operation.cache_hit
        assert calls == 2


def test_changed_strict_argument_parser_rejects_integral_float_before_hit(
    catalog: CatalogServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StrictModelArguments(BaseModel):
        limit: StrictInt
        model_config = ConfigDict(extra="forbid")

    registry = ComposerSessionCacheRegistry()
    state = make_small_composer_state()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        view = _view(catalog, ComposerAnalysis(lease.cache))
        context = ToolContext(catalog=view, plugin_snapshot=view.snapshot)
        compute = partial(execute_discovery_tool_with_context, "list_models", {"limit": 1}, state, context)
        operation: dict[str, CachedDiscoveryPayload] = {}
        assert execute_cached_discovery("list_models", {"limit": 1}, state, view, compute, operation).admitted.result.success
        declarations = dict(_registry._DECLARATIONS_BY_NAME)
        declarations["list_models"] = replace(
            declarations["list_models"], argument_model=StrictModelArguments, json_schema=deep_thaw(declarations["list_models"].json_schema)
        )
        monkeypatch.setattr(_registry, "_DECLARATIONS_BY_NAME", MappingProxyType(declarations))
        with pytest.raises(ToolArgumentError):
            execute_cached_discovery("list_models", {"limit": 1.0}, state, view, compute, operation)


@pytest.mark.parametrize("missing", ["argument_model", "cache_revision"])
def test_missing_declaration_dependency_bypasses_retention(
    catalog: CatalogServiceImpl, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    declaration = _registry.declaration_for("get_expression_grammar")
    changed = (
        replace(declaration, argument_model=None, json_schema=deep_thaw(declaration.json_schema))
        if missing == "argument_model"
        else replace(declaration, cache_revision=None, json_schema=deep_thaw(declaration.json_schema))
    )
    monkeypatch.setattr(
        _registry, "_DECLARATIONS_BY_NAME", MappingProxyType({**_registry._DECLARATIONS_BY_NAME, declaration.name: changed})
    )
    registry = ComposerSessionCacheRegistry()
    state = make_small_composer_state()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        view = _view(catalog, ComposerAnalysis(lease.cache))
        context = ToolContext(catalog=view, plugin_snapshot=view.snapshot)
        compute = partial(execute_discovery_tool_with_context, declaration.name, {}, state, context)
        assert not execute_cached_discovery(declaration.name, {}, state, view, compute).cache_hit
        assert not execute_cached_discovery(declaration.name, {}, state, view, compute).cache_hit
        assert lease.cache is not None and lease.cache.discovery.entry_count == 0


def test_catalog_warm_listings_emit_no_schemas_and_isolate_nested_responses(catalog: CatalogServiceImpl) -> None:
    class SchemaControl(BaseModel):
        field: str

    with count_composer_work() as counts:
        first = catalog.list_sources()
        second = catalog.list_sources()
        assert first == second
        assert counts.json_schema_generations == 0
        # Positive control proves the schema instrument is live.
        SchemaControl.model_json_schema()
        assert counts.json_schema_generations == 1
    first[0].config_fields.clear()
    first[0].name = "corrupted"
    assert catalog.list_sources() == second
    schema = catalog.get_schema("source", "csv")
    expected = schema.model_copy(deep=True)
    schema.json_schema.clear()
    schema.knob_schema.clear()
    assert catalog.get_schema("source", "csv") == expected


def test_registration_change_rebuilds_catalog_and_advances_revision() -> None:
    manager = PluginManager()
    manager.register_builtin_plugins()
    catalog = CatalogServiceImpl(manager)
    before = catalog.cache_revision

    class NewlyRegistered(PassThrough):
        name = "session_cache_new_transform"
        determinism = PassThrough.determinism
        source_file_hash = "sha256:0123456789abcdef"

    manager.register(create_dynamic_hookimpl([NewlyRegistered], "elspeth_get_transforms"))
    assert NewlyRegistered.name in {item.name for item in catalog.list_transforms()}
    assert catalog.cache_revision != before
    assert catalog.get_schema("transform", NewlyRegistered.name).name == NewlyRegistered.name


def test_cacheable_compatibility_does_not_promote_lifetime() -> None:
    original = _registry.declaration_for("get_expression_grammar")
    compatibility = replace(original, cache_scope=DiscoveryCacheScope.NONE, json_schema=deep_thaw(original.json_schema))
    assert compatibility.cacheable
    assert compatibility.discovery_cache_scope is DiscoveryCacheScope.OPERATION


def test_catalog_and_profile_generations_separate_equal_policy_snapshots(catalog: CatalogServiceImpl) -> None:
    registry = ComposerSessionCacheRegistry()
    runtime = RuntimeWebPluginConfig.from_settings(
        WebSettings(
            composer_max_composition_turns=4,
            composer_max_discovery_turns=4,
            composer_timeout_seconds=60,
            composer_rate_limit_per_minute=20,
            shareable_link_signing_key=SecretBytes(b"0123456789abcdef0123456789abcdef"),
        )
    )
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    replacement_profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        original = PolicyCatalogView(catalog, snapshot, profiles, analysis=analysis)
        changed_profile = PolicyCatalogView(catalog, snapshot, replacement_profiles, analysis=analysis)
        changed_catalog = PolicyCatalogView(CatalogServiceImpl(get_shared_plugin_manager()), snapshot, profiles, analysis=analysis)
        assert original.project_json("witness", lambda: "original") == "original"
        assert original.project_json("witness", lambda: "must-not-run") == "original"
        assert changed_profile.project_json("witness", lambda: "profile replacement") == "profile replacement"
        assert changed_catalog.project_json("witness", lambda: "catalog replacement") == "catalog replacement"
        assert len({original.projection_revision, changed_profile.projection_revision, changed_catalog.projection_revision}) == 3


def test_policy_change_immediately_hides_previously_cached_plugin(catalog: CatalogServiceImpl) -> None:
    registry = ComposerSessionCacheRegistry()
    state = make_small_composer_state()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        view = _view(catalog, ComposerAnalysis(lease.cache))
        context = ToolContext(catalog=view, plugin_snapshot=view.snapshot)
        first = execute_cached_discovery(
            "list_sources", {}, state, view, partial(execute_discovery_tool_with_context, "list_sources", {}, state, context)
        )
        before = view.snapshot
        changed = PluginAvailabilitySnapshot.create(
            policy_hash=before.policy_hash,
            principal_scope=before.principal_scope,
            available=before.available - {PluginId("source", "csv")},
            unavailable=before.unavailable,
            selected=before.selected,
            usable_profile_aliases=before.usable_profile_aliases,
            selected_profile_aliases=before.selected_profile_aliases,
            binding_generation_fingerprint=before.binding_generation_fingerprint,
            authority=before.authority,
        )
        updated = PolicyCatalogView.for_trained_operator(catalog, changed, analysis=view.analysis)
        updated_context = ToolContext(catalog=updated, plugin_snapshot=changed)
        second = execute_cached_discovery(
            "list_sources", {}, state, updated, partial(execute_discovery_tool_with_context, "list_sources", {}, state, updated_context)
        )
        assert not first.cache_hit and not second.cache_hit
        assert "csv" in {item.name for item in view.list_sources()}
        assert "csv" not in {item.name for item in updated.list_sources()}
        assert first.admitted.response is not None and second.admitted.response is not None
        assert first.admitted.response.to_wire() != second.admitted.response.to_wire()


def test_session_public_projection_returns_isolated_models(catalog: CatalogServiceImpl) -> None:
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        view = _view(catalog, ComposerAnalysis(lease.cache))
        first = view.list_sources()
        expected = view.list_sources()
        first[0].config_fields.clear()
        first[0].name = "corrupted"
        assert view.list_sources() == expected
        schema = view.get_schema("source", "csv")
        expected_schema = schema.model_copy(deep=True)
        schema.json_schema.clear()
        schema.knob_schema.clear()
        assert view.get_schema("source", "csv") == expected_schema
        assert lease.cache is not None and lease.cache.projections.entry_count == 2


def test_authoring_aids_key_includes_catalog_and_profile_revision(catalog: CatalogServiceImpl, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer import planner_authoring_aids

    builds: list[str | None] = []

    def build(view: PolicyCatalogView) -> dict[str, str]:
        revision = view.projection_revision
        assert revision is not None
        builds.append(revision)
        return {"purpose": revision}

    monkeypatch.setattr(planner_authoring_aids, "_build_planner_authoring_aids", build)
    first = _view(catalog, ComposerAnalysis())
    second = _view(CatalogServiceImpl(get_shared_plugin_manager()), ComposerAnalysis())
    assert first.snapshot.snapshot_hash == second.snapshot.snapshot_hash
    assert planner_authoring_aids.build_planner_authoring_aids(first) == planner_authoring_aids.build_planner_authoring_aids(first)
    assert planner_authoring_aids.build_planner_authoring_aids(first) != planner_authoring_aids.build_planner_authoring_aids(second)
    assert builds == [first.projection_revision, second.projection_revision]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_planner_repeat_still_exhausts_discovery_budget_when_session_is_warm(
    catalog: CatalogServiceImpl, enabled: bool, tmp_path: Path, tool_context: ToolContext
) -> None:
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.pipeline_planner import PipelinePlannerError
    from tests.unit.web.composer.test_pipeline_planner import _plan, _response, _ScriptedCompletion

    registry = ComposerSessionCacheRegistry(enabled=enabled)
    state = make_small_composer_state()
    try:
        with registry.borrow(SessionCacheKey("p", "s")) as lease:
            view = _view(catalog, ComposerAnalysis(lease.cache))
            context = ToolContext(catalog=view, plugin_snapshot=view.snapshot)
            warm = execute_cached_discovery(
                "get_expression_grammar",
                {},
                state,
                view,
                partial(execute_discovery_tool_with_context, "get_expression_grammar", {}, state, context),
            )
            assert not warm.cache_hit
            provider = _ScriptedCompletion(_response(("get_expression_grammar", {})), _response(("get_expression_grammar", {})))
            recorder = BufferingRecorder()
            with pytest.raises(PipelinePlannerError) as caught:
                await _plan(
                    tmp_path=tmp_path,
                    tool_context=tool_context,
                    completion=provider,
                    recorder=recorder,
                    policy_override=(view, view.snapshot),
                    current_state=state,
                    provider_current_state=state.to_dict(),
                    model_overrides={"max_discovery_turns": 1, "escape_hatch_model": None},
                )
            assert caught.value.code == "DISCOVERY_EXHAUSTED"
            assert len(provider.requests) == 2
            assert len(recorder.invocations) == 1
            assert recorder.invocations[0].cache_hit is enabled
    finally:
        await registry.finish_shutdown()

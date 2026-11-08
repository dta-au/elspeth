"""Actual disclosure, policy revisions, and bounded owner guidance."""

from __future__ import annotations

import asyncio
from functools import partial
from unittest.mock import patch

import pytest

from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager, get_shared_plugin_manager
from elspeth.plugins.transforms.passthrough import PassThrough
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from tests.fixtures.composer_session_cache import make_small_composer_state


def _view(catalog, cache):
    return PolicyCatalogView.for_trained_operator(
        catalog, PluginAvailabilitySnapshot.for_trained_operator(catalog), analysis=ComposerAnalysis(cache)
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_disclosure_shares_owner_but_not_fork_principal_or_replacement_catalog(enabled):
    registry = ComposerSessionCacheRegistry(enabled=enabled)
    catalog = CatalogServiceImpl(get_shared_plugin_manager())
    key = SessionCacheKey("alice", "session")
    with registry.borrow(key) as first:
        view = _view(catalog, first.cache)
        view.schema_disclosure.mark_plugin_schema_loaded("session", "source", "csv")
    with (
        registry.borrow(key) as second,
        registry.borrow(SessionCacheKey("alice", "fork")) as fork,
        registry.borrow(SessionCacheKey("bob", "session")) as bob,
    ):
        warm = _view(catalog, second.cache)
        assert warm.schema_disclosure.schemas_loaded_for_session("session") == frozenset({("source", "csv")})
        for cache in (fork.cache, bob.cache):
            assert _view(catalog, cache).schema_disclosure.schemas_loaded_for_session("session") == frozenset()
        replacement = _view(CatalogServiceImpl(get_shared_plugin_manager()), second.cache)
        assert replacement.schema_disclosure.schemas_loaded_for_session("session") == frozenset()
        with registry.retire_session("session"):
            assert warm.schema_disclosure.schemas_loaded_for_session("session") == frozenset()
            warm.schema_disclosure.mark_plugin_schema_loaded("session", "sink", "json")
            assert warm.schema_disclosure.schemas_loaded_for_session("session") == frozenset()
    with registry.borrow(key) as replacement:
        assert _view(catalog, replacement.cache).schema_disclosure.schemas_loaded_for_session("session") == frozenset()


def test_pending_disclosure_bounds_and_delivery_filter_do_not_fabricate_guidance():
    tracker = SchemaDisclosureTracker()
    tracker.stage_plugin_schema("large", "session", "source", "x" * 65536)
    for index in range(300):
        tracker.stage_plugin_schema(str(index), "session", "source", str(index))
    assert len(tracker._pending) == 128
    assert tracker._pending_weight <= 64 * 1024
    assert tracker.schemas_loaded_for_session("session") == frozenset()
    tracker.confirm_delivery(frozenset({"large", "299", "1"}))
    assert tracker.schemas_loaded_for_session("session") == frozenset({("source", "1")})
    assert len(tracker._pending) == 127
    tracker.stage_plugin_schema("299", "session", "source", "299")
    tracker.confirm_delivery(frozenset({"299"}))
    assert tracker.schemas_loaded_for_session("session") == frozenset({("source", "1"), ("source", "299")})


def test_disclosure_profile_and_policy_changes_require_new_delivery(tmp_path):
    from tests.unit.web.test_app import _settings

    catalog = CatalogServiceImpl(get_shared_plugin_manager())
    runtime = RuntimeWebPluginConfig.from_settings(_settings(tmp_path))
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    changed = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope=snapshot.principal_scope,
        available=snapshot.available,
        unavailable=snapshot.unavailable,
        selected=snapshot.selected,
        usable_profile_aliases=snapshot.usable_profile_aliases,
        selected_profile_aliases=snapshot.selected_profile_aliases,
        binding_generation_fingerprint=snapshot.binding_generation_fingerprint,
        authority=snapshot.authority,
    )
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(SessionCacheKey("p", "session")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        original = PolicyCatalogView(catalog, snapshot, profiles, analysis=analysis)
        original.schema_disclosure.mark_plugin_schema_loaded("session", "source", "csv")
        assert original.schema_disclosure.schemas_loaded_for_session("session")
        new_profile = PolicyCatalogView(catalog, snapshot, OperatorProfileRegistry(policy=policy, settings=runtime), analysis=analysis)
        new_policy = PolicyCatalogView(catalog, changed, profiles, analysis=analysis)
        assert new_profile.schema_disclosure.schemas_loaded_for_session("session") == frozenset()
        assert new_policy.schema_disclosure.schemas_loaded_for_session("session") == frozenset()
        new_policy.schema_disclosure.mark_plugin_schema_loaded("session", "source", "csv")
        assert new_policy.schema_disclosure.schemas_loaded_for_session("session") == frozenset({("source", "csv")})
        assert original.schema_disclosure.schemas_loaded_for_session("session") == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_outer_disclosure_requires_successful_next_provider_round(tmp_path, outcome):
    from tests.unit.web.composer.test_service import _composer_service_with_session, _make_llm_response, _make_settings

    catalog = CatalogServiceImpl(get_shared_plugin_manager())
    service, session_id = _composer_service_with_session(catalog, _make_settings())
    views = []
    bind = PolicyCatalogView.with_analysis

    def capture(view, analysis):
        bound = bind(view, analysis)
        views.append(bound)
        return bound

    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _make_llm_response(
                tool_calls=[{"id": "schema", "name": "get_plugin_schema", "arguments": {"plugin_type": "source", "name": "csv"}}]
            )
        assert views[-1].schema_disclosure.schemas_loaded_for_session(session_id) == frozenset()
        assert "schema" in views[-1].schema_disclosure._pending
        if outcome == "failure":
            raise RuntimeError("provider did not deliver")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return _make_llm_response(content="Finished schema discovery.")

    try:
        with patch.object(PolicyCatalogView, "with_analysis", capture), patch.object(service._provider_gateway, "_call_llm", provider):
            operation = service.compose("Describe the source", [], make_small_composer_state(), session_id=session_id, user_id="test-user")
            if outcome == "success":
                await operation
            else:
                with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
                    await operation
        assert calls == 2
        assert views[-1].schema_disclosure.schemas_loaded_for_session(session_id) == (
            frozenset({("source", "csv")}) if outcome == "success" else frozenset()
        )
        assert service._session_caches.snapshot().borrows == 0
    finally:
        assert service._session_engine is not None
        service._session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("rotate", [False, True])
async def test_real_planner_never_credits_old_schema_to_new_catalog_generation(tmp_path, tool_context, rotate):
    from elspeth.web.composer.pipeline_planner import PlannerDeclined
    from tests.unit.web.composer.test_pipeline_planner import _plan, _response, _ScriptedCompletion, _text_response

    manager = PluginManager()
    manager.register_builtin_plugins()
    catalog = CatalogServiceImpl(manager)
    registry = ComposerSessionCacheRegistry()

    class AdditionalTransform(PassThrough):
        name = "disclosure_generation_test"
        determinism = PassThrough.determinism
        source_file_hash = "sha256:0123456789abcdef"

    scripted = _ScriptedCompletion(
        _response(("get_plugin_schema", {"plugin_type": "source", "name": "csv"})), _text_response("Which source next?")
    )
    calls = 0

    async def provider(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and rotate:
            manager.register(create_dynamic_hookimpl([AdditionalTransform], "elspeth_get_transforms"))
        return await scripted(**kwargs)

    with registry.borrow(SessionCacheKey("p", "session")) as lease:
        view = _view(catalog, lease.cache)
        with pytest.raises(PlannerDeclined):
            await _plan(
                tmp_path=tmp_path,
                tool_context=tool_context,
                completion=provider,
                policy_override=(view, view.snapshot),
                mark_schema_loaded=partial(view.schema_disclosure.mark_plugin_schema_loaded, "session"),
            )
        assert calls == 2
        assert view.schema_disclosure.schemas_loaded_for_session("session") == (frozenset() if rotate else frozenset({("source", "csv")}))
        fresh = _view(catalog, lease.cache)
        assert fresh.schema_disclosure.schemas_loaded_for_session("session") == (frozenset() if rotate else frozenset({("source", "csv")}))
        fresh.schema_disclosure.mark_plugin_schema_loaded("session", "source", "csv")
        assert fresh.schema_disclosure.schemas_loaded_for_session("session") == frozenset({("source", "csv")})

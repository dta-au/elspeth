"""Raw reference parity and complete dependency identities for shared analysis."""

from __future__ import annotations

import sys
import threading
from dataclasses import replace
from unittest.mock import patch

import pytest

from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager, get_shared_plugin_manager, scoped_plugin_manager
from elspeth.plugins.transforms.passthrough import PassThrough
from elspeth.web.composer.analysis import ComposerAnalysis, _audited_builtin_classes, _eligible, registry_revision
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.composer.state import CompositionState, ValidationSummary
from tests.fixtures.composer_session_cache import (
    SCENARIO_NAMES,
    count_composer_work,
    make_composer_validation_scenario,
    make_small_composer_state,
)


def _registry() -> ComposerSessionCacheRegistry:
    return ComposerSessionCacheRegistry()


@pytest.mark.parametrize("scenario_name", SCENARIO_NAMES[:3])
def test_raw_reference_cold_warm_and_reconstructed_content_parity(scenario_name: str) -> None:
    # The factory's Literal contract is preserved by parametrizing its public
    # tuple directly; no arbitrary external scenario name reaches it.
    scenario = next(make_composer_validation_scenario(name) for name in SCENARIO_NAMES if name == scenario_name)
    state = scenario.state
    expected = state.validate()
    registry = _registry()
    with registry.borrow(SessionCacheKey("principal", "session")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        with count_composer_work() as counts:
            assert analysis.validate_raw(state) == expected
            assert analysis.validate_raw(state) == expected
            reconstructed = CompositionState.from_dict(state.to_dict())
            assert analysis.validate_raw(reconstructed) == expected
        assert counts.validation_walks == 1
        assert registry.snapshot().hits == 2


def test_same_version_changed_content_and_version_only_change() -> None:
    state = make_small_composer_state()
    registry = _registry()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        with count_composer_work() as counts:
            analysis.validate_raw(state)
            analysis.validate_raw(replace(state, version=state.version + 1))
            changed = replace(state, outputs=(), version=state.version)
            assert analysis.validate_raw(changed) == changed.validate()
        # One cold original, one changed walk, one explicit reference walk.
        assert counts.validation_walks == 3
        assert registry.snapshot().hits == 1


@pytest.mark.asyncio
async def test_async_analysis_binds_explicit_manager_inside_worker() -> None:
    state = make_small_composer_state()
    manager = get_shared_plugin_manager()
    registry = _registry()
    expected = state.validate()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache, manager=manager)
        with scoped_plugin_manager(PluginManager()):
            assert await analysis.validate_raw_async(state) == expected
            assert await analysis.validate_raw_async(state) == expected
    assert registry.snapshot().hits == 1
    await registry.finish_shutdown()


@pytest.mark.asyncio
async def test_async_analysis_builds_graph_identity_off_event_loop() -> None:
    state = make_small_composer_state()
    registry = _registry()
    loop_thread = threading.get_ident()
    hash_threads: list[int] = []

    def hash_in_worker(current: CompositionState) -> str:
        hash_threads.append(threading.get_ident())
        return composition_content_hash(current)

    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        with patch("elspeth.web.composer.analysis.composition_content_hash", side_effect=hash_in_worker):
            await analysis.validate_raw_async(state)
            await analysis.validate_raw_async(state)
    assert len(hash_threads) == 2
    assert all(thread != loop_thread for thread in hash_threads)
    await registry.finish_shutdown()


def test_registry_replacement_and_new_registration_invalidate_results() -> None:
    state = make_small_composer_state()
    manager = PluginManager()
    manager.register_builtin_plugins()
    registry = _registry()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache, manager=manager)
        analysis.validate_raw(state)

        class NewTransform(PassThrough):
            name = "cache_test_unreviewed"
            determinism = PassThrough.determinism

        manager.register(create_dynamic_hookimpl([NewTransform], "elspeth_get_transforms"))
        with count_composer_work() as counts:
            analysis.validate_raw(state)
            analysis.validate_raw(state)
        assert counts.validation_walks == 1
        assert registry.snapshot().misses == 2
        # Same registered name, different class must bypass even if it derives
        # from an audited builtin and carries the same declarations.
        replaced_state = replace(state, nodes=(replace(state.nodes[0], plugin=NewTransform.name), *state.nodes[1:]))
        assert analysis._key(replaced_state) is None


def test_registry_change_during_computation_does_not_admit_stale_key() -> None:
    state = make_small_composer_state()
    manager = PluginManager()
    manager.register_builtin_plugins()
    registry = _registry()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache, manager=manager)

        class NewTransform(PassThrough):
            name = "cache_test_midwalk"
            determinism = PassThrough.determinism

        def compute() -> ValidationSummary:
            result = state.validate()
            manager.register(create_dynamic_hookimpl([NewTransform], "elspeth_get_transforms"))
            return result

        analysis.validate_profile_summary(state, compute, snapshot_hash="policy", profile_generation=1)
        assert registry.snapshot().retained_entries == 0
        assert registry.snapshot().pending_entries == 0


def test_profile_summary_public_revisions_and_lowered_dependency_bypass() -> None:
    state = make_small_composer_state()
    registry = _registry()
    calls = [0]

    def compute() -> ValidationSummary:
        calls[0] += 1
        return ValidationSummary(True, ())

    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        for snapshot, generation in (("a", 1), ("a", 1), ("b", 1), ("b", 2)):
            analysis.validate_profile_summary(state, compute, snapshot_hash=snapshot, profile_generation=generation)
        assert calls == [3]
        lowered = replace(state, outputs=(replace(state.outputs[0], plugin="database"),))
        analysis.validate_profile_summary(state, compute, snapshot_hash="a", profile_generation=1, dependency_state=lowered)
        analysis.validate_profile_summary(state, compute, snapshot_hash="a", profile_generation=1, dependency_state=lowered)
        assert calls == [5]


@pytest.mark.parametrize("kind,name", (("source", "power_automate"), ("sink", "power_automate"), ("sink", "database")))
def test_ambient_source_and_sink_dependencies_bypass(kind: str, name: str) -> None:
    state = make_small_composer_state()
    if kind == "source":
        state = replace(state, sources={key: replace(value, plugin=name) for key, value in state.sources.items()})
    else:
        state = replace(state, outputs=(replace(state.outputs[0], plugin=name),))
    assert not _eligible(state, registry_revision(get_shared_plugin_manager()))


@pytest.mark.parametrize("enabled", (True, False))
def test_langfuse_transform_bypasses_even_when_disabled(enabled: bool) -> None:
    state = make_small_composer_state()
    node = replace(state.nodes[0], plugin="llm", options={"tracing": {"provider": "langfuse", "enabled": enabled}})
    state = replace(state, nodes=(node,))
    assert not _eligible(state, registry_revision(get_shared_plugin_manager()))


def test_unknown_and_same_name_subclass_fail_exact_identity() -> None:
    state = make_small_composer_state()
    manager = PluginManager()

    class Replacement(PassThrough):
        name = "passthrough"
        determinism = PassThrough.determinism

    manager.register(create_dynamic_hookimpl([Replacement], "elspeth_get_transforms"))
    state = replace(state, sources={}, outputs=(), nodes=(replace(state.nodes[0], plugin="passthrough"),))
    assert not _eligible(state, registry_revision(manager))
    state = replace(state, nodes=(replace(state.nodes[0], plugin="unknown"),))
    assert not _eligible(state, registry_revision(manager))


def test_eligibility_does_not_import_missing_optional_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = get_shared_plugin_manager()
    canonical = "elspeth.plugins.transforms.aws.textract_document_analysis"
    assert canonical in sys.modules
    monkeypatch.delitem(sys.modules, canonical)
    before = frozenset(sys.modules)
    assert _eligible(make_small_composer_state(), registry_revision(manager))
    assert frozenset(sys.modules) == before
    assert all(cls.__name__ != "AWSTextractDocumentAnalysis" for cls in _audited_builtin_classes())


@pytest.mark.parametrize("bad", (float("nan"), float("inf"), 2**60, "\ud800"))
def test_noncanonical_drafts_bypass_key_and_keep_reference_diagnostics(bad: object) -> None:
    state = make_small_composer_state()
    state = replace(state, nodes=(replace(state.nodes[0], options={**state.nodes[0].options, "invalid_option": bad}), *state.nodes[1:]))
    expected = state.validate()
    registry = _registry()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        assert analysis.validate_raw(state) == expected
        assert registry.snapshot().retained_entries == 0


def test_key_programming_failure_and_probe_cleanup_failure_are_not_cached() -> None:
    state = make_small_composer_state()
    registry = _registry()
    with registry.borrow(SessionCacheKey("p", "s")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        with (
            patch("elspeth.web.composer.analysis.composition_content_hash", side_effect=ValueError("programming defect")),
            pytest.raises(ValueError, match="programming defect"),
        ):
            analysis.validate_raw(state)
        with (
            patch.object(CompositionState, "validate", side_effect=RuntimeError("probe close failed")),
            pytest.raises(RuntimeError, match="probe close failed"),
        ):
            analysis.validate_raw(state)
        assert registry.snapshot().retained_entries == registry.snapshot().pending_entries == registry.snapshot().pins == 0
        assert analysis.validate_raw(state) == state.validate()

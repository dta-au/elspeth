"""Tool envelopes reuse exact graph analysis without retaining rejection feedback."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from threading import get_ident
from uuid import uuid4

import pytest

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.composer.tools._common import (
    ToolContext,
    _candidate_rejection_result,
    _discovery_result,
    _mutation_result,
    _post_mutation_invariant_error,
    normalize_tool_result_validation,
)
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context, execute_tool
from elspeth.web.composer.tools.sessions import _handle_request_interpretation_review
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.fixtures.composer_session_cache import count_composer_work, make_small_composer_state
from tests.unit.web.composer.test_request_interpretation_review_tool import (
    _empty_list_interpretation_events,
    _fake_create_pending_interpretation_event,
    _llm_node,
    _now,
    _state_with,
)


@pytest.fixture
def policy_catalog() -> Iterator[PolicyCatalogView]:
    manager = get_shared_plugin_manager()
    full = CatalogServiceImpl(manager)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(full)
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(SessionCacheKey(snapshot.principal_scope, "tool-analysis-test")) as lease:
        analysis = ComposerAnalysis(lease.cache, manager=manager)
        yield PolicyCatalogView.for_trained_operator(full, snapshot, analysis=analysis)


@pytest.mark.parametrize("planner_dispatch", [False, True])
def test_discovery_does_not_reuse_candidate_rejection_as_state_validation(
    policy_catalog: PolicyCatalogView,
    planner_dispatch: bool,
) -> None:
    state = make_small_composer_state()
    expected = state.validate()
    rejection = _candidate_rejection_result(
        state,
        "This candidate was refused.",
        error_code="candidate_test_refused",
        rejected_component=None,
        catalog=policy_catalog,
    )
    with count_composer_work() as counts:
        for _ in range(3):
            if planner_dispatch:
                result = execute_discovery_tool_with_context(
                    "get_expression_grammar",
                    {},
                    state,
                    ToolContext(
                        catalog=policy_catalog,
                        plugin_snapshot=policy_catalog.snapshot,
                        current_validation=rejection.validation,
                    ),
                )
            else:
                result = execute_tool(
                    "get_expression_grammar",
                    {},
                    state,
                    policy_catalog,
                    plugin_snapshot=policy_catalog.snapshot,
                    prior_validation=rejection.validation,
                )
            assert result.validation == expected
            assert all(entry.component != "rejected_mutation" for entry in result.validation.errors)
    assert counts.validation_walks == 1


def test_normalization_rechecks_replaced_state_under_the_same_snapshot(policy_catalog: PolicyCatalogView) -> None:
    state = make_small_composer_state()
    changed = replace(state, outputs=())
    expected_changed = changed.validate()
    assert not expected_changed.is_valid
    with count_composer_work() as counts:
        initial = normalize_tool_result_validation(_discovery_result(state, {}, catalog=policy_catalog), policy_catalog)
        assert initial._validation_snapshot_hash == policy_catalog.snapshot.snapshot_hash
        changed_result = normalize_tool_result_validation(replace(initial, updated_state=changed), policy_catalog)
        assert changed_result.validation == expected_changed
        assert not changed_result.validation.is_valid
    assert counts.validation_walks == 2


def test_failed_mutation_feedback_does_not_poison_following_discovery(policy_catalog: PolicyCatalogView) -> None:
    state = make_small_composer_state()
    expected = state.validate()
    with count_composer_work() as counts:
        failed = execute_tool(
            "remove_output",
            {"sink_name": "missing-output"},
            state,
            policy_catalog,
            plugin_snapshot=policy_catalog.snapshot,
        )
        assert not failed.success
        assert failed.updated_state is state
        assert failed.validation.errors[0].component == "rejected_mutation"
        discovery = execute_tool(
            "get_expression_grammar",
            {},
            state,
            policy_catalog,
            plugin_snapshot=policy_catalog.snapshot,
            prior_validation=failed.validation,
        )
        assert discovery.validation == expected
    assert counts.validation_walks == 1


def test_failed_replacement_keeps_only_candidate_feedback(policy_catalog: PolicyCatalogView) -> None:
    state = make_small_composer_state()
    expected = state.validate()
    failed = execute_tool(
        "set_pipeline",
        {
            "source": {
                "plugin": "csv",
                "on_success": "output",
                "on_validation_failure": "",
                "options": {"path": "rows.csv", "schema": {"mode": "observed"}},
            },
            "nodes": [],
            "edges": [],
            "outputs": [],
        },
        state,
        policy_catalog,
        plugin_snapshot=policy_catalog.snapshot,
    )
    assert not failed.success
    assert failed.updated_state is state
    assert failed._state_validation_withheld
    assert all(entry.component == "rejected_mutation" for entry in failed.validation.errors)
    discovery = execute_tool(
        "get_expression_grammar",
        {},
        state,
        policy_catalog,
        plugin_snapshot=policy_catalog.snapshot,
        prior_validation=failed.validation,
    )
    assert discovery.validation == expected


def test_rejection_only_normalization_does_not_validate_the_saved_state(policy_catalog: PolicyCatalogView) -> None:
    state = make_small_composer_state()
    with count_composer_work() as counts:
        rejection = _candidate_rejection_result(
            state,
            "This candidate was refused.",
            error_code="candidate_test_refused",
            rejected_component=None,
            catalog=policy_catalog,
        )
        normalized = normalize_tool_result_validation(rejection, policy_catalog)
    assert normalized.validation == rejection.validation
    assert normalized._state_validation_withheld
    assert counts.validation_walks == 0


def test_raw_invariant_mutation_and_discovery_share_one_summary(policy_catalog: PolicyCatalogView) -> None:
    state = make_small_composer_state()
    expected = state.validate()
    context = ToolContext(catalog=policy_catalog, plugin_snapshot=policy_catalog.snapshot)
    assert context.analysis is policy_catalog.analysis
    with count_composer_work() as counts:
        assert _post_mutation_invariant_error(state, analysis=context.analysis) is None
        mutation = _mutation_result(state, (), catalog=policy_catalog)
        normalized = normalize_tool_result_validation(mutation, policy_catalog)
        discovery = _discovery_result(state, {"available": []}, catalog=policy_catalog)
        assert mutation.validation == normalized.validation == discovery.validation == expected
    assert counts.validation_walks == 1


def test_mutation_content_gets_a_new_summary_without_repeated_walks(policy_catalog: PolicyCatalogView) -> None:
    state = make_small_composer_state()
    with count_composer_work() as counts:
        mutation = execute_tool(
            "set_metadata",
            {"patch": {"name": "Updated pipeline"}},
            state,
            policy_catalog,
            plugin_snapshot=policy_catalog.snapshot,
        )
        assert mutation.success
        assert mutation.updated_state.metadata.name == "Updated pipeline"
        for _ in range(3):
            discovery = execute_tool(
                "get_expression_grammar",
                {},
                mutation.updated_state,
                policy_catalog,
                plugin_snapshot=policy_catalog.snapshot,
            )
            assert discovery.validation == mutation.validation
    assert counts.validation_walks == 2


@pytest.mark.asyncio
async def test_catalog_bound_review_keeps_graph_work_off_the_event_loop(
    policy_catalog: PolicyCatalogView,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.composer.tools import sessions

    state = _state_with(_llm_node())
    expected = state.validate()
    loop_thread = get_ident()
    worker_threads: list[int] = []
    original_check = sessions._assert_affected_component

    def checked_component(*args, **kwargs):
        worker_threads.append(get_ident())
        return original_check(*args, **kwargs)

    monkeypatch.setattr(sessions, "_assert_affected_component", checked_component)
    result = await _handle_request_interpretation_review(
        arguments={
            "affected_node_id": "rate_node",
            "kind": "vague_term",
            "user_term": "cool",
            "llm_draft": "Visually appealing.",
        },
        state=state,
        session_id=uuid4(),
        composition_state_id=uuid4(),
        tool_call_id="call-reviewed-worker",
        now=_now(),
        per_term_cap=3,
        per_session_day_cap=10,
        model_identifier="test-model",
        model_version="test-version",
        provider="test-provider",
        composer_skill_hash="test-skill",
        create_pending_interpretation_event=_fake_create_pending_interpretation_event,
        list_interpretation_events=_empty_list_interpretation_events,
        catalog=policy_catalog,
    )
    assert result.success
    assert result.validation == expected
    assert worker_threads and all(thread_id != loop_thread for thread_id in worker_threads)

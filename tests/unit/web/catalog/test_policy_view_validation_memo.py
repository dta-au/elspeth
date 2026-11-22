"""The policy view's per-round memo of ``validate_composition_state``.

One view is built per compose call or per request. With the memo on, a second
validation of the identical state object in the same mutation epoch returns
the first result object. The key is object identity on purpose: two states
whose dict keys differ only in order are ``==`` and share a content hash, but
authoring errors come out in key order, so they must get their own verdicts.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic import SecretBytes

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog import policy_view as policy_view_module
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy import validation as policy_validation
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig


class _Inventory:
    def has_server_ref(self, name: str) -> bool:
        return False

    def has_user_ref(self, principal: str, name: str) -> bool:
        return False

    def has_ref(self, principal: str, name: str) -> bool:
        return False

    def server_generation(self, name: str) -> str | None:
        return None

    def user_generation(self, principal: str, name: str) -> str | None:
        return None


def _policy_parts() -> tuple[PluginAvailabilitySnapshot, OperatorProfileRegistry]:
    settings = WebSettings(
        composer_max_composition_turns=4,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=4,
        composer_timeout_seconds=60,
        composer_rate_limit_per_minute=20,
        shareable_link_signing_key=SecretBytes(b"0123456789abcdef0123456789abcdef"),
        llm_profiles={"task-role": {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0"}},
        default_llm_profile="task-role",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=create_catalog_service(),
        profiles=profiles,
        principal_scope="local:alice",
        secret_inventory=_Inventory(),
        generation_key=b"policy-view-memo-test-key",
    )
    return snapshot, profiles


def _view(*, memo: bool) -> PolicyCatalogView:
    snapshot, profiles = _policy_parts()
    return PolicyCatalogView(create_catalog_service(), snapshot, profiles, validation_memo_enabled=memo)


def _query(template: str) -> dict[str, Any]:
    """A query whose template reads an undeclared row field: one error naming the query."""
    return {
        "input_fields": {"f1": "text"},
        "response_format": "structured",
        "output_fields": [{"suffix": "label", "type": "string"}],
        "template": template,
    }


def _state(queries: dict[str, Any]) -> CompositionState:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    state = state.with_source(
        SourceSpec(
            plugin="csv",
            on_success="c0",
            on_validation_failure="discard",
            options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
        )
    )
    state = state.with_node(
        NodeSpec(
            id="llm_00",
            node_type="transform",
            plugin="llm",
            input="c0",
            on_success="final_out",
            on_error="discard",
            options={
                "profile": "task-role",
                "queries": queries,
                "required_input_fields": [],
                "schema": {"mode": "observed"},
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )
    return state.with_output(
        OutputSpec(
            name="final_out",
            plugin="csv",
            options={
                "path": "outputs/o.csv",
                "schema": {"mode": "observed"},
                "mode": "write",
                "collision_policy": "auto_increment",
            },
            on_write_failure="discard",
        )
    )


def _key_order_pair() -> tuple[CompositionState, CompositionState]:
    qa, qb = _query("A {{ row.nope_a }}"), _query("B {{ row.nope_b }}")
    return _state({"qa": qa, "qb": qb}), _state({"qb": qb, "qa": qa})


def _error_order(result: policy_validation.ProfileAwareValidationResult) -> list[tuple[str | None, str | None, str]]:
    return [(error.component, error.error_code, error.message) for error in result.validation.errors]


class _CountingValidation:
    def __init__(self, *, raise_first: bool = False) -> None:
        self.calls = 0
        self._raise_first = raise_first
        self._real = policy_validation.validate_authored_composition_state

    def __call__(self, *args: Any, **kwargs: Any) -> policy_validation.ProfileAwareValidationResult:
        self.calls += 1
        if self._raise_first and self.calls == 1:
            raise RuntimeError("synthetic validation failure")
        return self._real(*args, **kwargs)


class _RecordingCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, object]]] = []

    def add(self, value: int, attributes: dict[str, object]) -> None:
        self.calls.append((value, dict(attributes)))


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> _CountingValidation:
    counter = _CountingValidation()
    monkeypatch.setattr(policy_validation, "validate_authored_composition_state", counter)
    return counter


def test_the_same_state_object_is_validated_once_per_epoch(counting: _CountingValidation, monkeypatch: pytest.MonkeyPatch) -> None:
    reused = _RecordingCounter()
    monkeypatch.setattr(policy_view_module, "VALIDATION_REUSE_COUNTER", reused)
    view = _view(memo=True)
    state, _ = _key_order_pair()

    first = view.validate_composition_state(state)
    second = view.validate_composition_state(state)

    assert counting.calls == 1
    assert second is first
    assert reused.calls == [(1, {"layer": "policy_view"})]


# T8 ------------------------------------------------------------------------


def test_states_differing_only_in_key_order_each_get_their_own_verdict(counting: _CountingValidation) -> None:
    left, right = _key_order_pair()
    # The collision the identity key exists for: equal and same content hash.
    assert left == right
    assert composition_content_hash(left) == composition_content_hash(right)
    fresh = _view(memo=False)
    fresh_left, fresh_right = _error_order(fresh.validate_composition_state(left)), _error_order(fresh.validate_composition_state(right))
    # Positive control: the two verdicts really differ in order, so a shared
    # verdict would be caught below.
    assert fresh_left != fresh_right
    assert sorted(fresh_left) == sorted(fresh_right)
    memo = _view(memo=True)

    memo_left = memo.validate_composition_state(left)
    memo_right = memo.validate_composition_state(right)

    assert memo_right is not memo_left
    assert _error_order(memo_left) == fresh_left
    assert _error_order(memo_right) == fresh_right
    assert memo.validate_composition_state(left) is memo_left
    assert memo.validate_composition_state(right) is memo_right


# T9 ------------------------------------------------------------------------


def test_an_epoch_bump_clears_the_memo(counting: _CountingValidation) -> None:
    view = _view(memo=True)
    state, _ = _key_order_pair()
    first = view.validate_composition_state(state)

    view.record_mutation_tool_end()
    second = view.validate_composition_state(state)

    assert counting.calls == 2
    assert second is not first
    assert _error_order(second) == _error_order(first)


def test_a_mutation_tool_call_through_execute_tool_clears_the_memo(counting: _CountingValidation) -> None:
    view = _view(memo=True)
    state, _ = _key_order_pair()
    view.validate_composition_state(state)
    epoch = view.mutation_epoch.value

    result = execute_tool("patch_node_options", {"node_id": "absent", "patch": {"x": 1}}, state, view, plugin_snapshot=view.snapshot)
    # The refused call hands back the same state object and validates it
    # itself before the epoch moves; the next validation after the call must
    # compute again.
    after_call = counting.calls
    view.validate_composition_state(state)

    assert result.success is False and result.updated_state is state
    assert view.mutation_epoch.value == epoch + 1
    assert counting.calls == after_call + 1


def test_a_failed_validation_is_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _CountingValidation(raise_first=True)
    monkeypatch.setattr(policy_validation, "validate_authored_composition_state", counter)
    view = _view(memo=True)
    state, _ = _key_order_pair()

    with pytest.raises(RuntimeError, match="synthetic validation failure"):
        view.validate_composition_state(state)
    first = view.validate_composition_state(state)
    second = view.validate_composition_state(state)

    assert counter.calls == 2
    assert second is first


def test_the_memo_holds_at_most_eight_states(counting: _CountingValidation) -> None:
    view = _view(memo=True)
    states = [_state({f"q{index}": _query("A {{ row.f1 }}")}) for index in range(9)]
    for state in states:
        view.validate_composition_state(state)
    assert counting.calls == 9

    view.validate_composition_state(states[-1])
    assert counting.calls == 9
    view.validate_composition_state(states[0])
    assert counting.calls == 10


# T10 -------------------------------------------------------------------------


def test_memo_off_validates_every_call(counting: _CountingValidation) -> None:
    view = _view(memo=False)
    state, _ = _key_order_pair()

    first = view.validate_composition_state(state)
    second = view.validate_composition_state(state)

    assert counting.calls == 2
    assert second is not first


@pytest.mark.parametrize("enabled", [True, False])
def test_the_factory_hands_the_switch_to_every_view_it_builds(counting: _CountingValidation, enabled: bool) -> None:
    snapshot, profiles = _policy_parts()
    factory = PluginPolicyContextFactory(
        create_catalog_service(),
        lambda _user_id: snapshot,
        profiles,
        trained_operator_mode=False,
        validation_reuse_enabled=enabled,
    )
    _, view = factory.build("alice")
    state, _ = _key_order_pair()

    view.validate_composition_state(state)
    view.validate_composition_state(state)

    assert counting.calls == (1 if enabled else 2)


# Round 2 -------------------------------------------------------------------


def test_a_verdict_whose_validation_saw_a_mutation_end_is_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutation-kind tool call that ends while the validation runs leaves nothing memoized."""
    view = _view(memo=True)
    real = policy_validation.validate_authored_composition_state
    calls: list[int] = []

    def mutating(*args: Any, **kwargs: Any) -> policy_validation.ProfileAwareValidationResult:
        calls.append(1)
        if len(calls) == 1:
            view.record_mutation_tool_end()
        return real(*args, **kwargs)

    monkeypatch.setattr(policy_validation, "validate_authored_composition_state", mutating)
    state, _ = _key_order_pair()

    view.validate_composition_state(state)

    assert view._validation_memo == {}
    view.validate_composition_state(state)
    view.validate_composition_state(state)
    assert len(calls) == 2


def _is_frozen_dataclass(value: object) -> bool:
    """True when assigning to the value's first field raises ``FrozenInstanceError``."""
    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        return False
    try:
        value.__setattr__(dataclasses.fields(value)[0].name, None)
    except dataclasses.FrozenInstanceError:
        return True
    return False


def test_the_shared_verdict_cannot_be_changed_by_a_caller(counting: _CountingValidation) -> None:
    """F6 for this layer: a memo hit hands every caller one object, so it must be immutable.

    Every container in the result is a tuple and every element a frozen
    dataclass; the two states are ``CompositionState`` values, frozen by
    construction.
    """
    view = _view(memo=True)
    state, _ = _key_order_pair()
    first = view.validate_composition_state(state)
    summary = first.validation
    assert summary.errors  # the checks below have something to check

    assert _is_frozen_dataclass(first) and _is_frozen_dataclass(summary)
    assert _is_frozen_dataclass(first.authored_state) and _is_frozen_dataclass(first.executable_state)
    for container in (
        first.policy_findings,
        summary.errors,
        summary.warnings,
        summary.suggestions,
        summary.edge_contracts,
        summary.semantic_contracts,
    ):
        assert type(container) is tuple
        assert all(_is_frozen_dataclass(item) for item in container)
    assert view.validate_composition_state(state) is first

"""LLM security gates derive their subject from the capability registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from elspeth.contracts import SourceProtocol, TransformProtocol
from elspeth.contracts.plugin_capabilities import PluginCapability
from elspeth.plugins.infrastructure.manager import PluginManager, get_shared_plugin_manager
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution._validation_materialization import (
    validate_llm_base_url_policy,
    validate_llm_retry_budget_policy,
    validate_llm_tracing_policy,
)
from elspeth.web.execution._validation_model import AuthoredValidatedState, MaterializedYaml, PhaseFailure, PolicyLoweredState
from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SHIELD_USER_TERM,
    PROMPT_SHIELD_WARNING_DRAFT,
    _llm_has_authorized_shield_upstream,
    _output_stream_graph,
    prompt_shield_recommendation_warning_pairs,
    prompt_shield_state_for_node,
    reconcile_authoritative_reviews,
)
from elspeth.web.plugin_policy.coverage import node_has_capability, source_has_capability

_ALTERNATE_LLM_PLUGIN = "alternate_llm"
_ALTERNATE_LLM_SOURCE = "alternate_llm_source"


class _AlternateNameRegistry:
    """Registry view exposing the canonical LLM transform under another name."""

    def __init__(self, delegate: PluginManager) -> None:
        self._delegate = delegate
        self._llm_transform = delegate.get_transform_by_name("llm")
        self._llm_source = delegate.get_source_by_name("llm")

    def get_transform_by_name(self, name: str) -> type[TransformProtocol]:
        if name == _ALTERNATE_LLM_PLUGIN:
            return self._llm_transform
        return self._delegate.get_transform_by_name(name)

    def get_source_by_name(self, name: str) -> type[SourceProtocol]:
        if name == _ALTERNATE_LLM_SOURCE:
            return self._llm_source
        return self._delegate.get_source_by_name(name)


@pytest.fixture
def alternate_llm_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _AlternateNameRegistry(get_shared_plugin_manager())
    monkeypatch.setattr("elspeth.web.plugin_policy.coverage.get_shared_plugin_manager", lambda: registry)


def _llm_node(*, options: dict[str, object]) -> NodeSpec:
    return NodeSpec(
        id="alternate-model",
        node_type="transform",
        plugin=_ALTERNATE_LLM_PLUGIN,
        input="node_in",
        on_success="primary",
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(
    node: NodeSpec,
    *,
    source: SourceSpec | None = None,
    upstream_nodes: tuple[NodeSpec, ...] = (),
) -> CompositionState:
    return CompositionState(
        source=source
        or SourceSpec(
            plugin="csv",
            on_success="node_in",
            options={"path": "rows.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(*upstream_nodes, node),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _materialized(state: CompositionState) -> MaterializedYaml:
    return MaterializedYaml(
        authored=AuthoredValidatedState(
            policy=PolicyLoweredState(
                authored_state=state,
                state=state,
                profiled_s3_audit_identities=(),
                profiled_textract_audit_identities=(),
                operator_resolved_model_node_ids=frozenset(),
            ),
            all_secret_refs=(),
            env_ref_names=frozenset(),
            semantic_contracts=(),
        ),
        materialized_state=state,
        pipeline_yaml="sources: {}\nsinks: {}\n",
    )


@pytest.mark.parametrize(
    ("validator", "options"),
    [
        pytest.param(validate_llm_base_url_policy, {"base_url": "https://attacker.invalid/v1"}, id="base-url"),
        pytest.param(validate_llm_tracing_policy, {"tracing": {"endpoint": "https://attacker.invalid"}}, id="tracing"),
        pytest.param(validate_llm_retry_budget_policy, {"queries": [{}]}, id="retry-budget"),
    ],
)
def test_llm_policy_rejects_capability_declared_under_another_name(
    alternate_llm_registry: None,
    validator: Callable[[MaterializedYaml], object],
    options: dict[str, object],
) -> None:
    node = _llm_node(options=options)
    assert node_has_capability(node, PluginCapability.LLM), "positive control: the registry must classify the alias as LLM"

    assert isinstance(validator(_materialized(_state(node))), PhaseFailure)


@pytest.mark.parametrize(
    ("validator", "options"),
    [
        pytest.param(validate_llm_base_url_policy, {"base_url": "https://attacker.invalid/v1"}, id="base-url"),
        pytest.param(validate_llm_tracing_policy, {"tracing": {"endpoint": "https://attacker.invalid"}}, id="tracing"),
    ],
)
def test_llm_egress_policy_rejects_source_capability_declared_under_another_name(
    alternate_llm_registry: None,
    validator: Callable[[MaterializedYaml], object],
    options: dict[str, object],
) -> None:
    source = SourceSpec(
        plugin=_ALTERNATE_LLM_SOURCE,
        on_success="node_in",
        options=options,
        on_validation_failure="discard",
    )
    assert source_has_capability(source, PluginCapability.LLM), "positive control: the source alias must be LLM-capable"

    assert isinstance(validator(_materialized(_state(_llm_node(options={}), source=source))), PhaseFailure)


@pytest.mark.parametrize(
    ("validator", "options"),
    [
        pytest.param(validate_llm_base_url_policy, {"base_url": "https://attacker.invalid/v1"}, id="base-url"),
        pytest.param(validate_llm_tracing_policy, {"tracing": {"endpoint": "https://attacker.invalid"}}, id="tracing"),
        pytest.param(validate_llm_retry_budget_policy, {"queries": [{}]}, id="retry-budget"),
    ],
)
def test_llm_policy_ignores_transform_without_llm_capability(
    alternate_llm_registry: None,
    validator: Callable[[MaterializedYaml], object],
    options: dict[str, object],
) -> None:
    node = replace(_llm_node(options=options), plugin="web_scrape")
    assert not node_has_capability(node, PluginCapability.LLM), "positive control: web_scrape must not be LLM-capable"

    assert not isinstance(validator(_materialized(_state(node))), PhaseFailure)


def test_fanout_guard_includes_llm_capability_declared_under_another_name(
    alternate_llm_registry: None,
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "rows.txt"
    data_path.write_text("".join(f"row {index}\n" for index in range(101)), encoding="utf-8")
    node = _llm_node(options={"provider": "openrouter", "model": "openai/gpt-4o-mini"})
    source = SourceSpec(
        plugin="text",
        on_success="node_in",
        options={"path": str(data_path), "column": "body", "schema": {"mode": "observed"}},
        on_validation_failure="discard",
    )
    assert node_has_capability(node, PluginCapability.LLM), "positive control: the registry must classify the alias as LLM"

    guard = evaluate_execution_fanout_guard(_state(node, source=source), data_dir=tmp_path)

    assert guard is not None
    assert [risk.node_id for risk in guard.risks] == [node.id]


def _upstream_transform(
    *,
    node_id: str,
    plugin: str,
    input_stream: str,
    on_success: str,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin=plugin,
        input=input_stream,
        on_success=on_success,
        on_error="discard",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def test_prompt_shield_advisory_traces_remote_content_for_alternate_llm_name(
    alternate_llm_registry: None,
) -> None:
    scrape = _upstream_transform(node_id="fetch", plugin="web_scrape", input_stream="node_in", on_success="remote_rows")
    node = replace(_llm_node(options={"prompt_template": "Summarise {{ row.content }}"}), input="remote_rows")

    warnings = prompt_shield_recommendation_warning_pairs(_state(node, upstream_nodes=(scrape,)))

    assert len(warnings) == 1
    assert warnings[0][0] == f"node:{node.id}"
    assert "produced by web_scrape" in warnings[0][1]
    assert "without an authorized prompt-injection shield between them" in warnings[0][1]


def test_prompt_shield_advisory_credits_shield_for_alternate_llm_name(
    alternate_llm_registry: None,
) -> None:
    shield = _upstream_transform(
        node_id="shield",
        plugin="azure_prompt_shield",
        input_stream="node_in",
        on_success="shielded_rows",
    )
    node = replace(_llm_node(options={"prompt_template": "Summarise {{ row.content }}"}), input="shielded_rows")
    state = _state(node, upstream_nodes=(shield,))

    assert _llm_has_authorized_shield_upstream(node, _output_stream_graph(state.nodes))
    assert prompt_shield_recommendation_warning_pairs(state) == ()


def test_prompt_shield_state_is_fail_safe_for_unshielded_alternate_llm_name(
    alternate_llm_registry: None,
) -> None:
    node = _llm_node(options={"prompt_template": "Summarise {{ row.content }}"})

    assert prompt_shield_state_for_node(node, (node,), shield_available=False) == "C"


def test_reconciliation_retires_moot_shield_review_for_alternate_llm_name(
    alternate_llm_registry: None,
) -> None:
    requirement = {
        "id": "prompt-shield-review",
        "kind": "pipeline_decision",
        "user_term": PROMPT_SHIELD_USER_TERM,
        "status": "pending",
        "draft": PROMPT_SHIELD_WARNING_DRAFT,
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }
    node = replace(
        _llm_node(
            options={
                "prompt_template": "Summarise {{ row.content }}",
                INTERPRETATION_REQUIREMENTS_KEY: [requirement],
            }
        ),
        input="shielded_rows",
    )
    shield = _upstream_transform(
        node_id="shield",
        plugin="azure_prompt_shield",
        input_stream="node_in",
        on_success="shielded_rows",
    )
    previous = _state(node)
    proposed = _state(node, upstream_nodes=(shield,))

    reconciled = reconcile_authoritative_reviews(previous, proposed)
    reconciled_node = next(candidate for candidate in reconciled.nodes if candidate.id == node.id)

    assert INTERPRETATION_REQUIREMENTS_KEY not in reconciled_node.options

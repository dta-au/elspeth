"""Execution secret-approval guard (elspeth-f3c1aafd25) — the out-of-band half
of the secret-wiring policy: no run resolves wired secrets until the caller
re-submits the deterministic ack token bound to the exact disclosure set."""

from __future__ import annotations

from typing import Any, cast

from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution.secret_guard import (
    SECRET_GUARD_AUDIT_COMMENT,
    annotate_pipeline_yaml_with_secret_guard,
    evaluate_execution_secret_guard,
)


def _source(options: dict[str, object] | None = None, *, plugin: str = "csv") -> SourceSpec:
    return SourceSpec(
        plugin=plugin,
        on_success="node_in",
        options=options or {"path": "/data/in.csv"},
        on_validation_failure="discard",
    )


def _node(options: dict[str, object] | None = None, *, node_id: str = "node", plugin: str = "value_transform") -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=cast(Any, "transform"),
        plugin=plugin,
        input="node_in",
        on_success="primary",
        on_error="discard",
        options=options or {},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(
    *,
    source: SourceSpec | None = None,
    nodes: tuple[NodeSpec, ...] = (),
    outputs: tuple[OutputSpec, ...] = (),
) -> CompositionState:
    return CompositionState(
        source=source or _source(),
        nodes=nodes,
        edges=(),
        outputs=outputs,
        metadata=PipelineMetadata(),
        version=1,
    )


class TestEvaluateExecutionSecretGuard:
    def test_no_wired_secrets_returns_none(self) -> None:
        assert evaluate_execution_secret_guard(_state()) is None

    def test_wired_secrets_across_components_are_disclosed(self) -> None:
        state = _state(
            source=_source({"path": "/data/in.csv", "api_key": {"secret_ref": "SRC_KEY"}}),
            nodes=(_node({"api_key": {"secret_ref": "NODE_KEY"}}),),
            outputs=(
                OutputSpec(name="primary", plugin="csv", options={"token": {"secret_ref": "SINK_TOKEN"}}, on_write_failure="discard"),
            ),
        )
        guard = evaluate_execution_secret_guard(state)
        assert guard is not None
        disclosed = {(w.secret_name, w.component_id, w.component_type, w.plugin, w.option_key) for w in guard.wirings}
        assert disclosed == {
            ("SRC_KEY", "source", "source", "csv", "api_key"),
            ("NODE_KEY", "node", "transform", "value_transform", "api_key"),
            ("SINK_TOKEN", "primary", "sink", "csv", "token"),
        }
        assert len(guard.ack_token) == 32
        assert "SRC_KEY" in guard.summary or "3" in guard.summary

    def test_env_marker_strings_count_only_for_inventory_names(self) -> None:
        state = _state(source=_source({"path": "/data/in.csv", "api_key": "${MY_KEY}"}))
        assert evaluate_execution_secret_guard(state) is None
        guard = evaluate_execution_secret_guard(state, env_ref_names=frozenset({"MY_KEY"}))
        assert guard is not None
        assert guard.wirings[0].secret_name == "MY_KEY"

    def test_token_is_deterministic_and_rekeys_on_state_change(self) -> None:
        state_a = _state(source=_source({"path": "/data/in.csv", "api_key": {"secret_ref": "KEY"}}))
        state_b = _state(source=_source({"path": "/data/other.csv", "api_key": {"secret_ref": "KEY"}}))
        guard_a1 = evaluate_execution_secret_guard(state_a)
        guard_a2 = evaluate_execution_secret_guard(state_a)
        guard_b = evaluate_execution_secret_guard(state_b)
        assert guard_a1 is not None and guard_a2 is not None and guard_b is not None
        assert guard_a1.ack_token == guard_a2.ack_token
        # Any composition change re-keys the approval — a token approved for
        # one snapshot can never authorize a mutated one.
        assert guard_a1.ack_token != guard_b.ack_token

    def test_annotation_prepends_audit_comment_with_token(self) -> None:
        state = _state(source=_source({"path": "/data/in.csv", "api_key": {"secret_ref": "KEY"}}))
        guard = evaluate_execution_secret_guard(state)
        assert guard is not None
        annotated = annotate_pipeline_yaml_with_secret_guard("pipeline: {}\n", guard)
        first_line, rest = annotated.split("\n", 1)
        assert first_line.startswith(f"# {SECRET_GUARD_AUDIT_COMMENT}: ")
        assert guard.ack_token in first_line
        assert rest == "pipeline: {}\n"

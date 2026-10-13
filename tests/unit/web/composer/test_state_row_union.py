"""Composer parity for the correlated row-union barrier."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)


class TestCompositionStateRowUnion:
    """Composer parity for the plugin-free correlated row_union barrier."""

    def _source(self, on_success: str = "fork_in", *, schema: dict[str, Any] | None = None) -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success=on_success,
            options={"schema": schema or {"mode": "observed"}},
            on_validation_failure="discard",
        )

    def _gate(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "fork_rows",
            "node_type": "gate",
            "plugin": None,
            "input": "fork_in",
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": ("control_branch", "treatment_branch"),
            "branches": None,
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def _transform(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="passthrough",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options=options or {"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _llm(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        response_field: str,
    ) -> NodeSpec:
        """An llm arm emitting its guaranteed provenance trio.

        ``llm`` guarantees ``<response_field>`` plus the ``_usage``/``_model``
        side-fields (``LLM_GUARANTEED_SUFFIXES``). Those are row data, not
        audit-only provenance, so they reach a downstream consumer's input
        contract — the shape that produced elspeth-9d13900064.
        """
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="llm",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options={
                "schema": {"mode": "observed"},
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "system_prompt": "You judge each row. Reply with a one-word verdict.",
                "prompt_template": "Judge this row.",
                "api_key": "env:OPENROUTER_API_KEY",
                "response_field": response_field,
                "required_input_fields": [],
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _aggregation(
        self,
        node_id: str,
        input_connection: str,
        on_success: str,
        *,
        output_mode: str | None,
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="aggregation",
            plugin="batch_stats",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options={
                "schema": {"mode": "observed"},
                "value_field": "value",
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={},
            output_mode=output_mode,
        )

    def _row_union(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "variant_union",
            "node_type": "row_union",
            "plugin": None,
            # Serialized adapter placeholder: the first branch connection.
            "input": "control_done",
            "on_success": "union_out",
            "on_error": None,
            "options": {},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": {
                "control_branch": "control_done",
                "treatment_branch": "treatment_done",
            },
            "policy": None,
            "merge": None,
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def _output(self, name: str = "output", *, options: dict[str, Any] | None = None) -> OutputSpec:
        return OutputSpec(
            name=name,
            plugin="json",
            options=options or {"schema": {"mode": "observed"}},
            on_write_failure="discard",
        )

    def _state(
        self,
        *,
        row_union: NodeSpec | None = None,
        gate: NodeSpec | None = None,
        arms: tuple[NodeSpec, ...] | None = None,
        extra_nodes: tuple[NodeSpec, ...] = (),
        tail_options: dict[str, Any] | None = None,
    ) -> CompositionState:
        return CompositionState(
            source=self._source(),
            nodes=(
                gate or self._gate(),
                *(
                    arms
                    or (
                        self._transform("control", "control_branch", "control_done"),
                        self._transform("treatment", "treatment_branch", "treatment_done"),
                    )
                ),
                row_union or self._row_union(),
                self._transform("after_union", "union_out", "output", options=tail_options),
                *extra_nodes,
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

    def test_row_union_survives_serialization_round_trip(self) -> None:
        state = self._state(row_union=self._row_union(timeout_seconds=2.5))

        payload = state.to_dict()
        restored = CompositionState.from_dict(payload)

        assert payload["nodes"][3]["timeout_seconds"] == 2.5
        assert restored == state
        assert restored.nodes[3].timeout_seconds == 2.5

    def test_from_dict_normalizes_row_union_branch_list_to_identity_mapping(self) -> None:
        node = NodeSpec.from_dict(
            {
                "id": "variant_union",
                "node_type": "row_union",
                "plugin": None,
                "input": "control_branch",
                "on_success": "union_out",
                "on_error": None,
                "options": {},
                "branches": ["control_branch", "treatment_branch"],
            }
        )

        assert node.branches == {
            "control_branch": "control_branch",
            "treatment_branch": "treatment_branch",
        }

    def test_direct_row_union_branch_list_normalizes_before_round_trip(self) -> None:
        row_union = self._row_union(
            input="control_branch",
            branches=("control_branch", "treatment_branch"),
        )
        state = self._state(row_union=row_union)

        assert row_union.branches == {
            "control_branch": "control_branch",
            "treatment_branch": "treatment_branch",
        }
        assert CompositionState.from_dict(state.to_dict()) == state

    def test_from_dict_does_not_hide_duplicate_row_union_branch_aliases(self) -> None:
        row_union = NodeSpec.from_dict(
            {
                "id": "variant_union",
                "node_type": "row_union",
                "plugin": None,
                "input": "control_branch",
                "on_success": "union_out",
                "on_error": None,
                "options": {},
                "branches": ["control_branch", "treatment_branch", "control_branch"],
            }
        )

        result = self._state(row_union=row_union).validate()

        assert any(error.error_code == "row_union_branches_invalid" for error in result.errors)

    def test_valid_row_union_topology(self) -> None:
        result = self._state().validate()

        assert result.is_valid, result.errors

    def test_repeated_row_union_mappings_reuse_lineage_queries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer import state as state_module

        state = self._state()
        original_lineage = state_module._runtime_connection_lineage
        lineage_queries: list[tuple[str, str]] = []

        def recording_lineage(
            origin: str,
            target: str,
            sources: dict[str, SourceSpec],
            nodes: tuple[NodeSpec, ...],
        ) -> tuple[bool, tuple[NodeSpec, ...]]:
            lineage_queries.append((origin, target))
            return original_lineage(origin, target, sources, nodes)

        monkeypatch.setattr(state_module, "_runtime_connection_lineage", recording_lineage)
        repeated_unions = tuple(
            replace(
                self._row_union(),
                id=f"variant_union_{index}",
                on_success=f"union_out_{index}",
            )
            for index in range(20)
        )
        state = replace(
            state,
            nodes=tuple(node for node in state.nodes if node.node_type != "row_union") + repeated_unions,
        )

        state.validate()

        assert lineage_queries == [
            ("control_branch", "control_done"),
            ("treatment_branch", "treatment_done"),
        ]

    def test_single_lineage_query_memoizes_shared_queue_subgraphs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer import state as state_module
        from elspeth.web.composer._producer_resolver import ProducerResolver

        def node(**overrides: Any) -> NodeSpec:
            values: dict[str, Any] = {
                "id": "node",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "root",
                "on_success": "out",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
                "condition": None,
                "routes": None,
                "fork_to": None,
                "branches": None,
                "policy": None,
                "merge": None,
            }
            values.update(overrides)
            return NodeSpec(**values)

        depth = 8
        nodes = [
            node(id="seed_a", on_success="queue_0_a"),
            node(id="seed_b", on_success="queue_0_b"),
            node(id="queue_0_a", node_type="queue", plugin=None, input="queue_0_a", on_success=None, on_error=None, options={}),
            node(id="queue_0_b", node_type="queue", plugin=None, input="queue_0_b", on_success=None, on_error=None, options={}),
        ]
        for layer in range(1, depth + 1):
            previous = layer - 1
            destinations = (f"queue_{layer}_a", f"queue_{layer}_b")
            for branch in ("a", "b"):
                nodes.append(
                    node(
                        id=f"gate_{layer}_{branch}",
                        node_type="gate",
                        plugin=None,
                        input=f"queue_{previous}_{branch}",
                        on_success=None,
                        on_error=None,
                        options={},
                        condition="True",
                        routes={"true": destinations[0], "false": destinations[1]},
                    )
                )
            for destination in destinations:
                nodes.append(
                    node(id=destination, node_type="queue", plugin=None, input=destination, on_success=None, on_error=None, options={})
                )

        calls = 0
        original = ProducerResolver.queue_predecessors

        def counting_queue_predecessors(resolver: ProducerResolver, queue_id: str):
            nonlocal calls
            calls += 1
            return original(resolver, queue_id)

        monkeypatch.setattr(ProducerResolver, "queue_predecessors", counting_queue_predecessors)

        compatible, lineage = state_module._runtime_connection_lineage(
            "root",
            f"queue_{depth}_a",
            {"source": self._source(on_success="root")},
            tuple(nodes),
        )

        assert compatible is True
        assert lineage
        assert calls <= (2 * depth) + 2

    @pytest.mark.parametrize("output_mode", [None, "transform"])
    def test_row_union_rejects_transform_mode_aggregation_inside_branch(self, output_mode: str | None) -> None:
        state = self._state()
        branch_aggregation = self._aggregation(
            "control",
            "control_branch",
            "control_done",
            output_mode=output_mode,
        )
        state = replace(
            state,
            nodes=tuple(branch_aggregation if node.id == "control" else node for node in state.nodes),
        )

        result = state.validate()

        error = next(error for error in result.errors if error.error_code == "row_union_branch_aggregation_invalid")
        assert error.component == "node:variant_union"
        assert "control" in error.message
        assert "row_id" in error.message
        # The remedy must NOT recommend passthrough mode — rule 6 rejects an
        # in-region aggregation regardless of output_mode (WS2 Task 9 wrong-
        # remedy enrichment); the message says so instead.
        assert "passthrough" not in error.message
        assert "banned inside every bound region" in error.message

    def test_row_union_rejects_passthrough_aggregation_inside_branch(self) -> None:
        # RULING-25 CASUALTY (WS2 Task 9, 2026-08-23; formerly
        # test_row_union_accepts_passthrough_aggregation_inside_branch, an
        # elspeth-a5b86149d4-family positive control). The identity-preservation
        # property passthrough mode demonstrates is real and unaffected, but
        # spec §7 rule 6 bans aggregators inside every bound region regardless
        # of output mode: a batch flush consumes members the region's roster
        # must account for (loss-blindness), and passthrough does not mitigate
        # that. Stage 1 mirrors the runtime rejection.
        state = self._state()
        branch_aggregation = self._aggregation(
            "control",
            "control_branch",
            "control_done",
            output_mode="passthrough",
        )
        state = replace(
            state,
            nodes=tuple(branch_aggregation if node.id == "control" else node for node in state.nodes),
        )

        result = state.validate()

        assert not result.is_valid
        error = next(error for error in result.errors if error.error_code == "bound_region_aggregation_invalid")
        assert error.component == "node:variant_union"
        assert "control" in error.message
        assert "rule 6" in error.message
        # The twin's transform-mode-specific code must NOT fire for a
        # passthrough aggregation — this shape belongs to rule 6 alone.
        assert not any(error.error_code == "row_union_branch_aggregation_invalid" for error in result.errors)

    def test_row_union_accepts_transform_mode_aggregation_before_fork(self) -> None:
        state = self._state()
        pre_fork_aggregation = self._aggregation(
            "pre_fork_batch",
            "pre_fork_in",
            "fork_in",
            output_mode="transform",
        )
        state = replace(
            state,
            sources={"source": self._source(on_success="pre_fork_in")},
            nodes=(pre_fork_aggregation, *state.nodes),
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_row_union_rejects_nested_fork_inside_branch(self) -> None:
        nested_gate = self._gate(
            id="nested_fork",
            input="control_branch",
            fork_to=("nested_a", "nested_b"),
        )
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._gate(),
                nested_gate,
                self._transform("control", "nested_a", "control_done"),
                self._transform("treatment", "treatment_branch", "treatment_done"),
                self._row_union(),
                self._transform("after_union", "union_out", "output"),
            ),
            edges=(),
            outputs=(self._output(), self._output("nested_b")),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        error = next(error for error in result.errors if error.error_code == "row_union_nested_fork_invalid")
        assert error.component == "node:variant_union"
        assert "nested_fork" in error.message
        assert "control_branch" in error.message

    def test_gate_fork_aliases_must_be_unique_before_row_union_origin_resolution(self) -> None:
        state = self._state(
            gate=self._gate(
                fork_to=("control_branch", "control_branch", "treatment_branch"),
            )
        )

        result = state.validate()

        error = next(error for error in result.errors if error.error_code == "gate_duplicate_fork_branch")
        assert error.component == "node:fork_rows"
        assert "control_branch" in error.message

    @pytest.mark.parametrize(
        "node_id",
        [
            "bad name",
            "a" * 39,
            "fork",
            "__private",
            " variant_union ",
        ],
    )
    def test_row_union_name_matches_runtime_identifier_contract(self, node_id: str) -> None:
        result = self._state(row_union=self._row_union(id=node_id)).validate()

        error = next(error for error in result.errors if error.error_code == "row_union_name_invalid")
        assert error.component == f"node:{node_id}"

    def test_malformed_row_union_branch_values_return_errors_without_sorting_type_error(self) -> None:
        payload = json.loads(json.dumps(self._state().to_dict()))
        row_union = next(node for node in payload["nodes"] if node["node_type"] == "row_union")
        row_union["branches"] = {
            "control_branch": 123,
            "treatment_branch": "missing",
        }
        row_union["input"] = 123

        result = CompositionState.from_dict(payload).validate()

        assert not result.is_valid
        assert any(error.error_code == "row_union_branch_invalid" for error in result.errors)

    def test_row_union_rejects_branch_aliases_from_multiple_fork_gates(self) -> None:
        # Single-branch gates: the pre-rule-2 overflow filler branches were
        # independently mixed-closure-invalid and would drown the finding
        # under test.
        second_gate = self._gate(
            id="fork_treatment",
            input="second_fork_in",
            fork_to=("treatment_branch",),
        )
        state = CompositionState(
            sources={
                "control_source": self._source(on_success="fork_in"),
                "treatment_source": self._source(on_success="second_fork_in"),
            },
            nodes=(
                self._gate(fork_to=("control_branch",)),
                second_gate,
                self._transform("control", "control_branch", "control_done"),
                self._transform("treatment", "treatment_branch", "treatment_done"),
                self._row_union(),
                self._transform("after_union", "union_out", "output"),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        codes = {error.error_code for error in result.errors}
        # Rule 2 supersedes the retired origin-analysis mirror (maintainer
        # ruling 2026-08-23): the enriched roster mismatch is the one
        # rejection for a union drawn from multiple gates.
        assert "row_union_branch_origin_invalid" not in codes, codes
        roster_errors = [error for error in result.errors if error.error_code == "fork_roster_mismatch" and "drawn from" in error.message]
        assert len(roster_errors) == 1, [error.message for error in result.errors]
        enriched = roster_errors[0].message
        assert "variant_union" in enriched
        assert "drawn from 2 fork gate(s)" in enriched
        assert "fork_rows" in enriched
        assert "fork_treatment" in enriched
        # A step-8 topology finding, not the intrinsic node-shape code the
        # mutation preflight blocks on.
        assert "row_union_branch_invalid" not in codes

    def test_row_union_rejects_branch_connection_from_a_different_alias(self) -> None:
        row_union = self._row_union(
            input="treatment_done",
            branches={
                "control_branch": "treatment_done",
                "treatment_branch": "control_done",
            },
        )

        result = self._state(row_union=row_union).validate()

        mapping_error = next(error for error in result.errors if error.error_code == "row_union_branch_not_downstream")
        assert "control_branch" in mapping_error.message
        assert "treatment_done" in mapping_error.message
        assert "not downstream" in mapping_error.message
        # A step-8 topology finding, not the intrinsic node-shape code the
        # mutation preflight blocks on.
        assert "row_union_branch_invalid" not in {error.error_code for error in result.errors}

    def test_row_union_rejects_queue_branch_with_unrelated_producer(self) -> None:
        queue = NodeSpec(
            id="control_done",
            node_type="queue",
            plugin=None,
            input="control_done",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._state(extra_nodes=(queue,))
        state = replace(
            state,
            sources={
                "primary": self._source(),
                "contaminant": self._source(on_success="control_done"),
            },
        )

        result = state.validate()

        lineage_error = next(error for error in result.errors if error.error_code == "row_union_branch_not_downstream")
        assert "control_branch" in lineage_error.message
        assert "control_done" in lineage_error.message

    @pytest.mark.parametrize(
        ("control_fields", "treatment_fields", "is_compatible"),
        [
            (["id: str", "score: float"], ["id: str", "score: float"], True),
            (["id: str", "score: float"], ["id: str", "label: str"], False),
        ],
    )
    def test_row_union_requires_compatible_known_fixed_branch_schemas(
        self,
        control_fields: list[str],
        treatment_fields: list[str],
        is_compatible: bool,
    ) -> None:
        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={"schema": {"mode": "fixed", "fields": control_fields}},
            )
            if node.id == "control"
            else replace(
                node,
                options={"schema": {"mode": "fixed", "fields": treatment_fields}},
            )
            if node.id == "treatment"
            else node
            for node in state.nodes
        )

        result = replace(state, nodes=nodes).validate()
        schema_errors = [
            error
            for error in result.errors
            if error.component == "node:variant_union"
            and error.error_code == "row_union_schema_incompatible"
            and "incompatible" in error.message
        ]

        assert bool(schema_errors) is not is_compatible, result.errors
        if not is_compatible:
            detail = schema_errors[0].row_union_schema
            assert detail is not None
            assert detail.conflicting_fields == ("label", "score")
            assert tuple(branch.branch for branch in detail.branches) == (
                "control_branch",
                "treatment_branch",
            )

    def test_row_union_observed_branch_abstains_against_fixed_branch(self) -> None:
        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={"schema": {"mode": "fixed", "fields": ["id: str", "score: float"]}},
            )
            if node.id == "treatment"
            else node
            for node in state.nodes
        )

        result = replace(state, nodes=nodes).validate()

        assert not any(
            error.component == "node:variant_union"
            and error.error_code == "row_union_schema_incompatible"
            and "incompatible" in error.message
            for error in result.errors
        ), result.errors

    def test_row_union_accepts_disjoint_flexible_branch_declarations(self) -> None:
        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["score: float"] if node.id == "control" else ["label: str"],
                    }
                },
            )
            if node.id in {"control", "treatment"}
            else node
            for node in state.nodes
        )

        result = replace(state, nodes=nodes).validate()

        assert not any(error.error_code == "row_union_schema_incompatible" for error in result.errors), result.errors

    def test_row_union_flexible_shared_type_conflict_carries_repair_facts(self) -> None:
        from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
        from elspeth.web.composer.tools import ToolResult

        state = self._state()
        nodes = tuple(
            replace(
                node,
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["id: str"] if node.id == "control" else ["id: int"],
                    }
                },
            )
            if node.id in {"control", "treatment"}
            else node
            for node in state.nodes
        )
        candidate = replace(state, nodes=nodes)

        result = candidate.validate()

        entry = next(error for error in result.errors if error.error_code == "row_union_schema_incompatible")
        detail = entry.row_union_schema
        assert detail is not None
        assert detail.conflicting_fields == ("id",)
        assert [
            {
                "branch": branch.branch,
                "mode": branch.mode,
                "fields": tuple((field.name, field.field_type) for field in branch.fields),
            }
            for branch in detail.branches
        ] == [
            {
                "branch": "control_branch",
                "mode": "flexible",
                "fields": (("id", "str"),),
            },
            {
                "branch": "treatment_branch",
                "mode": "flexible",
                "fields": (("id", "int"),),
            },
        ]

        tool_result = ToolResult(
            success=False,
            updated_state=candidate,
            validation=result,
            affected_nodes=(),
        )
        projected = next(
            error
            for error in _allowlisted_candidate_feedback(tool_result)["validation"]["errors"]
            if error["error_code"] == "row_union_schema_incompatible"
        )
        assert "message" not in projected
        assert projected["row_union_schema"] == detail.to_dict()
        assert projected["suggested_fix"]

    def _coalesce(self, **overrides: Any) -> NodeSpec:
        defaults: dict[str, Any] = {
            "id": "dup_merge",
            "node_type": "coalesce",
            "plugin": None,
            "input": "join",
            "on_success": "output",
            "on_error": None,
            "options": {},
            "condition": None,
            "routes": None,
            "fork_to": None,
            # Identity branches: direct gate->barrier COPY edges that claim no
            # ordinary connection consumer, so the duplicate-consumer check
            # cannot mask the barrier-ownership conflict under test.
            "branches": ("control_branch", "treatment_branch"),
            "policy": "require_all",
            "merge": "nested",
        }
        defaults.update(overrides)
        return NodeSpec(**defaults)

    def test_fork_branch_claimed_by_a_coalesce_and_a_row_union_is_rejected(self) -> None:
        """Composer/runtime parity for the engine's one-barrier-per-branch rule.

        The DAG builder raises ``GraphValidationError`` ("Each fork branch can
        only join at one barrier") when a coalesce and a row_union both declare
        the same branch, because the branch's arrival is delivered to exactly
        one barrier's pending map. validate() used to pass this composition, so
        generate_yaml handed the runtime a graph it refuses to build.
        """
        result = self._state(extra_nodes=(self._coalesce(),)).validate()

        codes = {error.error_code for error in result.errors}
        assert "fork_branch_multiple_barriers" in codes, result.errors
        # composer_mcp.server gates generate_yaml on is_valid, so a red
        # validate() is what stops the runtime-invalid YAML being exported.
        assert not result.is_valid
        conflict = next(error for error in result.errors if error.error_code == "fork_branch_multiple_barriers")
        assert "control_branch" in conflict.message
        assert "variant_union" in conflict.message
        assert "dup_merge" in conflict.message
        # A cross-node topology finding, not either barrier's intrinsic
        # node-shape code the mutation preflight blocks on.
        assert "row_union_branch_invalid" not in codes

    def test_fork_branch_claimed_by_two_coalesces_is_rejected(self) -> None:
        """The same engine rule covers coalesce/coalesce claims."""
        result = self._state(
            extra_nodes=(self._coalesce(), self._coalesce(id="second_merge")),
        ).validate()

        assert any(error.error_code == "fork_branch_multiple_barriers" for error in result.errors), result.errors

    def test_fork_branch_claimed_by_two_row_unions_is_rejected(self) -> None:
        """And row_union/row_union claims, which the engine rejects too."""
        second_union = self._row_union(id="second_union", on_success="second_union_out")
        result = self._state(
            extra_nodes=(second_union, self._transform("after_second", "second_union_out", "output")),
        ).validate()

        assert any(error.error_code == "fork_branch_multiple_barriers" for error in result.errors), result.errors

    def test_valid_topology_does_not_report_a_barrier_conflict(self) -> None:
        """A single barrier per branch stays clean — the rule is not a blanket ban."""
        result = self._state().validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "fork_branch_multiple_barriers" for error in result.errors)

    def test_row_union_output_feeds_ordinary_node_without_placeholder_consumer(self) -> None:
        state = self._state()

        result = state.validate()

        assert any(node.id == "after_union" and node.input == "union_out" for node in state.nodes)
        assert result.is_valid, result.errors
        assert not any(error.error_code == "duplicate_connection_consumer" for error in result.errors)

    def test_queue_output_feeds_row_union_branch_without_placeholder_consumer(self) -> None:
        queue = NodeSpec(
            id="control_done",
            node_type="queue",
            plugin=None,
            input="control_done",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = self._state(extra_nodes=(queue,))

        result = state.validate()

        assert result.is_valid, result.errors
        assert not any(error.error_code == "duplicate_connection_consumer" for error in result.errors)

    def test_identity_row_union_branch_does_not_consume_same_named_queue(self) -> None:
        queue = NodeSpec(
            id="control_branch",
            node_type="queue",
            plugin=None,
            input="control_branch",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        row_union = self._row_union(
            input="control_branch",
            branches={
                "control_branch": "control_branch",
                "treatment_branch": "treatment_branch",
            },
        )
        state = CompositionState(
            source=self._source(),
            nodes=(
                self._gate(),
                queue,
                row_union,
                self._transform("after_union", "union_out", "output"),
            ),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        assert any(error.error_code == "queue_no_consumer" and error.component == "node:control_branch" for error in result.errors)

    def test_row_union_rejects_downstream_aggregation_with_early_trigger(self) -> None:
        aggregation = NodeSpec(
            id="after_union",
            node_type="aggregation",
            plugin="batch_stats",
            input="union_out",
            on_success="output",
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 2},
            output_mode="transform",
        )
        state = self._state()
        state = replace(
            state,
            nodes=tuple(aggregation if node.id == "after_union" else node for node in state.nodes),
        )

        result = state.validate()

        group_error = next(
            error
            for error in result.errors
            if error.component == "node:variant_union"
            and error.error_code == "row_union_downstream_group_invalid"
            and "indivisible" in error.message
        )
        assert "count/timeout/condition trigger" in group_error.message

    @pytest.mark.parametrize("aggregation_first", (False, True))
    def test_row_union_invalid_downstream_trigger_keeps_intrinsic_rejection(self, aggregation_first: bool) -> None:
        """Skipping derived group analysis must retain the actual parse error."""
        aggregation = replace(
            self._aggregation("after_union", "union_out", "output", output_mode="transform"),
            trigger={"count": "not-a-count"},
        )
        state = self._state()
        nodes = tuple(aggregation if node.id == "after_union" else node for node in state.nodes)
        if aggregation_first:
            nodes = (aggregation, *(node for node in nodes if node.id != aggregation.id))

        result = replace(state, nodes=nodes).validate()

        assert not result.is_valid
        errors = [error for error in result.errors if error.error_code == "aggregation_trigger_invalid"]
        assert len(errors) == 1
        assert errors[0].component == "node:after_union"
        assert errors[0].severity == "high"

    @pytest.mark.parametrize("barrier_type", ["coalesce", "row_union"])
    def test_row_union_rejects_downstream_correlated_barrier(self, barrier_type: str) -> None:
        post_union_gate = self._gate(
            id="post_union_fork",
            input="union_out",
            fork_to=("downstream_a", "downstream_b"),
        )
        if barrier_type == "coalesce":
            downstream_barrier = self._coalesce(
                id="downstream_barrier",
                input="downstream_a",
                branches=("downstream_a", "downstream_b"),
            )
            tail: tuple[NodeSpec, ...] = ()
        else:
            downstream_barrier = self._row_union(
                id="downstream_barrier",
                input="downstream_a",
                branches={
                    "downstream_a": "downstream_a",
                    "downstream_b": "downstream_b",
                },
                on_success="downstream_out",
            )
            tail = (self._transform("after_downstream", "downstream_out", "output"),)

        state = self._state()
        state = replace(
            state,
            nodes=(
                *(node for node in state.nodes if node.id != "after_union"),
                post_union_gate,
                downstream_barrier,
                *tail,
            ),
        )

        result = state.validate()

        group_error = next(
            error
            for error in result.errors
            if error.component == "node:variant_union"
            and error.error_code == "row_union_downstream_group_invalid"
            and "correlated barrier" in error.message
        )
        assert barrier_type in group_error.message
        assert "downstream_barrier" in group_error.message

    @pytest.mark.parametrize("branches", [None, (), ("only_branch",), {"only_branch": "control_done"}])
    def test_row_union_requires_at_least_two_branches(self, branches: object) -> None:
        result = self._state(row_union=self._row_union(branches=branches)).validate()

        assert any(error.error_code == "row_union_branches_invalid" for error in result.errors)

    @pytest.mark.parametrize("on_success", [None, "", "   "])
    def test_row_union_requires_non_empty_on_success(self, on_success: object) -> None:
        result = self._state(row_union=self._row_union(on_success=on_success)).validate()

        assert any(error.error_code == "row_union_on_success_invalid" for error in result.errors)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("plugin", "passthrough"),
            ("options", {"schema": {"mode": "observed"}}),
            ("on_error", "discard"),
            ("condition", "True"),
            ("routes", {"true": "union_out"}),
            ("fork_to", ("branch",)),
            ("policy", "require_all"),
            ("merge", "union"),
            ("trigger", {"kind": "count"}),
            ("output_mode", "passthrough"),
            ("expected_output_count", 2),
        ],
    )
    def test_row_union_rejects_fields_owned_by_other_node_kinds(self, field: str, value: object) -> None:
        result = self._state(row_union=self._row_union(**{field: value})).validate()

        assert any(error.error_code == "row_union_config_invalid" and field in error.message for error in result.errors), result.errors

    @pytest.mark.parametrize(
        "timeout_seconds",
        # The two oversized ints are unrepresentable as float; classifying them
        # INVALID must not regress the bool / NaN / inf / non-positive verdicts.
        [True, False, float("nan"), float("inf"), 0.0, -1.0, 10**400, -(10**400)],
    )
    def test_row_union_rejects_invalid_timeout(self, timeout_seconds: object) -> None:
        result = self._state(row_union=self._row_union(timeout_seconds=timeout_seconds)).validate()

        assert any(error.error_code == "row_union_timeout_invalid" for error in result.errors)

    def test_oversized_persisted_timeout_rejects_instead_of_overflowing(self) -> None:
        """An oversized int from a restored session must reject, not crash.

        ``timeout_seconds`` reaches ``NodeSpec.from_dict`` straight from the
        persisted session payload, bypassing the Pydantic
        ``_StrictTimeoutSeconds`` tool boundary. JSON has no integer ceiling,
        so ``10**400`` survives the round trip as an ``int`` that ``float()``
        cannot represent — ``math.isfinite`` used to raise ``OverflowError``
        out of ``validate()`` instead of producing a rejection.
        """
        payload = json.loads(
            json.dumps(
                {
                    "id": "variant_union",
                    "node_type": "row_union",
                    "plugin": None,
                    "input": "control_done",
                    "on_success": "union_out",
                    "on_error": None,
                    "options": {},
                    "branches": {"control_branch": "control_done", "treatment_branch": "treatment_done"},
                    "timeout_seconds": 10**400,
                }
            )
        )
        assert isinstance(payload["timeout_seconds"], int)

        result = self._state(row_union=NodeSpec.from_dict(payload)).validate()

        assert any(error.error_code == "row_union_timeout_invalid" for error in result.errors)

    def test_row_union_input_is_only_first_branch_placeholder(self) -> None:
        result = self._state(row_union=self._row_union(input="treatment_done")).validate()

        assert any(error.error_code == "row_union_input_mismatch" for error in result.errors)

    @pytest.mark.parametrize(
        "branches",
        [
            {"__control": "control_done", "treatment_branch": "treatment_done"},
            {"control_branch": "__control_done", "treatment_branch": "treatment_done"},
        ],
    )
    def test_row_union_branch_aliases_and_connections_obey_connection_name_rules(
        self,
        branches: dict[str, str],
    ) -> None:
        row_union = self._row_union(input=next(iter(branches.values())), branches=branches)
        result = self._state(row_union=row_union).validate()

        assert any(error.error_code == "row_union_branch_invalid" for error in result.errors)

    def test_row_union_requires_each_branch_alias_and_value_to_be_reachable(self) -> None:
        row_union = self._row_union(
            branches={
                "control_branch": "control_done",
                "unforked_branch": "missing_connection",
            }
        )
        result = self._state(row_union=row_union).validate()
        codes = {error.error_code for error in result.errors}

        assert "row_union_branch_alias_unreachable" in codes
        assert "row_union_branch_unreachable" in codes

    def test_row_union_claims_every_branch_value_as_a_consumer(self) -> None:
        competing = self._transform("competing", "treatment_done", "unused")
        state = self._state(
            extra_nodes=(competing,),
        )

        result = state.validate()

        assert any(error.error_code == "duplicate_connection_consumer" for error in result.errors)

    def test_row_union_on_success_must_feed_a_processing_node(self) -> None:
        row_union = self._row_union(on_success="output")
        result = self._state(row_union=row_union).validate()

        assert any(error.error_code == "row_union_on_success_must_be_connection" for error in result.errors)

    def test_row_union_with_participating_branches_propagates_guarantees(self) -> None:
        """elspeth-41bcaa882e: a participating union is checked, not skipped.

        Historically the walk-back abstained at every row_union with a
        medium "Contract check skipped" warning and emitted no EdgeContract,
        which let the composer author union-consumer requirements the engine
        then deterministically rejected at /validate. When every branch
        participates, the contract check now proceeds against the union's
        branch-intersection guarantee.
        """
        state = self._state(
            tail_options={
                "required_input_fields": ["id"],
                "schema": {"mode": "observed"},
            }
        )
        state = replace(
            state,
            sources={
                "source": self._source(
                    schema={
                        "mode": "fixed",
                        "fields": ["id: str"],
                        "guaranteed_fields": ["id"],
                    }
                )
            },
        )

        result = state.validate()

        assert result.is_valid, result.errors
        contract = next(contract for contract in result.edge_contracts if contract.to_id == "after_union")
        assert contract.from_id == "variant_union"
        assert contract.satisfied
        assert not any("row_union" in warning.message and "observed schema" in warning.message for warning in result.warnings)

    def _union_arm_queue(self, connection_name: str) -> NodeSpec:
        """An in-place queue on a branch connection: an arm Composer cannot resolve."""
        return NodeSpec(
            id=connection_name,
            node_type="queue",
            plugin=None,
            input=connection_name,
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _locked_tail_options(self, fields: list[str]) -> dict[str, Any]:
        return {"schema": {"mode": "fixed", "fields": fields}}

    def _llm_arms(self, *, control_field: str = "verdict", treatment_field: str = "verdict") -> tuple[NodeSpec, ...]:
        return (
            self._llm("control", "control_branch", "control_done", response_field=control_field),
            self._llm("treatment", "treatment_branch", "treatment_done", response_field=treatment_field),
        )

    def test_row_union_arm_emits_reach_a_locked_input_consumer(self) -> None:
        """elspeth-9d13900064: Rule A must not fail open across a row_union.

        The presence direction abstains at a row_union (an arm's guarantees
        cannot be promoted to the union's), but the extras direction is the
        opposite polarity: a field guaranteed by an arm WILL arrive on that
        arm's rows, so a fixed-mode consumer forbidding it is a definite
        runtime PluginContractViolation, not a maybe.
        """
        state = self._state(
            arms=self._llm_arms(),
            tail_options=self._locked_tail_options(["verdict: str"]),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:after_union"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.consumer == "after_union"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_row_union_extras_reach_a_fixed_mode_field_mapper_consumer(self) -> None:
        """The ticket's literal graph: llm x2 -> row_union -> fixed field_mapper.

        A field_mapper consumer also selects the plugin-specific repair
        wording, which the boundary path must carry like the resolved-producer
        path does.
        """
        field_mapper = NodeSpec(
            id="after_union",
            node_type="transform",
            plugin="field_mapper",
            input="union_out",
            on_success="output",
            on_error="discard",
            options={
                "schema": {"mode": "fixed", "fields": ["verdict: str"]},
                "mapping": {"verdict": "verdict"},
                "select_only": True,
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(
            source=self._source(),
            nodes=(self._gate(), *self._llm_arms(), self._row_union(), field_mapper),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")
        assert "consumer's schema.fields" in entry.message

    def test_row_union_arm_emits_union_rather_than_intersect(self) -> None:
        """A field guaranteed by ONE arm only is still a definite extra.

        Intersection math (the presence-direction merge) would clear this
        graph: the arms share no guaranteed field. Rows from the treatment arm
        still carry its trio, so the union is the sound set here.
        """
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            tail_options=self._locked_tail_options(["verdict: str", "verdict_usage: any", "verdict_model: str"]),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.extra_fields == ("tone", "tone_model", "tone_usage")

    def test_row_union_locked_input_accepting_every_arm_emit_is_clean(self) -> None:
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            tail_options=self._locked_tail_options(
                [
                    "verdict: str",
                    "verdict_usage: any",
                    "verdict_model: str",
                    "tone: str",
                    "tone_usage: any",
                    "tone_model: str",
                ]
            ),
        )

        result = state.validate()

        assert result.is_valid, result.errors

    def test_row_union_unresolvable_arm_does_not_invent_locked_input_extras(self) -> None:
        """An arm the EMIT walker cannot resolve contributes no fields, and no error.

        The guarantee-direction vote sees through the in-place queue (fan-in
        intersection, elspeth-3619b8774f), so the union participates and the
        contract check runs — no skip warning. The emit direction stays
        conservative: the queued arm contributes nothing, so no extras are
        invented for it.
        """
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            extra_nodes=(self._union_arm_queue("treatment_done"),),
            tail_options=self._locked_tail_options(["verdict: str", "verdict_usage: any", "verdict_model: str"]),
        )

        result = state.validate()

        assert not any(error.error_code == "locked_input_extras" for error in result.errors), result.errors

    def test_row_union_known_arm_extras_survive_an_unresolvable_sibling(self) -> None:
        """Partial knowledge still errors on what IS known.

        The queued treatment arm is opaque to the EMIT walker, but the
        control arm's guarantees are proven — so its extras are reported.
        """
        state = self._state(
            arms=self._llm_arms(treatment_field="tone"),
            extra_nodes=(self._union_arm_queue("treatment_done"),),
            tail_options=self._locked_tail_options(["verdict: str"]),
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_row_union_arm_emits_reach_a_locked_sink_through_a_gate(self) -> None:
        """Rule B shares Rule A's walker, so it shares the row_union hole.

        A row_union cannot feed a sink directly (its on_success must be a
        processing connection), so the sink boundary is only reachable through
        an intervening routing gate.
        """
        release_gate = self._gate(
            id="release",
            input="union_out",
            routes={"true": "output", "false": "output"},
            fork_to=None,
        )
        state = CompositionState(
            source=self._source(),
            nodes=(self._gate(), *self._llm_arms(), self._row_union(), release_gate),
            edges=(),
            outputs=(self._output(options={"schema": {"mode": "fixed", "fields": ["verdict: str"]}}),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "sink_locked_extras")
        assert entry.component == "output:output"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.consumer == "output:output"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_resolvable_pass_through_node_between_union_and_locked_consumer_still_errors(self) -> None:
        """elspeth-902fc354b2 gap 1: a resolvable intermediate node must not bypass Rule A.

        With a pass-through relay between the row_union and the locked
        consumer, the presence walk RESOLVES (to the relay), so the row_union
        boundary path never runs. The relay declares
        ``passes_through_input=True`` — a runtime-verified ADR-008 contract —
        so every arm emit definitely survives it and must still reach the
        Rule A comparison at the locked consumer.
        """
        relay = self._transform("relay", "union_out", "relay_done")
        locked_tail = self._transform(
            "after_union",
            "relay_done",
            "output",
            options=self._locked_tail_options(["verdict: str"]),
        )
        state = CompositionState(
            source=self._source(),
            nodes=(self._gate(), *self._llm_arms(), self._row_union(), relay, locked_tail),
            edges=(),
            outputs=(self._output(),),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        assert entry.component == "node:after_union"
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "relay"
        assert detail.consumer == "after_union"
        assert detail.extra_fields == ("verdict_model", "verdict_usage")

    def test_row_union_arm_pass_through_carries_source_guarantees_to_locked_consumer(self) -> None:
        """elspeth-902fc354b2 gap 2 through a union: arms pass through source fields.

        g08's live rejection included ``complaint_text`` — an ordinary
        pass-through field, not an llm side-field. An llm arm declares
        ``passes_through_input=True``, so a source-guaranteed field definitely
        arrives on every union row and a locked consumer forbidding it is a
        definite runtime PluginContractViolation.
        """
        state = self._state(
            arms=self._llm_arms(),
            tail_options=self._locked_tail_options(["verdict: str", "verdict_model: str", "verdict_usage: any"]),
        )
        state = replace(
            state,
            sources={
                "source": self._source(
                    schema={
                        "mode": "fixed",
                        "fields": ["complaint_text: str"],
                        "guaranteed_fields": ["complaint_text"],
                    }
                )
            },
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "locked_input_extras")
        detail = entry.contract
        assert detail is not None
        assert detail.producer == "variant_union"
        assert detail.extra_fields == ("complaint_text",)

    def test_union_consumer_requirement_satisfied_by_every_branch_validates(self) -> None:
        """elspeth-41bcaa882e (battery-2026-08-06 g08): the barrier is transparent.

        Both arms are pass-through, so a source-guaranteed field arrives on
        every released row. A consumer downstream of the union requiring it
        must validate — the walker abstaining at the row_union previously
        reported "guarantees: (none)" and rejected the runnable pipeline,
        mirroring the engine's "(none - dynamic schema)" rejection.
        """
        state = self._state(
            tail_options={
                "schema": {"mode": "observed"},
                "required_input_fields": ["amount"],
            },
        )
        state = replace(
            state,
            sources={"source": self._source(schema={"mode": "observed", "guaranteed_fields": ["id", "amount"]})},
        )

        result = state.validate()

        assert result.is_valid, [e.message for e in result.errors]

    def test_union_consumer_requiring_branch_only_field_still_errors(self) -> None:
        """Fail-closed twin: a field only ONE branch guarantees is not union-guaranteed.

        Control rows never carry the treatment arm's extra field, and every
        released group contains a control row, so the intersection must drop
        it and the consumer requirement must still reject.
        """
        arms = (
            self._transform("control", "control_branch", "control_done"),
            self._transform(
                "treatment",
                "treatment_branch",
                "treatment_done",
                options={"schema": {"mode": "observed", "guaranteed_fields": ["treatment_tag"]}},
            ),
        )
        state = self._state(
            arms=arms,
            tail_options={
                "schema": {"mode": "observed"},
                "required_input_fields": ["treatment_tag"],
            },
        )
        state = replace(
            state,
            sources={"source": self._source(schema={"mode": "observed", "guaranteed_fields": ["id", "amount"]})},
        )

        result = state.validate()

        entry = next(error for error in result.errors if error.error_code == "schema_contract_violation")
        detail = entry.contract
        assert detail is not None
        assert detail.consumer == "after_union"
        assert detail.missing_fields == ("treatment_tag",)

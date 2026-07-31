"""Tests for guided-mode turn emitters (Task 10.0 — Gap 6 wire shape)."""

from __future__ import annotations

from types import SimpleNamespace

from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.guided.emitters import (
    _node_cardinality,
    _step_index,
    build_component_review_turn,
    build_initial_step_1_turn,
    build_step_1_schema_form_turn,
    build_step_1_schema_form_turn_from_resolved,
    build_step_2_schema_form_turn,
    build_step_4_wire_turn,
)
from elspeth.web.composer.guided.planning import _node_behavior
from elspeth.web.composer.guided.protocol import GuidedStep, ProposePipelinePayload, TurnType, validate_payload
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.source_inspection import SourceInspectionFacts
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig


class _Catalog:
    def get_schema(self, plugin_type: str, plugin_name: str) -> SimpleNamespace:
        return SimpleNamespace(
            json_schema={"properties": {"path": {"type": "string"}}},
            knob_schema={
                "fields": [
                    {
                        "name": "path",
                        "label": "Path",
                        "kind": "text",
                        "required": True,
                        "nullable": False,
                    }
                ]
            },
        )


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _queue_state() -> CompositionState:
    """Two sources fan into a declared queue that feeds one ordinary consumer."""

    def _src() -> SourceSpec:
        return SourceSpec(
            plugin="csv",
            on_success="inbound",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        )

    return CompositionState(
        source=None,
        sources={"orders": _src(), "refunds": _src()},
        nodes=(
            NodeSpec(
                id="inbound",
                node_type="queue",
                plugin=None,
                input="inbound",
                on_success=None,
                on_error=None,
                options={"description": "Orders and refunds interleave here"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="normalize",
                node_type="transform",
                plugin="passthrough",
                input="inbound",
                on_success="combined",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="combined",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Queue fan-in", description=""),
        version=1,
    )


def _gate_state() -> CompositionState:
    """One source feeding a boolean gate that routes to the reviewed output."""

    return CompositionState(
        source=None,
        sources={
            "rows": SourceSpec(
                plugin="csv",
                on_success="triage",
                options={"schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="triage",
                node_type="gate",
                plugin=None,
                input="triage",
                on_success=None,
                on_error=None,
                options={},
                condition="row['score'] >= 7",
                routes={"true": "combined", "false": "discard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="combined",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Gate wire review", description=""),
        version=1,
    )


def _schema_output_state(fields: list[object]) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="primary",
                plugin="json",
                options={
                    "path": "/private/result.jsonl",
                    "schema": {
                        "mode": "fixed",
                        "fields": fields,
                        "guaranteed_fields": ["id"],
                        "required_fields": ["email"],
                    },
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Public schema review", description=""),
        version=1,
    )


def _stable_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _wire_authority(state: CompositionState) -> tuple[ProposePipelinePayload, GuidedSession]:
    source_items = [
        {
            "stable_id": _stable_id(index),
            "label": f"source-{index}",
            "plugin": {"kind": "source", "id": source.plugin},
        }
        for index, source in enumerate(state.sources.values(), start=1)
    ]
    node_offset = len(source_items) + 1
    node_items = [
        {
            "stable_id": _stable_id(node_offset + index),
            "label": f"node-{index + 1}",
            "node_type": node.node_type,
            "plugin": ({"kind": "transform", "id": node.plugin} if node.plugin is not None else None),
            "behavior": {"kind": node.node_type},
        }
        for index, node in enumerate(state.nodes)
    ]
    output_offset = node_offset + len(node_items)
    output_items = [
        {
            "stable_id": _stable_id(output_offset + index),
            "label": f"output-{index + 1}",
            "plugin": {"kind": "sink", "id": output.plugin},
        }
        for index, output in enumerate(state.outputs)
    ]
    projection: ProposePipelinePayload = {
        "proposal_id": "00000000-0000-4000-8000-000000000001",
        "draft_hash": "d" * 64,
        "summary": "Review the proposed pipeline.",
        "rationale": "Verify the exact candidate wiring.",
        "component_counts": {
            "sources": len(source_items),
            "nodes": len(node_items),
            "edges": 0,
            "outputs": len(output_items),
        },
        "blockers": [],
        "graph": {"sources": source_items, "edges": []},
        "nodes": node_items,
        "outputs": output_items,
        "edit_targets": [],
    }
    reviewed_sources = {
        public["stable_id"]: SourceResolved(
            name=name,
            plugin=source.plugin,
            options=source.options,
            observed_columns=(),
            sample_rows=(),
            on_validation_failure=source.on_validation_failure,
        )
        for public, (name, source) in zip(source_items, state.sources.items(), strict=True)
    }
    reviewed_outputs = {
        public["stable_id"]: SinkOutputResolved(
            name=output.name,
            plugin=output.plugin,
            options=deep_thaw(output.options),
            required_fields=(),
            schema_mode="observed",
            on_write_failure=output.on_write_failure,
        )
        for public, output in zip(output_items, state.outputs, strict=True)
    }
    guided = GuidedSession(
        step=GuidedStep.STEP_4_WIRE,
        source_order=tuple(reviewed_sources),
        reviewed_sources=reviewed_sources,
        output_order=tuple(reviewed_outputs),
        reviewed_outputs=reviewed_outputs,
    )
    return projection, guided


def _wire_turn(state: CompositionState):
    projection, guided = _wire_authority(state)
    return build_step_4_wire_turn(state, proposal_projection=projection, guided=guided)


class TestBuildSchemaFormTurns:
    def test_step_1_schema_form_uses_knob_schema_payload(self) -> None:
        turn = build_step_1_schema_form_turn("csv", _Catalog())

        assert turn["type"] == TurnType.SCHEMA_FORM.value
        payload = turn["payload"]
        assert payload["mode"] == "plugin_options"
        assert payload["plugin"] == "csv"
        assert payload["knobs"]["fields"][0]["name"] == "path"
        assert "schema_block" not in payload
        assert validate_payload(TurnType.SCHEMA_FORM, payload) is None

    def test_step_1_schema_form_prefills_from_inspection_facts(self) -> None:
        facts = SourceInspectionFacts(
            source_kind="csv",
            redacted_identity={"filename": "input.csv"},
            byte_range_inspected=(0, 32),
            sample_row_count=1,
            observed_headers=("name", "age"),
            inferred_types={"name": "str", "age": "int"},
            url_candidates=(),
            warnings=(),
        )

        turn = build_step_1_schema_form_turn("csv", _Catalog(), inspection_facts=facts)

        schema_prefill = turn["payload"]["prefilled"]["schema"]
        assert schema_prefill == {"mode": "flexible", "fields": ["name: str", "age: int"]}

    def test_step_1_schema_form_keeps_observed_mode_for_unsafe_inspected_header(self) -> None:
        # A Tier-3 uploaded CSV whose header is an env-var placeholder must not be
        # emitted as an explicit schema.fields spec: those strings later flow through
        # the runtime YAML loader, and a raw ``${VAR}`` header would become a
        # config-to-output host-secret exfiltration gadget on the CLI loader path.
        facts = SourceInspectionFacts(
            source_kind="csv",
            redacted_identity={"filename": "input.csv"},
            byte_range_inspected=(0, 64),
            sample_row_count=1,
            observed_headers=("${AWS_SECRET_ACCESS_KEY}", "ok"),
            inferred_types={"${AWS_SECRET_ACCESS_KEY}": "str", "ok": "int"},
            url_candidates=(),
            warnings=(),
        )

        turn = build_step_1_schema_form_turn("csv", _Catalog(), inspection_facts=facts)

        schema_prefill = turn["payload"]["prefilled"]["schema"]
        assert schema_prefill == {"mode": "observed"}

    def test_step_1_schema_form_keeps_observed_mode_for_keyword_header(self) -> None:
        # A header that is a Python keyword ("class") is not a safe explicit field
        # name either — runtime header normalization would rename it, so declaring
        # it as an explicit spec would diverge from actual runtime behaviour.
        facts = SourceInspectionFacts(
            source_kind="csv",
            redacted_identity={"filename": "input.csv"},
            byte_range_inspected=(0, 64),
            sample_row_count=1,
            observed_headers=("class", "ok"),
            inferred_types={"class": "str", "ok": "int"},
            url_candidates=(),
            warnings=(),
        )

        turn = build_step_1_schema_form_turn("csv", _Catalog(), inspection_facts=facts)

        schema_prefill = turn["payload"]["prefilled"]["schema"]
        assert schema_prefill == {"mode": "observed"}

    def test_step_2_schema_form_uses_sink_knobs(self) -> None:
        turn = build_step_2_schema_form_turn("json", _Catalog())

        payload = turn["payload"]
        assert payload["mode"] == "plugin_options"
        assert payload["plugin"] == "json"
        assert payload["knobs"]["fields"][0]["label"] == "Path"
        assert payload["knobs"]["fields"][-1] == {
            "name": "on_write_failure",
            "label": "On Write Failure",
            "description": "Sink name for rows that cannot be written, or 'discard' for explicit drop",
            "kind": "text",
            "required": False,
            "nullable": False,
        }
        assert payload["prefilled"] == {
            "schema": {"mode": "observed"},
            "on_write_failure": "discard",
        }


class TestComponentReviewTurn:
    def test_source_review_is_server_authored_in_persisted_order(self) -> None:
        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"
        guided = GuidedSession(
            step=GuidedStep.STEP_1_SOURCE,
            source_order=(second_id, first_id),
            reviewed_sources={
                first_id: SourceResolved(
                    name="orders",
                    plugin="csv",
                    options={"path": "/private/orders.csv"},
                    observed_columns=("id",),
                    sample_rows=(),
                    on_validation_failure="quarantine",
                ),
                second_id: SourceResolved(
                    name="refunds",
                    plugin="json",
                    options={"path": "/private/refunds.json"},
                    observed_columns=("id",),
                    sample_rows=(),
                    on_validation_failure="discard",
                ),
            },
        )

        turn = build_component_review_turn(guided, "source")

        assert turn == {
            "type": "review_components",
            "step_index": 0,
            "payload": {
                "component_kind": "source",
                "items": [
                    {
                        "stable_id": second_id,
                        "name": "refunds",
                        "plugin": "json",
                        "status": "reviewed",
                    },
                    {
                        "stable_id": first_id,
                        "name": "orders",
                        "plugin": "csv",
                        "status": "reviewed",
                    },
                ],
                "allowed_actions": ["add", "edit", "remove", "reorder", "finish"],
            },
        }
        assert "/private" not in str(turn)
        assert validate_payload(TurnType.REVIEW_COMPONENTS, turn["payload"]) is None

    def test_single_output_review_omits_remove(self) -> None:
        source_id = "11111111-1111-4111-8111-111111111111"
        output_id = "33333333-3333-4333-8333-333333333333"
        guided = GuidedSession(
            step=GuidedStep.STEP_2_SINK,
            source_order=(source_id,),
            reviewed_sources={
                source_id: SourceResolved(
                    name="source",
                    plugin="csv",
                    options={"path": "/private/source.csv"},
                    observed_columns=("id",),
                    sample_rows=(),
                    on_validation_failure="discard",
                )
            },
            output_order=(output_id,),
            reviewed_outputs={
                output_id: SinkOutputResolved(
                    name="primary",
                    plugin="json",
                    options={"path": "/private/output.json"},
                    required_fields=("id",),
                    schema_mode="observed",
                    on_write_failure="quarantine",
                )
            },
        )

        turn = build_component_review_turn(guided, "output")

        assert turn["payload"]["allowed_actions"] == ["add", "edit", "reorder", "finish"]
        assert turn["payload"]["items"] == [
            {
                "stable_id": output_id,
                "name": "primary",
                "plugin": "json",
                "status": "reviewed",
            }
        ]
        assert validate_payload(TurnType.REVIEW_COMPONENTS, turn["payload"]) is None


class TestStep4WireEmitter:
    def test_step_4_wire_index_matches_guided_order(self) -> None:
        assert _step_index(GuidedStep.STEP_4_WIRE) == 3

    def test_builds_confirm_wiring_skeleton_payload(self) -> None:
        turn = _wire_turn(_empty_state())

        assert turn["type"] == TurnType.CONFIRM_WIRING.value
        assert turn["step_index"] == 3
        assert validate_payload(TurnType.CONFIRM_WIRING, turn["payload"]) is None
        payload = turn["payload"]
        assert set(payload.keys()) == {
            "proposal_id",
            "draft_hash",
            "sources",
            "nodes",
            "outputs",
            "connections",
            "semantic_contracts",
            "warnings",
            "blockers",
            "can_confirm",
        }
        assert payload["sources"] == []
        assert payload["nodes"] == []
        assert payload["outputs"] == []
        assert payload["connections"] == []
        assert payload["semantic_contracts"] == []
        assert payload["warnings"] == []
        assert len(payload["blockers"]) == 2
        assert payload["can_confirm"] is False

    def test_emits_queue_node_generically_without_a_queue_branch(self) -> None:
        # A declared queue fan-in flows through the generic emitter unchanged:
        # the canonical queue row appears in the topology and the payload
        # validates, with no queue-specific emitter mutation
        # (elspeth-a5b86149d4 / elspeth-6421ffa028).
        turn = _wire_turn(_queue_state())

        assert turn["type"] == TurnType.CONFIRM_WIRING.value
        assert validate_payload(TurnType.CONFIRM_WIRING, turn["payload"]) is None
        queue = next(node for node in turn["payload"]["nodes"] if node["node_type"] == "queue")
        assert queue["label"] == "node-1"
        assert queue["plugin"] is None
        assert queue["behavior"] == {"kind": "queue"}

    def test_preserves_string_and_mapping_business_schema_fields_without_private_options(self) -> None:
        state = _schema_output_state(
            [
                "id: int",
                {"name": "email", "type": "str", "required": False, "nullable": True},
            ]
        )

        turn = _wire_turn(state)

        business_schema = turn["payload"]["outputs"][0]["business_schema"]
        assert business_schema == {
            "mode": "fixed",
            "fields": [
                {"name": "id", "type": "int", "required": True, "nullable": False},
                {"name": "email", "type": "str", "required": False, "nullable": True},
            ],
            "guaranteed_fields": ["id"],
            "required_fields": ["email"],
        }
        assert "/private/result.jsonl" not in str(turn)
        assert validate_payload(TurnType.CONFIRM_WIRING, turn["payload"]) is None

    def test_aggregation_projection_uses_the_canonical_trigger_contract(self) -> None:
        node = NodeSpec(
            id="batch",
            node_type="aggregation",
            plugin="batch_stats",
            input="batch",
            on_success="primary",
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
            trigger={"count": 25, "timeout_seconds": 12.5, "condition": "batch_ready"},
            output_mode="transform",
            expected_output_count=1,
        )

        assert _node_behavior(node, route_aliases={}, branch_aliases={}) == {
            "kind": "aggregation",
            "trigger_kinds": ["count", "timeout", "condition"],
            "count": "25",
            "timeout_seconds": 12.5,
            "output_mode": "transform",
            "expected_output_count": "1",
        }

    def test_coalesce_projection_preserves_material_timeout_deadlines(self) -> None:
        for policy, timeout_seconds in (
            ("best_effort", 12.5),
            ("quorum", 30.0),
            ("require_all", None),
        ):
            node = NodeSpec(
                id="variants",
                node_type="coalesce",
                plugin=None,
                input="control_done",
                on_success="primary",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={
                    "control_branch": "control_done",
                    "treatment_branch": "treatment_done",
                },
                policy=policy,
                merge="nested",
                timeout_seconds=timeout_seconds,
            )

            assert _node_behavior(
                node,
                route_aliases={},
                branch_aliases={
                    "control_branch": "branch-1",
                    "treatment_branch": "branch-2",
                },
            ) == {
                "kind": "coalesce",
                "branch_aliases": ["branch-1", "branch-2"],
                "policy": policy,
                "merge": "nested",
                "timeout_seconds": timeout_seconds,
            }

    def test_wire_turn_forwards_the_projected_coalesce_timeout_verbatim(self) -> None:
        node = NodeSpec(
            id="variants",
            node_type="coalesce",
            plugin=None,
            input="control_done",
            on_success="primary",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches={
                "control_branch": "control_done",
                "treatment_branch": "treatment_done",
            },
            policy="best_effort",
            merge="nested",
            timeout_seconds=12.5,
        )
        state = CompositionState(
            source=None,
            sources={},
            nodes=(node,),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        projection, guided = _wire_authority(state)
        projected_behavior = {
            "kind": "coalesce",
            "branch_aliases": ["branch-1", "branch-2"],
            "policy": "best_effort",
            "merge": "nested",
            "timeout_seconds": 12.5,
        }
        projection["nodes"][0]["behavior"] = projected_behavior

        turn = build_step_4_wire_turn(
            state,
            proposal_projection=projection,
            guided=guided,
        )

        assert turn["payload"]["nodes"][0]["behavior"] == projected_behavior
        assert validate_payload(TurnType.CONFIRM_WIRING, turn["payload"]) is None

    def test_row_union_projection_preserves_n_to_n_cardinality(self) -> None:
        node = NodeSpec(
            id="variants",
            node_type="row_union",
            plugin=None,
            input="control_done",
            on_success="experiment_rows",
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches={
                "control_branch": "control_done",
                "treatment_branch": "treatment_done",
            },
            policy=None,
            merge=None,
            timeout_seconds=None,
        )

        assert _node_cardinality(node, executable_node=SimpleNamespace()) == {
            "input": "branches",
            "output": "one_per_branch",
            "expected_output_count": None,
        }

    @staticmethod
    def _gate_node(*, condition: str, routes: dict[str, str], fork_to: tuple[str, ...] | None = None) -> NodeSpec:
        return NodeSpec(
            id="triage",
            node_type="gate",
            plugin=None,
            input="triage",
            on_success=None,
            on_error=None,
            options={},
            condition=condition,
            routes=routes,
            fork_to=fork_to,
            branches=None,
            policy=None,
            merge=None,
        )

    def test_boolean_gate_projection_binds_condition_and_ordered_route_keys(self) -> None:
        # F11: the authored predicate travels verbatim and each ordinal route
        # alias is bound 1:1, in route_aliases order, to its boolean route key
        # ("false" sorts before "true" in the canonical route walk).
        node = self._gate_node(condition="row['amount'] > 500", routes={"true": "primary", "false": "review"})

        assert _node_behavior(node, route_aliases={"false": "route-1", "true": "route-2"}, branch_aliases={}) == {
            "kind": "gate",
            "condition": "row['amount'] > 500",
            "route_aliases": ["route-1", "route-2"],
            "routes": [{"alias": "route-1", "key": "false"}, {"alias": "route-2", "key": "true"}],
            "fork_branches": [],
        }

    def test_author_labeled_string_gate_projection_preserves_the_authored_route_keys(self) -> None:
        node = self._gate_node(condition="row['tier']", routes={"high": "escalate", "low": "archive"})

        assert _node_behavior(node, route_aliases={"high": "route-1", "low": "route-2"}, branch_aliases={}) == {
            "kind": "gate",
            "condition": "row['tier']",
            "route_aliases": ["route-1", "route-2"],
            "routes": [{"alias": "route-1", "key": "high"}, {"alias": "route-2", "key": "low"}],
            "fork_branches": [],
        }

    def test_fork_gate_projection_binds_fork_routes_and_keeps_fork_branches_unchanged(self) -> None:
        # Fork gates are NOT rejected by the new bindings: route_aliases and
        # fork_branches keep their exact pre-F11 shape, and the added routes
        # list covers the fork routes too (all derive from the same ordered
        # route walk).
        node = self._gate_node(
            condition="row['ok']",
            routes={"true": "fork", "false": "fork"},
            fork_to=("branch_a", "branch_b"),
        )

        assert _node_behavior(
            node,
            route_aliases={"false": "route-1", "true": "route-2"},
            branch_aliases={"branch_a": "branch-1", "branch_b": "branch-2"},
        ) == {
            "kind": "gate",
            "condition": "row['ok']",
            "route_aliases": ["route-1", "route-2"],
            "routes": [{"alias": "route-1", "key": "false"}, {"alias": "route-2", "key": "true"}],
            "fork_branches": [
                {"routes": ["route-1", "route-2"], "branch": "branch-1"},
                {"routes": ["route-1", "route-2"], "branch": "branch-2"},
            ],
        }

    def test_wire_turn_forwards_the_projected_gate_behavior_verbatim(self) -> None:
        # The wire stage must FORWARD the proposal projection's gate behavior,
        # never re-derive it from candidate state: the projected condition here
        # deliberately differs from the state's authored condition, and the
        # projected value is the one that must reach the wire payload.
        state = _gate_state()
        projection, guided = _wire_authority(state)
        gate_behavior = {
            "kind": "gate",
            "condition": "row['score'] >= 9",
            "route_aliases": ["route-1", "route-2"],
            "routes": [{"alias": "route-1", "key": "false"}, {"alias": "route-2", "key": "true"}],
            "fork_branches": [],
        }
        projection["nodes"][0]["behavior"] = gate_behavior

        turn = build_step_4_wire_turn(state, proposal_projection=projection, guided=guided)

        wire_gate = next(node for node in turn["payload"]["nodes"] if node["node_type"] == "gate")
        assert wire_gate["behavior"] == gate_behavior
        assert wire_gate["behavior"]["condition"] == "row['score'] >= 9"
        assert validate_payload(TurnType.CONFIRM_WIRING, turn["payload"]) is None

    def test_profile_bound_authored_fallback_renders_degraded_cardinality_without_raising(self) -> None:
        # inv-f6 F6 hardening: an authored-only validation_state (operator
        # profile options never lowered because authored validation already
        # errs) must render a degraded wire review — the row-cardinality
        # probe must not crash on the plugin's runtime config rejection.
        state = CompositionState(
            source=None,
            sources={
                "rows": SourceSpec(
                    plugin="csv",
                    on_success="summarize",
                    options={"schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            },
            nodes=(
                NodeSpec(
                    id="summarize",
                    node_type="transform",
                    plugin="llm",
                    input="summarize",
                    on_success="results",
                    on_error="discard",
                    options={
                        "schema": {"mode": "observed"},
                        "profile": "task-role",
                        "prompt_template": "Summarise this row in one short sentence.",
                        "response_field": "summary",
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(
                OutputSpec(
                    name="results",
                    plugin="json",
                    options={"schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(name="Profile-bound wire review", description=""),
            version=1,
        )

        turn = _wire_turn(state)

        assert turn["type"] == TurnType.CONFIRM_WIRING.value
        assert validate_payload(TurnType.CONFIRM_WIRING, turn["payload"]) is None
        llm = next(node for node in turn["payload"]["nodes"] if node["plugin"] == "llm")
        assert llm["row_cardinality"] == {
            "input": "one",
            "output": "zero_or_many",
            "expected_output_count": None,
        }


def _policy_catalog(*, plugin_allowlist: tuple[str, ...] = ()) -> PolicyCatalogView:
    """One request's real catalog projection, built from a real snapshot."""

    class _NoSecrets:
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

    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "plugin_allowlist": plugin_allowlist,
        }
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = create_catalog_service()
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:alice",
        secret_inventory=_NoSecrets(),
        generation_key=b"emitter-test-generation-key",
    )
    return PolicyCatalogView(catalog, snapshot, profiles)


class _SourceCatalog:
    """Catalog stub exposing list_sources for the step-1 single_select path."""

    def list_sources(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name="csv", description="Load rows from a CSV file."),
            SimpleNamespace(name="null", description="A source that yields no rows."),
            SimpleNamespace(name="json", description="Load rows from a JSON file."),
        ]


class TestStep1SourcePicker:
    def test_single_select_excludes_the_degenerate_null_source(self) -> None:
        turn = build_initial_step_1_turn(_empty_state(), blob_inspection=None, catalog=_SourceCatalog())

        assert turn["type"] == TurnType.SINGLE_SELECT.value
        option_ids = [opt["id"] for opt in turn["payload"]["options"]]
        assert "null" not in option_ids
        assert option_ids == ["csv", "json"]
        assert turn["payload"]["source_blob_compatible_option_ids"] == ["csv", "json"]

    def test_picker_never_offers_a_source_the_web_surface_prohibits(self) -> None:
        """The first step must not offer a guaranteed dead end.

        The picker lists whatever its catalog projection lists, so the exclusion
        has to come from the request's availability snapshot rather than a
        second hardcoded hide-list. With ``source:aws_s3`` authorized for the
        deployment, a restricted (web) session must not see it, while the local
        trained-operator projection still does.
        """
        restricted = _policy_catalog(plugin_allowlist=("source:aws_s3", "sink:aws_s3"))

        turn = build_initial_step_1_turn(_empty_state(), blob_inspection=None, catalog=restricted)

        option_ids = [opt["id"] for opt in turn["payload"]["options"]]
        assert "aws_s3" not in option_ids
        assert "csv" in option_ids
        assert "aws_s3" not in turn["payload"]["source_blob_compatible_option_ids"]

        catalog = create_catalog_service()
        trained_snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        trained = PolicyCatalogView.for_trained_operator(catalog, trained_snapshot)

        trained_turn = build_initial_step_1_turn(_empty_state(), blob_inspection=None, catalog=trained)

        assert "aws_s3" in [opt["id"] for opt in trained_turn["payload"]["options"]]


class _SinkCatalog:
    """Catalog stub exposing list_sinks for the step-2 single_select path."""

    def list_sinks(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name="csv", description="Write rows to a CSV file."),
            SimpleNamespace(name="azure_blob", description="Write to Azure Blob Storage."),
            SimpleNamespace(name="json_explode", description=None),
        ]


class TestPluginDisplayLabels:
    """Option labels are human display names; option ids stay the raw plugin id
    (ux review elspeth-5ee1f76e39, backend half — mirrors the frontend
    ``pluginDisplayName`` module)."""

    def test_source_picker_labels_are_humanised_and_values_unchanged(self) -> None:
        turn = build_initial_step_1_turn(_empty_state(), blob_inspection=None, catalog=_SourceCatalog())

        options = {opt["id"]: opt for opt in turn["payload"]["options"]}
        # VALUE (id) unchanged: the raw plugin id is what the client submits.
        assert set(options) == {"csv", "json"}
        # LABEL humanised: acronyms upper-cased.
        assert options["csv"]["label"] == "CSV"
        assert options["json"]["label"] == "JSON"
        # Hints still ride through untouched.
        assert options["csv"]["hint"] == "Load rows from a CSV file."

    def test_sink_picker_labels_use_overrides_and_humaniser(self) -> None:
        from elspeth.web.composer.guided.emitters import build_step_2_single_select_turn

        turn = build_step_2_single_select_turn(_SinkCatalog())

        options = {opt["id"]: opt for opt in turn["payload"]["options"]}
        assert set(options) == {"csv", "azure_blob", "json_explode"}
        # Curated override beats the humaniser.
        assert options["azure_blob"]["label"] == "Azure Blob Storage"
        # Humanised fallback: underscores to spaces, acronyms upper-cased.
        assert options["json_explode"]["label"] == "JSON Explode"
        assert options["csv"]["label"] == "CSV"
        assert turn["payload"]["source_blob_compatible_option_ids"] == []

    def test_plugin_display_label_helper_directly(self) -> None:
        from elspeth.web.composer.guided._display import plugin_display_label

        assert plugin_display_label("batch_top_k") == "Batch Top-K"
        assert plugin_display_label("dataverse") == "Microsoft Dataverse"
        assert plugin_display_label("web_scrape") == "Web Scrape"
        assert plugin_display_label("llm_transform") == "LLM Transform"


class TestSchemaFormPathMask:
    """Fix B: a blob-backed source's absolute storage_path must never reach the
    wire — it is rendered as a stable blob:<ref> sentinel that the step_1 commit
    handler re-resolves to the real path."""

    def test_step_1_from_resolved_masks_blob_backed_path(self) -> None:
        source = SourceResolved(
            name="source",
            plugin="json",
            options={
                "path": "/home/someuser/elspeth/data/blobs/sess/abc123_urls.json",
                "blob_ref": "abc123",
                "schema": {"mode": "observed"},
            },
            observed_columns=("url",),
            sample_rows=(),
            on_validation_failure="discard",
        )
        turn = build_step_1_schema_form_turn_from_resolved(source, _Catalog())
        assert turn["payload"]["prefilled"]["path"] == "blob:abc123"
        # the absolute path (deploy dir + OS username) is gone from the payload
        assert "/home/someuser" not in str(turn["payload"]["prefilled"])

    def test_step_1_from_resolved_leaves_non_blob_path_untouched(self) -> None:
        source = SourceResolved(
            name="source",
            plugin="json",
            options={"path": "data/input.json", "schema": {"mode": "observed"}},
            observed_columns=("url",),
            sample_rows=(),
            on_validation_failure="discard",
        )
        turn = build_step_1_schema_form_turn_from_resolved(source, _Catalog())
        assert turn["payload"]["prefilled"]["path"] == "data/input.json"

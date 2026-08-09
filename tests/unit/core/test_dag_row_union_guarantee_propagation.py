# tests/unit/core/test_dag_row_union_guarantee_propagation.py
"""row_union publishes the intersection of its branch guarantees.

Sibling of the queue fan-in fix (test_dag_queue_guarantee_propagation.py,
elspeth-5a372d3267). The builder assigns every row_union node a hardcoded
``SchemaConfig(mode="observed", fields=None)``, so
``walk_effective_guarantee_vote`` stopped at the barrier and reported an
empty guarantee. ``validate_single_edge`` then compared union consumers
against ``frozenset()`` and falsely rejected runnable
fork → branches → row_union → consumer pipelines with
"guarantees: (none - dynamic schema)" (elspeth-41bcaa882e, battery-2026-08-06
g08: branches guaranteeing source fields feeding a field_mapper that
requires them).

row_union is a correlated UNION ALL: every released row is exactly one
branch's payload, unchanged. The sound fan-in rule is therefore the queue
rule, not ``compose_propagation``'s abstainer-skip (which is correct only
when every predecessor delivers the SAME row):

- all branches participate → intersection of branch guarantees, participated=True
- any branch abstains      → the union abstains entirely (an unconstrained
  branch means no field can be vouched for on every released row)
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts import NodeType
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.config import (
    GateSettings,
    RowUnionSettings,
    SourceSettings,
    TransformSettings,
)
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.guarantees import (
    get_effective_guaranteed_fields,
    walk_effective_guarantee_vote,
)
from elspeth.core.dag.models import EdgeContractError, GraphValidationError
from elspeth.core.dag.wiring import WiredTransform


class _SourceWithGuarantees:
    """Mock source declaring guaranteed_fields via its config schema."""

    output_schema = None
    _output_schema_config: SchemaConfig | None = None
    _on_validation_failure = "discard"

    def __init__(self, guaranteed: tuple[str, ...]) -> None:
        self.name = "mock_source"
        self.on_success = "routed"
        if guaranteed:
            self.config = {"schema": {"mode": "observed", "guaranteed_fields": list(guaranteed)}}
        else:
            self.config = {"schema": {"mode": "observed"}}


class _BranchTransform:
    """Mock branch-chain transform.

    ``passes_through_input=True`` with declared extra guarantees models a
    branch that annotates rows (the queue file has no analogue — queues have
    no per-arm chains). ``passes_through_input=False`` with a bare observed
    schema models an opaque llm-style branch that abstains.
    """

    input_schema = None
    output_schema = None
    on_error: str | None = None
    declared_output_fields: frozenset[str] = frozenset()
    declared_input_fields: frozenset[str] = frozenset()
    declared_string_input_fields: frozenset[str] = frozenset()
    forwards_input_fields: bool = False
    removed_input_fields: frozenset[str] = frozenset()

    def __init__(
        self,
        name: str,
        *,
        guaranteed: tuple[str, ...] = (),
        passes_through: bool = True,
    ) -> None:
        self.name = name
        self.passes_through_input = passes_through
        schema: dict[str, Any] = {"mode": "observed"}
        if guaranteed:
            schema["guaranteed_fields"] = list(guaranteed)
        self.config = {"schema": schema}
        self._output_schema_config = SchemaConfig(
            mode="observed",
            fields=None,
            guaranteed_fields=guaranteed or None,
        )


class _RequiringTransform:
    """Mock union consumer declaring requirements via either config surface."""

    input_schema = None
    output_schema = None
    on_error: str | None = None
    declared_output_fields: frozenset[str] = frozenset()
    declared_input_fields: frozenset[str] = frozenset()
    declared_string_input_fields: frozenset[str] = frozenset()
    passes_through_input: bool = False
    forwards_input_fields: bool = False
    removed_input_fields: frozenset[str] = frozenset()

    def __init__(self, required: tuple[str, ...], *, via: str = "required_input_fields") -> None:
        self.name = "union_consumer"
        if via == "required_input_fields":
            self.config = {
                "schema": {"mode": "observed"},
                "required_input_fields": list(required),
            }
        elif via == "schema.required_fields":
            self.config = {"schema": {"mode": "observed", "required_fields": list(required)}}
        else:  # pragma: no cover - test bug
            raise ValueError(f"unknown requirement surface: {via}")
        self._output_schema_config = SchemaConfig(mode="observed", fields=None)


class _BuilderMockSink:
    name = "mock_sink"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure: str = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


def _build_row_union_graph(
    *,
    source_guaranteed: tuple[str, ...],
    branch_transforms: dict[str, _BranchTransform | None],
    consumer_required: tuple[str, ...],
    consumer_via: str = "required_input_fields",
) -> ExecutionGraph:
    """source → fork gate → branches (identity or 1-transform chain) → row_union → consumer → sink."""
    source = _SourceWithGuarantees(source_guaranteed)
    consumer = _RequiringTransform(consumer_required, via=consumer_via)

    branches: dict[str, str] = {}
    wired: list[WiredTransform] = []
    for branch_name, transform in branch_transforms.items():
        if transform is None:
            branches[branch_name] = branch_name
            continue
        chain_out = f"{branch_name}_scored"
        branches[branch_name] = chain_out
        wired.append(
            WiredTransform(
                plugin=transform,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name=transform.name,
                    plugin=transform.name,
                    input=branch_name,
                    on_success=chain_out,
                    on_error="discard",
                    options={},
                ),
            )
        )
    wired.append(
        WiredTransform(
            plugin=consumer,  # type: ignore[arg-type]
            settings=TransformSettings(
                name="consumer",
                plugin=consumer.name,
                input="union_out",
                on_success="output",
                on_error="discard",
                options={},
            ),
        )
    )

    return ExecutionGraph.from_plugin_instances(
        sources={"rows": source},  # type: ignore[dict-item]
        source_settings_map={"rows": SourceSettings(plugin=source.name, on_success="routed", options={})},
        transforms=wired,
        sinks={"output": _BuilderMockSink()},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="variant_fork",
                input="routed",
                condition="True",
                routes={"true": "fork", "false": "output"},
                fork_to=list(branch_transforms),
            )
        ],
        row_union_settings=[
            RowUnionSettings(name="variant_union", branches=branches, on_success="union_out"),
        ],
    )


def _row_union_node_id(graph: ExecutionGraph) -> str:
    union_nodes = [n for n in graph.get_nodes() if n.node_type == NodeType.ROW_UNION]
    assert len(union_nodes) == 1
    return union_nodes[0].node_id


class TestRowUnionGuaranteeTransparency:
    def test_identity_branch_guarantees_pass_through_union(self) -> None:
        """The battery g08 shape: guaranteed source fields must survive the barrier.

        Before the fix the build raised GraphValidationError on the
        row_union → consumer edge: "Producer (row_union:variant_union)
        guarantees: (none - dynamic schema)" despite every branch delivering
        rows that carry the source's guaranteed fields unchanged.
        """
        graph = _build_row_union_graph(
            source_guaranteed=("id", "amount"),
            branch_transforms={"control_branch": None, "treatment_branch": None},
            consumer_required=("amount",),
        )
        assert get_effective_guaranteed_fields(graph, _row_union_node_id(graph)) == frozenset({"id", "amount"})

    def test_schema_required_fields_surface_is_the_same_contract(self) -> None:
        """g08-s2/s3 declared schema.required_fields, not required_input_fields."""
        graph = _build_row_union_graph(
            source_guaranteed=("id", "amount"),
            branch_transforms={"control_branch": None, "treatment_branch": None},
            consumer_required=("amount",),
            consumer_via="schema.required_fields",
        )
        assert get_effective_guaranteed_fields(graph, _row_union_node_id(graph)) == frozenset({"id", "amount"})

    def test_transform_chain_branch_forwards_guarantees(self) -> None:
        """A pass-through branch chain keeps the source's guarantees flowing."""
        graph = _build_row_union_graph(
            source_guaranteed=("id", "amount"),
            branch_transforms={
                "control_branch": None,
                "treatment_branch": _BranchTransform("treatment_tagger", guaranteed=("treatment_tag",)),
            },
            consumer_required=("amount",),
        )
        assert get_effective_guaranteed_fields(graph, _row_union_node_id(graph)) == frozenset({"id", "amount"})

    def test_branch_only_field_is_not_union_guaranteed(self) -> None:
        """A field guaranteed by only ONE branch must NOT satisfy a union consumer.

        Control rows never carry treatment_tag, and every released group
        contains a control row, so requiring it downstream of the union is a
        real contract violation — the fail-closed direction must survive the
        transparency fix.
        """
        with pytest.raises(GraphValidationError, match="treatment_tag"):
            _build_row_union_graph(
                source_guaranteed=("id", "amount"),
                branch_transforms={
                    "control_branch": None,
                    "treatment_branch": _BranchTransform("treatment_tagger", guaranteed=("treatment_tag",)),
                },
                consumer_required=("treatment_tag",),
            )

    def test_abstaining_branch_collapses_union_to_abstention(self) -> None:
        """One opaque (dynamic-output) branch means the union can vouch for nothing.

        The consumer's requirement must still fail (fail-closed), and the
        union's vote must ABSTAIN rather than participate-with-empty, so sink
        validation keeps deferring dynamic upstreams to per-row enforcement
        (elspeth-3283f2eaec).
        """
        opaque = {
            "control_branch": None,
            "treatment_branch": _BranchTransform("opaque_llm", passes_through=False),
        }
        with pytest.raises(GraphValidationError, match="amount"):
            _build_row_union_graph(
                source_guaranteed=("id", "amount"),
                branch_transforms=dict(opaque),
                consumer_required=("amount",),
            )

        graph = _build_row_union_graph(
            source_guaranteed=("id", "amount"),
            branch_transforms=dict(opaque),
            consumer_required=(),
        )
        vote = walk_effective_guarantee_vote(graph, _row_union_node_id(graph), {})
        assert vote.fields == frozenset()
        assert vote.participated is False

    def test_all_branches_abstaining_union_abstains(self) -> None:
        """Control: fully dynamic upstreams keep today's abstention behavior."""
        graph = _build_row_union_graph(
            source_guaranteed=(),
            branch_transforms={"control_branch": None, "treatment_branch": None},
            consumer_required=(),
        )
        vote = walk_effective_guarantee_vote(graph, _row_union_node_id(graph), {})
        assert vote.fields == frozenset()
        assert vote.participated is False


class TestMissingFieldsFailureIsStructured:
    """The Phase-1 missing-fields family must carry a repair affordance.

    battery-2026-08-06 g08-s2/s3 surfaced this failure with suggestion:null:
    the raise was the bare GraphValidationError, which the composer preflight
    cannot build patch advice from. It must raise the structured
    EdgeContractError (a GraphValidationError subclass, so legacy catch sites
    keep working) carrying the missing fields and the producer's node type —
    the graph does not exist yet at the build-time catch site, so the raise
    site is the only place row_union-ness can come from.
    """

    def test_raises_edge_contract_error_with_structured_fields(self) -> None:
        with pytest.raises(EdgeContractError) as exc_info:
            _build_row_union_graph(
                source_guaranteed=(),
                branch_transforms={"control_branch": None, "treatment_branch": None},
                consumer_required=("amount",),
            )
        exc = exc_info.value
        assert exc.compatibility_result.missing_fields == ("amount",)
        assert exc.from_component_type == "row_union"
        assert exc.component_type == "transform"

    def test_remediation_names_both_declaration_surfaces(self) -> None:
        """g08's remediation said required_input_fields to a node that had
        declared schema.required_fields — the wrong-key text stranded repair.
        The requirement set merges both surfaces, so the message must name
        both."""
        for via in ("required_input_fields", "schema.required_fields"):
            with pytest.raises(EdgeContractError) as exc_info:
                _build_row_union_graph(
                    source_guaranteed=(),
                    branch_transforms={"control_branch": None, "treatment_branch": None},
                    consumer_required=("amount",),
                    consumer_via=via,
                )
            message = str(exc_info.value)
            assert "required_input_fields" in message
            assert "schema.required_fields" in message

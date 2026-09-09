# tests/unit/engine/test_plugin_detection.py
"""Tests for plugin dispatch in the processor.

elspeth-8783933d99: the engine dispatches nominally on GateSettings (negative
form) over ``node_to_plugin``, which is closed by construction
(graph_wiring builds it from config.transforms + config.gates and raises on
anything else). Protocol conformance is measured, not declared — it is
deliberately NOT a dispatch control, because widening the runtime_checkable
TransformProtocol silently de-classified every non-conforming implementation
tree-wide and the engine raised mid-traversal (ef5e6e593).
"""

from typing import Any

from elspeth.contracts import NodeType, SourceRow
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.types import NodeID
from elspeth.engine.processor import DAGTraversalContext
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from tests.fixtures.base_classes import create_observed_contract
from tests.fixtures.factories import make_context


def _single_node_traversal(source_node_id: NodeID, node_id: NodeID, plugin: Any) -> DAGTraversalContext:
    """Build explicit traversal context for a one-node pipeline."""
    return DAGTraversalContext(
        node_step_map={source_node_id: 0, node_id: 1},
        node_to_plugin={node_id: plugin},
        node_to_next={source_node_id: node_id, node_id: None},
        coalesce_node_map={},
    )


class TestPluginTypeDetection:
    """Tests for isinstance-based plugin detection."""

    def test_transform_is_base_transform(self) -> None:
        """Transforms should be instances of BaseTransform."""
        from elspeth.plugins.transforms.passthrough import PassThrough

        transform = PassThrough({"schema": {"mode": "observed"}})
        assert isinstance(transform, BaseTransform)

    def test_unknown_type_is_not_recognized(self) -> None:
        """Unknown plugin types should not match any base class."""

        class UnknownPlugin:
            """A class that is not a proper plugin."""

            pass

        unknown = UnknownPlugin()
        assert not isinstance(unknown, BaseTransform)

    def test_duck_typed_transform_not_recognized(self) -> None:
        """Duck-typed transforms without inheritance should NOT be recognized.

        This is the key behavior change - hasattr checks would have accepted
        this class, but isinstance checks correctly reject it.
        """

        class DuckTypedTransform:
            """Looks like a transform but doesn't inherit from BaseTransform."""

            name = "duck"

            def process(self, row: dict[str, Any], ctx: PluginContext) -> TransformResult:
                return TransformResult.success(row.to_dict(), success_reason={"action": "test"})  # type: ignore[attr-defined]

        duck = DuckTypedTransform()
        # Has the method but NOT an instance of BaseTransform
        assert hasattr(duck, "process")
        assert not isinstance(duck, BaseTransform)  # type: ignore[unreachable]


class TestNegativeNominalDispatch:
    """Non-conforming transform-shaped plugins still dispatch as transforms.

    Mechanism pin for elspeth-8783933d99: reverting token_traversal's dispatch
    to structural TransformProtocol classification makes this fail with
    ``TypeError: Unknown transform type`` — the exact mid-traversal failure
    that widening the protocol (ef5e6e593, preserves_input_values) produced
    against every plugin lacking the new member.
    """

    def test_non_conforming_transform_dispatches_as_transform(self) -> None:
        """An object missing a TransformProtocol member still executes as a transform.

        node_to_plugin is closed by construction (graph_wiring): anything that
        is not GateSettings IS a transform. Protocol membership must not be
        re-measured at dispatch time.
        """
        from elspeth.contracts import TransformProtocol
        from elspeth.contracts.types import NodeID
        from elspeth.engine.processor import RowProcessor
        from elspeth.engine.spans import SpanFactory
        from tests.fixtures.landscape import make_recorder_with_run, register_test_node
        from tests.fixtures.nonconforming_transform import NonConformingTransform

        transform = NonConformingTransform(node_id="nonconforming_node", on_error="discard")
        # Precondition, not the pin: the fake genuinely fails structural
        # conformance (it lacks preserves_input_values), so a pass below can
        # only come from dispatch NOT measuring the protocol.
        assert not isinstance(transform, TransformProtocol)

        setup = make_recorder_with_run()
        factory, run_id, source_node_id = setup.factory, setup.run_id, setup.source_node_id
        transform_node_id = NodeID(
            register_test_node(
                setup.data_flow,
                run_id,
                transform.node_id,
                node_type=NodeType.TRANSFORM,
                plugin_name=transform.name,
            )
        )

        processor = RowProcessor(
            setup.execution,
            setup.data_flow,
            span_factory=SpanFactory(),
            run_id=run_id,
            source_node_id=NodeID(source_node_id),
            source_on_success="default",
            traversal=_single_node_traversal(NodeID(source_node_id), transform_node_id, transform),
            scheduler=setup.factory.scheduler,
        )

        ctx = make_context(run_id=run_id, landscape=factory.plugin_audit_writer())

        results = processor.process_row(
            row_index=0,
            source_row=SourceRow.valid({"value": 1}, contract=create_observed_contract({"value": 1}), source_row_index=0),
            transforms=[transform],
            ctx=ctx,
            source_row_index=0,
            ingest_sequence=0,
        )

        assert transform.process_called, "dispatch must reach the transform arm and execute process()"
        assert results, "the row must complete traversal, not die in dispatch"

"""Compare immutable Landscape plugin evidence before resume takes leadership."""

from collections.abc import Mapping

from elspeth.contracts import ResumeCheck
from elspeth.contracts.types import NodeID
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator.graph_wiring import build_source_id_map
from elspeth.engine.orchestrator.landscape_registration import NodeAuditMetadata, resolve_node_audit_metadata
from elspeth.engine.orchestrator.types import PipelineConfig


def compare_implementation_metadata(
    recorded: Mapping[NodeID, NodeAuditMetadata], current: Mapping[NodeID, NodeAuditMetadata]
) -> ResumeCheck:
    """Require the exact registered node set and implementation evidence."""
    if recorded.keys() != current.keys():
        return ResumeCheck(can_resume=False, reason="Plugin implementation baseline has a different node set")
    for node_id, metadata in current.items():
        if recorded[node_id] != metadata:
            return ResumeCheck(can_resume=False, reason=f"Plugin implementation changed for node {node_id!r}")
    return ResumeCheck(can_resume=True)


def check_implementation_compatibility(factory: RecorderFactory, run_id: str, config: PipelineConfig, graph: ExecutionGraph) -> ResumeCheck:
    """Read the original node baseline, never regenerate it from current code."""
    recorded = {
        NodeID(node.node_id): NodeAuditMetadata(
            plugin_version=node.plugin_version, determinism=node.determinism, source_file_hash=node.source_file_hash
        )
        for node in factory.data_flow.get_nodes(run_id)
    }
    current = resolve_node_audit_metadata(
        config,
        graph,
        source_id_map=build_source_id_map(graph),
        transform_id_map=graph.get_transform_id_map(),
        sink_id_map=graph.get_sink_id_map(),
        config_gate_node_ids=set(graph.get_config_gate_id_map().values()),
        aggregation_node_ids=set(graph.get_aggregation_id_map().values()),
        coalesce_node_ids=set(graph.get_coalesce_id_map().values()),
        collector_id_map=graph.get_collector_id_map(),
        collector_transforms=graph.get_collector_transform_map(),
    )
    return compare_implementation_metadata(recorded, current)

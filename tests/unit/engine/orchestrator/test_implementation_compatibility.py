"""Resume implementation evidence is independent of graph configuration."""

from dataclasses import replace

from elspeth.contracts.types import NodeID
from elspeth.engine.orchestrator.implementation_compatibility import compare_implementation_metadata
from elspeth.engine.orchestrator.landscape_registration import NodeAuditMetadata
from tests.fixtures.plugins import ListSource


def test_same_implementation_accepts() -> None:
    source = ListSource([])
    metadata = NodeAuditMetadata(plugin_version=source.plugin_version, determinism=source.determinism, source_file_hash="a" * 64)
    assert compare_implementation_metadata({NodeID("source"): metadata}, {NodeID("source"): metadata}).can_resume


def test_changed_implementation_refuses_same_graph() -> None:
    source = ListSource([])
    metadata = NodeAuditMetadata(plugin_version=source.plugin_version, determinism=source.determinism, source_file_hash="a" * 64)
    check = compare_implementation_metadata({NodeID("source"): metadata}, {NodeID("source"): replace(metadata, source_file_hash="b" * 64)})
    assert not check.can_resume
    assert "implementation" in check.reason

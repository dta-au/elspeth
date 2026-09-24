"""Audited source input can be reconstructed from an actual completed run."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from elspeth.contracts import ArtifactDescriptor, PluginSchema, SourceRow
from elspeth.contracts.diversion import SinkWriteResult
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.source_replay import prepare_audited_sources
from tests.fixtures.base_classes import _TestSinkBase, _TestSourceBase, as_sink, as_source
from tests.fixtures.pipeline import build_production_graph


class _ReplaySchema(PluginSchema):
    value: int


class _AuditedSource(_TestSourceBase):
    name = "audited_source"
    output_schema = _ReplaySchema
    on_success = "output"

    def __init__(self) -> None:
        super().__init__()
        self.load_count = 0

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        self.load_count += 1
        yield from self.wrap_rows([{"value": 7}, {"value": 8}])


class _AuditedSink(_TestSinkBase):
    name = "audited_sink"

    def write(self, rows: Any, ctx: Any) -> SinkWriteResult:
        return SinkWriteResult(
            artifact=ArtifactDescriptor.for_file(path="memory://replay-source", size_bytes=len(rows), content_hash="a" * 64)
        )


def test_prepare_audited_sources_reconstructs_live_run_without_source_load(tmp_path: Path) -> None:
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    source = _AuditedSource()
    config = PipelineConfig(sources={"primary": as_source(source)}, transforms=[], sinks={"output": as_sink(_AuditedSink())}, config={})
    result = Orchestrator(db).run(config, graph=build_production_graph(config), payload_store=payload_store)
    assert source.load_count == 1

    read = RecorderFactory.read_only(db, payload_store=payload_store)
    plan = prepare_audited_sources(read, result.run_id, config.sources)

    assert [row.row for row in plan["primary"].rows] == [{"value": 7}, {"value": 8}]
    assert [row.source_row_index for row in plan["primary"].rows] == [0, 1]
    assert source.load_count == 1
    db.close()

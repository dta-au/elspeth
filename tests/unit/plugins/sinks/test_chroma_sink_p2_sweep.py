"""Chroma sink document-ID defects from the 20261111-plugins-review sweep.

Both findings are row-attributable ID defects that must divert the offending
member at prepare time (like the other invalid-ID cases of elspeth-32bf1a9b63)
instead of failing or wedging the whole effect group:

- #90: an empty-string document ID passed preparation and Chroma rejected it at
  upsert with ``InvalidArgumentError`` (a ``ChromaError``, not a ``ValueError``)
  on every attempt.
- #91: duplicate document IDs within one effect group raised out of
  ``prepare_effect`` and failed the whole plan, blocking valid siblings.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from chromadb.errors import InvalidArgumentError

from elspeth.contracts import NodeType
from elspeth.contracts.sink_effects import SinkEffectFinalizationResult, SinkEffectState
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator
from elspeth.plugins.sinks.chroma_sink import ChromaSink
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db, register_test_node
from tests.unit.engine.test_chroma_member_effect_diversion import _build_request, _make_sink, _RecordingCollection


class _ChromaIdRulesCollection(_RecordingCollection):
    """Recording collection that enforces chromadb 1.5.5's empty-ID rejection."""

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, object]] | None,
    ) -> None:
        if any(document_id == "" for document_id in ids):
            raise InvalidArgumentError("Empty ID, ID must have at least one character")
        super().upsert(ids=ids, documents=documents, metadatas=metadatas)


@dataclass
class _EffectRun:
    factory: Any
    sink: ChromaSink
    collection: _RecordingCollection
    outcome: SinkEffectFinalizationResult | Exception


@contextmanager
def _execute(rows: list[dict[str, Any]]) -> Iterator[_EffectRun]:
    """Run one effect group through the real coordinator; capture an escaping error as the outcome."""
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
        sink_id = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="chroma_sink")
        request = _build_request(factory, run.run_id, source_id, sink_id, rows)
        collection = _ChromaIdRulesCollection()
        sink = _make_sink(collection)
        coordinator = SinkEffectCoordinator(
            factory=factory,
            worker_id="worker-a",
            lease_ttl=timedelta(minutes=5),
            coordination_token=leader_token_for(db, run.run_id),
        )
        outcome: SinkEffectFinalizationResult | Exception
        try:
            outcome = coordinator.execute(request, sink)
        except (InvalidArgumentError, ValueError) as exc:  # the defects under test escape as these
            outcome = exc
        yield _EffectRun(factory=factory, sink=sink, collection=collection, outcome=outcome)
    finally:
        db.close()


def test_empty_string_document_id_diverts_at_prepare_while_siblings_land() -> None:
    rows: list[dict[str, Any]] = [
        {"doc_id": "d0", "text": "document 0", "topic": "test"},
        {"doc_id": "", "text": "document 1", "topic": "test"},
        {"doc_id": "d2", "text": "document 2", "topic": "test"},
    ]
    with _execute(rows) as run:
        assert not isinstance(run.outcome, Exception), f"empty ID escaped the effect instead of diverting: {run.outcome!r}"
        assert run.outcome.effect.state is SinkEffectState.FINALIZED
        assert set(run.collection.documents) == {"d0", "d2"}
        members = run.factory.execution.sink_effects.get_members(run.outcome.effect.effect_id)
        assert [member.prepared_disposition for member in members] == ["accepted", "diverted", "accepted"]
        assert members[1].reason_hash is not None
        live = {item.row_index: item.reason for item in run.sink._get_diversions()}
        assert set(live) == {1}
        assert "empty" in live[1]

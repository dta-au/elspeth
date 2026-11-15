"""Per-member prepare-time diversion for Chroma effects.

Regression coverage for elspeth-32bf1a9b63: an invalid member (bad ID/document/
metadata/non-finite float) must divert individually during preparation while
valid siblings continue, instead of aborting the whole plan on every attempt.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import chromadb
import numpy as np
from chromadb.api.types import EmbeddingFunction, normalize_insert_record_set, validate_insert_record_set
from chromadb.config import Settings
from chromadb.utils.embedding_functions import known_embedding_functions

from elspeth.contracts import NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.sink_effects import (
    SinkEffectFinalizationMember,
    SinkEffectMemberCandidate,
    SinkEffectPipelineMembersInput,
    SinkEffectState,
)
from elspeth.core.landscape.execution.sink_effect_identity import (
    compute_pipeline_effect_identity,
    resolve_sink_effect_members,
)
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, SinkEffectExecutionRequest
from elspeth.plugins.sinks.chroma_sink import ChromaSink
from tests.fixtures.base_classes import inject_write_failure
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import leader_coordination_token, leader_token_for, make_factory, make_landscape_db, register_test_node
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_request


class _RecordingCollection:
    def __init__(self) -> None:
        self.documents: dict[str, tuple[str, dict[str, object] | None]] = {}
        self._client = self
        self.id = "diverting-members"
        self.tenant = "default_tenant"
        self.database = "default_database"

    def _validate_and_prepare_upsert_request(self, **kwargs: Any) -> dict[str, Any]:
        records = normalize_insert_record_set(**kwargs)
        validate_insert_record_set(records)
        return {
            "ids": records["ids"],
            "documents": records["documents"],
            "metadatas": records["metadatas"],
            "embeddings": [np.array([1.0, 0.0], dtype=np.float32)],
            "uris": records["uris"],
        }

    def _upsert(
        self,
        *,
        collection_id: str,
        ids: list[str],
        embeddings: list[np.ndarray],
        documents: list[str],
        metadatas: list[dict[str, object]] | None,
        uris: list[str] | None,
        tenant: str,
        database: str,
    ) -> None:
        del collection_id, embeddings, uris, tenant, database
        assert len(ids) == len(documents) == 1
        self.documents[ids[0]] = (documents[0], None if metadatas is None else dict(metadatas[0]))

    def get(self, *, ids: list[str], include: list[str] | None = None) -> dict[str, object]:
        del include
        found = [document_id for document_id in ids if document_id in self.documents]
        return {
            "ids": found,
            "documents": [self.documents[document_id][0] for document_id in found],
            "metadatas": [self.documents[document_id][1] for document_id in found],
        }


def _chroma_config() -> dict[str, object]:
    return {
        "collection": "diverting-members",
        "mode": "persistent",
        "persist_directory": "./unused",
        "field_mapping": {
            "document_field": "text",
            "id_field": "doc_id",
            "metadata_fields": ["topic"],
        },
        "on_duplicate": "overwrite",
        "schema": {"mode": "observed"},
    }


def _make_sink(collection: _RecordingCollection) -> ChromaSink:
    sink = inject_write_failure(ChromaSink(_chroma_config()))
    sink._collection = collection  # type: ignore[assignment]
    return sink


class _RejectingLocalEmbedding(EmbeddingFunction):
    def __init__(self) -> None:
        pass

    def __call__(self, input: list[str]) -> list[np.ndarray]:
        if "REJECT" in input:
            raise ValueError("document rejected by local embedding policy")
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in input]

    @staticmethod
    def name() -> str:
        return "chroma_member_local_policy"

    def get_config(self) -> dict[str, object]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, object]) -> _RejectingLocalEmbedding:
        return _RejectingLocalEmbedding()


def _build_request(factory, run_id: str, source_id: str, sink_id: str, rows: list[dict[str, Any]]) -> SinkEffectExecutionRequest:
    candidates: list[SinkEffectMemberCandidate] = []
    for ordinal, payload in enumerate(rows):
        _row, token = factory.data_flow.create_row_with_token(
            coordination_token=leader_coordination_token(factory, run_id),
            source_node_id=source_id,
            row_index=ordinal,
            data=payload,
            source_row_index=ordinal,
            ingest_sequence=ordinal,
        )
        factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id=sink_id,
            member_token=leader_coordination_token(factory, run_id).membership,
            step_index=0,
            input_data=payload,
        )
        candidates.append(SinkEffectMemberCandidate(token_id=token.token_id, row=payload))
    members = resolve_sink_effect_members(factory, candidates)
    reservation = _pipeline_request(run_id, sink_id, members)
    identity = compute_pipeline_effect_identity(
        run_id=run_id,
        sink_node_id=sink_id,
        role=reservation.role,
        sink_config={"name": "chroma_sink"},
        target_config={"collection": "diverting-members"},
        members=tuple(replace(member, member_effect_id=None) for member in members),
    )
    return SinkEffectExecutionRequest(
        reservation=reservation,
        effect_input=SinkEffectPipelineMembersInput(identity.members, identity.members, len(identity.members)),
        finalization_members=tuple(
            SinkEffectFinalizationMember(
                ordinal=member.ordinal,
                output_data=dict(member.row),
                duration_ms=0,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.DEFAULT_FLOW,
                sink_name="chroma_sink",
            )
            for member in identity.members
        ),
    )


def test_invalid_member_diverts_during_preparation_while_valid_siblings_continue() -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
        sink_id = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="chroma_sink")
        rows: list[dict[str, Any]] = [
            {"doc_id": "d0", "text": "document 0", "topic": "test"},
            {"doc_id": "d1", "text": 123, "topic": "test"},  # non-string document
            {"doc_id": "d2", "text": "document 2", "topic": {"nested": True}},  # non-scalar metadata
            {"doc_id": "d3", "text": "document 3", "topic": "test"},
        ]
        request = _build_request(factory, run.run_id, source_id, sink_id, rows)
        collection = _RecordingCollection()
        sink = _make_sink(collection)

        result = SinkEffectCoordinator(
            factory=factory,
            worker_id="worker-a",
            lease_ttl=timedelta(minutes=5),
            coordination_token=leader_token_for(db, run.run_id),
        ).execute(request, sink)

        assert result.effect.state is SinkEffectState.FINALIZED
        # Valid siblings landed; invalid members never reached the collection.
        assert set(collection.documents) == {"d0", "d3"}

        members = factory.execution.sink_effects.get_members(result.effect.effect_id)
        assert [member.prepared_disposition for member in members] == ["accepted", "diverted", "diverted", "accepted"]
        assert all(member.member_state is SinkEffectState.FINALIZED for member in members)
        # Prepare-time attribution is durable on the member records.
        assert members[1].reason_hash is not None
        assert members[2].reason_hash is not None

        # Live diversion log carries the real per-member reasons.
        live = {item.row_index: item.reason for item in sink._get_diversions()}
        assert set(live) == {1, 2}
        assert "string" in live[1]
        assert "scalar" in live[2]
    finally:
        db.close()


def test_all_invalid_members_divert_without_wedging_the_batch() -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
        sink_id = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="chroma_sink")
        rows: list[dict[str, Any]] = [
            {"doc_id": "d0", "text": 1, "topic": "test"},
            {"doc_id": 5, "text": "document 1", "topic": "test"},
        ]
        request = _build_request(factory, run.run_id, source_id, sink_id, rows)
        collection = _RecordingCollection()
        sink = _make_sink(collection)

        result = SinkEffectCoordinator(
            factory=factory,
            worker_id="worker-a",
            lease_ttl=timedelta(minutes=5),
            coordination_token=leader_token_for(db, run.run_id),
        ).execute(request, sink)

        assert result.effect.state is SinkEffectState.FINALIZED
        assert collection.documents == {}
        members = factory.execution.sink_effects.get_members(result.effect.effect_id)
        assert [member.prepared_disposition for member in members] == ["diverted", "diverted"]
    finally:
        db.close()


def test_existing_persistent_collection_embedding_rejection_diverts_sibling(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    monkeypatch.setitem(known_embedding_functions, _RejectingLocalEmbedding.name(), _RejectingLocalEmbedding)
    path = str(tmp_path / "store")
    client = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))
    client.create_collection("diverting-members", embedding_function=_RejectingLocalEmbedding(), metadata={"hnsw:space": "cosine"})
    sink = inject_write_failure(ChromaSink({**_chroma_config(), "persist_directory": path}))
    sink.on_start(make_context())
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
        sink_id = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="chroma_sink")
        request = _build_request(
            factory,
            run.run_id,
            source_id,
            sink_id,
            [
                {"doc_id": "bad", "text": "REJECT"},
                {"doc_id": "good", "text": "one"},
            ],
        )
        result = SinkEffectCoordinator(factory=factory, worker_id="worker-a", coordination_token=leader_token_for(db, run.run_id)).execute(
            request, sink
        )
        assert result.effect.state is SinkEffectState.FINALIZED
        assert client.get_collection("diverting-members").get()["ids"] == ["good"]
        assert [diversion.row_index for diversion in sink._get_diversions()] == [0]
    finally:
        db.close()
        sink.close()
        client.close()

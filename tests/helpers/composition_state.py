"""Composition-state test data that a real writer and reader both accept.

Every stored composition state carries its pipeline metadata
(``require_stored_state_metadata``), so ``CompositionStateData`` requires it.
Fixtures that do not care which metadata use the empty graph's default, as
the production serialiser writes it.
"""

from typing import Any

from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.state_envelope import StateColumnEnvelope, envelope_state_column


def default_pipeline_metadata() -> dict[str, Any]:
    """The default ``PipelineMetadata`` exactly as a stored state carries it."""
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).to_dict()["metadata"]


def composition_state_data(**fields: Any) -> CompositionStateData:
    """``CompositionStateData`` with the default pipeline metadata unless one is given."""
    return CompositionStateData(**{"metadata_": default_pipeline_metadata(), **fields})


def stored_default_pipeline_metadata() -> StateColumnEnvelope | None:
    """The default metadata column value as a raw ``composition_states`` insert stores it.

    The column is NOT NULL, so raw-SQL fixtures must supply it in the writer's
    envelope form.
    """
    return envelope_state_column(default_pipeline_metadata())


def stored_composer_meta(
    *,
    session_id: Any,
    state_id: Any,
    version: int,
    sources: Any = None,
    nodes: Any = None,
    edges: Any = None,
    outputs: Any = None,
    metadata: Any = None,
    extra: dict[str, Any] | None = None,
) -> StateColumnEnvelope | None:
    """The ``composer_meta`` column a real insert stores, for raw ``composition_states`` fixtures.

    Every writer stamps the version-bound content publication, the column is
    NOT NULL, and readers refuse a stored state without it. Pass the same
    authored column values the fixture inserts (unenveloped), so the storage
    hash matches what a reader recomputes from the row.
    """
    from uuid import UUID

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.sessions.content_publication import prepare_content_publication

    data = CompositionStateData(
        sources=sources,
        nodes=nodes,
        edges=edges,
        outputs=outputs,
        metadata_=metadata if metadata is not None else default_pipeline_metadata(),
        composer_meta=extra,
    )
    stamped = prepare_content_publication(data, session_id=UUID(str(session_id)), state_id=UUID(str(state_id)), state_version=version)
    return envelope_state_column(deep_thaw(stamped.composer_meta))

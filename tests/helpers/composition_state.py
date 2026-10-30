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

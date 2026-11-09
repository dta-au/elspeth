"""Owned persistence values for terminal graph recovery.

Recovery qualification reads graph columns only. Historical validation and
completion metadata never transfer execution permission to a restored draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.graph_recovery import GraphRecoveryContext, GraphRecoveryIntent, qualify_editable_checkpoint
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from elspeth.web.sessions.state_envelope import unwrap_state_column

if TYPE_CHECKING:
    from elspeth.web.execution.completion_gates import CompletionGateFacts


class GraphRecoveryTargetUnavailable(ValueError):
    """An explicit recovery target cannot be reconstructed safely."""


def recovery_identity(value: object) -> UUID:
    """Fail categorically on malformed durable identity, without echoing it."""
    if type(value) is not str:
        raise AuditIntegrityError("Recovery identity is not canonical")
    try:
        identity = UUID(value)
    except ValueError:
        raise AuditIntegrityError("Recovery identity is not canonical") from None
    if str(identity) != value:
        raise AuditIntegrityError("Recovery identity is not canonical")
    return identity


@dataclass(frozen=True, slots=True)
class CompositionStateHead:
    state_id: UUID | None
    version: int


@dataclass(frozen=True, slots=True)
class CompositionGraphRead:
    head: CompositionStateHead
    record: CompositionStateRecord | None
    hydration_error: str | None = None
    effective_completion_gates: CompletionGateFacts | None = None


@dataclass(frozen=True, slots=True)
class GraphRecoveryAdmission:
    context: GraphRecoveryContext
    record: CompositionStateRecord | None
    effective_completion_gates: CompletionGateFacts | None = None


@dataclass(frozen=True, slots=True)
class GraphRecoverySettlement:
    """Frozen selection used unchanged by the exact terminal retry."""

    session_id: UUID
    operation_id: str
    intent: GraphRecoveryIntent
    expected_state_id: UUID | None
    expected_version: int
    target_state_id: UUID | None
    state_id: UUID | None
    data: CompositionStateData | None
    park_id: UUID | None
    empty_fallback: bool
    failure_reason: str | None = None
    receipt_id: UUID | None = None
    malformed_head: bool = False


def empty_recovery_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def checkpoint_data(state: CompositionState) -> CompositionStateData:
    qualification = qualify_editable_checkpoint(state)
    if not qualification.eligible:
        raise GraphRecoveryTargetUnavailable("Recovery target is not an editable checkpoint")
    raw = state.to_dict()
    return CompositionStateData(
        sources=raw["sources"],
        nodes=raw["nodes"],
        edges=raw["edges"],
        outputs=raw["outputs"],
        metadata_=raw["metadata"],
        is_valid=False,
        validation_errors=None,
        composer_meta=None,
    )


def read_editable_checkpoint(conn: Connection, session_id: UUID, state_id: UUID) -> CompositionState:
    """Read only graph content, excluding damaged gates and validation results."""
    table = composition_states_table
    row = conn.execute(
        select(
            table.c.id,
            table.c.session_id,
            table.c.version,
            table.c.source,
            table.c.sources,
            table.c.nodes,
            table.c.edges,
            table.c.outputs,
            table.c.metadata_,
            table.c.created_at,
        ).where(table.c.id == str(state_id), table.c.session_id == str(session_id))
    ).one_or_none()
    if row is None:
        raise GraphRecoveryTargetUnavailable("Recovery target does not belong to this session")
    record = CompositionStateRecord(
        id=state_id,
        session_id=session_id,
        version=row.version,
        source=unwrap_state_column(row.source),
        sources=unwrap_state_column(row.sources),
        nodes=unwrap_state_column(row.nodes),
        edges=unwrap_state_column(row.edges),
        outputs=unwrap_state_column(row.outputs),
        metadata_=unwrap_state_column(row.metadata_),
        is_valid=False,
        validation_errors=None,
        created_at=row.created_at,
        derived_from_state_id=None,
        composer_meta=None,
    )
    state = state_from_record(record)
    original = {
        "version": record.version,
        "sources": deep_thaw(record.sources) if record.sources is not None else {},
        "nodes": deep_thaw(record.nodes) if record.nodes is not None else [],
        "edges": deep_thaw(record.edges) if record.edges is not None else [],
        "outputs": deep_thaw(record.outputs) if record.outputs is not None else [],
        "metadata": deep_thaw(record.metadata_),
    }
    if record.source is not None:
        if record.sources is not None:
            raise GraphRecoveryTargetUnavailable("Recovery target has ambiguous source representation")
        original["sources"] = {"source": deep_thaw(record.source)}
    if canonical_json(original) != canonical_json(state.to_dict()):
        raise GraphRecoveryTargetUnavailable("Recovery target cannot be reconstructed without changing content")
    if not qualify_editable_checkpoint(state).eligible:
        raise GraphRecoveryTargetUnavailable("Recovery target is not an editable checkpoint")
    return state

"""Sessions-DB round-trip for persisted composer completion-gate facts.

The unit tests in ``tests/unit/web/execution/test_completion_gates.py``
exercise the envelope in memory; these tests pin the two seams only a real
persistence cycle can break:

* JSON round-trip fidelity — the envelope written through
  ``save_composition_state`` parses back into identical gate facts; and
* fingerprint stability across reconstruction — ``state_from_record`` must
  rebuild a state whose canonical graph fingerprint matches the one the
  writer computed from the live ``CompositionState``, or every persisted
  verdict would be misreported as stale after reload.

Spec: docs-archive/specs/2026-08-01-composer-completion-gate-persistence-design.md.
"""

from __future__ import annotations

import pytest
import structlog
from sqlalchemy.pool import StaticPool

from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.execution.completion_gates import (
    ADVISOR_SIGNOFF_PENDING_DETAIL,
    COMPLETION_GATES_META_KEY,
    AdvisorSignoffGateFact,
    CompletionGateFacts,
    completion_gate_fingerprint,
    merge_completion_gates,
    parse_completion_gates,
)
from elspeth.web.execution.schemas import (
    ADVISOR_SIGNOFF_BLOCKED_CODE,
    ValidationReadiness,
    ValidationResult,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

_BLOCKED_DETAIL = "The advisor sign-off could not be obtained; the pipeline cannot complete."


@pytest.fixture
def service():
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )


def _make_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            options=deep_freeze({"path": "/x.csv"}),
            on_success="out",
            on_validation_failure="quarantine",
        ),
        nodes=(),
        edges=(EdgeSpec(id="e1", from_node="source", to_node="out", edge_type="on_success", label=None),),
        outputs=(OutputSpec(name="out", plugin="json", options=deep_freeze({}), on_write_failure="discard"),),
        metadata=PipelineMetadata(name="roundtrip"),
        version=3,
    )


def _green_result() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )


async def _save_with_gate(service: SessionServiceImpl, state: CompositionState, for_graph: str):
    session = await service.create_session("alice", "Gate roundtrip", "local")
    state_d = state.to_dict()
    data = CompositionStateData(
        sources=state_d["sources"],
        nodes=state_d["nodes"],
        edges=state_d["edges"],
        outputs=state_d["outputs"],
        metadata_=state_d["metadata"],
        is_valid=True,
        validation_errors=None,
        composer_meta={
            "repair_turns_used": 1,
            COMPLETION_GATES_META_KEY: {
                "advisor_signoff": {
                    "status": "blocked",
                    "detail": _BLOCKED_DETAIL,
                    "for_graph": for_graph,
                }
            },
        },
    )
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        await service.save_composition_state(
            session_id=session.id,
            state=data,
            provenance="post_compose",
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)
    record = await service.get_current_state(session.id)
    assert record is not None
    return record


@pytest.mark.asyncio
async def test_blocked_gate_survives_db_roundtrip_and_blocks_recompute(service) -> None:
    """Persist → read → parse → merge reproduces the withheld sign-off exactly."""
    state = _make_state()
    record = await _save_with_gate(service, state, completion_gate_fingerprint(state))

    facts = parse_completion_gates(record.composer_meta)
    assert facts == CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(detail=_BLOCKED_DETAIL, for_graph=completion_gate_fingerprint(state))
    )

    # Fingerprint stability across reconstruction: the state rebuilt from the
    # persisted row must fingerprint identically to the live state the writer
    # hashed, else every reload would misreport the verdict as stale.
    rebuilt = state_from_record(record)
    assert completion_gate_fingerprint(rebuilt) == completion_gate_fingerprint(state)

    merged = merge_completion_gates(_green_result(), facts, rebuilt)
    assert merged.readiness.completion_ready is False
    (blocker,) = merged.readiness.blockers
    assert blocker.code == ADVISOR_SIGNOFF_BLOCKED_CODE
    assert blocker.detail == _BLOCKED_DETAIL


@pytest.mark.asyncio
async def test_carried_forward_gate_reports_pending_on_changed_graph(service) -> None:
    """A fact riding a graph the advisor never reviewed reclassifies to pending."""
    reviewed = _make_state()
    record = await _save_with_gate(service, reviewed, completion_gate_fingerprint(reviewed))

    # Simulate merge_composer_meta_updates carry-forward: the same envelope
    # rides a NEW version whose graph differs (extra output).
    changed = CompositionState(
        source=reviewed.sources["source"],
        nodes=reviewed.nodes,
        edges=reviewed.edges,
        outputs=(
            *reviewed.outputs,
            OutputSpec(name="extra", plugin="json", options=deep_freeze({}), on_write_failure="discard"),
        ),
        metadata=reviewed.metadata,
        version=reviewed.version + 1,
    )

    facts = parse_completion_gates(record.composer_meta)
    merged = merge_completion_gates(_green_result(), facts, changed)

    assert merged.readiness.completion_ready is False
    (blocker,) = merged.readiness.blockers
    assert blocker.code == ADVISOR_SIGNOFF_BLOCKED_CODE
    assert blocker.detail == ADVISOR_SIGNOFF_PENDING_DETAIL


@pytest.mark.asyncio
async def test_durable_completion_gates_returns_prior_envelope_verbatim(service) -> None:
    """The recovery-save carry-forward helper reads the latest persisted row
    and re-serializes its gate fact byte-identically; a session with no
    persisted state yields the explicit empty envelope."""
    from elspeth.web.sessions.routes._helpers import _durable_completion_gates

    state = _make_state()
    fingerprint = completion_gate_fingerprint(state)
    record = await _save_with_gate(service, state, fingerprint)

    carried = await _durable_completion_gates(service, record.session_id)
    assert carried == {
        "advisor_signoff": {
            "status": "blocked",
            "detail": _BLOCKED_DETAIL,
            "for_graph": fingerprint,
        }
    }

    fresh = await service.create_session("alice", "No gate yet", "local")
    assert await _durable_completion_gates(service, fresh.id) == {}

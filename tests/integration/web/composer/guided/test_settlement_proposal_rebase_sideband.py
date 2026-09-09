"""The rebase sideband at the settlement seam: required, honoured, and bounded.

These drive ``settle_guided_state_operation`` directly, over a REAL staged
Step-3 proposal, so each arm of the carrying transition can be exercised on
its own instead of through whichever route happens to reach it:

* a command that carries a pending proposal onto a new checkpoint with NO
  sideband must be refused, naming the settlement and both ids;
* the same command WITH the sideband must settle, move the proposal's anchor
  onto the checkpoint it wrote, and append one immutable ``proposal.rebased``
  event recording the hop;
* a command whose composition content actually CHANGED must be refused even
  with a well-formed sideband;
* a PERSISTED rebase event whose ``reason_code`` is outside the closed
  vocabulary must be refused on the way back in, not silently walked.

That last one is the adversarial case, and it cannot be written through the
routes: the guided graph stays empty until commit, so every version through
Steps 1-3 shares one composition content hash and both sides of the admission
are degenerate. Only a hand-built command can make them differ — and without
it the sideband would be a hole through which any settlement could re-anchor
a reviewed proposal onto an arbitrary later composition.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.guided.planning import guided_private_reviewed_facts
from elspeth.web.composer.pipeline_proposal import PresentBase, composition_content_hash
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import composition_proposals_table, proposal_events_table
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    CompositionStateRecord,
    GuidedOperationClaimed,
    GuidedPendingProposalRebase,
    GuidedResponseDescriptor,
    GuidedStateOperationCommand,
)
from tests.integration.web.composer.guided.test_wrong_stage_intent import (
    _stage_schema8_topology_intent_proposal,
)
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


def _head(client: TestClient, session_id: str) -> CompositionStateRecord:
    """Return the session's current composition-state row."""

    record = asyncio.run(client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    return record


def _carrying_command(
    client: TestClient,
    session_id: str,
    *,
    with_sideband: bool,
    extra_node: dict[str, Any] | None = None,
) -> GuidedStateOperationCommand:
    """Build one settlement that carries the staged proposal onto a new row.

    The candidate checkpoint is the head verbatim — the guided session, its
    ``active_proposal`` reference included, rides across unchanged, which is
    exactly what an advisory chat or a declined revision does. ``extra_node``
    is the adversarial lever: it makes the candidate's composition content
    genuinely differ from the anchor's.
    """

    service = client.app.state.session_service
    head = _head(client, session_id)
    state = state_from_record(head)
    guided = state.guided_session
    assert guided is not None
    assert guided.active_proposal is not None, "the staged Step-3 proposal must still be pending"
    state_dict = state.to_dict()
    claimed = asyncio.run(
        service.reserve_guided_operation(
            session_id=UUID(session_id),
            operation_id=str(uuid4()),
            kind="guided_respond",
            request_hash="a" * 64,
            actor="worker",
            lease_seconds=60,
        )
    )
    assert type(claimed) is GuidedOperationClaimed
    sideband = (
        GuidedPendingProposalRebase(
            proposal_id=guided.active_proposal.proposal_id,
            draft_hash=guided.active_proposal.draft_hash,
            reviewed_facts=guided_private_reviewed_facts(guided),
            from_state_id=head.id,
            composition_content_hash=composition_content_hash(state),
            reason="advisory_chat",
        )
        if with_sideband
        else None
    )
    return GuidedStateOperationCommand(
        fence=claimed.fence,
        expected_current_state_id=head.id,
        expected_current_state_version=head.version,
        expected_current_content_hash=composition_content_hash(state),
        state_id=uuid4(),
        state=CompositionStateData(
            sources=state_dict["sources"],
            nodes=[*state_dict["nodes"], extra_node] if extra_node is not None else state_dict["nodes"],
            edges=state_dict["edges"],
            outputs=state_dict["outputs"],
            metadata_=state_dict["metadata"],
            is_valid=head.is_valid,
            validation_errors=head.validation_errors,
            composer_meta={"guided_session": guided.to_dict()},
        ),
        provenance="convergence_persist",
        actor="composer_route",
        response=GuidedResponseDescriptor(kind="guided_respond", next_turn=None, assistant_turn_seq=None),
        rebased_pending_proposal=sideband,
    )


def _proposal_anchor(client: TestClient, session_id: str) -> PresentBase:
    """Return the live derived anchor of the session's pending proposal."""

    head = _head(client, session_id)
    guided = state_from_record(head).guided_session
    assert guided is not None
    assert guided.active_proposal is not None
    authority = asyncio.run(
        client.app.state.session_service.get_authoritative_pipeline_proposal(
            session_id=UUID(session_id),
            proposal_id=guided.active_proposal.proposal_id,
            reviewed_facts=guided_private_reviewed_facts(guided),
        )
    )
    anchor = authority.current_base
    assert type(anchor) is PresentBase
    return anchor


def test_carrying_a_pending_proposal_without_the_sideband_is_refused(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard the carrying arm never had, stated at the seam.

    Before elspeth-ed67eb9d0d this settled a clean 200 and left the session
    permanently unreadable. It must now fail loudly at write time, and the
    message must name the settlement and both disagreeing ids — the original
    failure was undiagnosable from its text alone.
    """

    client = composer_test_client
    session_id, _retained, _staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    head = _head(client, session_id)
    command = _carrying_command(client, session_id, with_sideband=False)

    with pytest.raises(AuditIntegrityError) as raised:
        asyncio.run(client.app.state.session_service.settle_guided_state_operation(command))

    message = str(raised.value)
    assert "must rebase its anchor onto the checkpoint it writes" in message
    assert str(command.state_id) in message, "the refusal must name the checkpoint being written"
    assert str(head.id) in message, "the refusal must name the anchor the proposal still holds"
    assert "guided guided_respond operation" in message, "the refusal must name the settlement that wrote it"


def test_the_rebase_sideband_moves_the_anchor_onto_the_settled_checkpoint(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the sideband the settlement lands, and the anchor follows it.

    The move is recorded three ways that must agree: the derived anchor, the
    lifecycle-managed ``composition_proposals.base_state_id`` column, and one
    appended ``proposal.rebased`` event naming both ends of the hop. The
    proposal stays PENDING throughout — a rebase is not a terminal event.
    """

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    creation_head = _head(client, session_id)
    command = _carrying_command(client, session_id, with_sideband=True)

    settlement = asyncio.run(client.app.state.session_service.settle_guided_state_operation(command))

    assert settlement.primary_state.id == command.state_id
    assert _proposal_anchor(client, session_id).state_id == command.state_id
    proposal_id = staged["next_turn"]["payload"]["proposal_id"]
    with client.app.state.session_engine.connect() as conn:
        row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).one()
        events = conn.execute(
            select(proposal_events_table)
            .where(proposal_events_table.c.proposal_id == proposal_id)
            .where(proposal_events_table.c.event_type == "proposal.rebased")
        ).all()
    assert row.status == "pending", "a rebase must not terminalize the proposal"
    assert row.base_state_id == str(command.state_id)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["schema"] == "pipeline_proposal_rebased.v1"
    assert payload["from_state_id"] == str(creation_head.id)
    assert payload["to_state_id"] == str(command.state_id)
    assert payload["draft_hash"] == staged["next_turn"]["payload"]["draft_hash"]
    assert payload["reason_code"] == "advisory_chat", "the event must record which gesture moved the anchor"


def test_the_rebase_is_refused_when_the_composition_content_changed(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing admission: a rebase can never launder a moved graph.

    Everything else about this command is well-formed — the sideband names
    the right proposal, the right draft, the right anchor and the right
    content hash. Only the checkpoint being written carries a different
    composition. Without this check the sideband would let any settlement
    re-anchor a reviewed proposal onto a graph nobody reviewed.
    """

    client = composer_test_client
    session_id, _retained, _staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    head = _head(client, session_id)
    command = _carrying_command(
        client,
        session_id,
        with_sideband=True,
        extra_node={
            "id": "smuggled_transform",
            "type": "transform",
            "plugin": "python_expression",
            "options": {"expressions": {"smuggled": "1"}},
        },
    )

    with pytest.raises(AuditIntegrityError) as raised:
        asyncio.run(client.app.state.session_service.settle_guided_state_operation(command))

    assert "onto a checkpoint whose composition content changed" in str(raised.value)
    assert _proposal_anchor(client, session_id).state_id == head.id, "the refused rebase must leave the anchor where it was"


def test_a_persisted_rebase_event_with_an_unknown_reason_is_refused_on_restore(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor is DERIVED from stored events, so the parser is a trust boundary.

    ``_effective_pipeline_proposal_base`` walks the appended rebase chain to
    answer "where is this proposal anchored"; the mutable
    ``composition_proposals.base_state_id`` column is then checked against
    that derivation rather than trusted. So a corrupt stored payload must
    fail the restore closed, not be walked as if it were sound. This drives
    the ``reason_code`` arm specifically — the field is only as good as the
    validation on the way back in, and the write-side validation cannot
    speak for a row that was written by something else.
    """

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    command = _carrying_command(client, session_id, with_sideband=True)
    asyncio.run(client.app.state.session_service.settle_guided_state_operation(command))
    proposal_id = staged["next_turn"]["payload"]["proposal_id"]
    guided = state_from_record(_head(client, session_id)).guided_session
    assert guided is not None and guided.active_proposal is not None

    with client.app.state.session_engine.begin() as conn:
        row = conn.execute(
            select(proposal_events_table)
            .where(proposal_events_table.c.proposal_id == proposal_id)
            .where(proposal_events_table.c.event_type == "proposal.rebased")
        ).one()
        corrupted = {**deep_thaw(row.payload), "reason_code": "not_a_declared_reason"}
        conn.execute(update(proposal_events_table).where(proposal_events_table.c.id == row.id).values(payload=corrupted))

    with pytest.raises(AuditIntegrityError) as raised:
        asyncio.run(
            client.app.state.session_service.get_authoritative_pipeline_proposal(
                session_id=UUID(session_id),
                proposal_id=guided.active_proposal.proposal_id,
                reviewed_facts=guided_private_reviewed_facts(guided),
            )
        )

    assert "rebase reason is outside the closed vocabulary" in str(raised.value)

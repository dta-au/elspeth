"""Every guided settlement carrying a pending proposal must re-anchor it.

``_require_guided_pending_proposal_transition`` (service.py) had two arms. The
CLEARING arm — the candidate checkpoint drops ``active_proposal`` — performs
five field comparisons, restores the row authority, and re-derives the base
content binding. The CARRYING arm — prior and candidate name the SAME proposal
— returned ``None`` with no checks at all.

That is the defect class behind elspeth-ed67eb9d0d. A guided settlement always
mints a fresh ``composition_states`` row (in guided mode that row is a SESSION
checkpoint, not a graph-change record: ``chat_history`` lives inside
``composer_meta``), so every carrying settlement advanced the head while the
proposal's anchor kept naming the PREVIOUS checkpoint. Nothing on the write
path noticed, and the composition-content hashes could not notice either — the
graph stays empty until commit, so every version through Steps 1-3 shares one
content hash.

Two guards downstream do notice, and each one is a different amputation:

* ``GET /guided`` at Step 3 (``guided.py``) compares the anchor's state id
  against the head row id and raises "guided proposal base differs from
  current checkpoint". The session is then permanently unreadable through the
  guided route, and the frontend's tolerant read silently reopens it in
  freeform — so the author sees the wizard, the stepper and the whole guided
  transcript disappear rather than an error.
* ``back_edit_guided_pipeline_proposal`` (service.py) admits an anchor one hop
  stale — ``current_record.derived_from_state_id``, and only for ``origin ==
  "wire_review"`` — which is exactly what the Step-3 proposal acceptance
  spends. Any FURTHER carrying settlement at Step 4 puts the anchor out of
  that set, and "edit this component" on the wire-review card starts failing
  closed.

These tests state each invariant positively, and are RED until the settlement
re-anchors the carried proposal.

SCOPE, so this file is not mistaken for a completeness proof. These tests
cover the settlements that go through ``settle_guided_state_operation`` plus
``decline_guided_full_pipeline_proposal``. They do NOT cover every writer
that inserts a ``composition_states`` row carrying a live ``active_proposal``
forward. Two such writers are measured and still open:
``POST /api/sessions/{id}/messages`` and ``POST /api/sessions/{id}/compose``
re-inject ``guided_session`` and call ``save_composition_state``, stranding
the anchor with no transition guard at all (elspeth-f561d651c8, pre-existing
and unaffected by this fix).

The third, ``stage_guided_full_pipeline_proposal``, is covered here but by a
REFUSAL rather than a rebase: staging mints a second pending proposal
against the checkpoint it writes, so moving the first proposal's anchor onto
that checkpoint would not say which of the two the walk owns. It now fails
closed with a coded ``integrity_error`` (pinned by
``test_a_guided_full_staging_refuses_rather_than_bricking_a_reviewing_walk``)
and the durable remedy — refuse, or supersede the predecessor — stays with
elspeth-da0e3db919.

NOTE on WHICH binding moves. ``GuidedProposalRef.base`` and
``PipelineProposal.base`` do NOT move and cannot: ``base`` is hashed into
``draft_hash`` (``pipeline_draft_hash``), so it is the reviewed artifact's
immutable identity. What moves is the proposal's ANCHOR — the
lifecycle-managed ``composition_proposals.base_state_id``, derived as the
creation base plus every appended ``proposal.rebased`` event and surfaced as
``AuthoritativePipelineProposal.current_base``. Both guards above ask the
currency question of the anchor.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_llm_audit import ComposerChatTurnStatus
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.guided.planning import guided_private_reviewed_facts
from elspeth.web.composer.pipeline_planner import GuidedPlannerDecline
from elspeth.web.composer.pipeline_proposal import PresentBase
from elspeth.web.sessions._guided_step_chat import GuidedStepChatOnlyResult, StepChatResult
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import (
    GUIDED_PROPOSAL_REBASE_REASONS,
    CompositionStateRecord,
    GuidedOperationClaimed,
)
from elspeth.web.sessions.routes.composer import guided as guided_route
from elspeth.web.sessions.routes.composer.guided_chat_atomic import GuidedChatProviderOutcome
from tests.integration.web.composer.guided.test_respond import _get_guided, _review_wiring
from tests.integration.web.composer.guided.test_wrong_stage_intent import (
    _post as _post_guided_chat,
)
from tests.integration.web.composer.guided.test_wrong_stage_intent import (
    _stage_schema8_topology_intent_proposal,
)
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


async def _advisory_provider(**_kwargs: object) -> GuidedChatProviderOutcome:
    """Answer one guided chat with ordinary advice and no state transition."""

    return GuidedStepChatOnlyResult(
        chat=StepChatResult(
            assistant_message="Those choices look reasonable for that goal.",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=1,
            error_class=None,
        ),
    )


def _head_record(client: TestClient, session_id: str) -> CompositionStateRecord:
    """Return the session's current composition-state row."""

    record = asyncio.run(client.app.state.session_service.get_current_state(UUID(session_id)))
    assert record is not None
    return record


def _carried_proposal_anchor(client: TestClient, session_id: str) -> PresentBase:
    """Return the live anchor of the pending proposal the head still carries.

    The anchor is ``AuthoritativePipelineProposal.current_base`` — the
    creation base moved forward by every appended ``proposal.rebased`` event,
    and the value ``composition_proposals.base_state_id`` is kept equal to.
    It is deliberately NOT ``GuidedProposalRef.base``: that base is hashed
    into ``draft_hash`` (``pipeline_draft_hash``), so it is the reviewed
    artifact's immutable identity and cannot move. Reading the anchor here
    is reading exactly what the guided read and the wire-review back-edit
    read when they ask whether the proposal is still current.
    """

    head = _head_record(client, session_id)
    guided = state_from_record(head).guided_session
    assert guided is not None
    active = guided.active_proposal
    assert active is not None, "the checkpoint must still carry its pending proposal"
    authority = asyncio.run(
        client.app.state.session_service.get_authoritative_pipeline_proposal(
            session_id=UUID(session_id),
            proposal_id=active.proposal_id,
            reviewed_facts=guided_private_reviewed_facts(guided),
        )
    )
    anchor = authority.current_base
    assert type(anchor) is PresentBase, "a staged guided proposal always anchors to a persisted checkpoint"
    return anchor


def _assert_carried_anchor_names_head(client: TestClient, session_id: str, *, gesture: str) -> None:
    """Assert the head checkpoint's carried proposal is anchored to the head itself."""

    head = _head_record(client, session_id)
    anchor = _carried_proposal_anchor(client, session_id)
    assert anchor.state_id == head.id, (
        f"{gesture} settled composition state {head.id} (version {head.version}) while the carried "
        f"pending proposal is still anchored to {anchor.state_id}. The carrying arm of "
        "_require_guided_pending_proposal_transition performed no checks, so the stale anchor was "
        "written silently."
    )


def _advisory_chat(
    client: TestClient,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turn_token: str,
    message: str,
) -> dict:
    """Answer the current turn with one ordinary advisory chat."""

    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider)
    chatted = _post_guided_chat(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=turn_token,
        message=message,
    )
    assert chatted.status_code == 200, chatted.json()
    return chatted.json()


def _decline_a_prose_revision(
    client: TestClient,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turn: dict,
) -> dict:
    """Ask for a revision the planner declines, at Step 3."""

    async def _decline(**_kwargs: object) -> GuidedPlannerDecline:
        return GuidedPlannerDecline(decline_text="I could not fit that instruction to the reviewed components.")

    monkeypatch.setattr(client.app.state.composer_service, "plan_guided_pipeline", _decline)
    declined = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": turn["turn_token"],
            "proposal_id": turn["payload"]["proposal_id"],
            "draft_hash": turn["payload"]["draft_hash"],
            "edited_values": {"revision_instruction": "Add a transform that cannot exist."},
        },
    )
    assert declined.status_code == 200, declined.json()
    return declined.json()


def _decline_a_guided_full_plan(
    client: TestClient,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Ask the guided-full escape hatch for a whole pipeline; have it decline."""

    async def _decline(**_kwargs: object) -> GuidedPlannerDecline:
        return GuidedPlannerDecline(decline_text="That is not something I can plan in one shot.")

    monkeypatch.setattr(client.app.state.composer_service, "plan_guided_full_pipeline", _decline)
    planned = client.post(
        f"/api/sessions/{session_id}/guided/plan",
        json={"operation_id": str(uuid4()), "intent": "Just plan the whole thing instead."},
    )
    assert planned.status_code == 200, planned.json()
    assert planned.json()["outcome"] == "declined"
    return planned.json()


def _sole_rebase_reason(client: TestClient, session_id: str, *, proposal_id: str) -> object:
    """Return the ``reason_code`` of the one anchor move appended for ``proposal_id``."""

    events = [
        event
        for event in asyncio.run(client.app.state.session_service.list_proposal_events(UUID(session_id)))
        if str(event.proposal_id) == proposal_id and event.event_type == "proposal.rebased"
    ]
    assert len(events) == 1, f"expected exactly one anchor move, got {len(events)}"
    payload = deep_thaw(events[0].payload)
    assert type(payload) is dict
    assert payload["schema"] == "pipeline_proposal_rebased.v1"
    return payload["reason_code"]


def _back_edit_first_reviewed_source(client: TestClient, session_id: str, wire_turn: dict):
    """Press "edit this component" on the wire-review card's first source."""

    payload = wire_turn["payload"]
    return client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": wire_turn["turn_token"],
            "proposal_id": payload["proposal_id"],
            "draft_hash": payload["draft_hash"],
            "edit_target": {"kind": "source", "stable_id": payload["sources"][0]["stable_id"]},
            "correction_feedback": "Change the selected source settings.",
        },
    )


def test_step_3_advisory_chat_keeps_the_guided_read_working(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking a question at Step 3 must not make the session unreadable.

    ``POST /guided/chat`` is admitted at Step 3 whenever a deferred intent is
    pending (``guided_chat_atomic`` rejects Steps 3/4 only when
    ``not guided.deferred_intents``), and nothing on that route used to move
    the pending proposal's anchor. Prior and candidate named the same ref,
    the carrying arm waved the settlement through, and the fresh checkpoint
    left the anchor pointing one version back — which is precisely what
    ``GET /guided`` refuses to serve, from then on, forever.

    The session must still read, and the proposal must still be presented:
    ``active_proposal`` is private and never reaches the wire, so the public
    evidence that the review card survived is the re-presented
    ``propose_pipeline`` turn.
    """

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    _assert_carried_anchor_names_head(client, session_id, gesture="staging the Step-3 proposal")
    _advisory_chat(
        client,
        session_id,
        monkeypatch,
        turn_token=staged["next_turn"]["turn_token"],
        message="Which of these transforms actually changes the row count?",
    )

    read_back = client.get(f"/api/sessions/{session_id}/guided")

    assert read_back.status_code == 200, read_back.json()
    body = read_back.json()
    assert body["guided_session"]["step"] == "step_3_transforms"
    assert body["next_turn"]["type"] == "propose_pipeline"
    assert body["next_turn"]["payload"]["proposal_id"] == staged["next_turn"]["payload"]["proposal_id"]
    assert body["next_turn"]["payload"]["draft_hash"] == staged["next_turn"]["payload"]["draft_hash"]


def test_step_3_advisory_chat_keeps_the_carried_proposal_anchored_to_the_head(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanism behind the unreadable session: a stranded anchor."""

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    _advisory_chat(
        client,
        session_id,
        monkeypatch,
        turn_token=staged["next_turn"]["turn_token"],
        message="Does the dedupe step run before or after the filter?",
    )

    _assert_carried_anchor_names_head(client, session_id, gesture="a Step-3 advisory chat")


def test_wire_review_component_back_edit_works_on_the_freshly_accepted_proposal(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the one-hop tolerance covers the Step-3 acceptance itself.

    Accepting a Step-3 proposal into wire review carries the proposal forward
    under a fresh ``state_id``, so its base is already one hop behind the head
    the moment Step 4 opens. ``back_edit_guided_pipeline_proposal`` absorbs
    exactly that hop through ``current_record.derived_from_state_id`` for
    ``origin == "wire_review"``. This test pins that protection, which is
    load-bearing for the sibling test below: without it the edit affordance
    would be dead from the first moment of Step 4 rather than from the second
    carrying settlement.
    """

    client = composer_test_client
    session_id, _retained, _staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    reviewed = _review_wiring(client, session_id)
    assert reviewed["guided_session"]["step"] == "step_4_wire"

    response = _back_edit_first_reviewed_source(client, session_id, reviewed["next_turn"])

    assert response.status_code == 200, response.json()
    assert response.json()["guided_session"]["step"] == "step_1_source"


def test_wire_review_component_back_edit_survives_a_further_step_4_settlement(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second carrying settlement at Step 4 must not kill the edit affordance.

    The Step-3 acceptance spends the whole one-hop ``derived_from`` tolerance.
    Every further Step-4 settlement that carries the proposal — an advisory
    chat like this one, or a declined wiring correction, which needs no
    deferred intent at all — pushes the binding a second hop back, out of
    ``allowed_base_state_ids`` entirely. "Edit this component" on the
    wire-review card then fails closed with an integrity error, on a proposal
    nothing has changed.
    """

    client = composer_test_client
    session_id, _retained, _staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    reviewed = _review_wiring(client, session_id)
    assert reviewed["guided_session"]["step"] == "step_4_wire"
    chatted = _advisory_chat(
        client,
        session_id,
        monkeypatch,
        turn_token=reviewed["next_turn"]["turn_token"],
        message="Is this wiring right for what I asked for?",
    )

    response = _back_edit_first_reviewed_source(client, session_id, chatted["next_turn"])

    assert response.status_code == 200, response.json()
    assert response.json()["guided_session"]["step"] == "step_1_source"


def test_a_guided_full_decline_keeps_the_guided_read_working(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same amputation, through a settlement outside ``settle_guided_state_operation``.

    ``POST /guided/plan`` is the guided-full escape hatch, and it is admitted
    on a session already mid-walk. On a decline it writes a checkpoint over
    the observed head with ``composer_meta`` — the guided session and its
    ``active_proposal`` included — carried forward VERBATIM, through
    ``decline_guided_full_pipeline_proposal`` rather than through the guided
    RESPOND/CHAT settlement. So the anchor was stranded here for exactly the
    same reason, and the clean 200 was followed by a permanently unreadable
    session.

    That this route survived the first fix is a method finding, not a coding
    slip: the defect class was closed by censusing
    ``GuidedStateOperationCommand`` construction sites — a call-shape census —
    rather than every writer that carries a live ``active_proposal`` onto a
    new checkpoint row.
    """

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    assert _get_guided(client, session_id)["next_turn"]["type"] == "propose_pipeline"

    _decline_a_guided_full_plan(client, session_id, monkeypatch)

    read_back = _get_guided(client, session_id)
    assert read_back["guided_session"]["step"] == "step_3_transforms"
    assert read_back["next_turn"]["type"] == "propose_pipeline"
    assert read_back["next_turn"]["payload"]["proposal_id"] == staged["next_turn"]["payload"]["proposal_id"]
    _assert_carried_anchor_names_head(client, session_id, gesture="a guided-full planner decline")


def test_every_carrying_settlement_names_its_own_gesture_in_the_rebase_event(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proposal's own trail must say WHICH settlement moved its anchor.

    Four different gestures carry a pending proposal across a new checkpoint,
    and their ``proposal.rebased`` payloads are otherwise identical modulo
    ids. The settlement's own identity cannot separate them either: the
    declined revision and the wire-review advance both settle a
    ``guided_respond`` operation. Without ``reason_code`` the only way to ask
    "what moved this anchor" is to leave the proposal trail entirely and join
    ``to_state_id`` against ``guided_operations.result_state_id``.

    The closing assertion is the parity fence: every member of the closed
    vocabulary must be emitted by one of these gestures, so a fifth member
    cannot be declared without wiring — and naming — the site that emits it.
    """

    client = composer_test_client
    # All four sessions are staged BEFORE any gesture runs: each gesture
    # monkeypatches a planner or chat provider for the rest of the test, and
    # staging a Step-3 proposal goes through those same seams.
    chat_session, _r1, chat_staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    wire_session, _r2, wire_staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    revision_session, _r3, revision_staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    full_session, _r4, full_staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)

    _advisory_chat(
        client,
        chat_session,
        monkeypatch,
        turn_token=chat_staged["next_turn"]["turn_token"],
        message="Which of these transforms actually changes the row count?",
    )
    _review_wiring(client, wire_session)
    _decline_a_prose_revision(client, revision_session, monkeypatch, turn=revision_staged["next_turn"])
    _decline_a_guided_full_plan(client, full_session, monkeypatch)

    observed = {
        "advisory chat": _sole_rebase_reason(client, chat_session, proposal_id=chat_staged["next_turn"]["payload"]["proposal_id"]),
        "wire-review advance": _sole_rebase_reason(client, wire_session, proposal_id=wire_staged["next_turn"]["payload"]["proposal_id"]),
        "declined revision": _sole_rebase_reason(
            client, revision_session, proposal_id=revision_staged["next_turn"]["payload"]["proposal_id"]
        ),
        "guided-full decline": _sole_rebase_reason(client, full_session, proposal_id=full_staged["next_turn"]["payload"]["proposal_id"]),
    }

    assert observed == {
        "advisory chat": "advisory_chat",
        "wire-review advance": "wire_review_entry",
        "declined revision": "revision_declined",
        "guided-full decline": "guided_full_declined",
    }
    assert set(observed.values()) == GUIDED_PROPOSAL_REBASE_REASONS, (
        "every declared rebase reason must be emitted by one of the carrying settlements"
    )


def _seed_a_live_confirmation_admission(client: TestClient, session_id: str) -> str:
    """Leave an un-expired guided operation owning dispatch on the head's proposal.

    This is the durable residue of ``admit_guided_pipeline_confirmation``: an
    ``in_progress`` ``guided_respond`` row whose ``proposal_id`` names the
    pending proposal, holding a live lease. The admission commits that in its
    own transaction before the confirm route goes on to dispatch, so process
    death anywhere in the rest of that route leaves exactly this state behind
    until the lease runs out.
    """

    service = client.app.state.session_service
    guided = state_from_record(_head_record(client, session_id)).guided_session
    assert guided is not None
    active = guided.active_proposal
    assert active is not None, "the staged Step-3 proposal must still be pending"
    claimed = asyncio.run(
        service.reserve_guided_operation(
            session_id=UUID(session_id),
            operation_id=str(uuid4()),
            kind="guided_respond",
            request_hash="b" * 64,
            actor="worker",
            lease_seconds=300,
        )
    )
    assert type(claimed) is GuidedOperationClaimed
    asyncio.run(service.bind_guided_operation(claimed.fence, proposal_id=active.proposal_id))
    return claimed.fence.operation_id


def test_a_carrying_settlement_refuses_while_a_confirmation_admission_is_live(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor must not move while an admitted confirmation owns dispatch.

    Re-anchoring a carried proposal is a mutation of a live proposal's
    lifecycle, so it takes the same fence the terminalizing sibling
    ``_reject_guided_pending_proposal`` takes: a settlement that would move
    the anchor while a confirmation admission still holds an unexpired lease
    on that proposal refuses with ``stale_conflict`` rather than moving it
    underneath the in-flight dispatch.

    This is a NEW refusal on the ordinary guided path — before the anchor
    moved at all, a carrying settlement never touched the proposal row and so
    never met this fence. Pinning it here is the point: the refusal is
    intended, it is bounded by the lease rather than permanent, and it must
    not widen. The failure it replaces would have arrived later and named the
    wrong thing — the dispatch's own ``expected_current_state_id`` check
    against a head this settlement had already advanced.

    Nothing else may move. The fence fires inside
    ``_rebind_guided_pending_proposal``, which runs AFTER the checkpoint
    insert, so the refusal is only honest if the whole transaction rolls
    back: an orphaned checkpoint would leave the session one version ahead
    with no settlement to account for it.
    """

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)
    _seed_a_live_confirmation_admission(client, session_id)
    monkeypatch.setattr(guided_route, "_run_guided_chat_provider_attempt", _advisory_provider)

    refused = _post_guided_chat(
        client,
        session_id,
        operation_id=str(uuid4()),
        turn_token=staged["next_turn"]["turn_token"],
        message="Which of these transforms actually changes the row count?",
    )

    assert refused.status_code == 409, refused.json()
    detail = refused.json()["detail"]
    assert detail["error_type"] == "guided_operation_terminal_failure"
    assert detail["failure_code"] == "stale_conflict"
    after = _head_record(client, session_id)
    assert (after.id, after.version) == (before.id, before.version), (
        "the refused settlement must roll back the checkpoint it had already inserted"
    )
    _assert_carried_anchor_names_head(client, session_id, gesture="the refused Step-3 advisory chat")


def test_a_guided_full_staging_refuses_rather_than_bricking_a_reviewing_walk(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The staging twin of the guided-full decline fails closed instead.

    ``POST /guided/plan`` writes the same checkpoint on both outcomes, with
    ``composer_meta`` copied from the observed head verbatim, so a walk
    mid-review carries its live ``active_proposal`` across either one. The
    DECLINE re-anchors it. The STAGING outcome cannot: it also mints a second
    pending proposal based on that very checkpoint, so moving the first
    proposal's anchor there would leave two live proposals anchored to one
    row and say nothing about which one the walk is reviewing. Which
    semantics that route should have — refuse, or supersede the predecessor —
    is elspeth-da0e3db919, and it is not this fix's to decide.

    What IS this fix's to decide is that the route must not keep bricking
    while that ruling is outstanding. The refusal costs a coded, diagnosable
    ``integrity_error`` on a route the SPA never calls; letting it through
    costs the session permanently, exactly as the decline twin did.
    """

    client = composer_test_client
    session_id, _retained, staged = _stage_schema8_topology_intent_proposal(client, monkeypatch)
    before = _head_record(client, session_id)

    refused = client.post(
        f"/api/sessions/{session_id}/guided/plan",
        json={"operation_id": str(uuid4()), "intent": "Forget the steps, just build the whole pipeline."},
    )

    assert refused.status_code == 500, refused.json()
    detail = refused.json()["detail"]
    assert detail["error_type"] == "guided_operation_terminal_failure"
    assert detail["failure_code"] == "integrity_error"
    # The refusal message names the pending proposal so the operator can chase
    # it in the audit trail; the closed failure vocabulary is what reaches the
    # client. Pin the boundary, so a later "make the error more helpful" edit
    # cannot quietly turn a server-side diagnostic into an egress.
    assert staged["next_turn"]["payload"]["proposal_id"] not in refused.text
    after = _head_record(client, session_id)
    assert (after.id, after.version) == (before.id, before.version), "a refused staging must not advance the head"
    proposals = asyncio.run(client.app.state.session_service.list_composition_proposals(UUID(session_id)))
    assert [str(record.id) for record in proposals] == [staged["next_turn"]["payload"]["proposal_id"]], (
        "the refused staging must not publish a second proposal"
    )
    read_back = _get_guided(client, session_id)
    assert read_back["guided_session"]["step"] == "step_3_transforms"
    assert read_back["next_turn"]["payload"]["proposal_id"] == staged["next_turn"]["payload"]["proposal_id"]

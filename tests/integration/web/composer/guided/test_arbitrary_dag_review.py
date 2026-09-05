"""Authoritative pending-proposal wire review lifecycle.

These tests intentionally exercise the public HTTP surface and durable rows:
Step 3 reviews an immutable pending proposal without publishing it, and Step 4 is the sole
commit point.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.sessions.models import guided_operation_events_table, guided_operations_table
from tests.helpers.guided_leases import abandon_guided_worker_leases
from tests.integration.web.composer.guided.test_plural_sources_outputs import _stage_minimal_plural_proposal
from tests.integration.web.composer.guided.test_respond import (
    TestStep2IntraStep as _Step2Journey,
)
from tests.integration.web.composer.guided.test_respond import (
    _create_session,
    _full_guided_session,
    _get_guided,
    _independent_guided_peer_app,
)


def _stage(client: TestClient, *, filename: str) -> tuple[str, dict]:
    session_id = _create_session(client)
    staged = _Step2Journey()._stage_proposal(client, session_id, filename=filename)
    return session_id, staged


def _bound_action(turn: dict, *, chosen: list[str], operation_id: str | None = None) -> dict:
    return {
        "operation_id": operation_id or str(uuid4()),
        "turn_token": turn["turn_token"],
        "proposal_id": turn["payload"]["proposal_id"],
        "draft_hash": turn["payload"]["draft_hash"],
        "chosen": chosen,
    }


def _source_success_edge(turn: dict) -> dict:
    """Select a success edge that can move to the other reviewed output."""
    return next(connection for connection in turn["payload"]["connections"] if connection["flow"]["kind"] == "source_success")


class _SimulatedWorkerCrash(BaseException):
    """Escape route settlement exactly as a process loss would."""


def test_review_wiring_changes_only_checkpoint_and_keeps_proposal_pending(
    composer_test_client: TestClient,
) -> None:
    session_id, staged = _stage(composer_test_client, filename="review-only.jsonl")
    proposal_turn = staged["next_turn"]
    service = composer_test_client.app.state.session_service
    before = asyncio.run(service.get_current_state(UUID(session_id)))
    assert before is not None
    from elspeth.web.sessions.service import state_from_record

    before_composition = state_from_record(before)
    before_guided = before_composition.guided_session
    assert before_guided is not None
    before_hash = composition_content_hash(before_composition)
    before_deferred = before_guided.deferred_intents

    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(proposal_turn, chosen=["review_wiring"]),
    )

    assert reviewed.status_code == 200, reviewed.json()
    body = reviewed.json()
    guided = _full_guided_session(body)
    assert body["guided_session"]["step"] == "step_4_wire"
    assert body["next_turn"]["type"] == "confirm_wiring"
    assert guided["active_proposal"]["proposal_id"] == proposal_turn["payload"]["proposal_id"]
    assert guided["deferred_intents"] == [intent.to_dict() for intent in before_deferred]

    after = asyncio.run(service.get_current_state(UUID(session_id)))
    assert after is not None and after.id != before.id
    after_composition = state_from_record(after)
    assert composition_content_hash(after_composition) == before_hash
    proposal = next(
        proposal
        for proposal in asyncio.run(service.list_composition_proposals(UUID(session_id)))
        if str(proposal.id) == proposal_turn["payload"]["proposal_id"]
    )
    assert proposal.status == "pending"
    # elspeth-ed67eb9d0d: "changes only checkpoint" now includes moving the
    # pending proposal's anchor onto that checkpoint, recorded as one
    # non-terminal ``proposal.rebased`` event. The row stays pending.
    assert [
        event.event_type
        for event in asyncio.run(service.list_proposal_events(UUID(session_id)))
        if str(event.proposal_id) == proposal_turn["payload"]["proposal_id"]
    ] == ["proposal.created", "proposal.rebased"]


def test_confirm_wiring_is_the_only_commit_and_consumption_point(
    composer_test_client: TestClient,
) -> None:
    session_id, staged = _stage(composer_test_client, filename="confirm-only.jsonl")
    proposal_turn = staged["next_turn"]
    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(proposal_turn, chosen=["review_wiring"]),
    )
    assert reviewed.status_code == 200, reviewed.json()
    wire_turn = reviewed.json()["next_turn"]

    confirmed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(wire_turn, chosen=["confirm_wiring"]),
    )

    assert confirmed.status_code == 200, confirmed.json()
    body = confirmed.json()
    guided = _full_guided_session(body)
    assert guided["active_proposal"] is None
    assert guided["deferred_intents"] == []
    assert body["composition_state"]["outputs"]
    assert body["terminal"]["kind"] == "completed"
    service = composer_test_client.app.state.session_service
    proposal = next(
        proposal
        for proposal in asyncio.run(service.list_composition_proposals(UUID(session_id)))
        if str(proposal.id) == proposal_turn["payload"]["proposal_id"]
    )
    assert proposal.status == "committed"
    assert [
        event.event_type
        for event in asyncio.run(service.list_proposal_events(UUID(session_id)))
        if str(event.proposal_id) == proposal_turn["payload"]["proposal_id"]
        # ``proposal.rebased`` between them is the wire-review anchor move.
    ] == ["proposal.created", "proposal.rebased", "proposal.accepted"]
    assert _get_guided(composer_test_client, session_id)["terminal"]["kind"] == "completed"


def test_step_4_fork_gets_child_local_replan_and_can_complete(
    composer_test_client: TestClient,
) -> None:
    parent_id, staged = _stage(composer_test_client, filename="fork-replan.jsonl")
    reviewed = composer_test_client.post(
        f"/api/sessions/{parent_id}/guided/respond",
        json=_bound_action(staged["next_turn"], chosen=["review_wiring"]),
    )
    assert reviewed.status_code == 200, reviewed.json()
    service = composer_test_client.app.state.session_service
    parent_state = asyncio.run(service.get_current_state(UUID(parent_id)))
    assert parent_state is not None
    fork_message = asyncio.run(
        service.add_message(
            UUID(parent_id),
            "user",
            "Fork and replan this reviewed topology.",
            composition_state_id=parent_state.id,
            writer_principal="route_user_message",
        )
    )

    forked = composer_test_client.post(
        f"/api/sessions/{parent_id}/fork",
        json={
            "operation_id": str(uuid4()),
            "from_message_id": str(fork_message.id),
            "new_message_content": "Replan this topology in the child.",
        },
    )
    assert forked.status_code == 201, forked.json()
    child_id = forked.json()["session_id"]
    resumed = _get_guided(composer_test_client, child_id)
    assert resumed["guided_session"]["step"] == "step_2_sink"
    assert resumed["next_turn"]["type"] == "review_components"
    assert resumed["next_turn"]["payload"]["component_kind"] == "output"

    replanned = composer_test_client.post(
        f"/api/sessions/{child_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": resumed["next_turn"]["turn_token"],
            "component_action": {"action": "finish", "component_kind": "output"},
        },
    )
    assert replanned.status_code == 200, replanned.json()
    assert replanned.json()["next_turn"]["type"] == "propose_pipeline"

    child_wire = composer_test_client.post(
        f"/api/sessions/{child_id}/guided/respond",
        json=_bound_action(replanned.json()["next_turn"], chosen=["review_wiring"]),
    )
    assert child_wire.status_code == 200, child_wire.json()
    completed = composer_test_client.post(
        f"/api/sessions/{child_id}/guided/respond",
        json=_bound_action(child_wire.json()["next_turn"], chosen=["confirm_wiring"]),
    )
    assert completed.status_code == 200, completed.json()
    assert completed.json()["terminal"]["kind"] == "completed"
    assert _get_guided(composer_test_client, parent_id)["terminal"] is None


def test_wire_projection_uses_the_pending_candidate_stable_ids_and_exact_contract_shape(
    composer_test_client: TestClient,
) -> None:
    session_id, staged = _stage(composer_test_client, filename="stable-wire.jsonl")
    proposal = staged["next_turn"]["payload"]

    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(staged["next_turn"], chosen=["review_wiring"]),
    )

    assert reviewed.status_code == 200, reviewed.json()
    wire = reviewed.json()["next_turn"]["payload"]
    assert set(wire) == {
        "proposal_id",
        "draft_hash",
        "sources",
        "nodes",
        "outputs",
        "connections",
        "semantic_contracts",
        "warnings",
        "blockers",
        "can_confirm",
    }
    assert wire["proposal_id"] == proposal["proposal_id"]
    assert wire["draft_hash"] == proposal["draft_hash"]
    assert [source["stable_id"] for source in wire["sources"]] == [source["stable_id"] for source in proposal["graph"]["sources"]]
    assert [output["stable_id"] for output in wire["outputs"]] == [output["stable_id"] for output in proposal["outputs"]]
    assert [connection["stable_id"] for connection in wire["connections"]] == [edge["stable_id"] for edge in proposal["graph"]["edges"]]
    assert wire["connections"] == [
        {
            "stable_id": edge["stable_id"],
            "from_endpoint": edge["from_endpoint"],
            "to_endpoint": edge["to_endpoint"],
            "flow": edge["flow"],
            "schema_contract": wire["connections"][index]["schema_contract"],
        }
        for index, edge in enumerate(proposal["graph"]["edges"])
    ]
    assert wire["sources"][0]["row_cardinality"] == {
        "input": "none",
        "output": "zero_or_many",
        "expected_output_count": None,
    }
    assert wire["outputs"][0]["business_schema"] == {
        "mode": "observed",
        "fields": [],
        "guaranteed_fields": [],
        "required_fields": [],
    }
    assert wire["outputs"][0]["required_fields"] == ["text"]
    assert wire["nodes"] == []
    assert wire["blockers"] == []
    assert wire["can_confirm"] is True
    assert "topology" not in wire


def test_wire_correction_persists_feedback_once_and_immutably_supersedes(
    composer_test_client: TestClient,
) -> None:
    session_id, staged = _stage_minimal_plural_proposal(
        composer_test_client,
        suffix="corrected-wire",
        source_count=3,
    )
    original = staged["next_turn"]["payload"]
    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(staged["next_turn"], chosen=["review_wiring"]),
    )
    assert reviewed.status_code == 200, reviewed.json()
    wire_turn = reviewed.json()["next_turn"]
    selected_edge = _source_success_edge(wire_turn)
    target = {
        "kind": "edge",
        "stable_id": selected_edge["stable_id"],
    }
    operation_id = str(uuid4())
    correction_request = {
        "operation_id": operation_id,
        "turn_token": wire_turn["turn_token"],
        "proposal_id": original["proposal_id"],
        "draft_hash": original["draft_hash"],
        "edit_target": target,
        "correction_feedback": "Route the reviewed source to the other reviewed output.",
    }
    service = composer_test_client.app.state.session_service
    messages_before = asyncio.run(service.get_messages(UUID(session_id), limit=None))

    corrected = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=correction_request,
    )

    assert corrected.status_code == 200, corrected.json()
    body = corrected.json()
    assert body["guided_session"]["step"] == "step_4_wire"
    assert body["next_turn"]["type"] == "confirm_wiring"
    successor = body["next_turn"]["payload"]
    assert successor["proposal_id"] != original["proposal_id"]
    assert successor["draft_hash"] != original["draft_hash"]
    replay = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=correction_request,
    )
    assert replay.status_code == 200
    assert replay.json() == body

    messages_after = asyncio.run(service.get_messages(UUID(session_id), limit=None))
    new_messages = [message for message in messages_after if message.id not in {item.id for item in messages_before}]
    new_user_messages = [message for message in new_messages if message.role == "user"]
    assert [(message.role, message.content, message.writer_principal) for message in new_user_messages] == [
        ("user", correction_request["correction_feedback"], "route_user_message")
    ]
    proposals = {str(proposal.id): proposal for proposal in asyncio.run(service.list_composition_proposals(UUID(session_id)))}
    assert proposals[original["proposal_id"]].status == "rejected"
    assert proposals[successor["proposal_id"]].status == "pending"
    events = asyncio.run(service.list_proposal_events(UUID(session_id)))
    assert [event.event_type for event in events if str(event.proposal_id) == original["proposal_id"]] == [
        "proposal.created",
        # The wire-review advance moved this proposal's anchor before the
        # correction superseded it (elspeth-ed67eb9d0d).
        "proposal.rebased",
        "proposal.rejected",
    ]
    rejected = next(
        event for event in events if str(event.proposal_id) == original["proposal_id"] and event.event_type == "proposal.rejected"
    )
    assert rejected.payload["reason_code"] == "superseded"

    confirmed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": body["next_turn"]["turn_token"],
            "proposal_id": successor["proposal_id"],
            "draft_hash": successor["draft_hash"],
            "chosen": ["confirm_wiring"],
        },
    )
    assert confirmed.status_code == 200, confirmed.json()
    assert confirmed.json()["terminal"]["kind"] == "completed"


def test_wire_correction_rejects_a_stale_target_without_writes(composer_test_client: TestClient) -> None:
    session_id, staged = _stage(composer_test_client, filename="stale-target.jsonl")
    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(staged["next_turn"], chosen=["review_wiring"]),
    )
    assert reviewed.status_code == 200, reviewed.json()
    wire_turn = reviewed.json()["next_turn"]
    service = composer_test_client.app.state.session_service
    state_before = asyncio.run(service.get_current_state(UUID(session_id)))
    proposals_before = asyncio.run(service.list_composition_proposals(UUID(session_id)))
    messages_before = asyncio.run(service.get_messages(UUID(session_id), limit=None))

    rejected = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": wire_turn["turn_token"],
            "proposal_id": wire_turn["payload"]["proposal_id"],
            "draft_hash": wire_turn["payload"]["draft_hash"],
            "edit_target": {"kind": "node", "stable_id": str(uuid4())},
            "correction_feedback": "Change the missing node.",
        },
    )

    assert rejected.status_code == 409
    assert asyncio.run(service.get_current_state(UUID(session_id))).id == state_before.id
    assert asyncio.run(service.list_composition_proposals(UUID(session_id))) == proposals_before
    assert asyncio.run(service.get_messages(UUID(session_id), limit=None)) == messages_before


@pytest.mark.parametrize("crash_point", ("after_admission", "after_compute_before_record", "after_dispatch"))
def test_expired_confirmation_takeover_recovers_without_duplicate_dispatch(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    from elspeth.web.composer import pipeline_commit

    session_id, staged = _stage(composer_test_client, filename=f"crash-{crash_point}.jsonl")
    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(staged["next_turn"], chosen=["review_wiring"]),
    )
    assert reviewed.status_code == 200, reviewed.json()
    wire_turn = reviewed.json()["next_turn"]
    operation_id = str(uuid4())
    request_body = _bound_action(wire_turn, chosen=["confirm_wiring"], operation_id=operation_id)
    service = composer_test_client.app.state.session_service
    original_prepare = pipeline_commit.prepare_pipeline_proposal_commit
    original_execute = pipeline_commit.execute_tool
    original_record = service.record_guided_pipeline_dispatch
    original_accept = service.accept_guided_pipeline_proposal
    execute_calls = 0

    def counted_execute(*args, **kwargs):
        nonlocal execute_calls
        execute_calls += 1
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(pipeline_commit, "execute_tool", counted_execute)
    state_versions_before = asyncio.run(service.get_state_versions(UUID(session_id)))
    proposal_events_before = asyncio.run(service.list_proposal_events(UUID(session_id)))

    engine = composer_test_client.app.state.session_engine

    def _worker_lost(reason: str) -> _SimulatedWorkerCrash:
        # A lost process runs no cleanup; its leases lapse and another
        # replica takes the operation over. Model that here: both of this
        # worker's authorities lapse BEFORE its exception reaches the lease
        # guard, so the guard finds no live authority to terminalise (the
        # Q3 ruling: a takeover is not a failure) and the row stays
        # ``in_progress`` for the recovery below to claim.
        abandon_guided_worker_leases(engine, session_id=session_id, operation_id=operation_id)
        return _SimulatedWorkerCrash(reason)

    if crash_point == "after_admission":

        async def crash_before_dispatch(**_kwargs):
            raise _worker_lost("worker lost after admission")

        monkeypatch.setattr(pipeline_commit, "prepare_pipeline_proposal_commit", crash_before_dispatch)
    elif crash_point == "after_compute_before_record":

        async def crash_before_dispatch_record(*_args, **_kwargs):
            raise _worker_lost("worker lost after prepared computation and before durable record")

        monkeypatch.setattr(service, "record_guided_pipeline_dispatch", crash_before_dispatch_record)
    else:

        async def crash_before_accept(*_args, **_kwargs):
            raise _worker_lost("worker lost after durable dispatch")

        monkeypatch.setattr(service, "accept_guided_pipeline_proposal", crash_before_accept)

    with pytest.raises(_SimulatedWorkerCrash):
        composer_test_client.post(
            f"/api/sessions/{session_id}/guided/respond",
            json=request_body,
        )

    messages_after_crash = asyncio.run(service.get_messages(UUID(session_id), limit=None))
    dispatches_after_crash = [
        envelope
        for message in messages_after_crash
        for envelope in (message.tool_calls or ())
        if envelope.get("invocation", {}).get("tool_name") == "set_pipeline" and envelope.get("invocation", {}).get("status") == "success"
    ]
    assert len(dispatches_after_crash) == (1 if crash_point == "after_dispatch" else 0)
    assert asyncio.run(service.get_state_versions(UUID(session_id))) == state_versions_before
    assert asyncio.run(service.list_proposal_events(UUID(session_id))) == proposal_events_before

    with engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table)
                .where(guided_operations_table.c.session_id == session_id)
                .where(guided_operations_table.c.operation_id == operation_id)
            )
            .mappings()
            .one()
        )
        # The lease guard saw the crash escape with both authorities already
        # lapsed and did NOT terminalise the row: no failure is recorded
        # against an operation another replica may still legitimately finish.
        assert operation["status"] == "in_progress"
        assert operation["failure_code"] is None
        assert operation["proposal_id"] == wire_turn["payload"]["proposal_id"]

    if crash_point == "after_admission":
        monkeypatch.setattr(pipeline_commit, "prepare_pipeline_proposal_commit", original_prepare)
    elif crash_point == "after_compute_before_record":
        monkeypatch.setattr(service, "record_guided_pipeline_dispatch", original_record)
    else:
        monkeypatch.setattr(service, "accept_guided_pipeline_proposal", original_accept)

    recovered = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=request_body,
    )

    assert recovered.status_code == 200, recovered.json()
    assert recovered.json()["terminal"]["kind"] == "completed"
    messages = asyncio.run(service.get_messages(UUID(session_id), limit=None))
    dispatches = [
        envelope
        for message in messages
        for envelope in (message.tool_calls or ())
        if envelope.get("invocation", {}).get("tool_name") == "set_pipeline" and envelope.get("invocation", {}).get("status") == "success"
    ]
    assert len(dispatches) == 1
    assert execute_calls == (2 if crash_point == "after_compute_before_record" else 1)
    assert len(asyncio.run(service.get_state_versions(UUID(session_id)))) == len(state_versions_before) + 1
    proposal_events = asyncio.run(service.list_proposal_events(UUID(session_id)))
    assert [event.event_type for event in proposal_events if str(event.proposal_id) == wire_turn["payload"]["proposal_id"]] == [
        "proposal.created",
        "proposal.rebased",
        "proposal.accepted",
    ]
    with engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table)
                .where(guided_operations_table.c.session_id == session_id)
                .where(guided_operations_table.c.operation_id == operation_id)
            )
            .mappings()
            .one()
        )
        events = (
            connection.execute(
                select(guided_operation_events_table.c.event_kind)
                .where(guided_operation_events_table.c.session_id == session_id)
                .where(guided_operation_events_table.c.operation_id == operation_id)
                .order_by(guided_operation_events_table.c.sequence)
            )
            .scalars()
            .all()
        )
    assert operation["status"] == "completed"
    assert operation["attempt"] == 2
    assert events == ["claimed", "renewed", "taken_over", "renewed", "completed"]


@pytest.mark.parametrize(
    "composer_test_client",
    (
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.testcontainer),
    ),
    indirect=True,
)
@pytest.mark.parametrize("wire_action", ("confirm", "correct"))
@pytest.mark.parametrize("winner", ("revert", "wire"))
def test_independent_workers_serialize_revert_vs_wire_action_with_exact_publication(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    wire_action: str,
    winner: str,
) -> None:
    session_id, staged = _stage_minimal_plural_proposal(
        composer_test_client,
        suffix=f"revert-{winner}-wire-{wire_action}",
        source_count=3,
    )
    proposal = staged["next_turn"]["payload"]
    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_bound_action(staged["next_turn"], chosen=["review_wiring"]),
    )
    assert reviewed.status_code == 200, reviewed.json()
    wire_turn = reviewed.json()["next_turn"]
    service = composer_test_client.app.state.session_service
    peer_app = _independent_guided_peer_app(composer_test_client)
    peer_service = peer_app.state.session_service
    assert service is not peer_service
    assert composer_test_client.app.state.session_compose_lock_registry is not peer_app.state.session_compose_lock_registry
    versions = asyncio.run(service.get_state_versions(UUID(session_id)))
    target_state_id = str(versions[0].id)
    state_count_before = len(versions)
    correction_feedback = "Route the reviewed source to the other reviewed output."
    wire_operation_id = str(uuid4())
    if wire_action == "confirm":
        wire_request = _bound_action(wire_turn, chosen=["confirm_wiring"], operation_id=wire_operation_id)
        original_wire = service.admit_guided_pipeline_confirmation
    else:
        selected_edge = _source_success_edge(wire_turn)
        wire_request = {
            "operation_id": wire_operation_id,
            "turn_token": wire_turn["turn_token"],
            "proposal_id": proposal["proposal_id"],
            "draft_hash": proposal["draft_hash"],
            "edit_target": {
                "kind": "edge",
                "stable_id": selected_edge["stable_id"],
            },
            "correction_feedback": correction_feedback,
        }
        original_wire = service.stage_guided_pipeline_proposal
    revert_operation_id = str(uuid4())
    revert_request = {"operation_id": revert_operation_id, "state_id": target_state_id}
    original_revert = peer_service.revert_state_for_guided_operation
    wire_entered = asyncio.Event()
    revert_entered = asyncio.Event()
    release_race = asyncio.Event()
    winner_settled = asyncio.Event()

    async def blocking_revert(*args, **kwargs):
        revert_entered.set()
        await release_race.wait()
        if winner != "revert":
            await winner_settled.wait()
        try:
            return await original_revert(*args, **kwargs)
        finally:
            if winner == "revert":
                winner_settled.set()

    async def blocking_wire(*args, **kwargs):
        wire_entered.set()
        await release_race.wait()
        if winner != "wire":
            await winner_settled.wait()
        try:
            return await original_wire(*args, **kwargs)
        finally:
            if winner == "wire":
                winner_settled.set()

    monkeypatch.setattr(peer_service, "revert_state_for_guided_operation", blocking_revert)
    monkeypatch.setattr(
        service,
        "admit_guided_pipeline_confirmation" if wire_action == "confirm" else "stage_guided_pipeline_proposal",
        blocking_wire,
    )

    loser = "wire" if winner == "revert" else "revert"

    async def race():
        async with (
            AsyncClient(transport=ASGITransport(app=composer_test_client.app), base_url="http://wire-worker") as wire_client,
            AsyncClient(transport=ASGITransport(app=peer_app), base_url="http://revert-worker") as revert_client,
        ):
            clients = {"revert": revert_client, "wire": wire_client}
            paths = {"revert": f"/api/sessions/{session_id}/state/revert", "wire": f"/api/sessions/{session_id}/guided/respond"}
            bodies = {"revert": revert_request, "wire": wire_request}
            entered = {"revert": revert_entered, "wire": wire_entered}
            winner_task = asyncio.create_task(clients[winner].post(paths[winner], json=bodies[winner]))
            await asyncio.wait_for(entered[winner].wait(), timeout=10)
            # Independent workers serialise at the session-operation lease:
            # the second worker is refused with the platform's 409 while the
            # first holds the session, before any row or double of its own.
            refused = await asyncio.wait_for(clients[loser].post(paths[loser], json=bodies[loser]), timeout=10)
            assert not entered[loser].is_set()
            release_race.set()
            winner_response = await asyncio.wait_for(winner_task, timeout=20)
            return refused, winner_response

    async def retry_and_replay():
        async with (
            AsyncClient(transport=ASGITransport(app=composer_test_client.app), base_url="http://wire-worker") as wire_client,
            AsyncClient(transport=ASGITransport(app=peer_app), base_url="http://revert-worker") as revert_client,
        ):
            clients = {"revert": revert_client, "wire": wire_client}
            paths = {"revert": f"/api/sessions/{session_id}/state/revert", "wire": f"/api/sessions/{session_id}/guided/respond"}
            bodies = {"revert": revert_request, "wire": wire_request}
            loser_response = await asyncio.wait_for(clients[loser].post(paths[loser], json=bodies[loser]), timeout=20)
            replays = {
                "revert": await wire_client.post(paths["revert"], json=revert_request),
                "wire": await revert_client.post(paths["wire"], json=wire_request),
            }
            return loser_response, replays

    refused, winner_response = asyncio.run(race())
    assert refused.status_code == 409, refused.json()
    assert refused.json() == {"detail": "Session operation is already active"}
    assert winner_response.status_code == 200, winner_response.json()
    # The refused worker never reached its double and published nothing: the
    # race's exact publication is the winner's alone.
    assert not (wire_entered if loser == "wire" else revert_entered).is_set()
    assert len(asyncio.run(service.get_state_versions(UUID(session_id)))) == state_count_before + 1
    proposals = {str(item.id): item for item in asyncio.run(service.list_composition_proposals(UUID(session_id)))}
    events = asyncio.run(service.list_proposal_events(UUID(session_id)))
    messages = asyncio.run(service.get_messages(UUID(session_id), limit=None))
    dispatches = [
        envelope
        for message in messages
        for envelope in (message.tool_calls or ())
        if envelope.get("invocation", {}).get("tool_name") == "set_pipeline" and envelope.get("invocation", {}).get("status") == "success"
    ]
    if winner == "revert":
        assert set(proposals) == {proposal["proposal_id"]}
        assert proposals[proposal["proposal_id"]].status == "rejected"
        assert [event.event_type for event in events] == ["proposal.created", "proposal.rebased", "proposal.rejected"]
        assert all(message.content != correction_feedback for message in messages)
        assert dispatches == []
    elif wire_action == "confirm":
        assert set(proposals) == {proposal["proposal_id"]}
        assert proposals[proposal["proposal_id"]].status == "committed"
        assert [event.event_type for event in events] == ["proposal.created", "proposal.rebased", "proposal.accepted"]
        assert len(dispatches) == 1
    else:
        assert len(proposals) == 2
        assert proposals[proposal["proposal_id"]].status == "rejected"
        assert [event.event_type for event in events if str(event.proposal_id) == proposal["proposal_id"]] == [
            "proposal.created",
            "proposal.rebased",
            "proposal.rejected",
        ]
        successor = next(item for proposal_id, item in proposals.items() if proposal_id != proposal["proposal_id"])
        assert successor.status == "pending"
        assert [message.content for message in messages].count(correction_feedback) == 1
        assert dispatches == []

    engine = composer_test_client.app.state.session_engine
    with engine.connect() as connection:
        operation_rows = (
            connection.execute(
                select(guided_operations_table)
                .where(guided_operations_table.c.session_id == session_id)
                .where(guided_operations_table.c.operation_id.in_((revert_operation_id, wire_operation_id)))
            )
            .mappings()
            .all()
        )
        operation_event_rows = (
            connection.execute(
                select(guided_operation_events_table)
                .where(guided_operation_events_table.c.session_id == session_id)
                .where(guided_operation_events_table.c.operation_id.in_((revert_operation_id, wire_operation_id)))
                .order_by(guided_operation_events_table.c.operation_id, guided_operation_events_table.c.sequence)
            )
            .mappings()
            .all()
        )
    operations = {row["operation_id"]: row for row in operation_rows}
    winner_operation_id = revert_operation_id if winner == "revert" else wire_operation_id
    loser_operation_id = wire_operation_id if winner == "revert" else revert_operation_id
    # Refused at the session fence, the loser reserved no operation at all.
    assert set(operations) == {winner_operation_id}
    assert operations[winner_operation_id]["status"] == "completed"
    assert operations[winner_operation_id]["failure_code"] is None
    event_kinds = {
        operation_id: [row["event_kind"] for row in operation_event_rows if row["operation_id"] == operation_id]
        for operation_id in (revert_operation_id, wire_operation_id)
    }
    assert event_kinds[winner_operation_id][-1] == "completed"
    assert event_kinds[loser_operation_id] == []

    # The refused worker retries once the lease is free, and every request
    # replays exactly on the other worker.
    loser_response, replays = asyncio.run(retry_and_replay())
    responses = {winner: winner_response, loser: loser_response}
    for action in ("revert", "wire"):
        assert replays[action].status_code == responses[action].status_code
        assert replays[action].json() == responses[action].json()
    versions_after_retry = asyncio.run(service.get_state_versions(UUID(session_id)))
    with engine.connect() as connection:
        retry_rows = {
            row["operation_id"]: row
            for row in connection.execute(
                select(guided_operations_table)
                .where(guided_operations_table.c.session_id == session_id)
                .where(guided_operations_table.c.operation_id.in_((revert_operation_id, wire_operation_id)))
            ).mappings()
        }
    if winner == "revert":
        # The wire action names a proposal the revert made inactive: refused
        # at the guided turn contract before any reservation, publishing
        # nothing on the retry either.
        assert loser_response.status_code == 409, loser_response.json()
        assert loser_response.json() == {"detail": "proposal_id and draft_hash do not identify the active guided proposal"}
        assert set(retry_rows) == {revert_operation_id}
        assert len(versions_after_retry) == state_count_before + 1
    else:
        # A revert issued after the wire action settled is a legitimate new
        # operation: it completes and publishes exactly one more state.
        assert loser_response.status_code == 200, loser_response.json()
        assert set(retry_rows) == {revert_operation_id, wire_operation_id}
        assert retry_rows[revert_operation_id]["status"] == "completed"
        assert retry_rows[revert_operation_id]["result_state_id"] == loser_response.json()["id"]
        assert len(versions_after_retry) == state_count_before + 2

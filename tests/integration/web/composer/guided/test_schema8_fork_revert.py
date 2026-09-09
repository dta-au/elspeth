"""Public-route persistence checks for schema-8 fork and revert."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from elspeth.web.composer.guided.profile import EMPTY_PROFILE
from elspeth.web.composer.guided.protocol import GUIDED_GOAL_ACKNOWLEDGEMENT, GuidedStep
from elspeth.web.sessions.converters import state_from_record
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

_PARENT_GOAL = "Summarize each page and save the summaries as JSON"


def test_early_guided_checkpoint_survives_public_fork_and_revert_without_proposal_authority(
    composer_test_client: TestClient,
) -> None:
    created = composer_test_client.post("/api/sessions", json={"title": "schema-8 persistence"})
    assert created.status_code == 201, created.json()
    parent_id = created.json()["id"]

    started = composer_test_client.post(
        f"/api/sessions/{parent_id}/guided/start",
        json={"operation_id": str(uuid4()), "profile": "live", "intent": "Build a live pipeline"},
    )
    assert started.status_code == 200, started.json()
    target_state_id = started.json()["composition_state"]["id"]
    assert started.json()["guided_session"]["step"] == GuidedStep.STEP_1_SOURCE.value

    service = composer_test_client.app.state.session_service
    target_record = asyncio.run(service.get_state_in_session(UUID(target_state_id), UUID(parent_id)))
    target_guided = state_from_record(target_record).guided_session
    assert target_guided is not None
    assert target_guided.active_proposal is None
    assert target_guided.active_edit_target is None
    fork_message = asyncio.run(
        service.add_message(
            UUID(parent_id),
            "user",
            "Build from this checkpoint.",
            composition_state_id=UUID(target_state_id),
            writer_principal="route_user_message",
        )
    )
    forked = composer_test_client.post(
        f"/api/sessions/{parent_id}/fork",
        json={
            "operation_id": str(uuid4()),
            "from_message_id": str(fork_message.id),
            "new_message_content": "Build the edited request.",
        },
    )
    assert forked.status_code == 201, forked.json()
    child_id = UUID(forked.json()["session_id"])
    child_record = asyncio.run(service.get_current_state(child_id))
    assert child_record is not None
    child_guided = state_from_record(child_record).guided_session
    assert child_guided is not None
    assert child_guided.step is GuidedStep.STEP_1_SOURCE
    assert child_guided.active_proposal is None
    assert child_guided.active_edit_target is None

    reverted = composer_test_client.post(
        f"/api/sessions/{parent_id}/state/revert",
        json={"operation_id": str(uuid4()), "state_id": target_state_id},
    )
    assert reverted.status_code == 200, reverted.json()
    restored_record = asyncio.run(service.get_current_state(UUID(parent_id)))
    assert restored_record is not None
    restored_guided = state_from_record(restored_record).guided_session
    assert restored_guided is not None
    assert restored_guided.step is GuidedStep.STEP_1_SOURCE
    assert restored_guided.active_proposal is None
    assert restored_guided.active_edit_target is None


def _root_of(service, session_id: UUID) -> object:
    """Return the session's sole ``route_user_message`` root row."""
    roots = [message for message in asyncio.run(service.get_messages(session_id, limit=None)) if message.role == "user"]
    assert len(roots) == 1, [message.content for message in roots]
    return roots[0]


@pytest.mark.parametrize("parent_kind", ("tutorial_start", "convert"))
def test_rooted_parent_fork_hands_the_child_verifiable_root_custody(
    composer_test_client: TestClient,
    parent_kind: str,
) -> None:
    """A forked child's root re-derives under the child's OWN start authority.

    Goal-first roots EVERY guided session, so every fork now carries a root
    across the session boundary — including the two shapes that could not have
    one before: a tutorial-profile start, and a conversion (operation kind
    ``guided_convert``).

    The coupling this pins: ``_strip_guided_profile_in_meta`` resets the child
    to ``EMPTY_PROFILE`` whatever the parent's profile was, and the fork
    settlement synthesises the child's ``guided_start`` row under a literal
    ``"profile": "live"`` request hash. Those two agree only because the
    custody helper derives the profile discriminator from the CHILD's own
    start checkpoint rather than from the parent's. The settlement runs
    ``_verify_guided_root_message_authority`` on the child before it commits,
    so a 201 here is itself that helper passing; the explicit call below pins
    the second helper on the same row.
    """

    created = composer_test_client.post("/api/sessions", json={"title": f"fork-root-{parent_kind}"})
    assert created.status_code == 201, created.json()
    parent_id = created.json()["id"]

    if parent_kind == "tutorial_start":
        opened = composer_test_client.post(
            f"/api/sessions/{parent_id}/guided/start",
            json={"operation_id": str(uuid4()), "profile": "tutorial", "intent": _PARENT_GOAL},
        )
    else:
        opened = composer_test_client.post(
            f"/api/sessions/{parent_id}/guided/convert",
            json={"operation_id": str(uuid4()), "intent": _PARENT_GOAL},
        )
    assert opened.status_code == 200, opened.json()
    target_state_id = opened.json()["composition_state"]["id"]

    service = composer_test_client.app.state.session_service
    parent_root = _root_of(service, UUID(parent_id))
    fork_message = asyncio.run(
        service.add_message(
            UUID(parent_id),
            "user",
            "Build from this checkpoint.",
            composition_state_id=UUID(target_state_id),
            writer_principal="route_user_message",
        )
    )

    forked = composer_test_client.post(
        f"/api/sessions/{parent_id}/fork",
        json={
            "operation_id": str(uuid4()),
            "from_message_id": str(fork_message.id),
            "new_message_content": "Build the edited request.",
        },
    )
    assert forked.status_code == 201, forked.json()
    child_id = UUID(forked.json()["session_id"])

    child_record = asyncio.run(service.get_current_state(child_id))
    assert child_record is not None
    child_guided = state_from_record(child_record).guided_session
    assert child_guided is not None
    # The child is always EMPTY_PROFILE, even from a tutorial parent — which is
    # what makes the synthesised ``"profile": "live"`` request hash honest.
    assert child_guided.profile == EMPTY_PROFILE
    assert child_guided.root_intent_message_id is not None
    assert child_guided.root_intent_message_id != str(parent_root.id)

    verified = asyncio.run(
        service.get_verified_guided_root_intent(
            session_id=child_id,
            root_message_id=UUID(child_guided.root_intent_message_id),
        )
    )
    assert verified.content == _PARENT_GOAL

    # The seeded transcript pair crosses verbatim: the child opens on the same
    # conversation the parent did.
    assert [(turn.role.value, turn.content) for turn in child_guided.chat_history[:2]] == [
        ("user", _PARENT_GOAL),
        ("assistant", GUIDED_GOAL_ACKNOWLEDGEMENT),
    ]


def test_fork_at_the_goal_message_is_refused_rather_than_rooting_a_child_outside_its_slice(
    composer_test_client: TestClient,
) -> None:
    """Forking AT the goal leaves the checkpoint pointing outside the copied slice.

    Pre-existing behaviour of ``_strip_guided_profile_in_meta``: a fork copies
    the messages strictly BEFORE the fork point, so a guided lineage reference
    to the fork message itself can never be remapped and ``_child_user_message``
    fails the operation closed. Goal-first does not create that defect, but it
    makes ``root_intent_message_id`` its FIRST reachable arm — every guided
    session's first user message is now the goal row, and "edit and resend" is
    offered on it — so the shape is pinned here rather than left to be
    rediscovered.

    What is pinned is the CURRENT behaviour, exactly, not an endorsement of it:
    an opaque terminal-failure envelope with no code the user can act on. The
    honest answer for "you cannot fork at the message that roots this session"
    is a coded refusal, and choosing it is the open decision on
    elspeth-8b7999d1b0 ("Needs a decision on what the child's guided state
    should mean, not just a relaxed guard"), which names this exact raise and
    this exact reference. This test is the tripwire for that ticket: when the
    refusal is given its code, this assertion is the one that must change, and
    it says so rather than accepting anything that merely failed.
    """

    created = composer_test_client.post("/api/sessions", json={"title": "fork-at-goal"})
    assert created.status_code == 201, created.json()
    parent_id = created.json()["id"]
    started = composer_test_client.post(
        f"/api/sessions/{parent_id}/guided/start",
        json={"operation_id": str(uuid4()), "profile": "live", "intent": _PARENT_GOAL},
    )
    assert started.status_code == 200, started.json()

    service = composer_test_client.app.state.session_service
    root = _root_of(service, UUID(parent_id))
    sessions_before = asyncio.run(service.list_sessions("alice", "local", limit=100))

    forked = composer_test_client.post(
        f"/api/sessions/{parent_id}/fork",
        json={
            "operation_id": str(uuid4()),
            "from_message_id": str(root.id),
            "new_message_content": "A different goal entirely.",
        },
    )

    assert forked.status_code == 500, forked.json()
    assert forked.json()["detail"] == {
        "error_type": "guided_operation_terminal_failure",
        "failure_code": "integrity_error",
        "detail": "The operation failed an integrity check.",
    }
    sessions_after = asyncio.run(service.list_sessions("alice", "local", limit=100))
    assert [session.id for session in sessions_after] == [session.id for session in sessions_before]

"""Integration tests for POST /api/sessions/{id}/guided/convert.

The "Switch to guided" affordance on a freeform session that has already done
composition work used to hit GET /guided, which 400s by design for a session
whose persisted CompositionState carries no ``guided_session`` (freeform).
There was no backend operation to move such a session into guided mode
(elspeth-e2c3dba6b5).

POST /guided/convert is that operation. Per the "fresh wizard + consent"
product decision it does NOT try to walk the retained freeform graph through
the wizard (GuidedSession.initial() starts at STEP_1_SOURCE and the step
handlers would clobber a pre-built graph). Instead it seeds a FRESH guided
wizard as a NEW composition-state version. The prior freeform pipeline stays
reachable via GET /state/versions + POST /state/revert — the same
recoverability contract as YAML import — so nothing is lost.

Goal-first (elspeth-378cfa0e18): the conversion now REQUIRES the author's goal
and writes it as the session's durable root intent, under its own
``guided_convert`` operation kind, in the same transaction as the checkpoint.

Branch behaviour:
  * no persisted state (empty session)         -> persist a fresh schema-8 wizard,
                                                  rooted in the goal, with an
                                                  immutable replay locator
  * guided_session already present             -> 409 ``guided_already_started``:
                                                  the session cannot adopt this
                                                  request's goal, and returning it
                                                  unchanged would discard it
  * persisted state, guided_session is None    -> THE CONVERSION: reseed a fresh
                                                  rooted wizard as a new version;
                                                  the prior freeform version is
                                                  recoverable
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.protocol import GUIDED_GOAL_ACKNOWLEDGEMENT
from elspeth.web.composer.guided.state_machine import TerminalKind, TerminalState
from elspeth.web.sessions.guided_operations import guided_operation_request_hash
from elspeth.web.sessions.guided_replay import load_guided_json_payload
from elspeth.web.sessions.models import guided_operations_table
from elspeth.web.sessions.protocol import CompositionStateData, GuidedOperationCompleted, GuidedOperationSettlementConflictError
from elspeth.web.sessions.schemas import ConvertGuidedRequest
from tests.integration.web.conftest import _save_composition_state_with_compose_authority
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

# ---------------------------------------------------------------------------
# Helpers (self-contained; must not couple to another test module's fixtures)
# ---------------------------------------------------------------------------


_START_INTENT = "Summarize each page and save the summaries as JSON"
_CONVERT_INTENT = "Turn the uploaded CSV into a per-region summary"


def _create_session(client: TestClient, *, profile: str | None = None) -> str:
    resp = client.post("/api/sessions", json={"title": "convert-test"})
    assert resp.status_code == 201, resp.json()
    session_id = resp.json()["id"]
    if profile is not None:
        start = client.post(
            f"/api/sessions/{session_id}/guided/start",
            json={"profile": profile, "intent": _START_INTENT, "operation_id": str(uuid4())},
        )
        assert start.status_code == 200, start.json()
    return session_id


def _convert_raw(client: TestClient, session_id: str, *, operation_id: str | None = None) -> object:
    return client.post(
        f"/api/sessions/{session_id}/guided/convert",
        json={"operation_id": operation_id or str(uuid4()), "intent": _CONVERT_INTENT},
    )


def _convert(client: TestClient, session_id: str) -> dict:
    resp = _convert_raw(client, session_id)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _convert_outcome(client: TestClient, session_id: str, operation_id: str) -> GuidedOperationCompleted:
    request = ConvertGuidedRequest(operation_id=operation_id, intent=_CONVERT_INTENT)
    request_hash = guided_operation_request_hash(
        session_id=UUID(session_id),
        kind="guided_convert",
        request=request,
    )
    outcome = asyncio.run(
        client.app.state.session_service.get_guided_operation(
            session_id=UUID(session_id),
            operation_id=operation_id,
            kind="guided_convert",
            request_hash=request_hash,
        )
    )
    assert isinstance(outcome, GuidedOperationCompleted)
    return outcome


def _guided_turn_emitted_args(client: TestClient, session_id: str) -> list[dict]:
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    events: list[dict] = []
    for message in messages:
        for tool_call in message.tool_calls or ():
            invocation = tool_call.get("invocation", {})
            if invocation.get("tool_name") == "guided_turn_emitted":
                events.append(json.loads(invocation["arguments_canonical"]))
    return events


def _seed_freeform_state_with_work(client: TestClient, session_id: str) -> None:
    """Persist a freeform composition state that carries real work.

    ``composer_meta={}`` (no ``guided_session`` key) is what makes GET /guided
    400 — the exact precondition of the bug. A committed source stands in for
    "the operator did freeform composition work worth preserving".
    """
    service = client.app.state.session_service
    freeform_state = CompositionStateData(
        sources={
            "src": {
                "plugin": "csv",
                "on_success": "main",
                "options": {"path": "/tmp/convert-test-source.csv"},
                "on_validation_failure": "discard",
            }
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata_={"name": "My freeform pipeline", "description": ""},
        is_valid=False,
        validation_errors=None,
        composer_meta={},  # No guided_session key -> freeform.
    )
    asyncio.run(
        _save_composition_state_with_compose_authority(
            service,
            UUID(session_id),
            freeform_state,
            provenance="session_seed",
        )
    )


# ---------------------------------------------------------------------------
# Branch 3 — the conversion + the load-bearing recoverability promise
# ---------------------------------------------------------------------------


class TestConvertFreeformWithWork:
    def test_convert_reseeds_fresh_wizard(self, composer_test_client: TestClient) -> None:
        """A worked freeform session converts into a fresh Step-1 wizard."""
        client = composer_test_client
        session_id = _create_session(client)
        _seed_freeform_state_with_work(client, session_id)

        # Precondition: the bug — GET /guided rejects the freeform session.
        pre = client.get(f"/api/sessions/{session_id}/guided")
        assert pre.status_code == 400, pre.json()

        body = _convert(client, session_id)

        gs = body["guided_session"]
        assert gs is not None
        assert gs["step"] == "step_1_source"
        assert len(gs["history"]) == 1
        assert gs["history"][0]["response_hash"] is None
        assert gs["terminal"] is None
        assert body["terminal"] is None
        assert body["next_turn"] is not None
        loaded_turn = load_guided_json_payload(
            client.app.state.payload_store,
            payload_id=gs["history"][0]["payload_hash"],
            purpose="turn",
        )
        assert deep_thaw(loaded_turn.payload) == body["next_turn"]["payload"]
        # The fresh wizard replaced the freeform graph: the new version carries
        # no source.
        state = body["composition_state"]
        assert state is not None
        assert not state["sources"]

        # And the session is now readable as guided (the bug is gone).
        post = client.get(f"/api/sessions/{session_id}/guided")
        assert post.status_code == 200, post.json()

    def test_prior_freeform_pipeline_is_recoverable_via_version_history(self, composer_test_client: TestClient) -> None:
        """The load-bearing promise behind "fresh wizard + consent": the freeform
        pipeline the conversion set aside is fully recoverable.

        Convert -> the prior freeform version is still listed -> reverting to it
        restores the graph AND drops the session back to freeform (its
        composer_meta had no guided_session, and revert copies composer_meta
        verbatim). If this cannot go green the consent copy is a lie.
        """
        client = composer_test_client
        session_id = _create_session(client)
        _seed_freeform_state_with_work(client, session_id)

        versions_before = client.get(f"/api/sessions/{session_id}/state/versions")
        assert versions_before.status_code == 200, versions_before.json()
        freeform_version = next(v for v in versions_before.json() if v["sources"])
        assert freeform_version["sources"]["src"]["plugin"] == "csv"

        _convert(client, session_id)

        # Revert to the pre-conversion freeform version.
        revert = client.post(
            f"/api/sessions/{session_id}/state/revert",
            json={"operation_id": str(uuid4()), "state_id": freeform_version["id"]},
        )
        assert revert.status_code == 200, revert.json()
        restored = revert.json()
        assert restored["sources"]["src"]["plugin"] == "csv"

        # Reverting restored freeform: GET /guided 400s again.
        after = client.get(f"/api/sessions/{session_id}/guided")
        assert after.status_code == 400, after.json()

    def test_convert_records_recovery_breadcrumb_message(self, composer_test_client: TestClient) -> None:
        """The destructive-but-recoverable conversion leaves a durable trail.

        The closed ``provenance`` enum cannot distinguish the conversion from a
        fresh seed without a governance change, so the conscious audit choice is
        a system message (mirroring revert_state) that names the recoverable
        version.
        """
        client = composer_test_client
        session_id = _create_session(client)
        _seed_freeform_state_with_work(client, session_id)

        _convert(client, session_id)

        service = client.app.state.session_service
        messages = asyncio.run(service.get_messages(UUID(session_id)))
        system_msgs = [m for m in messages if m.role == "system"]
        assert any("guided" in m.content.lower() and "version" in m.content.lower() for m in system_msgs), (
            f"expected a system breadcrumb naming the recoverable version; got {[m.content for m in system_msgs]}"
        )

    def test_same_operation_replays_exactly_without_duplicate_state_or_message(self, composer_test_client: TestClient) -> None:
        client = composer_test_client
        session_id = _create_session(client)
        _seed_freeform_state_with_work(client, session_id)
        operation_id = str(uuid4())

        first = _convert_raw(client, session_id, operation_id=operation_id)
        replay = _convert_raw(client, session_id, operation_id=operation_id)

        assert first.status_code == 200, first.json()
        assert replay.status_code == 200, replay.json()
        assert replay.json() == first.json()
        versions = asyncio.run(client.app.state.session_service.get_state_versions(UUID(session_id)))
        assert [version.version for version in versions] == [1, 2]
        messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id)))
        assert len([message for message in messages if message.role == "system"]) == 1
        emissions = _guided_turn_emitted_args(client, session_id)
        assert len(emissions) == 1
        assert emissions[0]["payload_hash"] == first.json()["guided_session"]["history"][-1]["payload_hash"]
        assert emissions[0]["payload_payload_id"] == emissions[0]["payload_hash"]
        outcome = _convert_outcome(client, session_id, operation_id)
        assert str(outcome.result.state_id) == first.json()["composition_state"]["id"]

    def test_audit_insert_failure_rolls_back_conversion_state_and_breadcrumb(self, composer_test_client: TestClient) -> None:
        client = composer_test_client
        session_id = _create_session(client)
        _seed_freeform_state_with_work(client, session_id)
        service = client.app.state.session_service

        with patch.object(
            service,
            "_insert_prepared_guided_audit_rows_on_connection",
            side_effect=RuntimeError("injected audit insert failure"),
        ):
            response = _convert_raw(client, session_id)

        assert response.status_code == 500
        versions = asyncio.run(service.get_state_versions(UUID(session_id)))
        assert [version.version for version in versions] == [1]
        assert asyncio.run(service.get_messages(UUID(session_id), limit=None)) == []


# ---------------------------------------------------------------------------
# Branch 2 — an already-guided session refuses, it does not absorb the goal
# ---------------------------------------------------------------------------


class TestConvertAlreadyStarted:
    def test_convert_on_active_guided_refuses_instead_of_discarding_the_goal(self, composer_test_client: TestClient) -> None:
        """Branch 2 answers a coded 409 and changes nothing.

        Before goal-first this branch returned the existing session unchanged,
        which was harmless when convert carried no payload. Now it carries the
        author's goal, and "return the existing session" would mean accepting a
        goal and silently dropping it. The client's GET-first probe makes this
        reachable only in a cross-tab race, where the loser refetches.
        """

        client = composer_test_client
        session_id = _create_session(client, profile="tutorial")
        before = client.get(f"/api/sessions/{session_id}/guided")
        assert before.status_code == 200, before.json()

        response = _convert_raw(client, session_id)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"]["code"] == "guided_already_started"
        # Nothing moved: no new version, no goal row, and — because the branch
        # is classified before the reservation — no retry artefact either.
        after = client.get(f"/api/sessions/{session_id}/guided")
        assert after.status_code == 200
        assert after.json() == before.json()
        versions = asyncio.run(client.app.state.session_service.get_state_versions(UUID(session_id)))
        assert [version.version for version in versions] == [1]
        with client.app.state.session_engine.connect() as conn:
            assert (
                conn.execute(
                    select(guided_operations_table.c.operation_id).where(
                        guided_operations_table.c.session_id == session_id,
                        guided_operations_table.c.kind == "guided_convert",
                    )
                ).all()
                == []
            )
        user_messages = [
            message.content for message in asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
        ]
        assert _CONVERT_INTENT not in user_messages

    def test_convert_same_operation_id_still_replays_a_settled_conversion(self, composer_test_client: TestClient) -> None:
        """The 409 is for a NEW operation id; a retry of a settled one replays.

        The replay lookup runs before the branch, so a client that re-sends the
        same conversion after a dropped response still gets its own settled
        session rather than the conflict.
        """

        client = composer_test_client
        session_id = _create_session(client)
        _seed_freeform_state_with_work(client, session_id)
        operation_id = str(uuid4())

        first = _convert_raw(client, session_id, operation_id=operation_id)
        assert first.status_code == 200, first.json()
        replay = _convert_raw(client, session_id, operation_id=operation_id)

        assert replay.status_code == 200, replay.json()
        assert replay.json() == first.json()
        fresh = _convert_raw(client, session_id)
        assert fresh.status_code == 409, fresh.json()
        assert fresh.json()["detail"]["code"] == "guided_already_started"

    def test_convert_on_completed_session_also_refuses(self, composer_test_client: TestClient) -> None:
        """A terminal guided session is still an already-started one.

        ``enterGuided`` used to route completed / solver-exhausted /
        protocol-violation terminals through convert to re-read them. Reading a
        session is GET /guided's job; convert now only ever CREATES a rooted
        one, so a terminal session takes the same coded refusal as an active
        one and the client reads the terminal from GET.
        """
        client = composer_test_client
        session_id = _create_session(client, profile="tutorial")
        service = client.app.state.session_service
        current = asyncio.run(service.get_current_state(UUID(session_id)))
        assert current is not None
        from elspeth.web.sessions.routes._helpers import _state_from_record

        state = _state_from_record(current)
        assert state.guided_session is not None
        terminal_guided = replace(
            state.guided_session,
            terminal=TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml="pipeline: {}\n"),
        )
        state_data = state.to_dict()
        asyncio.run(
            _save_composition_state_with_compose_authority(
                service,
                UUID(session_id),
                CompositionStateData(
                    sources=state_data["sources"],
                    nodes=state_data["nodes"],
                    edges=state_data["edges"],
                    outputs=state_data["outputs"],
                    metadata_=state_data["metadata"],
                    is_valid=current.is_valid,
                    validation_errors=current.validation_errors,
                    composer_meta={"guided_session": terminal_guided.to_dict()},
                ),
                provenance="session_seed",
            )
        )

        response = _convert_raw(client, session_id)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"]["code"] == "guided_already_started"
        # The terminal is still readable where it belongs — on GET /guided,
        # which is what the client falls back to.
        read_back = client.get(f"/api/sessions/{session_id}/guided")
        assert read_back.status_code == 200, read_back.json()
        assert read_back.json()["terminal"]["kind"] == "completed"
        assert read_back.json()["next_turn"] is None


# ---------------------------------------------------------------------------
# Branch 1 — empty session persisted for immutable replay
# ---------------------------------------------------------------------------


class TestConvertEmptySession:
    def test_convert_on_empty_session_persists_retry_located_seed(self, composer_test_client: TestClient) -> None:
        """A brand-new session persists the schema-8 seed needed for replay."""
        client = composer_test_client
        session_id = _create_session(client)
        operation_id = str(uuid4())

        first = _convert_raw(client, session_id, operation_id=operation_id)
        replay = _convert_raw(client, session_id, operation_id=operation_id)

        assert first.status_code == 200, first.json()
        assert replay.status_code == 200, replay.json()
        assert replay.json() == first.json()
        body = first.json()
        assert body["guided_session"]["step"] == "step_1_source"
        assert body["composition_state"] is not None

        versions = client.get(f"/api/sessions/{session_id}/state/versions")
        assert versions.status_code == 200, versions.json()
        assert len(versions.json()) == 1
        outcome = _convert_outcome(client, session_id, operation_id)
        assert str(outcome.result.state_id) == body["composition_state"]["id"]

    def test_convert_requires_operation_id_and_intent_before_persisting(self, composer_test_client: TestClient) -> None:
        client = composer_test_client
        session_id = _create_session(client)

        missing_both = client.post(f"/api/sessions/{session_id}/guided/convert", json={})
        missing_intent = client.post(f"/api/sessions/{session_id}/guided/convert", json={"operation_id": str(uuid4())})
        blank_intent = client.post(
            f"/api/sessions/{session_id}/guided/convert",
            json={"operation_id": str(uuid4()), "intent": "   "},
        )

        assert missing_both.status_code == 422
        assert missing_intent.status_code == 422
        assert blank_intent.status_code == 422
        versions = client.get(f"/api/sessions/{session_id}/state/versions")
        assert versions.json() == []

    def test_convert_roots_the_session_in_the_goal_and_seeds_the_transcript(self, composer_test_client: TestClient) -> None:
        """The goal becomes a durable root row bound to the ``guided_convert`` operation.

        Everything lands in ONE transaction: the checkpoint that names the root,
        the ``route_user_message`` row itself, and the operation row's
        ``originating_message_id`` binding that the custody helper re-derives
        from. Without that binding the root row exists but no operation claims
        it, and the failure only surfaces much later, at proposal staging, as
        "absent or ambiguous start-operation authority".
        """

        client = composer_test_client
        session_id = _create_session(client)
        _seed_freeform_state_with_work(client, session_id)
        operation_id = str(uuid4())

        response = _convert_raw(client, session_id, operation_id=operation_id)

        assert response.status_code == 200, response.json()
        guided = response.json()["guided_session"]
        assert [(turn["role"], turn["content"], turn["seq"], turn["step"]) for turn in guided["chat_history"]] == [
            ("user", _CONVERT_INTENT, 0, "step_1_source"),
            ("assistant", GUIDED_GOAL_ACKNOWLEDGEMENT, 1, "step_1_source"),
        ]
        assert guided["chat_turn_seq"] == 2

        service = client.app.state.session_service
        messages = asyncio.run(service.get_messages(UUID(session_id), limit=None))
        roots = [message for message in messages if message.role == "user" and message.content == _CONVERT_INTENT]
        assert len(roots) == 1
        assert roots[0].writer_principal == "route_user_message"

        with client.app.state.session_engine.connect() as conn:
            rows = conn.execute(
                select(
                    guided_operations_table.c.kind,
                    guided_operations_table.c.status,
                    guided_operations_table.c.originating_message_id,
                ).where(guided_operations_table.c.session_id == session_id)
            ).all()
        assert [(row.kind, row.status, row.originating_message_id) for row in rows] == [("guided_convert", "completed", str(roots[0].id))]

        # The custody helper accepts a ``guided_convert`` root: it derives the
        # operation kind from the row, not from a hardcoded "guided_start".
        verified = asyncio.run(
            service.get_verified_guided_root_intent(
                session_id=UUID(session_id),
                root_message_id=roots[0].id,
            )
        )
        assert verified.content == _CONVERT_INTENT

    def test_root_custody_refuses_a_user_row_no_start_operation_claims(self, composer_test_client: TestClient) -> None:
        """Fail-closed: a plain user row is not a root intent just because it exists.

        The custody helper's authority is the completed ``guided_start`` /
        ``guided_convert`` row that NAMES the message, not the message itself.
        Widening the helper to accept two operation kinds must not weaken that:
        an ordinary chat row still has no start-operation authority.

        (Content drift is not testable from here — ``chat_messages.content`` is
        append-only at the database, so the hash re-derivation guards a row no
        writer can rewrite in place.)
        """

        client = composer_test_client
        session_id = _create_session(client)
        response = _convert_raw(client, session_id)
        assert response.status_code == 200, response.json()

        service = client.app.state.session_service
        unclaimed = asyncio.run(service.add_message(UUID(session_id), "user", "just a chat message", writer_principal="route_user_message"))

        with pytest.raises(AuditIntegrityError, match="absent or ambiguous start-operation authority"):
            asyncio.run(service.get_verified_guided_root_intent(session_id=UUID(session_id), root_message_id=unclaimed.id))

    def test_replay_uses_durable_turn_after_live_catalog_drift(self, composer_test_client: TestClient) -> None:
        """Completed conversion replay never rebuilds from mutable policy state."""
        client = composer_test_client
        session_id = _create_session(client)
        operation_id = str(uuid4())
        first = _convert_raw(client, session_id, operation_id=operation_id)
        assert first.status_code == 200, first.json()
        with patch(
            "elspeth.web.sessions.routes.composer.guided._build_get_guided_turn",
            side_effect=AssertionError("completed replay must not consult the live catalog"),
        ):
            replay = _convert_raw(client, session_id, operation_id=operation_id)

        assert replay.status_code == 200, replay.json()
        assert replay.json() == first.json()
        assert replay.json()["next_turn"]["turn_token"] == first.json()["next_turn"]["turn_token"]

    def test_deterministic_response_failure_is_settled_and_replayed(self, composer_test_client: TestClient) -> None:
        client = composer_test_client
        session_id = _create_session(client)
        operation_id = str(uuid4())

        with patch(
            "elspeth.web.sessions.routes.composer.guided._build_get_guided_turn",
            side_effect=InvariantError("tier-3 diagnostic must not escape"),
        ):
            first = _convert_raw(client, session_id, operation_id=operation_id)
        replay = _convert_raw(client, session_id, operation_id=operation_id)

        assert first.status_code == 500
        assert replay.status_code == 500
        assert (
            first.json()
            == replay.json()
            == {
                "detail": {
                    "error_type": "guided_operation_terminal_failure",
                    "failure_code": "operation_failed",
                    "detail": "The operation failed.",
                }
            }
        )
        assert "tier-3 diagnostic" not in first.text

    def test_audit_integrity_failure_is_settled_without_swallowing_diagnostic(
        self,
        composer_test_client: TestClient,
    ) -> None:
        client = composer_test_client
        session_id = _create_session(client)
        operation_id = str(uuid4())
        service = client.app.state.session_service

        from structlog.testing import capture_logs

        with (
            capture_logs() as cap_logs,
            patch.object(
                service,
                "save_state_for_guided_operation",
                side_effect=AuditIntegrityError("diagnostic retained for audit"),
            ),
        ):
            first = _convert_raw(client, session_id, operation_id=operation_id)

        replay = _convert_raw(client, session_id, operation_id=operation_id)
        assert first.status_code == replay.status_code == 500
        assert (
            first.json()
            == replay.json()
            == {
                "detail": {
                    "error_type": "guided_operation_terminal_failure",
                    "failure_code": "integrity_error",
                    "detail": "The operation failed an integrity check.",
                }
            }
        )
        events = [entry for entry in cap_logs if entry.get("event") == "guided.operation_terminal_failure"]
        assert len(events) == 1
        assert events[0]["exc_class"] == "AuditIntegrityError"
        assert events[0]["site"] == "post_guided_convert"
        assert events[0]["frames"]
        # R2-F16b: the correlation field is always emitted (None here — this
        # app carries no RequestIdMiddleware).
        assert "request_id" in events[0]
        assert "diagnostic retained for audit" not in repr(events[0])

    def test_unclassified_failure_is_recorded_with_its_failure_code_and_frames(
        self,
        composer_test_client: TestClient,
    ) -> None:
        """A first-party bug settles AND leaves a server-side record.

        The durable row and the replayable coded response say only THAT the
        operation failed. Before the diagnostic widened past the integrity
        arm, an unclassified defect discarded its traceback entirely, so the
        one 500 an operator had to work from named no site, no class and no
        frames. The response contract is deliberately unchanged — settlement
        succeeded, so the coded answer stays exactly replayable.
        """
        client = composer_test_client
        session_id = _create_session(client)
        operation_id = str(uuid4())

        from structlog.testing import capture_logs

        with (
            capture_logs() as cap_logs,
            patch(
                "elspeth.web.sessions.routes.composer.guided._build_get_guided_turn",
                side_effect=InvariantError("tier-3 diagnostic must not escape"),
            ),
        ):
            first = _convert_raw(client, session_id, operation_id=operation_id)

        assert first.status_code == 500
        assert first.json()["detail"]["failure_code"] == "operation_failed"
        events = [entry for entry in cap_logs if entry.get("event") == "guided.operation_terminal_failure"]
        assert len(events) == 1
        assert events[0]["exc_class"] == "InvariantError"
        assert events[0]["failure_code"] == "operation_failed"
        assert events[0]["site"] == "post_guided_convert"
        assert events[0]["frames"]
        # The widened log carries frames, never the exception's own text.
        assert "tier-3 diagnostic" not in repr(events[0])

    def test_settlement_conflict_is_not_logged_as_a_terminal_failure(
        self,
        composer_test_client: TestClient,
    ) -> None:
        """A settlement conflict stays out of the error log.

        Pins the one exclusion the widened diagnostic makes: a stale conflict
        is an expected concurrency outcome answered by its own 409 contract,
        so paging on it would bury the defects the widening exists to surface.
        """
        client = composer_test_client
        session_id = _create_session(client)

        from structlog.testing import capture_logs

        with (
            capture_logs() as cap_logs,
            patch.object(
                client.app.state.session_service,
                "save_state_for_guided_operation",
                side_effect=GuidedOperationSettlementConflictError(),
            ),
        ):
            response = _convert_raw(client, session_id, operation_id=str(uuid4()))

        assert response.status_code == 409
        assert response.json()["detail"]["failure_code"] == "stale_conflict"
        assert [entry for entry in cap_logs if entry.get("event") == "guided.operation_terminal_failure"] == []

    def test_losing_the_race_after_the_compose_lock_conflicts_instead_of_discarding_the_goal(
        self,
        composer_test_client: TestClient,
    ) -> None:
        """The post-lock loser gets 409, not a 200 carrying the winner's session.

        The pre-lock classification (409 ``guided_already_started``) is reachable
        only when the head is ALREADY guided when this request arrives. The other
        half of that contract — "returning it unchanged would silently discard
        the goal this request carries" — belongs to the post-lock re-read, where
        a competing start or convert won between classification and the compose
        lock. Every other test in this file seeds the guided state before the
        request and therefore exercises only the pre-lock branch, which is why a
        post-lock branch that settled and answered 200 could pass the whole
        suite while dropping the loser's intent on the floor.

        The race is made deterministic without a second thread by hiding the
        head from the probes that run BEFORE the operation row is reserved: the
        durable ``guided_convert`` row is the marker, so this keys on a fact the
        route itself creates rather than on a call count.

        ``post_guided_start`` may answer 200 in the same position because
        ``_verify_start_root`` first proves the durable root carries THIS
        request's intent. A conversion has no such equality, so it must conflict.
        """
        client = composer_test_client
        session_id = _create_session(client, profile="live")
        service = client.app.state.session_service
        engine = client.app.state.session_engine
        real_get_current_state = service.get_current_state

        async def _head_visible_only_after_reservation(observed_session_id):
            record = await real_get_current_state(observed_session_id)
            with engine.connect() as conn:
                reserved = (
                    conn.execute(
                        select(guided_operations_table.c.operation_id).where(
                            guided_operations_table.c.session_id == str(observed_session_id),
                            guided_operations_table.c.kind == "guided_convert",
                        )
                    ).first()
                    is not None
                )
            return record if reserved else None

        with patch.object(service, "get_current_state", new=_head_visible_only_after_reservation):
            response = _convert_raw(client, session_id)

        assert response.status_code == 409, response.json()
        assert response.json()["detail"]["failure_code"] == "stale_conflict"
        # The loser's goal never became anyone's durable root intent, and the
        # winner's rooted session is exactly as it was.
        messages = asyncio.run(service.get_messages(UUID(session_id), limit=None))
        assert [message for message in messages if message.content == _CONVERT_INTENT] == []
        surviving = client.get(f"/api/sessions/{session_id}/guided")
        assert surviving.status_code == 200, surviving.json()
        assert [turn["content"] for turn in surviving.json()["guided_session"]["chat_history"] if turn["role"] == "user"] == [_START_INTENT]

    def test_expected_head_conflict_is_terminal_409_and_exactly_replayed(self, composer_test_client: TestClient) -> None:
        client = composer_test_client
        session_id = _create_session(client)
        operation_id = str(uuid4())
        service = client.app.state.session_service

        with patch.object(
            service,
            "save_state_for_guided_operation",
            side_effect=GuidedOperationSettlementConflictError(),
        ):
            first = _convert_raw(client, session_id, operation_id=operation_id)
        replay = _convert_raw(client, session_id, operation_id=operation_id)

        assert first.status_code == replay.status_code == 409
        assert first.json() == replay.json()
        assert first.json()["detail"]["failure_code"] == "stale_conflict"

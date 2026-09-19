"""Workflow governance through the closed-local application and its real lifespan."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select, update

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.sessions.models import approvals_table, identities_table, identity_relationships_table, identity_roles_table, runs_table
from elspeth.web.sessions.protocol import CompositionStateData
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import _passthrough_composition_state, _save_composition_state_with_compose_authority

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@dataclass(frozen=True, slots=True)
class GovernedApp:
    client: AsyncClient
    app: FastAPI
    settings: WebSettings


@pytest_asyncio.fixture
async def governed_app(tmp_path: Path) -> AsyncIterator[GovernedApp]:
    for name in ("blobs", "outputs", "runs"):
        (tmp_path / name).mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        auth_provider="local",
        registration_mode="closed",
        workflow_governance="on",
        compartment_id="team-red",
        identity_dormancy_days=90,
        plugin_allowlist=("transform:passthrough",),
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app = create_app(settings=settings)
    with app.state.session_engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol", "dave", "erin", "anna", "bert", "cleo", "piet", "quin", "milo"):
            ensure_test_identity(conn, identity_id=identity_id)
        grants = (
            ("alice", "admin"),
            ("erin", "admin"),
            ("bob", "approver"),
            ("carol", "approver"),
            ("carol", "reviewer"),
            ("dave", "user"),
            ("anna", "approver"),
            ("bert", "approver"),
            ("cleo", "approver"),
            ("piet", "approver"),
            ("quin", "approver"),
        )
        for identity_id, role in grants:
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=str(uuid4()),
                    identity_id=identity_id,
                    role=role,
                    granted_by_identity_id="alice",
                    granted_at=datetime.now(UTC),
                )
            )
    for identity_id in ("alice", "bob", "carol", "dave", "milo"):
        app.state.auth_provider.create_user(identity_id, "password123", display_name=identity_id)
    async with (
        LifespanManager(app, startup_timeout=15, shutdown_timeout=30) as manager,
        AsyncClient(transport=ASGITransport(app=manager.app), base_url="http://test") as client,
    ):
        yield GovernedApp(client, app, settings)


async def _bearer(governed: GovernedApp, identity_id: str) -> dict[str, str]:
    response = await governed.client.post("/api/auth/login", json={"username": identity_id, "password": "password123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _state(governed: GovernedApp, alice: dict[str, str]) -> tuple[str, str]:
    created = await governed.client.post("/api/sessions", headers=alice, json={"title": "governed state"})
    assert created.status_code == 201, created.text
    session_id = UUID(created.json()["id"])
    blob_dir = governed.settings.data_dir / "blobs" / str(session_id)
    blob_dir.mkdir(parents=True, exist_ok=True)
    (governed.settings.data_dir / "outputs" / str(session_id)).mkdir(parents=True, exist_ok=True)
    (blob_dir / "audit_readiness_fixture.csv").write_text("id,name,value\n1,one,10\n", encoding="utf-8")
    state = _passthrough_composition_state(governed.settings.data_dir, session_id)
    payload = state.to_dict()
    record = await _save_composition_state_with_compose_authority(
        governed.app.state.session_service,
        session_id,
        CompositionStateData(
            sources=payload["sources"],
            nodes=payload["nodes"],
            edges=payload["edges"],
            outputs=payload["outputs"],
            metadata_=payload["metadata"],
            is_valid=True,
            validation_errors=None,
        ),
        provenance="session_seed",
    )
    return str(session_id), str(record.id)


async def _request(
    governed: GovernedApp,
    session_id: str,
    state_id: str,
    alice: dict[str, str],
    *,
    addressed_to: str = "bob",
) -> str:
    response = await governed.client.post(
        f"/api/sessions/{session_id}/approvals",
        headers=alice,
        json={"state_id": state_id, "approver_identity_id": addressed_to, "note": "please check this state"},
    )
    assert response.status_code == 201, response.text
    return response.json()["approval_id"]


def _runs(governed: GovernedApp, session_id: str) -> list[str]:
    with governed.app.state.session_engine.connect() as conn:
        return list(conn.execute(select(runs_table.c.id).where(runs_table.c.session_id == session_id)).scalars())


async def _edge(governed: GovernedApp, admin: dict[str, str], overseer: str, subject: str):
    return await governed.client.post(
        "/api/auth/admin/relationships",
        headers=admin,
        json={"from_identity_id": overseer, "to_identity_id": subject, "relationship_type": "approver"},
    )


async def _grant(governed: GovernedApp, admin: dict[str, str], identity_id: str, role: str):
    return await governed.client.post("/api/auth/admin/roles", headers=admin, json={"identity_id": identity_id, "role": role})


def _active_edges(governed: GovernedApp) -> set[tuple[str, str]]:
    with governed.app.state.session_engine.connect() as conn:
        rows = conn.execute(
            select(identity_relationships_table.c.from_identity_id, identity_relationships_table.c.to_identity_id).where(
                identity_relationships_table.c.revoked_at.is_(None)
            )
        ).all()
    return {(row.from_identity_id, row.to_identity_id) for row in rows}


async def test_non_addressed_approver_can_decide_and_admit_exact_run(governed_app: GovernedApp) -> None:
    alice = await _bearer(governed_app, "alice")
    bob = await _bearer(governed_app, "bob")
    carol = await _bearer(governed_app, "carol")
    dave = await _bearer(governed_app, "dave")
    session_id, state_id = await _state(governed_app, alice)

    unapproved = await governed_app.client.post(f"/api/sessions/{session_id}/execute", headers=alice)
    assert unapproved.status_code == 409, unapproved.text
    assert unapproved.json()["detail"]["error_type"] == "approval_required"
    assert _runs(governed_app, session_id) == []
    retained_root = governed_app.settings.data_dir / "retained-run-inputs"
    assert not retained_root.exists() or not any(retained_root.rglob("*"))

    approval_id = await _request(governed_app, session_id, state_id, alice)
    second_session_id, second_state_id = await _state(governed_app, alice)
    carol_addressed_id = await _request(governed_app, second_session_id, second_state_id, alice, addressed_to="carol")
    for approver, expected in ((bob, [approval_id, carol_addressed_id]), (carol, [carol_addressed_id, approval_id])):
        inbox = await governed_app.client.get("/api/workflow/mailbox/inbox", headers=approver)
        assert inbox.status_code == 200, inbox.text
        assert [row["approval_id"] for row in inbox.json()["approvals"]] == expected
    outsider = await governed_app.client.get("/api/workflow/mailbox/inbox", headers=dave)
    assert outsider.status_code == 200 and outsider.json()["approvals"] == []
    cannot_decide = await governed_app.client.post(f"/api/approvals/{approval_id}/decide", headers=dave, json={"decision": "approved"})
    assert cannot_decide.status_code == 404, cannot_decide.text
    assert cannot_decide.json()["detail"]["error_type"] == "approval_not_found"

    decided = await governed_app.client.post(
        f"/api/approvals/{approval_id}/decide",
        headers=carol,
        json={"decision": "approved", "note": "approved after checking the source"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decided_by_identity_id"] == "carol"
    summary = await governed_app.client.get("/api/workflow/mailbox/summary", headers=alice)
    assert summary.status_code == 200, summary.text
    assert summary.json()["decisions_unseen"] == 1
    sent = await governed_app.client.get("/api/workflow/mailbox/sent", headers=alice)
    assert sent.status_code == 200, sent.text
    decided_row = next(row for row in sent.json()["approvals"] if row["approval_id"] == approval_id)
    assert (decided_row["request_note"], decided_row["decision_note"], decided_row["decided_by_identity_id"]) == (
        "please check this state",
        "approved after checking the source",
        "carol",
    )
    seen = await governed_app.client.post(f"/api/workflow/mailbox/{approval_id}/seen", headers=alice)
    assert seen.status_code == 200, seen.text
    summary = await governed_app.client.get("/api/workflow/mailbox/summary", headers=alice)
    assert summary.json()["decisions_unseen"] == 0
    admitted = await governed_app.client.post(f"/api/sessions/{session_id}/execute", headers=alice)
    assert admitted.status_code == 202, admitted.text
    assert admitted.json()["run_id"] in _runs(governed_app, session_id)
    with LandscapeDB.from_url(governed_app.settings.landscape_url) as db, db.read_only_connection() as conn:
        events = conn.execute(
            select(auth_events_table).where(auth_events_table.c.event_type.in_(("approval_requested", "approval_decided")))
        ).all()
    approval_events = [
        (event.event_type, json.loads(event.metadata_json))
        for event in events
        if json.loads(event.metadata_json)["approval_id"] == approval_id
    ]
    assert len(approval_events) == 2
    assert {event_type for event_type, _metadata in approval_events} == {"approval_requested", "approval_decided"}
    assert all(metadata["compartment_id"] == "team-red" for _event_type, metadata in approval_events)


async def test_later_rejection_retires_approval_and_blocks_run(governed_app: GovernedApp) -> None:
    alice = await _bearer(governed_app, "alice")
    bob = await _bearer(governed_app, "bob")
    carol = await _bearer(governed_app, "carol")
    session_id, state_id = await _state(governed_app, alice)
    first_id = await _request(governed_app, session_id, state_id, alice)
    first = await governed_app.client.post(f"/api/approvals/{first_id}/decide", headers=bob, json={"decision": "approved"})
    assert first.status_code == 200, first.text
    second_id = await _request(governed_app, session_id, state_id, alice)
    rejected = await governed_app.client.post(
        f"/api/approvals/{second_id}/decide",
        headers=carol,
        json={"decision": "rejected", "note": "requires a new state"},
    )
    assert rejected.status_code == 200, rejected.text
    with governed_app.app.state.session_engine.connect() as conn:
        rows = conn.execute(select(approvals_table.c.approval_id, approvals_table.c.decision)).all()
    assert {row.approval_id: row.decision for row in rows} == {first_id: "superseded", second_id: "rejected"}

    blocked = await governed_app.client.post(f"/api/sessions/{session_id}/execute", headers=alice)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["error_type"] == "approval_required"
    assert _runs(governed_app, session_id) == []
    with LandscapeDB.from_url(governed_app.settings.landscape_url) as db, db.read_only_connection() as conn:
        events = conn.execute(select(auth_events_table).where(auth_events_table.c.event_type == "approval_decided")).all()
    decisions = [json.loads(event.metadata_json) for event in events]
    supersessions = [metadata for metadata in decisions if metadata["decision"] == "superseded"]
    assert len(supersessions) == 1, decisions
    assert supersessions[0]["cause"] == "later_rejection"
    assert supersessions[0]["trigger_approval_id"] == second_id
    assert supersessions[0]["compartment_id"] == "team-red"


async def test_review_attestation_requires_an_open_exact_state_request(governed_app: GovernedApp) -> None:
    alice = await _bearer(governed_app, "alice")
    carol = await _bearer(governed_app, "carol")
    session_id, state_id = await _state(governed_app, alice)
    requested = await governed_app.client.post(
        f"/api/sessions/{session_id}/reviews",
        headers=alice,
        json={"state_id": state_id, "reviewer_identity_id": "carol", "note": "review this version"},
    )
    assert requested.status_code == 201, requested.text
    request_id = requested.json()["request_id"]
    signed = await governed_app.client.post(f"/api/reviews/{request_id}/attest", headers=carol, json={"verdict": "signed_off"})
    assert signed.status_code == 201, signed.text
    assert signed.json()["state_id"] == state_id
    again = await governed_app.client.post(f"/api/reviews/{request_id}/attest", headers=carol, json={"verdict": "signed_off"})
    assert again.status_code in (404, 409), again.text


async def test_review_request_for_previous_state_cannot_authorize_new_state(governed_app: GovernedApp) -> None:
    alice = await _bearer(governed_app, "alice")
    carol = await _bearer(governed_app, "carol")
    session_id, old_state_id = await _state(governed_app, alice)
    requested = await governed_app.client.post(
        f"/api/sessions/{session_id}/reviews",
        headers=alice,
        json={"state_id": old_state_id, "reviewer_identity_id": "carol"},
    )
    assert requested.status_code == 201, requested.text

    state = _passthrough_composition_state(governed_app.settings.data_dir, UUID(session_id))
    payload = state.to_dict()
    new_state = await _save_composition_state_with_compose_authority(
        governed_app.app.state.session_service,
        UUID(session_id),
        CompositionStateData(
            sources=payload["sources"],
            nodes=payload["nodes"],
            edges=payload["edges"],
            outputs=payload["outputs"],
            metadata_=payload["metadata"],
            is_valid=True,
            validation_errors=None,
        ),
        provenance="session_seed",
    )
    assert str(new_state.id) != old_state_id
    cancelled = await governed_app.client.post(f"/api/reviews/{requested.json()['request_id']}/cancel", headers=alice)
    assert cancelled.status_code == 200, cancelled.text
    current_request = await governed_app.client.post(
        f"/api/sessions/{session_id}/reviews",
        headers=alice,
        json={"state_id": str(new_state.id), "reviewer_identity_id": "carol"},
    )
    assert current_request.status_code == 201, current_request.text
    stale = await governed_app.client.post(
        f"/api/reviews/{requested.json()['request_id']}/attest",
        headers=carol,
        json={"verdict": "signed_off"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["error_type"] == "open_review_request_required"
    current = await governed_app.client.post(
        f"/api/reviews/{current_request.json()['request_id']}/attest",
        headers=carol,
        json={"verdict": "signed_off"},
    )
    assert current.status_code == 201, current.text
    assert current.json()["state_id"] == str(new_state.id)


async def test_r7_cycle_refusal_follows_live_relationship_rows(governed_app: GovernedApp) -> None:
    admin = await _bearer(governed_app, "alice")
    first = await _edge(governed_app, admin, "anna", "bert")
    middle = await _edge(governed_app, admin, "bert", "cleo")
    assert first.status_code == middle.status_code == 201
    cycle = await _edge(governed_app, admin, "cleo", "anna")
    assert cycle.status_code == 409, cycle.text
    assert cycle.json()["detail"]["refusal"] == "relationship_cycle"
    assert _active_edges(governed_app) == {("anna", "bert"), ("bert", "cleo")}

    revoked = await governed_app.client.post(
        f"/api/auth/admin/relationships/{middle.json()['relationship_id']}/revoke",
        headers=admin,
        json={"note": "edge no longer applies"},
    )
    assert revoked.status_code == 200, revoked.text
    admitted = await _edge(governed_app, admin, "cleo", "anna")
    assert admitted.status_code == 201, admitted.text
    assert _active_edges(governed_app) == {("anna", "bert"), ("cleo", "anna")}


async def test_r7_unqualified_overseer_becomes_eligible_only_after_role_grant(governed_app: GovernedApp) -> None:
    admin = await _bearer(governed_app, "alice")
    refused = await _edge(governed_app, admin, "milo", "dave")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["refusal"] == "approver_role_required"
    assert _active_edges(governed_app) == set()
    granted = await _grant(governed_app, admin, "milo", "approver")
    assert granted.status_code == 201, granted.text
    admitted = await _edge(governed_app, admin, "milo", "dave")
    assert admitted.status_code == 201, admitted.text


def _seed_pre_existing_cycle(governed_app: GovernedApp) -> None:
    with governed_app.app.state.session_engine.begin() as conn:
        for relationship_id, overseer, subject in (
            ("seed-piet-quin", "piet", "quin"),
            ("seed-quin-piet", "quin", "piet"),
        ):
            conn.execute(
                insert(identity_relationships_table).values(
                    relationship_id=relationship_id,
                    from_identity_id=overseer,
                    to_identity_id=subject,
                    relationship_type="approver",
                    asserted_by_identity_id="alice",
                    asserted_at=datetime.now(UTC),
                )
            )


async def test_r7_seeded_cycle_refuses_the_next_insert(governed_app: GovernedApp) -> None:
    _seed_pre_existing_cycle(governed_app)
    admin = await _bearer(governed_app, "alice")
    cycle = await _edge(governed_app, admin, "piet", "milo")
    assert cycle.status_code == 409, cycle.text
    assert cycle.json()["detail"]["refusal"] == "relationship_cycle"
    assert _active_edges(governed_app) == {("piet", "quin"), ("quin", "piet")}


async def test_r7_seeded_cycle_is_refused_until_its_authority_row_is_revoked(governed_app: GovernedApp) -> None:
    _seed_pre_existing_cycle(governed_app)
    admin = await _bearer(governed_app, "alice")
    cycle = await _edge(governed_app, admin, "piet", "milo")
    assert cycle.status_code == 409, cycle.text
    assert cycle.json()["detail"]["refusal"] == "relationship_cycle"
    with governed_app.app.state.session_engine.begin() as conn:
        changed = conn.execute(
            update(identity_relationships_table)
            .where(identity_relationships_table.c.relationship_id == "seed-quin-piet")
            .values(revoked_at=datetime.now(UTC), revoked_by_identity_id="alice")
        )
    assert changed.rowcount == 1
    admitted = await _edge(governed_app, admin, "piet", "milo")
    assert admitted.status_code == 201, admitted.text


async def test_r8_admin_conflict_derives_from_live_workload_role(governed_app: GovernedApp) -> None:
    admin = await _bearer(governed_app, "alice")
    refused = await _grant(governed_app, admin, "dave", "admin")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["refusal"] == "role_forbidden_for_identity"
    with governed_app.app.state.session_engine.begin() as conn:
        changed = conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.identity_id == "dave", identity_roles_table.c.role == "user")
            .values(revoked_at=datetime.now(UTC))
        )
    assert changed.rowcount == 1
    admitted = await _grant(governed_app, admin, "dave", "admin")
    assert admitted.status_code == 201, admitted.text


async def test_r8_workload_conflict_derives_from_live_admin_role(governed_app: GovernedApp) -> None:
    admin = await _bearer(governed_app, "alice")
    refused = await _grant(governed_app, admin, "erin", "approver")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["refusal"] == "role_forbidden_for_identity"
    with governed_app.app.state.session_engine.begin() as conn:
        changed = conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.identity_id == "erin", identity_roles_table.c.role == "admin")
            .values(revoked_at=datetime.now(UTC))
        )
    assert changed.rowcount == 1
    admitted = await _grant(governed_app, admin, "erin", "approver")
    assert admitted.status_code == 201, admitted.text


async def test_r9_local_login_dormancy_depends_on_stored_window(governed_app: GovernedApp) -> None:
    with governed_app.app.state.session_engine.begin() as conn:
        for identity_id, days in (("dave", 91), ("milo", 89)):
            stamped = datetime.now(UTC) - timedelta(days=days)
            changed = conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == identity_id)
                .values(last_login_at=stamped, activated_at=stamped)
            )
            assert changed.rowcount == 1
    dormant = await governed_app.client.post("/api/auth/login", json={"username": "dave", "password": "password123"})
    assert dormant.status_code == 401, dormant.text
    admitted = await governed_app.client.post("/api/auth/login", json={"username": "milo", "password": "password123"})
    assert admitted.status_code == 200, admitted.text
    with governed_app.app.state.session_engine.connect() as conn:
        rows = conn.execute(
            select(identities_table.c.identity_id, identities_table.c.access_state).where(
                identities_table.c.identity_id.in_(("dave", "milo"))
            )
        ).all()
    assert {row.identity_id: row.access_state for row in rows} == {"dave": "pending", "milo": "active"}

"""The library HTTP surface over real session and library authorities."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, update

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.library_authority import RepositoryLibraryAuthority
from elspeth.web.sessions.models import identities_table, identity_roles_table
from elspeth.web.sessions.protocol import SessionNotFoundError, SessionRecord
from elspeth.web.sessions.routes.workflow.library import create_library_router
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import _seed_session_with_state
from tests.unit.web._sync_asgi_client import SyncASGITestClient


@dataclass
class _Actor:
    identity_id: str = "alice"


@pytest.fixture
def library_client(closed_local_app: SyncASGITestClient, tmp_path: Path) -> tuple[SyncASGITestClient, _Actor, list[str]]:
    client = closed_local_app
    actor = _Actor()
    events: list[str] = []
    audit_calls: list[dict[str, object]] = []

    async def current_user() -> UserIdentity:
        return UserIdentity(user_id=actor.identity_id, username=actor.identity_id)

    def record_published(_request: object, **fields: object) -> None:
        events.append("published")
        audit_calls.append(fields)

    def record_curated(_request: object, **fields: object) -> None:
        events.append(str(fields["action"]))
        audit_calls.append(fields)

    client.app.dependency_overrides[get_current_user] = current_user
    client.app.state.library_audit_calls = audit_calls
    client.app.state.auth_audit_recorder = SimpleNamespace(
        record_library_published=record_published,
        record_library_accepted=lambda request, **fields: record_curated(request, action="accepted", **fields),
        record_library_rejected=lambda request, **fields: record_curated(request, action="rejected", **fields),
        record_library_deprecated=lambda request, **fields: record_curated(request, action="deprecated", **fields),
        record_library_recalled=lambda request, **fields: record_curated(request, action="recalled", **fields),
    )
    engine = client.app.state.session_engine
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id in ("alice", "bob"):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=str(uuid4()),
                    identity_id=identity_id,
                    role="user",
                    expires_at=None,
                    note="fixture",
                    scope=None,
                    granted_by_identity_id=identity_id,
                    granted_at=datetime.now(UTC),
                    revoked_at=None,
                )
            )
        conn.execute(
            insert(identity_roles_table).values(
                role_id=str(uuid4()),
                identity_id="carol",
                role="curator",
                expires_at=None,
                note="fixture",
                scope=None,
                granted_by_identity_id="carol",
                granted_at=datetime.now(UTC),
                revoked_at=None,
            )
        )
    client.app.state.identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=lambda _token, _event: None)
    client.app.state.library_authority = RepositoryLibraryAuthority(
        engine, payload_store=FilesystemPayloadStore(tmp_path / "library-payloads")
    )
    client.app.include_router(create_library_router())
    return client, actor, events


def _publish(client: SyncASGITestClient, *, owner: str = "alice") -> tuple[UUID, dict[str, object]]:
    session_id = _seed_session_with_state(client, user_id=owner)
    published = client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "shared pipeline"})
    assert published.status_code == 201, published.text
    return session_id, published.json()


def test_publish_curate_and_browse_hides_source_session(library_client: tuple[SyncASGITestClient, _Actor, list[str]]) -> None:
    client, actor, events = library_client
    session_id, published = _publish(client)
    assert (published["state"], published["version"], published["compartment_id"]) == ("pending", 1, "test-compartment")
    assert published["published_from_session_id"] == str(session_id)

    actor.identity_id = "bob"
    assert client.get("/api/library").json()["entries"] == []
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "stolen"}).status_code == 404
    assert client.get("/api/library", params={"view": "queue"}).status_code == 404
    assert client.post(f"/api/library/{published['entry_id']}/accept", json={}).status_code == 404

    actor.identity_id = "carol"
    queue = client.get("/api/library", params={"view": "queue"})
    assert [entry["entry_id"] for entry in queue.json()["entries"]] == [published["entry_id"]]
    accepted = client.post(f"/api/library/{published['entry_id']}/accept", json={"note": "reviewed"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "accepted"

    actor.identity_id = "bob"
    browse = client.get("/api/library").json()["entries"]
    assert [entry["entry_id"] for entry in browse] == [published["entry_id"]]
    assert browse[0]["published_from_session_id"] is None
    assert browse[0]["payload_digest"] == published["payload_digest"]
    assert events == ["published", "accepted"]
    assert [call["entry_compartment_id"] for call in client.app.state.library_audit_calls] == ["test-compartment"] * 2


def test_rejection_blocks_fork_and_requires_note(library_client: tuple[SyncASGITestClient, _Actor, list[str]]) -> None:
    client, actor, _events = library_client
    _session_id, published = _publish(client)
    entry_id = published["entry_id"]
    actor.identity_id = "carol"
    blank = client.post(f"/api/library/{entry_id}/reject", json={})
    assert (blank.status_code, blank.json()["detail"]["error_type"]) == (409, "library_rejection_note_required")
    too_long = client.post(f"/api/library/{entry_id}/reject", json={"note": "x" * 4097})
    assert (too_long.status_code, too_long.json()["detail"]["error_type"]) == (409, "library_note_too_long")
    rejected = client.post(f"/api/library/{entry_id}/reject", json={"note": "needs changes"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "rejected"
    again = client.post(f"/api/library/{entry_id}/accept", json={})
    assert (again.status_code, again.json()["detail"]["error_type"], again.json()["detail"]["current_state"]) == (
        409,
        "library_entry_already_curated",
        "rejected",
    )
    actor.identity_id = "bob"
    refused = client.post(f"/api/library/{entry_id}/fork")
    assert (refused.status_code, refused.json()["detail"]["error_type"]) == (409, "library_entry_not_forkable")


def test_disabled_curator_cannot_read_queue(library_client: tuple[SyncASGITestClient, _Actor, list[str]]) -> None:
    client, actor, _events = library_client
    _publish(client)
    actor.identity_id = "carol"
    assert client.get("/api/library", params={"view": "queue"}).status_code == 200
    with client.app.state.session_engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == "carol")
            .values(access_state="disabled", disabled_at=datetime.now(UTC))
        )
    assert client.get("/api/library", params={"view": "queue"}).status_code == 404


def test_publish_waits_for_a_live_state_writer_instead_of_snapshotting_stale_head(
    library_client: tuple[SyncASGITestClient, _Actor, list[str]],
) -> None:
    client, _actor, events = library_client
    session_id = _seed_session_with_state(client, user_id="alice")
    service = client.app.state.session_service
    writer = service.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    try:
        refused = client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "stale candidate"})
    finally:
        service.session_operation_authority.release(writer)
    assert (refused.status_code, refused.json()["detail"]["error_type"]) == (409, "session_operation_conflict")
    assert events == []
    assert client.get("/api/library", params={"view": "mine"}).json()["entries"] == []
    published = client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "fresh candidate"})
    assert published.status_code == 201, published.text


def test_publish_without_compartment_returns_named_refusal(
    library_client: tuple[SyncASGITestClient, _Actor, list[str]],
) -> None:
    client, _actor, events = library_client
    session_id = _seed_session_with_state(client, user_id="alice")
    client.app.state.settings = client.app.state.settings.model_copy(update={"compartment_id": None})
    refused = client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "unmarked"})
    assert (refused.status_code, refused.json()["detail"]["error_type"]) == (409, "compartment_not_configured")
    assert events == []


def test_fork_passes_exact_frozen_yaml_and_provenance_to_seed_seam(
    library_client: tuple[SyncASGITestClient, _Actor, list[str]],
) -> None:
    client, actor, _events = library_client
    source_session_id, published = _publish(client)
    actor.identity_id = "carol"
    accepted = client.post(f"/api/library/{published['entry_id']}/accept", json={})
    assert accepted.status_code == 200, accepted.text
    actor.identity_id = "bob"
    received: list[dict[str, object]] = []

    async def seed(**kwargs: object) -> SimpleNamespace:
        received.append(kwargs)
        return SimpleNamespace(id="seeded-state-id")

    client.app.state.library_state_seeder = seed
    forked = client.post(f"/api/library/{published['entry_id']}/fork")
    assert forked.status_code == 201, forked.text
    new_session = asyncio.run(client.app.state.session_service.get_session(UUID(forked.json()["session_id"])))
    assert new_session.user_id == "bob" and new_session.forked_from_session_id is None
    assert forked.json()["state_id"] == "seeded-state-id"
    assert len(received) == 1
    body = received[0]["body"]
    assert hashlib.sha256(body.yaml.encode("utf-8")).hexdigest() == published["payload_digest"]
    assert received[0]["session"].id == new_session.id
    assert received[0]["composer_meta_updates"] == {
        "library_fork": {
            "entry_id": published["entry_id"],
            "payload_digest": published["payload_digest"],
            "published_from_session_id": str(source_session_id),
            "compartment_id": "test-compartment",
            "version": 1,
        }
    }


def test_fork_archives_new_session_if_seed_raises_internal_error(
    library_client: tuple[SyncASGITestClient, _Actor, list[str]],
) -> None:
    client, actor, _events = library_client
    _session_id, published = _publish(client)
    actor.identity_id = "carol"
    assert client.post(f"/api/library/{published['entry_id']}/accept", json={}).status_code == 200
    actor.identity_id = "bob"
    before = client.get("/api/sessions").json()
    created: list[UUID] = []

    async def failing_seed(**kwargs: object) -> None:
        session = kwargs["session"]
        assert isinstance(session, SessionRecord)
        created.append(session.id)
        raise RuntimeError("seed failed after session creation")

    client.app.state.library_state_seeder = failing_seed
    with pytest.raises(RuntimeError, match="seed failed after session creation"):
        client.post(f"/api/library/{published['entry_id']}/fork")
    assert len(created) == 1
    with pytest.raises(SessionNotFoundError):
        asyncio.run(client.app.state.session_service.get_session(created[0]))
    assert client.get("/api/sessions").json() == before


def test_recall_committed_before_fork_authorization_refuses(
    library_client: tuple[SyncASGITestClient, _Actor, list[str]],
) -> None:
    client, actor, _events = library_client
    _session_id, published = _publish(client)
    actor.identity_id = "carol"
    assert client.post(f"/api/library/{published['entry_id']}/accept", json={}).status_code == 200
    assert client.post(f"/api/library/{published['entry_id']}/recall", json={}).status_code == 200
    actor.identity_id = "bob"
    refused = client.post(f"/api/library/{published['entry_id']}/fork")
    assert (refused.status_code, refused.json()["detail"]["error_type"], refused.json()["detail"]["current_state"]) == (
        409,
        "library_entry_not_forkable",
        "recalled",
    )


@pytest.mark.parametrize("ineligible", ["revoked_user", "disabled", "wrong_provider"])
def test_fork_requires_a_live_same_provider_user(
    library_client: tuple[SyncASGITestClient, _Actor, list[str]],
    ineligible: str,
) -> None:
    client, actor, _events = library_client
    _session_id, published = _publish(client)
    actor.identity_id = "carol"
    assert client.post(f"/api/library/{published['entry_id']}/accept", json={}).status_code == 200
    engine = client.app.state.session_engine
    with engine.begin() as conn:
        if ineligible == "revoked_user":
            conn.execute(
                update(identity_roles_table)
                .where(identity_roles_table.c.identity_id == "bob", identity_roles_table.c.role == "user")
                .values(revoked_at=datetime.now(UTC))
            )
        elif ineligible == "disabled":
            conn.execute(
                update(identities_table)
                .where(identities_table.c.identity_id == "bob")
                .values(access_state="disabled", disabled_at=datetime.now(UTC))
            )
        else:
            conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(provider="google"))
    actor.identity_id = "bob"
    before = client.get("/api/sessions").json()
    refused = client.post(f"/api/library/{published['entry_id']}/fork")
    assert (refused.status_code, refused.json()["detail"]["error_type"]) == (409, "library_forker_not_active")
    assert client.get("/api/sessions").json() == before


def test_fork_without_seed_wiring_refuses_before_session_creation(
    library_client: tuple[SyncASGITestClient, _Actor, list[str]],
) -> None:
    client, actor, _events = library_client
    _session_id, published = _publish(client)
    actor.identity_id = "carol"
    assert client.post(f"/api/library/{published['entry_id']}/accept", json={}).status_code == 200
    actor.identity_id = "bob"
    before = client.get("/api/sessions").json()
    refused = client.post(f"/api/library/{published['entry_id']}/fork")
    assert (refused.status_code, refused.json()["detail"]["error_type"]) == (503, "library_seed_unavailable")
    assert client.get("/api/sessions").json() == before


def test_governance_off_refuses_every_library_route(library_client: tuple[SyncASGITestClient, _Actor, list[str]]) -> None:
    client, _actor, _events = library_client
    session_id = _seed_session_with_state(client, user_id="alice")
    client.app.state.settings = client.app.state.settings.model_copy(update={"workflow_governance": "off"})
    calls = (
        client.post(f"/api/sessions/{session_id}/library/publish", json={"title": "t"}),
        client.get("/api/library"),
        client.post("/api/library/missing/accept", json={}),
        client.post("/api/library/missing/fork"),
    )
    assert [(call.status_code, call.json()["detail"]["error_type"]) for call in calls] == [(409, "workflow_governance_off")] * len(calls)

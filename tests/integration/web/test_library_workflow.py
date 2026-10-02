"""Library publish, curation and fork on the real app wiring and audit store."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import insert, select

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web.app import create_app
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.sessions.models import identity_roles_table
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import _lifespan_test_client, _seed_session_with_state


@dataclass
class _Actor:
    identity_id: str = "alice"


def test_library_round_trip_preserves_frozen_provenance_and_audit(tmp_path: Path) -> None:
    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        composer_boot_probe_enabled=False,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough",),
        registration_mode="closed",
        workflow_governance="on",
        compartment_id="local-team",
    )
    app = create_app(settings=settings)
    actor = _Actor()

    async def current_user() -> UserIdentity:
        return UserIdentity(user_id=actor.identity_id, username=actor.identity_id)

    app.dependency_overrides[get_current_user] = current_user
    with _lifespan_test_client(app) as client:
        with client.app.state.session_engine.begin() as conn:
            for identity_id in ("alice", "bob", "carol"):
                ensure_test_identity(conn, identity_id=identity_id)
            for identity_id, role in (("alice", "user"), ("bob", "user"), ("carol", "curator")):
                conn.execute(
                    insert(identity_roles_table).values(
                        role_id=str(uuid4()),
                        identity_id=identity_id,
                        role=role,
                        expires_at=None,
                        note="fixture",
                        scope=None,
                        granted_by_identity_id=identity_id,
                        granted_at=datetime.now(UTC),
                        revoked_at=None,
                    )
                )

        source_session_id = _seed_session_with_state(client, user_id="alice")
        published = client.post(f"/api/sessions/{source_session_id}/library/publish", json={"title": "shared pipeline"})
        assert published.status_code == 201, published.text
        entry = published.json()
        assert (entry["state"], entry["compartment_id"]) == ("pending", "local-team")

        actor.identity_id = "carol"
        accepted = client.post(f"/api/library/{entry['entry_id']}/accept", json={"note": "reviewed"})
        assert accepted.status_code == 200, accepted.text

        actor.identity_id = "bob"
        browse = client.get("/api/library")
        assert browse.status_code == 200, browse.text
        assert browse.json()["entries"][0]["published_from_session_id"] is None
        assert client.get(f"/api/sessions/{source_session_id}").status_code == 404

        forked = client.post(f"/api/library/{entry['entry_id']}/fork")
        assert forked.status_code == 201, forked.text
        fork_session_id = UUID(forked.json()["session_id"])
        fork_session = asyncio.run(client.app.state.session_service.get_session(fork_session_id))
        assert fork_session.user_id == "bob" and fork_session.forked_from_session_id is None
        state = asyncio.run(client.app.state.session_service.get_current_state(fork_session_id))
        assert state is not None and state.composer_meta is not None
        assert forked.json()["state_id"] == str(state.id)
        assert deep_thaw(state.composer_meta["library_fork"]) == {
            "entry_id": entry["entry_id"],
            "payload_digest": entry["payload_digest"],
            "published_from_session_id": str(source_session_id),
            "compartment_id": "local-team",
            "version": 1,
        }
        assert deep_thaw(state.composer_meta["ingress"]) == {
            "text_sha256": entry["payload_digest"],
            "foreign_compartment_ids": [],
        }

        with LandscapeDB.from_url(settings.landscape_url) as db, db.read_only_connection() as conn:
            rows = conn.execute(
                select(auth_events_table).where(auth_events_table.c.event_type.in_(("library_published", "library_accepted")))
            ).all()
        assert {row.event_type for row in rows} == {"library_published", "library_accepted"}
        assert {row.identity_id for row in rows} == {"alice"}
        for row in rows:
            metadata = json.loads(row.metadata_json)
            assert metadata["entry_compartment_id"] == "local-team"
            assert metadata["payload_digest"] == entry["payload_digest"]

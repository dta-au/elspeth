"""Integration tests for /api/sessions/{sid}/{mark-ready-for-review,shareable-link}
and /api/sessions/shared/{token} routes.

Phase 6A Task 6 (UX redesign 2026-05). Reuses the audit-readiness test
harness because the same ``_passthrough_composition_state`` fixture produces
a valid composition that satisfies the mark-time gate.

Fixtures live in ``tests/integration/web/conftest.py``:
    * ``audit_readiness_test_client`` — full app, alice authenticated.
    * ``audit_readiness_client_with_state`` — also seeds a passthrough state.
    * ``audit_readiness_client_anonymous`` — auth raises 401.
    * ``audit_readiness_other_user_session_id`` — session owned by bob.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from elspeth.core.canonical import canonical_json
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.sessions.models import blobs_table, chat_messages_table, composer_completion_events_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.shareable_reviews.signer import ShareTokenPayload
from tests.fixtures.identities import ensure_test_identity

from .conftest import (
    _TEST_AUTHED_USER_ID,
    _passthrough_composition_state,
    _save_composition_state_with_compose_authority,
)

# ── POST /mark-ready-for-review ─────────────────────────────────────────


def test_mark_ready_for_review_happy_path(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["token"], str) and body["token"]
    assert body["share_url"].endswith(body["token"])
    assert body["payload_digest"].startswith("sha256:")


def test_mark_ready_for_review_records_audit_row(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """A `mark_ready_for_review` row lands in ``composer_completion_events_table``."""
    client, session_id = audit_readiness_client_with_state
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert response.status_code == 200
    digest = response.json()["payload_digest"]
    engine = client.app.state.session_engine
    with engine.connect() as conn:
        rows = conn.execute(
            select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session_id))
        ).all()
    assert len(rows) == 1
    assert rows[0].event_type == "mark_ready_for_review"
    assert rows[0].payload_digest == digest
    assert rows[0].actor == "alice"


def test_mark_ready_for_review_requires_auth(
    audit_readiness_client_anonymous: TestClient,
) -> None:
    any_session_id = uuid.uuid4()
    response = audit_readiness_client_anonymous.post(f"/api/sessions/{any_session_id}/mark-ready-for-review")
    assert response.status_code == 401


def test_mark_ready_for_review_idor_returns_404(
    audit_readiness_test_client: TestClient,
    audit_readiness_other_user_session_id: UUID,
) -> None:
    """alice posts against bob's session → 404 (byte-identical to "not found")."""
    response = audit_readiness_test_client.post(f"/api/sessions/{audit_readiness_other_user_session_id}/mark-ready-for-review")
    assert response.status_code == 404


def test_mark_ready_for_review_no_state_returns_409(
    audit_readiness_client_without_state: tuple[TestClient, UUID],
) -> None:
    """Session exists but has no composition state — validation gate fires 409."""
    client, session_id = audit_readiness_client_without_state
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    # validate() returns is_valid=False for "no state" → CompositionNotRunnableError → 409.
    assert response.status_code == 409


def _seed_session_with_blob_subtree_sink(client: TestClient, *, user_id: str) -> UUID:
    """Seed a session whose sink writes into the session's OWN blob subtree.

    Mirrors ``conftest._seed_session_with_state`` but anchors the sink path
    at ``data_dir/blobs/<session_id>/...`` — a target the session-scoped
    sink allowlist (elspeth-bdc17cfdb1) only admits when the validator
    receives the caller's session id. The path depends on the session id,
    so the state is built after ``create_session``.
    """
    session_service = client.app.state.session_service
    settings = client.app.state.settings
    (settings.data_dir / "outputs").mkdir(parents=True, exist_ok=True)

    async def _seed() -> UUID:
        with client.app.state.session_engine.begin() as conn:
            ensure_test_identity(conn, identity_id=user_id, provider=settings.auth_provider)
        record = await session_service.create_session(
            user_id=user_id,
            title="blob-subtree sink fixture",
            auth_provider_type=settings.auth_provider,
        )
        (settings.data_dir / "blobs" / str(record.id)).mkdir(parents=True, exist_ok=True)
        state_d = _passthrough_composition_state(settings.data_dir, record.id).to_dict()
        state_d["outputs"][0]["options"]["path"] = str(settings.data_dir / "blobs" / str(record.id) / "review_out.csv")
        await _save_composition_state_with_compose_authority(
            session_service,
            record.id,
            CompositionStateData(
                sources=state_d["sources"],
                nodes=state_d["nodes"],
                edges=state_d["edges"],
                outputs=state_d["outputs"],
                metadata_=state_d["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
        return record.id

    return asyncio.run(_seed())


def test_mark_ready_for_review_allows_own_session_blob_sink(
    audit_readiness_test_client: TestClient,
) -> None:
    """A sink targeting the session's own ``blobs/<session_id>/`` subtree
    must pass the mark-time validation gate exactly as it passes /validate:
    ``mark_ready_for_review`` threads the session id into ``validate_state``
    so the session-scoped sink allowlist includes the caller's own subtree.
    """
    client = audit_readiness_test_client
    session_id = _seed_session_with_blob_subtree_sink(client, user_id=_TEST_AUTHED_USER_ID)
    # Parity guard: the same state is valid through /validate...
    validate_response = client.post(f"/api/sessions/{session_id}/validate")
    assert validate_response.status_code == 200, validate_response.text
    assert validate_response.json()["is_valid"] is True, validate_response.text
    # ...so the share gate must agree.
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert response.status_code == 200, response.text


# ── GET /shareable-link ─────────────────────────────────────────────────


def test_get_shareable_link_remints_stable_digest(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """Two GETs yield identical payload_digest but different token strings."""
    client, session_id = audit_readiness_client_with_state
    mark_response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert mark_response.status_code == 200, mark_response.text

    r1 = client.get(f"/api/sessions/{session_id}/shareable-link")
    r2 = client.get(f"/api/sessions/{session_id}/shareable-link")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["payload_digest"] == r2.json()["payload_digest"]
    assert r1.json()["token"] != r2.json()["token"]


def test_get_shareable_link_requires_auth(
    audit_readiness_client_anonymous: TestClient,
) -> None:
    any_session_id = uuid.uuid4()
    response = audit_readiness_client_anonymous.get(f"/api/sessions/{any_session_id}/shareable-link")
    assert response.status_code == 401


def test_get_shareable_link_idor_returns_404(
    audit_readiness_test_client: TestClient,
    audit_readiness_other_user_session_id: UUID,
) -> None:
    response = audit_readiness_test_client.get(f"/api/sessions/{audit_readiness_other_user_session_id}/shareable-link")
    assert response.status_code == 404


# ── GET /sessions/shared/{token} ────────────────────────────────────────


def _mint_token(client: TestClient, session_id: UUID) -> str:
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_get_shared_inspect_happy_path(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    token = _mint_token(client, session_id)
    response = client.get(f"/api/sessions/shared/{token}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == str(session_id)
    assert body["yaml"]
    # audit_readiness is the six-row snapshot, served from the frozen blob.
    assert {row["id"] for row in body["audit_readiness"]["rows"]} == {
        "validation",
        "plugin_trust",
        "provenance",
        "retention",
        "llm_interpretations",
        "secrets",
    }


def _seed_session_with_described_state(client: TestClient, *, user_id: str) -> UUID:
    """The conftest passthrough fixture with an authored description on the
    source, the node and the sink — the shape the freeform tool surface
    instructs the planner to produce on every step (``_STEP_DESCRIPTION_
    DESCRIPTION``, tools/_common.py) and which the share mirror did not
    declare from 2026-08-15 until elspeth-989d369d82."""
    session_service = client.app.state.session_service
    settings = client.app.state.settings

    async def _seed() -> UUID:
        with client.app.state.session_engine.begin() as conn:
            ensure_test_identity(conn, identity_id=user_id, provider=settings.auth_provider)
        record = await session_service.create_session(
            user_id=user_id,
            title="described fixture",
            auth_provider_type=settings.auth_provider,
        )
        (settings.data_dir / "blobs" / str(record.id)).mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "outputs" / str(record.id)).mkdir(parents=True, exist_ok=True)
        base = _passthrough_composition_state(settings.data_dir, record.id)
        state = replace(
            base,
            sources={name: replace(source, description="Orders export") for name, source in base.sources.items()},
            nodes=tuple(replace(node, description="Pass every row through unchanged") for node in base.nodes),
            outputs=tuple(replace(output, description="Rows as received") for output in base.outputs),
        )
        state_d = state.to_dict()
        await _save_composition_state_with_compose_authority(
            session_service,
            record.id,
            CompositionStateData(
                sources=state_d["sources"],
                nodes=state_d["nodes"],
                edges=state_d["edges"],
                outputs=state_d["outputs"],
                metadata_=state_d["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
        return record.id

    return asyncio.run(_seed())


def test_get_shared_inspect_resolves_a_pipeline_with_authored_step_descriptions(
    audit_readiness_test_client: TestClient,
) -> None:
    """elspeth-989d369d82, the collector-free arm, end to end through the real
    stack: persistence round-trip (``state_from_record`` keeps every key),
    the mark-ready gate (200, token minted — the owner never runs the strict
    mirror, so they got no signal), then resolve. Before the fix the resolve
    was a bare 500 for the recipient; the mirror must now carry the prose."""
    client = audit_readiness_test_client
    session_id = _seed_session_with_described_state(client, user_id=_TEST_AUTHED_USER_ID)
    token = _mint_token(client, session_id)

    response = client.get(f"/api/sessions/shared/{token}")

    assert response.status_code == 200, response.text
    snapshot = response.json()["composition_snapshot"]
    assert snapshot["sources"]["source"]["description"] == "Orders export"
    assert [node["description"] for node in snapshot["nodes"]] == ["Pass every row through unchanged"]
    assert [output["description"] for output in snapshot["outputs"]] == ["Rows as received"]


def test_get_shared_inspect_attributes_the_share_to_the_username(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """The route must send the sharer's USERNAME, not only the identity id.

    The shared view's banner names who shared the pipeline. Its recipient
    has no login, so the opaque ``created_by_user_id`` names nobody to
    them. The authenticated identity here carries a username distinct from
    its user_id precisely so a route that passed ``user.user_id`` for both
    would fail this test.
    """
    client, session_id = audit_readiness_client_with_state
    named_identity = UserIdentity(user_id=_TEST_AUTHED_USER_ID, username="alice-the-analyst")

    async def _named() -> UserIdentity:
        return named_identity

    original_override = client.app.dependency_overrides[get_current_user]
    client.app.dependency_overrides[get_current_user] = _named
    try:
        token = _mint_token(client, session_id)
        response = client.get(f"/api/sessions/shared/{token}")
    finally:
        client.app.dependency_overrides[get_current_user] = original_override

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_by_username"] == "alice-the-analyst"
    assert body["created_by_user_id"] == _TEST_AUTHED_USER_ID


def test_shared_route_and_persistence_expose_only_public_projection(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """The path-only integration fixture must stay private across every carrier."""
    client, session_id = audit_readiness_client_with_state
    settings = client.app.state.settings
    private_source_path = str(settings.data_dir / "blobs" / str(session_id) / "audit_readiness_fixture.csv")
    private_sink_path = str(settings.data_dir / "outputs" / str(session_id) / "audit_readiness_fixture_out.csv")

    marked = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert marked.status_code == 200, marked.text
    marked_body = marked.json()
    resolved = client.get(f"/api/sessions/shared/{marked_body['token']}")
    assert resolved.status_code == 200, resolved.text
    resolved_body = resolved.json()

    digest_hex = marked_body["payload_digest"].removeprefix("sha256:")
    stored_bytes = client.app.state.payload_store.retrieve(digest_hex)
    stored_body = json.loads(stored_bytes)
    with client.app.state.session_engine.connect() as conn:
        audit_row = conn.execute(
            select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session_id))
        ).one()

    for representation in (stored_body, resolved_body):
        serialized = json.dumps(representation, sort_keys=True)
        assert private_source_path not in serialized
        assert private_sink_path not in serialized
        assert "blob_ref" not in serialized
        assert "secret(s) in your inventory" not in serialized

    assert resolved_body["composition_snapshot"]["sources"]["source"]["plugin"] == "csv"
    assert resolved_body["composition_snapshot"]["nodes"][0]["id"] == "pass"
    assert resolved_body["composition_snapshot"]["outputs"][0]["name"] == "out"
    assert private_source_path not in stored_body["yaml"]
    assert audit_row.payload_digest == marked_body["payload_digest"]
    assert private_source_path not in repr(audit_row)


def test_shared_route_projects_signed_blob_without_rewriting_evidence(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """A valid signed token cannot bypass the current public projection."""
    client, session_id = audit_readiness_client_with_state
    marked = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert marked.status_code == 200, marked.text
    service = client.app.state.shareable_review_service
    payload_store = client.app.state.payload_store
    signed_payload = service._signer.verify(marked.json()["token"])
    stored = json.loads(payload_store.retrieve(signed_payload.payload_digest.removeprefix("sha256:")))

    private_path = "/srv/elspeth/blobs/alice/private-http.csv"
    private_index = "/srv/elspeth/indexes/alice/private-http"
    blob_id = "98b1357d-5aab-4fb3-85b4-5ad643912e84"
    stored["composition_snapshot"]["sources"]["source"]["options"].update(
        {
            "path": private_path,
            "blob_ref": blob_id,
            "mode": "bind_source",
        }
    )
    stored["composition_snapshot"]["nodes"][0]["plugin"] = "llm"
    stored["composition_snapshot"]["nodes"][0]["options"] = {
        "prompt_template": "{{ lookup.path }}",
        "lookup": {"path": "north", "file": "case.txt", "mode": "bind_source"},
        "provider_config": {"persist_directory": private_index},
    }
    stored["yaml"] = f"private_path: {private_path}\nprivate_blob: {blob_id}\n"
    stored["audit_readiness"]["rows"][-1]["detail"] = "23 secret(s) in your inventory"
    signed_bytes = canonical_json(stored).encode()
    digest_hex = payload_store.store(signed_bytes)
    signed_token = service._signer.sign(
        replace(
            signed_payload,
            nonce_hex="cd" * 16,
            payload_digest=f"sha256:{digest_hex}",
        )
    )

    response = client.get(f"/api/sessions/shared/{signed_token}")

    assert response.status_code == 200, response.text
    serialized = response.text
    for private_value in (private_path, private_index, blob_id, "23 secret(s) in your inventory"):
        assert private_value not in serialized
    expected_lookup = {"path": "north", "file": "case.txt", "mode": "bind_source"}
    assert response.json()["composition_snapshot"]["nodes"][0]["options"]["lookup"] == expected_lookup
    assert yaml.safe_load(response.json()["yaml"])["transforms"][0]["options"]["lookup"] == expected_lookup
    assert payload_store.retrieve(digest_hex) == signed_bytes


def test_get_shared_inspect_recipient_is_not_creator(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """alice mints, bob (different authenticated user) resolves successfully."""
    client, session_id = audit_readiness_client_with_state
    token = _mint_token(client, session_id)

    # Swap the get_current_user override to return bob, then re-GET.
    bob_identity = UserIdentity(user_id="bob", username="bob")

    async def _bob() -> UserIdentity:
        return bob_identity

    original_override = client.app.dependency_overrides[get_current_user]
    client.app.dependency_overrides[get_current_user] = _bob
    try:
        response = client.get(f"/api/sessions/shared/{token}")
    finally:
        client.app.dependency_overrides[get_current_user] = original_override
    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == str(session_id)


def test_get_shared_inspect_requires_auth(
    audit_readiness_client_anonymous: TestClient,
) -> None:
    """The token is a CAPABILITY, not an authenticator — auth dep still gates."""
    response = audit_readiness_client_anonymous.get("/api/sessions/shared/some-token")
    assert response.status_code == 401


def test_get_shared_inspect_preserves_real_authentication_challenge(
    audit_readiness_test_client: TestClient,
) -> None:
    client = audit_readiness_test_client
    original_override = client.app.dependency_overrides.pop(get_current_user)
    try:
        response = client.get("/api/sessions/shared/some-token")
    finally:
        client.app.dependency_overrides[get_current_user] = original_override

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid Authorization header"}
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_get_shared_inspect_tampered_token_returns_401(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    token = _mint_token(client, session_id)
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    response = client.get(f"/api/sessions/shared/{tampered}")
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired share token"}
    assert "WWW-Authenticate" not in response.headers


def test_get_shared_inspect_expired_token_returns_401(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """Token verifies cryptographically but is past its ``expires_at`` → 401.

    Mechanism: mint a real token via the route, decode its payload using the
    production signer, re-sign an identical payload with ``expires_at`` in the
    past. The resulting token is signature-valid against the live signing key
    AND structurally indistinguishable from a normally-minted token that has
    aged past its lifetime — exactly what the route must reject with 401.

    Exercises the full route-stack exception-translation path: the route
    calls ``service.resolve_token`` → ``signer.verify`` raises
    ``InvalidToken("token expired")`` → route maps to 401 with the same body
    shape as the tampered-token case (the error string is intentionally
    indistinguishable to a probing attacker, per the signer docstring).

    Phase 6A backend plan line 19a:966 mandates this coverage. Without it,
    a future broadening of the route's ``except InvalidToken`` clause (e.g.
    catching a wider exception) would silently regress an expired-token 401
    into a 500 with no test failure — the unit-level signer/service tests
    would still pass.

    The frozen-pydantic-WebSettings instance precludes the in-place lifetime
    mutation used by the corresponding unit test
    (``test_resolve_token_rejects_expired_token``), which uses a ``MagicMock``
    settings object. Re-signing with the production signer is the equivalent
    end-state and keeps the wire format identical to a naturally-expired
    token.
    """
    client, session_id = audit_readiness_client_with_state
    real_token = _mint_token(client, session_id)
    service = client.app.state.shareable_review_service
    signer = service._signer
    # Verify the real token to extract the live payload fields, then re-sign
    # an otherwise-identical payload with expires_at in the past. Using
    # ``created_at`` 1h ago + ``expires_at`` 1s ago mirrors the relative
    # ordering of a token that aged out under its normal lifetime.
    payload = signer.verify(real_token)
    expired_payload = ShareTokenPayload(
        version=payload.version,
        session_id=payload.session_id,
        state_id=payload.state_id,
        created_at=datetime.now(UTC) - timedelta(hours=1),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        nonce_hex=payload.nonce_hex,
        payload_digest=payload.payload_digest,
        created_by_user_id=payload.created_by_user_id,
    )
    expired_token = signer.sign(expired_payload)
    response = client.get(f"/api/sessions/shared/{expired_token}")
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired share token"}
    assert "WWW-Authenticate" not in response.headers


def test_get_shared_inspect_blob_expired_returns_404(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """Token verifies but payload-store blob has been reaped → 404."""
    client, session_id = audit_readiness_client_with_state
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert response.status_code == 200
    body = response.json()
    token = body["token"]
    digest_hex = body["payload_digest"].removeprefix("sha256:")
    # Reach into the wired payload store and delete the blob.
    payload_store = client.app.state.payload_store
    assert payload_store.delete(digest_hex), "blob should exist before deletion"
    response = client.get(f"/api/sessions/shared/{token}")
    assert response.status_code == 404


def _seed_uploaded_file_source(client: TestClient, session_id: UUID, content: bytes) -> dict[str, str]:
    uploaded = client.post(
        f"/api/sessions/{session_id}/blobs",
        files={"file": ("customers.csv", content, "text/csv")},
    )
    assert uploaded.status_code == 201, uploaded.text
    blob = uploaded.json()
    with client.app.state.session_engine.connect() as conn:
        storage_path = conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob["id"])).scalar_one()
    state_d = _passthrough_composition_state(client.app.state.settings.data_dir, session_id).to_dict()
    state_d["sources"]["source"]["options"].update({"path": storage_path, "blob_ref": blob["id"]})
    asyncio.run(
        _save_composition_state_with_compose_authority(
            client.app.state.session_service,
            session_id,
            CompositionStateData(
                sources=state_d["sources"],
                nodes=state_d["nodes"],
                edges=state_d["edges"],
                outputs=state_d["outputs"],
                metadata_=state_d["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
    )
    return {"id": blob["id"], "content_hash": blob["content_hash"], "storage_path": storage_path}


def _import_share_body(client: TestClient, session_id: UUID, **extra: object) -> dict[str, object]:
    """An importable share's body, confirming the files the preview lists now."""
    preview = client.get(f"/api/sessions/{session_id}/shareable-files")
    assert preview.status_code == 200, preview.text
    fingerprint = preview.json()["manifest_fingerprint"]
    assert fingerprint is not None, preview.json()["not_importable_reason"]
    return {"allow_import": True, "file_manifest_fingerprint": fingerprint, **extra}


def test_preview_lists_each_file_once_with_every_use(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    content = b"id,name\n1,Ada\n"
    _seed_uploaded_file_source(client, session_id, content)

    response = client.get(f"/api/sessions/{session_id}/shareable-files")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "files": [{"filename": "customers.csv", "size_bytes": len(content), "used_by": ["source 'source'"]}],
        "importable": True,
        "not_importable_reason": None,
        "max_lifetime_seconds": client.app.state.settings.shareable_link_lifetime_seconds,
        "manifest_fingerprint": response.json()["manifest_fingerprint"],
    }
    assert re.fullmatch(r"[0-9a-f]{64}", response.json()["manifest_fingerprint"])


def test_preview_reports_llm_authored_file_as_not_importable(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    seeded = _seed_uploaded_file_source(client, session_id, b"id\n1\n")
    with client.app.state.session_engine.begin() as conn:
        conn.execute(
            chat_messages_table.insert().values(
                id="msg-1",
                session_id=str(session_id),
                role="user",
                content="please write notes",
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=datetime.now(UTC),
            )
        )
        conn.execute(
            blobs_table.update()
            .where(blobs_table.c.id == seeded["id"])
            .values(
                creation_modality="llm_generated",
                created_from_message_id="msg-1",
                creating_model_identifier="model",
                creating_model_version="1",
                creating_provider="provider",
                creating_composer_skill_hash="a" * 64,
                creating_arguments_hash="b" * 64,
            )
        )

    response = client.get(f"/api/sessions/{session_id}/shareable-files")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["importable"] is False
    assert body["not_importable_reason"] == "'customers.csv' was written by the assistant and cannot be shared for import"
    assert [f["filename"] for f in body["files"]] == ["customers.csv"]


@pytest.mark.parametrize(
    ("created_by", "reason"),
    [
        pytest.param("assistant", "'customers.csv' was written by the assistant and cannot be shared for import", id="assistant"),
        pytest.param("pipeline", "'customers.csv' was produced by a pipeline run and cannot be shared for import", id="pipeline"),
    ],
)
def test_preview_and_mark_ready_refuse_a_verbatim_file_the_user_did_not_upload(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
    created_by: str,
    reason: str,
) -> None:
    """R20: a VERBATIM blob travels only when the user created it."""
    client, session_id = audit_readiness_client_with_state
    seeded = _seed_uploaded_file_source(client, session_id, b"id\n1\n")
    with client.app.state.session_engine.begin() as conn:
        conn.execute(blobs_table.update().where(blobs_table.c.id == seeded["id"]).values(created_by=created_by))

    preview = client.get(f"/api/sessions/{session_id}/shareable-files")
    marked = client.post(
        f"/api/sessions/{session_id}/mark-ready-for-review",
        # Refused before the fingerprint is compared.
        json={"allow_import": True, "file_manifest_fingerprint": "0" * 64},
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["importable"] is False
    assert preview.json()["not_importable_reason"] == reason
    assert marked.status_code == 409, marked.text
    assert marked.json()["detail"] == reason


def test_preview_route_is_owner_only_404_for_other_user(
    audit_readiness_test_client: TestClient,
    audit_readiness_other_user_session_id: UUID,
) -> None:
    response = audit_readiness_test_client.get(f"/api/sessions/{audit_readiness_other_user_session_id}/shareable-files")
    assert response.status_code == 404


def test_preview_response_contains_no_blob_identity(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    seeded = _seed_uploaded_file_source(client, session_id, b"id,name\n1,Ada\n")

    response = client.get(f"/api/sessions/{session_id}/shareable-files")

    assert response.status_code == 200, response.text
    for private in (seeded["id"], seeded["storage_path"], seeded["content_hash"]):
        assert private not in response.text


def test_review_write_endpoints_use_write_bucket_and_resolve_stays_strict(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    client.app.state.rate_limiter = ComposerRateLimiter(limit=1)
    client.app.state.write_rate_limiter = ComposerRateLimiter(limit=100)
    # Two consecutive write-side calls succeed on a starved strict bucket:
    r1 = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert r1.status_code in (200, 409)
    r2 = client.get(f"/api/sessions/{session_id}/shareable-link")
    assert r2.status_code in (200, 409)
    r3 = client.get(f"/api/sessions/{session_id}/shareable-files")
    assert r3.status_code == 200
    # get_shared_inspect (token resolve) remains strict: the second
    # resolve trips the limit=1 strict bucket.
    token = "not-a-real-token"
    first = client.get(f"/api/sessions/shared/{token}")
    second = client.get(f"/api/sessions/shared/{token}")
    assert 429 in (first.status_code, second.status_code)
    # Import (token probe that creates a session) is strict too: a fresh
    # limit=1 strict bucket trips on the second call while the write bucket
    # is untouched.
    client.app.state.rate_limiter = ComposerRateLimiter(limit=1)
    first_import = client.post(f"/api/sessions/shared/{token}/import")
    second_import = client.post(f"/api/sessions/shared/{token}/import")
    assert (first_import.status_code, second_import.status_code) == (401, 429)


def test_mark_ready_for_review_audit_write_failure_returns_no_token(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the audit insert raises, the request fails — no token returned, no blob exposed.

    Mechanism: the audit row is written through the session-operation
    authority's ``mutate`` seam (``transaction.composer_completion
    .mark_ready_for_review`` under the exact BLOB_READ context), not through
    a bare ``engine.begin()``. Injecting at that seam makes the audit insert
    raise before any blob is written. TestClient defaults to
    ``raise_server_exceptions=True``, so the test catches the exception
    directly; the assertion that matters is that NO blob was written,
    confirming audit-first ordering.
    """
    client, session_id = audit_readiness_client_with_state

    # Track payload_store.store calls so we can prove no blob was written.
    payload_store = client.app.state.payload_store
    original_store = payload_store.store
    store_calls: list[bytes] = []

    def tracking_store(content: bytes) -> str:
        store_calls.append(content)
        return original_store(content)

    monkeypatch.setattr(payload_store, "store", tracking_store)

    # Break the authority's mutate seam so the audit insert raises. Only the
    # shareable-review service's reference is replaced: the route still
    # acquires its BLOB_READ lease from the real session-service authority.
    service = client.app.state.shareable_review_service

    class _AuditWriteBoom(Exception): ...

    class _AuditWriteFailingAuthority:
        def mutate(self, session_operation_context, writer):  # type: ignore[no-untyped-def]
            del session_operation_context, writer
            raise _AuditWriteBoom("audit write injected failure")

    monkeypatch.setattr(service, "_session_operation_authority", _AuditWriteFailingAuthority())

    with pytest.raises(_AuditWriteBoom):
        client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    # CRITICAL: no blob was written.
    assert store_calls == [], "audit insert must precede blob write — blob should not exist when audit fails"


# ── Mark-ready body: import opt-in and per-link lifespan ────────────────


def test_mark_ready_accepts_absent_body(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """No body is today's behaviour: view-only, the settings lifetime."""
    client, session_id = audit_readiness_client_with_state
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review")
    assert response.status_code == 200, response.text
    service = client.app.state.shareable_review_service
    payload = service._signer.verify(response.json()["token"])
    lifetime = client.app.state.settings.shareable_link_lifetime_seconds
    assert payload.expires_at - payload.created_at == timedelta(seconds=lifetime)
    stored = json.loads(client.app.state.payload_store.retrieve(payload.payload_digest.removeprefix("sha256:")))
    assert stored["import_allowed"] is False
    assert stored["files"] == []


def test_mark_ready_refuses_files_that_changed_since_the_preview(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """The owner consents to the previewed files; a swapped file is not frozen."""
    client, session_id = audit_readiness_client_with_state
    _seed_uploaded_file_source(client, session_id, b"id,name\n1,Ada\n")
    previewed = _import_share_body(client, session_id)
    swapped = _seed_uploaded_file_source(client, session_id, b"id,salary\n1,90000\n")

    refused = client.post(f"/api/sessions/{session_id}/mark-ready-for-review", json=previewed)

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["error_type"] == "share_files_changed"
    assert detail["detail"] == "the files this pipeline uses changed since you reviewed them; review the list and create the link again"
    assert not client.app.state.payload_store.exists(swapped["content_hash"])
    with client.app.state.session_engine.connect() as conn:
        rows = conn.execute(
            select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session_id))
        ).all()
    assert rows == []

    confirmed = client.post(f"/api/sessions/{session_id}/mark-ready-for-review", json=_import_share_body(client, session_id))
    assert confirmed.status_code == 200, confirmed.text
    assert client.app.state.payload_store.exists(swapped["content_hash"])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"lifetime_seconds": 3600}, id="unknown-lifetime"),
        pytest.param({"lifetime_seconds": "86400"}, id="string-lifetime"),
        pytest.param({"allow_import": "yes"}, id="non-bool-import"),
        pytest.param({"allow_import": True, "expires": 1}, id="extra-field"),
        pytest.param({"allow_import": True}, id="import-without-previewed-files"),
        pytest.param({"allow_import": True, "file_manifest_fingerprint": "not-a-fingerprint"}, id="malformed-fingerprint"),
    ],
)
def test_mark_ready_rejects_unknown_lifetime_with_422(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
    body: dict[str, object],
) -> None:
    client, session_id = audit_readiness_client_with_state
    response = client.post(f"/api/sessions/{session_id}/mark-ready-for-review", json=body)
    assert response.status_code == 422, response.text
    with client.app.state.session_engine.connect() as conn:
        rows = conn.execute(
            select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session_id))
        ).all()
    assert rows == []


def test_mark_ready_maps_lifetime_above_maximum_to_422(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server, not the browser, enforces the operator's cap."""
    client, session_id = audit_readiness_client_with_state
    service = client.app.state.shareable_review_service
    capped = client.app.state.settings.model_copy(update={"shareable_link_lifetime_seconds": 604800})
    monkeypatch.setattr(service, "_settings", capped)
    response = client.post(
        f"/api/sessions/{session_id}/mark-ready-for-review",
        json={"lifetime_seconds": 2592000},
    )
    assert response.status_code == 422, response.text
    assert "7 days" in response.json()["detail"]


def test_mark_ready_with_lifetime_and_copy_link_keep_one_day(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    marked = client.post(
        f"/api/sessions/{session_id}/mark-ready-for-review",
        json=_import_share_body(client, session_id, lifetime_seconds=86400),
    )
    assert marked.status_code == 200, marked.text
    service = client.app.state.shareable_review_service
    payload = service._signer.verify(marked.json()["token"])
    assert payload.expires_at - payload.created_at == timedelta(days=1)

    reminted = client.get(f"/api/sessions/{session_id}/shareable-link")
    assert reminted.status_code == 200, reminted.text
    reminted_payload = service._signer.verify(reminted.json()["token"])
    assert reminted_payload.expires_at - reminted_payload.created_at == timedelta(days=1)


def test_mark_ready_with_import_freezes_an_uploaded_file(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """End to end through the real blob service: the uploaded bytes are frozen."""
    client, session_id = audit_readiness_client_with_state
    content = b"id,name\n1,Ada\n"
    uploaded = client.post(
        f"/api/sessions/{session_id}/blobs",
        files={"file": ("customers.csv", content, "text/csv")},
    )
    assert uploaded.status_code == 201, uploaded.text
    blob = uploaded.json()
    with client.app.state.session_engine.connect() as conn:
        storage_path = conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob["id"])).scalar_one()

    session_service = client.app.state.session_service
    state_d = _passthrough_composition_state(client.app.state.settings.data_dir, session_id).to_dict()
    state_d["sources"]["source"]["options"].update({"path": storage_path, "blob_ref": blob["id"]})
    asyncio.run(
        _save_composition_state_with_compose_authority(
            session_service,
            session_id,
            CompositionStateData(
                sources=state_d["sources"],
                nodes=state_d["nodes"],
                edges=state_d["edges"],
                outputs=state_d["outputs"],
                metadata_=state_d["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
    )

    marked = client.post(f"/api/sessions/{session_id}/mark-ready-for-review", json=_import_share_body(client, session_id))

    assert marked.status_code == 200, marked.text
    payload_store = client.app.state.payload_store
    assert payload_store.retrieve(blob["content_hash"]) == content
    stored = json.loads(payload_store.retrieve(marked.json()["payload_digest"].removeprefix("sha256:")))
    assert stored["import_allowed"] is True
    assert [(entry["sha256"], entry["filename"], entry["size_bytes"]) for entry in stored["files"]] == [
        (blob["content_hash"], "customers.csv", len(content))
    ]
    assert storage_path not in json.dumps(stored)


# ── POST /sessions/shared/{token}/import ────────────────────────────────


def test_import_route_requires_authentication(
    audit_readiness_client_anonymous: TestClient,
) -> None:
    """The token is a capability, not an authenticator: import needs a login."""
    response = audit_readiness_client_anonymous.post("/api/sessions/shared/some-token/import")
    assert response.status_code == 401


def test_import_route_copies_the_shared_file_into_a_new_session(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    """End to end through the real routes: share with import, then import it."""
    client, session_id = audit_readiness_client_with_state
    content = b"id,name\n1,Ada\n"
    seeded = _seed_uploaded_file_source(client, session_id, content)
    marked = client.post(f"/api/sessions/{session_id}/mark-ready-for-review", json=_import_share_body(client, session_id))
    assert marked.status_code == 200, marked.text

    imported = client.post(f"/api/sessions/shared/{marked.json()['token']}/import")

    assert imported.status_code == 201, imported.text
    assert imported.headers["Cache-Control"] == "no-store"
    new_session_id = imported.json()["session_id"]
    assert new_session_id != str(session_id)
    with client.app.state.session_engine.connect() as conn:
        copies = conn.execute(select(blobs_table).where(blobs_table.c.session_id == new_session_id)).mappings().all()
    assert [(row["content_hash"], row["filename"]) for row in copies] == [(seeded["content_hash"], "customers.csv")]
    record = asyncio.run(client.app.state.session_service.get_current_state(UUID(new_session_id)))
    assert record is not None
    assert str(record.id) == imported.json()["state_id"]
    assert record.sources["source"]["options"]["blob_ref"] == copies[0]["id"]
    assert record.composer_meta["shared_import"]["payload_digest"] == marked.json()["payload_digest"]


def test_import_route_refuses_a_view_only_link_with_403(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    token = _mint_token(client, session_id)
    before = client.get("/api/sessions").json()

    response = client.post(f"/api/sessions/shared/{token}/import")

    assert response.status_code == 403, response.text
    assert client.get("/api/sessions").json() == before

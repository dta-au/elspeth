"""POST /validate's interpretation-review repair pass through the REAL authority (elspeth-86866d4b92).

The route holds a shareable BLOB_READ admission. When the repair pass finds a
pending requirement with no evidence row it must WRITE, and the interpretation
writer admits only COMPOSE/PROPOSAL authority: before this fix the write raised
``SessionOperationFenceLost(token_mismatch)`` and the route answered 404
"Session not found". The surfacer now takes its own COMPOSE lease for the write
step, only when it has work. Pinned here against the full app and the SQLite
authority, which the route-level fakes cannot see.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.sessions.protocol import CompositionStateData
from tests.integration.web.conftest import (
    _TEST_AUTHED_USER_ID,
    _lifespan_test_client,
    _save_composition_state_with_compose_authority,
)

PROMPT = "Rate this row and return JSON."


def _build_repair_pass_app(tmp_path: Path) -> FastAPI:
    """The audit-readiness app builder with ``transform:llm`` admitted.

    Only an ``llm`` transform carries prompt-template review sites
    (``interpretation_state._pending_node_sites``), so the fixture node must
    be one and the plugin allowlist must admit it.
    """
    from elspeth.web.app import create_app

    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough", "transform:llm"),
    )
    app = create_app(settings=settings)
    identity = UserIdentity(user_id=_TEST_AUTHED_USER_ID, username=_TEST_AUTHED_USER_ID)

    async def _mock_user() -> UserIdentity:
        return identity

    app.dependency_overrides[get_current_user] = _mock_user
    return app


def _state_with_unsurfaced_prompt_template(data_dir: Path, session_id: UUID) -> CompositionState:
    source_path = str(data_dir / "blobs" / str(session_id) / "repair_pass_fixture.csv")
    sink_path = str(data_dir / "outputs" / str(session_id) / "repair_pass_fixture_out.csv")
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="src_out",
            options={"path": source_path, "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="rate_node",
                node_type="transform",
                plugin="llm",
                input="src_out",
                on_success="out",
                on_error="discard",
                options={
                    "prompt_template": PROMPT,
                    "model": "anthropic/claude-sonnet-4.6",
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "id": "prompt_template_review:rate_node",
                            "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
                            "user_term": "llm_prompt_template:rate_node",
                            "status": "pending",
                            "draft": PROMPT,
                            "event_id": None,
                            "accepted_value": None,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": None,
                        }
                    ],
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="out",
                plugin="csv",
                options={"path": sink_path, "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.fixture
def repair_pass_client(tmp_path: Path):
    with _lifespan_test_client(_build_repair_pass_app(tmp_path)) as client:
        yield client


def _seed(client: TestClient) -> tuple[UUID, UUID]:
    session_service = client.app.state.session_service
    settings = client.app.state.settings

    async def _run() -> tuple[UUID, UUID]:
        record = await session_service.create_session(
            user_id=_TEST_AUTHED_USER_ID,
            title="repair pass fixture",
            auth_provider_type=settings.auth_provider,
        )
        (settings.data_dir / "blobs" / str(record.id)).mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "outputs" / str(record.id)).mkdir(parents=True, exist_ok=True)
        state_d = _state_with_unsurfaced_prompt_template(settings.data_dir, record.id).to_dict()
        saved = await _save_composition_state_with_compose_authority(
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
        return record.id, saved.id

    return asyncio.run(_run())


def _pending_events(client: TestClient, session_id: UUID):
    session_service = client.app.state.session_service
    return asyncio.run(session_service.list_interpretation_events(session_id, status="pending"))


def test_validate_repairs_the_unsurfaced_requirement_under_its_own_writer_lease(repair_pass_client: TestClient) -> None:
    client = repair_pass_client
    session_id, state_id = _seed(client)
    assert _pending_events(client, session_id) == []

    response = client.post(f"/api/sessions/{session_id}/validate", params={"state_id": str(state_id)})

    assert response.status_code == 200, response.text
    events = _pending_events(client, session_id)
    assert [event.kind for event in events] == [InterpretationKind.LLM_PROMPT_TEMPLATE]
    # Idempotent: the evidence now exists, so a second pass writes nothing.
    assert client.post(f"/api/sessions/{session_id}/validate", params={"state_id": str(state_id)}).status_code == 200
    assert len(_pending_events(client, session_id)) == 1


def test_validate_with_nothing_to_repair_takes_no_writer_lease(repair_pass_client: TestClient) -> None:
    """The common page load: a live COMPOSE elsewhere must not make validate a 409."""
    from elspeth.contracts.session_operation import SessionOperationKind

    client = repair_pass_client
    session_id, state_id = _seed(client)
    assert client.post(f"/api/sessions/{session_id}/validate", params={"state_id": str(state_id)}).status_code == 200
    service = client.app.state.session_service
    writer = service.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    try:
        assert client.post(f"/api/sessions/{session_id}/validate", params={"state_id": str(state_id)}).status_code == 200
    finally:
        service.session_operation_authority.release(writer)

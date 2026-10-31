"""Authenticated graph recovery binds new versions without a model dispatch."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select, update

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.yaml_generator import PUBLIC_EXPORT_REDACTION_HEADER
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.sessions.content_publication import read_content_publication
from elspeth.web.sessions.models import composer_completion_events_table, composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.state_envelope import envelope_state_column
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.helpers.composition_state import composition_state_data
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines
from tests.unit.web.sessions.test_routes import _make_app, _save_test_composition_state

_YAML = """# exact recovery input: café
sources:
  source:
    plugin: csv
    on_success: main
    options:
      schema:
        mode: observed
    on_validation_failure: discard
sinks:
  main:
    plugin: csv
    options:
      path: outputs/recovered.csv
    on_write_failure: discard
"""


async def _pass_preflight(_state: object, **_context: object) -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


@pytest.mark.asyncio
async def test_revert_binds_new_version_and_replay_keeps_the_same_publication(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Revert content", "local")
    original = await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(metadata_={"name": "Original café", "description": ""}, is_valid=True),
        provenance="session_seed",
    )
    await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(metadata_={"name": "Edited", "description": ""}, is_valid=True),
        provenance="session_seed",
    )
    original_publication = read_content_publication(original)
    assert original_publication is not None
    body = {"operation_id": str(uuid4()), "state_id": str(original.id)}
    client = SyncASGITestClient(app)

    with patch(
        "litellm.acompletion", new_callable=AsyncMock, spec=True, side_effect=AssertionError("revert must not dispatch")
    ) as dispatch:
        response = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert response.status_code == 200, response.text
        reverted = await service.get_current_state(session.id)
        assert reverted is not None
        publication = read_content_publication(reverted)
        assert publication is not None
        assert publication.state_id == reverted.id != original.id
        assert publication.session_id == session.id
        assert publication.state_version == reverted.version == 3
        assert publication.storage_content_hash == original_publication.storage_content_hash
        assert publication.to_payload()["applicability"] == "requires_current_inspection"
        assert deep_thaw(reverted.metadata_) == deep_thaw(original.metadata_)
        replay = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        dispatch.assert_not_awaited()

    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    current = await service.get_current_state(session.id)
    assert current is not None and current.id == reverted.id
    assert read_content_publication(current) == publication
    assert [record.version for record in await service.get_state_versions(session.id)] == [1, 2, 3]
    assert read_content_publication(await service.get_state(original.id)) == original_publication


@pytest.mark.asyncio
async def test_yaml_replacement_rebinds_content_and_preserves_exact_ingress(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Replace content", "local")
    original = await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(metadata_={"name": "Old", "description": ""}, is_valid=True),
        provenance="session_seed",
    )
    client = SyncASGITestClient(app)
    with (
        patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", autospec=True, side_effect=_pass_preflight),
        patch(
            "litellm.acompletion", new_callable=AsyncMock, spec=True, side_effect=AssertionError("replacement must not dispatch")
        ) as dispatch,
    ):
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _YAML})
        dispatch.assert_not_awaited()

    assert response.status_code == 200, response.text
    replacement = await service.get_current_state(session.id)
    assert replacement is not None and replacement.id != original.id
    publication = read_content_publication(replacement)
    previous = read_content_publication(original)
    assert publication is not None and previous is not None
    assert publication.state_id == replacement.id
    assert publication.state_version == 2
    assert publication.storage_content_hash != previous.storage_content_hash
    assert publication.to_payload()["applicability"] == "requires_current_inspection"
    assert replacement.composer_meta is not None
    assert replacement.composer_meta["ingress"]["text_sha256"] == hashlib.sha256(_YAML.encode("utf-8")).hexdigest()
    assert replacement.sources["source"]["options"]["schema"]["mode"] == "observed"
    assert replacement.outputs[0]["options"]["path"] == "outputs/recovered.csv"
    persisted_original = await service.get_state(original.id)
    assert persisted_original == original


@pytest.mark.asyncio
async def test_export_legacy_uninspected_graph_without_mutation_or_dispatch(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Export legacy", "local")
    saved = await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(
            sources={
                "source": {
                    "plugin": "csv",
                    "on_success": "main",
                    "on_validation_failure": "discard",
                    "options": {"schema": {"mode": "observed"}},
                }
            },
            outputs=[
                {
                    "name": "main",
                    "plugin": "csv",
                    "options": {"path": "outputs/authored.csv"},
                    "on_write_failure": "discard",
                }
            ],
            metadata_={"name": "Legacy café", "description": ""},
            is_valid=True,
        ),
        provenance="session_seed",
    )
    # Model a pre-publication historical row; ordinary writers must not mint it.
    with service._engine.begin() as connection:
        connection.execute(
            update(composition_states_table).where(composition_states_table.c.id == str(saved.id)).values(composer_meta=None)
        )
    before = await service.get_current_state(session.id)
    assert before is not None and read_content_publication(before) is None
    client = SyncASGITestClient(app)
    with (
        patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", autospec=True, side_effect=_pass_preflight),
        patch("litellm.acompletion", new_callable=AsyncMock, spec=True, side_effect=AssertionError("export must not dispatch")) as dispatch,
    ):
        response = client.get(f"/api/sessions/{session.id}/state/yaml")
        dispatch.assert_not_awaited()

    assert response.status_code == 200, response.text
    exported = yaml.safe_load(response.json()["yaml"])
    assert exported["sources"]["source"]["plugin"] == "csv"
    assert exported["sinks"]["main"]["plugin"] == "csv"
    assert "options" not in exported["sinks"]["main"]
    assert PUBLIC_EXPORT_REDACTION_HEADER in response.json()["yaml"]
    assert "outputs/authored.csv" not in response.json()["yaml"]
    assert await service.get_current_state(session.id) == before
    assert [record.id for record in await service.get_state_versions(session.id)] == [saved.id]
    with service._engine.connect() as connection:
        audit = connection.execute(
            select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session.id))
        ).one()
    assert audit.composition_state_id == str(saved.id)
    assert audit.event_type == "export_yaml"


@pytest.mark.asyncio
async def test_revert_current_legacy_state_repairs_publication_without_dispatch(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Repair legacy metadata", "local")
    seeded = await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(metadata_={"name": "Authored café", "description": "unchanged"}, is_valid=True),
        provenance="session_seed",
    )
    legacy_metadata = {"legacy_import": {"source": "manual café"}}
    with service._engine.begin() as connection:
        connection.execute(
            update(composition_states_table)
            .where(composition_states_table.c.id == str(seeded.id))
            .values(composer_meta=envelope_state_column(legacy_metadata))
        )
        original_row = connection.execute(select(composition_states_table).where(composition_states_table.c.id == str(seeded.id))).one()
    legacy = await service.get_current_state(session.id)
    assert legacy is not None and read_content_publication(legacy) is None
    body = {"operation_id": str(uuid4()), "state_id": str(legacy.id)}
    client = SyncASGITestClient(app)
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, spec=True, side_effect=AssertionError("repair must not dispatch")) as dispatch,
        patch(
            "elspeth.web.composer.prompt_guard_clients.AzurePromptGuardClient.inspect",
            new_callable=AsyncMock,
            spec=True,
            side_effect=AssertionError("repair must not require provider inspection"),
        ) as inspection,
    ):
        response = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert response.status_code == 200, response.text
        replay = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        dispatch.assert_not_awaited()
        inspection.assert_not_awaited()

    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    repaired = await service.get_current_state(session.id)
    assert repaired is not None and repaired.id != legacy.id
    publication = read_content_publication(repaired)
    assert publication is not None
    assert publication.state_id == repaired.id
    assert publication.state_version == 2
    assert publication.to_payload()["applicability"] == "requires_current_inspection"
    assert deep_thaw(repaired.metadata_) == deep_thaw(legacy.metadata_)
    assert repaired.sources == legacy.sources
    assert repaired.nodes == legacy.nodes
    assert repaired.edges == legacy.edges
    assert repaired.outputs == legacy.outputs
    assert repaired.composer_meta is not None
    assert deep_thaw(repaired.composer_meta["legacy_import"]) == legacy_metadata["legacy_import"]
    assert [record.version for record in await service.get_state_versions(session.id)] == [1, 2]
    with service._engine.connect() as connection:
        retained_row = connection.execute(select(composition_states_table).where(composition_states_table.c.id == str(legacy.id))).one()
    assert retained_row == original_row


@pytest.mark.asyncio
async def test_revert_refuses_foreign_session_target_without_publication(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Current", "local")
    other = await service.create_session("alice", "Other", "local")
    before = await _save_test_composition_state(service, session.id, composition_state_data(is_valid=True), provenance="session_seed")
    foreign = await _save_test_composition_state(service, other.id, composition_state_data(is_valid=True), provenance="session_seed")
    response = SyncASGITestClient(app).post(
        f"/api/sessions/{session.id}/state/revert",
        json={"operation_id": str(uuid4()), "state_id": str(foreign.id)},
    )
    assert response.status_code == 404
    assert await service.get_current_state(session.id) == before
    assert [record.id for record in await service.get_state_versions(session.id)] == [before.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["revert", "replace", "export"])
async def test_recovery_requires_session_ownership_without_mutating_content(tmp_path: Path, operation: str) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Owned", "local")
    before = await _save_test_composition_state(service, session.id, composition_state_data(is_valid=True), provenance="session_seed")
    with service._engine.begin() as connection:
        ensure_test_identity(connection, identity_id="bob")
        grant_test_pipeline_user(connection, identity_id="bob")

    async def bob() -> UserIdentity:
        return UserIdentity(user_id="bob", username="bob")

    app.dependency_overrides[get_current_user] = bob
    client = SyncASGITestClient(app)
    if operation == "revert":
        response = client.post(
            f"/api/sessions/{session.id}/state/revert",
            json={"operation_id": str(uuid4()), "state_id": str(before.id)},
        )
    elif operation == "replace":
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _YAML})
    else:
        response = client.get(f"/api/sessions/{session.id}/state/yaml")

    assert response.status_code == 404
    assert await service.get_current_state(session.id) == before
    assert [record.id for record in await service.get_state_versions(session.id)] == [before.id]

"""YAML paste and library fork preserve exact ingress on the first state."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Request
from sqlalchemy import func, select
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import _make_app

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.auth.models import UserIdentity
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.routes.composer.state import ImportStateYamlRequest, seed_state_from_runtime_yaml

_YAML = """# compartment_id: foreign-team
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
      path: outputs/out.csv
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
async def test_pasted_yaml_records_exact_text_hash_and_foreign_marking(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    app.state.settings = app.state.settings.model_copy(update={"compartment_id": "local-team"})
    session = await service.create_session("alice", "Pasted state", "local")
    client = SyncASGITestClient(app)

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _YAML})

    assert response.status_code == 200, response.text
    state = await service.get_current_state(session.id)
    assert state is not None and state.composer_meta is not None
    assert deep_thaw(state.composer_meta["ingress"]) == {
        "text_sha256": hashlib.sha256(_YAML.encode("utf-8")).hexdigest(),
        "foreign_compartment_ids": ["foreign-team"],
    }


@pytest.mark.asyncio
async def test_library_seed_records_fork_provenance_and_ingress_in_one_state(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    app.state.settings = app.state.settings.model_copy(update={"compartment_id": "local-team"})
    session = await service.create_session("alice", "Library fork", "local")
    request = Request({"type": "http", "app": app})
    facts = {
        "library_fork": {
            "entry_id": "entry-1",
            "payload_digest": hashlib.sha256(_YAML.encode("utf-8")).hexdigest(),
            "published_from_session_id": "source-session-1",
            "compartment_id": "foreign-team",
            "version": 4,
        }
    }

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = await seed_state_from_runtime_yaml(
            session=session,
            body=ImportStateYamlRequest(yaml=_YAML),
            request=request,
            user=UserIdentity(user_id="alice", username="alice"),
            composer_meta_updates=facts,
        )

    state = await service.get_current_state(session.id)
    assert state is not None and state.composer_meta is not None and response.id == str(state.id)
    assert deep_thaw(state.composer_meta["library_fork"]) == facts["library_fork"]
    assert deep_thaw(state.composer_meta["ingress"]) == {
        "text_sha256": facts["library_fork"]["payload_digest"],
        "foreign_compartment_ids": ["foreign-team"],
    }
    with app.state.session_engine.connect() as conn:
        state_count = conn.execute(
            select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == str(session.id))
        ).scalar_one()
    assert state_count == 1

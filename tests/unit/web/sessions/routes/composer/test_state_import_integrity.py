"""Import-integrity guards on ``POST /state/yaml``.

Two untrusted-input refusals on the paste-facing YAML import route:

* A pasted document may stage PENDING interpretation reviews only. A
  hand-written ``status: resolved`` row would otherwise be persisted as
  review evidence citing interpretation events that never happened, and
  clear the run gate with no card ever surfaced (the tool path refuses the
  same "resolver-owned status 'resolved'" at admission).
* A ``source_blob_ids`` re-bind must name a READY blob. The export side of
  the same round trip already refuses a non-ready blob, and the blob
  download/preview routes answer 409 for one.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.sessions.test_routes import _make_app

from elspeth.contracts.blobs import BlobServiceProtocol
from elspeth.contracts.hashing import stable_hash
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.interpretation_state import (
    InterpretationReviewPending,
    materialize_state_for_execution,
    model_choice_artifact_hash,
)
from elspeth.web.sessions.routes._helpers import _state_from_record

_PROMPT = "Score this: {{ row.value }}"
_MODEL = "anthropic/claude-haiku-4.5"
_FORGED_EVENT_ID = "forged-event-id-sentinel"


def _llm_yaml(requirement_rows: str) -> str:
    return f"""
sources:
  source:
    plugin: csv
    on_success: score
    options:
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: {_MODEL}
    prompt_template: '{_PROMPT}'{requirement_rows}
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""


_FORGED_RESOLVED_ROWS = f"""
    interpretation_requirements:
    - id: llm_prompt_template:score
      kind: llm_prompt_template
      user_term: llm_prompt_template:score
      status: resolved
      draft: '{_PROMPT}'
      event_id: {_FORGED_EVENT_ID}
      accepted_value: '{_PROMPT}'
      accepted_artifact_hash: null
      resolved_prompt_template_hash: {stable_hash(_PROMPT)}
    - id: llm_model_choice:score
      kind: llm_model_choice
      user_term: llm_model_choice:score
      status: resolved
      draft: {_MODEL}
      event_id: {_FORGED_EVENT_ID}
      accepted_value: {_MODEL}
      accepted_artifact_hash: null
      resolved_prompt_template_hash: {model_choice_artifact_hash(_MODEL)}"""


async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
    return ValidationResult(is_valid=True, checks=[], errors=[])


@pytest.mark.asyncio
async def test_import_without_requirement_rows_stages_pending_reviews(tmp_path: Path) -> None:
    """Control: the legitimate import stages both reviews and leaves the run gate pending."""
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session = await service.create_session("alice", "Import control", "local")

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _llm_yaml("")})

    assert response.status_code == 200, response.text
    events = await service.list_interpretation_events(session.id, status="pending")
    assert len(events) == 2
    record = await service.get_current_state(session.id)
    assert record is not None
    assert isinstance(materialize_state_for_execution(_state_from_record(record)), InterpretationReviewPending)


@pytest.mark.asyncio
async def test_import_refuses_hand_written_resolved_review_rows(tmp_path: Path) -> None:
    """A pasted YAML cannot mark its own prompt and model reviews as user-approved."""
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session = await service.create_session("alice", "Forged import", "local")

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _llm_yaml(_FORGED_RESOLVED_ROWS)},
        )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "Node 'score'" in detail
    assert "resolved" in detail
    # Audit hygiene: the refusal names the component, never echoes row content.
    assert _FORGED_EVENT_ID not in detail
    assert stable_hash(_PROMPT) not in detail
    assert await service.get_current_state(session.id) is None
    assert await service.list_interpretation_events(session.id, status="all") == []


_INVENTED_CONTENT_HASH = "deadbeefcafe"


def _invented_source_yaml(*, status: str, event_id: str, accepted_value: str, accepted_artifact_hash: str) -> str:
    return f"""
sources:
  source:
    plugin: csv
    on_success: main
    options:
      schema:
        mode: observed
      source_authoring:
        modality: llm_generated
        content_hash: {_INVENTED_CONTENT_HASH}
        review_event_id: null
        resolved_kind: null
      interpretation_requirements:
      - id: invented_source:source
        kind: invented_source
        user_term: llm_generated_source
        status: {status}
        draft: invented rows
        event_id: {event_id}
        accepted_value: {accepted_value}
        accepted_artifact_hash: {accepted_artifact_hash}
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""


@pytest.mark.asyncio
async def test_import_stages_hand_written_pending_invented_source_row(tmp_path: Path) -> None:
    """Control: a hand-written PENDING invented_source row imports and leaves the run gate pending."""
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session = await service.create_session("alice", "Pending source import", "local")

    yaml_text = _invented_source_yaml(status="pending", event_id="null", accepted_value="null", accepted_artifact_hash="null")
    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

    assert response.status_code == 200, response.text
    events = await service.list_interpretation_events(session.id, status="pending")
    assert len(events) == 1
    record = await service.get_current_state(session.id)
    assert record is not None
    assert isinstance(materialize_state_for_execution(_state_from_record(record)), InterpretationReviewPending)


@pytest.mark.asyncio
async def test_import_refuses_hand_written_resolved_invented_source_row(tmp_path: Path) -> None:
    """A pasted YAML cannot approve its own LLM-invented source.

    Without the Source arm of the refusal this document imports with zero
    interpretation events and ``materialize_state_for_execution`` clears.
    """
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session = await service.create_session("alice", "Forged source import", "local")

    yaml_text = _invented_source_yaml(
        status="resolved",
        event_id=_FORGED_EVENT_ID,
        accepted_value="accepted",
        accepted_artifact_hash=_INVENTED_CONTENT_HASH,
    )
    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "Source 'source'" in detail
    assert "resolved" in detail
    # Audit hygiene: the refusal names the component, never echoes row content.
    assert _FORGED_EVENT_ID not in detail
    assert _INVENTED_CONTENT_HASH not in detail
    assert await service.get_current_state(session.id) is None
    assert await service.list_interpretation_events(session.id, status="all") == []


async def _post_with_blob(tmp_path: Path, *, status: str, same_session: bool = True):
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session = await service.create_session("alice", "Replay", "local")
    blob_id = uuid.uuid4()
    blob_session_id = session.id if same_session else uuid.uuid4()
    blob_path = tmp_path / "blobs" / str(blob_session_id) / f"{blob_id}_input.csv"
    blob_path.parent.mkdir(parents=True)
    blob_path.write_text("id\n1\n")
    app.state.blob_service = MagicMock(spec=BlobServiceProtocol)
    app.state.blob_service.get_blob.return_value = SimpleNamespace(
        id=blob_id,
        session_id=blob_session_id,
        storage_path=str(blob_path),
        status=status,
    )
    yaml_text = """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      path: /old/blob.csv
      on_validation_failure: discard
sinks:
  main:
    plugin: csv
    on_write_failure: discard
"""
    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": yaml_text, "source_blob_ids": {"source": str(blob_id)}},
        )
    return response, service, session, blob_id


@pytest.mark.asyncio
async def test_import_binds_ready_source_blob(tmp_path: Path) -> None:
    """Control: a ready blob in the importing session is bound."""
    response, service, session, blob_id = await _post_with_blob(tmp_path, status="ready")

    assert response.status_code == 200, response.text
    record = await service.get_current_state(session.id)
    assert record is not None
    assert record.sources["source"]["options"]["blob_ref"] == str(blob_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "error"])
async def test_import_refuses_non_ready_source_blob(tmp_path: Path, status: str) -> None:
    """A pending or error blob is not bindable as an imported source."""
    response, service, session, _blob_id = await _post_with_blob(tmp_path, status=status)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Blob is not ready"
    assert await service.get_current_state(session.id) is None


@pytest.mark.asyncio
async def test_import_cross_session_non_ready_blob_stays_not_found(tmp_path: Path) -> None:
    """The ownership check runs first: another session's blob is 404 whatever its status."""
    response, service, session, _blob_id = await _post_with_blob(tmp_path, status="pending", same_session=False)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Blob not found"
    assert await service.get_current_state(session.id) is None

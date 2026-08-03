"""Schema-9 atomic RESPOND route contracts."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from elspeth.contracts.blobs import BlobNotFoundError, BlobStateError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.protocol import (
    GuidedStep,
    TurnType,
)
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import (
    ComponentTarget,
    GuidedProposalRef,
    GuidedSession,
    SinkIntent,
    SourceIntent,
    TerminalKind,
    TerminalReason,
    TerminalState,
    TurnRecord,
    guided_reviewed_anchor_hash,
)
from elspeth.web.composer.pipeline_proposal import AbsentBase
from elspeth.web.composer.source_inspection import SourceInspectionFacts
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.guided_replay import guided_turn_token, load_guided_json_payload
from elspeth.web.sessions.models import guided_operations_table
from elspeth.web.sessions.protocol import CompositionStateData, GuidedOperationTakenOver
from elspeth.web.sessions.routes._helpers import _initial_composition_state_with_guided_session
from elspeth.web.sessions.routes.composer import guided as guided_route
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.schemas import GuidedRespondRequest
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.integration.web.composer.guided.test_respond import TestStep2IntraStep as _Step2Journey
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


@pytest.fixture
def file_composer_test_client(composer_test_client: TestClient, tmp_path: Path) -> Iterator[TestClient]:
    """Rebind the minimal app to file SQLite for real multi-connection races."""
    engine = create_session_engine(f"sqlite:///{tmp_path / 'respond-races.db'}")
    initialize_session_schema(engine)
    composer_test_client.app.state.session_engine = engine
    composer_test_client.app.state.session_service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.guided.respond.races"),
    )
    try:
        yield composer_test_client
    finally:
        engine.dispose()


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"title": "schema-8 respond"})
    assert response.status_code == 201, response.json()
    return response.json()["id"]


def _with_active_step3(guided: GuidedSession, active: GuidedProposalRef) -> GuidedSession:
    """Attach schema-10 proposal authority to its sole unanswered turn."""
    return replace(
        guided,
        step=GuidedStep.STEP_3_TRANSFORMS,
        history=(
            TurnRecord(
                step=GuidedStep.STEP_3_TRANSFORMS,
                turn_type=TurnType.PROPOSE_PIPELINE,
                payload_hash="d" * 64,
                response_hash=None,
                emitter="server",
            ),
        ),
        active_proposal=active,
    )


def _start(client: TestClient, session_id: str) -> dict:
    response = client.post(
        f"/api/sessions/{session_id}/guided/start",
        json={"profile": "tutorial", "operation_id": str(uuid4())},
    )
    assert response.status_code == 200, response.json()
    return response.json()


def _stage_proposal(client: TestClient, *, filename: str) -> tuple[str, dict]:
    session_id = _create_session(client)
    staged = _Step2Journey()._stage_proposal(client, session_id, filename=filename)
    return session_id, staged


def _live_body(turn: dict, **overrides: object) -> dict:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "turn_token": turn["turn_token"],
        "chosen": None,
        "edited_values": None,
        "custom_inputs": None,
        "control_signal": None,
        "proposal_id": None,
        "draft_hash": None,
        "edit_target": None,
    }
    body.update(overrides)
    return body


def _respond_operation_count(client: TestClient, session_id: str) -> int:
    with client.app.state.session_engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count())
                .select_from(guided_operations_table)
                .where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.kind == "guided_respond",
                )
            ).scalar_one()
        )


def _payload_file_count(client: TestClient) -> int:
    return sum(path.is_file() for path in client.app.state.payload_store.base_path.rglob("*"))


_NON_FILE_SOURCE = "azure_blob"


def _enable_non_file_source(client: TestClient) -> None:
    """Authorize one blob-inspection-INCOMPATIBLE source for this deployment.

    These tests only need a source that cannot be schema-inspected from an
    uploaded file. ``aws_s3`` used to play that role, but the web authoring
    surface now declines it categorically (``WEB_SURFACE_PROHIBITED``), so it is
    never offered or selectable in a web session — the subject here is the
    non-file selection path, not the S3 policy.
    """
    settings = client.app.state.settings.model_copy(
        update={
            "plugin_allowlist": (
                *client.app.state.settings.plugin_allowlist,
                f"source:{_NON_FILE_SOURCE}",
            )
        }
    )
    runtime_policy = RuntimeWebPluginConfig.from_settings(settings)
    client.app.state.settings = settings
    client.app.state.web_plugin_policy = compile_web_plugin_policy(
        registry=get_shared_plugin_manager(),
        settings=runtime_policy,
    )
    client.app.state.operator_profile_registry = OperatorProfileRegistry(
        policy=client.app.state.web_plugin_policy,
        settings=runtime_policy,
    )


class _ReadRaceBlobService:
    def __init__(self, record: object, outcome: bytes | Exception) -> None:
        self._record = record
        self._outcome = outcome

    async def get_blob(self, _blob_id: UUID) -> object:
        # An explicit source_blob_id resolves via get_blob (a direct,
        # session-qualified lookup) rather than list_blobs + Python filter —
        # see inspect_selected_ready_session_blob. Both tests using this fake
        # select an explicit blob_id, so this is the method exercised now.
        return self._record

    async def list_blobs(self, _session_id: UUID, limit: int | None = 50, offset: int = 0) -> list[object]:
        del limit, offset
        return [self._record]

    async def read_blob_content_prefix_verified(self, _blob_id: UUID, *, prefix_bytes: int) -> tuple[bytes, str, int]:
        # inspect_selected_ready_session_blob now streams+verifies via this
        # method instead of read_blob_content. This fake stands in for a
        # (possibly non-compliant) BlobServiceProtocol implementation: on the
        # bytes-mismatch path it returns a hash genuinely computed from the
        # changed bytes it "read", which will not match the original
        # record.content_hash the caller already holds — that mismatch is
        # exactly what source_inspection.py's own verification must catch.
        if isinstance(self._outcome, Exception):
            raise self._outcome
        content = self._outcome
        computed_hash = hashlib.sha256(content).hexdigest()
        return content[:prefix_bytes], computed_hash, len(content)


def _guided_audit_events(client: TestClient, session_id: str) -> list[tuple[str, dict[str, object]]]:
    messages = asyncio.run(client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    events: list[tuple[str, dict[str, object]]] = []
    for message in messages:
        for tool_call in message.tool_calls or ():
            invocation = tool_call.get("invocation", {})
            tool_name = invocation.get("tool_name")
            arguments = invocation.get("arguments_canonical")
            if isinstance(tool_name, str) and isinstance(arguments, str):
                events.append((tool_name, json.loads(arguments)))
    return events


def _persist_guided(client: TestClient, session_id: str, guided: GuidedSession) -> None:
    state = replace(_initial_composition_state_with_guided_session(), guided_session=guided)
    state_dict = state.to_dict()
    asyncio.run(
        client.app.state.session_service.save_composition_state(
            UUID(session_id),
            CompositionStateData(
                sources=state_dict["sources"],
                nodes=state_dict["nodes"],
                edges=state_dict["edges"],
                outputs=state_dict["outputs"],
                metadata_=state_dict["metadata"],
                is_valid=False,
                composer_meta={"guided_session": guided.to_dict()},
            ),
            provenance="session_seed",
        )
    )


def _with_route_turn(guided: GuidedSession, turn_type: TurnType) -> GuidedSession:
    return replace(
        guided,
        history=(
            *guided.history,
            TurnRecord(
                step=guided.step,
                turn_type=turn_type,
                payload_hash="a" * 64,
                response_hash=None,
                emitter="server",
            ),
        ),
    )


def test_http_output_edit_omits_hidden_policy_and_preserves_server_reviewed_value(
    composer_test_client: TestClient,
) -> None:
    client = composer_test_client
    session_id = _create_session(client)
    source_id = "11111111-1111-4111-8111-111111111111"
    output_id = "33333333-3333-4333-8333-333333333333"
    guided = GuidedSession(
        step=GuidedStep.STEP_2_SINK,
        source_order=(source_id,),
        reviewed_sources={
            source_id: SourceResolved(
                name="source",
                plugin="csv",
                options={"path": "input.csv"},
                observed_columns=("id", "name"),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(output_id,),
        reviewed_outputs={
            output_id: SinkOutputResolved(
                name="output",
                plugin="json",
                options={"path": "old.jsonl"},
                required_fields=("id",),
                schema_mode="observed",
                on_write_failure="failures",
            )
        },
    )
    _persist_guided(client, session_id, guided)

    review = client.get(f"/api/sessions/{session_id}/guided")
    assert review.status_code == 200, review.json()
    review_turn = review.json()["next_turn"]
    assert review_turn["type"] == "review_components"
    editing = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            review_turn,
            component_action={"action": "edit", "target": {"kind": "output", "stable_id": output_id}},
        ),
    )
    assert editing.status_code == 200, editing.json()
    form_turn = editing.json()["next_turn"]
    assert form_turn["type"] == "schema_form"
    assert form_turn["payload"]["prefilled"]["on_write_failure"] == "failures"

    output_path = Path(client.app.state.settings.data_dir) / "outputs" / session_id / "revised.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    submitted_options = {
        "path": str(output_path),
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "auto_increment",
    }
    staged = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            form_turn,
            edited_values={"plugin": "json", "options": submitted_options},
        ),
    )
    assert staged.status_code == 200, staged.json()
    staged_record = asyncio.run(client.app.state.session_service.get_current_state(UUID(session_id)))
    assert staged_record is not None and staged_record.composer_meta is not None
    staged_guided = GuidedSession.from_dict(deep_thaw(staged_record.composer_meta)["guided_session"])
    staged_options = staged_guided.pending_output_intents[output_id].options
    assert staged_options is not None
    assert staged_options["on_write_failure"] == "failures"

    field_turn = staged.json()["next_turn"]
    assert field_turn["type"] == "multi_select_with_custom"
    reviewed = client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(field_turn, control_signal="passthrough"),
    )
    assert reviewed.status_code == 200, reviewed.json()
    reviewed_record = asyncio.run(client.app.state.session_service.get_current_state(UUID(session_id)))
    assert reviewed_record is not None and reviewed_record.composer_meta is not None
    reviewed_guided = GuidedSession.from_dict(deep_thaw(reviewed_record.composer_meta)["guided_session"])
    resolved = reviewed_guided.reviewed_outputs[output_id]
    assert resolved.options["path"] == str(output_path)
    assert resolved.on_write_failure == "failures"


def test_first_prospective_response_settles_schema8_and_replays_exactly_after_drift(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    fetched = composer_test_client.get(f"/api/sessions/{session_id}/guided")
    assert fetched.status_code == 200, fetched.json()
    turn = fetched.json()["next_turn"]
    assert fetched.json()["composition_state"] is None
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == []
    assert asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None)) == []
    assert _respond_operation_count(composer_test_client, session_id) == 0
    chosen = turn["payload"]["options"][0]["id"]
    body = _live_body(turn, chosen=[chosen])

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert first.status_code == 200, first.json()
    first_json = first.json()
    assert first_json["guided_session"]["step"] == "step_1_source"
    assert first_json["next_turn"]["type"] == "schema_form"
    assert first_json["composition_state"]["sources"] == {}
    persisted_guided = first_json["composition_state"]["composer_meta"]["guided_session"]
    stable_id = next(iter(persisted_guided["pending_source_intents"]))
    assert str(UUID(stable_id)) == stable_id
    assert stable_id != body["operation_id"]
    assert stable_id != str(UUID(int=0))
    history = first_json["guided_session"]["history"]
    assert len(history) == 2
    assert history[0]["response_hash"] is not None
    assert history[1]["response_hash"] is None
    assert len(asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))) == 1
    audit_messages = asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    assert len(audit_messages) == 3
    events = _guided_audit_events(composer_test_client, session_id)
    assert [name for name, _arguments in events] == [
        "guided_turn_emitted",
        "guided_turn_answered",
        "guided_turn_emitted",
    ]
    current_emitted, answered, next_emitted = (arguments for _name, arguments in events)
    assert current_emitted["payload_hash"] == current_emitted["payload_payload_id"] == history[0]["payload_hash"]
    assert answered["response_hash"] == answered["response_payload_id"] == history[0]["response_hash"]
    assert next_emitted["payload_hash"] == next_emitted["payload_payload_id"] == history[1]["payload_hash"]
    stored_response = load_guided_json_payload(
        composer_test_client.app.state.payload_store,
        payload_id=history[0]["response_hash"],
        purpose="turn_response",
    )
    stored_next = load_guided_json_payload(
        composer_test_client.app.state.payload_store,
        payload_id=history[1]["payload_hash"],
        purpose="turn",
    )
    assert deep_thaw(stored_response.payload) == {"chosen": [chosen]}
    assert deep_thaw(stored_next.payload) == first_json["next_turn"]["payload"]

    monkeypatch.setattr(
        guided_route,
        "_build_get_guided_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replay consulted live turn builders")),
    )
    monkeypatch.setattr(
        guided_route,
        "_request_plugin_policy_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replay consulted mutable policy")),
    )
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert replay.status_code == 200, replay.json()
    assert replay.json() == first_json
    replay_guided = replay.json()["composition_state"]["composer_meta"]["guided_session"]
    assert next(iter(replay_guided["pending_source_intents"])) == stable_id
    assert _respond_operation_count(composer_test_client, session_id) == 1


def test_plural_source_selections_bind_each_intent_to_its_exact_blob_and_replay_first(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    earlier = composer_test_client.post(
        f"/api/sessions/{session_id}/blobs/inline",
        json={
            "filename": "earlier-source.csv",
            "content": "earlier_id,earlier_label\n1,first\n",
            "mime_type": "text/csv",
        },
    )
    assert earlier.status_code == 201, earlier.json()
    newer = composer_test_client.post(
        f"/api/sessions/{session_id}/blobs/inline",
        json={
            "filename": "newer-other-source.csv",
            "content": "newer_id,newer_label\n2,second\n",
            "mime_type": "text/csv",
        },
    )
    assert newer.status_code == 201, newer.json()
    fetched = composer_test_client.get(f"/api/sessions/{session_id}/guided")
    assert fetched.status_code == 200, fetched.json()
    turn = fetched.json()["next_turn"]
    body = _live_body(
        turn,
        chosen=["csv"],
        source_blob_id=earlier.json()["id"],
    )

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert first.status_code == 200, first.json()
    first_selection_json = first.json()
    first_prefilled = first_selection_json["next_turn"]["payload"]["prefilled"]
    assert first_prefilled["path"] == f"blob:{earlier.json()['id']}"
    assert first_prefilled["schema"]["fields"] == [
        "earlier_id: int",
        "earlier_label: str",
    ]
    persisted_guided = first_selection_json["composition_state"]["composer_meta"]["guided_session"]
    first_stable_id, intent = next(iter(persisted_guided["pending_source_intents"].items()))
    assert intent["inspection_facts"]["redacted_identity"]["blob_id"] == earlier.json()["id"]
    assert intent["inspection_facts"]["redacted_identity"]["blob_id"] != newer.json()["id"]
    response_payload = load_guided_json_payload(
        composer_test_client.app.state.payload_store,
        payload_id=first_selection_json["guided_session"]["history"][0]["response_hash"],
        purpose="turn_response",
    )
    assert deep_thaw(response_payload.payload) == {
        "chosen": ["csv"],
        "source_blob_id": earlier.json()["id"],
    }

    first_form = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            first_selection_json["next_turn"],
            edited_values={"plugin": "csv", "options": first_prefilled},
        ),
    )
    assert first_form.status_code == 200, first_form.json()
    assert first_form.json()["next_turn"]["type"] == "inspect_and_confirm"
    first_review = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(first_form.json()["next_turn"], edited_values={"columns": ["earlier_id", "earlier_label"]}),
    )
    assert first_review.status_code == 200, first_review.json()
    assert first_review.json()["next_turn"]["type"] == "review_components"

    added = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            first_review.json()["next_turn"],
            component_action={"action": "add", "component_kind": "source"},
        ),
    )
    assert added.status_code == 200, added.json()
    assert added.json()["next_turn"]["type"] == "single_select"

    second_selection = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            added.json()["next_turn"],
            chosen=["csv"],
            source_blob_id=newer.json()["id"],
        ),
    )
    assert second_selection.status_code == 200, second_selection.json()
    plural_guided = second_selection.json()["composition_state"]["composer_meta"]["guided_session"]
    assert plural_guided["source_order"][0] == first_stable_id
    assert plural_guided["reviewed_sources"][first_stable_id]["options"]["path"] == f"blob:{earlier.json()['id']}"
    second_stable_id, second_intent = next(iter(plural_guided["pending_source_intents"].items()))
    assert second_stable_id != first_stable_id
    assert second_intent["inspection_facts"]["redacted_identity"]["blob_id"] == newer.json()["id"]

    monkeypatch.setattr(
        guided_route,
        "inspect_selected_ready_session_blob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replay re-read mutable blob state")),
    )

    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert replay.status_code == 200, replay.json()
    assert replay.json() == first_selection_json
    replay_intents = replay.json()["composition_state"]["composer_meta"]["guided_session"]["pending_source_intents"]
    assert replay_intents[first_stable_id]["inspection_facts"]["redacted_identity"]["blob_id"] == earlier.json()["id"]


def test_source_selection_without_blob_identity_does_not_fall_back_to_latest_upload(
    composer_test_client: TestClient,
) -> None:
    session_id = _create_session(composer_test_client)
    for filename, content in (
        ("earlier.csv", "earlier_id\n1\n"),
        ("newer.csv", "newer_id\n2\n"),
    ):
        upload = composer_test_client.post(
            f"/api/sessions/{session_id}/blobs/inline",
            json={"filename": filename, "content": content, "mime_type": "text/csv"},
        )
        assert upload.status_code == 201, upload.json()

    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(turn, chosen=["csv"]),
    )

    assert response.status_code == 200, response.json()
    assert response.json()["next_turn"]["payload"]["prefilled"] == {"schema": {"mode": "observed"}}
    intents = response.json()["composition_state"]["composer_meta"]["guided_session"]["pending_source_intents"]
    assert next(iter(intents.values()))["inspection_facts"] is None


def test_non_file_source_without_blob_identity_ignores_one_unrelated_ready_blob(
    composer_test_client: TestClient,
) -> None:
    _enable_non_file_source(composer_test_client)
    session_id = _create_session(composer_test_client)
    uploaded = composer_test_client.post(
        f"/api/sessions/{session_id}/blobs/inline",
        json={"filename": "unrelated.csv", "content": "id\n1\n", "mime_type": "text/csv"},
    )
    assert uploaded.status_code == 201, uploaded.json()
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    assert _NON_FILE_SOURCE in {option["id"] for option in turn["payload"]["options"]}

    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(turn, chosen=[_NON_FILE_SOURCE]),
    )

    assert response.status_code == 200, response.json()
    assert response.json()["next_turn"]["payload"]["plugin"] == _NON_FILE_SOURCE
    intents = response.json()["composition_state"]["composer_meta"]["guided_session"]["pending_source_intents"]
    assert next(iter(intents.values()))["inspection_facts"] is None


def test_non_file_source_rejects_explicit_incompatible_blob_before_reservation(
    composer_test_client: TestClient,
) -> None:
    _enable_non_file_source(composer_test_client)
    session_id = _create_session(composer_test_client)
    uploaded = composer_test_client.post(
        f"/api/sessions/{session_id}/blobs/inline",
        json={"filename": "unrelated.csv", "content": "id\n1\n", "mime_type": "text/csv"},
    )
    assert uploaded.status_code == 201, uploaded.json()
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    assert _NON_FILE_SOURCE in {option["id"] for option in turn["payload"]["options"]}

    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            turn,
            chosen=[_NON_FILE_SOURCE],
            source_blob_id=uploaded.json()["id"],
        ),
    )

    assert response.status_code == 400, response.json()
    assert response.json()["detail"] == "source_blob_id is not valid for the selected source plugin."
    assert _respond_operation_count(composer_test_client, session_id) == 0


def test_source_selection_rejects_blob_identity_outside_ready_session_set_before_reservation(
    composer_test_client: TestClient,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(turn, chosen=["csv"], source_blob_id=str(uuid4())),
    )

    assert response.status_code == 400, response.json()
    assert response.json()["detail"] == "Selected source blob is not a ready upload for this session."
    assert _respond_operation_count(composer_test_client, session_id) == 0


@pytest.mark.parametrize("failure_kind", ("deleted", "not_ready"))
def test_source_selection_maps_get_to_read_lifecycle_drift_before_reservation(
    composer_test_client: TestClient,
    failure_kind: str,
) -> None:
    # Renamed from ...maps_list_to_read...: an explicit selection now
    # resolves via a direct get_blob lookup, not list_blobs + Python
    # filter (see inspect_selected_ready_session_blob). The invariant this
    # test pins — a blob that goes missing/not-ready between resolution and
    # read raises SourceInspectionBlobLifecycleError -> 400 — is unchanged.
    session_id = _create_session(composer_test_client)
    uploaded = composer_test_client.post(
        f"/api/sessions/{session_id}/blobs/inline",
        json={"filename": "racing.csv", "content": "id\n1\n", "mime_type": "text/csv"},
    )
    assert uploaded.status_code == 201, uploaded.json()
    blob_id = UUID(uploaded.json()["id"])
    original_blob_service = composer_test_client.app.state.blob_service
    record = asyncio.run(original_blob_service.get_blob(blob_id))
    secret_canary = "private-race-detail"
    failure: Exception
    if failure_kind == "deleted":
        failure = BlobNotFoundError(f"{blob_id}-{secret_canary}")
    else:
        failure = BlobStateError(str(blob_id), message=f"not ready: {secret_canary}")
    composer_test_client.app.state.blob_service = _ReadRaceBlobService(record, failure)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    try:
        response = composer_test_client.post(
            f"/api/sessions/{session_id}/guided/respond",
            json=_live_body(turn, chosen=["csv"], source_blob_id=str(blob_id)),
        )
    finally:
        composer_test_client.app.state.blob_service = original_blob_service

    assert response.status_code == 400, response.json()
    assert response.json()["detail"] == "Selected source blob is no longer a ready upload for this session."
    assert secret_canary not in response.text
    assert _respond_operation_count(composer_test_client, session_id) == 0


def test_source_selection_fails_closed_when_read_bytes_do_not_match_fetched_hash(
    composer_test_client: TestClient,
) -> None:
    # Renamed from ...match_listed_hash...: the record's content_hash now
    # comes from get_blob rather than list_blobs. The invariant this test
    # pins is unchanged and remains load-bearing: source_inspection.py's own
    # hash re-verification against `record.content_hash` is not redundant
    # with a compliant BlobServiceProtocol implementation's internal check —
    # it independently certifies the content_hash_prefix this module stamps
    # into redacted_identity, catching a case (simulated here by a fake that
    # skips its own verification) where the bytes returned by read_blob_content
    # don't match the hash on the record snapshot this module already holds.
    session_id = _create_session(composer_test_client)
    uploaded = composer_test_client.post(
        f"/api/sessions/{session_id}/blobs/inline",
        json={"filename": "stable.csv", "content": "listed_id\n1\n", "mime_type": "text/csv"},
    )
    assert uploaded.status_code == 201, uploaded.json()
    blob_id = UUID(uploaded.json()["id"])
    original_blob_service = composer_test_client.app.state.blob_service
    record = asyncio.run(original_blob_service.get_blob(blob_id))
    composer_test_client.app.state.blob_service = _ReadRaceBlobService(record, b"changed_id\n2\n")
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]

    try:
        response = composer_test_client.post(
            f"/api/sessions/{session_id}/guided/respond",
            json=_live_body(turn, chosen=["csv"], source_blob_id=str(blob_id)),
        )
    finally:
        composer_test_client.app.state.blob_service = original_blob_service

    assert response.status_code == 500, response.json()
    assert response.json()["detail"] == {
        "error_type": "server_invariant_violated",
        "detail": "Server invariant violated. See application audit log for diagnostic detail.",
    }
    assert _respond_operation_count(composer_test_client, session_id) == 0


@pytest.mark.parametrize(
    "action_kind",
    ("review_wiring", "reject", "component_revise", "prose_revise"),
)
def test_step3_closed_actions_reject_source_blob_identity_before_reservation(
    composer_test_client: TestClient,
    action_kind: str,
) -> None:
    session_id, staged = _stage_proposal(composer_test_client, filename=f"illegal-{action_kind}.jsonl")
    turn = staged["next_turn"]
    payload = turn["payload"]
    operation_id = str(uuid4())
    body = _live_body(
        turn,
        operation_id=operation_id,
        proposal_id=payload["proposal_id"],
        draft_hash=payload["draft_hash"],
        source_blob_id=str(uuid4()),
    )
    if action_kind == "review_wiring":
        body["chosen"] = ["review_wiring"]
    elif action_kind == "reject":
        body["control_signal"] = "reject"
    elif action_kind == "component_revise":
        body["edit_target"] = payload["edit_targets"][0]
    else:
        body["edited_values"] = {"revision_instruction": "Add one audited transform."}
    operation_count_before = _respond_operation_count(composer_test_client, session_id)

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == 400, response.json()
    assert response.json()["detail"] == "source_blob_id is only valid for a Step 1 source selection."
    assert _respond_operation_count(composer_test_client, session_id) == operation_count_before


@pytest.mark.parametrize("action_kind", ("confirm_wiring", "correction"))
def test_step4_closed_actions_reject_source_blob_identity_before_reservation(
    composer_test_client: TestClient,
    action_kind: str,
) -> None:
    session_id, staged = _stage_proposal(composer_test_client, filename=f"illegal-{action_kind}.jsonl")
    proposal_turn = staged["next_turn"]
    reviewed = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            proposal_turn,
            chosen=["review_wiring"],
            proposal_id=proposal_turn["payload"]["proposal_id"],
            draft_hash=proposal_turn["payload"]["draft_hash"],
        ),
    )
    assert reviewed.status_code == 200, reviewed.json()
    wire_turn = reviewed.json()["next_turn"]
    wire_payload = wire_turn["payload"]
    operation_id = str(uuid4())
    body = _live_body(
        wire_turn,
        operation_id=operation_id,
        proposal_id=wire_payload["proposal_id"],
        draft_hash=wire_payload["draft_hash"],
        source_blob_id=str(uuid4()),
    )
    if action_kind == "confirm_wiring":
        body["chosen"] = ["confirm_wiring"]
    else:
        body["correction_feedback"] = "Correct this source before confirmation."
        body["edit_target"] = {
            "kind": "source",
            "stable_id": wire_payload["sources"][0]["stable_id"],
        }
    operation_count_before = _respond_operation_count(composer_test_client, session_id)

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == 400, response.json()
    assert response.json()["detail"] == "source_blob_id is only valid for a Step 1 source selection."
    assert _respond_operation_count(composer_test_client, session_id) == operation_count_before


def test_preflight_and_settlement_share_one_server_identity_and_inspection_authority(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    facts = SourceInspectionFacts(
        source_kind="csv",
        redacted_identity={"filename": "authority.csv"},
        byte_range_inspected=(0, 32),
        sample_row_count=1,
        observed_headers=("record_id",),
        inferred_types={"record_id": "int"},
        url_candidates=(),
        warnings=(),
    )
    original_answer = guided_route._schema8_answer_and_project_next
    captured: list[tuple[UUID, SourceInspectionFacts | None]] = []

    def capture_authority(*args: object, **kwargs: object) -> object:
        captured.append((kwargs["new_stable_id"], kwargs["source_inspection_facts"]))
        return original_answer(*args, **kwargs)

    inspection_calls = 0

    async def inspect_once(*_args: object, **_kwargs: object) -> SourceInspectionFacts:
        nonlocal inspection_calls
        inspection_calls += 1
        if inspection_calls > 1:
            raise AssertionError("settlement re-read mutable inspection authority")
        return facts

    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", capture_authority)
    monkeypatch.setattr(guided_route, "inspect_selected_ready_session_blob", inspect_once)

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == 200, response.json()
    assert inspection_calls == 1
    assert len(captured) == 2
    assert captured[0] == captured[1]
    stable_id, captured_facts = captured[0]
    assert captured_facts is facts
    assert stable_id != UUID(body["operation_id"])
    assert stable_id != UUID(int=0)
    persisted_guided = response.json()["composition_state"]["composer_meta"]["guided_session"]
    assert next(iter(persisted_guided["pending_source_intents"])) == str(stable_id)


@pytest.mark.parametrize(
    ("body_change", "expected_status"),
    [
        ({"turn_token": "0" * 64}, 409),
        ({"chosen": ["csv"], "edited_values": {"plugin": "csv", "options": {}}}, 400),
        ({"chosen": ["not-server-permitted"]}, 400),
        ({"proposal_id": "not-canonical", "draft_hash": "a" * 64}, 400),
        ({"proposal_id": str(uuid4()), "draft_hash": "A" * 64}, 400),
        ({"chosen": ["csv"], "proposal_id": str(uuid4()), "draft_hash": "a" * 64}, 409),
        (
            {
                "chosen": ["csv"],
                "proposal_id": str(uuid4()),
                "draft_hash": "a" * 64,
                "edit_target": {"kind": "source", "stable_id": "not-a-uuid"},
            },
            400,
        ),
    ],
)
def test_invalid_live_response_never_reserves_an_operation(
    composer_test_client: TestClient,
    body_change: dict[str, object],
    expected_status: int,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = _start(composer_test_client, session_id)["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    body.update(body_change)
    versions_before = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))
    messages_before = asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    payloads_before = _payload_file_count(composer_test_client)

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == expected_status
    assert _respond_operation_count(composer_test_client, session_id) == 0
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == versions_before
    assert asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None)) == messages_before
    assert _payload_file_count(composer_test_client) == payloads_before


def test_step3_rejects_mismatched_proposal_binding_before_stage_dispatch_or_reservation(
    composer_test_client: TestClient,
) -> None:
    session_id = _create_session(composer_test_client)
    proposal_id = uuid4()
    draft_hash = "b" * 64
    initial = _initial_composition_state_with_guided_session().guided_session
    assert initial is not None
    active = GuidedProposalRef(
        proposal_id=proposal_id,
        draft_hash=draft_hash,
        base=AbsentBase(),
        reviewed_anchor_hash=guided_reviewed_anchor_hash(
            source_order=(),
            reviewed_sources={},
            output_order=(),
            reviewed_outputs={},
        ),
        covered_deferred_intent_ids=(),
        creation_event_schema="pipeline_proposal_created.v1",
    )
    guided = _with_active_step3(initial, active)
    _persist_guided(composer_test_client, session_id, guided)
    versions_before = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))

    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": "a" * 64,
            "chosen": ["accept"],
            "proposal_id": str(proposal_id),
            "draft_hash": "c" * 64,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "proposal_id and draft_hash do not identify the active guided proposal"
    assert _respond_operation_count(composer_test_client, session_id) == 0
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == versions_before


def test_step3_matching_proposal_binding_requires_durable_payload_before_reservation(
    composer_test_client: TestClient,
) -> None:
    session_id = _create_session(composer_test_client)
    proposal_id = uuid4()
    draft_hash = "b" * 64
    initial = _initial_composition_state_with_guided_session().guided_session
    assert initial is not None
    active = GuidedProposalRef(
        proposal_id=proposal_id,
        draft_hash=draft_hash,
        base=AbsentBase(),
        reviewed_anchor_hash=guided_reviewed_anchor_hash(
            source_order=(),
            reviewed_sources={},
            output_order=(),
            reviewed_outputs={},
        ),
        covered_deferred_intent_ids=(),
        creation_event_schema="pipeline_proposal_created.v1",
    )
    guided = _with_active_step3(initial, active)
    _persist_guided(composer_test_client, session_id, guided)

    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": "a" * 64,
            "chosen": ["review_wiring"],
            "proposal_id": str(proposal_id),
            "draft_hash": draft_hash,
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "error_type": "server_invariant_violated",
        "detail": "Server invariant violated. See application audit log for diagnostic detail.",
    }
    assert _respond_operation_count(composer_test_client, session_id) == 0


def test_step3_active_proposal_requires_binding_for_every_non_exit_action(
    composer_test_client: TestClient,
) -> None:
    session_id = _create_session(composer_test_client)
    initial = _initial_composition_state_with_guided_session().guided_session
    assert initial is not None
    active = GuidedProposalRef(
        proposal_id=uuid4(),
        draft_hash="b" * 64,
        base=AbsentBase(),
        reviewed_anchor_hash=guided_reviewed_anchor_hash(
            source_order=(),
            reviewed_sources={},
            output_order=(),
            reviewed_outputs={},
        ),
        covered_deferred_intent_ids=(),
        creation_event_schema="pipeline_proposal_created.v1",
    )
    guided = _with_active_step3(initial, active)
    _persist_guided(composer_test_client, session_id, guided)

    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json={
            "operation_id": str(uuid4()),
            "turn_token": "a" * 64,
            "chosen": ["accept"],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "the active guided proposal requires proposal_id and draft_hash"
    assert _respond_operation_count(composer_test_client, session_id) == 0


def test_active_proposal_binding_gate_preserves_unbound_exit() -> None:
    initial = _initial_composition_state_with_guided_session().guided_session
    assert initial is not None
    active = GuidedProposalRef(
        proposal_id=uuid4(),
        draft_hash="b" * 64,
        base=AbsentBase(),
        reviewed_anchor_hash=guided_reviewed_anchor_hash(
            source_order=(),
            reviewed_sources={},
            output_order=(),
            reviewed_outputs={},
        ),
        covered_deferred_intent_ids=(),
        creation_event_schema="pipeline_proposal_created.v1",
    )
    guided = _with_active_step3(initial, active)
    body = GuidedRespondRequest.model_validate(
        {
            "operation_id": str(uuid4()),
            "turn_token": "a" * 64,
            "control_signal": "exit_to_freeform",
        }
    )

    guided_route._verify_schema8_proposal_binding(guided, body)


def test_completed_operation_id_reused_with_different_body_conflicts_without_mutation(
    composer_test_client: TestClient,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)
    assert first.status_code == 200, first.json()
    versions_before = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))
    messages_before = asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    payloads_before = _payload_file_count(composer_test_client)
    conflicting = {**body, "chosen": ["different-request"]}

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=conflicting)

    assert response.status_code == 409
    assert response.json()["detail"] == "Operation id is already bound to a different request."
    assert _respond_operation_count(composer_test_client, session_id) == 1
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == versions_before
    assert asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None)) == messages_before
    assert _payload_file_count(composer_test_client) == payloads_before


def test_schema_form_plugin_mismatch_never_selects_a_client_named_model_or_reserves(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    selected = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(turn, chosen=[turn["payload"]["options"][0]["id"]]),
    )
    assert selected.status_code == 200, selected.json()
    form_turn = selected.json()["next_turn"]
    operations_before = _respond_operation_count(composer_test_client, session_id)
    versions_before = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))

    def explode_if_client_selects_model(_plugin: str) -> object:
        raise AssertionError("client plugin selected the config model")

    monkeypatch.setattr(guided_route, "get_source_config_model", explode_if_client_selects_model)
    response = composer_test_client.post(
        f"/api/sessions/{session_id}/guided/respond",
        json=_live_body(
            form_turn,
            edited_values={
                "plugin": "json",
                "options": form_turn["payload"]["prefilled"],
            },
        ),
    )

    assert response.status_code == 400, response.json()
    assert response.json()["detail"] == "Guided response does not satisfy the current turn contract."
    assert _respond_operation_count(composer_test_client, session_id) == operations_before
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == versions_before


def test_unsupported_guided_selection_never_reaches_operator_logs(
    composer_test_client: TestClient,
) -> None:
    from structlog.testing import capture_logs

    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    canary = "raw-guided-selection-canary-7f3a9d"

    with capture_logs() as logs:
        response = composer_test_client.post(
            f"/api/sessions/{session_id}/guided/respond",
            json=_live_body(turn, chosen=[canary]),
        )

    assert response.status_code == 400, response.json()
    rejection = next(entry for entry in logs if entry["event"] == "guided.respond_turn_contract_rejected")
    assert rejection["rejection_code"] == "invalid_guided_response"
    assert rejection["exc_class"] == "ValueError"
    assert "error_detail" not in rejection
    # The generic branch must log the class ONLY — its messages echo raw
    # client-supplied values, and the audit scrubber redacts secret-shaped
    # text, not arbitrary client text.
    assert "exc_message" not in rejection
    assert canary not in repr(logs)


def test_web_surface_policy_rejection_reaches_operator_logs_with_distinct_code(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S7: a deployment-policy refusal must be diagnosable from the log alone.

    The generic contract-rejection branch logs the exception class only (see
    the canary test above), which left a policy refusal indistinguishable
    from any authoring mistake — "invalid_guided_response ValueError" — with
    the policy explanation dropped on the floor. The policy error's message
    is server-composed end to end, so its branch logs the explanation under
    its own closed rejection code. The client response body stays the closed
    generic 400 either way.
    """
    from structlog.testing import capture_logs

    from elspeth.web.composer.guided.stage_transitions import WebSurfacePolicyRejectedError
    from elspeth.web.sessions.routes.composer import guided as guided_route

    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    explanation = "source plugin 'aws_s3' is prohibited on the web authoring surface: policy-explanation-sentinel"

    def reject(*_args: object, **_kwargs: object) -> None:
        raise WebSurfacePolicyRejectedError(explanation)

    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", reject)

    with capture_logs() as logs:
        response = composer_test_client.post(
            f"/api/sessions/{session_id}/guided/respond",
            json=_live_body(turn, chosen=["csv"]),
        )

    assert response.status_code == 400, response.json()
    assert response.json()["detail"] == "Guided response does not satisfy the current turn contract."
    assert "policy-explanation-sentinel" not in response.text
    rejection = next(entry for entry in logs if entry["event"] == "guided.respond_turn_contract_rejected")
    assert rejection["rejection_code"] == "web_surface_policy_rejected"
    assert rejection["exc_class"] == "WebSurfacePolicyRejectedError"
    assert "policy-explanation-sentinel" in rejection["exc_message"]


def test_expired_operation_is_not_taken_over_before_live_preflight(
    composer_test_client: TestClient,
) -> None:
    from elspeth.web.sessions.guided_operations import guided_operation_request_hash
    from elspeth.web.sessions.protocol import GuidedOperationClaimed

    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    body["turn_token"] = "0" * 64
    request_model = guided_route.GuidedRespondRequest.model_validate(body, strict=True)
    service = composer_test_client.app.state.session_service
    claim = asyncio.run(
        service.reserve_guided_operation(
            session_id=UUID(session_id),
            operation_id=body["operation_id"],
            kind="guided_respond",
            request_hash=guided_operation_request_hash(
                session_id=UUID(session_id),
                kind="guided_respond",
                request=request_model,
            ),
            actor="composer_route",
            lease_seconds=300,
        )
    )
    assert isinstance(claim, GuidedOperationClaimed)
    with composer_test_client.app.state.session_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE guided_operations SET lease_expires_at = :expired WHERE session_id = :session_id AND operation_id = :operation_id"
            ),
            {
                "expired": datetime.now(UTC) - timedelta(seconds=1),
                "session_id": session_id,
                "operation_id": body["operation_id"],
            },
        )

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == 409, response.json()
    with composer_test_client.app.state.session_engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == body["operation_id"],
                )
            )
            .mappings()
            .one()
        )
    assert operation["attempt"] == 1
    assert operation["status"] == "in_progress"


def test_preflight_invariant_is_sanitized_without_reservation_or_mutation(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    secret_canary = "/private/operator/preflight-secret.csv"
    versions_before = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))
    messages_before = asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    payloads_before = _payload_file_count(composer_test_client)

    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise InvariantError(secret_canary)

    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", fail_preflight)
    with capture_logs() as logs:
        response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "error_type": "server_invariant_violated",
        "detail": "Server invariant violated. See application audit log for diagnostic detail.",
    }
    assert secret_canary not in response.text
    entry = next(log for log in logs if log["event"] == "guided.invariant_violated")
    assert entry["exc_class"] == "InvariantError"
    assert entry["site"] == "post_guided_respond.preflight"
    assert entry["frames"]
    assert secret_canary not in repr(logs)
    assert _respond_operation_count(composer_test_client, session_id) == 0
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == versions_before
    assert asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None)) == messages_before
    assert _payload_file_count(composer_test_client) == payloads_before


@pytest.mark.parametrize(
    ("step", "turn"),
    [
        (
            GuidedStep.STEP_4_WIRE,
            {
                "type": TurnType.CONFIRM_WIRING.value,
                "step_index": 3,
                "payload": {
                    "proposal_id": "00000000-0000-4000-8000-000000000001",
                    "draft_hash": "d" * 64,
                    "sources": [],
                    "nodes": [],
                    "outputs": [],
                    "connections": [],
                    "semantic_contracts": [],
                    "warnings": [],
                    "blockers": [],
                    "can_confirm": True,
                },
            },
        ),
    ],
)
def test_orphan_wire_occurrence_fails_closed_before_reservation_or_mutation(
    composer_test_client: TestClient,
    step: GuidedStep,
    turn: dict[str, object],
) -> None:
    session_id = _create_session(composer_test_client)
    guided = replace(GuidedSession.initial(), step=step)
    guided, _record, _turn_type, _prepared = guided_route._prepare_server_turn_occurrence(
        guided,
        current_step=guided.step,
        turn=turn,
        payload_store=composer_test_client.app.state.payload_store,
    )
    _persist_guided(composer_test_client, session_id, guided)
    versions_before = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))
    messages_before = asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    payloads_before = _payload_file_count(composer_test_client)
    body = _live_body(
        {"turn_token": guided_turn_token(guided)},
        chosen=["confirm_wiring"],
    )

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "error_type": "server_invariant_violated",
        "detail": "Server invariant violated. See application audit log for diagnostic detail.",
    }
    assert _respond_operation_count(composer_test_client, session_id) == 0
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == versions_before
    assert asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None)) == messages_before
    assert _payload_file_count(composer_test_client) == payloads_before


def test_completed_terminal_exit_with_null_token_is_retry_safe(composer_test_client: TestClient) -> None:
    session_id = _create_session(composer_test_client)
    completed = replace(
        GuidedSession.initial(),
        terminal=TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml="pipeline: {}"),
    )
    _persist_guided(composer_test_client, session_id, completed)
    body = {
        "operation_id": str(uuid4()),
        "turn_token": None,
        "chosen": None,
        "edited_values": None,
        "custom_inputs": None,
        "control_signal": "exit_to_freeform",
        "proposal_id": None,
        "draft_hash": None,
        "edit_target": None,
    }

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["terminal"]["kind"] == "exited_to_freeform"
    assert first.json()["next_turn"] is None
    assert _respond_operation_count(composer_test_client, session_id) == 1
    assert len(asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))) == 1


@pytest.mark.parametrize(
    ("terminal", "response_fields"),
    [
        (
            TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml="pipeline: {}"),
            {"chosen": ["csv"]},
        ),
        (
            TerminalState(
                kind=TerminalKind.EXITED_TO_FREEFORM,
                reason=TerminalReason.USER_PRESSED_EXIT,
                pipeline_yaml=None,
            ),
            {"control_signal": "exit_to_freeform"},
        ),
        (
            TerminalState(
                kind=TerminalKind.EXITED_TO_FREEFORM,
                reason=TerminalReason.USER_PRESSED_EXIT,
                pipeline_yaml=None,
            ),
            {"chosen": ["csv"]},
        ),
    ],
)
def test_terminal_new_operation_is_rejected_without_reservation(
    composer_test_client: TestClient,
    terminal: TerminalState,
    response_fields: dict[str, object],
) -> None:
    session_id = _create_session(composer_test_client)
    _persist_guided(composer_test_client, session_id, replace(GuidedSession.initial(), terminal=terminal))
    versions_before = asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))
    messages_before = asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))
    payloads_before = _payload_file_count(composer_test_client)
    token = None if response_fields.get("control_signal") == "exit_to_freeform" else "a" * 64
    body = _live_body({"turn_token": token}, **response_fields)

    response = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert response.status_code == 409
    assert response.json()["detail"] == "Guided session is already terminal."
    assert _respond_operation_count(composer_test_client, session_id) == 0
    assert asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id))) == versions_before
    assert asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None)) == messages_before
    assert _payload_file_count(composer_test_client) == payloads_before


def test_live_exit_answers_exact_turn_and_atomically_drops_to_freeform(composer_test_client: TestClient) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, control_signal="exit_to_freeform")

    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)
    replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["terminal"] == {
        "kind": "exited_to_freeform",
        "reason": "user_pressed_exit",
        "pipeline_yaml": None,
    }
    assert first.json()["next_turn"] is None
    assert first.json()["guided_session"]["history"][-1]["response_hash"] is not None
    assert len(asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))) == 3


def test_respond_handler_has_no_legacy_or_unfenced_mutation_calls() -> None:
    tree = ast.parse(inspect.getsource(guided_route.post_guided_respond))
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    attributes = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}

    assert calls.isdisjoint(
        {
            "RecoveredPipelineCommit",
            "_persist_tool_invocations",
            "step_advance",
            "_dispatch_guided_respond",
            "_append_server_turn_record",
            "_store_guided_audit_payload",
            "stable_hash",
        }
    )
    assert attributes.isdisjoint(
        {
            "get_pipeline_dispatch_recovery",
            "save_composition_state",
            "_persist_tool_invocations",
            "_persist_llm_calls",
        }
    )
    assert {"reserve_or_replay_guided_operation", "GuidedStateOperationCommand"} <= calls
    assert {"renew_guided_operation", "settle_guided_state_operation"} <= attributes


def test_respond_sink_preflight_uses_owned_session_record_for_path_namespace() -> None:
    """Filesystem scope must come from the ownership-verified record, not route input."""
    tree = ast.parse(inspect.getsource(guided_route.post_guided_respond))
    ownership_assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and isinstance(node.value.value.func, ast.Name)
        and node.value.value.func.id == "_verify_session_ownership"
    ]

    assert len(ownership_assignments) == 1
    assert [ast.unparse(target) for target in ownership_assignments[0].targets] == ["owned_session"]

    sink_preflight_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_schema8_require_runnable_sink_form"
    ]
    assert len(sink_preflight_calls) == 1
    session_keyword = next(keyword for keyword in sink_preflight_calls[0].keywords if keyword.arg == "session_id")
    assert ast.unparse(session_keyword.value) == "str(owned_session.id)"


def test_respond_settlement_shares_chat_lock_and_never_polls_under_it() -> None:
    from elspeth.web.sessions.routes.composer import guided_chat_atomic

    tree = ast.parse(inspect.getsource(guided_route.post_guided_respond))
    route = next(node for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)))
    chat_tree = ast.parse(inspect.getsource(guided_chat_atomic.post_guided_chat_schema8))
    chat_route = next(node for node in ast.walk(chat_tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)))
    offenders: list[int] = []
    settlement_under_compose = False
    for node in ast.walk(route):
        if not isinstance(node, ast.AsyncWith):
            continue
        holds_compose_lock = any(isinstance(item.context_expr, ast.Name) and item.context_expr.id == "compose_lock" for item in node.items)
        if not holds_compose_lock:
            continue
        for descendant in ast.walk(node):
            if (
                isinstance(descendant, ast.Call)
                and isinstance(descendant.func, ast.Attribute)
                and descendant.func.attr == "settle_guided_state_operation"
            ):
                settlement_under_compose = True
            if (
                isinstance(descendant, ast.Call)
                and isinstance(descendant.func, ast.Name)
                and descendant.func.id == "reserve_or_replay_guided_operation"
            ):
                offenders.append(descendant.lineno)

    def compose_lock_key(handler: ast.AsyncFunctionDef | ast.FunctionDef) -> str:
        assignment = next(
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "compose_lock" for target in node.targets)
        )
        assert isinstance(assignment.value, ast.Await)
        call = assignment.value.value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        assert call.func.attr == "get_lock"
        return ast.unparse(call.args[0])

    assert offenders == []
    assert settlement_under_compose
    assert compose_lock_key(route) == compose_lock_key(chat_route) == "str(session_id)"


def test_only_one_schema8_respond_route_is_registered(composer_test_client: TestClient) -> None:
    routes = [
        route
        for route in composer_test_client.app.routes
        if getattr(route, "path", None) == "/api/sessions/{session_id}/guided/respond" and "POST" in getattr(route, "methods", set())
    ]
    assert len(routes) == 1
    session_id = _create_session(composer_test_client)
    assert composer_test_client.post(f"/api/sessions/{session_id}/guided/respond-legacy-disabled", json={}).status_code == 404


def test_same_operation_concurrent_callers_join_one_exact_result(file_composer_test_client: TestClient) -> None:
    composer_test_client = file_composer_test_client
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])

    async def race() -> list[tuple[int, dict]]:
        async with AsyncClient(
            transport=ASGITransport(app=composer_test_client.app),
            base_url="http://test",
        ) as client:
            responses = await asyncio.gather(
                client.post(f"/api/sessions/{session_id}/guided/respond", json=body),
                client.post(f"/api/sessions/{session_id}/guided/respond", json=body),
            )
            return [(response.status_code, response.json()) for response in responses]

    results = asyncio.run(race())

    assert [status for status, _body in results] == [200, 200]
    assert results[0][1] == results[1][1]
    assert _respond_operation_count(composer_test_client, session_id) == 1
    assert len(asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))) == 1
    assert len(asyncio.run(composer_test_client.app.state.session_service.get_messages(UUID(session_id), limit=None))) == 3


def test_active_same_operation_join_does_not_block_local_winner(
    file_composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = file_composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    service = client.app.state.session_service
    original_settle = service.settle_guided_state_operation
    original_get = service.get_guided_operation

    async def race() -> list[object]:
        from elspeth.web.sessions.protocol import GuidedOperationActive

        settlement_entered = asyncio.Event()
        join_observed_active = asyncio.Event()
        allow_settlement = asyncio.Event()

        async def settle(*args: object, **kwargs: object) -> object:
            settlement_entered.set()
            await allow_settlement.wait()
            return await original_settle(*args, **kwargs)

        async def get_operation(*args: object, **kwargs: object) -> object:
            outcome = await original_get(*args, **kwargs)
            if isinstance(outcome, GuidedOperationActive):
                join_observed_active.set()
            return outcome

        monkeypatch.setattr(service, "settle_guided_state_operation", settle)
        monkeypatch.setattr(service, "get_guided_operation", get_operation)
        async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as async_client:
            winner = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/respond", json=body))
            await asyncio.wait_for(settlement_entered.wait(), timeout=3)
            joiner = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/respond", json=body))
            await asyncio.wait_for(join_observed_active.wait(), timeout=3)
            allow_settlement.set()
            return list(await asyncio.wait_for(asyncio.gather(winner, joiner), timeout=5))

    winner_response, joined_response = asyncio.run(race())

    assert winner_response.status_code == joined_response.status_code == 200
    assert winner_response.json() == joined_response.json()
    assert _respond_operation_count(client, session_id) == 1


def test_competing_operations_for_one_token_have_one_winner_and_no_loser_reservation(
    file_composer_test_client: TestClient,
) -> None:
    composer_test_client = file_composer_test_client
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    chosen = [turn["payload"]["options"][0]["id"]]
    bodies = [_live_body(turn, chosen=chosen), _live_body(turn, chosen=chosen)]

    async def race() -> list[int]:
        async with AsyncClient(
            transport=ASGITransport(app=composer_test_client.app),
            base_url="http://test",
        ) as client:
            responses = await asyncio.gather(*(client.post(f"/api/sessions/{session_id}/guided/respond", json=body) for body in bodies))
            return [response.status_code for response in responses]

    statuses = asyncio.run(race())

    assert sorted(statuses) == [200, 409]
    assert _respond_operation_count(composer_test_client, session_id) == 1
    assert len(asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))) == 1


@pytest.mark.parametrize("failure_mode", ["missing", "corrupt"])
def test_exact_replay_fails_closed_when_next_turn_cas_is_unavailable(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)
    assert first.status_code == 200
    store = composer_test_client.app.state.payload_store

    def broken_retrieve(_store: object, content_hash: str) -> bytes:
        if failure_mode == "missing":
            raise PayloadNotFoundError(content_hash)
        return b"{}"

    monkeypatch.setattr(type(store), "retrieve", broken_retrieve)
    with pytest.raises(AuditIntegrityError, match=r"unavailable or corrupt|bytes do not match"):
        composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert _respond_operation_count(composer_test_client, session_id) == 1
    assert len(asyncio.run(composer_test_client.app.state.session_service.get_state_versions(UUID(session_id)))) == 1


@pytest.mark.parametrize(
    ("failure_type", "expected_failure_code"),
    [
        (RuntimeError, "operation_failed"),
        (AuditIntegrityError, "integrity_error"),
    ],
)
def test_settlement_failure_rolls_back_state_and_evidence_and_returns_only_safe_typed_failure(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_type: type[Exception],
    expected_failure_code: str,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    secret_canary = "/private/operator/settlement-secret.csv"
    service = composer_test_client.app.state.session_service
    caplog.set_level("DEBUG")

    async def fail_settlement(*_args: object, **_kwargs: object) -> None:
        raise failure_type(secret_canary)

    monkeypatch.setattr(service, "settle_guided_state_operation", fail_settlement)
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)
        replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert first.status_code == replay.status_code == 500
    assert first.json() == replay.json()
    assert first.json()["detail"]["error_type"] == "guided_operation_terminal_failure"
    assert first.json()["detail"]["failure_code"] == expected_failure_code
    assert secret_canary not in first.text
    assert secret_canary not in caplog.text
    failure_log = next(entry for entry in logs if entry["event"] == "guided.operation_terminal_failure")
    assert failure_log["exc_class"] == failure_type.__name__
    assert failure_log["site"] == "post_guided_respond"
    assert failure_log["frames"]
    # R2-F16b: the correlation field is always emitted (None here — this app
    # carries no RequestIdMiddleware).
    assert "request_id" in failure_log
    assert secret_canary not in repr(logs)
    assert asyncio.run(service.get_state_versions(UUID(session_id))) == []
    assert asyncio.run(service.get_messages(UUID(session_id), limit=None)) == []
    assert _respond_operation_count(composer_test_client, session_id) == 1
    with composer_test_client.app.state.session_engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == body["operation_id"],
                )
            )
            .mappings()
            .one()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == expected_failure_code
    assert operation["lease_token"] is None
    assert operation["lease_expires_at"] is None
    assert operation["result_kind"] is None
    assert operation["result_state_id"] is None
    assert operation["response_hash"] is None
    assert secret_canary not in str(dict(operation))
    secret_bytes = secret_canary.encode()
    assert all(
        secret_bytes not in path.read_bytes()
        for path in composer_test_client.app.state.payload_store.base_path.rglob("*")
        if path.is_file()
    )


def test_settlement_recheck_invariant_is_typed_and_replays_without_secret_leak(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    secret_canary = "/private/operator/recheck-secret.csv"
    service = composer_test_client.app.state.session_service
    original_transition = guided_route._schema8_answer_and_project_next
    calls = 0

    def fail_only_recheck(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InvariantError(secret_canary)
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(guided_route, "_schema8_answer_and_project_next", fail_only_recheck)
    with capture_logs() as logs:
        first = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)
        replay = composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert first.status_code == replay.status_code == 500
    assert replay.json() == first.json()
    assert first.json()["detail"]["failure_code"] == "integrity_error"
    assert secret_canary not in first.text
    entry = next(log for log in logs if log["event"] == "guided.operation_terminal_failure")
    assert entry["exc_class"] == "InvariantError"
    assert entry["site"] == "post_guided_respond"
    assert entry["frames"]
    assert secret_canary not in repr(logs)
    assert asyncio.run(service.get_state_versions(UUID(session_id))) == []
    assert asyncio.run(service.get_messages(UUID(session_id), limit=None)) == []
    assert _respond_operation_count(composer_test_client, session_id) == 1
    with composer_test_client.app.state.session_engine.connect() as connection:
        operation = (
            connection.execute(
                select(guided_operations_table).where(
                    guided_operations_table.c.session_id == session_id,
                    guided_operations_table.c.operation_id == body["operation_id"],
                )
            )
            .mappings()
            .one()
        )
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "integrity_error"
    assert secret_canary not in str(dict(operation))
    assert all(
        secret_canary.encode() not in path.read_bytes()
        for path in composer_test_client.app.state.payload_store.base_path.rglob("*")
        if path.is_file()
    )


def test_failure_handler_failure_raises_static_integrity_error_and_logs_only_safe_frames(
    composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = _create_session(composer_test_client)
    turn = composer_test_client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    service = composer_test_client.app.state.session_service
    settlement_canary = "/private/operator/settlement-original.csv"
    failure_canary = "/private/operator/failure-handler.csv"
    caplog.set_level("DEBUG")

    async def fail_settlement(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(settlement_canary)

    async def fail_failure_handler(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_canary)

    monkeypatch.setattr(service, "settle_guided_state_operation", fail_settlement)
    monkeypatch.setattr(service, "fail_guided_operation_with_audit", fail_failure_handler)

    from structlog.testing import capture_logs

    with (
        capture_logs() as logs,
        pytest.raises(AuditIntegrityError, match="Guided RESPOND could not record its terminal failure"),
    ):
        composer_test_client.post(f"/api/sessions/{session_id}/guided/respond", json=body)

    assert [entry["event"] for entry in logs] == [
        "guided.operation_terminal_failure",
        "guided.operation_failure_record_failed",
    ]
    assert all(entry["exc_class"] == "RuntimeError" for entry in logs)
    assert all(entry["frames"] for entry in logs)
    assert settlement_canary not in repr(logs)
    assert failure_canary not in repr(logs)
    assert settlement_canary not in caplog.text
    assert failure_canary not in caplog.text


@pytest.mark.parametrize(
    ("case", "transition_name", "response_type"),
    [
        ("source_select", "transition_source_plugin_selection", guided_route.PluginSelectionResponse),
        ("source_form", "transition_source_schema_form", guided_route.SchemaFormResponse),
        ("source_inspect", "transition_source_inspection_review", guided_route.InspectionResponse),
        ("sink_select", "transition_sink_plugin_selection", guided_route.PluginSelectionResponse),
        ("sink_form", "transition_sink_schema_form", guided_route.SchemaFormResponse),
        ("sink_field", "transition_sink_field_review", guided_route.FieldSelectionResponse),
    ],
)
def test_route_adapter_dispatches_all_six_schema8_stage_transitions(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    transition_name: str,
    response_type: type,
) -> None:
    stable_id = "11111111-1111-4111-8111-111111111111"
    operation_id = "22222222-2222-4222-8222-222222222222"
    server_stable_id = UUID("33333333-3333-4333-8333-333333333333")
    inspection = SourceInspectionFacts(
        source_kind="csv",
        redacted_identity={"filename": "input.csv"},
        byte_range_inspected=(0, 8),
        sample_row_count=1,
        observed_headers=("id",),
        inferred_types={"id": "str"},
        url_candidates=(),
        warnings=(),
    )
    if case == "source_select":
        guided = _with_route_turn(GuidedSession.initial(), TurnType.SINGLE_SELECT)
        turn = {"type": "single_select", "step_index": 0, "payload": {"options": [{"id": "csv"}]}}
        body = _live_body({"turn_token": "a" * 64}, chosen=["csv"])
    elif case == "source_form":
        guided = _with_route_turn(
            GuidedSession(
                step=GuidedStep.STEP_1_SOURCE,
                source_order=(stable_id,),
                pending_source_intents={
                    stable_id: SourceIntent(
                        name="source",
                        phase="plugin_options",
                        plugin="csv",
                        options=None,
                        inspection_facts=None,
                        observed_columns=(),
                        sample_rows=(),
                    )
                },
            ),
            TurnType.SCHEMA_FORM,
        )
        turn = {"type": "schema_form", "step_index": 0, "payload": {"plugin": "csv"}}
        body = _live_body({"turn_token": "a" * 64}, edited_values={"plugin": "csv", "options": {"path": "x.csv"}})
    elif case == "source_inspect":
        guided = _with_route_turn(
            GuidedSession(
                step=GuidedStep.STEP_1_SOURCE,
                source_order=(stable_id,),
                pending_source_intents={
                    stable_id: SourceIntent(
                        name="source",
                        phase="inspection_review",
                        plugin="csv",
                        options={"path": "x.csv", "on_validation_failure": "discard"},
                        inspection_facts=inspection,
                        observed_columns=("id",),
                        sample_rows=(),
                    )
                },
            ),
            TurnType.INSPECT_AND_CONFIRM,
        )
        turn = {"type": "inspect_and_confirm", "step_index": 0, "payload": {}}
        body = _live_body({"turn_token": "a" * 64}, edited_values={"columns": ["id"]})
    elif case == "sink_select":
        guided = _with_route_turn(replace(GuidedSession.initial(), step=GuidedStep.STEP_2_SINK), TurnType.SINGLE_SELECT)
        turn = {"type": "single_select", "step_index": 1, "payload": {"options": [{"id": "json"}]}}
        body = _live_body({"turn_token": "a" * 64}, chosen=["json"])
    elif case == "sink_form":
        guided = _with_route_turn(
            GuidedSession(
                step=GuidedStep.STEP_2_SINK,
                output_order=(stable_id,),
                pending_output_intents={stable_id: SinkIntent(name="output", phase="plugin_options", plugin="json", options=None)},
            ),
            TurnType.SCHEMA_FORM,
        )
        turn = {"type": "schema_form", "step_index": 1, "payload": {"plugin": "json"}}
        body = _live_body({"turn_token": "a" * 64}, edited_values={"plugin": "json", "options": {"path": "out.jsonl"}})
    else:
        guided = _with_route_turn(
            GuidedSession(
                step=GuidedStep.STEP_2_SINK,
                output_order=(stable_id,),
                pending_output_intents={
                    stable_id: SinkIntent(
                        name="output",
                        phase="field_review",
                        plugin="json",
                        options={"path": "out.jsonl", "on_write_failure": "discard"},
                    )
                },
            ),
            TurnType.MULTI_SELECT_WITH_CUSTOM,
        )
        turn = {"type": "multi_select_with_custom", "step_index": 1, "payload": {}}
        body = _live_body({"turn_token": "a" * 64}, chosen=["id"], custom_inputs=[])

    body["operation_id"] = operation_id
    request_model = guided_route.GuidedRespondRequest.model_validate(body, strict=True)
    captured: dict[str, object] = {}

    def transition(session: GuidedSession, **kwargs: object) -> GuidedSession:
        captured.update(kwargs)
        return session

    monkeypatch.setattr(guided_route, transition_name, transition)
    monkeypatch.setattr(
        guided_route,
        "_schema8_schema_authority",
        lambda **_kwargs: guided_route.SchemaFormAuthority(knobs={"fields": []}, model_validated_options={}),
    )

    class _AvailableCatalog:
        def unavailable_reason(self, _plugin_id: object) -> None:
            return None

    updated, _payload = guided_route._schema8_transition(
        guided,
        turn,
        request_model,
        catalog=_AvailableCatalog(),
        new_stable_id=server_stable_id,
    )

    assert updated is guided
    assert isinstance(captured["response"], response_type)
    assert captured["turn"] == guided_route.AnsweredTurn(history_index=len(guided.history) - 1)
    if case in {"source_select", "sink_select"}:
        assert captured["permitted_plugins"] in {("csv",), ("json",)}
        assert captured["new_stable_id"] == server_stable_id
        assert captured["new_stable_id"] != UUID(operation_id)
    if case in {"source_form", "source_inspect", "sink_form", "sink_field"}:
        assert captured["target_id"] == stable_id


@pytest.mark.parametrize(
    ("action", "transition_name"),
    [
        ({"action": "add", "component_kind": "source"}, "add_component_intent"),
        (
            {"action": "edit", "target": {"kind": "source", "stable_id": "11111111-1111-4111-8111-111111111111"}},
            "begin_component_edit",
        ),
        (
            {"action": "remove", "target": {"kind": "source", "stable_id": "11111111-1111-4111-8111-111111111111"}},
            "remove_reviewed_component",
        ),
        (
            {
                "action": "reorder",
                "component_kind": "source",
                "stable_ids": [
                    "22222222-2222-4222-8222-222222222222",
                    "11111111-1111-4111-8111-111111111111",
                ],
            },
            "reorder_reviewed_components",
        ),
        ({"action": "finish", "component_kind": "source"}, "finish_component_review"),
    ],
)
def test_route_adapter_dispatches_closed_component_review_actions(
    monkeypatch: pytest.MonkeyPatch,
    action: dict[str, object],
    transition_name: str,
) -> None:
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    guided = _with_route_turn(
        GuidedSession(
            step=GuidedStep.STEP_1_SOURCE,
            source_order=(first_id, second_id),
            reviewed_sources={
                first_id: SourceResolved(
                    name="source",
                    plugin="csv",
                    options={"path": "a.csv"},
                    observed_columns=("id",),
                    sample_rows=(),
                    on_validation_failure="discard",
                ),
                second_id: SourceResolved(
                    name="source_2",
                    plugin="json",
                    options={"path": "b.json"},
                    observed_columns=("id",),
                    sample_rows=(),
                    on_validation_failure="discard",
                ),
            },
        ),
        TurnType.REVIEW_COMPONENTS,
    )
    turn = {
        "type": "review_components",
        "step_index": 0,
        "payload": {
            "component_kind": "source",
            "items": [],
            "allowed_actions": ["add", "edit", "remove", "reorder", "finish"],
        },
    }
    request = GuidedRespondRequest.model_validate(
        {
            "operation_id": "44444444-4444-4444-8444-444444444444",
            "turn_token": "a" * 64,
            "component_action": action,
        },
        strict=True,
    )
    captured: list[tuple[object, ...]] = []

    def transition(*args: object) -> GuidedSession:
        captured.append(args)
        return guided

    monkeypatch.setattr(guided_route, transition_name, transition)

    updated, response_payload = guided_route._schema8_transition(
        guided,
        turn,
        request,
        catalog=object(),  # Component actions do not consult the catalog.
        new_stable_id=UUID("33333333-3333-4333-8333-333333333333"),
    )

    assert updated is guided
    assert captured and captured[0][0] is guided
    assert deep_thaw(response_payload) == {"component_action": action}
    if action["action"] == "add":
        assert captured[0][1:] == ("source", UUID("33333333-3333-4333-8333-333333333333"))
    elif action["action"] in {"edit", "remove"}:
        assert captured[0][1] == ComponentTarget(kind="source", stable_id=first_id)
    elif action["action"] == "reorder":
        assert captured[0][1:] == ("source", (UUID(second_id), UUID(first_id)))
    else:
        assert captured[0][1:] == ("source",)


def test_route_takeover_uses_live_fence_and_stale_worker_joins_winner(
    file_composer_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = file_composer_test_client
    session_id = _create_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    service = client.app.state.session_service
    engine = client.app.state.session_engine
    original_reserve = service.reserve_guided_operation
    original_settle = service.settle_guided_state_operation
    first_at_settle: asyncio.Event
    takeover_reserved: asyncio.Event
    allow_stale_settle: asyncio.Event
    settle_attempts: list[int] = []

    async def race() -> list[object]:
        nonlocal first_at_settle, takeover_reserved, allow_stale_settle
        first_at_settle = asyncio.Event()
        takeover_reserved = asyncio.Event()
        allow_stale_settle = asyncio.Event()

        async def reserve(*args: object, **kwargs: object) -> object:
            outcome = await original_reserve(*args, **kwargs)
            if isinstance(outcome, GuidedOperationTakenOver):
                takeover_reserved.set()
            return outcome

        async def settle(command: object, **kwargs: object) -> object:
            attempt = command.fence.attempt
            settle_attempts.append(attempt)
            if attempt == 1:
                first_at_settle.set()
                await allow_stale_settle.wait()
            return await original_settle(command, **kwargs)

        monkeypatch.setattr(service, "reserve_guided_operation", reserve)
        monkeypatch.setattr(service, "settle_guided_state_operation", settle)
        async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as async_client:
            stale = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/respond", json=body))
            await asyncio.wait_for(first_at_settle.wait(), timeout=3)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE guided_operations SET lease_expires_at = :expired "
                        "WHERE session_id = :session_id AND operation_id = :operation_id"
                    ),
                    {
                        "expired": datetime.now(UTC) - timedelta(seconds=1),
                        "session_id": session_id,
                        "operation_id": body["operation_id"],
                    },
                )
            winner = asyncio.create_task(async_client.post(f"/api/sessions/{session_id}/guided/respond", json=body))
            await asyncio.wait_for(takeover_reserved.wait(), timeout=3)
            allow_stale_settle.set()
            return list(await asyncio.wait_for(asyncio.gather(stale, winner), timeout=5))

    stale_response, winner_response = asyncio.run(race())

    assert stale_response.status_code == winner_response.status_code == 200
    assert stale_response.json() == winner_response.json()
    assert settle_attempts == [1, 2]
    assert _respond_operation_count(client, session_id) == 1
    assert len(asyncio.run(service.get_state_versions(UUID(session_id)))) == 1

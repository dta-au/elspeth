"""Session state read, revert and YAML route tests."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import structlog
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool

from elspeth.contracts.blobs import BlobServiceProtocol
from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.yaml_generator import PUBLIC_EXPORT_REBIND_GUIDANCE, PUBLIC_EXPORT_REDACTION_HEADER
from elspeth.web.config import WebSettings
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.execution.schemas import ValidationError
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.provider_config_policy import AWS_S3_ENDPOINT_URL_POLICY_ERROR
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from elspeth.web.sessions.routes import create_session_router
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, wire_test_pipeline_user_authority
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.composer import test_file_inject_upload_only_tool_parity as _file_inject_tool_parity
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness
from tests.unit.web.sessions.test_routes import (
    ValidationResult,
    _compose_session_operation_context,
    _install_restricted_plugin_policy,
    _make_app,
    _ready_blob_record,
    _save_test_composition_state,
)
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines


class TestStateRoutes:
    """Tests for composition state endpoints."""

    def test_get_state_empty(self, tmp_path) -> None:
        app, _ = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/sessions", json={"title": "Empty"})
        session_id = resp.json()["id"]

        state_resp = client.get(f"/api/sessions/{session_id}/state")
        assert state_resp.status_code == 200
        assert state_resp.json() is None
        fixture = Path(__file__).resolve().parents[3] / "fixtures/web/composer/empty_composition_state_response.json"
        assert json.loads(fixture.read_text()) == {"status": state_resp.status_code, "body": state_resp.json()}

    def test_get_state_versions(self, tmp_path) -> None:
        app, _ = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/sessions", json={"title": "Pipeline"})
        session_id = resp.json()["id"]

        versions_resp = client.get(
            f"/api/sessions/{session_id}/state/versions",
        )
        assert versions_resp.status_code == 200
        assert versions_resp.json() == []


class TestRevertEndpoint:
    """Tests for POST /api/sessions/{id}/state/revert (R1)."""

    @pytest.mark.asyncio
    async def test_revert_resolves_target_and_mutates_under_compose_lock_but_reserves_outside(self, tmp_path) -> None:
        from elspeth.web.sessions.routes._helpers import _SessionComposeLockRegistry

        app, service = _make_app(tmp_path)
        registry = _SessionComposeLockRegistry()
        app.state.session_compose_lock_registry = registry
        session = await service.create_session("alice", "Pipeline", "local")
        target = await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        compose_lock = await registry.get_lock(str(session.id))
        lock_observations: list[tuple[str, bool]] = []
        original_get_state = service.get_state
        original_reserve = service.reserve_operation_receipt
        original_revert = service.revert_state_for_operation_receipt

        async def observed_get_state(state_id):
            lock_observations.append(("target", compose_lock.locked()))
            return await original_get_state(state_id)

        async def observed_reserve(*args, **kwargs):
            lock_observations.append(("reserve", compose_lock.locked()))
            return await original_reserve(*args, **kwargs)

        async def observed_revert(*args, **kwargs):
            lock_observations.append(("mutation", compose_lock.locked()))
            return await original_revert(*args, **kwargs)

        with (
            patch.object(service, "get_state", side_effect=observed_get_state),
            patch.object(service, "reserve_operation_receipt", side_effect=observed_reserve),
            patch.object(service, "revert_state_for_operation_receipt", side_effect=observed_revert),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    f"/api/sessions/{session.id}/state/revert",
                    json={"operation_id": str(uuid.uuid4()), "state_id": str(target.id)},
                )

        assert response.status_code == 200
        assert lock_observations == [("target", True), ("reserve", False), ("mutation", True)]

    @pytest.mark.asyncio
    async def test_revert_cancellation_terminalizes_receipt_before_releasing_session_authority(self, tmp_path) -> None:
        from sqlalchemy import select

        from elspeth.web.sessions.models import session_operation_fences_table, session_operation_receipts_table

        app, service = _make_app(tmp_path)
        session = await service.create_session("alice", "Pipeline", "local")
        target = await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        operation_id = str(uuid.uuid4())
        mutation_started = asyncio.Event()
        release_mutation = asyncio.Event()
        original_revert = service.revert_state_for_operation_receipt

        async def blocking_revert(*args, **kwargs):
            mutation_started.set()
            await release_mutation.wait()
            return await original_revert(*args, **kwargs)

        with patch.object(service, "revert_state_for_operation_receipt", side_effect=blocking_revert):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                request_task = asyncio.create_task(
                    client.post(
                        f"/api/sessions/{session.id}/state/revert",
                        json={"operation_id": operation_id, "state_id": str(target.id)},
                    )
                )
                await asyncio.wait_for(mutation_started.wait(), timeout=3)
                request_task.cancel("operator cancelled state revert")
                request_task.cancel("shutdown repeated state revert cancellation")
                with pytest.raises(asyncio.CancelledError, match="operator cancelled state revert") as caught:
                    await request_task

        assert caught.value.args == ("operator cancelled state revert",)
        with service._engine.connect() as connection:
            operation = (
                connection.execute(
                    select(session_operation_receipts_table).where(
                        session_operation_receipts_table.c.session_id == str(session.id),
                        session_operation_receipts_table.c.operation_id == operation_id,
                    )
                )
                .mappings()
                .one()
            )
            session_fence = connection.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
            ).one()
        assert operation["status"] == "failed"
        assert operation["failure_code"] == "request_cancelled"
        assert session_fence.released_at is not None

    @pytest.mark.asyncio
    async def test_revert_creates_new_version(self, tmp_path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        # Create session and two state versions via the service
        session = await service.create_session("alice", "Pipeline", "local")
        v1 = await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                sources={"source": {"plugin": "csv", "on_success": "rows", "options": {}, "on_validation_failure": "discard"}},
                metadata_={"name": "before", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                sources={"source": {"plugin": "json", "on_success": "rows", "options": {}, "on_validation_failure": "discard"}},
                metadata_={"name": "after", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        # Revert to v1
        operation_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/sessions/{session.id}/state/revert",
            json={"operation_id": operation_id, "state_id": str(v1.id)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 3
        # Should match v1's source, not v2's
        assert body["sources"] == {"source": {"plugin": "csv", "on_success": "rows", "options": {}, "on_validation_failure": "discard"}}
        # Lineage: new version derives from v1
        assert body["derived_from_state_id"] == str(v1.id)

        replay = client.post(
            f"/api/sessions/{session.id}/state/revert",
            json={"operation_id": operation_id, "state_id": str(v1.id)},
        )
        assert replay.status_code == 200
        assert replay.json() == body
        versions = await service.get_state_versions(session.id)
        assert [state.version for state in versions] == [1, 2, 3]
        messages = await service.get_messages(session.id, limit=None)
        assert [message.content for message in messages] == ["Pipeline reverted to version 1."]

    @pytest.mark.asyncio
    async def test_revert_replay_is_stable_after_plugin_policy_changes(self, tmp_path) -> None:
        """A receipt replays its committed response, while GET reports live policy."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Policy drift", "local")
        target = await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                sources={"source": {"plugin": "csv", "on_success": "main", "options": {}, "on_validation_failure": "discard"}},
                metadata_={"name": "Policy drift", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                sources={"source": {"plugin": "json", "on_success": "main", "options": {}, "on_validation_failure": "discard"}},
                metadata_={"name": "Policy drift", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )
        body = {"operation_id": str(uuid.uuid4()), "state_id": str(target.id)}

        first = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["plugin_policy_findings"] == []
        versions_before = [record.id for record in await service.get_state_versions(session.id)]
        messages_before = [message.id for message in await service.get_messages(session.id, limit=None)]

        snapshot = _install_restricted_plugin_policy(app, PluginId("source", "csv"))
        current = client.get(f"/api/sessions/{session.id}/state")
        assert current.status_code == 200, current.text
        assert current.json()["plugin_policy_findings"] == [
            {
                "component_id": "source",
                "plugin_id": "source:csv",
                "reason_code": "plugin_not_enabled",
                "snapshot_fingerprint": snapshot.snapshot_hash,
            }
        ]

        replay = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.content == first.content
        assert [record.id for record in await service.get_state_versions(session.id)] == versions_before
        assert [message.id for message in await service.get_messages(session.id, limit=None)] == messages_before

    @pytest.mark.asyncio
    async def test_revert_resurfaces_review_cards_for_restored_pending_state(self, tmp_path: Path) -> None:
        """Reverting to a once-pending version creates fresh consumable cards
        even when the original version's cards are already terminal."""
        from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
        from elspeth.web.sessions.routes._helpers import _state_from_record

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Review revert", "local")
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: score
    options:
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            imported = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
        assert imported.status_code == 200, imported.text
        imported_id = imported.json()["id"]

        original_events = await service.list_interpretation_events(session.id, status="pending")
        assert {event.kind for event in original_events} == {
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            InterpretationKind.LLM_MODEL_CHOICE,
        }
        for event in original_events:
            resolved = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert resolved.status_code == 200, resolved.text
        assert await service.list_interpretation_events(session.id, status="pending") == []

        reverted = client.post(
            f"/api/sessions/{session.id}/state/revert",
            json={"operation_id": str(uuid.uuid4()), "state_id": imported_id},
        )

        assert reverted.status_code == 200, reverted.text
        fresh_events = await service.list_interpretation_events(session.id, status="pending")
        assert {event.kind for event in fresh_events} == {
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            InterpretationKind.LLM_MODEL_CHOICE,
        }
        assert {str(event.composition_state_id) for event in fresh_events} == {reverted.json()["id"]}
        # The revert consulted no LLM: its cards name the route and claim no model.
        assert {event.surface_origin for event in fresh_events} == {InterpretationSurfaceOrigin.STATE_REVERT}
        assert {event.composer_skill_hash for event in fresh_events} == {None}
        for event in fresh_events:
            resolved = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert resolved.status_code == 200, resolved.text
        head = await service.get_current_state(session.id)
        assert head is not None
        assert not isinstance(materialize_state_for_execution(_state_from_record(head)), InterpretationReviewPending)

    @pytest.mark.asyncio
    async def test_revert_replay_repairs_cards_after_post_commit_surface_failure(self, tmp_path: Path) -> None:
        """A retry of the same completed operation repairs the interval where
        revert committed but post-commit card surfacing failed."""
        app, service = _make_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        session = await service.create_session("alice", "Review revert replay", "local")
        yaml_text = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score {{ row.value }}'
"""
        imported = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
        assert imported.status_code == 200, imported.text
        for event in await service.list_interpretation_events(session.id, status="pending"):
            resolved = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert resolved.status_code == 200, resolved.text
        operation_id = str(uuid.uuid4())

        with patch(
            "elspeth.web.sessions.routes.composer.state._surface_reverted_interpretation_reviews",
            side_effect=RuntimeError("surface interrupted"),
        ):
            first = client.post(
                f"/api/sessions/{session.id}/state/revert",
                json={"operation_id": operation_id, "state_id": imported.json()["id"]},
            )
        assert first.status_code == 500
        versions_after_first = await service.get_state_versions(session.id)

        replay = client.post(
            f"/api/sessions/{session.id}/state/revert",
            json={"operation_id": operation_id, "state_id": imported.json()["id"]},
        )

        assert replay.status_code == 200, replay.text
        versions_after_replay = await service.get_state_versions(session.id)
        assert [record.id for record in versions_after_replay] == [record.id for record in versions_after_first]
        replay_events = [
            event
            for event in await service.list_interpretation_events(session.id, status="pending")
            if str(event.composition_state_id) == replay.json()["id"]
        ]
        assert {event.kind for event in replay_events} == {
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            InterpretationKind.LLM_MODEL_CHOICE,
        }

    @pytest.mark.asyncio
    async def test_revert_replay_writes_nothing_when_the_stored_response_hash_mismatches(self, tmp_path: Path) -> None:
        """Response-hash verification must precede every state_revert replay write.

        The revert replay arm repairs surfacing debt, so if that repair ran
        before the projected response was proven identical to the stored one,
        a corrupt projection could insert new ``interpretation_events`` rows
        and flip existing PENDING rows to SUPERSEDED -- and only afterwards
        fail integrity verification. Both are audit-primary mutations. The
        mismatch must abort with ZERO interpretation and state writes.

        The fixture drives BOTH halves rather than merely detecting them: the
        reverted state owes two fresh cards, and a later import leaves two
        PENDING cards on a different state whose reviewed content the reverted
        state no longer matches, which the repair's writer supersedes. The
        positive control at the end proves both writes really do fire on a
        clean replay, so the zero-write assertions above it are not vacuous.

        Sibling of
        the operation receipt's hash check must precede any replay repair.
        """
        from elspeth.web.sessions.routes.composer import state as state_module

        class _SurfacingWorkerCrash(BaseException):
            """Escape the route exactly as a process loss would."""

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Revert replay hash mismatch", "local")
        yaml_text = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score {{ row.value }}'
"""
        superseding_yaml = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-sonnet-4.5
    prompt_template: 'Rank {{ row.value }} carefully'
"""
        imported = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
        assert imported.status_code == 200, imported.text
        # Resolve the imported state's own cards so the debt this test relies
        # on belongs to the REVERTED state, not inherited from the import.
        for event in await service.list_interpretation_events(session.id, status="pending"):
            resolved = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert resolved.status_code == 200, resolved.text
        # Import a DIFFERENT pipeline for the same node, and leave ITS cards
        # pending. The repair pass reads evidence per state but supersedes
        # PENDING rows for the site session-wide, so these are what the
        # supersession half of the hazard would flip. Without them that half
        # is detectable by the equality below but never actually driven.
        superseding = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": superseding_yaml})
        assert superseding.status_code == 200, superseding.text
        stale_pending = await service.list_interpretation_events(session.id, status="pending")
        assert {event.kind for event in stale_pending} == {
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            InterpretationKind.LLM_MODEL_CHOICE,
        }
        assert {str(event.composition_state_id) for event in stale_pending} == {superseding.json()["id"]}
        operation_id = str(uuid.uuid4())
        revert_body = {"operation_id": operation_id, "state_id": imported.json()["id"]}

        def _crash_between_settlement_and_surfacing(*_args: Any, **_kwargs: Any) -> None:
            raise _SurfacingWorkerCrash("worker lost after durable settlement, before surfacing")

        with (
            patch.object(
                state_module,
                "_surface_reverted_interpretation_reviews",
                side_effect=_crash_between_settlement_and_surfacing,
            ),
            pytest.raises(_SurfacingWorkerCrash),
        ):
            client.post(f"/api/sessions/{session.id}/state/revert", json=revert_body)

        # NON-VACUITY PRECONDITION, one clause per way the repair can silently
        # write nothing. (1) ``_surface_reverted_interpretation_reviews``
        # returns early on a state whose ``metadata_`` is None. (2)
        # ``only_missing_evidence=True`` makes the surfacer skip every site
        # already carrying evidence in ANY resolution status, so the debt must
        # be checked unfiltered -- a resolved or superseded row on the reverted
        # state would make the replay a no-op. (3) the stale cards the
        # supersession half needs must still be PENDING once the settlement has
        # run. All three are preconditions of the ZERO-write assertions below;
        # the positive control at the end of the test closes the loop by
        # proving the writes really do happen when the projection is intact.
        reverted_state = await service.get_current_state(session.id)
        assert reverted_state is not None
        assert reverted_state.id != uuid.UUID(imported.json()["id"])
        assert reverted_state.metadata_ is not None, (
            "fixture is vacuous: the surfacer returns early on a state with no metadata_, so replay would write nothing"
        )
        assert (await service.list_interpretation_events(session.id, composition_state_id=reverted_state.id)) == [], (
            "fixture is vacuous: the reverted state already carries evidence, so replay would write nothing"
        )
        stale_ids = {event.id for event in stale_pending}
        assert {
            event.id for event in await service.list_interpretation_events(session.id, status="pending") if event.id in stale_ids
        } == stale_ids, "fixture cannot drive the supersession half: the settlement already retired the stale cards"

        # Corrupt the PROJECTION, not the stored row: terminal
        # Terminal operation receipt rows are immutable by database trigger, and a
        # corrupt projection is the failure this ordering actually guards.
        # ``validation_errors`` is inside the hash domain and survives the
        # strict re-validation ``operation_receipt_response_hash`` performs.
        original_state_response = state_module._state_response

        def _corrupt_projection(record: Any, **kwargs: Any) -> Any:
            projected = original_state_response(record, **kwargs)
            from elspeth.web.sessions.schemas import CompositionValidationErrorResponse

            return projected.model_copy(
                update={
                    "validation_errors": [
                        CompositionValidationErrorResponse(message="tampered", error_code=None, component=None),
                    ]
                }
            )

        events_before = sorted(await service.list_interpretation_events(session.id), key=lambda e: str(e.id))
        versions_before = [record.id for record in await service.get_state_versions(session.id)]

        # The patch goes on ONLY around the replay POST: ``_state_response``
        # also feeds the settlement path's response_hash_factory, so patching
        # it earlier would corrupt the stored hash too and no mismatch would
        # occur.
        #
        # ``match=`` pins the ONE raise this test exists for: the hash
        # comparison in the operation receipt replay path.
        # Four other AuditIntegrityError sites in ``state.py`` alone are
        # reachable from this POST, and each would satisfy a bare
        # ``pytest.raises`` while leaving both zero-write assertions trivially
        # true -- so a bare one cannot tell this test passing from this test
        # never running the ordering at all. They divide in two:
        #   - BEFORE the comparison, which therefore never runs: "session
        #     unexpectedly has no current checkpoint", ``_replay``'s
        #     "non-state result locator" guard, and "operation was not
        #     reserved". Further reserve/lookup guards in
        #     the operation receipt route helper sit here too.
        #   - AFTER a comparison that MATCHED: the locator guard in
        #     ``_repair_reverted_surfacing_debt``, which runs as
        #     ``after_verified`` and raises before the surfacing write. It
        #     writes nothing either, but it proves the opposite of what this
        #     test asserts -- that verification succeeded.
        # ``_replay``'s guard and the repair's guard share a message, so only
        # a match on the comparison's own message discriminates.
        with (
            patch.object(state_module, "_state_response", _corrupt_projection),
            pytest.raises(AuditIntegrityError, match="Operation receipt replay response hash does not match stored evidence"),
        ):
            client.post(f"/api/sessions/{session.id}/state/revert", json=revert_body)

        replay_events = await service.list_interpretation_events(session.id, composition_state_id=reverted_state.id)
        assert [event.kind for event in replay_events] == [], (
            "the rejected replay wrote interpretation_events before the response hash was verified"
        )
        # One equality over every column of every row in the session covers
        # both halves of the hazard: no INSERT, and no PENDING row flipped to
        # SUPERSEDED by the supersession pass inside the same window. The
        # fixture drives both -- see the positive control below.
        assert sorted(await service.list_interpretation_events(session.id), key=lambda e: str(e.id)) == events_before
        assert [record.id for record in await service.get_state_versions(session.id)] == versions_before
        current_after = await service.get_current_state(session.id)
        assert current_after is not None and current_after.id == reverted_state.id

        # POSITIVE CONTROL. Everything above is a ZERO-write assertion, which a
        # fixture that could never write also satisfies. Re-POST the same
        # operation with the projection intact and prove that BOTH writes the
        # rejected replay was in a position to make do in fact happen: two
        # fresh cards minted on the reverted state, and the two stale PENDING
        # cards flipped to SUPERSEDED.
        repaired = client.post(f"/api/sessions/{session.id}/state/revert", json=revert_body)
        assert repaired.status_code == 200, repaired.text
        repaired_events = await service.list_interpretation_events(session.id, composition_state_id=reverted_state.id)
        assert {event.kind for event in repaired_events} == {
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            InterpretationKind.LLM_MODEL_CHOICE,
        }, "the verified replay did not mint the cards the rejected one was refused, so the INSERT half was never driven"
        assert [event.choice for event in await service.list_interpretation_events(session.id, status="all") if event.id in stale_ids] == [
            InterpretationChoice.SUPERSEDED,
            InterpretationChoice.SUPERSEDED,
        ], "the verified replay superseded nothing, so the supersession half was never driven"

    @pytest.mark.asyncio
    async def test_revert_injects_system_message(self, tmp_path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Pipeline", "local")
        v1 = await _save_test_composition_state(service, session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        await _save_test_composition_state(service, session.id, CompositionStateData(is_valid=True), provenance="session_seed")

        client.post(
            f"/api/sessions/{session.id}/state/revert",
            json={"operation_id": str(uuid.uuid4()), "state_id": str(v1.id)},
        )

        # Check that a system message was injected
        msgs_resp = client.get(f"/api/sessions/{session.id}/messages")
        messages = msgs_resp.json()
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "Pipeline reverted to version 1."

    @pytest.mark.asyncio
    async def test_revert_idor_protection(self, tmp_path) -> None:
        """Revert to a state in another user's session returns 404."""
        engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(engine)
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id="alice")
            ensure_test_identity(conn, identity_id="bob")
        service = FencedSessionServiceHarness(
            engine,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test"),
        )

        def make_app_for_user(uid: str) -> FastAPI:
            with engine.begin() as conn:
                ensure_test_identity(conn, identity_id=uid)
            app = FastAPI()
            identity = UserIdentity(user_id=uid, username=uid)

            async def mock_user():
                return identity

            app.dependency_overrides[get_current_user] = mock_user
            wire_test_pipeline_user_authority(app, identity_id=uid, engine=engine)
            app.state.session_service = service
            app.state.settings = WebSettings(
                data_dir=tmp_path,
                composer_max_composition_turns=15,
                composer_context_max_prompt_tokens=200_000,
                composer_max_discovery_turns=10,
                composer_timeout_seconds=85.0,
                composer_rate_limit_per_minute=10,
                shareable_link_signing_key=b"\x00" * 32,
            )

            app.state.rate_limiter = ComposerRateLimiter(limit=100)
            app.state.composer_progress_registry = ComposerProgressRegistry()
            app.include_router(create_session_router())
            return app

        bob_app = make_app_for_user("bob")
        bob_client = TestClient(bob_app)

        # Alice creates a session with a state
        session = await service.create_session("alice", "Alice Only", "local")
        v1 = await _save_test_composition_state(service, session.id, CompositionStateData(is_valid=True), provenance="session_seed")

        # Bob tries to revert -- should be 404
        resp = bob_client.post(
            f"/api/sessions/{session.id}/state/revert",
            json={"operation_id": str(uuid.uuid4()), "state_id": str(v1.id)},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_revert_state_not_belonging_to_session(self, tmp_path) -> None:
        """Revert with a state_id from a different session returns 404."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        s1 = await service.create_session("alice", "Session 1", "local")
        s2 = await service.create_session("alice", "Session 2", "local")
        v1_s2 = await _save_test_composition_state(service, s2.id, CompositionStateData(is_valid=True), provenance="session_seed")

        # Try to revert s1 using s2's state -- should fail
        resp = client.post(
            f"/api/sessions/{s1.id}/state/revert",
            json={"operation_id": str(uuid.uuid4()), "state_id": str(v1_s2.id)},
        )
        assert resp.status_code == 404


class TestRevertSurfacingDebt:
    """Receipt replay reads may avoid authority; actual repair remains fenced."""

    _REVIEW_YAML = """
transforms:
- name: label_record
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Label {{ row.label }}'
"""

    async def _missing_review_case(self, tmp_path: Path) -> tuple:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Replay review debt", "local")
        imported = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": self._REVIEW_YAML})
        assert imported.status_code == 200, imported.text
        originals = await service.list_interpretation_events(session.id, status="pending")
        assert {event.kind for event in originals} == {
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            InterpretationKind.LLM_MODEL_CHOICE,
        }
        for event in originals:
            resolved = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert resolved.status_code == 200, resolved.text
        body = {"operation_id": str(uuid.uuid4()), "state_id": imported.json()["id"]}
        with (
            patch(
                "elspeth.web.sessions.routes.composer.state._surface_reverted_interpretation_reviews",
                side_effect=RuntimeError("post-commit review publication interrupted"),
            ),
            pytest.raises(RuntimeError, match="post-commit review publication interrupted"),
        ):
            client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        record = await service.get_current_state(session.id)
        assert record is not None and record.metadata_ is not None
        assert str(record.id) != imported.json()["id"]
        assert await service.list_interpretation_events(session.id, status="all", composition_state_id=record.id) == []
        return app, service, client, session, record, body, originals

    async def _snapshot(self, service, session_id: uuid.UUID) -> tuple:
        return (
            await service.get_state_versions(session_id),
            await service.get_messages(session_id, limit=None),
            await service.list_interpretation_events(session_id, status="all"),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("metadata", [None, {"name": "No review sites", "description": ""}])
    async def test_no_site_replay_keeps_unrelated_writer_and_stored_response(self, tmp_path: Path, metadata: dict[str, str] | None) -> None:
        from elspeth.web.composer import interpretation_surfacing
        from elspeth.web.sessions.routes.composer import state as state_routes

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "No review replay", "local")
        target = await _save_test_composition_state(
            service, session.id, CompositionStateData(metadata_=metadata, is_valid=True), provenance="session_seed"
        )
        body = {"operation_id": str(uuid.uuid4()), "state_id": str(target.id)}
        first = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert first.status_code == 200, first.text
        before = await self._snapshot(service, session.id)
        authority = service.session_operation_authority
        writer = authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=30,
        )
        try:
            with (
                patch.object(authority, "acquire", wraps=authority.acquire) as acquisition,
                patch.object(
                    interpretation_surfacing,
                    "has_pending_interpretation_review_surfacing_debt",
                    wraps=interpretation_surfacing.has_pending_interpretation_review_surfacing_debt,
                ) as query,
                patch.object(state_routes, "_state_from_record", wraps=state_routes._state_from_record) as hydrate,
            ):
                replay = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
            assert replay.status_code == 200, replay.text
            assert replay.content == first.content
            acquisition.assert_not_called()
            if metadata is None:
                query.assert_not_called()
                hydrate.assert_not_called()
            else:
                query.assert_called_once()
                hydrate.assert_called_once()
            authority.compare_and_swap(writer)
            assert await self._snapshot(service, session.id) == before
            # A different operation is a fresh revert and still needs authority.
            with pytest.raises(SessionOperationConflictError):
                client.post(
                    f"/api/sessions/{session.id}/state/revert",
                    json={**body, "operation_id": str(uuid.uuid4())},
                )
            authority.compare_and_swap(writer)
        finally:
            authority.release(writer)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("evidence_status", ["pending", "accepted_as_drafted", "superseded"])
    async def test_evidenced_replay_uses_all_statuses_without_acquiring_writer(self, tmp_path: Path, evidence_status: str) -> None:
        _app, service, client, session, record, body, _originals = await self._missing_review_case(tmp_path)
        first = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert first.status_code == 200, first.text
        events = await service.list_interpretation_events(session.id, composition_state_id=record.id)
        assert len(events) == 2
        if evidence_status == "accepted_as_drafted":
            for event in events:
                response = client.post(
                    f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                    json={"choice": "accepted_as_drafted"},
                )
                assert response.status_code == 200, response.text
        elif evidence_status == "superseded":
            await _save_test_composition_state(
                service,
                session.id,
                CompositionStateData(metadata_={"name": "Different head", "description": ""}, is_valid=False),
                provenance="session_seed",
            )
        evidence = await service.list_interpretation_events(session.id, status="all", composition_state_id=record.id)
        assert {event.choice.value for event in evidence} == {evidence_status}
        before = await self._snapshot(service, session.id)
        authority = service.session_operation_authority
        writer = authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=30,
        )
        try:
            with patch.object(authority, "acquire", wraps=authority.acquire) as acquisition:
                replay = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
            assert replay.status_code == 200, replay.text
            assert replay.content == first.content
            acquisition.assert_not_called()
            authority.compare_and_swap(writer)
            assert await self._snapshot(service, session.id) == before
        finally:
            authority.release(writer)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("partial_evidence", [False, True], ids=["wrong-state-only", "mixed-sites"])
    async def test_missing_sites_still_require_authority_then_repair(self, tmp_path: Path, partial_evidence: bool) -> None:
        _app, service, client, session, record, body, originals = await self._missing_review_case(tmp_path)
        original_create = service.create_pending_interpretation_event
        if partial_evidence:
            event = originals[0]
            await original_create(
                session_id=session.id,
                composition_state_id=record.id,
                affected_node_id=event.affected_node_id,
                tool_call_id=f"backend_auto_surface:{uuid.uuid4()}",
                user_term=event.user_term,
                kind=event.kind,
                llm_draft=event.llm_draft,
                model_identifier=None,
                model_version=None,
                provider=None,
                composer_skill_hash=None,
                surface_origin=InterpretationSurfaceOrigin.STATE_REVERT,
            )
        evidence = await service.list_interpretation_events(session.id, composition_state_id=record.id)
        assert len(evidence) == int(partial_evidence)
        assert len(await service.list_interpretation_events(session.id, status="all")) > len(evidence)
        before = await self._snapshot(service, session.id)
        authority = service.session_operation_authority
        writer = authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=30,
        )
        try:
            with pytest.raises(SessionOperationConflictError):
                client.post(f"/api/sessions/{session.id}/state/revert", json=body)
            authority.compare_and_swap(writer)
            assert await self._snapshot(service, session.id) == before
        finally:
            authority.release(writer)
        repaired = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["id"] == str(record.id)
        assert repaired.json()["version"] == record.version
        assert await service.get_state_versions(session.id) == before[0]
        assert await service.get_messages(session.id, limit=None) == before[1]
        events = await service.list_interpretation_events(session.id, composition_state_id=record.id)
        assert {event.kind for event in events} == {
            InterpretationKind.LLM_PROMPT_TEMPLATE,
            InterpretationKind.LLM_MODEL_CHOICE,
        }
        assert len(events) == 2
        assert all(event.surface_origin is InterpretationSurfaceOrigin.STATE_REVERT for event in events)
        assert all(event.model_identifier is event.model_version is event.provider is event.composer_skill_hash is None for event in events)
        for event in events:
            response = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["evidence", "head"])
    async def test_repair_rereads_after_acquisition_and_refuses_stale_new_cards(self, tmp_path: Path, change: str) -> None:
        _app, service, client, session, record, body, originals = await self._missing_review_case(tmp_path)
        authority = service.session_operation_authority
        original_acquire = authority.acquire
        original_read = service.list_interpretation_events
        original_create = service.create_pending_interpretation_event
        contexts: list[SessionOperationContext] = []
        reads: list[tuple[SessionOperationContext | None, tuple]] = []
        attempts: list[dict] = []
        changed_head = None

        def acquired(*args, **kwargs):
            context = original_acquire(*args, **kwargs)
            contexts.append(context)
            return context

        async def read(*args, **kwargs):
            nonlocal changed_head
            events = await original_read(*args, **kwargs)
            assert kwargs["status"] == "all" and kwargs["composition_state_id"] == record.id
            context = contexts[-1] if contexts else None
            if context is not None:
                authority.compare_and_swap(context)
            reads.append((context, tuple(event.id for event in events)))
            if len(reads) == 1:
                assert events == [] and contexts == [], "initial advisory read must precede repair authority"
                if change == "evidence":
                    for event in originals:
                        await original_create(
                            session_id=session.id,
                            composition_state_id=record.id,
                            affected_node_id=event.affected_node_id,
                            tool_call_id=f"backend_auto_surface:{uuid.uuid4()}",
                            user_term=event.user_term,
                            kind=event.kind,
                            llm_draft=event.llm_draft,
                            model_identifier=None,
                            model_version=None,
                            provider=None,
                            composer_skill_hash=None,
                            surface_origin=InterpretationSurfaceOrigin.STATE_REVERT,
                        )
                else:
                    changed_head = await _save_test_composition_state(
                        service,
                        session.id,
                        CompositionStateData(metadata_={"name": "Removed review sites", "description": ""}, is_valid=False),
                        provenance="session_seed",
                    )
            return events

        async def create(*args, **kwargs):
            context = kwargs["session_operation_context"]
            authority.compare_and_swap(context)
            assert context.operation_kind is SessionOperationKind.COMPOSE
            assert context.fence.session_id == str(session.id)
            assert kwargs["composition_state_id"] == record.id
            attempts.append(kwargs)
            return await original_create(*args, **kwargs)

        with (
            patch.object(authority, "acquire", side_effect=acquired),
            patch.object(service, "list_interpretation_events", side_effect=read),
            patch.object(service, "create_pending_interpretation_event", side_effect=create),
        ):
            replay = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == str(record.id) and replay.json()["version"] == record.version
        assert len(reads) == 2 and reads[0] == (None, ())
        assert reads[1][0] is contexts[-1] and contexts[-1].operation_kind is SessionOperationKind.COMPOSE
        events = await original_read(session.id, status="all", composition_state_id=record.id)
        if change == "evidence":
            assert len(reads[1][1]) == len(events) == 2
            assert attempts == [], "under-writer reread must skip evidenced sites before writer dedup"
            assert await service.get_current_state(session.id) == record
        else:
            assert reads[1][1] == () and len(attempts) == 2
            assert events == [], "stale reviewed content must not mint new cards"
            assert changed_head is not None and await service.get_current_state(session.id) == changed_head

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [RuntimeError("evidence storage unavailable"), AuditIntegrityError("corrupt evidence")])
    async def test_advisory_read_failure_propagates_without_acquisition(self, tmp_path: Path, failure: Exception) -> None:
        _app, service, client, session, _record, body, _originals = await self._missing_review_case(tmp_path)
        before = await self._snapshot(service, session.id)
        authority = service.session_operation_authority
        with (
            patch.object(service, "list_interpretation_events", side_effect=failure),
            patch.object(authority, "acquire", wraps=authority.acquire) as acquisition,
            pytest.raises(type(failure), match=str(failure)) as caught,
        ):
            client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert caught.value is failure
        acquisition.assert_not_called()
        assert await self._snapshot(service, session.id) == before

    @pytest.mark.asyncio
    async def test_replay_hash_is_verified_before_advisory_query(self, tmp_path: Path) -> None:
        from elspeth.web.composer import interpretation_surfacing
        from elspeth.web.sessions.routes.composer import state as state_routes
        from elspeth.web.sessions.schemas import CompositionValidationErrorResponse

        _app, service, client, session, record, body, _originals = await self._missing_review_case(tmp_path)
        before = await self._snapshot(service, session.id)
        original_response = state_routes._state_response

        def corrupted_response(record, **kwargs):
            return original_response(record, **kwargs).model_copy(
                update={
                    "validation_errors": [CompositionValidationErrorResponse(message="altered response", error_code=None, component=None)]
                }
            )

        with (
            patch.object(state_routes, "_state_response", side_effect=corrupted_response),
            patch.object(
                interpretation_surfacing,
                "has_pending_interpretation_review_surfacing_debt",
                side_effect=AssertionError("unverified replay reached advisory query"),
            ) as query,
            pytest.raises(AuditIntegrityError, match="Operation receipt replay response hash does not match stored evidence"),
        ):
            client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        query.assert_not_called()
        assert await self._snapshot(service, session.id) == before
        repaired = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert repaired.status_code == 200 and repaired.json()["id"] == str(record.id)
        assert len(await service.list_interpretation_events(session.id, composition_state_id=record.id)) == 2


class TestYamlEndpoint:
    """Tests for GET /api/sessions/{id}/state/yaml."""

    @pytest.mark.asyncio
    @pytest.mark.timing_sensitive
    async def test_yaml_template_validation_offloads_while_health_responds(self, tmp_path: Path, monkeypatch) -> None:
        import elspeth.web.sessions.routes.composer.state as state_routes

        app, service = _make_app(tmp_path)
        session = await service.create_session("alice", "Jinja bound", "local")

        @app.get("/api/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        entered = threading.Event()
        release = threading.Event()
        original = state_routes.run_sync_in_worker

        async def traced_worker(function, *args, **kwargs):
            if function.__name__ == "unsurfaceable_pending_interpretation_review_sites":

                def check_in_worker():
                    entered.set()
                    release.wait(timeout=3)
                    return function(*args, **kwargs)

                return await original(check_in_worker)
            return await original(function, *args, **kwargs)

        monkeypatch.setattr(state_routes, "run_sync_in_worker", traced_worker)
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: score
    options:
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: '{{ (3**(3**16)) % 7 }}'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            loop = asyncio.get_running_loop()
            loop_gaps: list[float] = []
            watching = True

            async def watch_loop() -> None:
                previous = loop.time()
                while watching:
                    await asyncio.sleep(0.02)
                    now = loop.time()
                    loop_gaps.append(now - previous)
                    previous = now

            watcher = asyncio.create_task(watch_loop())
            await asyncio.sleep(0)
            import_task = asyncio.create_task(client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text}))
            try:
                try:
                    for _ in range(200):
                        if entered.is_set():
                            break
                        await asyncio.sleep(0.01)
                    assert entered.is_set()
                    assert not import_task.done()
                    started = asyncio.get_running_loop().time()
                    response = await client.get("/api/health")
                    assert asyncio.get_running_loop().time() - started < 0.5
                    assert response.json() == {"status": "ok"}
                finally:
                    release.set()
                imported = await import_task
            finally:
                watching = False
                release.set()
                await watcher
            assert max(loop_gaps, default=0.0) < 1.0
        assert imported.status_code < 500

    @pytest.mark.asyncio
    @pytest.mark.parametrize("compartment_id", ["alpha", None])
    async def test_yaml_download_marks_configured_compartment(self, tmp_path: Path, compartment_id: str | None) -> None:
        app, service = _make_app(tmp_path)
        app.state.settings = app.state.settings.model_copy(update={"compartment_id": compartment_id})
        client = TestClient(app)
        session = await service.create_session("alice", "Export compartment", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={
                    "plugin": "csv",
                    "on_success": "main",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
                outputs=[
                    {
                        "name": "main",
                        "plugin": "csv",
                        "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Export compartment", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            response = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert response.status_code == 200
        document = response.json()["yaml"]
        if compartment_id is None:
            assert not document.startswith("# compartment_id:")
        else:
            assert document.startswith(f"# compartment_id: {compartment_id}\n")
        assert yaml.safe_load(document)["sources"]["source"]["plugin"] == "csv"

    @pytest.mark.asyncio
    async def test_post_state_yaml_with_disabled_plugin_is_atomic(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        _install_restricted_plugin_policy(app, PluginId("sink", "database"))
        client = TestClient(app)
        session = await service.create_session("alice", "Policy atomicity", "local")
        before = await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        yaml_text = """
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
    plugin: database
    options: {}
    on_write_failure: discard
"""

        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "plugin_not_enabled"
        after = await service.get_current_state(session.id)
        assert after is not None
        assert after.id == before.id
        assert after.version == before.version

    @pytest.mark.asyncio
    async def test_saved_disabled_state_is_readable_and_exported_as_authored(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        snapshot = _install_restricted_plugin_policy(app, PluginId("transform", "llm"))
        app.state.operator_profile_registry.lower_options.side_effect = AssertionError("export must not lower private bindings")
        client = TestClient(app)
        session = await service.create_session("alice", "Historical disabled plugin", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                sources={
                    "source": {
                        "plugin": "csv",
                        "on_success": "score",
                        "options": {"schema": {"mode": "observed"}},
                        "on_validation_failure": "discard",
                    }
                },
                nodes=[
                    {
                        "id": "score",
                        "node_type": "transform",
                        "plugin": "llm",
                        "input": "source",
                        "on_success": "main",
                        "on_error": "discard",
                        "options": {
                            "profile": "task-role",
                            "prompt_template": "Score {{ row }}",
                            "schema": {"mode": "observed"},
                        },
                    }
                ],
                outputs=[
                    {
                        "name": "main",
                        "plugin": "json",
                        "options": {"path": "outputs/scored.jsonl"},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Historical disabled plugin", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )

        state_response = client.get(f"/api/sessions/{session.id}/state")
        export_response = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert state_response.status_code == 200
        body = state_response.json()
        assert body["nodes"][0]["plugin"] == "llm"
        assert body["plugin_policy_findings"] == [
            {
                "component_id": "score",
                "plugin_id": "transform:llm",
                "reason_code": "plugin_not_enabled",
                "snapshot_fingerprint": snapshot.snapshot_hash,
            }
        ]
        assert export_response.status_code == 200
        exported_yaml = export_response.json()["yaml"]
        assert "plugin: llm" in exported_yaml
        assert "profile: task-role" in exported_yaml
        assert "bedrock" not in exported_yaml
        assert "credential" not in exported_yaml

    @pytest.mark.asyncio
    @pytest.mark.parametrize("malformed_component", ["source", "transform", "output"])
    async def test_saved_malformed_plugin_policy_component_fails_loudly(
        self,
        tmp_path: Path,
        malformed_component: str,
    ) -> None:
        """A corrupt component must not disappear from a mixed persisted cohort."""
        sources: dict[str, dict[str, Any]] = {
            "valid_source": {
                "plugin": "csv",
                "on_success": "valid_node",
                "options": {},
                "on_validation_failure": "discard",
            }
        }
        nodes: list[dict[str, Any]] = [
            {
                "id": "valid_node",
                "node_type": "transform",
                "plugin": "llm",
                "input": "valid_source",
                "on_success": "valid_output",
                "on_error": "discard",
                "options": {},
            }
        ]
        outputs: list[dict[str, Any]] = [{"name": "valid_output", "plugin": "json", "options": {}, "on_write_failure": "discard"}]
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Corrupt plugin projection", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                sources=sources,
                nodes=nodes,
                outputs=outputs,
                metadata_={"name": "Corrupt plugin projection", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )
        baseline = client.get(f"/api/sessions/{session.id}/state")
        assert baseline.status_code == 200, baseline.text
        if malformed_component == "source":
            sources["corrupt_source"] = {}
        elif malformed_component == "transform":
            nodes.append({"id": "corrupt_node", "node_type": "transform"})
        else:
            # ``sink_name`` belonged to the retired runtime-event shape. A
            # persisted composition output has one canonical identity key.
            outputs.append({"sink_name": "corrupt_output", "plugin": "json"})

        corrupt = await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                sources=sources,
                nodes=nodes,
                outputs=outputs,
                metadata_={"name": "Corrupt plugin projection", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )
        versions_before = await service.get_state_versions(session.id)
        response = client.get(f"/api/sessions/{session.id}/state")
        assert response.status_code == 409, response.text
        assert response.json() == {
            "detail": {
                "error_type": "composition_state_unavailable",
                "detail": "The saved graph is unavailable. You can ask Composer to recover a saved draft.",
                "state_id": str(corrupt.id),
                "version": corrupt.version,
            }
        }
        assert await service.get_state_versions(session.id) == versions_before

    @pytest.mark.asyncio
    async def test_get_state_succeeds_for_collector_bearing_persisted_state(self, tmp_path: Path) -> None:
        """GET /state must serve a collector-bearing persisted state.

        Regression (found live by the 2026-08-26 S1-S5 battery round): the
        plugin-policy walker had no collector arm, so every read of a
        collector-bearing state raised AuditIntegrityError -> HTTP 500 on the
        composer's primary read path.
        """
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Collector state read", "local")
        await service.save_composition_state(
            session.id,
            CompositionStateData(
                sources={
                    "source": {
                        "plugin": "csv",
                        "on_success": "pages",
                        "options": {},
                        "on_validation_failure": "discard",
                    }
                },
                nodes=[
                    {
                        "id": "page_stitcher",
                        "node_type": "collector",
                        "plugin": "batch_stats",
                        "input": "pages",
                        "on_success": "out",
                        "on_error": "discard",
                        "options": {},
                    }
                ],
                outputs=[{"name": "out", "plugin": "json", "options": {}, "on_write_failure": "discard"}],
                metadata_={"name": "Collector state read", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )

        response = client.get(f"/api/sessions/{session.id}/state")

        assert response.status_code == 200, response.text
        body = response.json()
        assert [n["id"] for n in body["nodes"] if n["node_type"] == "collector"] == ["page_stitcher"]

    def test_plugin_policy_projection_preserves_legacy_source_and_node_variants(self, tmp_path: Path) -> None:
        """The legacy source bridge is explicit; structural nodes are not plugins."""
        from elspeth.web.catalog.policy_view import PolicyCatalogView
        from elspeth.web.sessions.routes._helpers import _plugin_policy_findings

        app, _service = _make_app(tmp_path)
        snapshot = _install_restricted_plugin_policy(app)
        catalog = PolicyCatalogView(
            app.state.catalog_service,
            snapshot,
            app.state.operator_profile_registry,
        )
        state = CompositionStateRecord(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            version=1,
            source={"plugin": "missing_source"},
            sources=None,
            nodes=[
                {"id": "transform_node", "node_type": "transform", "plugin": "missing_transform"},
                {"id": "aggregation_node", "node_type": "aggregation", "plugin": "missing_aggregation"},
                # Collector: plugin-bearing (batch-transform contract, ADR-042).
                # Regression: the walker raised "unknown node_type 'collector'"
                # on every collector-bearing persisted state, turning GET
                # /state into a 500 (found live by the 2026-08-26 S1-S5
                # battery round).
                {"id": "collector_node", "node_type": "collector", "plugin": "missing_collector"},
                {"id": "gate_node", "node_type": "gate", "plugin": None},
                {"id": "coalesce_node", "node_type": "coalesce", "plugin": None},
                {"id": "row_union_node", "node_type": "row_union", "plugin": None},
                {"id": "queue_node", "node_type": "queue", "plugin": None},
            ],
            edges=None,
            outputs=[{"name": "missing_output", "plugin": "missing_sink"}],
            metadata_=None,
            is_valid=False,
            validation_errors=None,
            created_at=datetime.now(UTC),
            derived_from_state_id=None,
        )

        findings = _plugin_policy_findings(state, catalog)

        assert [(finding.component_id, finding.plugin_id, finding.reason_code) for finding in findings] == [
            ("source", "source:missing_source", "plugin_not_installed"),
            ("transform_node", "transform:missing_transform", "plugin_not_installed"),
            ("aggregation_node", "transform:missing_aggregation", "plugin_not_installed"),
            ("collector_node", "transform:missing_collector", "plugin_not_installed"),
            ("missing_output", "sink:missing_sink", "plugin_not_installed"),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid_component", ["source", "sink"])
    async def test_post_state_yaml_persists_aws_s3_endpoint_url_as_invalid(
        self,
        tmp_path: Path,
        invalid_component: str,
    ) -> None:
        endpoint_sentinel = "https://yaml-canary.attacker.invalid/private"
        source_options = {"endpoint_url": endpoint_sentinel} if invalid_component == "source" else {}
        sink_options = {"endpoint_url": endpoint_sentinel} if invalid_component == "sink" else {}
        yaml_text = yaml.safe_dump(
            {
                "sources": {
                    "source": {
                        "plugin": "aws_s3" if invalid_component == "source" else "csv",
                        "on_success": "main",
                        "options": source_options,
                        "on_validation_failure": "discard",
                    }
                },
                "sinks": {
                    "main": {
                        "plugin": "aws_s3" if invalid_component == "sink" else "json",
                        "options": sink_options,
                        "on_write_failure": "discard",
                    }
                },
            }
        )
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "AWS S3 policy import", "local")

        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_valid"] is False
        assert [error["message"] for error in body["validation_errors"]] == [AWS_S3_ENDPOINT_URL_POLICY_ERROR]
        assert endpoint_sentinel not in repr(body["validation_errors"])
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.is_valid is False
        assert [error.message for error in record.validation_errors or ()] == [AWS_S3_ENDPOINT_URL_POLICY_ERROR]
        assert endpoint_sentinel not in repr(record.validation_errors)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid_component", ["source", "sink"])
    async def test_e2e_seed_persists_aws_s3_endpoint_url_as_invalid(
        self,
        tmp_path: Path,
        invalid_component: str,
    ) -> None:
        endpoint_sentinel = "https://seed-canary.attacker.invalid/private"
        source = SourceSpec(
            plugin="aws_s3" if invalid_component == "source" else "csv",
            on_success="main",
            options={"endpoint_url": endpoint_sentinel} if invalid_component == "source" else {},
            on_validation_failure="discard",
        )
        output = OutputSpec(
            name="main",
            plugin="aws_s3" if invalid_component == "sink" else "json",
            options={"endpoint_url": endpoint_sentinel} if invalid_component == "sink" else {},
            on_write_failure="discard",
        )
        seeded_state = CompositionState(
            source=source,
            nodes=(),
            edges=(),
            outputs=(output,),
            metadata=PipelineMetadata(name="AWS S3 policy seed"),
            version=1,
        )
        app, service = _make_app(tmp_path)
        app.state.settings = app.state.settings.model_copy(update={"e2e_state_seed_enabled": True})
        client = TestClient(app)
        session = await service.create_session("alice", "AWS S3 policy seed", "local")

        response = client.post(
            f"/api/sessions/{session.id}/state/e2e-seed",
            json={"state": seeded_state.to_dict()},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_valid"] is False
        assert [error["message"] for error in body["validation_errors"]] == [AWS_S3_ENDPOINT_URL_POLICY_ERROR]
        assert endpoint_sentinel not in repr(body["validation_errors"])
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.is_valid is False
        assert [error.message for error in record.validation_errors or ()] == [AWS_S3_ENDPOINT_URL_POLICY_ERROR]
        assert endpoint_sentinel not in repr(record.validation_errors)

    @pytest.mark.asyncio
    async def test_post_state_yaml_imports_exported_runtime_yaml(self, tmp_path) -> None:
        """Replay can seed a fresh session from captured final_yaml.json."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        blob_id = uuid.uuid4()
        blob_path = tmp_path / "blobs" / str(session.id) / f"{blob_id}_input.csv"
        blob_path.parent.mkdir(parents=True)
        blob_path.write_text("id\n1\n")
        app.state.blob_service = MagicMock(spec=BlobServiceProtocol)
        app.state.blob_service.get_blob.return_value = SimpleNamespace(
            id=blob_id,
            session_id=session.id,
            storage_path=str(blob_path),
            status="ready",
        )
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      path: /old/blob.csv
      schema:
        mode: observed
      on_validation_failure: discard
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
      schema:
        mode: observed
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": yaml_text, "source_blob_ids": {"source": str(blob_id)}},
            )

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.sources["source"]["plugin"] == "csv"
        assert record.sources["source"]["options"]["path"] == str(blob_path)
        assert record.outputs[0]["name"] == "main"

    @pytest.mark.asyncio
    async def test_post_state_yaml_surfaces_llm_review_events(self, tmp_path) -> None:
        """Importing YAML with an llm node must surface resolvable pending
        interpretation EVENTS, not just fail-closed requirements
        (elspeth-ae5160c3cb). Without the events the run gate blocks while no
        review card renders and nothing can resolve the block."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: score
    on_validation_failure: discard
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
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None

        events = await service.list_interpretation_events(session.id, status="pending")
        by_kind = {event.kind: event for event in events}
        assert set(by_kind) == {InterpretationKind.LLM_PROMPT_TEMPLATE, InterpretationKind.LLM_MODEL_CHOICE}
        pt = by_kind[InterpretationKind.LLM_PROMPT_TEMPLATE]
        assert pt.affected_node_id == "score"
        assert pt.llm_draft == "Score this: {{ row.value }}"
        assert pt.user_term == "llm_prompt_template:score"
        assert str(pt.composition_state_id) == str(record.id)
        assert pt.tool_call_id is not None and pt.tool_call_id.startswith("backend_auto_surface:")
        # The import consulted no LLM: the origin names the route and the LLM
        # provenance is empty, not a label standing in for a model.
        assert pt.surface_origin is InterpretationSurfaceOrigin.YAML_IMPORT
        assert pt.model_identifier is None
        assert pt.model_version is None
        assert pt.provider is None
        assert pt.composer_skill_hash is None
        mc = by_kind[InterpretationKind.LLM_MODEL_CHOICE]
        assert mc.affected_node_id == "score"
        assert mc.llm_draft == "anthropic/claude-haiku-4.5"
        assert mc.user_term == "llm_model_choice:score"

    @pytest.mark.asyncio
    async def test_post_state_yaml_opt_out_returns_the_final_durable_head(self, tmp_path: Path) -> None:
        """Auto-resolution may advance the head while cards are surfaced;
        the import response must describe that final durable state."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Opt-out import", "local")
        async with _compose_session_operation_context(service, session.id) as opt_out_context:
            await service.record_session_interpretation_opt_out(
                session_id=session.id,
                actor="user:alice",
                session_operation_context=opt_out_context,
            )
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: score
    options:
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 200, response.text
        current = await service.get_current_state(session.id)
        assert current is not None
        assert response.json()["id"] == str(current.id)
        assert response.json()["version"] == current.version
        assert await service.list_interpretation_events(session.id, status="pending") == []

    @pytest.mark.asyncio
    async def test_post_state_yaml_surfaces_source_data_contract_review(self, tmp_path: Path) -> None:
        """A YAML-bound uploaded source uses the same source-site surfacer as
        Composer settlement, so its derived contract debt is visible and
        resolvable instead of blocking execution behind an empty card list."""
        from elspeth.web.composer.source_demand import build_source_data_contract_draft
        from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
        from elspeth.web.sessions.routes._helpers import _state_from_record

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Source contract import", "local")
        blob_id = uuid.uuid4()
        blob_path = tmp_path / "blobs" / str(session.id) / f"{blob_id}_input.csv"
        blob_path.parent.mkdir(parents=True)
        blob_path.write_text("colour,extra\nred,1\n", encoding="utf-8")
        app.state.blob_service = MagicMock(spec=BlobServiceProtocol)
        app.state.blob_service.get_blob.return_value = SimpleNamespace(
            id=blob_id,
            session_id=session.id,
            storage_path=str(blob_path),
            status="ready",
        )
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: source
    options:
      on_validation_failure: discard
      path: /old/blob.csv
      schema:
        mode: observed
transforms:
- name: consumer
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    required_input_fields: [colour]
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            response = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": yaml_text, "source_blob_ids": {"source": str(blob_id)}},
            )
            reimported = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": yaml_text, "source_blob_ids": {"source": str(blob_id)}},
            )

        assert response.status_code == 200, response.text
        assert reimported.status_code == 200, reimported.text
        record = await service.get_current_state(session.id)
        assert record is not None
        blocked = materialize_state_for_execution(_state_from_record(record))
        assert isinstance(blocked, InterpretationReviewPending)
        assert [(site.component_id, site.kind) for site in blocked.sites] == [("source", InterpretationKind.SOURCE_DATA_CONTRACT)]

        events = await service.list_interpretation_events(session.id, status="pending")
        # Same source contract on a new imported state reuses the live card;
        # a twin would make the exactly-one resolver boundary unconsumable.
        assert [(event.affected_node_id, event.kind) for event in events] == [("source", InterpretationKind.SOURCE_DATA_CONTRACT)]
        event = events[0]
        assert event.llm_draft == build_source_data_contract_draft(["colour"], ("colour", "extra"))
        assert event.surface_origin is InterpretationSurfaceOrigin.YAML_IMPORT
        assert event.model_identifier is None
        assert event.composer_skill_hash is None

        resolved = client.post(
            f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
            json={"choice": "accepted_as_drafted"},
        )
        assert resolved.status_code == 200, resolved.text
        current = await service.get_current_state(session.id)
        assert current is not None
        assert not isinstance(materialize_state_for_execution(_state_from_record(current)), InterpretationReviewPending)

    @pytest.mark.asyncio
    async def test_post_state_yaml_mixed_review_order_matches_generic_surfacer(self, tmp_path: Path) -> None:
        """Source and node review cards retain the generic surfacer's stable
        ordering rather than a YAML-route-specific node-only projection."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Mixed review import", "local")
        blob_id = uuid.uuid4()
        blob_path = tmp_path / "blobs" / str(session.id) / f"{blob_id}_input.csv"
        blob_path.parent.mkdir(parents=True)
        blob_path.write_text("colour,extra\nred,1\n", encoding="utf-8")
        app.state.blob_service = MagicMock(spec=BlobServiceProtocol)
        app.state.blob_service.get_blob.return_value = SimpleNamespace(
            id=blob_id,
            session_id=session.id,
            storage_path=str(blob_path),
            status="ready",
        )
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: source
    options:
      on_validation_failure: discard
      path: /old/blob.csv
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.colour }}'
    required_input_fields: [colour]
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            response = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": yaml_text, "source_blob_ids": {"source": str(blob_id)}},
            )

        assert response.status_code == 200, response.text
        events = await service.list_interpretation_events(session.id, status="pending")
        assert [(event.affected_node_id, event.kind) for event in events] == [
            ("score", InterpretationKind.LLM_PROMPT_TEMPLATE),
            ("source", InterpretationKind.SOURCE_DATA_CONTRACT),
            ("score", InterpretationKind.LLM_MODEL_CHOICE),
        ]
        assert all(event.surface_origin is InterpretationSurfaceOrigin.YAML_IMPORT for event in events)
        assert all(event.composer_skill_hash is None for event in events)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [ValueError("writer refused"), RuntimeError("storage failed")])
    async def test_post_state_yaml_rolls_back_state_and_event_prefix_when_event_cohort_fails(
        self,
        tmp_path: Path,
        failure: Exception,
    ) -> None:
        """The state and every review card are one durability cohort."""
        app, service = _make_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        session = await service.create_session("alice", "Atomic import", "local")
        yaml_text = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
"""
        original_prepare = service._prepare_or_create_pending_interpretation_event
        prepared_count = 0

        async def _inject_second_writer_failure(*args, **kwargs):
            nonlocal prepared_count
            writer = await original_prepare(*args, **kwargs)
            prepared_count += 1
            if prepared_count != 2:
                return writer

            def _fail(_connection):
                raise failure

            return _fail

        with patch.object(
            service,
            "_prepare_or_create_pending_interpretation_event",
            side_effect=_inject_second_writer_failure,
        ):
            response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 500
        assert await service.get_current_state(session.id) is None
        assert await service.list_interpretation_events(session.id, status="all") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("draft_yaml", ["null", "''"])
    async def test_post_state_yaml_rejects_unsurfaceable_pending_site_atomically(
        self,
        tmp_path: Path,
        draft_yaml: str,
    ) -> None:
        """Every persisted pending review site must have a consumable event.

        A hand-written vague-term row without a draft is schema-valid enough
        to block execution but cannot pass the event writer boundary. Reject
        the whole import before writing either state or event rows.
        """
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Unsupported review import", "local")
        yaml_text = f"""
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    interpretation_requirements:
    - id: vague:score
      kind: vague_term
      user_term: recent
      status: pending
      draft: {draft_yaml}
"""

        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 400, response.text
        assert "cannot be surfaced" in response.json()["detail"]
        assert await service.get_current_state(session.id) is None
        assert await service.list_interpretation_events(session.id, status="all") == []

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_vague_term_site_the_writer_cannot_surface(self, tmp_path: Path) -> None:
        """The pure precheck must include the LLM-transform discriminator
        enforced by the event writer, not just the requirement draft."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Writer mismatch", "local")
        yaml_text = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    interpretation_requirements:
    - id: vague:score
      kind: vague_term
      user_term: recent
      status: pending
      draft: last 30 days
"""

        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 400, response.text
        assert "cannot be surfaced" in response.json()["detail"]
        assert await service.get_current_state(session.id) is None
        assert await service.list_interpretation_events(session.id, status="all") == []

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_structured_vague_term_without_prompt_part(self, tmp_path: Path) -> None:
        """A structured vague-term card must bind exactly one prompt part,
        matching the writer's reviewed-content identity boundary."""
        app, service = _make_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        session = await service.create_session("alice", "Vague-term wiring mismatch", "local")
        yaml_text = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    prompt_template: 'Score records from the last 30 days'
    interpretation_requirements:
    - id: vague:score
      kind: vague_term
      user_term: recent
      status: pending
      draft: last 30 days
"""

        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 400, response.text
        assert "cannot be surfaced" in response.json()["detail"]
        assert await service.get_current_state(session.id) is None
        assert await service.list_interpretation_events(session.id, status="all") == []

    @pytest.mark.asyncio
    async def test_post_state_yaml_accepts_wired_kindless_legacy_vague_term(self, tmp_path: Path) -> None:
        """The canonical parser defaults an absent kind to vague_term; the
        surfacer must not falsely reject that writer-valid legacy shape."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Legacy vague term", "local")
        yaml_text = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score records from the last 30 days'
    prompt_template_parts:
    - kind: text
      text: 'Score records from '
    - kind: interpretation_ref
      requirement_id: vague:score
    interpretation_requirements:
    - id: vague:score
      user_term: recent
      status: pending
      draft: last 30 days
"""

        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 200, response.text
        events = await service.list_interpretation_events(session.id, status="pending")
        assert InterpretationKind.VAGUE_TERM in {event.kind for event in events}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "yaml_text",
        [
            pytest.param(
                """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    prompt_template: [not, a, string]
    interpretation_requirements:
    - id: prompt:score
      kind: llm_prompt_template
      user_term: llm_prompt_template:score
      status: pending
      draft: a draft
""",
                id="malformed-prompt-template",
            ),
            pytest.param(
                """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      on_validation_failure: discard
      source_authoring:
        content_hash: abc123
""",
                id="malformed-source-authoring",
            ),
        ],
    )
    async def test_post_state_yaml_sanitizes_malformed_review_metadata(
        self,
        tmp_path: Path,
        yaml_text: str,
    ) -> None:
        """Untrusted review metadata is a named 400, never an invariant 500."""
        app, service = _make_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        session = await service.create_session("alice", "Malformed review metadata", "local")

        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Imported YAML contains malformed interpretation review metadata."
        assert await service.get_current_state(session.id) is None
        assert await service.list_interpretation_events(session.id, status="all") == []

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_malformed_interpretation_requirements(self, tmp_path) -> None:
        """Hand-written interpretation_requirements rows the schema would
        refuse are rejected 400 before persistence (elspeth-ae5160c3cb)."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
    interpretation_requirements:
    - kind: not_a_kind
      user_term: x
      status: pending
"""
        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
        assert resp.status_code == 400
        assert "score" in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_post_state_yaml_reviews_are_resolvable_and_unblock_execution(self, tmp_path) -> None:
        """The defect in elspeth-ae5160c3cb was an UNRESOLVABLE block: the run
        gate held while nothing existed to resolve. Lock in the full loop —
        import, accept both surfaced cards, requirements patch to resolved,
        no pending events remain, and the interpretation run gate clears."""
        from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
        from elspeth.web.sessions.routes._helpers import _state_from_record

        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: score
    options:
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
        assert resp.status_code == 200, resp.text

        events = await service.list_interpretation_events(session.id, status="pending")
        assert len(events) == 2
        for event in events:
            resolve_resp = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert resolve_resp.status_code == 200, resolve_resp.text

        assert await service.list_interpretation_events(session.id, status="pending") == []
        record = await service.get_current_state(session.id)
        assert record is not None
        node = next(n for n in record.nodes if n["id"] == "score")
        statuses = {r["kind"]: r["status"] for r in node["options"]["interpretation_requirements"]}
        assert statuses == {"llm_prompt_template": "resolved", "llm_model_choice": "resolved"}

        gate_result = materialize_state_for_execution(_state_from_record(record))
        assert not isinstance(gate_result, InterpretationReviewPending)

    @pytest.mark.asyncio
    async def test_post_state_yaml_reimport_does_not_duplicate_pending_reviews(self, tmp_path) -> None:
        """Re-importing the same YAML must not surface twin pending events
        (elspeth-1fcaec9b63). The resolve path demands exactly one pending
        requirement per (node, kind, user_term), so a duplicate event is
        permanently unresolvable once its twin resolves: the card 422s with
        interpretation_placeholder_unavailable in a loop reload never clears."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: score
    options:
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            for _ in range(2):
                resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
                assert resp.status_code == 200, resp.text

        events = await service.list_interpretation_events(session.id, status="pending")
        kinds = sorted(event.kind.value for event in events)
        assert kinds == ["llm_model_choice", "llm_prompt_template"], kinds

        for event in events:
            resolve_resp = client.post(
                f"/api/sessions/{session.id}/interpretations/{event.id}/resolve",
                json={"choice": "accepted_as_drafted"},
            )
            assert resolve_resp.status_code == 200, resolve_resp.text

        assert await service.list_interpretation_events(session.id, status="pending") == []

    @pytest.mark.asyncio
    async def test_post_state_yaml_allows_wired_secret_ref_marker(self, tmp_path) -> None:
        """Hardening (T-1) no-false-positive lock-in: a legitimately wired
        {secret_ref: NAME} marker in a credential field -- the form export
        itself produces -- must import cleanly, not trip the new
        fabricated-literal-credential rejection."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      api_key: {secret_ref: OPENAI_API_KEY}
      on_validation_failure: discard
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.sources["source"]["options"]["api_key"] == {"secret_ref": "OPENAI_API_KEY"}

    @pytest.mark.asyncio
    async def test_post_state_yaml_allows_structural_key_literal(self, tmp_path) -> None:
        """Regression (elspeth-61f2c0732e): a literal ``data_key`` — the JSON
        source's structural extraction key — must import cleanly rather than
        tripping the fabricated-literal-credential rejection via the bare
        ``_key`` suffix heuristic."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  source:
    plugin: json
    on_success: main
    options:
      data_key: results
      on_validation_failure: discard
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.sources["source"]["options"]["data_key"] == "results"

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_blob_storage_path_without_sidecar(self, tmp_path) -> None:
        """Path-only imports must not bypass source blob ownership checks."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        blob_path = tmp_path / "blobs" / "other-session" / "old.csv"
        blob_path.parent.mkdir(parents=True)
        blob_path.write_text("id\n1\n")
        yaml_text = f"""
sources:
  source:
    plugin: csv
    on_success: main
    options:
      path: {blob_path}
      on_validation_failure: discard
sinks:
  main:
    plugin: csv
    on_write_failure: discard
"""

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 400
        assert "source_blob_ids" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_source_path_outside_allowed_directories(self, tmp_path) -> None:
        """Imports must fail before persistence when a source path cannot run."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  primary:
    plugin: json
    on_success: out
    options:
      path: examples/json_explode/input.json
      on_validation_failure: discard
sinks:
  out:
    plugin: json
    on_write_failure: discard
"""

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 400
        assert (
            resp.json()["detail"]
            == "Path traversal blocked: source 'primary' path='examples/json_explode/input.json' resolves outside allowed directories"
        )
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_post_state_yaml_remaps_source_blob_ids_to_owned_blob(self, tmp_path) -> None:
        """Replay imports bind captured source blobs to the newly uploaded blob row."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        blob_id = uuid.uuid4()
        blob_path = tmp_path / "blobs" / str(session.id) / f"{blob_id}_input.csv"
        blob_path.parent.mkdir(parents=True)
        blob_path.write_text("id\n1\n")
        app.state.blob_service = MagicMock(spec=BlobServiceProtocol)
        app.state.blob_service.get_blob.return_value = SimpleNamespace(
            id=blob_id,
            session_id=session.id,
            storage_path=str(blob_path),
            status="ready",
        )
        old_blob_path = tmp_path / "blobs" / "old-session" / "old.csv"
        yaml_text = f"""
sources:
  source:
    plugin: csv
    on_success: main
    options:
      path: {old_blob_path}
      on_validation_failure: discard
sinks:
  main:
    plugin: csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": yaml_text, "source_blob_ids": {"source": str(blob_id)}},
            )

        assert resp.status_code == 200, resp.text
        app.state.blob_service.get_blob.assert_awaited_once_with(blob_id, session_operation_context=ANY)
        assert type(app.state.blob_service.get_blob.await_args.kwargs["session_operation_context"]) is SessionOperationContext
        record = await service.get_current_state(session.id)
        assert record is not None
        source_options = record.sources["source"]["options"]
        assert source_options["blob_ref"] == str(blob_id)
        assert source_options["path"] == str(blob_path)

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_unknown_source_blob_sidecar_entry(self, tmp_path) -> None:
        """source_blob_ids cannot bind blobs to sources absent from the YAML."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
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

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": yaml_text, "source_blob_ids": {"other": str(uuid.uuid4())}},
        )

        assert resp.status_code == 400
        assert "unknown source" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_malformed_source_blob_id(self, tmp_path) -> None:
        """source_blob_ids values must be UUIDs before any blob lookup."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        app.state.blob_service = MagicMock(spec=BlobServiceProtocol)
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

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": yaml_text, "source_blob_ids": {"source": "not-a-uuid"}},
        )

        assert resp.status_code == 400
        assert "must be a UUID" in resp.json()["detail"]
        app.state.blob_service.get_blob.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_cross_session_source_blob(self, tmp_path) -> None:
        """Replay sidecars cannot attach a blob owned by another session."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        other_session_id = uuid.uuid4()
        blob_id = uuid.uuid4()
        app.state.blob_service = MagicMock(spec=BlobServiceProtocol)
        app.state.blob_service.get_blob.return_value = SimpleNamespace(
            id=blob_id,
            session_id=other_session_id,
            storage_path=str(tmp_path / "blobs" / str(other_session_id) / "input.csv"),
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

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": yaml_text, "source_blob_ids": {"source": str(blob_id)}},
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Blob not found"
        app.state.blob_service.get_blob.assert_awaited_once_with(blob_id, session_operation_context=ANY)
        assert type(app.state.blob_service.get_blob.await_args.kwargs["session_operation_context"]) is SessionOperationContext

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_oversized_document(self, tmp_path) -> None:
        """Hardening (T-1): a paste over the Pydantic body cap is rejected as 422,
        and (production parity) the oversized content is not echoed back in the
        error body.

        The egress redaction is a global ``RequestValidationError`` handler in
        ``web/app.py`` (``handle_validation_error`` -- allowlists only
        type/loc/msg, dropping FastAPI's default ``input`` echo) rather than
        anything endpoint-specific, so it is registered here explicitly:
        ``_make_app`` builds a bare ``FastAPI()`` without the production
        app's exception handlers wired up.
        """
        app, service = _make_app(tmp_path)

        _SAFE_VALIDATION_ERROR_KEYS = frozenset({"type", "loc", "msg"})

        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse

        @app.exception_handler(RequestValidationError)
        async def _handle_validation_error(request, exc: RequestValidationError) -> JSONResponse:
            safe_errors = [{k: v for k, v in error.items() if k in _SAFE_VALIDATION_ERROR_KEYS} for error in exc.errors()]
            return JSONResponse(status_code=422, content={"detail": safe_errors})

        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        oversized = "sources:\n  source:\n    plugin: csv\n" + ("x" * 300_000)

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": oversized})

        assert resp.status_code == 422
        assert "x" * 100 not in resp.text

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_malformed_yaml_syntax(self, tmp_path) -> None:
        """Hardening (T-1): non-YAML paste is a categorized 400, never a 500,
        and the error body does not echo the pasted content or raw parser prose."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        not_yaml = "sources: [unterminated\n  plugin: csv"

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": not_yaml})

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail == "YAML parse failed: ParserError"
        assert "unterminated" not in resp.text
        assert "plugin" not in resp.text

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_non_pipeline_mapping(self, tmp_path) -> None:
        """Hardening (T-1): a syntactically valid YAML mapping that names no
        pipeline section must not silently import as an empty composition --
        that would be a silent destructive replace of the session's prior work.
        The rejection names every unrecognised key so a typo is actionable."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        not_a_pipeline = "shopping_list:\n  - milk\n  - eggs\nnotes: just some random yaml\n"

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": not_a_pipeline})

        assert resp.status_code == 400
        assert resp.json()["detail"] == (
            "pipeline YAML contains top-level content the composer cannot import: "
            "unknown keys ['notes', 'shopping_list']. Importing would silently discard or override it."
        )
        # The session's current state must remain unset -- nothing was persisted.
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_unknown_key_before_persistence(self, tmp_path) -> None:
        """A typo beside valid sections is a named 400, never a lossy import."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      schema:
        mode: observed
sinks:
  main:
    plugin: csv
    on_write_failure: discard
commencment_gates: []
"""

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 400
        assert resp.json()["detail"] == (
            "pipeline YAML contains top-level content the composer cannot import: "
            "unknown keys ['commencment_gates']. Importing would silently discard or override it."
        )
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_json_key_collision_before_persistence(self, tmp_path) -> None:
        """Distinct YAML keys must not collapse during session JSON storage."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      schema:
        mode: observed
      labels:
        1: integer-key
        "1": string-key
sinks:
  main:
    plugin: csv
    on_write_failure: discard
"""

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 400
        assert resp.json()["detail"] == "sources.source.options.labels contains non-string mapping key 1"
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_aliased_yaml(self, tmp_path) -> None:
        """Hardening (T-1): anchors/aliases are rejected outright (billion-laughs
        defense) rather than silently expanded server-side."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        aliased = """
sources:
  source: &src
    plugin: csv
    on_success: out
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
sinks:
  out:
    plugin: csv
    on_write_failure: discard
also: *src
"""

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": aliased})

        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("YAML parse failed: ")

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_literal_credential_value(self, tmp_path) -> None:
        """Hardening (T-1): a pasted literal credential (not a {secret_ref: ...}
        marker) in a credential-bearing field is rejected outright, before any
        persistence -- unlike the tool-call composer surface, which only
        catches this at /validate or runtime-preflight time (after the value
        is already written into CompositionState), pasted YAML has no prior
        tool-call gate, so it would otherwise reach save_composition_state and
        get echoed back in the response verbatim. The response carries only
        the fixed refusal classification and never the field or value."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        secret_value = "sk-live-totally-real-credential-do-not-leak-1234567890"
        yaml_text = f"""
sources:
  source:
    plugin: csv
    on_success: main
    options:
      api_key: {secret_value}
      on_validation_failure: discard
sinks:
  main:
    plugin: csv
    on_write_failure: discard
"""

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 422
        assert resp.json() == {
            "detail": {
                "error_type": "credential_material_rejected",
                "failure_code": "credential_material_rejected",
                "detail": (
                    "This control content appears to contain a credential. Store the value through the secret service and use a secret reference."
                ),
                "category": "credential_field",
                "surface": "composer_yaml_import",
                "detector_version": "1",
            },
            "request_id": None,
        }
        assert "api_key" not in resp.text
        assert secret_value not in resp.text
        # Nothing was persisted -- the session has no composition state at all.
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_post_state_yaml_preserves_field_mapper_data_labels(self, tmp_path) -> None:
        from elspeth.web.blobs.service import BlobServiceImpl
        from tests.helpers.session_fences import fenced_operation_context

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Data field mapping", "local")
        blobs = BlobServiceImpl(service._engine, tmp_path, session_operation_authority=service.session_operation_authority)
        app.state.blob_service = blobs
        with fenced_operation_context(service._engine, str(session.id), operation_kind=SessionOperationKind.CREATE) as upload_context:
            blob = await blobs.create_blob(
                session.id, "customers.csv", b"customer_key\nC1\n", "text/csv", session_operation_context=upload_context
            )
        yaml_text = """
sources:
  source:
    plugin: csv
    on_success: rows
    options:
      path: SYNTHETIC_SOURCE_PATH
      on_validation_failure: discard
      schema:
        mode: fixed
        fields: ['customer_key: str']
transforms:
  - name: rename_customer
    plugin: field_mapper
    input: rows
    on_success: main
    on_error: discard
    options:
      mapping: {customer_key: customer_id}
      schema: {mode: observed}
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
      schema: {mode: observed}
    on_write_failure: discard
"""
        response = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": yaml_text.replace("SYNTHETIC_SOURCE_PATH", blob.storage_path), "source_blob_ids": {"source": str(blob.id)}},
        )
        assert response.status_code == 200, response.text
        current = await service.get_current_state(session.id)
        assert current is not None
        assert current.nodes[0]["options"]["mapping"] == {"customer_key": "customer_id"}

    @pytest.mark.asyncio
    async def test_post_state_yaml_rejects_plugin_specific_credential_field(self, tmp_path) -> None:
        """Hardening (T-1 review follow-up): the fabricated-secret import gate
        must match plugin-specific credential fields, not just the name/suffix
        heuristic. The database sink's whole-connection ``url`` field does not
        end in a secret suffix, so the heuristic predicate alone lets an opaque
        value through -- the import gate feeds
        allowed_secret_ref_fields per component (mirroring the set_output tool
        gate) so pasted connection material is rejected here rather than
        persisted and echoed back. The response carries only the fixed refusal
        classification and never the field or value."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Replay", "local")
        connection_value = "opaque-database-connection-material"
        yaml_text = f"""
sources:
  source:
    plugin: csv
    on_success: main
    options:
      on_validation_failure: discard
sinks:
  main:
    plugin: database
    on_write_failure: discard
    options:
      url: {connection_value}
"""

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 422
        assert resp.json() == {
            "detail": {
                "error_type": "credential_material_rejected",
                "failure_code": "credential_material_rejected",
                "detail": (
                    "This control content appears to contain a credential. Store the value through the secret service and use a secret reference."
                ),
                "category": "credential_field",
                "surface": "composer_yaml_import",
                "detector_version": "1",
            },
            "request_id": None,
        }
        assert "url" not in resp.text
        assert connection_value not in resp.text
        # Nothing was persisted -- the connection material never reached the DB.
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_yaml_returns_yaml_when_state_exists(self, tmp_path) -> None:
        """Returns generated YAML for a valid state even when edge_contracts is empty."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={"plugin": "csv", "on_success": "out", "options": {"path": "/data.csv"}, "on_validation_failure": "quarantine"},
                outputs=[
                    {
                        "name": "out",
                        "plugin": "csv",
                        "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Test Pipeline", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")
        assert resp.status_code == 200
        body = resp.json()
        assert "yaml" in body
        assert "csv" in body["yaml"]

    @pytest.mark.asyncio
    async def test_yaml_response_omits_source_blob_identity_sidecar(self, tmp_path) -> None:
        """Public YAML export must not expose blob UUIDs beside scrubbed YAML."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        blob_id = "98b1357d-5aab-4fb3-85b4-5ad643912e84"

        session = await service.create_session("alice", "Pipeline", "local")
        app.state.blob_service = SimpleNamespace(
            get_blob=AsyncMock(
                spec=BlobServiceProtocol.get_blob,
                return_value=_ready_blob_record(
                    blob_id=uuid.UUID(blob_id),
                    session_id=session.id,
                    storage_path="/data/blobs/session/contact_form_submissions.csv",
                ),
            )
        )
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={
                    "plugin": "csv",
                    "on_success": "out",
                    "options": {
                        "path": "/data/blobs/session/contact_form_submissions.csv",
                        "blob_ref": blob_id,
                        "mode": "bind_source",
                        "schema": {"mode": "observed"},
                    },
                    "on_validation_failure": "quarantine",
                },
                outputs=[
                    {
                        "name": "out",
                        "plugin": "csv",
                        "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Blob-backed source", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 200
        body = resp.json()
        assert "source_blob_ids" not in body
        assert blob_id not in json.dumps(body, sort_keys=True)
        assert blob_id not in body["yaml"]
        assert "/data/blobs/session/contact_form_submissions.csv" not in body["yaml"]
        exported_source_options = yaml.safe_load(body["yaml"])["sources"]["source"]["options"]
        assert "path" not in exported_source_options
        assert "mode" not in exported_source_options
        assert exported_source_options["schema"] == {"mode": "observed"}
        # elspeth-06f92da0d9: the deliberate redaction is never presented
        # bare — the response names what was stripped (names only, no blob
        # identity: the sidecar scrub above still holds) and the YAML text
        # opens with the matching marker comment.
        # The summary names custody carriers (storage paths, blob linkage);
        # the bind_source "mode" marker is web-layer bookkeeping and stays out.
        assert body["redaction"] == {
            "stripped_source_options": {"source": ["blob_ref", "path"]},
            "stripped_node_options": {},
            "stripped_output_options": {"out": ["path"]},
            "blob_linked_sources": ["source"],
            "rebind_guidance": PUBLIC_EXPORT_REBIND_GUIDANCE,
        }
        assert body["yaml"].startswith(PUBLIC_EXPORT_REDACTION_HEADER)

    @pytest.mark.asyncio
    async def test_yaml_export_accounts_for_a_node_file_marker_it_removed(self, tmp_path: Path) -> None:
        """A transform's uploaded-file marker is removed whole and named.

        Before the fix the export kept a headless ``{mode, sha256, encoding}``
        fragment in the node's options and returned no redaction account at
        all, because nothing else in this graph was stripped.
        """
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        blob_id = "22222222-2222-4222-8222-222222222222"
        file_digest = "cd" * 32
        session = await service.create_session("alice", "Node file", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={
                    "plugin": "csv",
                    "on_success": "lookup_in",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
                nodes=[
                    {
                        "id": "lookup",
                        "node_type": "transform",
                        "plugin": "reference_join",
                        "input": "lookup_in",
                        "on_success": "out",
                        "on_error": "discard",
                        "options": {
                            "tables": {
                                "t": {
                                    "reference_content": {
                                        "blob_ref": blob_id,
                                        "mode": "inline_content",
                                        "sha256": file_digest,
                                        "encoding": "utf-8",
                                    },
                                    "reference_format": "csv",
                                }
                            }
                        },
                    }
                ],
                outputs=[{"name": "out", "plugin": "json", "options": {}, "on_write_failure": "discard"}],
                metadata_={"name": "Node file", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 200
        body = resp.json()
        assert blob_id not in json.dumps(body, sort_keys=True)
        assert file_digest not in json.dumps(body, sort_keys=True)
        exported_node_options = yaml.safe_load(body["yaml"])["transforms"][0]["options"]
        assert exported_node_options == {"tables": {"t": {"reference_format": "csv"}}}
        assert body["redaction"] == {
            "stripped_source_options": {},
            "stripped_node_options": {"lookup": ["tables.t.reference_content"]},
            "stripped_output_options": {},
            "blob_linked_sources": [],
            "rebind_guidance": PUBLIC_EXPORT_REBIND_GUIDANCE,
        }
        assert body["yaml"].startswith(PUBLIC_EXPORT_REDACTION_HEADER)

    @pytest.mark.asyncio
    async def test_reimport_of_redacted_export_without_rebind_gets_guidance_not_pydantic(self, tmp_path: Path) -> None:
        """Round-trip fence (elspeth-06f92da0d9): POSTing a custody-redacted
        export back without ``source_blob_ids`` must 400 with re-bind guidance
        instead of sailing through Stage-1 into a strict-lane
        "path Field required"."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        blob_id = "98b1357d-5aab-4fb3-85b4-5ad643912e84"
        session = await service.create_session("alice", "Redacted round trip", "local")
        app.state.blob_service = SimpleNamespace(
            get_blob=AsyncMock(
                spec=BlobServiceProtocol.get_blob,
                return_value=_ready_blob_record(
                    blob_id=uuid.UUID(blob_id),
                    session_id=session.id,
                    storage_path="/data/blobs/session/contact_form_submissions.csv",
                ),
            )
        )
        await service.save_composition_state(
            session.id,
            CompositionStateData(
                source={
                    "plugin": "csv",
                    "on_success": "out",
                    "options": {
                        "path": "/data/blobs/session/contact_form_submissions.csv",
                        "blob_ref": blob_id,
                        "schema": {"mode": "observed"},
                    },
                    "on_validation_failure": "discard",
                },
                outputs=[
                    {
                        "name": "out",
                        "plugin": "csv",
                        "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Redacted round trip", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            exported = client.get(f"/api/sessions/{session.id}/state/yaml")
            assert exported.status_code == 200, exported.text
            reimported = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": exported.json()["yaml"]},
            )

        assert reimported.status_code == 400, reimported.text
        detail = reimported.json()["detail"]
        assert "custody-redacted" in detail
        assert "source_blob_ids" in detail
        assert "path Field required" not in detail

    @pytest.mark.asyncio
    async def test_yaml_allows_connection_valid_state_without_ui_edges(self, tmp_path) -> None:
        """Connection-defined pipelines should export even when the editor graph is incomplete."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={
                    "plugin": "text",
                    "on_success": "mapper_in",
                    "options": {
                        "path": "/data/input.txt",
                        "column": "text",
                        "schema": {"mode": "observed"},
                    },
                    "on_validation_failure": "quarantine",
                },
                nodes=[
                    {
                        "id": "map_body",
                        "node_type": "transform",
                        "plugin": "field_mapper",
                        "input": "mapper_in",
                        "on_success": "main",
                        "on_error": "discard",
                        "options": {
                            "schema": {"mode": "observed", "guaranteed_fields": ["text"], "required_fields": ["text"]},
                            "mapping": {"text": "body"},
                        },
                        "condition": None,
                        "routes": None,
                        "fork_to": None,
                        "branches": None,
                        "policy": None,
                        "merge": None,
                    },
                ],
                edges=[],
                outputs=[
                    {
                        "name": "main",
                        "plugin": "csv",
                        "options": {
                            "path": "outputs/out.csv",
                            "schema": {"mode": "observed", "required_fields": ["body"]},
                        },
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Connection-only Pipeline", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")
        assert resp.status_code == 200
        assert "field_mapper" in resp.json()["yaml"]
        assert "body" in resp.json()["yaml"]

    @pytest.mark.asyncio
    async def test_yaml_serializes_coalesce_on_success_runtime_route(self, tmp_path) -> None:
        """Coalesce terminal routing must survive export/reload parity checks."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={
                    "plugin": "csv",
                    "on_success": "gate_in",
                    "options": {"path": "/data/input.csv"},
                    "on_validation_failure": "quarantine",
                },
                nodes=[
                    {
                        "id": "fork_gate",
                        "node_type": "gate",
                        "plugin": None,
                        "input": "gate_in",
                        "on_success": None,
                        "on_error": None,
                        "options": {},
                        "condition": "True",
                        "routes": {},
                        "fork_to": ["path_a", "path_b"],
                        "branches": None,
                        "policy": None,
                        "merge": None,
                    },
                    {
                        "id": "merge_point",
                        "node_type": "coalesce",
                        "plugin": None,
                        "input": "join",
                        "on_success": "main",
                        "on_error": None,
                        "options": {},
                        "condition": None,
                        "routes": None,
                        "fork_to": None,
                        "branches": ["path_a", "path_b"],
                        "policy": "require_all",
                        "merge": "nested",
                    },
                ],
                edges=[],
                outputs=[
                    {
                        "name": "main",
                        "plugin": "csv",
                        "options": {"path": "outputs/out.csv"},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Fork and merge", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 200
        doc = yaml.safe_load(resp.json()["yaml"])
        assert doc["coalesce"][0]["on_success"] == "main"

    def test_yaml_returns_404_when_no_state(self, tmp_path) -> None:
        """No composition state yet -> 404."""
        app, _ = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/sessions", json={"title": "Empty"})
        session_id = resp.json()["id"]

        yaml_resp = client.get(f"/api/sessions/{session_id}/state/yaml")
        assert yaml_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_state_yaml_validates_exact_state_snapshot(self, tmp_path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={
                    "plugin": "csv",
                    "on_success": "main",
                    "options": {"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "quarantine",
                },
                outputs=[
                    {
                        "name": "main",
                        "plugin": "csv",
                        "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Snapshot", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        captured_states: list[CompositionState] = []

        async def fake_runtime_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            captured_states.append(state)
            return ValidationResult(
                is_valid=False,
                checks=[],
                errors=[
                    ValidationError(
                        component_id="source",
                        component_type="source",
                        message="runtime preflight failed for captured state",
                        suggestion=None,
                        error_code=None,
                    )
                ],
            )

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=fake_runtime_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Current composition state failed runtime preflight. Fix validation errors before exporting YAML."
        assert "runtime preflight failed for captured state" not in resp.text
        assert len(captured_states) == 1
        assert captured_states[0].sources["source"] is not None

    @pytest.mark.asyncio
    async def test_get_state_yaml_does_not_echo_preflight_error_messages(self, tmp_path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(metadata_={"name": "Snapshot", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
        leaked_value = "REDACTED-preflight-error-canary"

        async def fake_runtime_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            del state, settings, secret_service, user_id, session_id
            return ValidationResult(
                is_valid=False,
                checks=[],
                errors=[
                    ValidationError(
                        component_id="scraper",
                        component_type="transform",
                        message=f"Invalid CIDR in allowed_hosts: {leaked_value!r}",
                        suggestion=None,
                        error_code=None,
                    )
                ],
            )

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=fake_runtime_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Current composition state failed runtime preflight. Fix validation errors before exporting YAML."
        assert leaked_value not in resp.text

    @pytest.mark.asyncio
    async def test_get_state_yaml_threads_session_id_to_runtime_preflight(self, tmp_path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(metadata_={"name": "Snapshot", "description": ""}, is_valid=True),
            provenance="session_seed",
        )
        seen_session_ids: list[uuid.UUID] = []

        async def capture_session_id(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            del state, settings, secret_service, user_id
            seen_session_ids.append(session_id)
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=capture_session_id):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 200, resp.text
        assert seen_session_ids == [session.id]

    @pytest.mark.asyncio
    async def test_get_state_yaml_emits_yaml_export_telemetry_on_passed_preflight(self, tmp_path, monkeypatch) -> None:
        from elspeth.web.sessions.routes import _helpers as routes_module

        emitted: list[tuple[str, dict[str, str]]] = []

        class FakeCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((str(value), dict(attributes)))

        monkeypatch.setattr(routes_module, "_COMPOSER_RUNTIME_PREFLIGHT_COUNTER", FakeCounter())
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(metadata_={"name": "Snapshot", "description": ""}, is_valid=True),
            provenance="session_seed",
        )

        async def pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=pass_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 200
        assert any(attrs.get("source") == "yaml_export" and attrs.get("result") == "passed" for _, attrs in emitted)

    @pytest.mark.asyncio
    async def test_get_state_yaml_emits_yaml_export_telemetry_on_failed_preflight(self, tmp_path, monkeypatch) -> None:
        from elspeth.web.sessions.routes import _helpers as routes_module

        emitted: list[tuple[str, dict[str, str]]] = []

        class FakeCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((str(value), dict(attributes)))

        monkeypatch.setattr(routes_module, "_COMPOSER_RUNTIME_PREFLIGHT_COUNTER", FakeCounter())
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(metadata_={"name": "Snapshot", "description": ""}, is_valid=True),
            provenance="session_seed",
        )

        failure = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[ValidationError(component_id=None, component_type=None, message="bad runtime", suggestion=None, error_code=None)],
        )

        async def fail_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return failure

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=fail_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 409
        assert any(attrs.get("source") == "yaml_export" and attrs.get("result") == "failed" for _, attrs in emitted)

    @pytest.mark.asyncio
    async def test_get_state_yaml_handles_preflight_exception_with_telemetry_and_409(self, tmp_path, monkeypatch) -> None:
        """Preflight exceptions must surface as 409 with bounded telemetry, not 500 with raw exception text."""
        from elspeth.web.sessions.routes import _helpers as routes_module

        emitted: list[tuple[str, dict[str, str]]] = []

        class FakeCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((str(value), dict(attributes)))

        monkeypatch.setattr(routes_module, "_COMPOSER_RUNTIME_PREFLIGHT_COUNTER", FakeCounter())
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(metadata_={"name": "Snapshot", "description": ""}, is_valid=True),
            provenance="session_seed",
        )

        secret_canary = "this-text-must-not-appear-in-the-response-body"

        async def boom(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            raise TimeoutError(secret_canary)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=boom):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 409
        # The raw exception text must not leak into the response body.
        assert secret_canary not in resp.text
        assert "Runtime preflight could not complete" in resp.json()["detail"]
        # Telemetry recorded the exception class as a bounded attribute.
        assert any(
            attrs.get("source") == "yaml_export" and attrs.get("result") == "exception" and attrs.get("exception_class") == "TimeoutError"
            for _, attrs in emitted
        )

    @pytest.mark.asyncio
    async def test_get_state_yaml_propagates_programmer_bugs_uncaught(self, tmp_path, monkeypatch) -> None:
        """I4a lock-in: the YAML-export catch is narrowed to user-fixable
        exception classes (TimeoutError, OSError, PluginConfigError,
        PluginNotFoundError, GraphValidationError). Programmer-bug classes
        (AttributeError, TypeError, KeyError, RuntimeError, ImportError)
        MUST propagate uncaught so:

        * Operators see the real traceback via FastAPI's default 500
          handler, not "Runtime preflight could not complete; YAML export
          aborted." which falsely implies a user-fixable failure.
        * No yaml_export telemetry is emitted for programmer bugs — the
          exception counter is reserved for the user-fixable bucket so
          dashboards measure the real preflight failure rate, not bugs
          we introduced ourselves.

        Per the ``engine-patterns-reference`` skill §Offensive Programming
        Examples: programmer bugs MUST
        crash. Conflating them with user-fixable failures destroys the
        operator's ability to diagnose.
        """
        from elspeth.web.sessions.routes import _helpers as routes_module

        emitted: list[tuple[str, dict[str, str]]] = []

        class FakeCounter:
            def add(self, value: int, attributes: dict[str, str]) -> None:
                emitted.append((str(value), dict(attributes)))

        monkeypatch.setattr(routes_module, "_COMPOSER_RUNTIME_PREFLIGHT_COUNTER", FakeCounter())
        app, service = _make_app(tmp_path)
        # raise_server_exceptions=False so Starlette returns the 500 body
        # rather than re-raising the AttributeError into the test runner.
        client = TestClient(app, raise_server_exceptions=False)
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(metadata_={"name": "Snapshot", "description": ""}, is_valid=True),
            provenance="session_seed",
        )

        async def programmer_bug(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            # A real bug we'd see if e.g. a refactor accidentally broke
            # an attribute lookup inside validate_pipeline. AttributeError
            # is in the canonical "programmer bug" set per the
            # engine-patterns-reference skill §Offensive Programming Examples.
            raise AttributeError("'NoneType' object has no attribute 'plugin'")

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=programmer_bug):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        # FastAPI's default 500 handler emits the bare 'Internal Server
        # Error' string body when an unhandled exception escapes a route.
        assert resp.status_code == 500
        assert "Runtime preflight could not complete" not in resp.text, (
            f"Programmer bug must not be relabeled as a user-fixable preflight failure; body was: {resp.text!r}"
        )
        # No yaml_export telemetry should fire — the exception bucket is
        # reserved for user-fixable preflight outcomes.
        assert not any(attrs.get("source") == "yaml_export" and attrs.get("result") == "exception" for _, attrs in emitted), (
            "yaml_export exception telemetry fired for a programmer bug — "
            "the catch should not have intercepted AttributeError. "
            f"emitted={emitted}"
        )

    @pytest.mark.asyncio
    async def test_get_state_yaml_preserves_secret_ref_markers_in_output(self, tmp_path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        resolved_secret = "__RESOLVED_SECRET_CANARY_DO_NOT_EXPORT__"  # secret-scan: allow-this-line

        class FakeResolvedSecretService:
            resolved_value = resolved_secret

        app.state.scoped_secret_resolver = FakeResolvedSecretService()
        session = await service.create_session("alice", "Pipeline", "local")
        await _save_test_composition_state(
            service,
            session.id,
            CompositionStateData(
                source={
                    "plugin": "csv",
                    "on_success": "main",
                    "options": {
                        "path": "/data/blobs/input.csv",
                        "schema": {"mode": "observed"},
                        "api_key": {"secret_ref": "OPENAI_API_KEY"},
                    },
                    "on_validation_failure": "quarantine",
                },
                outputs=[
                    {
                        "name": "main",
                        "plugin": "csv",
                        "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
                metadata_={"name": "Secret export", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        async def fake_runtime_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            assert secret_service is None
            # YAML export preflight must not receive the scoped resolver. It only
            # serializes the original state snapshot with the secret_ref marker.
            assert state.to_dict()["sources"]["source"]["options"]["api_key"] == {"secret_ref": "OPENAI_API_KEY"}
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=fake_runtime_preflight):
            resp = client.get(f"/api/sessions/{session.id}/state/yaml")

        assert resp.status_code == 200
        exported_yaml = resp.json()["yaml"]
        assert resolved_secret not in exported_yaml
        parsed = yaml.safe_load(exported_yaml)
        assert parsed["sources"]["source"]["options"]["api_key"] == {"secret_ref": "OPENAI_API_KEY"}

    @pytest.mark.asyncio
    async def test_post_state_yaml_imports_and_persists_queue(self, tmp_path) -> None:
        """A pasted runtime queue section survives import and is persisted as a
        canonical structural queue node (elspeth-a5b86149d4)."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Queue import", "local")
        yaml_text = """
sources:
  orders:
    plugin: csv
    on_success: inbound
    options:
      on_validation_failure: discard
      schema:
        mode: observed
  refunds:
    plugin: csv
    on_success: inbound
    options:
      on_validation_failure: discard
      schema:
        mode: observed
queues:
  inbound:
    description: Orders and refunds interleave here
transforms:
- name: normalize
  plugin: passthrough
  input: inbound
  on_success: main
  on_error: discard
  options:
    schema:
      mode: observed
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        queue_nodes = [node for node in record.nodes if node["node_type"] == "queue"]
        assert len(queue_nodes) == 1
        assert queue_nodes[0]["id"] == "inbound"
        assert queue_nodes[0]["options"] == {"description": "Orders and refunds interleave here"}

    @pytest.mark.asyncio
    async def test_post_state_yaml_malformed_queue_is_rejected_atomically(self, tmp_path) -> None:
        """Malformed queue YAML is a 400 that leaves the prior persisted state and
        version untouched — the import boundary is all-or-nothing."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)

        session = await service.create_session("alice", "Queue atomicity", "local")
        valid_yaml = """
sources:
  orders:
    plugin: csv
    on_success: inbound
    on_validation_failure: discard
    options:
      schema:
        mode: observed
  refunds:
    plugin: csv
    on_success: inbound
    on_validation_failure: discard
    options:
      schema:
        mode: observed
queues:
  inbound: {}
transforms:
- name: normalize
  plugin: passthrough
  input: inbound
  on_success: main
  on_error: discard
  options:
    schema:
      mode: observed
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""
        malformed_yaml = valid_yaml.replace("  inbound: {}", "  inbound:\n    priority: 5")

        async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
            return ValidationResult(is_valid=True, checks=[], errors=[])

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            ok = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": valid_yaml})
        assert ok.status_code == 200, ok.text
        before = await service.get_current_state(session.id)
        assert before is not None
        version_before = before.version
        nodes_before = list(before.nodes)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
            bad = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": malformed_yaml})

        assert bad.status_code == 400
        assert "priority" in bad.json()["detail"]
        after = await service.get_current_state(session.id)
        assert after is not None
        assert after.version == version_before
        assert list(after.nodes) == nodes_before


@pytest.mark.asyncio
async def test_get_state_yaml_is_409_while_another_compose_is_live(tmp_path) -> None:
    """The export records a completion event, so it holds COMPOSE authority and
    contends with a live compose (elspeth-bf52d495a2 option A; recorded as an
    observation — a state-scoped audit-only kind would relax it)."""
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session_id = uuid.UUID(client.post("/api/sessions", json={"title": "Exporting"}).json()["id"])
    writer = service.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    try:
        # This bare app registers no exception handlers, so the conflict the
        # full app maps to 409 (app.py) surfaces here as the raised error.
        with pytest.raises(SessionOperationConflictError):
            client.get(f"/api/sessions/{session_id}/state/yaml")
    finally:
        service.session_operation_authority.release(writer)
    # A page read admission does not contend with the export.
    reader = service.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    try:
        assert client.get(f"/api/sessions/{session_id}/state/yaml").status_code != 409
    finally:
        service.session_operation_authority.release(reader)


# ---------------------------------------------------------------------------
# Shared-graph import: re-binding inline file markers and multi-file sources
# ---------------------------------------------------------------------------

_REBIND_DIGEST_A = "a1" * 32
_REBIND_DIGEST_B = "b2" * 32

_REBIND_NODE_YAML = """
sources:
  source:
    plugin: csv
    on_success: lookup_in
    on_validation_failure: discard
    options:
      schema:
        mode: observed
transforms:
- name: lookup
  plugin: reference_join
  input: lookup_in
  on_success: main
  on_error: discard
  options:
    tables:
      t:
        reference_format: csv
sinks:
  main:
    plugin: json
    on_write_failure: discard
"""


_REBIND_GATE_YAML = """
sources:
  source:
    plugin: csv
    on_success: gate_in
    on_validation_failure: discard
    options:
      schema:
        mode: observed
gates:
  - name: threshold
    input: gate_in
    condition: row['amount'] > 500
    routes:
      true: high
      false: standard
sinks:
  high:
    plugin: json
    on_write_failure: discard
  standard:
    plugin: json
    on_write_failure: discard
"""

_REBIND_LLM_YAML = """
sources:
  source:
    plugin: csv
    on_success: score
    on_validation_failure: discard
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
    model: anthropic/claude-haiku-4.5
sinks:
  main:
    plugin: json
    on_write_failure: discard
"""

_BLOB_ROWS_YAML = """
sources:
  source:
    plugin: blob_rows
    on_success: main
    on_validation_failure: discard
    options: {}
sinks:
  main:
    plugin: json
    on_write_failure: discard
"""


def _hashed_blob(
    *,
    blob_id: uuid.UUID,
    session_id: uuid.UUID,
    content_hash: str,
    filename: str = "reference.csv",
    status: str = "ready",
) -> Any:
    from dataclasses import replace as dataclass_replace

    record = _ready_blob_record(blob_id=blob_id, session_id=session_id, storage_path=f"/data/blobs/{session_id}/{filename}")
    return dataclass_replace(record, content_hash=content_hash, size_bytes=17, status=status)


def _blob_service_for(*blobs: Any) -> SimpleNamespace:
    from elspeth.contracts.blobs import BlobNotFoundError

    by_id = {blob.id: blob for blob in blobs}

    async def _get_blob(blob_id: uuid.UUID, *, session_operation_context: Any) -> Any:
        if blob_id not in by_id:
            raise BlobNotFoundError(str(blob_id))
        return by_id[blob_id]

    return SimpleNamespace(get_blob=AsyncMock(spec=BlobServiceProtocol.get_blob, side_effect=_get_blob))


async def _rebind_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
    return ValidationResult(is_valid=True, checks=[], errors=[])


blob_env = _file_inject_tool_parity.blob_env


class TestImportRebindsSharedFiles:
    @pytest.mark.asyncio
    async def test_import_rebinds_inline_marker_at_field_path(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        node_blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        output_blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_B)
        app.state.blob_service = _blob_service_for(node_blob, output_blob)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={
                    "yaml": _REBIND_NODE_YAML,
                    "inline_blob_bindings": {
                        "node:lookup.options.tables.t.reference_content": {"blob_id": str(node_blob.id)},
                        # Missing intermediate mappings are created.
                        "output:main.options.template.body": {"blob_id": str(output_blob.id)},
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        node_options = record.nodes[0]["options"]
        assert node_options["tables"] == {
            "t": {
                "reference_format": "csv",
                "reference_content": {"blob_ref": str(node_blob.id), "mode": "inline_content", "sha256": _REBIND_DIGEST_A},
            }
        }
        assert record.outputs[0]["options"]["template"] == {
            "body": {"blob_ref": str(output_blob.id), "mode": "inline_content", "sha256": _REBIND_DIGEST_B}
        }

    @pytest.mark.asyncio
    async def test_import_inline_binding_keeps_non_utf8_encoding(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={
                    "yaml": _REBIND_NODE_YAML,
                    "inline_blob_bindings": {
                        "node:lookup.options.tables.t.reference_content": {"blob_id": str(blob.id), "encoding": "latin-1"},
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.nodes[0]["options"]["tables"]["t"]["reference_content"] == {
            "blob_ref": str(blob.id),
            "mode": "inline_content",
            "sha256": _REBIND_DIGEST_A,
            "encoding": "latin-1",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "field_path",
        [
            # The leaf already holds a value.
            "node:lookup.options.tables.t.reference_format",
            # An intermediate segment holds a non-mapping value.
            "node:lookup.options.tables.t.reference_format.inner",
        ],
    )
    async def test_import_inline_binding_refuses_occupied_path(self, tmp_path: Path, field_path: str) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _REBIND_NODE_YAML, "inline_blob_bindings": {field_path: {"blob_id": str(blob.id)}}},
        )

        assert resp.status_code == 400, resp.text
        assert field_path in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("blob_session", "status", "expected_status", "expected_detail"),
        [
            ("missing", "ready", 404, "Blob not found"),
            ("foreign", "ready", 404, "Blob not found"),
            ("own", "pending", 409, "Blob is not ready"),
        ],
    )
    async def test_import_inline_binding_refuses_foreign_or_unready_blob(
        self,
        tmp_path: Path,
        blob_session: str,
        status: str,
        expected_status: int,
        expected_detail: str,
    ) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        owner_session_id = session.id if blob_session == "own" else uuid.uuid4()
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=owner_session_id, content_hash=_REBIND_DIGEST_A, status=status)
        app.state.blob_service = _blob_service_for() if blob_session == "missing" else _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={
                "yaml": _REBIND_NODE_YAML,
                "inline_blob_bindings": {"node:lookup.options.tables.t.reference_content": {"blob_id": str(blob.id)}},
            },
        )

        assert resp.status_code == expected_status, resp.text
        assert resp.json()["detail"] == expected_detail
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "field_path",
        [
            "lookup.options.tables.t.reference_content",  # no component prefix
            "node:lookup.tables.t.reference_content",  # no .options segment
            "node:lookup.options.tables[0].reference_content",  # list index
            "node:missing.options.reference_content",  # unknown component
            # Server-owned source roots: file linkage is bound by
            # source_blob_ids / source_blob_rows, never as an inline marker.
            "source:source.options.blob_ref",
            "source:source.options.blobs",
            "source:source.options.source_authoring",
        ],
    )
    async def test_import_rejects_malformed_field_path_with_400(self, tmp_path: Path, field_path: str) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _REBIND_NODE_YAML, "inline_blob_bindings": {field_path: {"blob_id": str(blob.id)}}},
        )

        assert resp.status_code == 400, resp.text
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_rebinds_blob_rows_source_in_order(self, tmp_path: Path) -> None:
        from elspeth.web.composer.yaml_generator import generate_public_yaml, public_export_redaction_header

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        first = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A, filename="a.csv")
        second = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_B, filename="b.csv")
        app.state.blob_service = _blob_service_for(first, second)
        # Tom's graph, projected exactly as the public export projects it: the
        # blobs list is removed whole and the export marker names the source.
        tom_state = CompositionState(
            source=SourceSpec(
                plugin="blob_rows",
                on_success="main",
                options={
                    "blobs": [
                        {
                            "blob_id": str(uuid.uuid4()),
                            "payload_ref": _REBIND_DIGEST_A,
                            "filename": "a.csv",
                            "mime_type": "text/csv",
                            "size_bytes": 17,
                        }
                    ]
                },
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(OutputSpec(name="main", plugin="json", options={}, on_write_failure="discard"),),
            metadata=PipelineMetadata(),
            version=1,
        )
        yaml_text = public_export_redaction_header(tom_state) + generate_public_yaml(tom_state)
        assert "blobs" not in yaml.safe_load(yaml_text)["sources"]["source"]["options"]

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": yaml_text, "source_blob_rows": {"source": {"blob_ids": [str(second.id), str(first.id)]}}},
            )

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        from elspeth.contracts.freeze import deep_thaw

        assert deep_thaw(record.sources["source"]["options"]["blobs"]) == [
            {"blob_id": str(second.id), "payload_ref": _REBIND_DIGEST_B, "filename": "b.csv", "mime_type": "text/csv", "size_bytes": 17},
            {"blob_id": str(first.id), "payload_ref": _REBIND_DIGEST_A, "filename": "a.csv", "mime_type": "text/csv", "size_bytes": 17},
        ]

    @pytest.mark.asyncio
    async def test_import_blob_rows_binding_refuses_non_blob_rows_source(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _REBIND_NODE_YAML, "source_blob_rows": {"source": {"blob_ids": [str(blob.id)]}}},
        )

        assert resp.status_code == 400, resp.text
        assert "blob_rows" in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("blob_session", "status", "expected_status", "expected_detail"),
        [
            ("missing", "ready", 404, "Blob not found"),
            ("foreign", "ready", 404, "Blob not found"),
            ("own", "pending", 409, "Blob is not ready"),
            ("own", "error", 409, "Blob is not ready"),
        ],
    )
    async def test_import_blob_rows_binding_refuses_foreign_or_unready_blob(
        self,
        tmp_path: Path,
        blob_session: str,
        status: str,
        expected_status: int,
        expected_detail: str,
    ) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        owner_session_id = session.id if blob_session == "own" else uuid.uuid4()
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=owner_session_id, content_hash=_REBIND_DIGEST_A, status=status)
        app.state.blob_service = _blob_service_for() if blob_session == "missing" else _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _BLOB_ROWS_YAML, "source_blob_rows": {"source": {"blob_ids": [str(blob.id)]}}},
        )

        assert resp.status_code == expected_status, resp.text
        assert resp.json()["detail"] == expected_detail
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("blob_ids", "detail_fragment"),
        [
            pytest.param(lambda first, second: [str(first.id), str(first.id)], "the same blob more than once", id="same-blob-twice"),
            pytest.param(lambda first, second: [str(first.id), "not-a-uuid"], "blob_ids[1] must be a UUID", id="not-a-uuid"),
            pytest.param(lambda first, second: [str(first.id), str(second.id)], "two files with the same content", id="same-content"),
        ],
    )
    async def test_import_blob_rows_binding_refuses_duplicates_and_malformed_ids(
        self, tmp_path: Path, blob_ids: Callable[[Any, Any], list[str]], detail_fragment: str
    ) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        first = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A, filename="a.csv")
        second = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A, filename="b.csv")
        app.state.blob_service = _blob_service_for(first, second)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _BLOB_ROWS_YAML, "source_blob_rows": {"source": {"blob_ids": blob_ids(first, second)}}},
        )

        assert resp.status_code == 400, resp.text
        assert detail_fragment in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_blob_rows_binding_refuses_unknown_source(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _BLOB_ROWS_YAML, "source_blob_rows": {"missing": {"blob_ids": [str(blob.id)]}}},
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "source_blob_rows references unknown source 'missing'"
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_blob_rows_binding_refuses_llm_authored_blob(self, tmp_path: Path) -> None:
        """Parity with the canonical set_source_from_blobs writer: an LLM-authored file needs its review flow."""
        from dataclasses import replace as dataclass_replace

        from elspeth.contracts.enums import CreationModality

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = dataclass_replace(
            _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A),
            creation_modality=CreationModality.LLM_GENERATED,
        )
        app.state.blob_service = _blob_service_for(blob)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": _BLOB_ROWS_YAML, "source_blob_rows": {"source": {"blob_ids": [str(blob.id)]}}},
            )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"].startswith(f"source_blob_rows.source: blob '{blob.id}' is LLM-authored")
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_rebinds_inline_marker_on_a_source(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={
                    "yaml": _REBIND_NODE_YAML,
                    "inline_blob_bindings": {"source:source.options.lookup.doc": {"blob_id": str(blob.id)}},
                },
            )

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.sources["source"]["options"]["lookup"] == {
            "doc": {"blob_ref": str(blob.id), "mode": "inline_content", "sha256": _REBIND_DIGEST_A}
        }
        # Re-binding completes the imported graph; it is not an edit of it.
        assert record.version == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("yaml_text", "field_path", "expected_detail"),
        [
            (
                _REBIND_GATE_YAML,
                "node:threshold.options.doc",
                "Inline blob references can only be wired into source, transform, aggregation, collector, or output plugin options.",
            ),
            (
                _REBIND_NODE_YAML,
                "node:lookup.options.interpretation_requirements.doc",
                "wire_blob_inline_ref cannot write node interpretation_requirements",
            ),
            (
                _REBIND_NODE_YAML,
                "source:source.options.interpretation_requirements.doc",
                "wire_blob_inline_ref cannot write source interpretation_requirements",
            ),
            (
                _REBIND_NODE_YAML,
                "output:main.options.interpretation_requirements.doc",
                "wire_blob_inline_ref cannot write output interpretation_requirements",
            ),
            (
                _REBIND_LLM_YAML,
                "node:score.options.approved_prompt_artifact_hash",
                "targets runtime-owned top-level LLM option 'approved_prompt_artifact_hash'",
            ),
        ],
        ids=[
            "gate-node",
            "node-interpretation-requirements",
            "source-interpretation-requirements",
            "output-interpretation-requirements",
            "llm-runtime-owned-hash",
        ],
    )
    async def test_import_inline_binding_refuses_what_the_canonical_writer_refuses(
        self,
        tmp_path: Path,
        yaml_text: str,
        field_path: str,
        expected_detail: str,
    ) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={"yaml": yaml_text, "inline_blob_bindings": {field_path: {"blob_id": str(blob.id)}}},
            )

        assert resp.status_code == 400, resp.text
        assert expected_detail in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_inline_binding_refuses_llm_authored_blob_on_prompt_surface(self, tmp_path: Path) -> None:
        from dataclasses import replace as dataclass_replace

        from elspeth.contracts.enums import CreationModality

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = dataclass_replace(
            _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A),
            creation_modality=CreationModality.LLM_GENERATED,
        )
        app.state.blob_service = _blob_service_for(blob)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={
                    "yaml": _REBIND_LLM_YAML,
                    "inline_blob_bindings": {"node:score.options.prompt_template": {"blob_id": str(blob.id)}},
                },
            )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"].startswith(
            f"wire_blob_inline_ref cannot wire LLM-authored blob '{blob.id}' into LLM node 'score' option 'prompt_template'"
        )
        assert await service.get_current_state(session.id) is None

    @staticmethod
    def _file_inject_yaml(entry_lines: str = "", option_lines: str = "") -> str:
        return f"""
sources:
  source:
    plugin: csv
    on_success: inject_in
    on_validation_failure: discard
    options:
      schema:
        mode: observed
transforms:
- name: inject
  plugin: file_inject
  input: inject_in
  on_success: main
  on_error: discard
  options:
    schema:
      mode: observed
    mode: select
    key_field: kind
    output_field: guidance
{option_lines}    files:
      invoice:
        format: text
{entry_lines}sinks:
  main:
    plugin: json
    on_write_failure: discard
"""

    @pytest.mark.asyncio
    async def test_import_rebinds_user_uploaded_file_inject_content(self, tmp_path: Path) -> None:
        """The shared-graph case: the projection removed the marker, the copy is re-pointed at the same path."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A, filename="guidance.txt")
        app.state.blob_service = _blob_service_for(blob)

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={
                    "yaml": self._file_inject_yaml(),
                    "inline_blob_bindings": {"node:inject.options.files.invoice.content": {"blob_id": str(blob.id)}},
                },
            )

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.nodes[0]["options"]["files"] == {
            "invoice": {
                "format": "text",
                "content": {"blob_ref": str(blob.id), "mode": "inline_content", "sha256": _REBIND_DIGEST_A},
            }
        }

    @pytest.mark.asyncio
    async def test_import_keeps_a_file_inject_marker_written_in_the_yaml(self, tmp_path: Path) -> None:
        """A marker already in the YAML is not literal content: it is left untouched for /validate to resolve."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob_id = uuid.uuid4()
        marker_lines = (
            f"        content:\n          blob_ref: {blob_id}\n          mode: inline_content\n          sha256: {_REBIND_DIGEST_A}\n"
        )

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": self._file_inject_yaml(marker_lines)})

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.nodes[0]["options"]["files"]["invoice"]["content"] == {
            "blob_ref": str(blob_id),
            "mode": "inline_content",
            "sha256": _REBIND_DIGEST_A,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("created_by", ["assistant", "pipeline"])
    async def test_import_inline_binding_refuses_verbatim_blob_the_user_did_not_upload(self, tmp_path: Path, created_by: str) -> None:
        """A fork copy of a planner blob, a composer create_blob and a pipeline output all record VERBATIM."""
        from dataclasses import replace as dataclass_replace

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = dataclass_replace(
            _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A, filename="guidance.txt"),
            created_by=created_by,
        )
        app.state.blob_service = _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={
                "yaml": self._file_inject_yaml(),
                "inline_blob_bindings": {"node:inject.options.files.invoice.content": {"blob_id": str(blob.id)}},
            },
        )

        assert resp.status_code == 400, resp.text
        assert f"blob '{blob.id}' is not a file the user uploaded" in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_following_the_literal_content_refusal_imports_and_attaches(self, tmp_path: Path, blob_env: dict[str, Any]) -> None:
        """The refusal says: remove the literal, import again, upload, attach. Each step is run here."""
        from elspeth.contracts.freeze import deep_thaw
        from elspeth.web.composer.tools._common import _prevalidate_plugin_options
        from elspeth.web.sessions.converters import state_from_record
        from tests.unit.web.composer import test_file_inject_upload_only_tool_parity as tool_parity

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")

        refused = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": self._file_inject_yaml('        content: "Hand-written guidance."\n')},
        )
        assert refused.status_code == 400, refused.text

        # Step 1: the same YAML with the refused option removed is accepted.
        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            imported = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": self._file_inject_yaml()})
        assert imported.status_code == 200, imported.text
        record = await service.get_current_state(session.id)
        assert record is not None
        assert record.nodes[0]["options"]["files"] == {"invoice": {"format": "text"}}

        # Steps 2 and 3: upload, then attach the upload to that entry in the imported graph.
        state = state_from_record(record)
        uploaded = await tool_parity._stored_blob_async(
            blob_env, created_by="user", content="Check the totals.\n", source_description="uploaded"
        )
        attached = tool_parity._execute(
            "wire_blob_inline_ref",
            {"field_path": "node:inject.options.files.invoice.content", "blob_id": uploaded["blob_ref"]},
            state,
            blob_env,
        )
        assert attached.success is True, [error.message for error in attached.validation.errors]
        [node] = [node for node in attached.updated_state.nodes if node.id == "inject"]
        options = deep_thaw(node.options)
        assert options["files"]["invoice"] == {"format": "text", "content": uploaded}
        assert _prevalidate_plugin_options("transform", "file_inject", options) is None

    @pytest.mark.asyncio
    async def test_import_refuses_literal_file_inject_content(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": self._file_inject_yaml('        content: "Hand-written guidance."\n')},
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == (
            "Imported YAML: file_inject node 'inject' option 'files.invoice.content' is refused: it holds literal "
            "content, not an uploaded file. In the composer, file_inject content must be a file the user uploaded. "
            "Remove 'files.invoice.content' from the YAML and import again, upload the file in this session, then "
            "attach it to that entry."
        )
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_refuses_file_inject_entry_name_no_upload_can_attach_to(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        yaml_text = self._file_inject_yaml().replace("      invoice:\n", '      "v1.2":\n')
        assert '"v1.2":' in yaml_text

        resp = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("Imported YAML: file_inject node 'inject' option 'files.v1.2' is refused: ")
        assert detail.endswith(
            "Rename the entry in the YAML to a name of that form and import again. In mode select the entry name is "
            "the value rows are matched on, so renaming it changes which rows match."
        )
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_refuses_file_inject_default_content(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A, filename="guidance.txt")
        app.state.blob_service = _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={
                "yaml": self._file_inject_yaml(option_lines='    on_miss: default\n    default_content: "Hand-written fallback."\n'),
                "inline_blob_bindings": {"node:inject.options.files.invoice.content": {"blob_id": str(blob.id)}},
            },
        )

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("Imported YAML: file_inject node 'inject' option 'default_content' is refused: ")
        assert "Remove default_content from the YAML and use on_miss: null or on_miss: fail, then import again." in detail
        assert "Hand-written fallback" not in detail
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_import_inline_binding_refuses_llm_authored_blob_for_file_inject_content(self, tmp_path: Path) -> None:
        from dataclasses import replace as dataclass_replace

        from elspeth.contracts.enums import CreationModality

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebind", "local")
        blob = dataclass_replace(
            _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A, filename="guidance.txt"),
            creation_modality=CreationModality.LLM_GENERATED,
        )
        app.state.blob_service = _blob_service_for(blob)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={
                "yaml": self._file_inject_yaml(),
                "inline_blob_bindings": {"node:inject.options.files.invoice.content": {"blob_id": str(blob.id)}},
            },
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"].startswith(
            "wire_blob_inline_ref cannot set file_inject node 'inject' option 'files.invoice.content': "
            f"blob '{blob.id}' is not a file the user uploaded"
        )
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_seed_records_shared_import_meta(self, tmp_path: Path) -> None:
        from starlette.requests import Request as StarletteRequest

        from elspeth.contracts.freeze import deep_thaw
        from elspeth.web.sessions.routes.composer.state import (
            ImportStateYamlRequest,
            SharedImportMetaUpdates,
            seed_state_from_runtime_yaml,
        )

        app, service = _make_app(tmp_path)
        # The fixture's provisioned identity stands in for Chris.
        session = await service.create_session("alice", "Copy of Tom's pipeline", "local")
        blob = _hashed_blob(blob_id=uuid.uuid4(), session_id=session.id, content_hash=_REBIND_DIGEST_A)
        app.state.blob_service = _blob_service_for(blob)
        facts: SharedImportMetaUpdates = {
            "shared_import": {
                "payload_digest": "c3" * 32,
                "shared_by_user_id": "tom",
                "shared_by_username": "Tom",
                "shared_from_session_id": str(uuid.uuid4()),
                "shared_from_state_id": str(uuid.uuid4()),
                "files": [{"sha256": _REBIND_DIGEST_A, "blob_id": str(blob.id)}],
            }
        }

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            response = await seed_state_from_runtime_yaml(
                session=session,
                body=ImportStateYamlRequest(
                    yaml=_REBIND_NODE_YAML,
                    inline_blob_bindings={"node:lookup.options.tables.t.reference_content": {"blob_id": str(blob.id)}},
                ),
                request=StarletteRequest({"type": "http", "app": app}),
                user=UserIdentity(user_id="alice", username="alice"),
                composer_meta_updates=facts,
            )

        record = await service.get_current_state(session.id)
        assert record is not None and record.composer_meta is not None and response.id == str(record.id)
        assert deep_thaw(record.composer_meta["shared_import"]) == facts["shared_import"]
        assert "ingress" in record.composer_meta
        assert record.nodes[0]["options"]["tables"]["t"]["reference_content"]["blob_ref"] == str(blob.id)


_SINK_BINDING_YAML_WITH_PATH = _REBIND_NODE_YAML.replace(
    "    plugin: json\n    on_write_failure: discard\n",
    "    plugin: json\n    on_write_failure: discard\n    options:\n      path: outputs/existing.json\n",
)


class TestImportRebasesSinkPaths:
    """``sink_path_bindings`` re-authors a sink path under the CALLER's own session folders."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("folder", ["outputs", "blobs"])
    async def test_binding_writes_a_path_under_the_callers_session(self, tmp_path: Path, folder: str) -> None:
        from elspeth.contracts.freeze import deep_thaw
        from elspeth.web.composer.tools._common import _validate_sink_path

        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebase", "local")

        with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_rebind_preflight):
            resp = client.post(
                f"/api/sessions/{session.id}/state/yaml",
                json={
                    "yaml": _REBIND_NODE_YAML,
                    "sink_path_bindings": {"main": {"path": {"folder": folder, "relative_path": "nested/./out.jsonl"}}},
                },
            )

        assert resp.status_code == 200, resp.text
        record = await service.get_current_state(session.id)
        assert record is not None
        options = deep_thaw(record.outputs[0]["options"])
        assert options["path"] == str(tmp_path.resolve() / folder / str(session.id) / "nested" / "out.jsonl")
        # The re-based path passes the composer's own sink-path guard unchanged.
        assert _validate_sink_path(options, str(tmp_path), session_id=str(session.id)) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bindings", "detail_fragment"),
        [
            pytest.param(
                {"missing": {"path": {"folder": "outputs", "relative_path": "x.json"}}}, "unknown output 'missing'", id="unknown-output"
            ),
            pytest.param({"main": {"url": {"folder": "outputs", "relative_path": "x.json"}}}, "main.url", id="unknown-option"),
            pytest.param({"main": {"path": {"folder": "outputs", "relative_path": "../x.json"}}}, "main.path", id="dotdot"),
            pytest.param(
                {"main": {"path": {"folder": "outputs", "relative_path": "nested/../../x.json"}}}, "main.path", id="nested-dotdot"
            ),
            pytest.param({"main": {"path": {"folder": "outputs", "relative_path": "/etc/passwd"}}}, "main.path", id="absolute"),
            pytest.param({"main": {"path": {"folder": "outputs", "relative_path": ""}}}, "main.path", id="empty"),
            pytest.param({"main": {"path": {"folder": "outputs", "relative_path": "."}}}, "main.path", id="the-folder-itself"),
            pytest.param({"main": {"path": {"folder": "outputs", "relative_path": "..\\..\\x.json"}}}, "main.path", id="backslash"),
            pytest.param(
                {"main": {"path": {"folder": "outputs", "relative_path": "a\nINFO fake log line.json"}}}, "main.path", id="newline"
            ),
            pytest.param({"main": {"path": {"folder": "outputs", "relative_path": "a\x1b[2Jb.json"}}}, "main.path", id="escape"),
        ],
    )
    async def test_binding_that_is_unknown_or_escapes_is_400(self, tmp_path: Path, bindings: dict[str, Any], detail_fragment: str) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebase", "local")

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _REBIND_NODE_YAML, "sink_path_bindings": bindings},
        )

        assert resp.status_code == 400, resp.text
        assert detail_fragment in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_binding_through_a_symlink_out_of_the_session_is_400(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebase", "local")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        session_outputs = tmp_path / "outputs" / str(session.id)
        session_outputs.mkdir(parents=True)
        (session_outputs / "link").symlink_to(outside, target_is_directory=True)

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={
                "yaml": _REBIND_NODE_YAML,
                "sink_path_bindings": {"main": {"path": {"folder": "outputs", "relative_path": "link/x.json"}}},
            },
        )

        assert resp.status_code == 400, resp.text
        assert "main.path" in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_binding_refuses_an_option_that_already_has_a_value(self, tmp_path: Path) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebase", "local")

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={
                "yaml": _SINK_BINDING_YAML_WITH_PATH,
                "sink_path_bindings": {"main": {"path": {"folder": "outputs", "relative_path": "x.json"}}},
            },
        )

        assert resp.status_code == 400, resp.text
        assert "main.path" in resp.json()["detail"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    async def test_binding_refuses_credential_material_in_the_path(self, tmp_path: Path) -> None:
        """The bound path is persisted after the YAML's own credential guard ran, so it carries its own."""
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebase", "local")
        candidate = "sk-" + "a" * 24

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={
                "yaml": _REBIND_NODE_YAML,
                "sink_path_bindings": {"main": {"path": {"folder": "outputs", "relative_path": f"{candidate}.json"}}},
            },
        )

        assert resp.status_code == 422, resp.text
        # This bare test app keeps FastAPI's default 422 body; the real app's
        # RequestValidationError handler strips the echoed input.
        assert [error["type"] for error in resp.json()["detail"]] == ["credential_material_rejected"]
        assert await service.get_current_state(session.id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "binding",
        [
            pytest.param({"folder": "payloads", "relative_path": "x.json"}, id="unknown-folder"),
            pytest.param({"folder": "outputs", "relative_path": "x.json", "extra": 1}, id="extra-key"),
            pytest.param({"folder": "outputs"}, id="missing-relative-path"),
            pytest.param({"folder": "outputs", "relative_path": "a" * 1025}, id="longer-than-1024"),
        ],
    )
    async def test_binding_request_shape_is_validated(self, tmp_path: Path, binding: dict[str, Any]) -> None:
        app, service = _make_app(tmp_path)
        client = TestClient(app)
        session = await service.create_session("alice", "Rebase", "local")

        resp = client.post(
            f"/api/sessions/{session.id}/state/yaml",
            json={"yaml": _REBIND_NODE_YAML, "sink_path_bindings": {"main": {"path": binding}}},
        )

        assert resp.status_code == 422, resp.text
        assert await service.get_current_state(session.id) is None

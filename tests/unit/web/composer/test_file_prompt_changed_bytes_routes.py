"""What the user gets when a bound file's stored bytes no longer match its blob record.

The condition: the bytes on disk no longer hash to the blob row's
``content_hash`` (``BlobIntegrityError``, blobs/service.py), or the stored file
is gone while the row says ready (``BlobContentMissingError``). That is
storage damage, not an edit (an edit moves row and bytes together and is
refused earlier as ``hash_mismatch``). The tests produce it by writing to, or
unlinking, the blob row's ``storage_path`` directly.

Driven through the full ``create_app`` application, its lifespan and its
exception handlers (the audit-readiness app builder of
``tests/integration/web/test_validate_route_repair_pass.py`` with ``transform:llm``
and ``transform:file_inject`` admitted and an operator LLM profile). Server
exceptions are NOT re-raised into the test: the client returns what a browser
would receive. Before the fix both routes answered a bare 500.

No compose turn is driven. State is saved under a COMPOSE lease, the /validate
repair pass opens the pending review cards, and cards are approved through the
resolve route the review card posts to. Files are uploaded through the blobs
route. No provider call can happen: refused states create no run, and the
intact approved state is only validated, never run.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.config import WebSettings
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.protocol import CompositionStateData
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.helpers.prompt_review import accept_body
from tests.integration.web.conftest import (
    _TEST_AUTHED_USER_ID,
    _lifespan_test_client,
    _save_composition_state_with_compose_authority,
)
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

PROMPT_TEXT = "Label {{ row.text }} as good or bad."


def _build_app(tmp_path: Path) -> FastAPI:
    from elspeth.web.app import create_app

    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough", "transform:llm", "transform:file_inject"),
        composer_boot_probe_enabled=False,
        # An operator LLM profile, as a deployment configures one; no secret, no provider call (nothing runs).
        llm_profiles={"task-role": {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0", "temperature": 0.0}},
        default_llm_profile="task-role",
    )
    app = create_app(settings=settings)
    wire_test_pipeline_user_authority(
        app,
        identity_id=_TEST_AUTHED_USER_ID,
        provider=settings.auth_provider,
        engine=app.state.session_engine,
    )
    identity = UserIdentity(user_id=_TEST_AUTHED_USER_ID, username=_TEST_AUTHED_USER_ID)

    async def _mock_user() -> UserIdentity:
        return identity

    app.dependency_overrides[get_current_user] = _mock_user
    return app


@pytest.fixture
def clients(tmp_path: Path) -> Iterator[tuple[SyncASGITestClient, SyncASGITestClient]]:
    """(browser view, raising view): the same app and transport; the first returns a server exception as the browser gets it."""
    with (
        _lifespan_test_client(_build_app(tmp_path)) as raising,
        SyncASGITestClient(raising.app, raise_server_exceptions=False, transport_app=raising._app) as browser,
    ):
        yield browser, raising


def _state(data_dir: Path, session_id: UUID, marker: dict[str, str]) -> CompositionState:
    options: dict[str, Any] = {
        "profile": "task-role",
        "prompt_template": marker,
        "required_input_fields": ["text"],
        "response_field": "label",
        "schema": {"mode": "observed"},
    }
    options = _options_with_default_prompt_template_review(node_id="classify", plugin="llm", options=options)
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={
                "path": str(data_dir / "blobs" / str(session_id) / "input.csv"),
                "schema": {"mode": "flexible", "fields": ["text: str"], "guaranteed_fields": ["text"]},
            },
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="classify",
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="classified",
                on_error="discard",
                options=options,
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
                name="classified",
                plugin="json",
                options={
                    "path": str(data_dir / "outputs" / str(session_id) / "output.jsonl"),
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _seed(client: SyncASGITestClient) -> tuple[UUID, dict[str, Any]]:
    """A session whose llm node reads its prompt from an uploaded file; cards opened, none approved."""
    session_service = client.app.state.session_service
    settings = client.app.state.settings
    record = asyncio.run(
        session_service.create_session(
            user_id=_TEST_AUTHED_USER_ID, title="changed prompt bytes", auth_provider_type=settings.auth_provider
        )
    )
    session_id = record.id
    (settings.data_dir / "blobs" / str(session_id)).mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "outputs" / str(session_id)).mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "blobs" / str(session_id) / "input.csv").write_text("text\nhello\n")
    uploaded = client.post(
        f"/api/sessions/{session_id}/blobs",
        files={"file": ("prompt.txt", io.BytesIO(PROMPT_TEXT.encode()), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    blob = uploaded.json()
    marker = {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}
    document = _state(settings.data_dir, session_id, marker).to_dict()
    asyncio.run(
        _save_composition_state_with_compose_authority(
            session_service,
            session_id,
            CompositionStateData(
                sources=document["sources"],
                nodes=document["nodes"],
                edges=document["edges"],
                outputs=document["outputs"],
                metadata_=document["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
    )
    # The repair pass opens a card for every pending requirement with no evidence.
    opened = client.post(f"/api/sessions/{session_id}/validate")
    assert opened.status_code == 200, opened.text
    return session_id, blob


def _approve(client: SyncASGITestClient, session_id: UUID, kinds: set[InterpretationKind]) -> None:
    session_service = client.app.state.session_service
    events = asyncio.run(session_service.list_interpretation_events(session_id, status="pending"))
    assert {event.kind for event in events} == kinds, [event.kind for event in events]
    for event in events:
        if event.kind not in kinds:
            continue
        resolved = client.post(
            f"/api/sessions/{session_id}/interpretations/{event.id}/resolve", json=accept_body(client, session_id, event)
        )
        assert resolved.status_code == 200, resolved.text


def _observe(response: Any) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return {"status": response.status_code, "body": body}


def _storage_path(client: SyncASGITestClient, blob: dict[str, Any]) -> Path:
    with client.app.state.session_engine.connect() as conn:
        return Path(conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob["id"])).scalar_one())


def _tamper(client: SyncASGITestClient, blob: dict[str, Any]) -> None:
    """Overwrite the stored bytes in place; the blob row (and its recorded sha256) is untouched."""
    _storage_path(client, blob).write_bytes(b"Label {{ row.text }} as anything you like.")


def _remove_bytes(client: SyncASGITestClient, blob: dict[str, Any]) -> None:
    """Delete the stored file; the blob row still says ready."""
    _storage_path(client, blob).unlink()


def _runs(client: SyncASGITestClient, session_id: UUID) -> list[Any]:
    return list(asyncio.run(client.app.state.session_service.list_runs_for_session(session_id)))


def _validate_and_execute(client: SyncASGITestClient, session_id: UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    validated = _observe(client.post(f"/api/sessions/{session_id}/validate"))
    executed = _observe(client.post(f"/api/sessions/{session_id}/execute"))
    print(f"validate={validated}")
    print(f"execute={executed}")
    return validated, executed


def _approved_intact(client: SyncASGITestClient) -> tuple[UUID, dict[str, Any]]:
    session_id, blob = _seed(client)
    _approve(client, session_id, {InterpretationKind.LLM_PROMPT_TEMPLATE})
    approved = _observe(client.post(f"/api/sessions/{session_id}/validate"))
    assert approved["status"] == 200, approved
    assert approved["body"]["is_valid"] is True and approved["body"]["readiness"]["execution_ready"] is True, approved
    return session_id, blob


_DETAIL = {
    "bytes_overwritten": ("not_ready_inline_blob_content", "not_ready: stored file content does not match its record"),
    "bytes_removed": ("missing_inline_blob_content", "missing: stored file content is missing"),
}


def _assert_storage_refusal(validated: dict[str, Any], executed: dict[str, Any], *, field_path: str, change: str) -> None:
    """/validate: 200 invalid with the violation naming the field; /execute: 422 with the same violation; no "review"."""
    error_code, detail = _DETAIL[change]
    expected = f"Inline content blob reference at {field_path} is {detail}"
    assert validated["status"] == 200, validated
    body = validated["body"]
    assert body["is_valid"] is False and body["readiness"]["execution_ready"] is False, body
    assert [(error["error_code"], error["message"]) for error in body["errors"]] == [(error_code, expected)], body["errors"]
    assert [blocker["code"] for blocker in body["readiness"]["blockers"]] == ["blob_inline_refs"], body["readiness"]
    assert executed["status"] == 422, executed
    detail_body = executed["body"]["detail"]
    assert detail_body["error_type"] == "pipeline_validation_failure", detail_body
    assert [(error["error_code"], error["message"]) for error in detail_body["errors"]] == [(error_code, expected)], detail_body
    # Storage damage is not something a review can repair: nothing tells the user to review.
    assert "review" not in str(body["errors"]).lower() and "review" not in str(detail_body).lower()


@pytest.mark.parametrize("change", ["bytes_overwritten", "bytes_removed"])
def test_approved_prompt_file_whose_stored_bytes_no_longer_match_is_refused_cleanly(
    clients: tuple[SyncASGITestClient, SyncASGITestClient], change: str
) -> None:
    client, _raising = clients
    session_id, blob = _approved_intact(client)
    (_tamper if change == "bytes_overwritten" else _remove_bytes)(client, blob)

    validated, executed = _validate_and_execute(client, session_id)

    _assert_storage_refusal(validated, executed, field_path="node:classify.options.prompt_template", change=change)
    assert _runs(client, session_id) == []


def test_pending_card_control_validate_and_execute_routes(clients: tuple[SyncASGITestClient, SyncASGITestClient]) -> None:
    client, _raising = clients
    session_id, _blob = _seed(client)
    # The profile supplies the model (operator-resolved), so the prompt card is the only card; leave it open.
    validated, executed = _validate_and_execute(client, session_id)
    assert validated["status"] == 200
    assert validated["body"]["is_valid"] is False
    assert [error["error_code"] for error in validated["body"]["errors"]] == ["interpretation_review_pending"]
    assert executed["status"] == 422
    assert executed["body"]["detail"]["error_type"] == "interpretation_placeholder_unresolved"
    assert _runs(client, session_id) == []


def _seed_file_inject(client: SyncASGITestClient) -> tuple[UUID, dict[str, Any]]:
    """A non-prompt inline file: a file_inject node whose entry content is an uploaded file."""
    session_service = client.app.state.session_service
    settings = client.app.state.settings
    record = asyncio.run(
        session_service.create_session(
            user_id=_TEST_AUTHED_USER_ID, title="changed inject bytes", auth_provider_type=settings.auth_provider
        )
    )
    session_id = record.id
    blobs_dir = settings.data_dir / "blobs" / str(session_id)
    outputs_dir = settings.data_dir / "outputs" / str(session_id)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (blobs_dir / "input.csv").write_text("kind\ninvoice\n")
    uploaded = client.post(
        f"/api/sessions/{session_id}/blobs",
        files={"file": ("invoice.txt", io.BytesIO(b"Invoices go to accounts."), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    blob = uploaded.json()
    marker = {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="inject_input",
            options={"path": str(blobs_dir / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="inject",
                node_type="transform",
                plugin="file_inject",
                input="inject_input",
                on_success="results",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "mode": "select",
                    "key_field": "kind",
                    "output_field": "guidance",
                    "files": {"invoice": {"content": marker, "format": "text"}},
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
                name="results",
                plugin="json",
                options={
                    "path": str(outputs_dir / "output.jsonl"),
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    ).to_dict()
    asyncio.run(
        _save_composition_state_with_compose_authority(
            session_service,
            session_id,
            CompositionStateData(
                sources=state["sources"],
                nodes=state["nodes"],
                edges=state["edges"],
                outputs=state["outputs"],
                metadata_=state["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )
    )
    return session_id, blob


@pytest.mark.parametrize("change", ["bytes_overwritten", "bytes_removed"])
def test_non_prompt_inline_file_whose_stored_bytes_no_longer_match_is_refused_cleanly(
    clients: tuple[SyncASGITestClient, SyncASGITestClient], change: str
) -> None:
    client, _raising = clients
    session_id, blob = _seed_file_inject(client)
    # No /validate before the change: for a state with no prompt file, validate_state answers from its advisory
    # 10-second memo, which re-checks blob ROWS only, so an intact verdict would be served again.
    (_tamper if change == "bytes_overwritten" else _remove_bytes)(client, blob)

    validated, executed = _validate_and_execute(client, session_id)

    _assert_storage_refusal(validated, executed, field_path="node:inject.options.files.invoice.content", change=change)
    assert _runs(client, session_id) == []


async def _compose_lease(client: SyncASGITestClient, session_id: UUID) -> SessionOperationLease:
    service = client.app.state.session_service
    return await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )


def _current_state(client: SyncASGITestClient, session_id: UUID) -> CompositionState:
    record = asyncio.run(client.app.state.session_service.get_current_state(session_id))
    assert record is not None
    return state_from_record(record)


@pytest.mark.parametrize("change", ["bytes_overwritten", "bytes_removed"])
def test_composer_turn_runtime_preflight_reports_the_damaged_file_as_a_violation(
    clients: tuple[SyncASGITestClient, SyncASGITestClient], change: str
) -> None:
    """The chat turn's runtime preflight (ComposerPreflight.runtime_preflight, the read every turn takes) no longer raises."""
    client, _raising = clients
    session_id, blob = _approved_intact(client)
    (_tamper if change == "bytes_overwritten" else _remove_bytes)(client, blob)
    state = _current_state(client, session_id)
    preflight = client.app.state.composer_service._preflight

    async def _run() -> Any:
        lease = await _compose_lease(client, session_id)
        try:
            return preflight.runtime_preflight(state, _TEST_AUTHED_USER_ID, str(session_id), session_operation_context=lease.context)
        finally:
            await lease.close()

    result = asyncio.run(_run())

    error_code, detail = _DETAIL[change]
    print(f"composer runtime preflight ({change}): is_valid={result.is_valid} errors={[(e.error_code, e.message) for e in result.errors]}")
    assert result.is_valid is False
    assert [(error.error_code, error.message) for error in result.errors] == [
        (error_code, f"Inline content blob reference at node:classify.options.prompt_template is {detail}")
    ]


def test_recovery_after_damage_bind_a_fresh_upload_approve_it_and_validate(
    clients: tuple[SyncASGITestClient, SyncASGITestClient],
) -> None:
    """What the user can do afterwards: upload the same text as a NEW file, rebind, approve the new card, run is admitted."""
    client, _raising = clients
    session_id, blob = _approved_intact(client)
    _tamper(client, blob)
    damaged, _executed = _validate_and_execute(client, session_id)
    assert damaged["status"] == 200 and damaged["body"]["is_valid"] is False, damaged

    # 1. A new upload of the same prompt text gets a new id.
    uploaded = client.post(
        f"/api/sessions/{session_id}/blobs",
        files={"file": ("prompt-again.txt", io.BytesIO(PROMPT_TEXT.encode()), "text/plain")},
    )
    print(f"recovery 1 upload: status={uploaded.status_code}")
    assert uploaded.status_code == 201, uploaded.text
    fresh = uploaded.json()
    assert fresh["id"] != blob["id"]

    # 2. The binding moves to the new file with the composer's own tool, saved as the compose loop saves it.
    settings = client.app.state.settings
    service = client.app.state.session_service

    before_rebind = _current_state(client, session_id)

    async def _rebind() -> Any:
        lease = await _compose_lease(client, session_id)
        try:
            result = blob_tools.execute_tool(
                "wire_blob_inline_ref",
                {"field_path": "node:classify.options.prompt_template", "blob_id": fresh["id"]},
                before_rebind,
                blob_tools._catalog(),
                data_dir=str(settings.data_dir),
                session_engine=client.app.state.session_engine,
                session_id=str(session_id),
                session_operation_context=lease.context,
                session_operation_authority=service.session_operation_authority,
            )
        finally:
            await lease.close()
        return result

    rebound = asyncio.run(_rebind())
    print(
        f"recovery 2 rebind: success={rebound.success} errors={[e.to_dict() for e in rebound.validation.errors] if not rebound.success else []}"
    )
    assert rebound.success, rebound.to_dict()
    assert rebound.updated_state.nodes[0].options["prompt_template"]["blob_ref"] == fresh["id"]
    document = rebound.updated_state.to_dict()
    asyncio.run(
        _save_composition_state_with_compose_authority(
            service,
            session_id,
            CompositionStateData(
                sources=document["sources"],
                nodes=document["nodes"],
                edges=document["edges"],
                outputs=document["outputs"],
                metadata_=document["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="tool_call",
        )
    )

    # 3. /validate opens the new file's card; it is refused until approved.
    pending = _observe(client.post(f"/api/sessions/{session_id}/validate"))
    print(f"recovery 3 validate before approval: status={pending['status']} errors={[e['error_code'] for e in pending['body']['errors']]}")
    assert pending["status"] == 200 and pending["body"]["is_valid"] is False
    assert [error["error_code"] for error in pending["body"]["errors"]] == ["interpretation_review_pending"]

    # 4. The new card is approved through the resolve route.
    _approve(client, session_id, {InterpretationKind.LLM_PROMPT_TEMPLATE})

    # 5. Valid and ready to run.
    recovered = _observe(client.post(f"/api/sessions/{session_id}/validate"))
    print(f"recovery 5 validate after approval: status={recovered['status']} is_valid={recovered['body']['is_valid']}")
    assert recovered["status"] == 200, recovered
    assert recovered["body"]["is_valid"] is True and recovered["body"]["readiness"]["execution_ready"] is True, recovered
    assert recovered["body"]["errors"] == []

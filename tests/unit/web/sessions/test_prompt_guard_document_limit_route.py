"""PromptShield seam 7: one indivisible document unit at the Azure limit, through the real route.

Turn 1 sends a prompt sized so that, as turn-2 history, it becomes exactly one
guard document unit of ``PROMPT_GUARD_MAX_TEXT_CHARACTERS`` and then one more.
Turn 2 goes through the actual async ``/messages`` route with Azure limits
enforced (``required`` mode, mounted Azure adapter; only its HTTP transport is
replaced). Only ``litellm.acompletion`` stands in for the model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import litellm
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer import prompt_guard, prompt_guard_clients
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import quota_provider_attempts_table
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.unit.web.sessions.test_prompt_guard_capacity_errors import _LOCAL_DETAIL, _settings
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _TestSnapshotFactory

_MARKER = "SEAM7-LIMIT-UNIT:"
_SECOND_PROMPT = "Review the earlier request without changing anything."


def _history_unit(content: str) -> str:
    # The guard's rendering of a prior user message as an evidence document.
    return json.dumps({"content": content, "role": "user"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _first_prompt(unit_length: int) -> str:
    overhead = len(_history_unit(_MARKER))
    content = _MARKER + "u" * (unit_length - overhead)
    assert len(_history_unit(content)) == unit_length
    return content


def _assert_documents_reproduce_complete_units(units: Sequence[str], documents: Sequence[str]) -> None:
    """Every document is the newline join of a contiguous run of COMPLETE units, in order."""
    remaining = list(units)
    for position, document in enumerate(documents):
        width = next((size for size in range(1, len(remaining) + 1) if "\n".join(remaining[:size]) == document), None)
        assert width is not None, f"document {position} ({len(document)} chars) does not reproduce complete units"
        del remaining[:width]
    assert remaining == [], f"{len(remaining)} units were not reproduced"


def _handoff(kwargs: dict[str, Any]) -> litellm.ModelResponse:
    return litellm.ModelResponse(
        model=kwargs["model"],
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "seam7-handoff",
                            "type": "function",
                            "function": {
                                "name": "handoff_to_user",
                                "arguments": json.dumps({"action": "reset_to_safe_checkpoint", "message": "Noted."}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    )


def _guard_checks(rows) -> list[dict[str, Any]]:
    return [
        deep_thaw(envelope["check"]) for row in rows for envelope in (row.tool_calls or ()) if envelope["_kind"] == "prompt_guard_audit"
    ]


@pytest.mark.asyncio
async def test_indivisible_history_unit_at_limit_is_inspected_whole_and_one_more_is_refused(tmp_path, monkeypatch):
    app, service = _make_app(tmp_path, quota_enabled=True)
    pack_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    guard_bodies: list[dict[str, Any]] = []
    sdk_calls: list[dict[str, Any]] = []
    checks_at_sdk_entry: list[list[dict[str, Any]]] = []
    current_session: list[Any] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "shield.example.test"
        body = json.loads(request.content)
        guard_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in body["documents"]],
            },
        )

    def client_factory(settings: PromptGuardSettings) -> AzurePromptGuardClient:
        return AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(transport))

    original_pack = prompt_guard._pack_documents

    def observe_pack(units: Sequence[str]) -> tuple[str, ...]:
        packed: tuple[str, ...] = original_pack(units)
        pack_calls.append((tuple(units), packed))
        return packed

    async def completion(**kwargs):
        checks_at_sdk_entry.append(_guard_checks(await service.get_messages(current_session[0], limit=None)))
        sdk_calls.append(kwargs)
        return _handoff(kwargs)

    # Only the guard's HTTP transport and the model SDK are replaced; the
    # packer is observed through its original implementation.
    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", client_factory)
    monkeypatch.setattr(prompt_guard, "_pack_documents", observe_pack)
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    guard_settings = _settings()
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4.1-mini",
            "composer_advisor_model": "openai/gpt-4.1-mini",
            "composer_endpoint_base_url": "https://model.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-model-key"),  # secret-scan: allow-this-line
            "composer_advisor_endpoint_base_url": "https://model.example.test/v1",
            "composer_advisor_endpoint_api_key": SecretStr("offline-model-key"),  # secret-scan: allow-this-line
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": guard_settings,
            "composer_advisor_prompt_guard": guard_settings,
            "composer_boot_probe_enabled": False,
        }
    )
    app.state.composer_service = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(), app.state.settings, sessions_service=service, session_engine=app.state.session_engine
    )
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)

    async def second_turn_after_history(client, unit_length):
        session = await service.create_session("alice", f"Seam 7 unit of {unit_length}", "local")
        current_session[:] = [session.id]
        path = f"/api/sessions/{session.id}/messages"
        first = _first_prompt(unit_length)
        assert len(first) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS
        first_settled = await submit_and_settle(client, app, path=path, body=message_body(first))
        assert first_settled.result()["message"]["content"] == "Noted."
        first_state = await service.get_current_state(session.id)
        assert first_state is not None
        before = await service.get_messages(session.id, limit=None)
        pack_calls.clear()
        guard_bodies.clear()
        sdk_calls.clear()
        checks_at_sdk_entry.clear()
        body = message_body(_SECOND_PROMPT, state_id=first_state.id)
        settled = await submit_and_settle(client, app, path=path, body=body)
        assert len(pack_calls) == 1
        units, packed = pack_calls[0]
        marked = [unit for unit in units if _MARKER in unit]
        assert marked == [_history_unit(first)]
        assert len(marked[0]) == unit_length
        return session, body, first, first_state, before, settled, units, packed, marked[0]

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Exactly at the limit: the unit is inspected whole and the SDK is entered once.
            session, _body, _first, _state, before, settled, units, packed, unit = await second_turn_after_history(
                client, PROMPT_GUARD_MAX_TEXT_CHARACTERS
            )
            turn_one_checks = _guard_checks(before)
            _assert_documents_reproduce_complete_units(units, packed)
            assert unit in packed
            inspected = [document for body in guard_bodies for document in body["documents"]]
            _assert_documents_reproduce_complete_units(units, inspected)
            assert tuple(inspected) == packed
            assert all(body["userPrompt"] == _SECOND_PROMPT for body in guard_bodies)
            assert settled.result()["message"]["content"] == "Noted."
            assert len(sdk_calls) == 1
            # Turn 2's own allow decision (one row beyond turn 1's) was durable before the SDK ran.
            assert checks_at_sdk_entry[0][: len(turn_one_checks)] == turn_one_checks
            assert len(checks_at_sdk_entry[0]) == len(turn_one_checks) + 1
            assert (checks_at_sdk_entry[0][-1]["disposition"], checks_at_sdk_entry[0][-1]["reason"]) == ("allow", "clean")

            # One more character: the same indivisible unit is refused, never sliced.
            session, body, first, first_state, before, settled, units, packed, unit = await second_turn_after_history(
                client, PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1
            )
            # Byte-exact: the packed request still carries the whole unit, so the
            # size policy sees it and nothing is sent to Azure at all.
            _assert_documents_reproduce_complete_units(units, packed)
            assert unit in packed
            assert guard_bodies == []
            assert sdk_calls == []
            status, public = settled.error()
            assert status == 422, public
            assert public["detail"]["error_type"] == "prompt_guard_unsupported_input"
            assert public["detail"]["detail"] == _LOCAL_DETAIL

        record = await run_sync_in_worker(
            app.state.composer_async_operation_authority.get, session_id=session.id, operation_id=body["operation_id"]
        )
        assert record is not None and record.status == "failed"
        rows = await service.get_messages(session.id, limit=None)
        checks = _guard_checks(rows)
        refused = [check for check in checks if check["operation_id"] == record.session_operation_id]
        assert len(refused) == 1
        assert (refused[0]["disposition"], refused[0]["reason"]) == ("unavailable", "unsupported_input")
        assert refused[0]["operation_epoch"] == record.session_operation_epoch
        assert refused[0]["audit_id"] == public["detail"]["audit_id"]
        # Preceding effects are retained: turn 1's messages and state, and turn 2's user message.
        assert [row.id for row in rows][: len(before)] == [row.id for row in before]
        assert [row.content for row in rows if row.role == "user"] == [first, _SECOND_PROMPT]
        current = await service.get_current_state(session.id)
        assert current is not None and current.id == first_state.id
        with service._engine.connect() as connection:
            attempts = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session.id))
            ).all()
        assert len(attempts) == 1 and attempts[0].operation_id != record.session_operation_id
        assert first not in json.dumps(public) + json.dumps(checks)
    finally:
        service._engine.dispose()

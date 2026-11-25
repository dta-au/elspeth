"""A credential refusal at tool dispatch, end to end: real composer, route, settlement and session store.

The model's batch is two calls: the first a real mutation, the second carrying
credential material. The dispatcher's scan refuses the second. The route must
answer with the credential refusal, the first call's state and every audit row
of the turn must be durable, each stored call must say what happened to it, and
the refused value must be in no table of the session store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import litellm
import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy import Engine, MetaData, select

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import chat_messages_table
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _seed_output_only_draft

_CANARY = "sk-" + "r" * 24
_KEPT_NAME = "Kept before the refusal"


def _rows_containing(engine: Engine, needle: str) -> list[str]:
    metadata = MetaData()
    metadata.reflect(bind=engine)
    hits: list[str] = []
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            for row in conn.execute(select(table)).mappings():
                hits.extend(f"{table.name}.{column}" for column, value in row.items() if value is not None and needle in str(value))
    return hits


def _stored_envelopes(engine: Engine, session_id: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.tool_calls).where(
                chat_messages_table.c.session_id == session_id, chat_messages_table.c.tool_calls.is_not(None)
            )
        ).all()
    return [entry for (tool_calls,) in rows for entry in (json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls) or ()]


def _tool_row_contents(engine: Engine, session_id: str, tool_call_id: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.content).where(
                chat_messages_table.c.session_id == session_id,
                chat_messages_table.c.role == "tool",
                chat_messages_table.c.tool_call_id == tool_call_id,
            )
        ).all()
    return [json.loads(content) for (content,) in rows]


@pytest.mark.asyncio
async def test_a_dispatch_refusal_through_the_route_keeps_the_turn_and_stores_no_refused_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service = _make_app(tmp_path)
    engine = app.state.session_engine

    def local_available(**kwargs: object) -> ComposerAvailability:
        model = kwargs["model"]
        assert type(model) is str
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", local_available)
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": PromptGuardSettings(
                endpoint="https://primary-shield.example.test", subscription_key="offline-primary"
            ),
            "composer_advisor_prompt_guard": PromptGuardSettings(
                endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor"
            ),
        }
    )

    def shield_response(request: Any) -> Response:
        payload = json.loads(request.content)
        return Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def mount(settings: PromptGuardSettings) -> PromptGuardMount:
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
    # The gateway and the tool batch refuse a recognised credential in a model
    # response before dispatch. Removing those first lines reaches the
    # dispatcher's own scan with real arguments: the case it exists for.
    monkeypatch.setattr("elspeth.web.composer.provider_gateway.require_no_credential_material_in_tool_wire", lambda *_a, **_k: None)
    monkeypatch.setattr("elspeth.web.composer.tool_batch.require_no_credential_material_in_tool_wire", lambda *_a, **_k: None)
    app.state.composer_service = ComposerServiceImpl(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=service,
        session_engine=engine,
        plugin_snapshot_factory=app.state.plugin_snapshot_factory.for_user_id,
        operator_profile_registry=app.state.operator_profile_registry,
    )
    app.state.interpretation_surfacing = app.state.composer_service._interpretation_surfacing
    primary_calls = 0

    async def local_completion(**kwargs: Any) -> litellm.ModelResponse:
        nonlocal primary_calls
        model = kwargs["model"]
        tool_calls: list[dict[str, Any]] = []
        content: str | None
        if model == app.state.settings.composer_model:
            primary_calls += 1
            assert primary_calls == 1, "the turn must end at the refusal"
            sent = {tool["function"]["name"]: tool["function"] for tool in kwargs["tools"]}
            for call_id, name in (("kept", _KEPT_NAME), ("refused", f"Refused {_CANARY}")):
                dialect = ToolContractDialect.OPENAI_STRICT if "strict" in sent["set_metadata"] else ToolContractDialect.NONE
                arguments = encode_semantic_arguments("set_metadata", dialect, {"patch": {"name": name}})
                tool_calls.append(
                    {"id": call_id, "type": "function", "function": {"name": "set_metadata", "arguments": json.dumps(arguments)}}
                )
            content = None
        else:
            payload: dict[str, Any] = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "CLEAN", "note": None}
            if kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response":
                payload["publication_verdict"] = "ADMIT"
            content = json.dumps(payload)
        return litellm.ModelResponse(
            id=f"local-{model}",
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls or None},
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(litellm, "acompletion", local_completion)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/sessions", json={"title": "Dispatch refusal"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        await _seed_output_only_draft(app, service, UUID(session_id))
        current = await service.get_current_state(UUID(session_id))
        assert current is not None
        settled = await submit_and_settle(
            client,
            app,
            path=f"/api/sessions/{session_id}/messages",
            body=message_body("Rename the pipeline.", state_id=current.id, operation_id=str(uuid4())),
        )

    status, body = settled.error()
    assert status == 422
    assert body["detail"]["failure_code"] == "credential_material_rejected"
    assert body["detail"]["surface"] == "composer_tool_arguments"
    assert primary_calls == 1
    # Call 1 ran and its state is durable (also the sweep's positive control).
    assert "composition_states.metadata_" in _rows_containing(engine, _KEPT_NAME)
    # The refused value is in no table and no column.
    assert _rows_containing(engine, _CANARY) == []
    envelopes = _stored_envelopes(engine, session_id)
    # Each stored call says what happened to it.
    assert [row["success"] for row in _tool_row_contents(engine, session_id, "kept")] == [True]
    assert [(row["_redaction_status"], row["error_category"]) for row in _tool_row_contents(engine, session_id, "refused")] == [
        ("arg_error", "credential_material")
    ]
    # The turn's primary LLM call is durable.
    assert [entry["call"]["model_requested"] for entry in envelopes if "_kind" in entry and entry["_kind"] == "llm_call_audit"].count(
        app.state.settings.composer_model
    ) == 1

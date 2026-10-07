"""Per-transition provider custody through real Composer, quota and durable worker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import litellm
import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.provider_gateway import (
    ProviderGateway,
    _admit_composer_llm_completion,
)
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    composer_async_operations_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from tests.helpers.composer_operations import (
    message_body,
    recompose_body,
    submit_and_settle,
)
from tests.unit.web.sessions.test_routes import _make_app


@dataclass(frozen=True, slots=True)
class _Dispatch:
    operation_id: str
    attempt_id: str
    fence_id: str
    epoch: int
    model: str


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ("initial", "followup", "rootless", "recompose", "authoring"))
async def test_each_real_composer_transition_has_its_own_settled_provider_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    app, service = _make_app(tmp_path, quota_enabled=True)
    engine = app.state.session_engine

    def local_available(**kwargs: object) -> ComposerAvailability:
        model = kwargs["model"]
        assert type(model) is str
        return ComposerAvailability(available=True, model=model, provider="test")

    # This local SDK double needs no real provider credentials. Availability
    # is an explicit test dependency, rather than a fabricated environment key.
    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", local_available)
    app.state.composer_service = ComposerServiceImpl(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=service,
        session_engine=engine,
        plugin_snapshot_factory=app.state.plugin_snapshot_factory.for_user_id,
        operator_profile_registry=app.state.operator_profile_registry,
    )
    # This is the actual surfacing dependency constructed by the real Composer.
    app.state.interpretation_surfacing = app.state.composer_service._interpretation_surfacing
    dispatches: list[_Dispatch] = []
    primary_replies = ["Hello. I can explain pipelines."]
    if transition == "rootless":
        primary_replies = ["The pipeline is ready.", "The pipeline is ready."]
    if transition == "followup":
        primary_replies.append("Hello again. I can explain pipelines.")
    if transition == "authoring":
        primary_replies = ["", "", "", "Pipeline configured."]

    async def local_completion(**kwargs: object) -> litellm.ModelResponse:
        model = kwargs["model"]
        assert type(model) is str
        assert model in (
            app.state.settings.composer_model,
            app.state.settings.composer_advisor_model,
        )
        with engine.connect() as conn:
            pending = (
                conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.settled_at.is_(None)))
                .mappings()
                .one()
            )
            job = (
                conn.execute(select(composer_async_operations_table).where(composer_async_operations_table.c.status == "running"))
                .mappings()
                .one()
            )
        assert pending["identity_id"] == job["actor_user_id"] == "alice"
        assert pending["session_id"] == job["session_id"]
        assert pending["operation_id"] == job["session_operation_id"]
        assert pending["operation_epoch"] == job["session_operation_epoch"]
        assert pending["lease_token"] == job["session_operation_lease_token"]
        dispatches.append(
            _Dispatch(
                job["operation_id"],
                pending["attempt_id"],
                pending["operation_id"],
                pending["operation_epoch"],
                model,
            )
        )
        tool_calls = None
        if model == app.state.settings.composer_model:
            assert primary_replies, "Unexpected extra primary provider dispatch"
            content = primary_replies.pop(0)
            primary_ordinal = sum(call.model == model for call in dispatches)
            if transition == "authoring" and primary_ordinal < 4:
                arguments = {}
                tool_name = "preview_pipeline"
                if primary_ordinal == 2:
                    tool_name = "set_pipeline"
                    arguments = {
                        "source": {
                            "plugin": "text",
                            "on_success": "main",
                            "options": {
                                "column": "text",
                                "schema": {"mode": "observed"},
                            },
                            "inline_blob": {
                                "filename": "input.txt",
                                "mime_type": "text/plain",
                                "content": "hello",
                            },
                            "on_validation_failure": "discard",
                        },
                        "nodes": [],
                        "edges": [],
                        "outputs": [
                            {
                                "sink_name": "main",
                                "plugin": "csv",
                                "options": {
                                    "path": str(tmp_path / "outputs" / job["session_id"] / "authoring.csv"),
                                    "schema": {"mode": "observed"},
                                    "mode": "write",
                                    "collision_policy": "auto_increment",
                                },
                                "on_write_failure": "discard",
                            }
                        ],
                        "metadata": {"name": "Local provider-authored pipeline"},
                    }
                semantic_calls = [(tool_name, arguments)]
                if primary_ordinal == 1:
                    semantic_calls = [
                        ("get_plugin_schema", {"plugin_type": "source", "name": "text"}),
                        ("get_plugin_schema", {"plugin_type": "sink", "name": "csv"}),
                    ]
                sent_tools = kwargs["tools"]
                assert type(sent_tools) is list
                tool_calls = []
                for index, (tool_name, arguments) in enumerate(semantic_calls):
                    sent = next(tool["function"] for tool in sent_tools if tool["function"]["name"] == tool_name)
                    dialect = ToolContractDialect.OPENAI_STRICT if "strict" in sent else ToolContractDialect.NONE
                    wire_arguments = encode_semantic_arguments(tool_name, dialect, arguments)
                    Draft202012Validator(sent["parameters"]).validate(wire_arguments)
                    tool_calls.append(
                        {
                            "id": f"local-tool-{primary_ordinal}-{index}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(wire_arguments),
                            },
                        }
                    )
        else:
            content = json.dumps(
                {
                    "verdict": "CLEAN",
                    "category": "other",
                    "steps": [],
                    "findings": "CLEAN",
                    "note": None,
                }
            )
        return litellm.ModelResponse(
            id=f"local-{len(dispatches)}",
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None if tool_calls else content,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    # Leave ProviderGateway, physical admission, audited advisor and settlement real.
    # Replace the sole outbound SDK seam before running any transition.
    monkeypatch.setattr(litellm, "acompletion", local_completion)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/sessions", json={"title": "Per transition evidence"})
            assert created.status_code == 201, created.text
            session_id = created.json()["id"]
            operation_ids: list[str] = []
            rounds = 2 if transition == "followup" else 1
            for _ in range(rounds):
                operation_id = str(uuid4())
                operation_ids.append(operation_id)
                if transition == "recompose":
                    user = await service.add_message(
                        UUID(session_id),
                        "user",
                        "Hello",
                        writer_principal="route_user_message",
                    )
                    path = f"/api/sessions/{session_id}/recompose"
                    body = recompose_body(user.id, operation_id=operation_id)
                else:
                    path = f"/api/sessions/{session_id}/messages"
                    content = "Build a pipeline from customer complaints." if transition == "rootless" else "Hello"
                    if transition == "authoring":
                        content = "Create a pipeline reading hello from an inline text source and writing the text to CSV."
                    current = await service.get_current_state(UUID(session_id))
                    body = message_body(
                        content,
                        state_id=None if current is None else current.id,
                        operation_id=operation_id,
                    )
                before = len(dispatches)
                settled = await submit_and_settle(client, app, path=path, body=body)
                assert settled.final.json()["status"] == "completed", settled.final.text
                this_transition = dispatches[before:]
                assert this_transition, "transition bypassed the primary provider"
                assert {call.operation_id for call in this_transition} == {operation_id}
                assert sum(call.model == app.state.settings.composer_model for call in this_transition) == (
                    4 if transition == "authoring" else 2 if transition == "rootless" else 1
                ), "transition bypassed the primary provider"
                replay = await client.post(path, json=body)
                assert replay.status_code == 202, replay.text
                await app.state.composer_async_worker.run_until_idle()
                assert len(dispatches) == before + len(this_transition)
            assert not primary_replies
            with engine.connect() as conn:
                attempts = conn.execute(select(quota_provider_attempts_table)).mappings().all()
                usage = conn.execute(select(token_usage_ledger_table)).mappings().all()
                jobs = conn.execute(select(composer_async_operations_table)).mappings().all()
            assert {job["operation_id"] for job in jobs} == set(operation_ids)
            assert all(job["status"] == "completed" for job in jobs)
            assert {row["attempt_id"] for row in attempts} == {call.attempt_id for call in dispatches}
            assert all(row["settled_at"] is not None for row in attempts)
            assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in usage}
            assert all(row["prompt_tokens"] == row["completion_tokens"] == 1 for row in usage)
            messages = await service.get_messages(UUID(session_id), limit=None)
            audit_ids = {
                entry["call"]["call_id"]
                for message in messages
                if message.role == "audit" and message.tool_calls is not None
                for entry in message.tool_calls
                if entry["_kind"] == "llm_call_audit"
            }
            assert audit_ids == {call.attempt_id for call in dispatches}
            if transition == "authoring":
                current = await service.get_current_state(UUID(session_id))
                assert current is not None
                assert current.sources is not None
                assert current.sources["source"]["plugin"] == "text"
                assert current.metadata_ is not None
                assert current.metadata_["name"] == "Local provider-authored pipeline"
                calls = [
                    entry
                    for message in messages
                    if message.role == "assistant" and message.tool_calls is not None
                    for entry in message.tool_calls
                ]
                assert {(entry["id"], entry["function"]["name"]) for entry in calls} == {
                    ("local-tool-1-0", "get_plugin_schema"),
                    ("local-tool-1-1", "get_plugin_schema"),
                    ("local-tool-2-0", "set_pipeline"),
                    ("local-tool-3-0", "preview_pipeline"),
                }
                assert len(calls) == 4
                assert all(entry["wire_conformant"] is True for entry in calls)
                tool_rows = [message for message in messages if message.role == "tool"]
                assert len(tool_rows) == 4
                assert {message.tool_call_id for message in tool_rows} == {entry["id"] for entry in calls}
                assert all(json.loads(message.content)["success"] is True for message in tool_rows)
                mutation = next(message for message in tool_rows if message.tool_call_id == "local-tool-2-0")
                assert mutation.composition_state_id is not None
                authored = await service.get_state_in_session(state_id=mutation.composition_state_id, session_id=UUID(session_id))
                assert authored.session_id == current.session_id
                assert composition_content_hash(state_from_record(authored)) == composition_content_hash(state_from_record(current))
                # The default auto-commit compose-loop path persists the
                # authored state and dispatch audit directly. Proposal rows
                # belong to the separate approval/planner paths.
                proposals = await service.list_composition_proposals(UUID(session_id))
                assert proposals == []
    finally:
        await app.state.composer_async_worker.stop()
        engine.dispose()


@pytest.mark.asyncio
async def test_transition_instrument_rejects_primary_provider_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    physical_call = ProviderGateway._call_llm
    calls = 0

    async def bypass(
        self: ProviderGateway,
        messages: object,
        tools: object,
        *,
        provider_custody: ProviderCallCustody | None = None,
    ) -> _AdmittedLLMCompletion:
        nonlocal calls
        calls += 1
        if calls == 1:
            return await physical_call(self, messages, tools, provider_custody=provider_custody)
        # First transition dispatches normally. Counterfeit the second path
        # while leaving accounting real: a positive per-walk total misses it.
        assert type(provider_custody) is ProviderCallCustody
        await provider_custody.admit(model=self._model)
        # This intentional mutation counterfeits the dispatch marker and
        # response while skipping the sole SDK seam. The transition census
        # must still reject it after ordinary accounting settles.
        provider_custody.mark_sdk_entered()
        response = litellm.ModelResponse(
            id="local-bypass-control",
            model=self._model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello",
                        "tool_calls": None,
                    },
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )
        return _admit_composer_llm_completion(response)

    monkeypatch.setattr(ProviderGateway, "_call_llm", bypass)
    with pytest.raises(AssertionError, match="transition bypassed the primary provider"):
        await test_each_real_composer_transition_has_its_own_settled_provider_evidence(tmp_path, monkeypatch, "followup")
    assert calls == 2

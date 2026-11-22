"""What is durable after a runtime preflight fails inside a turn that already changed the pipeline.

One provider reply makes two tool calls: the first renames the pipeline and
succeeds, the second asks for a preview and the runtime preflight behind it
fails internally. The turn fails. Afterwards the record must still be
consistent: the audit trail says a call succeeded and produced a new version,
so the state history has to contain that version.

Driven through the real route, worker, compose loop and sessions service; only
the provider SDK call and the composer's runtime preflight are replaced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import litellm
import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import chat_messages_table, composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.sessions.test_routes import _make_app

_NEW_NAME = "Renamed before the diff"


async def _seed_valid_draft(app: Any, service: Any, session_id: UUID) -> Any:
    """A complete csv -> csv draft, so the turn starts from a state that is a usable diff baseline."""
    data_dir = Path(app.state.settings.data_dir)
    state = (
        CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
        .with_source(
            SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": str(data_dir / "blobs" / str(session_id) / "input.csv"), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )
        .with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": str(data_dir / "outputs" / str(session_id) / "out.csv"), "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
    ).to_dict()
    with fenced_operation_context(app.state.session_engine, str(session_id)) as context:
        return await service.save_composition_state(
            session_id,
            CompositionStateData(
                sources=state["sources"],
                nodes=state["nodes"],
                edges=state["edges"],
                outputs=state["outputs"],
                metadata_=state["metadata"],
                is_valid=True,
            ),
            provenance="session_seed",
            session_operation_context=context,
        )


@pytest.mark.asyncio
async def test_preflight_failure_after_a_mutation_leaves_a_consistent_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service = _make_app(tmp_path, quota_enabled=True)
    engine = app.state.session_engine

    def available(**kwargs: object) -> ComposerAvailability:
        model = kwargs["model"]
        assert type(model) is str
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)
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

    def mount(settings: Any) -> PromptGuardMount:
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
    composer = ComposerServiceImpl(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=service,
        session_engine=engine,
        plugin_snapshot_factory=app.state.plugin_snapshot_factory.for_user_id,
        operator_profile_registry=app.state.operator_profile_registry,
    )
    app.state.composer_service = composer
    app.state.interpretation_surfacing = composer._interpretation_surfacing

    def failing_runtime_preflight(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("synthetic runtime preflight worker failure")

    monkeypatch.setattr(composer._preflight, "runtime_preflight", failing_runtime_preflight)
    primary_calls = 0

    async def completion(**kwargs: Any) -> litellm.ModelResponse:
        nonlocal primary_calls
        model = kwargs["model"]
        tool_calls = None
        content: str | None = None
        if model == app.state.settings.composer_model:
            primary_calls += 1
            assert primary_calls == 1, "no provider call may follow the failed batch"
            tool_calls = []
            for index, (name, arguments) in enumerate((("set_metadata", {"patch": {"name": _NEW_NAME}}), ("preview_pipeline", {}))):
                sent = next(tool["function"] for tool in kwargs["tools"] if tool["function"]["name"] == name)
                dialect = ToolContractDialect.OPENAI_STRICT if "strict" in sent else ToolContractDialect.NONE
                tool_calls.append(
                    {
                        "id": ("call_rename", "call_preview")[index],
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(encode_semantic_arguments(name, dialect, arguments))},
                    }
                )
        else:
            payload = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "CLEAN", "note": None}
            if kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response":
                payload["publication_verdict"] = "ADMIT"
            content = json.dumps(payload)
        return litellm.ModelResponse(
            id=f"local-{uuid4()}",
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/sessions", json={"title": "Preflight double fault"})
        session_id = created.json()["id"]
        baseline = await _seed_valid_draft(app, service, UUID(session_id))
        settled = await submit_and_settle(
            client,
            app,
            path=f"/api/sessions/{session_id}/messages",
            body=message_body("Rename the pipeline, then preview it.", state_id=baseline.id, operation_id=str(uuid4())),
        )

    with engine.connect() as conn:
        messages = (
            conn.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == session_id)
                .order_by(chat_messages_table.c.sequence_no)
            )
            .mappings()
            .all()
        )
        states = (
            conn.execute(
                select(composition_states_table)
                .where(composition_states_table.c.session_id == session_id)
                .order_by(composition_states_table.c.version)
            )
            .mappings()
            .all()
        )

    # The turn failed, visibly, and the provider was not asked again.
    assert settled.final.json()["status"] == "failed"
    assert primary_calls == 1

    # (c) The change the first call made is in the state history, after the request-start state.
    assert [state["provenance"] for state in states] == ["session_seed", "preflight_persist"]
    request_start, saved = states
    assert request_start["id"] == str(baseline.id)
    assert _NEW_NAME in json.dumps(saved["metadata_"])

    # (a) Both calls are durable with their own outcome, and both point at the state that holds the change.
    envelopes = [(message, message["tool_calls"][0]) for message in messages if message["tool_calls"]]
    invocations = [(message, envelope["invocation"]) for message, envelope in envelopes if envelope.get("_kind") == "audit"]
    assert [(invocation["tool_call_id"], invocation["status"]) for _message, invocation in invocations] == [
        ("call_rename", "success"),
        ("call_preview", "plugin_crash"),
    ]
    assert {message["composition_state_id"] for message, _invocation in invocations} == {saved["id"]}

    # (b) The failure is recorded under the same operation as the calls' own evidence.
    evidence = [envelope for _message, envelope in envelopes if envelope.get("_kind") == "composer_diagnostic_evidence"]
    call_operations = {envelope["operation_id"] for envelope in evidence if envelope["tool_call_id"] in {"call_rename", "call_preview"}}
    failure_operations = {envelope["operation_id"] for envelope in evidence if envelope["evidence_id"].startswith("operation-failure:")}
    assert len(call_operations) == 1
    assert failure_operations == call_operations

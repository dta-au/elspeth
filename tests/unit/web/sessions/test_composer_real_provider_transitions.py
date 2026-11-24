"""Per-transition provider custody through real Composer, quota and durable worker."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import litellm
import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response
from jsonschema import Draft202012Validator
from litellm.exceptions import InternalServerError, RateLimitError, ServiceUnavailableError
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus, ToolContractDialect
from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardToolArgumentParts
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_gateway import (
    ProviderGateway,
    _admit_composer_llm_completion,
)
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
from elspeth.web.prompt_guard_config import PromptGuardSettings
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
from tests.unit.web.sessions.test_run_composer_turn import _seed_output_only_draft


@dataclass(frozen=True, slots=True)
class _Dispatch:
    operation_id: str
    attempt_id: str
    fence_id: str
    epoch: int
    model: str


def _inline_text_to_csv_pipeline(output_path: Path) -> dict[str, object]:
    return {
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
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "Local provider-authored pipeline"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ("initial", "followup", "source_missing", "recompose", "authoring", "proposal"))
async def test_each_real_composer_transition_has_its_own_settled_provider_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    first_primary_failure: str | None = None,
    quota_enabled: bool = True,
) -> None:
    app, service = _make_app(tmp_path, quota_enabled=quota_enabled)
    engine = app.state.session_engine

    def local_available(**kwargs: object) -> ComposerAvailability:
        model = kwargs["model"]
        assert type(model) is str
        return ComposerAvailability(available=True, model=model, provider="test")

    # This local SDK double needs no real provider credentials. Availability
    # is an explicit test dependency, rather than a fabricated environment key.
    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", local_available)
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": PromptGuardSettings(
                endpoint="https://primary-shield.example.test", subscription_key="offline-primary-shield"
            ),
            "composer_advisor_prompt_guard": PromptGuardSettings(
                endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield"
            ),
        }
    )
    shield_requests = []

    def shield_response(request):
        assert request.url.host in {"primary-shield.example.test", "advisor-shield.example.test"}
        payload = json.loads(request.content)
        shield_requests.append((request.url.host, payload))
        return Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def mount(settings):
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
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
    failed_attempt_ids: list[str] = []
    rootless_reviews = []
    rootless_outcomes = []
    rootless_prompts = []
    rootless_request = "Build a pipeline from customer complaints."
    rootless_correction = "No pipeline has been configured. Please supply the customer complaints source and desired report."
    rootless_findings = "The reply claims a ready pipeline, but the saved output draft has no customer complaints source."
    actual_checkpoint = app.state.composer_service._advisor_checkpoint._run_advisor_checkpoint
    assert actual_checkpoint.__func__ is AdvisorCheckpointOwner._run_advisor_checkpoint
    assert actual_checkpoint.__module__ == "elspeth.web.composer.advisor_checkpoint"

    async def observe_checkpoint(*args, **kwargs):
        if transition == "source_missing" and kwargs["phase"] == "end":
            rootless_reviews.append(kwargs["publication_review"])
        verdict = await actual_checkpoint(*args, **kwargs)
        if transition == "source_missing" and kwargs["phase"] == "end":
            assert verdict.publication_binding == rootless_reviews[-1].binding
            rootless_outcomes.append(verdict)
        return verdict

    monkeypatch.setattr(app.state.composer_service._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)
    primary_replies = ["Hello. I can explain pipelines."]
    if transition == "source_missing":
        primary_replies = ["The pipeline is ready.", rootless_correction]
    if transition == "followup":
        primary_replies.append("Hello again. I can explain pipelines.")
    if transition == "authoring":
        primary_replies = ["", "", "", "Pipeline configured."]
    if transition == "proposal":
        primary_replies = [""]

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
            if first_primary_failure is not None and not failed_attempt_ids:
                # One transient failure on the first primary dispatch. Its
                # retry must stay inside this transition, on the common path.
                failed_attempt_ids.append(pending["attempt_id"])
                if first_primary_failure == "internal_server":
                    raise InternalServerError("private failure", llm_provider="test", model=model)
                if first_primary_failure == "service_unavailable":
                    raise ServiceUnavailableError("private failure", llm_provider="test", model=model)
                assert first_primary_failure == "rate_limit"
                request = Request("POST", "https://provider.example.test")
                limited = Response(429, request=request, headers={"retry-after": "1"})
                raise RateLimitError("private failure", llm_provider="test", model=model, response=limited)
            assert primary_replies, "Unexpected extra primary provider dispatch"
            content = primary_replies.pop(0)
            primary_ordinal = sum(call.model == model and call.attempt_id not in failed_attempt_ids for call in dispatches)
            sent_names = {tool["function"]["name"] for tool in kwargs["tools"]}
            if transition in {"initial", "recompose", "proposal"}:
                assert "emit_pipeline_proposal" in sent_names and "set_pipeline" not in sent_names
            else:
                assert "set_pipeline" in sent_names
            if transition == "proposal":
                proposal_tools = kwargs["tools"]
                assert type(proposal_tools) is list
                sent = next(tool["function"] for tool in proposal_tools if tool["function"]["name"] == "emit_pipeline_proposal")
                proposal_arguments = {"pipeline": _inline_text_to_csv_pipeline(tmp_path / "outputs" / job["session_id"] / "proposal.csv")}
                Draft202012Validator(sent["parameters"]).validate(proposal_arguments)
                tool_calls = [
                    {
                        "id": "local-proposal-1",
                        "type": "function",
                        "function": {"name": "emit_pipeline_proposal", "arguments": json.dumps(proposal_arguments)},
                    }
                ]
            if transition == "source_missing":
                rootless_prompts.append(deepcopy(kwargs["messages"]))
                if primary_ordinal == 2:
                    assert len(rootless_reviews) == len(rootless_outcomes) == 1, "PRIMARY repair bypassed actual END"
                    assert rootless_outcomes[0].publication_verdict == "WITHHOLD"
                    assert rootless_findings in json.dumps(kwargs["messages"])
                    assert any(item["role"] == "assistant" and item["content"] == "The pipeline is ready." for item in kwargs["messages"])
            if transition == "authoring" and primary_ordinal < 4:
                arguments = {}
                tool_name = "preview_pipeline"
                if primary_ordinal == 2:
                    tool_name = "set_pipeline"
                    arguments = _inline_text_to_csv_pipeline(tmp_path / "outputs" / job["session_id"] / "authoring.csv")
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
            schema_name = kwargs["response_format"]["json_schema"]["name"]
            assert schema_name in {"advisor_checkpoint_response", "advisor_end_response"}
            payload = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "CLEAN", "note": None}
            if schema_name == "advisor_end_response":
                payload["publication_verdict"] = "ADMIT"
                if transition == "source_missing":
                    assert len(rootless_reviews) <= 2
                    review = rootless_reviews[-1]
                    assert any(
                        review.provider_review_context_json()
                        in (item["content"] if isinstance(item["content"], str) else "".join(part["text"] for part in item["content"]))
                        for item in kwargs["messages"]
                    )
                    packet = json.loads(review.payload_json)
                    assert packet["current_request"] == rootless_request
                    assert packet["candidate_reply"] == ("The pipeline is ready." if len(rootless_reviews) == 1 else rootless_correction)
                    assert packet["saved_effects"]["saved_revision_count"] == 0
                    payload["publication_verdict"] = "WITHHOLD" if len(rootless_reviews) == 1 else "ADMIT"
                    payload["findings"] = rootless_findings if len(rootless_reviews) == 1 else ""
            elif transition == "source_missing":
                raise AssertionError("An unchanged source-less draft turn must not use EARLY")
            content = json.dumps(payload)
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
            baseline = None
            if transition in {"followup", "source_missing", "authoring"}:
                # Ordinary-loop scenarios start from a real previous draft.
                # Initial and recompose retain the empty PLANNER -> END path.
                baseline = await _seed_output_only_draft(app, service, UUID(session_id))
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
                    content = rootless_request if transition == "source_missing" else "Hello"
                    if transition in {"authoring", "proposal"}:
                        content = "Create a pipeline reading hello from an inline text source and writing the text to CSV."
                    current = await service.get_current_state(UUID(session_id))
                    body = message_body(
                        content,
                        state_id=None if current is None else current.id,
                        operation_id=operation_id,
                    )
                before = len(dispatches)
                settled = await submit_and_settle(client, app, path=path, body=body)
                if first_primary_failure is not None and quota_enabled:
                    # A configured token quota cannot admit another call while
                    # the failed call's usage is unknown: the retry decision is
                    # reached on the common path and refused at re-admission.
                    final = settled.final.json()
                    assert final["status"] == "failed", settled.final.text
                    assert final["error"]["body"]["detail"]["failure_code"] == "token_accounting_unavailable", settled.final.text
                    assert [call.attempt_id for call in dispatches] == failed_attempt_ids
                    assert {call.operation_id for call in dispatches} == {operation_id}
                    with engine.connect() as conn:
                        attempts = conn.execute(select(quota_provider_attempts_table)).mappings().all()
                        usage = conn.execute(select(token_usage_ledger_table)).mappings().all()
                    assert [row["attempt_id"] for row in attempts] == failed_attempt_ids
                    assert all(row["settled_at"] is not None for row in attempts)
                    assert [(row["prompt_tokens"], row["completion_tokens"]) for row in usage] == [(None, None)]
                    messages = await service.get_messages(UUID(session_id), limit=None)
                    assert [
                        (entry["call"]["call_id"], entry["call"]["status"])
                        for message in messages
                        if message.role == "audit" and message.tool_calls is not None
                        for entry in message.tool_calls
                        if entry["_kind"] == "llm_call_audit"
                    ] == [(failed_attempt_ids[0], ComposerLLMCallStatus.API_ERROR)]
                    return
                assert settled.final.json()["status"] == "completed", settled.final.text
                this_transition = dispatches[before:]
                assert this_transition, "transition bypassed the primary provider"
                assert {call.operation_id for call in this_transition} == {operation_id}
                retried = [call.model for call in this_transition if call.attempt_id in failed_attempt_ids]
                assert sum(call.model == app.state.settings.composer_model for call in this_transition) == len(retried) + (
                    4 if transition == "authoring" else 2 if transition == "source_missing" else 1
                ), "transition bypassed the primary provider"
                if transition in {"initial", "recompose"}:
                    # Pinned current behaviour: a failed planner call leaves its
                    # cost unknown, so END has no validated allowance and the
                    # declined reply is blocked without an advisor call.
                    assert [call.model for call in this_transition] == [
                        *retried,
                        app.state.settings.composer_model,
                        *([] if retried else [app.state.settings.composer_advisor_model]),
                    ]
                if transition == "source_missing":
                    assert [call.model for call in this_transition] == [
                        *retried,
                        app.state.settings.composer_model,
                        app.state.settings.composer_advisor_model,
                        app.state.settings.composer_model,
                        app.state.settings.composer_advisor_model,
                    ]
                if transition == "proposal":
                    assert [call.model for call in this_transition] == [*retried, app.state.settings.composer_model]
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
            assert len(failed_attempt_ids) == (0 if first_primary_failure is None else 1)
            failed_ledger_ids = {row["ledger_entry_id"] for row in attempts if row["attempt_id"] in failed_attempt_ids}
            assert len(failed_ledger_ids) == len(failed_attempt_ids)
            assert all(
                (row["prompt_tokens"], row["completion_tokens"]) == ((None, None) if row["entry_id"] in failed_ledger_ids else (1, 1))
                for row in usage
            )
            messages = await service.get_messages(UUID(session_id), limit=None)
            audit_statuses = {
                entry["call"]["call_id"]: entry["call"]["status"]
                for message in messages
                if message.role == "audit" and message.tool_calls is not None
                for entry in message.tool_calls
                if entry["_kind"] == "llm_call_audit"
            }
            assert set(audit_statuses) == {call.attempt_id for call in dispatches}
            assert [call_id for call_id, status in audit_statuses.items() if status == ComposerLLMCallStatus.API_ERROR] == (
                failed_attempt_ids
            )
            guards = [
                entry["check"]
                for message in messages
                if message.role == "audit" and message.tool_calls is not None
                for entry in message.tool_calls
                if entry["_kind"] == "prompt_guard_audit"
            ]
            assert [(check["role"], check["disposition"]) for check in guards] == [
                ("composer" if call.model == app.state.settings.composer_model else "advisor", "allow") for call in dispatches
            ]
            assert shield_requests
            if transition == "source_missing":
                assert [outcome.publication_verdict for outcome in rootless_outcomes] == ["WITHHOLD", "ADMIT"]
                assert rootless_reviews[0].binding != rootless_reviews[1].binding
                passes = [
                    entry["pass"]
                    for message in messages
                    if message.role == "audit" and message.tool_calls is not None
                    for entry in message.tool_calls
                    if entry["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
                ]
                assert [(item["publication_verdict"], item["publication_binding"]) for item in passes] == [
                    (outcome.publication_verdict, review.binding)
                    for review, outcome in zip(rootless_reviews, rootless_outcomes, strict=True)
                ]
                assert not any(
                    item["role"] == "user"
                    and isinstance(item["content"], str)
                    and item["content"].startswith(
                        (
                            "[composer-system] No tool has run this turn",
                            "[composer-system] No composition-state mutation completed successfully",
                        )
                    )
                    for prompt in rootless_prompts
                    for item in prompt
                )
                current = await service.get_current_state(UUID(session_id))
                assert current is not None and baseline is not None
                assert current.id == baseline.id
                assert not current.sources and current.outputs == baseline.outputs
                assert any(message.role == "assistant" and message.content.startswith(rootless_correction) for message in messages)
                assert not any(message.role == "assistant" and "The pipeline is ready." in message.content for message in messages)
                assert any(
                    message.role == "audit"
                    and message.content == "The pipeline is ready."
                    and any(
                        entry["_kind"] == COMPOSER_WITHHELD_REPLY_KIND and entry["origin"] == "repair_gate_superseded"
                        for entry in (message.tool_calls or ())
                    )
                    for message in messages
                )
            if transition == "initial" and failed_attempt_ids:
                # The unreviewed decline is withheld, never published: the user
                # sees a completed turn whose only reply is trusted system
                # notices naming the real cause. The session has no draft, so
                # no saved draft progress is claimed.
                user_messages = await client.get(f"/api/sessions/{session_id}/messages")
                assert user_messages.status_code == 200, user_messages.text
                assert [item["role"] for item in user_messages.json()] == ["user", "assistant"]
                withheld = user_messages.json()[1]
                assert [(segment["kind"], segment["content"]) for segment in withheld["segments"]] == [
                    (
                        "trusted_system_notice",
                        "The reply was withheld because the completion review could not run: the cost of an earlier "
                        "model call in this request could not be confirmed. Continue in a new message.",
                    ),
                    ("trusted_system_notice", "The completion review could not finish, so the reply was withheld."),
                ]
                assert "Hello. I can explain pipelines." not in withheld["content"]
            if transition == "proposal":
                # Measured: a retried planner call still delivers its proposal,
                # exactly as on the no-failure path.
                user_messages = await client.get(f"/api/sessions/{session_id}/messages")
                assert user_messages.status_code == 200, user_messages.text
                assert [(item["role"], item["content"]) for item in user_messages.json()] == [
                    ("user", "Create a pipeline reading hello from an inline text source and writing the text to CSV."),
                    ("assistant", "I prepared and validated the requested pipeline. ELSPETH will commit it atomically."),
                ]
                user_proposals = await client.get(f"/api/sessions/{session_id}/proposals")
                assert user_proposals.status_code == 200, user_proposals.text
                assert [(item["tool_name"], item["status"], item["summary"]) for item in user_proposals.json()] == [
                    ("set_pipeline", "committed", "Replace the pipeline with 1 input, 0 processing nodes, and 1 output.")
                ]
                current = await service.get_current_state(UUID(session_id))
                assert current is not None and current.sources is not None
                assert current.sources["source"]["plugin"] == "text"
                assert user_proposals.json()[0]["committed_state_id"] == str(current.id)
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
@pytest.mark.parametrize("quota", ("quota_off", "quota_on"))
@pytest.mark.parametrize("failure_kind", ("internal_server", "rate_limit", "service_unavailable"))
@pytest.mark.parametrize("transition", ("initial", "source_missing", "proposal"))
async def test_rootless_and_first_turn_retry_a_transient_failure_inside_the_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str, failure_kind: str, quota: str
) -> None:
    # "initial" is the empty-session first turn the tutorial takes (PLANNER ->
    # END); "source_missing" is the rootless compose-loop turn. Neither has
    # its own retry: both reach the shared retry decision inside the same
    # transition, with the failed physical call audited and settled. Without
    # a token quota the retry runs; with one, the unknown usage of the failed
    # call refuses re-admission (the existing accounting contract).
    await test_each_real_composer_transition_has_its_own_settled_provider_evidence(
        tmp_path, monkeypatch, transition, first_primary_failure=failure_kind, quota_enabled=quota == "quota_on"
    )


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
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> _AdmittedLLMCompletion:
        nonlocal calls
        calls += 1
        if calls == 1:
            return await physical_call(
                self,
                messages,
                tools,
                provider_custody=provider_custody,
                credential_state=credential_state,
                prompt_guard_trusted_message_indexes=prompt_guard_trusted_message_indexes,
                prompt_guard_content_parts=prompt_guard_content_parts,
                prompt_guard_tool_argument_parts=prompt_guard_tool_argument_parts,
            )
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

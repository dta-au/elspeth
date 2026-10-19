"""Selected ST-02 retirement through the real public planner proposal route.

The wrong-constant cases deliberately characterize lost automatic correction.
Passing their admission checks is not a claim of semantic fidelity. SDK and
Azure HTTP responses are synthetic; no graph is executed.
"""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import litellm
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND, ComposerDiagnosticEvidence
from elspeth.web.composer.pipeline_planner import PLANNER_TERMINAL_INSTRUCTION
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.protocol import (
    COMPOSER_HISTORY_USER_AUTHORED_KEY,
    COMPOSER_HISTORY_USER_MESSAGE_ID_KEY,
)
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    chat_messages_table,
    composer_async_operations_table,
    composition_states_table,
    quota_provider_attempts_table,
    session_operation_fences_table,
    token_usage_ledger_table,
)
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.integration.web.composer.test_freeform_pipeline_planner import _routing_terminal_response
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _TestSnapshotFactory

_CURRENT_REQUEST = "Build the requested pipeline."
_ASSISTANT_DISTRACTOR = "Untrusted assistant interpretation: use amount > 900 instead."
_NEUTRAL = "Build a CSV-to-JSONL pipeline that sends each row unchanged to a pair of sinks named high_value / standard."
_QUOTED_DATA = (
    'Build a CSV-to-JSONL pipeline that sends each row, including the literal note text "amount > 500", '
    "unchanged to a pair of sinks named high_value / standard."
)
_GENUINE_ROUTING = "Build a CSV-to-JSONL pipeline that routes rows with amount > 500 to high_value, with every other row going to standard."


def _envelopes(rows, kind, field):
    return [envelope[field] for row in rows if row.role == "audit" for envelope in (row.tool_calls or ()) if envelope["_kind"] == kind]


def _planner_payload(messages):
    candidates = []
    for message in messages:
        if message["role"] != "user" or not isinstance(message["content"], str):
            continue
        try:
            value = json.loads(message["content"])
        except json.JSONDecodeError:
            continue
        if type(value) is dict and value.get("instruction") == PLANNER_TERMINAL_INSTRUCTION:
            candidates.append(value)
    assert len(candidates) == 1, "The actual SDK request must be the planner terminal-tool request"
    return candidates[0]


@pytest.fixture
def route_app(tmp_path):
    app, sessions = _make_app(tmp_path, quota_enabled=True)
    try:
        yield app, sessions
    finally:
        app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior_request", "condition", "trust_mode", "semantic_match"),
    [
        pytest.param(_NEUTRAL, "True", "explicit_approve", True, id="neutral-fan-out-control"),
        pytest.param(_QUOTED_DATA, "True", "explicit_approve", True, id="quoted-comparison-is-data"),
        pytest.param(_GENUINE_ROUTING, "row['amount'] > 500", "explicit_approve", True, id="genuine-routing-first-shot-control"),
        pytest.param(_GENUINE_ROUTING, "True", "explicit_approve", False, id="wrong-first-constant-pending-human-review"),
        pytest.param(_GENUINE_ROUTING, "True", "auto_commit", False, id="wrong-first-constant-auto-commit-loss"),
    ],
)
async def test_referential_planner_threshold_retirement_preserves_first_candidate(
    tmp_path, monkeypatch, route_app, prior_request, condition, trust_mode, semantic_match
):
    app, sessions = route_app
    engine = app.state.session_engine
    guard_settings = PromptGuardSettings(endpoint="https://st02-shield.example.test", subscription_key=SecretStr("offline-st02-shield"))
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "test/planner",
            "composer_boot_probe_enabled": False,
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": guard_settings,
            "composer_advisor_prompt_guard": guard_settings,
        }
    )
    # Keep the fixture's configured call, repair, byte, cost and deadline limits.
    settings = app.state.settings
    shield_payloads = []
    guard_bindings = []
    events = []

    def shield_response(request):
        assert request.url.host == "st02-shield.example.test"
        payload = json.loads(request.content)
        assert payload["userPrompt"] == _CURRENT_REQUEST
        shield_payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def mount(config):
        assert config == guard_settings
        client = AzurePromptGuardClient(config, http_transport=httpx.MockTransport(shield_response))
        real_inspect = client.inspect
        assert real_inspect.__func__ is AzurePromptGuardClient.inspect
        assert real_inspect.__module__ == "elspeth.web.composer.prompt_guard_clients"

        async def inspect(request):
            # Forward to the actual Azure adapter; this observer supplies no verdict.
            assert any(prior_request in document for document in request.documents)
            guard_bindings.append(request.request_hash)
            decision = await real_inspect(request)
            assert decision.disposition == "allow"
            events.append("guard")
            return decision

        monkeypatch.setattr(client, "inspect", inspect)
        return PromptGuardMount(client)

    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service,
        settings=settings,
        sessions_service=sessions,
        session_engine=engine,
    )
    # The planner success/proposal path does not invoke END. Do not substitute a
    # CLEAN verdict or suppress an unexpected advisor/hatch transition.
    checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint
    assert checkpoint.__func__ is AdvisorCheckpointOwner._run_advisor_checkpoint
    assert checkpoint.__module__ == "elspeth.web.composer.advisor_checkpoint"
    app.state.composer_service = composer
    app.state.interpretation_surfacing = composer._interpretation_surfacing
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    session = await sessions.create_session("alice", "Synthetic ST-02 retirement session", "local")
    await sessions.update_composer_preferences(session.id, trust_mode=trust_mode, density_default="high", actor="test")
    source_dir = tmp_path / "blobs" / str(session.id)
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "input.csv").write_text("amount,note\n600,synthetic\n100,ordinary\n", encoding="utf-8")
    (tmp_path / "outputs" / str(session.id)).mkdir(parents=True, exist_ok=True)
    prior = await sessions.add_message(session.id, "user", prior_request, writer_principal="route_user_message")
    await sessions.add_message(session.id, "assistant", _ASSISTANT_DISTRACTOR, writer_principal="compose_loop")
    mint_observations = []
    sdk_requests = []
    authored_pipelines = []
    compose_observations = []
    real_mint = ProviderInvocationOwner.mint
    real_compose = composer.compose
    # A spare identical response lets the old one-nudge path finish with real
    # rejection evidence. The selected behavior must leave this spare unused;
    # never script a corrected second graph and call it first-shot fidelity.
    conditions = (condition, condition)

    def observe_mint(owner, family):
        custody = real_mint(owner, family)
        assert owner.service is sessions
        assert family is ProviderInvocationFamily.PLANNER, "No unrelated PRIMARY, TITLE, ADVISOR or hatch is scripted"
        mint_observations.append((owner, custody))
        return custody

    async def observe_compose(message, messages, state, *args, **kwargs):
        assert message == _CURRENT_REQUEST
        human = [row for row in messages if COMPOSER_HISTORY_USER_AUTHORED_KEY in row]
        assert len(human) == 1
        assert human[0]["content"] == prior_request
        assert human[0][COMPOSER_HISTORY_USER_AUTHORED_KEY] is True
        assert human[0][COMPOSER_HISTORY_USER_MESSAGE_ID_KEY] == str(prior.id)
        owner = kwargs["provider_owner"]
        assert owner.service is sessions
        assert owner.required_work.coordinator is kwargs["required_work"].coordinator
        assert kwargs["session_operation_context"] == owner.required_work.coordinator.authority.context
        result = await real_compose(message, messages, state, *args, **kwargs)
        compose_observations.append((kwargs, result))
        return result

    async def complete(**kwargs):
        ordinal = len(sdk_requests)
        assert ordinal < len(conditions), "The finite characterization script has no extra response"
        assert kwargs["model"] == settings.composer_model
        assert kwargs["messages"][0] == {"role": "system", "content": composer._composer_skill_text}
        assert "emit_pipeline_proposal" in {tool["function"]["name"] for tool in kwargs["tools"]}
        assert "response_format" not in kwargs
        payload = _planner_payload(kwargs["messages"])
        assert payload["intent"] == _CURRENT_REQUEST
        assert payload["conversation_context"] == {
            "prior_user_requests": [{"history_index": 0, "content": prior_request}],
            "additional_prior_user_requests_omitted": 0,
            "prior_assistant_messages": [{"history_index": 1, "content": _ASSISTANT_DISTRACTOR}],
        }
        assert _ASSISTANT_DISTRACTOR not in json.dumps(payload["conversation_context"]["prior_user_requests"])
        assert len(mint_observations) == ordinal + 1
        _owner, custody = mint_observations[-1]
        assert custody.attempt is not None
        with engine.connect() as connection:
            pending = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == custody.attempt.attempt_id)
            ).one()
            rows = connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == str(session.id))
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        checks = _envelopes(rows, "prompt_guard_audit", "check")
        assert len(checks) == ordinal + 1
        assert checks[-1]["mode"] == "required" and checks[-1]["disposition"] == "allow"
        assert checks[-1]["role"] == "composer" and checks[-1]["request_hash"] == guard_bindings[-1]
        assert checks[-1]["operation_id"] == pending.operation_id == custody.context.fence.operation_id
        assert checks[-1]["operation_epoch"] == pending.operation_epoch == custody.context.fence.operation_epoch
        assert pending.identity_id == "alice" and pending.session_id == str(session.id)
        assert pending.settled_at is None
        assert events == [event for _ in range(ordinal) for event in ("guard", "sdk")] + ["guard"]
        events.append("sdk")
        sdk_requests.append((custody.attempt.attempt_id, deepcopy(kwargs)))
        scripted = _routing_terminal_response(tmp_path, str(session.id), condition=conditions[ordinal])
        call = scripted.choices[0].message.tool_calls[0]
        authored_pipelines.append(json.loads(call.function.arguments)["pipeline"])
        return litellm.ModelResponse(
            id=f"offline-st02-{ordinal + 1}",
            model=kwargs["model"],
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"st02-{ordinal + 1}",
                                "type": "function",
                                "function": {"name": call.function.name, "arguments": call.function.arguments},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    monkeypatch.setattr(ProviderInvocationOwner, "mint", observe_mint)
    monkeypatch.setattr(composer, "compose", observe_compose)
    monkeypatch.setattr(litellm, "acompletion", complete)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = message_body(_CURRENT_REQUEST)
            settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/messages", body=body)
            assert settled.final.json()["status"] == "completed", settled.final.text
            settled.result()
            before_replay = len(sdk_requests)
            replay = await client.post(f"/api/sessions/{session.id}/messages", json=body)
            assert replay.status_code == 202
            await app.state.composer_async_worker.run_until_idle()
            assert len(sdk_requests) == before_replay

        assert len(compose_observations) == 1
        proposals = await sessions.list_composition_proposals(session.id)
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.tool_name == "set_pipeline"
        assert proposal.pipeline_metadata is not None
        accepted = deep_thaw(proposal.arguments_json)
        assert accepted == authored_pipelines[0], "Retain the first exact authored graph, not a corrected replacement"
        assert accepted["nodes"][0]["condition"] == condition
        assert len(accepted["nodes"]) == 1 and accepted["nodes"][0]["node_type"] == "gate"
        assert {output["sink_name"] for output in accepted["outputs"]} == {"high_value", "standard"}
        if condition == "True":
            assert accepted["nodes"][0]["routes"] == {"true": "fork", "false": "fork"}
            assert accepted["nodes"][0]["fork_to"] == ["high_value", "standard"]
        else:
            assert accepted["nodes"][0]["routes"] == {"true": "high_value", "false": "standard"}
        # This fixture's semantic oracle is stated independently of admission.
        # For the genuine request, a constant fanout is deliberately WRONG even
        # when real structural checks or the configured trust mode admit it.
        requested_condition = "row['amount'] > 500" if prior_request == _GENUINE_ROUTING else "True"
        matches_request = accepted["nodes"][0]["condition"] == requested_condition
        assert matches_request is semantic_match
        if not semantic_match:
            assert prior_request == _GENUINE_ROUTING and condition == "True"
            assert accepted["nodes"][0]["routes"] != {"true": "high_value", "false": "standard"}
        observed_result = compose_observations[0][1]
        current = await sessions.get_current_state(session.id)
        if trust_mode == "explicit_approve":
            assert proposal.status == "pending" and current is None
            assert proposal.committed_state_id is None and observed_result.pipeline_commit_intent is None
        else:
            # Real runtime preflight and normal settlement must authorize this
            # structurally valid candidate. Neither is semantic proof.
            assert observed_result.runtime_preflight is not None and observed_result.runtime_preflight.is_valid
            intent = observed_result.pipeline_commit_intent
            assert intent is not None and intent.proposal_id == proposal.id
            assert intent.draft_hash == proposal.pipeline_metadata.draft_hash
            assert proposal.status == "committed" and current is not None
            assert proposal.committed_state_id == current.id
            saved = state_from_record(current)
            assert saved.nodes[0].condition == condition
            assert deep_thaw(saved.nodes[0].routes) == accepted["nodes"][0]["routes"]
        with engine.connect() as connection:
            rows = connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == str(session.id))
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
            attempts = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session.id))
            ).all()
            ledger = connection.execute(
                select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session.id))
            ).all()
            jobs = connection.execute(
                select(composer_async_operations_table).where(composer_async_operations_table.c.session_id == str(session.id))
            ).all()
            fence = connection.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
            ).one()
            states = connection.execute(
                select(composition_states_table).where(composition_states_table.c.session_id == str(session.id))
            ).all()
        assert len(states) == (1 if trust_mode == "auto_commit" else 0)
        assert len(jobs) == 1 and jobs[0].status == "completed"
        assert jobs[0].operation_id == body["operation_id"] and jobs[0].actor_user_id == "alice"
        assert {row.operation_id for row in attempts} == {jobs[0].session_operation_id}
        assert {row.operation_epoch for row in attempts} == {jobs[0].session_operation_epoch}
        assert fence.released_at is not None
        assert len({id(owner.required_work.coordinator) for owner, _custody in mint_observations}) == 1
        mint_observations[0][0].required_work.coordinator.assert_completed()
        calls = _envelopes(rows, "llm_call_audit", "call")
        semantic = _envelopes(rows, "planner_attempt_audit", "attempt")
        checks = _envelopes(rows, "prompt_guard_audit", "check")
        # This is the causal RED boundary on old source: its exact retired
        # threshold rejection remains observable, rather than an exhausted fake.
        assert [entry["rejection_codes"] for entry in semantic] == [[]]
        assert [entry["outcome"] for entry in semantic] == ["accepted"]
        assert [entry["led_to"] for entry in semantic] == ["done"]
        assert proposal.pipeline_metadata.repair_count == 0
        assert len(sdk_requests) == len(guard_bindings) == 1
        assert len(shield_payloads) >= 1, "The real Azure adapter may batch one guard inspection across HTTP calls"
        assert events == ["guard", "sdk"]
        assert not _envelopes(rows, "advisor_checkpoint_pass_audit", "pass"), "Planner success gains no replacement END stage"
        private = [
            ComposerDiagnosticEvidence.from_envelope(
                envelope, expected_session_id=str(session.id), expected_operation_id=jobs[0].session_operation_id
            )
            for row in rows
            for envelope in row.tool_calls or ()
            if envelope["_kind"] == DIAGNOSTIC_EVIDENCE_KIND
        ]
        candidates = [entry for entry in private if entry.tool_name == "emit_pipeline_proposal"]
        assert len(candidates) == 1 and candidates[0].status == "accepted"
        assert candidates[0].operation_epoch == jobs[0].session_operation_epoch
        assert candidates[0].tool_call_id == "st02-1"
        assert candidates[0].arguments.disposition == "retained" and candidates[0].arguments.content is not None
        assert json.loads(candidates[0].arguments.content)["pipeline"] == authored_pipelines[0]
        assert candidates[0].wire_arguments.disposition == "retained" and candidates[0].wire_arguments.content is not None
        assert json.loads(candidates[0].wire_arguments.content)["pipeline"] == authored_pipelines[0]
        assert [check["request_hash"] for check in checks] == guard_bindings
        assert len(attempts) == len(ledger) == len(calls) == len(sdk_requests)
        assert (
            {row.attempt_id for row in attempts}
            == {call["call_id"] for call in calls}
            == {attempt_id for attempt_id, _request in sdk_requests}
        )
        assert all(row.settled_at is not None and row.ledger_entry_id in {entry.entry_id for entry in ledger} for row in attempts)
        assert all(entry.prompt_tokens == 10 and entry.completion_tokens == 5 for entry in ledger)
        assert all(call["status"] == "success" for call in calls)
        for attempt_id, request in sdk_requests:
            audit = next(call for call in calls if call["call_id"] == attempt_id)
            assert audit["messages_hash"] == stable_hash(request["messages"])
            assert audit["tools_spec_hash"] == stable_hash(request["tools"])
    finally:
        await app.state.composer_async_worker.stop()

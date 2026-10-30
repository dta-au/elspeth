"""Actual END review protects rootless replies without a lexical PRIMARY retry."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import litellm
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_decision import AdvisorGatePassed
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_states_table,
    quota_provider_attempts_table,
    session_operation_fences_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.routes._helpers import _composer_history_content
from tests.helpers.advisor_review import parse_advisor_publication_review
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _seed_output_only_draft, _TestSnapshotFactory

_FALSE_COMPLETION = "The CSV source is configured and the pipeline is ready."
_TRUTHFUL_CORRECTION = "I have not configured a source. Please supply the CSV file."
_BUILD_REQUEST = 'Please create the pipeline described by this specification: "CSV source and JSON output".'
_BLOB_REQUEST = 'Please make CSV data for this example: "one id column". Do not configure a pipeline yet.'
_BLOB_CONTENT = "id\n1\n"
_BLOB_CORRECTION = "The example CSV is saved. No pipeline source or graph has been configured."
_ORDINARY_REQUESTS = (
    "Do not build anything; explain how CSV parsing works.",
    "Are you able to run pipelines yourself?",
    "How do I route rows to two different files?",
    "I make heavy use of CSV files.",
    'What does "build a pipeline" mean?',
)


@pytest.mark.asyncio
@pytest.mark.parametrize("publication_case", ["truthful-partial", "overclaim", "changed-decision"])
async def test_real_end_retains_scoped_limitations_after_supported_mutation(monkeypatch, real_route_app, publication_case):
    app, sessions = real_route_app
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4.1-mini",
            "composer_endpoint_base_url": "https://offline-model.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-primary-key"),  # secret-scan: allow-this-line
            "composer_advisor_model": "openai/gpt-4.1",
            "composer_advisor_endpoint_base_url": "https://offline-model.example.test/v1",
            "composer_advisor_endpoint_api_key": SecretStr("offline-advisor-key"),  # secret-scan: allow-this-line
            "composer_prompt_guard_mode": "debug-when-unconfigured",
            "composer_prompt_guard": None,
            "composer_advisor_prompt_guard": None,
        }
    )
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(), app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    session = await sessions.create_session("alice", "Scoped capability limit", "local")
    baseline = await _seed_output_only_draft(app, sessions, session.id)
    request = 'Follow this specification: "Save a JSON output named report, and perform unavailable operation B."'
    candidate = (
        "Both requested operations are complete."
        if publication_case == "overclaim"
        else "The report output is saved. Operation B is unavailable; this partial draft is not ready to run."
    )
    primary_index = 0
    stop_reviews = []
    end_inputs = []
    end_verdicts = []
    changed_decision = False
    original_accept = TurnEvidence.accept_abandonment
    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint
    assert original_checkpoint.__func__ is AdvisorCheckpointOwner._run_advisor_checkpoint

    def observe_accept(evidence, review, decision, **kwargs):
        original_accept(evidence, review, decision, **kwargs)
        stop_reviews.append((evidence, review, decision, kwargs))

    async def observe_checkpoint(*args, **kwargs):
        nonlocal changed_decision
        if kwargs["phase"] == "end":
            packet = kwargs["publication_review"]
            end_inputs.append(packet)
            payload = json.loads(packet.payload_json)
            assert payload["saved_effects"]["saved_revision_count"] == 1
            assert len(payload["historical_authoring_limitations"]) == 1
            assert payload["historical_authoring_limitations"][0]["decision"] == "CONTINUE_WITH_LIMITATION"
            assert payload["authoring_stop_review"]["decision"] == "STOP_CONFIRMED"
        verdict = await original_checkpoint(*args, **kwargs)
        if kwargs["phase"] == "end":
            end_verdicts.append(verdict)
            if publication_case == "changed-decision" and not changed_decision:
                evidence, review, decision, inputs = stop_reviews[-1]
                # The candidate and completed feedback advanced the transcript
                # since this stop was accepted. Change the decision using a
                # current owned review, rather than replaying stale authority.
                current_review = evidence.abandonment_context(
                    request=request,
                    current_state=inputs["current_state"],
                    plugin_snapshot=inputs["plugin_snapshot"],
                    messages=[],
                )
                assert current_review.binding != review.binding
                original_accept(
                    evidence,
                    current_review,
                    AdvisorAbandonmentResponse(
                        decision=decision.decision,
                        reason="A different prerequisite now blocks the remaining work.",
                        next_action=None,
                        question="Which prerequisite is intended?",
                    ),
                    **inputs,
                )
                changed_decision = True
        return verdict

    async def complete(**kwargs):
        nonlocal primary_index
        tool_calls = None
        if kwargs["model"] == app.state.settings.composer_model:
            primary_index += 1
            assert primary_index <= 4, "A confirmed whole-request stop must not restart authoring"
            if primary_index in (1, 3):
                name = "request_advisor_hint"
                arguments = {
                    "trigger": "cannot_complete",
                    "problem_summary": "Operation B is unavailable; inspect remaining supported work.",
                    "recent_errors": [],
                    "attempted_actions": [],
                }
            elif primary_index == 2:
                name = "set_output"
                arguments = {
                    "sink_name": "report",
                    "plugin": "json",
                    "options": {
                        "path": "outputs/report.jsonl",
                        "schema": {"mode": "observed"},
                        "format": "jsonl",
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            if primary_index < 4:
                content = None
                tool_calls = [
                    {
                        "id": f"supported-work-{primary_index}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            else:
                content = candidate
        else:
            schema_name = kwargs["response_format"]["json_schema"]["name"]
            if schema_name == "advisor_abandonment_response":
                content = json.dumps(
                    {
                        "decision": "CONTINUE_WITH_LIMITATION" if primary_index == 1 else "STOP_CONFIRMED",
                        "reason": "Operation B is unavailable; save the supported output first."
                        if primary_index == 1
                        else "The output is saved; all remaining requested work requires unavailable operation B.",
                        "next_action": "Call set_output to save the report output." if primary_index == 1 else None,
                        "question": None,
                    }
                )
            else:
                end = schema_name == "advisor_end_response"
                content = json.dumps(
                    {
                        "verdict": "FLAGGED" if end else "CLEAN",
                        "category": "request_not_met" if end else "other",
                        "steps": ["report"] if end else [],
                        "findings": "Operation B remains unavailable; the partial graph is not executable." if end else "",
                        "note": "The saved draft remains incomplete." if end else None,
                        **(
                            {"publication_verdict": "WITHHOLD" if publication_case == "overclaim" or changed_decision else "ADMIT"}
                            if end
                            else {}
                        ),
                    }
                )
                if end:
                    assert any(
                        isinstance(message["content"], str) and end_inputs[-1].provider_review_context_json() in message["content"]
                        for message in kwargs["messages"]
                    )
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        )

    monkeypatch.setattr(TurnEvidence, "accept_abandonment", observe_accept)
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)
    monkeypatch.setattr(litellm, "acompletion", complete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(
            client, app, path=f"/api/sessions/{session.id}/messages", body=message_body(request, state_id=baseline.id)
        )
    public = settled.result()["message"]["content"]
    assert primary_index == 4
    assert end_inputs and end_verdicts
    assert all(verdict.ok and verdict.blocking for verdict in end_verdicts)
    states = await sessions.get_state_versions(session.id)
    assert len(states) == 3  # Prior draft, current tool checkpoint and route snapshot.
    assert all(not state.is_valid for state in states)
    current_states = [state for state in states if state.id != baseline.id]
    assert len(current_states) == 2 and current_states[0].outputs == current_states[1].outputs
    assert len(current_states[0].outputs) == 2
    if publication_case == "truthful-partial":
        assert candidate in public
        assert end_verdicts[-1].publication_verdict == "ADMIT"
    else:
        assert candidate not in public
    if publication_case == "changed-decision":
        assert changed_decision


def _guard_checks(rows):
    return [
        envelope["check"]
        for row in rows
        if row.role == "audit"
        for envelope in (row.tool_calls or ())
        if envelope["_kind"] == "prompt_guard_audit"
    ]


@pytest.fixture
def real_route_app(tmp_path):
    app, sessions = _make_app(tmp_path, quota_enabled=True)
    try:
        yield app, sessions
    finally:
        app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_message", "primary_replies", "publication_verdicts", "create_blob"),
    [
        *[
            pytest.param(request, ("I can explain that without changing anything.",), ("ADMIT",), False, id=f"ordinary-{index}")
            for index, request in enumerate(_ORDINARY_REQUESTS)
        ],
        pytest.param(_BUILD_REQUEST, (_FALSE_COMPLETION, _TRUTHFUL_CORRECTION), ("WITHHOLD", "ADMIT"), False, id="false-claim-corrected"),
        pytest.param(_BUILD_REQUEST, (_FALSE_COMPLETION, _FALSE_COMPLETION), ("WITHHOLD", "WITHHOLD"), False, id="false-claim-stalled"),
        pytest.param(
            _BLOB_REQUEST, (_FALSE_COMPLETION, _BLOB_CORRECTION), ("WITHHOLD", "ADMIT"), True, id="saved-blob-is-not-bound-source"
        ),
    ],
)
async def test_actual_end_semantics_and_each_physical_transition(
    monkeypatch, real_route_app, user_message, primary_replies, publication_verdicts, create_blob
):
    app, sessions = real_route_app
    primary_guard = PromptGuardSettings(endpoint="https://primary-shield.example.test", subscription_key="offline-primary-shield")
    advisor_guard = PromptGuardSettings(endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield")
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4.1-mini",
            "composer_endpoint_base_url": "https://offline-model.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-primary-key"),  # secret-scan: allow-this-line
            "composer_advisor_model": "openai/gpt-4.1",
            "composer_advisor_endpoint_base_url": "https://offline-model.example.test/v1",
            "composer_advisor_endpoint_api_key": SecretStr("offline-advisor-key"),  # secret-scan: allow-this-line
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": primary_guard,
            "composer_advisor_prompt_guard": advisor_guard,
        }
    )
    shield_requests = []

    def shield_response(http_request):
        assert http_request.url.host in {"primary-shield.example.test", "advisor-shield.example.test"}
        payload = json.loads(http_request.content)
        assert payload["userPrompt"] == user_message
        shield_requests.append((http_request.url.host, payload))
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def mount(settings):
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(), app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    session = await sessions.create_session("alice", "Existing authored session", "local")
    # Correction and saved-blob scenarios exercise the ordinary authoring loop
    # over an existing output-only draft. Ordinary rootless questions use the
    # same Primary/END owners over their unchanged empty graph.
    baseline = await _seed_output_only_draft(app, sessions, session.id) if len(primary_replies) > 1 else None
    engine = app.state.session_engine
    authoring_family = ProviderInvocationFamily.PRIMARY
    expected_families = [family for _reply in primary_replies for family in (authoring_family, ProviderInvocationFamily.ADVISOR)]
    initial_calls = int(create_blob)
    if create_blob:
        expected_families.insert(0, ProviderInvocationFamily.PRIMARY)
    minted = []
    sdk_trace = []
    reviews = []
    review_inputs = []
    review_state_ids = []
    compose_results = []
    original_mint = ProviderInvocationOwner.mint
    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint
    original_compose = composer.compose
    # A forwarding observer must wrap the real review owner, never an inherited
    # autouse CLEAN fixture or a fabricated verdict carrier.
    assert original_checkpoint.__func__ is AdvisorCheckpointOwner._run_advisor_checkpoint
    assert original_checkpoint.__module__ == "elspeth.web.composer.advisor_checkpoint"

    def observe_mint(owner, family):
        custody = original_mint(owner, family)
        assert owner.service is sessions
        minted.append((owner, family, custody))
        return custody

    async def observe_checkpoint(*args, **kwargs):
        assert kwargs["phase"] == "end"
        assert kwargs["user_message"] == user_message
        review = kwargs["publication_review"]
        review_inputs.append(review)
        with engine.connect() as connection:
            saved = (
                connection.execute(
                    select(composition_states_table.c.id)
                    .where(composition_states_table.c.session_id == str(session.id))
                    .order_by(composition_states_table.c.version)
                )
                .scalars()
                .all()
            )
        review_state_ids.append(tuple(state_id for state_id in saved if baseline is None or state_id != str(baseline.id)))
        verdict = await original_checkpoint(*args, **kwargs)
        assert verdict.publication_binding == review.binding
        reviews.append((review, verdict))
        return verdict

    async def observe_compose(*args, **kwargs):
        result = await original_compose(*args, **kwargs)
        compose_results.append(result)
        return result

    async def complete(**kwargs):
        index = len(sdk_trace)
        assert index < len(expected_families), "No extra PRIMARY, ADVISOR, PLANNER or TITLE dispatch is allowed"
        _owner, family, custody = minted[-1]
        assert family is expected_families[index]
        assert custody.attempt is not None
        with engine.connect() as connection:
            admitted = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == custody.attempt.attempt_id)
            ).one()
            current_rows = connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == str(session.id))
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        assert admitted.settled_at is None and admitted.operation_id == custody.context.fence.operation_id
        checks = _guard_checks(current_rows)
        expected_role = "advisor" if family is ProviderInvocationFamily.ADVISOR else "composer"
        assert len(checks) == index + 1
        assert checks[-1]["role"] == expected_role and checks[-1]["disposition"] == "allow"
        sdk_trace.append((family, custody.attempt.attempt_id, deepcopy(kwargs)))
        pass_index = (index - initial_calls) // 2
        tool_calls = None
        if family is authoring_family:
            assert kwargs["model"] == app.state.settings.composer_model
            tool_names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "set_pipeline" in tool_names and "emit_pipeline_proposal" not in tool_names
            assert "response_format" not in kwargs
            if create_blob and index == 0:
                content = None
                tool_calls = [
                    {
                        "id": "sample-csv-blob",
                        "type": "function",
                        "function": {
                            "name": "create_blob",
                            "arguments": json.dumps({"filename": "example.csv", "mime_type": "text/csv", "content": _BLOB_CONTENT}),
                        },
                    }
                ]
            else:
                content = primary_replies[pass_index]
        else:
            assert kwargs["model"] == app.state.settings.composer_advisor_model
            assert not kwargs.get("tools")
            schema = kwargs["response_format"]["json_schema"]
            assert schema["name"] == "advisor_end_response"
            assert "publication_verdict" in schema["schema"]["required"]
            review = review_inputs[-1]
            supplied = parse_advisor_publication_review(kwargs["messages"][1]["content"])
            assert supplied["current_request"] == user_message
            assert supplied["candidate_reply"] == primary_replies[pass_index]
            assert supplied["saved_effects"] == json.loads(review.provider_payload_json)["saved_effects"]
            guard_documents = "\n".join(
                document for host, payload in shield_requests if host == "advisor-shield.example.test" for document in payload["documents"]
            )
            assert primary_replies[pass_index] in guard_documents
            assert f'"saved_revision_count": {len(review_state_ids[-1])}' in guard_documents
            content = json.dumps(
                {
                    "verdict": "CLEAN",
                    "category": "other",
                    "steps": [],
                    "findings": "The candidate claims a configured source but the saved graph has no bound source."
                    if publication_verdicts[pass_index] == "WITHHOLD"
                    else "",
                    "note": None,
                    "publication_verdict": publication_verdicts[pass_index],
                }
            )
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        )

    monkeypatch.setattr(ProviderInvocationOwner, "mint", observe_mint)
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)
    monkeypatch.setattr(composer, "compose", observe_compose)
    monkeypatch.setattr(litellm, "acompletion", complete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(
            client,
            app,
            path=f"/api/sessions/{session.id}/messages",
            body=message_body(user_message, state_id=baseline.id if baseline is not None else None),
        )
    public = settled.result()["message"]["content"]
    assert [family for family, _attempt, _kwargs in sdk_trace] == expected_families
    assert [family for _owner, family, _custody in minted] == expected_families
    assert len(compose_results) == 1
    result = compose_results[0]
    assert result.repair_turns_used == 0, "An END prose repair is not a lexical correctness repair"
    assert not result.state.sources and not result.state.nodes and not result.state.edges
    if baseline is not None:
        assert result.state.to_dict()["outputs"] == deep_thaw(baseline.outputs)
    else:
        assert not result.state.outputs
    if create_blob:
        assert len(result.tool_invocations) == 1
        assert result.tool_invocations[0].tool_name == "create_blob" and result.tool_invocations[0].status.value == "success"
    else:
        assert not result.tool_invocations
    assert not isinstance(result.advisor_gate_decision, AdvisorGatePassed)
    if result.runtime_preflight is not None:
        assert all(check.name != "state_exists" for check in result.runtime_preflight.checks)
    assert "pipeline is still empty" not in public
    assert "did not complete a valid build" not in public
    with engine.connect() as connection:
        rows = connection.execute(
            select(chat_messages_table)
            .where(chat_messages_table.c.session_id == str(session.id))
            .order_by(chat_messages_table.c.sequence_no)
        ).all()
        attempts = connection.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session.id))
        ).all()
        ledger = connection.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session.id))).all()
        states = connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(session.id))).all()
        blobs = connection.execute(select(blobs_table).where(blobs_table.c.session_id == str(session.id))).all()
        fence = connection.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
        ).one()
    call_audits = [
        envelope["call"]
        for row in rows
        if row.role == "audit"
        for envelope in (row.tool_calls or ())
        if envelope["_kind"] == "llm_call_audit"
    ]
    passes = [
        envelope["pass"]
        for row in rows
        if row.role == "audit"
        for envelope in (row.tool_calls or ())
        if envelope["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    if create_blob:
        assert len(blobs) == 1 and blobs[0].filename == "example.csv"
        assert Path(blobs[0].storage_path).read_text() == _BLOB_CONTENT
        tool_rows = [row for row in rows if row.role == "tool"]
        assert len(tool_rows) == 1 and tool_rows[0].tool_call_id == "sample-csv-blob"
        assert json.loads(tool_rows[0].content)["success"] is True
    else:
        assert not blobs and not any(row.role == "tool" for row in rows)
    if baseline is None:
        assert not states
    else:
        state_versions = await sessions.get_state_versions(session.id)
        assert state_versions and all(state.outputs == baseline.outputs and not state.sources for state in state_versions)
    assert len(attempts) == len(ledger) == len(call_audits) == len(sdk_trace)
    assert {row.attempt_id for row in attempts} == {attempt for _family, attempt, _kwargs in sdk_trace}
    assert {call["call_id"] for call in call_audits} == {row.attempt_id for row in attempts}
    assert all(call["status"] == "success" for call in call_audits)
    assert all(row.settled_at is not None and row.ledger_entry_id in {entry.entry_id for entry in ledger} for row in attempts)
    assert all(row.prompt_tokens == row.completion_tokens == 5 for row in ledger)
    assert fence.released_at is not None
    assert len({id(owner.required_work.coordinator) for owner, _family, _custody in minted}) == 1
    minted[0][0].required_work.coordinator.assert_completed()
    assert [(check["role"], check["disposition"]) for check in _guard_checks(rows)] == [
        ("advisor" if family is ProviderInvocationFamily.ADVISOR else "composer", "allow") for family in expected_families
    ]
    assert shield_requests and any(host == "advisor-shield.example.test" for host, _payload in shield_requests)
    assert [verdict.publication_verdict for _review, verdict in reviews] == list(publication_verdicts)
    assert [(entry["publication_verdict"], entry["publication_binding"]) for entry in passes] == [
        (verdict.publication_verdict, review.binding) for review, verdict in reviews
    ]
    assert len({review.binding for review, _verdict in reviews}) == len(reviews)
    for index, (review, _verdict) in enumerate(reviews):
        evidence = json.loads(review.payload_json)
        assert evidence["candidate_reply"] == primary_replies[index] and evidence["current_request"] == user_message
        effects = evidence["saved_effects"]
        assert effects["verification"] == "verified" and effects["saved_revision_count"] == len(review_state_ids[index])
        expected_state_id = review_state_ids[index][-1] if review_state_ids[index] else str(baseline.id) if baseline is not None else None
        assert effects["latest_committed_state_id"] == effects["latest_verified_state_id"] == expected_state_id
        assert effects["net_changed"] is False and effects["changed_fields"] == []
        assert effects["initial_authored_state_hash"] == effects["latest_verified_authored_state_hash"]
        assert evidence["inspection"]["currently_available"] == evidence["inspection"]["historically_delivered"] == []
        assert evidence["inspection"]["current_candidate_call"][0] == sdk_trace[initial_calls + index * 2][1]
    for family, _attempt, kwargs in sdk_trace:
        assert not any(
            message["role"] == "user" and message["content"].startswith("[composer-system] No tool has run this turn")
            for message in kwargs["messages"]
            if isinstance(message["content"], str)
        )
        if family is ProviderInvocationFamily.PRIMARY:
            assert any(message["role"] == "user" and message["content"] == user_message for message in kwargs["messages"])
    visible = [row for row in await sessions.get_messages(session.id, limit=None) if row.role != "audit"]
    if publication_verdicts[-1] == "ADMIT":
        assert public.startswith(primary_replies[-1])
        assert _composer_history_content(visible[-1]) == primary_replies[-1]
    else:
        assert "withheld" in public.lower()
    assert _FALSE_COMPLETION not in public
    assert _FALSE_COMPLETION not in json.dumps([row.content for row in visible])
    if len(primary_replies) == 2:
        expected_origin = "repair_gate_superseded" if publication_verdicts[-1] == "ADMIT" else "advisor_publication_withheld"
        assert any(
            envelope["_kind"] == COMPOSER_WITHHELD_REPLY_KIND and envelope["origin"] == expected_origin and row.content == _FALSE_COMPLETION
            for row in rows
            if row.role == "audit"
            for envelope in (row.tool_calls or ())
        ), "The withheld candidate remains private audit evidence"
        repaired_history = sdk_trace[initial_calls + 2][2]["messages"]
        assert any(message["role"] == "assistant" and message["content"] == _FALSE_COMPLETION for message in repaired_history)
        assert "configured source but the saved graph has no bound source" in json.dumps(repaired_history)

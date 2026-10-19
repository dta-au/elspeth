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

from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_decision import AdvisorGatePassed
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
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
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _TestSnapshotFactory

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
    engine = app.state.session_engine
    expected_families = [
        family for _reply in primary_replies for family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR)
    ]
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
        review_state_ids.append(tuple(saved))
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
        expected_role = "composer" if family is ProviderInvocationFamily.PRIMARY else "advisor"
        assert len(checks) == index + 1
        assert checks[-1]["role"] == expected_role and checks[-1]["disposition"] == "allow"
        sdk_trace.append((family, custody.attempt.attempt_id, deepcopy(kwargs)))
        pass_index = (index - initial_calls) // 2
        tool_calls = None
        if family is ProviderInvocationFamily.PRIMARY:
            assert kwargs["model"] == app.state.settings.composer_model
            assert "set_pipeline" in {tool["function"]["name"] for tool in kwargs["tools"]}
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
            assert any(
                isinstance(message["content"], str) and review.provider_payload_json in message["content"] for message in kwargs["messages"]
            ), "The actual END SDK request must contain the complete current candidate/request/effect packet"
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
        settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/messages", body=message_body(user_message))
    public = settled.result()["message"]["content"]
    assert [family for family, _attempt, _kwargs in sdk_trace] == expected_families
    assert [family for _owner, family, _custody in minted] == expected_families
    assert len(compose_results) == 1
    result = compose_results[0]
    assert result.repair_turns_used == 0, "An END prose repair is not a lexical correctness repair"
    assert not result.state.sources and not result.state.nodes and not result.state.edges and not result.state.outputs
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
        assert not blobs and not states and not any(row.role == "tool" for row in rows)
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
        ("composer" if family is ProviderInvocationFamily.PRIMARY else "advisor", "allow") for family in expected_families
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
        expected_state_id = review_state_ids[index][-1] if review_state_ids[index] else None
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

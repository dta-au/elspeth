"""Current public Composer outcomes use real admission, custody and settlement.

The removed compose/freeform route's repair-code translation, legacy cost loop,
retry and deadline fixtures are retired: those interfaces are not supported.
Current equivalents below enter the actual HTTP route and continuation worker;
only the physical provider replies are scripted. No legacy app adapter remains.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse
from openai import OpenAIError
from sqlalchemy import select

from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.interpretation_state import approved_prompt_artifact_hash_from_options
from elspeth.web.sessions.continuation_primary_sdk_terminal import read_v4_primary_sdk_terminal_verified_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composition_proposals_table as proposals
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import interpretation_events_table, proposal_events_table
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.pending_interpretation import _interpretation_hash_domain_v2
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.continuation_app import (
    PRIMARY_MODEL,
    ScriptedSDK,
    assistant_contents,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    sdk_reply,
    semantic_primary_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)
from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline

_REQUESTS = (
    "Explain how a pipeline works, without changing anything.",
    "Build the pipeline. Actually, do not.",
    "First explain the pipeline, then build it.",
)
_PRIVATE_FAILURE = "provider-private-error-marker-34719"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", _REQUESTS)
@pytest.mark.parametrize("observation", ("provider_failure", "provider_timeout"))
async def test_current_provider_failure_has_safe_public_terminal_and_original_accounting(tmp_path, monkeypatch, message, observation):
    """Wording cannot bypass the SDK or turn an unknown charge into zero."""
    failure = TimeoutError(_PRIVATE_FAILURE) if observation == "provider_timeout" else OpenAIError(_PRIVATE_FAILURE)
    sdk = ScriptedSDK([("raise", failure)])
    install_sdk(monkeypatch, sdk)
    operation = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch) as harness:
        result = await submit_and_settle(harness, {"content": message, "operation_id": operation, "state_id": None})
        assert result.status == "failed", result
        assert sdk.roles == ["raise"] and not sdk.steps
        assert sdk.requests[0]["model"] == PRIMARY_MODEL and sdk.requests[0]["tools"]
        assert message in json.dumps(sdk.requests[0]["messages"], ensure_ascii=False)
        assert assistant_contents(harness.app, harness.session_id) == []
        assert not any(row.owner == "tool_dispatch" for row in effects(harness.app, operation))
        saved = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation)
        assert saved is not None and saved.status == "failed" and saved.result_json is not None
        public = json.loads(saved.result_json)
        assert public["http_status"] == 502 and public["error_type"] == "provider_sdk_terminal"
        assert public["body"]["observation"] == observation
        assert public["body"]["retryable"] is False
        assert public["body"]["cost"] == {"status": "unknown", "amount": None}
        assert _PRIVATE_FAILURE not in saved.result_json and _PRIVATE_FAILURE not in result.model_dump_json()
        with harness.app.state.session_engine.connect() as conn:
            assert conn.execute(select(states.c.id).where(states.c.session_id == str(harness.session_id))).all() == []
            attempts = conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all()
            entries = conn.execute(select(ledger).where(ledger.c.session_id == str(harness.session_id))).all()
            hold = conn.execute(select(holds).where(holds.c.operation_id == operation)).one()
            cold = read_v4_primary_sdk_terminal_verified_on_connection(
                conn, session_id=harness.session_id, operation_id=operation, effect_key=hold.effect_key
            )
        assert len(attempts) == len(entries) == 1
        assert attempts[0].settled_at is not None and attempts[0].ledger_entry_id == entries[0].entry_id
        assert entries[0].prompt_tokens is None and entries[0].completion_tokens is None
        assert hold.state == "consumed" and cold is not None and cold.hold_state == "consumed"
        assert cold.receipt.observation == observation and cold.receipt.retryable is False
        assert cold.receipt.sdk_entered and not cold.receipt.sdk_result_observed
        assert cold.receipt.cost.status == "unknown" and cold.receipt.cost.amount is None
        assert cold.receipt.usage.status == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", _REQUESTS)
async def test_current_provider_decline_is_reviewed_before_publication_without_graph_mutation(tmp_path, monkeypatch, message):
    """The provider may decline any wording; the server never authors a graph."""
    candidate = "I need the input column names before I can build this pipeline."
    operation = str(uuid4())
    sdk = ScriptedSDK(
        [
            ("primary", (candidate, None)),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:

        def review(request):
            # This runs at the real END SDK boundary, before public settlement.
            assert assistant_contents(harness.app, harness.session_id) == []
            assert candidate in json.dumps(request["messages"], ensure_ascii=False)
            assert not any(row.owner == "tool_dispatch" for row in effects(harness.app, operation))
            return end_admit("The provider requests necessary input and changes no pipeline.")

        sdk.steps[2] = ("end", review)
        result = await submit_and_settle(harness, {"content": message, "operation_id": operation, "state_id": None})
        assert result.status == "completed", result.error
        assert sdk.roles == ["primary", "semantic", "end", "semantic"] and not sdk.steps
        assert message in json.dumps(sdk.requests[0]["messages"], ensure_ascii=False)
        assert assistant_contents(harness.app, harness.session_id) == [candidate]
        assert not any(row.owner == "tool_dispatch" for row in effects(harness.app, operation))
        with harness.app.state.session_engine.connect() as conn:
            assert conn.execute(select(states.c.id).where(states.c.session_id == str(harness.session_id))).all() == []
            attempts = conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all()
            entries = conn.execute(select(ledger).where(ledger.c.session_id == str(harness.session_id))).all()
        assert len(attempts) == len(entries) == len(sdk.requests) == 4
        assert all(attempt.settled_at is not None for attempt in attempts)
        assert {attempt.ledger_entry_id for attempt in attempts} == {entry.entry_id for entry in entries}


def _accounting(harness):
    """Read the actual current physical prefix and its persisted audit records."""
    with harness.app.state.session_engine.connect() as conn:
        attempts = conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(harness.session_id))).all()
        rows = conn.execute(select(messages).where(messages.c.session_id == str(harness.session_id), messages.c.role == "audit")).all()
    calls = [row.tool_calls[0]["call"] for row in rows if row.tool_calls and row.tool_calls[0].get("_kind") == "llm_call_audit"]
    assert len(attempts) == len(entries) == len(calls)
    assert all(attempt.settled_at is not None for attempt in attempts)
    assert {attempt.ledger_entry_id for attempt in attempts} == {entry.entry_id for entry in entries}
    assert len({call["call_id"] for call in calls}) == len(calls)
    return calls


def _cost_reply(reply, *, kind, request_id):
    """An external SDK reply with authoritative paid or malformed cost data."""
    # Deliberately corrupt the external SDK DTO after its construction. Passing
    # this string into Usage's constructor would fail inside the fake SDK and
    # exercise SDK-exception settlement instead of received-response admission.
    usage = reply.usage.model_copy(update={"cost": "malformed-explicit-cost" if kind == "unknown" else 6.0})
    return reply.model_copy(update={"id": request_id, "usage": usage})


def _require_cost_refusal(harness, result, operation, kind):
    assert result.status == "failed", result
    saved = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation)
    assert saved is not None and saved.result_json is not None
    public = json.loads(saved.result_json)
    assert public["http_status"] == (503 if kind == "unknown" else 422)
    assert public["error_type"] == ("cost_unavailable" if kind == "unknown" else "provider_policy_refused")
    assert "malformed-explicit-cost" not in saved.result_json


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("primary", "semantic", "end"))
@pytest.mark.parametrize("kind", ("unknown", "over-limit"))
async def test_current_paid_role_refusal_precedes_publication_and_keeps_actual_ledger(tmp_path, monkeypatch, role, kind):
    candidate = "Please provide the input column names."
    steps = [("primary", (candidate, None)), ("semantic", semantic_review_response), ("end", end_admit())]
    steps = steps[: [step[0] for step in steps].index(role) + 1]
    sdk = ScriptedSDK(steps)
    operation = str(uuid4())

    async def completion(**kwargs):
        reply = await sdk(**kwargs)
        return _cost_reply(reply, kind=kind, request_id="current-refused-paid-response") if sdk.roles[-1] == role else reply

    install_sdk(monkeypatch, completion)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        result = await submit_and_settle(harness, {"content": "Explain the required input before building.", "operation_id": operation})
        _require_cost_refusal(harness, result, operation, kind)
        assert sdk.roles == [step[0] for step in steps] and not sdk.steps
        calls = _accounting(harness)
        assert len(calls) == len(steps)
        refused = next(call for call in calls if call["provider_request_id"] == "current-refused-paid-response")
        assert refused["provider_cost"] == (None if kind == "unknown" else 6.0)
        if kind == "unknown":
            assert refused["provider_cost_source"] == "not_available"
        assert assistant_contents(harness.app, harness.session_id) == []
        assert not any(row.owner == "tool_dispatch" for row in effects(harness.app, operation))
        with harness.app.state.session_engine.connect() as conn:
            assert conn.execute(select(states.c.id).where(states.c.session_id == str(harness.session_id))).all() == []
            assert conn.execute(select(proposals.c.id).where(proposals.c.session_id == str(harness.session_id))).all() == []


@pytest.mark.asyncio
async def test_current_end_shares_cumulative_paid_allowance_with_primary_and_semantic_advisor(tmp_path, monkeypatch):
    """Each reply fits alone; the original request's combined charge does not."""
    cap = Decimal("0.025")
    operation = str(uuid4())
    sdk = ScriptedSDK(
        [
            ("primary", ("Please provide the input column names.", None)),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch, composer_planner_max_cumulative_provider_cost=cap) as harness:
        result = await submit_and_settle(harness, {"content": "Explain the required input before building.", "operation_id": operation})
        _require_cost_refusal(harness, result, operation, "over-limit")
        assert sdk.roles == ["primary", "semantic", "end"] and not sdk.steps
        calls = _accounting(harness)
        charges = [Decimal(str(call["provider_cost"])) for call in calls]
        assert len(charges) == 3 and all(Decimal(0) < charge <= cap for charge in charges)
        assert sum(charges) == Decimal("0.03") > cap
        assert assistant_contents(harness.app, harness.session_id) == []
        assert not any(row.owner == "tool_dispatch" for row in effects(harness.app, operation))


@pytest.mark.asyncio
async def test_current_unknown_primary_cost_blocks_received_pipeline_before_any_effect(tmp_path, monkeypatch):
    operation = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch) as harness:

        def author(app):
            return "Build the requested pipeline.", [
                tool_call(app, "unpaid-pipeline", "set_pipeline", _pipeline(tmp_path, str(harness.session_id)))
            ]

        sdk = ScriptedSDK([("primary", author)], app_ref=[harness.app])

        async def completion(**kwargs):
            return _cost_reply(await sdk(**kwargs), kind="unknown", request_id="unpaid-pipeline-response")

        install_sdk(monkeypatch, completion)
        result = await submit_and_settle(harness, {"content": "Build a CSV pipeline.", "operation_id": operation})
        _require_cost_refusal(harness, result, operation, "unknown")
        assert sdk.roles == ["primary"] and not sdk.steps
        assert len(_accounting(harness)) == 1
        assert not any(row.owner == "tool_dispatch" for row in effects(harness.app, operation))
        with harness.app.state.session_engine.connect() as conn:
            assert conn.execute(select(states.c.id).where(states.c.session_id == str(harness.session_id))).all() == []
            assert conn.execute(select(proposals.c.id).where(proposals.c.session_id == str(harness.session_id))).all() == []
            assert (
                conn.execute(select(messages.c.id).where(messages.c.session_id == str(harness.session_id), messages.c.role == "tool")).all()
                == []
            )


@pytest.mark.asyncio
async def test_current_catalog_recovery_settles_paid_response_before_reviewed_publication(tmp_path, monkeypatch):
    candidate = "Please provide the input column names before I build the pipeline."
    sdk = ScriptedSDK(
        [
            ("primary", (candidate, None)),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    )

    async def completion(**kwargs):
        reply = await sdk(**kwargs)
        if sdk.roles[-1] != "primary":
            return reply
        return ModelResponse(
            id="current-catalog-primary",
            model="private-azure-deployment-2026-07-09",
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            choices=[{"index": 0, "message": {"role": "assistant", "content": candidate}, "finish_reason": "stop"}],
        )

    install_sdk(monkeypatch, completion)
    async with continuation_app(tmp_path, monkeypatch, composer_pricing_model="openai/gpt-4o-2024-08-06") as harness:
        result = await submit_and_settle(harness, {"content": "What inputs are needed?", "operation_id": str(uuid4())})
        assert result.status == "completed", result.error
        assert sdk.roles == ["primary", "semantic", "end", "semantic"] and not sdk.steps
        calls = _accounting(harness)
        assert len(calls) == 4
        recovered = next(call for call in calls if call["provider_request_id"] == "current-catalog-primary")
        assert recovered["provider_cost"] is not None and recovered["provider_cost"] > 0
        assert recovered["provider_cost"] == pytest.approx(0.00045)
        assert recovered["provider_cost_source"] == "litellm.cost_per_token"
        assert assistant_contents(harness.app, harness.session_id) == [candidate]


@pytest.mark.asyncio
@pytest.mark.parametrize("refused_role", ("later_primary", "manual_advisor"))
async def test_current_paid_refusal_preserves_real_pipeline_prefix_and_stops_later_mutation(tmp_path, monkeypatch, refused_role):
    """Current tool/P4 custody retains paid saved work when later cost is unknown."""
    operation = str(uuid4())
    requests = []
    saved_before_refusal = []
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sessions = harness.app.state.session_service
        await sessions.update_composer_preferences(harness.session_id, trust_mode="auto_commit", density_default="high", actor="test")
        blob_dir = tmp_path / "blobs" / str(harness.session_id)
        blob_dir.mkdir(parents=True, exist_ok=True)
        (blob_dir / "input.csv").write_text("name\nAda\n", encoding="utf-8")
        (tmp_path / "outputs" / str(harness.session_id)).mkdir(parents=True, exist_ok=True)

        def author(app):
            calls = [tool_call(app, "paid-prefix-saved", "set_pipeline", _pipeline(tmp_path, str(harness.session_id)))]
            if refused_role == "manual_advisor":
                calls.extend(
                    [
                        tool_call(
                            app,
                            "paid-manual-refused",
                            "request_advisor_hint",
                            {
                                "trigger": "proactive_security_safety",
                                "problem_summary": "Review the supplied pipeline before continuing.",
                                "recent_errors": [],
                                "attempted_actions": ["set_pipeline"],
                            },
                        ),
                        tool_call(app, "paid-must-not-run", "set_metadata", {"patch": {"name": "refused later mutation"}}),
                    ]
                )
            return "Building the requested pipeline.", calls

        def second_primary(app):
            with app.state.session_engine.connect() as conn:
                rows = conn.execute(select(states).where(states.c.session_id == str(harness.session_id))).all()
            assert len(rows) == 1
            saved_before_refusal.extend(rows)
            return "Changing the saved pipeline.", [
                tool_call(app, "paid-must-not-run", "set_metadata", {"patch": {"name": "refused later mutation"}})
            ]

        steps = [("primary", author)]
        if refused_role == "later_primary":
            steps.extend(
                [
                    ("early", {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The draft is coherent.", "note": None}),
                    ("semantic", semantic_primary_response),
                    ("primary", second_primary),
                ]
            )
        sdk = ScriptedSDK(steps, app_ref=[harness.app])

        async def completion(**kwargs):
            requests.append(kwargs)
            if refused_role == "manual_advisor" and len(requests) == 2:
                assert kwargs["model"] == harness.app.state.settings.composer_advisor_model
                return _cost_reply(
                    sdk_reply("Review the supplied pipeline fields.", model=kwargs["model"]),
                    kind="unknown",
                    request_id="prefix-cost-refused",
                )
            reply = await sdk(**kwargs)
            if refused_role == "later_primary" and len(requests) == 4:
                return _cost_reply(reply, kind="unknown", request_id="prefix-cost-refused")
            return reply

        install_sdk(monkeypatch, completion)
        result = await submit_and_settle(
            harness, {"content": "Build a CSV to JSONL pipeline and review the draft.", "operation_id": operation}
        )
        _require_cost_refusal(harness, result, operation, "unknown")
        assert len(requests) == (4 if refused_role == "later_primary" else 2) and not sdk.steps
        calls = _accounting(harness)
        assert len(calls) == len(requests)
        refused = next(call for call in calls if call["provider_request_id"] == "prefix-cost-refused")
        assert refused["provider_cost"] is None and refused["provider_cost_source"] == "not_available"
        with harness.app.state.session_engine.connect() as conn:
            saved = conn.execute(select(states).where(states.c.session_id == str(harness.session_id))).all()
            assert len(saved) == 1
            assert "refused later mutation" not in json.dumps(dict(saved[0]._mapping), default=str)
            assert conn.execute(select(proposals.c.id).where(proposals.c.session_id == str(harness.session_id))).all() == []
            tool_rows = conn.execute(
                select(messages).where(messages.c.session_id == str(harness.session_id), messages.c.role == "tool")
            ).all()
        if refused_role == "later_primary":
            assert saved == saved_before_refusal
        assert any(row.tool_call_id == "paid-prefix-saved" for row in tool_rows)
        assert all(row.tool_call_id != "paid-must-not-run" for row in tool_rows)
        assert not any("refused later mutation" in content for content in assistant_contents(harness.app, harness.session_id))
        async with harness.client() as client:
            current = await client.get(f"/api/sessions/{harness.session_id}/state")
        assert current.status_code == 200 and current.json()["id"] == saved[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", (False, True))
async def test_current_recompose_sdk_failure_preserves_selected_state_and_user_request(tmp_path, monkeypatch, timeout):
    sdk = ScriptedSDK([("raise", TimeoutError(_PRIVATE_FAILURE) if timeout else OpenAIError(_PRIVATE_FAILURE))])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        seed = service.session_operation_authority.acquire(
            session_id=harness.session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="recompose-fixture",
            lease_seconds=30,
        )
        try:
            state = await service.save_composition_state(
                harness.session_id,
                CompositionStateData(metadata_={"name": "Original draft", "description": ""}),
                provenance="session_seed",
                session_operation_context=seed,
            )
            user = await service.add_message(
                harness.session_id,
                "user",
                "Explain the selected original draft.",
                writer_principal="route_user_message",
                session_operation_context=seed,
            )
        finally:
            service.session_operation_authority.release(seed)
        with harness.app.state.session_engine.connect() as conn:
            before = conn.execute(select(states).where(states.c.session_id == str(harness.session_id))).all()
        operation = str(uuid4())
        result = await submit_and_settle(
            harness,
            {
                "operation_id": operation,
                "state_id": str(state.id),
                "expected_user_message_id": str(user.id),
            },
            route="recompose",
        )
        assert result.status == "failed" and result.error is not None
        assert result.error.http_status == 502 and result.error.error_type == "provider_sdk_terminal"
        assert result.error.body["observation"] == ("provider_timeout" if timeout else "provider_failure")
        assert result.error.body["retryable"] is False and result.error.body["cost"]["status"] == "unknown"
        assert _PRIVATE_FAILURE not in result.model_dump_json()
        assert sdk.roles == ["raise"] and not sdk.steps
        assert "Explain the selected original draft." in json.dumps(sdk.requests[0]["messages"])
        assert len(_accounting(harness)) == 1
        with harness.app.state.session_engine.connect() as conn:
            assert conn.execute(select(states).where(states.c.session_id == str(harness.session_id))).all() == before
            users = conn.execute(
                select(messages.c.id).where(messages.c.session_id == str(harness.session_id), messages.c.role == "user")
            ).all()
        assert users == [(str(user.id),)]
        assert assistant_contents(harness.app, harness.session_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("has_base", (False, True), ids=("absent-base", "saved-base"))
async def test_current_authored_interpretations_publish_atomically_on_acceptance_and_resolve(tmp_path, monkeypatch, has_base):
    """Real provider-authored requirements survive proposal acceptance and amendment."""

    def no_row_provider(*args, **kwargs):
        raise AssertionError("Authoring must not execute a row LLM")

    monkeypatch.setattr(litellm, "completion", no_row_provider)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    profiles = {
        "offline-review": {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0", "region_name": "us-east-1"}
    }
    async with (
        continuation_app(
            tmp_path, monkeypatch, llm_profiles=profiles, plugin_allowlist=("transform:passthrough", "transform:llm")
        ) as harness,
        harness.client() as client,
    ):
        sid = str(harness.session_id)
        service = harness.app.state.session_service
        preference_path = f"/api/sessions/{sid}/composer/preferences"
        prior = await client.get(preference_path)
        assert prior.status_code == 200 and prior.json()["trust_mode"] == "auto_commit"
        changed = await client.patch(
            preference_path, json={"trust_mode": "explicit_approve", "density_default": prior.json()["density_default"]}
        )
        assert changed.status_code == 200 and changed.json()["trust_mode"] == "explicit_approve"
        directory = tmp_path / "blobs" / sid
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "input.csv").write_text("name\nAda\n", encoding="utf-8")
        (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
        base = None
        if has_base:
            seed = service.session_operation_authority.acquire(
                session_id=harness.session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id="interpretation-fixture",
                lease_seconds=30,
            )
            try:
                base = await service.save_composition_state(
                    harness.session_id,
                    CompositionStateData(metadata_={"name": "Original draft", "description": ""}),
                    provenance="session_seed",
                    session_operation_context=seed,
                )
            finally:
                service.session_operation_authority.release(seed)
        with harness.app.state.session_engine.connect() as conn:
            before = conn.execute(select(states).where(states.c.session_id == sid)).all()
        authored = _pipeline(tmp_path, sid)
        authored["source"]["on_success"] = "input_rows"
        authored["nodes"] = [
            {
                "id": "interpret_text",
                "node_type": "transform",
                "plugin": "llm",
                "input": "input_rows",
                "on_success": "rows",
                "on_error": "discard",
                "options": {
                    "profile": "offline-review",
                    "system_prompt": "Describe the supplied name. Reply with one short factual description.",
                    "prompt_template": "Describe {{ row.name }} using Use a concise factual tone.",
                    "prompt_template_parts": [
                        {"kind": "text", "text": "Describe {{ row.name }} using "},
                        {"kind": "interpretation_ref", "requirement_id": "tone:interpret_text"},
                    ],
                    "required_input_fields": ["name"],
                    "response_field": "summary",
                    "schema": {"mode": "observed", "guaranteed_fields": ["summary"]},
                    "interpretation_requirements": [{"kind": "vague_term", "user_term": "tone", "draft": "Use a concise factual tone."}],
                },
            }
        ]

        def author(app):
            return "Preparing the requested proposal.", [tool_call(app, "authored-interpretation", "set_pipeline", authored)]

        review = end_admit("The proposal still awaits approval and interpretation review.")
        review["verdict"] = "FLAGGED"
        sdk = ScriptedSDK(
            [
                ("primary", author),
                ("semantic", semantic_primary_response),
                ("primary", ("The proposed pipeline is waiting for your approval.", None)),
                ("semantic", semantic_review_response),
                ("end", review),
                ("semantic", semantic_stop_response),
            ],
            app_ref=[harness.app],
        )
        install_sdk(monkeypatch, sdk)
        result = await submit_and_settle(
            harness,
            {
                "content": "Build a CSV to JSONL pipeline with an LLM description of each name.",
                "operation_id": str(uuid4()),
                "state_id": None if base is None else str(base.id),
            },
        )
        assert result.status == "completed" and result.result is not None, result.error
        assert sdk.roles == ["primary", "semantic", "primary", "semantic", "end", "semantic"] and not sdk.steps
        assert len(_accounting(harness)) == 6
        (proposal,) = result.result.model_dump(mode="json")["proposals"]
        assert proposal["status"] == "pending" and proposal["tool_name"] == "set_pipeline"
        with harness.app.state.session_engine.connect() as conn:
            assert conn.execute(select(states).where(states.c.session_id == sid)).all() == before
            assert conn.execute(select(interpretation_events_table.c.id).where(interpretation_events_table.c.session_id == sid)).all() == []
            row = conn.execute(select(proposals).where(proposals.c.id == proposal["id"])).one()
        assert row.base_state_id == (None if base is None else str(base.id))
        accepted = await client.post(
            f"/api/sessions/{sid}/proposals/{proposal['id']}/accept", json={"draft_hash": proposal["pipeline_metadata"]["draft_hash"]}
        )
        assert accepted.status_code == 200 and accepted.json()["status"] == "committed", accepted.text
        current = await client.get(f"/api/sessions/{sid}/state")
        assert current.status_code == 200
        state = current.json()
        assert accepted.json()["committed_state_id"] == state["id"]
        assert state["composer_meta"]["completion_gates"]["advisor_signoff"]["status"] == "blocked"
        composition = CompositionState.from_dict(
            {key: state[key] for key in ("sources", "nodes", "edges", "outputs", "metadata", "version")}
        )
        pending = await client.get(f"/api/sessions/{sid}/interpretations?status=pending")
        assert pending.status_code == 200
        events = pending.json()["events"]
        assert events and all(event["composition_state_id"] == state["id"] and event["choice"] == "pending" for event in events)
        with harness.app.state.session_engine.connect() as conn:
            payload = conn.execute(
                select(proposal_events_table.c.payload).where(
                    proposal_events_table.c.proposal_id == proposal["id"], proposal_events_table.c.event_type == "proposal.accepted"
                )
            ).scalar_one()
            event_rows = conn.execute(select(interpretation_events_table).where(interpretation_events_table.c.session_id == sid)).all()
        cohort = payload["review_cohort"]
        assert payload["committed_state_content_hash"] == payload["final_state_content_hash"] == composition_content_hash(composition)
        assert {member["event_id"] for member in cohort} == {event["id"] for event in events} == {row.id for row in event_rows}
        by_event = {event["id"]: event for event in events}
        for ordinal, member in enumerate(cohort):
            material = member["semantic_material"]
            event = by_event[member["event_id"]]
            assert member["ordinal"] == material["ordinal"] == ordinal
            assert member["candidate_state_id"] == material["candidate_state_id"] == state["id"]
            assert member["tool_call_id"] == event["tool_call_id"]
            for key in (
                "affected_node_id",
                "kind",
                "user_term",
                "llm_draft",
                "surface_origin",
                "model_identifier",
                "model_version",
                "provider",
                "composer_skill_hash",
            ):
                assert material[key] == event[key]
            assert member["semantic_hash"] == stable_hash({"schema": "composer.pipeline-review-semantic.v1", "material": material})
            assert member["initial_disposition"] == "pending"
            assert member["initial_resolution_hash"] == stable_hash(member["initial_resolution"])
        (tone,) = [event for event in events if event["kind"] == "vague_term" and event["user_term"] == "tone"]
        resolved = await client.post(
            f"/api/sessions/{sid}/interpretations/{tone['id']}/resolve",
            json={"choice": "amended", "amended_value": "Use a precise formal tone."},
        )
        assert resolved.status_code == 200, resolved.text
        resolution = resolved.json()
        event = resolution["event"]
        assert event["id"] == tone["id"] and event["composition_state_id"] == state["id"]
        assert event["choice"] == "amended" and event["accepted_value"] == "Use a precise formal tone."
        assert event["llm_draft"] == tone["llm_draft"] == "Use a concise factual tone."
        assert event["hash_domain_version"] == "v2"
        assert event["arguments_hash"] == stable_hash(
            _interpretation_hash_domain_v2(
                **{
                    key: event[key]
                    for key in (
                        "session_id",
                        "composition_state_id",
                        "affected_node_id",
                        "tool_call_id",
                        "user_term",
                        "kind",
                        "llm_draft",
                        "accepted_value",
                        "actor",
                        "model_identifier",
                        "model_version",
                        "provider",
                        "composer_skill_hash",
                    )
                },
                context="current planner interpretation regression",
            )
        )
        (node,) = resolution["new_state"]["nodes"]
        assert "Use a precise formal tone." in node["options"]["prompt_template"]
        assert node["options"]["prompt_template"] != authored["nodes"][0]["options"]["prompt_template"]
        artifact = approved_prompt_artifact_hash_from_options(node["options"])
        assert (
            artifact is not None and event["approved_prompt_artifact_hash"] == node["options"]["approved_prompt_artifact_hash"] == artifact
        )
        (requirement,) = [
            item for item in node["options"]["interpretation_requirements"] if item["kind"] == "vague_term" and item["user_term"] == "tone"
        ]
        assert requirement["status"] == "resolved" and requirement["event_id"] == event["id"]
        assert requirement["accepted_value"] == event["accepted_value"]
        assert requirement["resolved_prompt_template_hash"] == stable_hash(event["accepted_value"])
        reloaded = await client.get(f"/api/sessions/{sid}/interpretations")
        assert reloaded.status_code == 200 and next(item for item in reloaded.json()["events"] if item["id"] == event["id"]) == event
        final = await client.get(f"/api/sessions/{sid}/state")
        assert final.status_code == 200 and final.json()["id"] == resolution["new_state"]["id"]
        assert sdk.roles == ["primary", "semantic", "primary", "semantic", "end", "semantic"] and len(_accounting(harness)) == 6

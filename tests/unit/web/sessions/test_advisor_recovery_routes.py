"""Advisor-only decisions survive real route, fenced write, and DB reload seams.

The requests drive the production app, worker and Continuation driver through
``tests.helpers.continuation_app``; only the SDK and the prompt-guard HTTP
boundary are scripted. The END Advisor review is the real checkpoint.
"""

from uuid import uuid4

import litellm
import pytest

from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, ADVISOR_GRAPH_REVIEW_AUDIT_KIND
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorSignoffGateFact
from elspeth.web.execution.completion_gates import (
    CompletionGateFacts,
    completion_gate_fingerprint,
    completion_gates_meta_from_facts,
    merge_completion_gates,
    parse_completion_gates,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schemas import MessageWithStateResponse
from tests.helpers.advisor_review import parse_advisor_publication_review
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, submit_and_settle
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.sessions.test_completion_gate_roundtrip import _green_result
from tests.unit.web.sessions.test_routes import _assert_required_route_guard_checks, _make_authoring_valid_partial


async def _seed_head(harness, state, composer_meta):
    state_d = state.to_dict()
    with fenced_operation_context(harness.app.state.session_engine, str(harness.session_id)) as context:
        await harness.app.state.session_service.save_composition_state(
            harness.session_id,
            CompositionStateData(
                sources=state_d["sources"],
                nodes=state_d["nodes"],
                edges=state_d["edges"],
                outputs=state_d["outputs"],
                metadata_=state_d["metadata"],
                is_valid=True,
                validation_errors=None,
                composer_meta=composer_meta,
            ),
            provenance="session_seed",
            session_operation_context=context,
        )
    head = await harness.app.state.session_service.get_current_state(harness.session_id)
    assert head is not None
    return head


async def _request_body(harness, route: str, content: str, state_id) -> dict[str, object]:
    if route == "recompose":
        with fenced_operation_context(harness.app.state.session_engine, str(harness.session_id)) as context:
            user_message = await harness.app.state.session_service.add_message(
                harness.session_id, "user", content, writer_principal="route_user_message", session_operation_context=context
            )
        return {"expected_user_message_id": str(user_message.id), "operation_id": str(uuid4()), "state_id": str(state_id)}
    return {"content": content, "operation_id": str(uuid4()), "state_id": str(state_id)}


def _audit(rows, kind: str, key: str) -> list[dict[str, object]]:
    return [row.tool_calls[0][key] for row in rows if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == kind]


def _advisor_guard_envelopes(rows) -> list[dict[str, object]]:
    return [
        envelope
        for row in rows
        if row.role == "audit" and row.tool_calls
        for envelope in row.tool_calls
        if envelope["_kind"] == "prompt_guard_audit" and envelope["check"]["role"] == "advisor"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["messages", "recompose"])
@pytest.mark.parametrize("cause", [AdvisorBlockCause.UNAVAILABLE, AdvisorBlockCause.MALFORMED, AdvisorBlockCause.MESSAGE_REJECTED])
async def test_advisor_recovery_is_durable_only_with_explicit_clean_decision(tmp_path, route, cause, monkeypatch):
    """A clean END decision on the unchanged graph clears the prior blocked fact durably."""
    reviews: list[dict[str, object]] = []

    class _ReviewingSDK(ScriptedSDK):
        async def __call__(self, **kwargs):
            if kwargs["model"] != "test/model":
                assert "tools" not in kwargs and kwargs["num_retries"] == kwargs["max_retries"] == 0
                assert kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response"
                review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
                assert review["current_request"] == "Please retry the review"
                assert review["candidate_reply"] == "Review complete"
                assert review["saved_effects"]["saved_revision_count"] == 0 and review["saved_effects"]["net_changed"] is False
                reviews.append(review)
            return await super().__call__(**kwargs)

    sdk = _ReviewingSDK([("primary", ("Review complete", None)), ("end", end_admit(""))])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        state = _make_authoring_valid_partial("advisor-recovery")
        fingerprint = completion_gate_fingerprint(state)
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail="Review did not complete", suggestion=None, note=None, for_graph=fingerprint, cause=cause
            )
        )
        before = await _seed_head(harness, state, {"completion_gates": completion_gates_meta_from_facts(facts), "repair_turns_used": 2})
        body = await _request_body(harness, route, "Please retry the review", before.id)
        snapshot = await submit_and_settle(harness, body, route=route)
        assert snapshot.status == "completed", (snapshot, sdk.roles)
        assert snapshot.result is not None
        response_body = MessageWithStateResponse.model_validate(snapshot.result).model_dump(mode="json")
        after = await service.get_current_state(harness.session_id)
        assert after is not None
        assert after.id == before.id and after.version == before.version
        assert after.review_projection is not None
        assert after.review_projection.stored.id == before.id
        assert after.review_projection.stored.composer_meta == before.composer_meta
        rebuilt = state_from_record(after)
        assert completion_gate_fingerprint(rebuilt) == fingerprint
        reloaded_facts = parse_completion_gates(after.composer_meta)
        assert response_body["state"]["id"] == str(after.id)
        assert response_body["message"]["composition_state_id"] == str(after.id)
        assert parse_completion_gates(response_body["state"]["composer_meta"]) == reloaded_facts
        assert reloaded_facts == CompletionGateFacts(advisor_signoff=None)
        assert merge_completion_gates(_green_result(), reloaded_facts, rebuilt).readiness.completion_ready is True
        assert len(reviews) == 1
        stored = await service.get_messages(harness.session_id, limit=None)
        _assert_required_route_guard_checks(
            _advisor_guard_envelopes(stored), harness.app.state.composer_service.composer_prompt_guard_mount, roles=["advisor"]
        )
        pass_records = _audit(stored, ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass")
        graph_records = _audit(stored, ADVISOR_GRAPH_REVIEW_AUDIT_KIND, "review")
        assert len(pass_records) == len(graph_records) == 1
        assert pass_records[0]["verdict"] == "clean" and pass_records[0]["publication_verdict"] == "ADMIT"
        assert pass_records[0]["provider_attempts"] == 1 and pass_records[0]["first_attempt_accepted"] is True
        assert pass_records[0]["publication_binding"] == graph_records[0]["review_binding"]
        assert graph_records[0]["state_id"] == str(before.id) and graph_records[0]["action"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["messages", "recompose"])
async def test_unchanged_graph_first_block_and_changed_failure_cause_are_saved(tmp_path, route, monkeypatch):
    unavailable = litellm.AuthenticationError(message="fixture advisor unavailable", llm_provider="anthropic", model="test/advisor")
    # UNAVAILABLE spends one END attempt; MALFORMED spends two (one repair retry).
    steps = [
        ("primary", ("Review failed", None)),
        ("raise", unavailable),
        ("primary", ("Review failed", None)),
        ("end", "not structured JSON"),
        ("end", "not structured JSON"),
        ("primary", ("Review failed", None)),
        ("end", "not structured JSON"),
        ("end", "not structured JSON"),
    ]
    sdk = ScriptedSDK(steps)
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        state = _make_authoring_valid_partial("advisor-failures")
        fingerprint = completion_gate_fingerprint(state)
        original = await _seed_head(harness, state, {"completion_gates": {"schema_version": 2}})
        previous = original
        guard_count = 0
        bindings = set()
        for cause, should_save in (
            (AdvisorBlockCause.UNAVAILABLE, True),
            (AdvisorBlockCause.MALFORMED, True),
            (AdvisorBlockCause.MALFORMED, False),
        ):
            body = await _request_body(harness, route, "Retry review", previous.id)
            snapshot = await submit_and_settle(harness, body, route=route)
            assert snapshot.status == "completed", (snapshot, sdk.roles)
            assert snapshot.result is not None
            response = MessageWithStateResponse.model_validate(snapshot.result).model_dump(mode="json")
            record = await service.get_current_state(harness.session_id)
            assert record is not None
            assert record.id == original.id and record.version == original.version
            assert record.review_projection is not None
            assert record.review_projection.stored.composer_meta == original.composer_meta
            assert completion_gate_fingerprint(state_from_record(record)) == fingerprint
            fact = parse_completion_gates(record.composer_meta).advisor_signoff
            assert fact is not None and fact.cause is cause
            assert (record.composer_meta != previous.composer_meta) is should_save
            assert parse_completion_gates(response["state"]["composer_meta"]).advisor_signoff == fact
            assert (
                merge_completion_gates(
                    _green_result(), parse_completion_gates(record.composer_meta), state_from_record(record)
                ).readiness.completion_ready
                is False
            )
            rows = await service.get_messages(harness.session_id, limit=None)
            expected_attempts = 1 if cause is AdvisorBlockCause.UNAVAILABLE else 2
            guard_envelopes = _advisor_guard_envelopes(rows)
            _assert_required_route_guard_checks(
                guard_envelopes[guard_count:],
                harness.app.state.composer_service.composer_prompt_guard_mount,
                roles=["advisor"] * expected_attempts,
            )
            guard_count += expected_attempts
            assert len(guard_envelopes) == guard_count
            reviews = _audit(rows, ADVISOR_GRAPH_REVIEW_AUDIT_KIND, "review")
            passes = _audit(rows, ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass")
            assert len(reviews) == len(passes)
            assert reviews[-1]["state_id"] == str(original.id) and reviews[-1]["action"] == "blocked"
            assert (
                reviews[-1]["review_source"]
                == passes[-1]["verdict"]
                == ("unavailable" if cause is AdvisorBlockCause.UNAVAILABLE else "malformed")
            )
            assert reviews[-1]["gates"]["advisor_signoff"]["cause"] == cause.value
            assert passes[-1]["publication_binding"] is None and passes[-1]["publication_verdict"] is None
            assert passes[-1]["provider_attempts"] == expected_attempts
            bindings.add(reviews[-1]["review_binding"])
            previous = record
        assert sdk.steps == [] and len(bindings) == 3

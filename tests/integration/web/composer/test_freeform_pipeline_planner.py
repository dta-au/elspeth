"""Freeform empty-pipeline requests use the canonical proposal lifecycle."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import litellm
import pytest
import structlog
from litellm.exceptions import APIError as LiteLLMAPIError
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.pipeline_planner import PlannerCustodyConfig, PlannerOriginatingMessage
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.provider_cost import CommonProviderCostBudget
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    proposal_events_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.protocol import ComposerSessionPreferencesTransition, ComposerTrustMode, CompositionStateData
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.helpers.composer_common_sdk import CommonSDK, Step, assert_actual_audits, assert_initial_wire
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer.test_pipeline_planner import _plan, _response, _ScriptedCompletion
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness


@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Function


@dataclass
class _Message:
    content: str | None
    tool_calls: list[_ToolCall]


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]
    usage: Mapping[str, object]
    model: str = "provider/planner-v1"
    id: str = "planner-request-1"
    # OpenRouter's served-endpoint name; absent unless a test sets it.
    provider: str | None = None


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _pipeline(data_dir: Path, session_id: str) -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "path": str(data_dir / "blobs" / session_id / "input.csv"),
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "discard",
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {
                    "path": str(data_dir / "outputs" / session_id / "result.jsonl"),
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
    }


def _terminal_response(data_dir: Path, session_id: str) -> _Response:
    return _Response(
        choices=[
            _Choice(
                message=_Message(
                    content=None,
                    tool_calls=[
                        _ToolCall(
                            id="freeform-terminal",
                            function=_Function(
                                name="emit_pipeline_proposal",
                                arguments=json.dumps({"pipeline": _pipeline(data_dir, session_id)}),
                            ),
                        )
                    ],
                )
            )
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
    )


def _routing_terminal_response(data_dir: Path, session_id: str, *, condition: str) -> _Response:
    pipeline = _pipeline(data_dir, session_id)
    pipeline["nodes"] = [
        {
            "id": "route_amount",
            "node_type": "gate",
            "input": "rows",
            "condition": condition,
            "routes": ({"true": "fork", "false": "fork"} if condition == "True" else {"true": "high_value", "false": "standard"}),
            **({"fork_to": ["high_value", "standard"]} if condition == "True" else {}),
        }
    ]
    pipeline["outputs"] = [
        {
            "sink_name": sink_name,
            "plugin": "json",
            "options": {
                "path": str(data_dir / "outputs" / session_id / f"{sink_name}.jsonl"),
                "schema": {"mode": "observed"},
                "format": "jsonl",
                "mode": "write",
                "collision_policy": "auto_increment",
            },
            "on_write_failure": "discard",
        }
        for sink_name in ("high_value", "standard")
    ]
    return _Response(
        choices=[
            _Choice(
                message=_Message(
                    content=None,
                    tool_calls=[
                        _ToolCall(
                            id=f"freeform-routing-{condition}",
                            function=_Function(
                                name="emit_pipeline_proposal",
                                arguments=json.dumps({"pipeline": pipeline}),
                            ),
                        )
                    ],
                )
            )
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
    )


def _source_files(tmp_path: Path, sid) -> None:
    source = tmp_path / "blobs" / str(sid) / "input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / str(sid)).mkdir(parents=True, exist_ok=True)


async def _common_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, trust_mode, persisted_base: bool = False):
    request = "Build a CSV to JSONL pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    await sessions.update_composer_preferences(session.id, trust_mode=trust_mode, density_default="high", actor="test")
    base = None
    if persisted_base:
        base = await sessions.save_composition_state(
            session.id,
            CompositionStateData(
                sources={},
                nodes=[],
                edges=[],
                outputs=[],
                metadata_={"name": "Untitled Pipeline", "description": ""},
                is_valid=False,
            ),
            provenance="session_seed",
        )
        with engine.connect() as conn:
            seed_before = dict(
                conn.execute(select(composition_states_table).where(composition_states_table.c.id == str(base.id))).mappings().one()
            )
    _source_files(tmp_path, session.id)
    authored = _pipeline(tmp_path, str(session.id))
    auto = trust_mode == "auto_commit"
    candidate = "The draft was saved." if auto else "The proposed pipeline is waiting for approval."
    steps = [Step("primary", tool_name="set_pipeline", arguments={"pipeline": authored})]
    if auto:
        steps.append(Step("early"))
    steps.extend((Step("primary", content=candidate), Step("end")))
    script = CommonSDK(request=request, candidate=candidate, steps=tuple(steps), saved=int(auto))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    result = await composer.compose(
        request,
        [],
        _empty_state(),
        session_id=str(session.id),
        current_state_id=None if base is None else str(base.id),
        user_id="planner-user",
        user_message_id=str(user.id),
    )
    script.assert_done()
    assert sdk.await_count == len(steps)
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert script.requests[0]["max_tokens"] == composer._settings.composer_planner_max_completion_tokens
    assert all(request["max_tokens"] <= composer._settings.composer_planner_max_completion_tokens for request in script.requests)
    calls = assert_actual_audits(engine, script.requests, ("success",) * len(steps), session_id=str(session.id))
    if base is not None:
        with engine.connect() as conn:
            seed_after = dict(
                conn.execute(select(composition_states_table).where(composition_states_table.c.id == str(base.id))).mappings().one()
            )
        assert seed_after == seed_before
    return engine, sessions, session, user, composer, base, result, script, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_mode", ["auto_commit", "explicit_approve"])
@pytest.mark.parametrize("persisted_base", [False, True], ids=["absent-base", "present-base"])
async def test_empty_build_obeys_common_trust_mode_and_exact_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_mode: str,
    persisted_base: bool,
) -> None:
    from elspeth.web.composer.authority_hashing import composer_authority_hash

    engine, sessions, session, user, composer, base, result, script, calls = await _common_build(
        tmp_path, monkeypatch, trust_mode=trust_mode, persisted_base=persisted_base
    )
    proposals = await sessions.list_composition_proposals(session.id)
    assert result.pipeline_commit_intent is None
    with engine.connect() as conn:
        states = (
            conn.execute(
                select(composition_states_table)
                .where(composition_states_table.c.session_id == str(session.id))
                .order_by(composition_states_table.c.version)
            )
            .mappings()
            .all()
        )
    if trust_mode == "explicit_approve":
        (proposal,) = proposals
        assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
        assert deep_thaw(proposal.arguments_json) == _pipeline(tmp_path, str(session.id))
        assert proposal.user_message_id == user.id and proposal.tool_call_id == "common-tool-1"
        assert proposal.base_state_id == (None if base is None else base.id)
        metadata = proposal.pipeline_metadata
        assert metadata is not None
        assert metadata.base == (
            {"kind": "absent"}
            if base is None
            else {"kind": "present", "state_id": str(base.id), "composition_content_hash": composition_content_hash(_empty_state())}
        )
        assert metadata.repair_count == 0
        assert metadata.skill_hash == proposal.composer_skill_hash == composer._composer_skill_hash
        assert proposal.composer_model_identifier == script.requests[0]["model"] == composer._settings.composer_model
        assert proposal.composer_model_version == calls[0]["model_returned"] == "provider/common-v1"
        assert proposal.composer_provider == composer._availability.provider == "test"
        assert proposal.tool_arguments_hash == composer_authority_hash(_pipeline(tmp_path, str(session.id)))
        authority = await sessions.get_authoritative_pipeline_proposal(session_id=session.id, proposal_id=proposal.id)
        assert authority.row == proposal
        assert deep_thaw(authority.proposal.pipeline) == _pipeline(tmp_path, str(session.id))
        assert authority.proposal.draft_hash == metadata.draft_hash
        assert result.state == _empty_state()
        assert len(states) == int(base is not None)
        assert all(state["provenance"] == "session_seed" for state in states)
        assert script.phases == ["end"]
    else:
        from elspeth.web.sessions.converters import state_from_record, state_record_from_row
        from tests.helpers.advisor_review import parse_advisor_publication_review

        assert proposals == [] and result.state.sources["source"].plugin == "csv"
        assert result.state.outputs[0].plugin == "json"
        assert len(states) == int(base is not None) + 1
        saved = [state for state in states if base is None or state["id"] != str(base.id)]
        (saved_state,) = saved
        end_review = parse_advisor_publication_review(script.requests[-1]["messages"][1]["content"])
        saved_effects = end_review["saved_effects"]
        assert saved_effects["verification"] == "verified"
        assert saved_effects["saved_revision_count"] == 1
        assert saved_effects["latest_committed_state_id"] == saved_state["id"]
        assert saved_effects["latest_verified_state_id"] == saved_state["id"]
        with engine.connect() as conn:
            (tool_row,) = (
                conn.execute(
                    select(chat_messages_table).where(
                        chat_messages_table.c.session_id == str(session.id),
                        chat_messages_table.c.role == "tool",
                        chat_messages_table.c.tool_call_id == "common-tool-1",
                    )
                )
                .mappings()
                .all()
            )
            (attempt,) = (
                conn.execute(
                    select(quota_provider_attempts_table).where(
                        quota_provider_attempts_table.c.session_id == str(session.id),
                        quota_provider_attempts_table.c.attempt_id == calls[0]["call_id"],
                    )
                )
                .mappings()
                .all()
            )
        assert result.persisted_tool_call_turn is True
        assert result.persisted_assistant_message_id is not None
        assert tool_row["writer_principal"] == "compose_loop"
        assert tool_row["parent_assistant_id"] == result.persisted_assistant_message_id
        assert tool_row["composition_state_id"] == saved_state["id"]
        assert saved_state["provenance"] == "tool_call"
        assert saved_state["derived_from_state_id"] == (None if base is None else str(base.id))
        assert saved_state["creator_operation_id"] == attempt["operation_id"]
        with engine.connect() as conn:
            saved_row = conn.execute(
                select(composition_states_table).where(
                    composition_states_table.c.session_id == str(session.id),
                    composition_states_table.c.id == saved_state["id"],
                )
            ).one()
        saved_record = state_record_from_row(saved_row)
        assert str(saved_record.id) == saved_state["id"] and saved_record.session_id == session.id
        saved_graph = state_from_record(saved_record)
        assert saved_graph.version == saved_record.version == saved_state["version"]
        assert saved_record.version == (1 if base is None else base.version + 1)
        assert saved_graph.sources["source"].plugin == "csv"
        assert saved_graph.outputs[0].plugin == "json"
        assert saved_graph.sources["source"].options["path"] == str(tmp_path / "blobs" / str(session.id) / "input.csv")
        assert saved_graph.outputs[0].options["path"] == str(tmp_path / "outputs" / str(session.id) / "result.jsonl")
        stored = saved_graph.to_dict()
        returned = result.state.to_dict()
        assert stored["sources"] == returned["sources"]
        assert stored["nodes"] == returned["nodes"] and stored["edges"] == returned["edges"]
        assert stored["outputs"] == returned["outputs"] and stored["metadata"] == returned["metadata"]
        assert script.phases == ["early", "end"]


@pytest.mark.asyncio
async def test_common_llm_call_audit_persists_the_served_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common provider boundary persists its served endpoint on each actual SQL call row."""
    _engine, _sessions, _session, _user, _composer, _base, _result, script, calls = await _common_build(
        tmp_path, monkeypatch, trust_mode="explicit_approve"
    )
    assert len(calls) == len(script.requests) == 3
    assert all(call["provider_served"] == "DeepInfra" for call in calls)
    assert {call["provider_request_id"] for call in calls} == {"common-sdk-1", "common-sdk-2", "common-sdk-3"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_trust_mode", "flipped_trust_mode"),
    [("auto_commit", "explicit_approve"), ("explicit_approve", "auto_commit")],
    ids=["downgrade-mid-plan", "upgrade-mid-plan"],
)
async def test_trust_mode_change_during_provider_requires_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_trust_mode: ComposerTrustMode,
    flipped_trust_mode: ComposerTrustMode,
) -> None:
    """A preference flip during the common primary provider call requires review.

    Regression for elspeth-01d4c6e683: trust authority was snapshotted before
    the provider call, so a user switching auto_commit -> explicit_approve
    mid-call still had the in-flight plan committed automatically. Auto-commit
    requires the request ceiling AND the preference at staging time to
    both be auto_commit; any mismatch lands the proposal on the review path.
    """
    request = "Build a CSV to JSONL pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    _source_files(tmp_path, session.id)
    await sessions.update_composer_preferences(
        session.id,
        trust_mode=initial_trust_mode,
        density_default="high",
        actor="test",
    )
    flips: list[ComposerSessionPreferencesTransition] = []

    async def flip() -> None:
        flips.append(
            await sessions.update_composer_preferences(
                session.id,
                trust_mode=flipped_trust_mode,
                density_default="high",
                actor="test",
            )
        )

    authored = _pipeline(tmp_path, str(session.id))
    candidate = "The proposed pipeline is waiting for your review."
    script = CommonSDK(
        request=request,
        candidate=candidate,
        steps=(
            Step("primary", tool_name="set_pipeline", arguments={"pipeline": authored}, before_return=flip),
            Step("primary", content=candidate),
            Step("end"),
        ),
    )
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    result = await composer.compose(
        request,
        [],
        _empty_state(),
        session_id=str(session.id),
        current_state_id=None,
        user_id="planner-user",
        user_message_id=str(user.id),
    )
    script.assert_done()
    assert sdk.await_count == 3 and script.phases == ["end"]
    (transition,) = flips
    assert transition.prior.trust_mode == initial_trust_mode
    assert transition.current.trust_mode == flipped_trust_mode
    assert (await sessions.get_composer_preferences(session.id)).trust_mode == flipped_trust_mode
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert result.message.startswith(candidate)
    assert "for your review" in result.message
    assert result.pipeline_commit_intent is None and result.state == _empty_state()
    (proposal,) = await sessions.list_composition_proposals(session.id)
    assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
    assert deep_thaw(proposal.arguments_json) == authored
    assert proposal.user_message_id == user.id and proposal.tool_call_id == "common-tool-1"
    assert proposal.base_state_id is None and proposal.committed_state_id is None
    metadata = proposal.pipeline_metadata
    assert metadata is not None
    assert metadata.base == {"kind": "absent"} and metadata.repair_count == 0
    assert metadata.skill_hash == proposal.composer_skill_hash == composer._composer_skill_hash
    authority = await sessions.get_authoritative_pipeline_proposal(session_id=session.id, proposal_id=proposal.id)
    assert authority.row == proposal
    assert deep_thaw(authority.proposal.pipeline) == authored
    assert authority.proposal.draft_hash == metadata.draft_hash
    with engine.connect() as conn:
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(blobs_table)).all() == []
    assert_actual_audits(engine, script.requests, ("success",) * 3, session_id=str(session.id))


@pytest.mark.asyncio
async def test_referential_empty_build_projects_authoritative_prior_user_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A referential build turn must not erase the request it refers to."""
    from elspeth.web.composer.request_history import authoritative_conversation_context

    request = "Build the requested pipeline."
    original = (
        "Read transactions.csv, route rows where amount > 500 to high_value, route every other row to standard, and write both as JSONL."
    )
    refinement = "Keep an audit trace field in both outputs."
    engine, sessions, session, first_user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=original)
    prior = [
        first_user,
        await sessions.add_message(session.id, "assistant", "I can prepare that pipeline.", writer_principal="compose_loop"),
        await sessions.add_message(session.id, "user", refinement, writer_principal="route_user_message"),
        await sessions.add_message(session.id, "assistant", "Understood.", writer_principal="compose_loop"),
    ]
    current = await sessions.add_message(session.id, "user", request, writer_principal="route_user_message")
    records = [*prior, current]
    assert len({record.id for record in records}) == 5
    sequence_numbers = [record.sequence_no for record in records]
    assert all(number is not None for number in sequence_numbers)
    assert sequence_numbers == sorted(sequence_numbers)
    assert len(set(sequence_numbers)) == 5
    history = _composer_chat_history(prior)
    context = authoritative_conversation_context(request, history)
    assert context is not None
    assert [(item.history_index, item.content) for item in context.prior_user_requests] == [(0, original), (2, refinement)]
    assert [(item.history_index, item.content) for item in context.prior_assistant_messages] == [
        (1, "I can prepare that pipeline."),
        (3, "Understood."),
    ]
    assert history[0]["_elspeth_user_message_id"] == str(prior[0].id)
    assert history[2]["_elspeth_user_message_id"] == str(prior[2].id)
    _source_files(tmp_path, session.id)
    await sessions.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="test")
    authored = _pipeline(tmp_path, str(session.id))
    (tmp_path / "blobs" / str(session.id) / "input.csv").write_text("name,amount,audit_trace\nAda,600,fixture\n", encoding="utf-8")
    authored["nodes"] = [
        {
            "id": "route_amount",
            "node_type": "gate",
            "input": "rows",
            "condition": "row['amount'] > 500",
            "routes": {"true": "high_value", "false": "standard"},
        }
    ]
    authored["outputs"] = [
        {
            **deepcopy(authored["outputs"][0]),
            "sink_name": name,
            "options": {
                **deepcopy(authored["outputs"][0]["options"]),
                "path": str(tmp_path / "outputs" / str(session.id) / f"{name}.jsonl"),
            },
        }
        for name in ("high_value", "standard")
    ]
    candidate = "The requested routing proposal is waiting for approval."
    script = CommonSDK(
        request=request,
        candidate=candidate,
        steps=(
            Step("primary", tool_name="set_pipeline", arguments={"pipeline": authored}),
            Step("primary", content=candidate),
            Step("end"),
        ),
    )
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    result = await composer.compose(
        request,
        history,
        _empty_state(),
        session_id=str(session.id),
        user_id="planner-user",
        user_message_id=str(current.id),
    )
    script.assert_done()
    assert sdk.await_count == 3 and script.phases == ["end"]
    for physical_primary in script.requests[:2]:
        primary = physical_primary["messages"]
        positions = [
            next(i for i, row in enumerate(primary) if row["role"] == role and row["content"] == text)
            for role, text in [
                ("user", original),
                ("assistant", "I can prepare that pipeline."),
                ("user", refinement),
                ("assistant", "Understood."),
            ]
        ]
        assert positions == sorted(positions)
        assert len(positions) == len(set(positions)) == 4
        (current_position,) = [i for i, row in enumerate(primary) if row["role"] == "user" and row["content"] == request]
        assert current_position > positions[-1]
    assert context.additional_prior_user_requests_omitted == 0
    assert [item.content for item in context.prior_user_requests] == [original, refinement]
    authoritative_requests = json.dumps([item.content for item in context.prior_user_requests])
    assert "I can prepare that pipeline." not in authoritative_requests
    assert "Understood." not in authoritative_requests
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert result.state == _empty_state() and result.pipeline_commit_intent is None
    (proposal,) = await sessions.list_composition_proposals(session.id)
    assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
    assert deep_thaw(proposal.arguments_json)["nodes"][0]["condition"] == "row['amount'] > 500"
    assert deep_thaw(proposal.arguments_json) == authored
    assert proposal.user_message_id == current.id and proposal.tool_call_id == "common-tool-1"
    assert proposal.base_state_id is None and proposal.committed_state_id is None
    metadata = proposal.pipeline_metadata
    assert metadata is not None
    assert metadata.base == {"kind": "absent"} and metadata.repair_count == 0
    assert metadata.skill_hash == proposal.composer_skill_hash == composer._composer_skill_hash
    authority = await sessions.get_authoritative_pipeline_proposal(session_id=session.id, proposal_id=proposal.id)
    assert authority.row == proposal
    assert deep_thaw(authority.proposal.pipeline) == authored
    assert authority.proposal.draft_hash == metadata.draft_hash
    with engine.connect() as conn:
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(blobs_table)).all() == []
    assert_actual_audits(engine, script.requests, ("success",) * 3, session_id=str(session.id))


@pytest.mark.parametrize("trust_mode", ["auto_commit", "explicit_approve"])
def test_cancellation_preserves_actual_common_or_pending_proposal_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_mode: str,
) -> None:
    if trust_mode == "auto_commit":
        from tests.integration.web.composer.test_common_mutation_authority_cancellation import run_public_cancel_case

        run_public_cancel_case(tmp_path, monkeypatch, seam="started_p4", fail_sql=False, count=3)
        return

    async def explicit_case() -> None:
        request = "Build a CSV to JSONL pipeline."
        engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
        _source_files(tmp_path, session.id)
        await sessions.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="test")
        script = CommonSDK(
            request=request,
            candidate="",
            steps=(
                Step(
                    "primary",
                    tool_name="set_pipeline",
                    arguments={"pipeline": _pipeline(tmp_path, str(session.id))},
                ),
            ),
        )
        script.bind(composer, monkeypatch)
        sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
        monkeypatch.setattr(litellm, "acompletion", sdk)
        entered, release = threading.Event(), threading.Event()

        def park(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("INSERT INTO composition_proposals"):
                entered.set()
                assert release.wait(10)

        event.listen(engine, "before_cursor_execute", park)
        task = asyncio.create_task(
            composer.compose(
                request,
                [],
                _empty_state(),
                session_id=str(session.id),
                current_state_id=None,
                user_id="planner-user",
                user_message_id=str(user.id),
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel("cancel during actual explicit proposal SQL")
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 10)
            assert type(caught.value) is asyncio.CancelledError
            assert caught.value.args == ("cancel during actual explicit proposal SQL",)
            script.assert_done()
            assert sdk.await_count == 1 and script.phases == []
            assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
            assert_actual_audits(engine, script.requests, ("success",), session_id=str(session.id))

            (proposal,) = await sessions.list_composition_proposals(session.id)
            assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
            assert proposal.committed_state_id is None and proposal.base_state_id is None
            assert proposal.user_message_id == user.id
            assert proposal.tool_call_id == "common-tool-1"
            assert deep_thaw(proposal.arguments_json) == _pipeline(tmp_path, str(session.id))
            assert proposal.pipeline_metadata is not None
            assert proposal.pipeline_metadata.base == {"kind": "absent"}
            authority = await sessions.get_authoritative_pipeline_proposal(session_id=session.id, proposal_id=proposal.id)
            assert authority.row.status == "pending"
            assert authority.row.audit_event_id == authority.creation_event_id

            with engine.connect() as conn:
                assert conn.execute(select(composition_states_table)).all() == []
                assert conn.execute(select(blobs_table)).all() == []
                assert (
                    conn.execute(select(proposal_events_table).where(proposal_events_table.c.event_type == "proposal.rejected")).all() == []
                )
                audit_rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.tool_calls)).all()
            assert not any(calls and calls[0].get("_kind") == "audit" for _role, calls in audit_rows)
            await asyncio.wait_for(
                sessions.add_message(session.id, "system", "lock probe", writer_principal="route_system_message"),
                2,
            )
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(engine, "before_cursor_execute", park)

    asyncio.run(explicit_case())


def test_auto_commit_cancellation_preserves_p4_failure_and_repeated_originals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.web.composer.test_common_mutation_authority_cancellation import run_public_cancel_case

    run_public_cancel_case(tmp_path, monkeypatch, seam="started_p4", fail_sql=True, count=3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "state"),
    [
        ("What can you help me build?", _empty_state()),
        (
            "Add a JSONL output to this pipeline.",
            CompositionState(
                source=SourceSpec(
                    plugin="csv",
                    on_success="rows",
                    options={"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                ),
                nodes=(),
                edges=(),
                outputs=(),
                metadata=PipelineMetadata(),
                version=1,
            ),
        ),
    ],
    ids=["empty-informational", "nonempty-incremental"],
)
async def test_empty_and_existing_graph_use_same_actual_common_provider_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    state: CompositionState,
) -> None:
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=message)
    candidate = "I can explain the current draft without changing it."
    script = CommonSDK(request=message, candidate=candidate, steps=(Step("primary", content=candidate), Step("end")))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    common = AsyncMock(spec=composer._compose_loop, wraps=composer._compose_loop)
    monkeypatch.setattr(composer, "_compose_loop", common)

    budget_bindings = []
    actual_bind = ProviderInvocationOwner.bind_cost_budget

    def observe_binding(owner: ProviderInvocationOwner, budget: CommonProviderCostBudget) -> None:
        actual_bind(owner, budget)
        budget_bindings.append((owner, budget, budget.snapshot()))

    monkeypatch.setattr(ProviderInvocationOwner, "bind_cost_budget", observe_binding)
    before = state.to_dict()

    result = await composer.compose(
        message,
        [],
        state,
        session_id=str(session.id),
        user_id="planner-user",
        user_message_id=str(user.id),
    )
    common.assert_awaited_once()
    script.assert_done()
    assert sdk.await_count == 2
    assert result.message.startswith(candidate)
    assert result.state == state and state.to_dict() == before
    assert await sessions.list_composition_proposals(session.id) == []
    assert_initial_wire(script.requests[0], text=message, state=state, session_id=str(session.id))
    _assert_no_pipeline_side_effects(engine)
    calls = assert_actual_audits(engine, script.requests, ("success", "success"), session_id=str(session.id))

    ((owner, budget, initial),) = budget_bindings
    assert type(owner) is ProviderInvocationOwner and type(budget) is CommonProviderCostBudget
    assert owner.service is sessions and owner.cost_budget is budget
    assert common.await_args is not None
    loop_call = common.await_args
    assert loop_call.args[0] == message and loop_call.args[2] is state
    assert loop_call.args[3] == str(session.id) and loop_call.args[5] == "planner-user" and loop_call.args[8] == str(user.id)
    assert loop_call.kwargs["provider_owner"] is owner and loop_call.kwargs["required_work"] is owner.required_work
    context = loop_call.kwargs["session_operation_context"]
    assert context is owner.required_work.coordinator.authority.context
    assert context.operation_kind is SessionOperationKind.COMPOSE and context.fence.session_id == str(session.id)
    snapshot = budget.snapshot()
    assert snapshot.session_id == initial.session_id == str(session.id)
    assert snapshot.request_id == initial.request_id == str(user.id)
    assert snapshot.lifecycle_version == initial.lifecycle_version == 1
    assert initial.physical_calls == 0 and initial.charges == ()
    assert snapshot.policy is initial.policy
    if not state.sources:
        assert snapshot.applicability == initial.applicability == "legacy_empty_v1"
        assert snapshot.policy is not None
        settings = composer._settings
        assert snapshot.policy.max_provider_calls == settings.composer_planner_max_provider_calls
        assert snapshot.policy.max_request_bytes == settings.composer_planner_max_request_bytes
        assert snapshot.policy.max_completion_tokens == settings.composer_planner_max_completion_tokens
        assert snapshot.policy.max_cumulative_provider_cost == settings.composer_planner_max_cumulative_provider_cost
        assert snapshot.policy.repair_budget == settings.composer_planner_repair_budget
        assert all(request["max_tokens"] <= snapshot.policy.max_completion_tokens for request in script.requests)
    else:
        assert snapshot.applicability == initial.applicability == "not_applicable"
        assert snapshot.policy is None
    assert snapshot.physical_calls == len(snapshot.charges) == 2
    assert [charge.call_id for charge in snapshot.charges] == [call["call_id"] for call in calls]
    assert all(charge.cost == Decimal("0.01") and charge.cost_source == "response_usage.cost" for charge in snapshot.charges)
    assert all(charge.response_accountable for charge in snapshot.charges)
    assert snapshot.known_cost == snapshot.validated_cost == Decimal("0.02")
    assert snapshot.cost_known is True and snapshot.refusal_code is None

    with engine.connect() as conn:
        attempts = (
            conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session.id)))
            .mappings()
            .all()
        )
        ledger = (
            conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session.id))).mappings().all()
        )
    assert len(attempts) == len(ledger) == 2
    assert {attempt["attempt_id"] for attempt in attempts} == {call["call_id"] for call in calls}
    assert len({attempt["ledger_entry_id"] for attempt in attempts}) == 2
    assert {attempt["ledger_entry_id"] for attempt in attempts} == {entry["entry_id"] for entry in ledger}
    for attempt in attempts:
        assert attempt["settled_at"] is not None
        assert attempt["identity_id"] == "planner-user" and attempt["source"] == "composer"
        assert attempt["operation_id"] == context.fence.operation_id
        assert attempt["operation_epoch"] == context.fence.operation_epoch
        assert attempt["lease_token"] == context.fence.lease_token
        (entry,) = [entry for entry in ledger if entry["entry_id"] == attempt["ledger_entry_id"]]
        (call,) = [call for call in calls if call["call_id"] == attempt["attempt_id"]]
        assert entry["identity_id"] == "planner-user" and entry["source"] == "composer"
        assert entry["model"] == call["model_returned"]
        assert entry["prompt_tokens"] == call["prompt_tokens"] == 10
        assert entry["completion_tokens"] == call["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_common_required_provider_audit_failure_publishes_no_proposal_or_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.sessions.service import ComposerRequiredAuditPersistenceError

    request = "Build a CSV to JSONL pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    preference_transition = await sessions.update_composer_preferences(
        session.id, trust_mode="explicit_approve", density_default="high", actor="test"
    )
    assert preference_transition.current.trust_mode == "explicit_approve"
    assert preference_transition.current.density_default == "high"
    with engine.connect() as conn:
        events_before = [dict(row) for row in conn.execute(select(proposal_events_table).order_by(proposal_events_table.c.id)).mappings()]
    (preference_event,) = events_before
    assert preference_event["event_type"] == "trust_mode.changed"
    assert preference_event["session_id"] == str(session.id)
    assert preference_event["proposal_id"] is None
    assert preference_event["actor"] == "test"
    assert preference_event["payload"] == {
        "trust_mode": preference_transition.current.trust_mode,
        "prior_trust_mode": preference_transition.prior.trust_mode,
        "density_default": preference_transition.current.density_default,
    }
    script = CommonSDK(
        request=request,
        candidate="",
        steps=(Step("primary", tool_name="set_pipeline", arguments={"pipeline": _pipeline(tmp_path, str(session.id))}),),
    )
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    original = sessions._insert_chat_message
    sql_failure = SQLAlchemyError("actual required native provider audit failure")
    fired: list[SQLAlchemyError] = []

    def fail(conn: Any, **kwargs: Any) -> Any:
        envelopes = kwargs["tool_calls"]
        if kwargs["role"] == "audit" and envelopes and envelopes[0].get("_kind") == "llm_call_audit":
            fired.append(sql_failure)
            raise sql_failure
        return original(conn, **kwargs)

    monkeypatch.setattr(sessions, "_insert_chat_message", fail)
    with pytest.raises(ComposerRequiredAuditPersistenceError) as caught:
        await composer.compose(
            request,
            [],
            _empty_state(),
            session_id=str(session.id),
            user_id="planner-user",
            user_message_id=str(user.id),
        )

    assert caught.value.__cause__ is sql_failure
    assert fired == [sql_failure]
    assert sdk.await_count == 1 and script.phases == []
    script.assert_done()
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert await sessions.list_composition_proposals(session.id) == []
    persisted = await sessions.get_messages(session.id, limit=None)
    durable_call_kinds = [
        envelope["_kind"]
        for row in persisted
        for envelope in (row.tool_calls or ())
        if envelope.get("_kind") in {"prompt_guard_audit", "llm_call_audit", "planner_attempt_audit"}
    ]
    assert durable_call_kinds == ["prompt_guard_audit"]
    with engine.connect() as conn:
        events_after = [dict(row) for row in conn.execute(select(proposal_events_table).order_by(proposal_events_table.c.id)).mappings()]
        assert events_after == events_before
        assert conn.execute(select(blobs_table)).all() == []
        assert conn.execute(select(composition_proposals_table)).all() == []
        assert conn.execute(select(composition_states_table)).all() == []
        assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "tool")).all() == []


async def _recipe_composer_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: str,
) -> tuple[Any, SessionServiceImpl, Any, Any, ComposerServiceImpl]:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="planner-user")
        grant_test_pipeline_user(conn, identity_id="planner-user")
    sessions = FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session = await sessions.create_session("planner-user", "Planner", "local")
    user_message = await sessions.add_message(
        session.id,
        "user",
        message,
        writer_principal="route_user_message",
    )
    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_boot_probe_enabled=False,
        composer_prompt_guard_mode="debug-when-unconfigured",
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **_kwargs: ComposerAvailability(available=True, provider="test", model="test/planner", reason=None),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(),
        settings,
        sessions_service=sessions,
        session_engine=engine,
    )
    return engine, sessions, session, user_message, composer


async def _direct_manifest_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
    expected_status: ComposerLLMCallStatus,
    *,
    pause_insert: Callable[..., None] | None = None,
) -> tuple[BaseException, AuditIntegrityError]:
    import elspeth.web.composer.pipeline_planner as planner_module

    request = "Build the requested pipeline."
    engine, sessions, session, user, _composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    tool_context = ToolContext(catalog=policy, plugin_snapshot=snapshot)
    origin = PlannerOriginatingMessage(
        session_id=str(session.id),
        message_id=str(user.id),
        content=request,
        user_id="planner-user",
    )
    manifests: list[Any] = []
    owned: list[list[dict[str, Any]]] = []
    owned_hashes: list[str] = []
    integrity_failures: list[AuditIntegrityError] = []
    real_builder = planner_module.build_planner_capability_manifest
    real_matcher = planner_module._assert_planner_call_matches_manifest

    def observe_manifest(**kwargs: Any) -> Any:
        manifest = real_builder(**kwargs)
        manifests.append(manifest)
        owned.append(kwargs["messages"])
        return manifest

    def observe_mismatch(call: Any, manifest: Any, recorder: Any) -> None:
        try:
            real_matcher(call, manifest, recorder)
        except AuditIntegrityError as failure:
            integrity_failures.append(failure)
            raise

    monkeypatch.setattr(planner_module, "build_planner_capability_manifest", observe_manifest)
    monkeypatch.setattr(planner_module, "_assert_planner_call_matches_manifest", observe_mismatch)

    class MutatingCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> Any:
            assert len(owned) == 1 and self.requests == []
            assert kwargs["messages"] is not owned[0]
            original_content = owned[0][0]["content"]
            kwargs["messages"][0]["content"] += "\nSDK detached mutation"
            assert owned[0][0]["content"] == original_content
            owned[0][0]["content"] += "\nactual owned manifest mutation"
            owned_hashes.append(stable_hash(owned[0]))
            return await super().__call__(**kwargs)

    completion = MutatingCompletion(outcome)
    recorder = BufferingRecorder()
    if pause_insert is not None:
        event.listen(engine, "before_cursor_execute", pause_insert)
    try:
        async with acquire_compose_context(sessions, session.id) as context:
            custody = PlannerCustodyConfig(
                data_dir=str(tmp_path),
                session_engine=engine,
                max_storage_per_session=1_000_000,
                secret_service=None,
                runtime_preflight=None,
                session_operation_context=context,
                session_operation_authority=sessions.session_operation_authority,
            )
            error: BaseException | None = None
            try:
                await _plan(
                    tmp_path=tmp_path,
                    tool_context=tool_context,
                    completion=completion,
                    recorder=recorder,
                    originating_message=origin,
                    custody_config=custody,
                )
            except BaseException as failure:
                error = failure
            assert error is not None, "controlled owned manifest mutation did not refuse"
            (integrity,) = integrity_failures
            assert type(integrity) is AuditIntegrityError
            assert str(integrity) == "planner call inputs changed after capability manifest construction"
            assert len(manifests) == len(completion.requests) == len(owned_hashes) == 1
            assert completion._responses == []
            (call,) = recorder.llm_calls
            assert call.status is expected_status
            assert call.call_id is not None
            assert call.planner_call_ordinal == 1
            assert call.messages_hash == owned_hashes[0]
            assert call.messages_hash != manifests[0].rendered_prompt_hash
            assert call.tools_spec_hash == manifests[0].effective_tool_hash
            physical = completion.requests[0]
            assert [row["role"] for row in physical["messages"]] == ["system", "user", "user"]
            assert json.loads(physical["messages"][2]["content"])["intent"] == request
            names = {tool["function"]["name"] for tool in physical["tools"]}
            assert "emit_pipeline_proposal" in names and "set_pipeline" not in names
            assert physical["messages"][0]["content"].endswith("\nSDK detached mutation")
            assert "actual owned manifest mutation" not in physical["messages"][0]["content"]

            persisted = await sessions.get_messages(session.id, limit=None)
            native = [
                (row, envelope) for row in persisted for envelope in (row.tool_calls or ()) if envelope.get("_kind") == "llm_call_audit"
            ]
            ((native_row, public_envelope),) = native
            public_envelope = deep_thaw(public_envelope)
            assert native_row.role == "audit" and native_row.writer_principal == "compose_loop"
            assert public_envelope == llm_call_audit_envelope(call)
            public_call = public_envelope["call"]
            assert public_call["call_id"] == call.call_id
            assert public_call["status"] == expected_status.value
            assert public_call["messages_hash"] == call.messages_hash
            assert public_call["messages_hash"] != manifests[0].rendered_prompt_hash
            assert public_call["tools_spec_hash"] == call.tools_spec_hash == manifests[0].effective_tool_hash
            public_text = json.dumps(public_envelope, sort_keys=True)
            assert request not in public_text
            assert "SDK detached mutation" not in public_text
            assert "actual owned manifest mutation" not in public_text

            with engine.connect() as conn:
                attempt = dict(
                    conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session.id)))
                    .mappings()
                    .one()
                )
                ledger = dict(
                    conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session.id)))
                    .mappings()
                    .one()
                )
            assert attempt["attempt_id"] == call.call_id
            assert attempt["identity_id"] == ledger["identity_id"] == "planner-user"
            assert attempt["session_id"] == ledger["session_id"] == str(session.id)
            assert attempt["source"] == ledger["source"] == "composer"
            assert attempt["operation_id"] == context.fence.operation_id
            assert attempt["operation_epoch"] == context.fence.operation_epoch
            assert attempt["lease_token"] == context.fence.lease_token
            assert attempt["settled_at"] is not None
            assert attempt["ledger_entry_id"] == ledger["entry_id"]
            expected_model = call.model_requested if call.model_returned is None else call.model_returned
            assert ledger["model"] == expected_model
            assert ledger["prompt_tokens"] == call.prompt_tokens
            assert ledger["completion_tokens"] == call.completion_tokens
            if expected_status is ComposerLLMCallStatus.SUCCESS:
                assert call.provider_cost == 0.01
                assert call.prompt_tokens == 10 and call.completion_tokens == 5
            else:
                assert call.provider_cost is None and call.provider_cost_source == "not_available"
                assert call.model_returned is None
                assert call.prompt_tokens is None and call.completion_tokens is None

            private_requests = [
                (row, envelope)
                for row in persisted
                for envelope in (row.tool_calls or ())
                if envelope.get("_kind") == "composer_diagnostic_evidence" and envelope["evidence"]["status"] == "provider_request"
            ]
            ((private_row, private_envelope),) = private_requests
            assert private_row.role == "audit" and private_row.writer_principal == "compose_loop"
            evidence = ComposerDiagnosticEvidence.from_envelope(
                private_envelope,
                expected_session_id=str(session.id),
                expected_operation_id=context.fence.operation_id,
                expected_tool_call_id=call.call_id,
            )
            assert evidence.operation_epoch == context.fence.operation_epoch
            assert evidence.operation_epoch == attempt["operation_epoch"]
            assert evidence.tool_name == "composer_provider_context" and evidence.status == "provider_request"
            assert call.private_request is not None and call.private_request.canonical is not None
            assert evidence.arguments.content is not None
            assert json.loads(evidence.arguments.content) == json.loads(call.private_request.canonical)
            assert "SDK detached mutation" not in evidence.arguments.content
            assert "actual owned manifest mutation" not in evidence.arguments.content
            assert await sessions.list_composition_proposals(session.id) == []
            _assert_no_pipeline_side_effects(engine)
            return error, integrity
    finally:
        if pause_insert is not None:
            event.remove(engine, "before_cursor_execute", pause_insert)


@pytest.mark.asyncio
async def test_freeform_matching_provider_cancellation_persists_once_without_self_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider cancellation remains the original exception after its audit commits."""
    message = "Build a CSV to JSONL pipeline."
    engine, sessions, session, user_message, composer = await _recipe_composer_context(
        tmp_path,
        monkeypatch,
        message=message,
    )

    async def cancelling_completion(**_kwargs: Any) -> _Response:
        raise asyncio.CancelledError("provider cancelled matching freeform planner request")

    monkeypatch.setattr("litellm.acompletion", cancelling_completion)

    with pytest.raises(asyncio.CancelledError) as caught:
        await composer.compose(
            message,
            [],
            _empty_state(),
            session_id=str(session.id),
            user_id="planner-user",
            user_message_id=str(user_message.id),
        )

    assert type(caught.value) is asyncio.CancelledError
    assert caught.value.args == ("provider cancelled matching freeform planner request",)
    assert caught.value.__cause__ is None
    persisted = await sessions.get_messages(session.id, limit=None)
    audit_calls = [
        envelope["call"] for row in persisted for envelope in (row.tool_calls or ()) if envelope.get("_kind") == "llm_call_audit"
    ]
    assert len(audit_calls) == 1
    assert audit_calls[0]["status"] == ComposerLLMCallStatus.CANCELLED.value
    assert await sessions.list_composition_proposals(session.id) == []
    _assert_no_pipeline_side_effects(engine)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "expected_status"),
    (
        ("success", ComposerLLMCallStatus.SUCCESS),
        ("error", ComposerLLMCallStatus.API_ERROR),
        ("cancel", ComposerLLMCallStatus.CANCELLED),
    ),
)
async def test_direct_planner_manifest_mismatch_is_durable_before_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_outcome: str,
    expected_status: ComposerLLMCallStatus,
) -> None:
    original: BaseException | None = None
    if provider_outcome == "error":
        original = LiteLLMAPIError(
            status_code=503,
            message="provider unavailable",
            llm_provider="test-provider",
            model="anthropic/claude-planner",
        )
    elif provider_outcome == "cancel":
        original = asyncio.CancelledError("actual direct planner provider cancellation")
    response = _response(("emit_pipeline_proposal", {"pipeline": {}}))
    error, integrity = await _direct_manifest_case(
        tmp_path,
        monkeypatch,
        response if original is None else original,
        expected_status,
    )
    if original is None:
        assert error is integrity
    else:
        expected_type = ExceptionGroup if provider_outcome == "error" else BaseExceptionGroup
        assert type(error) is expected_type
        assert isinstance(error, BaseExceptionGroup)
        assert error.message == "Provider original body and audit construction outcomes"
        assert error.exceptions == (original, integrity)


@pytest.mark.asyncio
async def test_direct_manifest_mismatch_checkpoint_defers_original_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    native_inserts: list[str] = []
    actual_cancellations: list[asyncio.CancelledError] = []
    real_raise = ProviderCallCustody.raise_deferred_cancellations

    def observe_deferred(
        owner: ProviderCallCustody,
        cancellations: tuple[asyncio.CancelledError, ...],
    ) -> None:
        actual_cancellations.extend(cancellations)
        real_raise(owner, cancellations)

    def pause(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        execution_context: Any,
        many: bool,
    ) -> None:
        if not statement.startswith("INSERT INTO chat_messages"):
            return
        (bound,) = execution_context.compiled_parameters
        envelopes = bound["tool_calls"]
        if bound["role"] != "audit" or not envelopes or envelopes[0].get("_kind") != "llm_call_audit":
            return
        assert not many and native_inserts == []
        native_inserts.append(envelopes[0]["call"]["call_id"])
        entered.set()
        assert release.wait(10), "actual native audit worker was not released"

    monkeypatch.setattr(ProviderCallCustody, "raise_deferred_cancellations", observe_deferred)
    task = asyncio.create_task(
        _direct_manifest_case(
            tmp_path,
            monkeypatch,
            _response(("emit_pipeline_proposal", {"pipeline": {}})),
            ComposerLLMCallStatus.SUCCESS,
            pause_insert=pause,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10), "actual native audit insert was not reached"
        task.cancel("request cancelled during actual native manifest checkpoint")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        error, integrity = await asyncio.wait_for(task, 10)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert len(native_inserts) == 1
    (cancellation,) = actual_cancellations
    assert type(cancellation) is asyncio.CancelledError
    assert cancellation.args == ("request cancelled during actual native manifest checkpoint",)
    assert type(error) is BaseExceptionGroup
    assert error.message == "Provider body and required settlement outcomes"
    assert error.exceptions == (integrity, cancellation)


def _assert_no_pipeline_side_effects(engine: Any) -> None:
    with engine.connect() as conn:
        assert conn.execute(select(blobs_table)).all() == []
        assert conn.execute(select(composition_proposals_table)).all() == []
        assert conn.execute(select(proposal_events_table)).all() == []
        assert conn.execute(select(composition_states_table)).all() == []


@pytest.mark.asyncio
async def test_freeform_common_provider_timeout_has_no_pipeline_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first physical common provider timeout leaves zero pipeline side effects."""
    from elspeth.web.composer.protocol import ComposerConvergenceError

    request = "Build the requested pipeline."
    engine, sessions, session, user, composer = await _recipe_composer_context(tmp_path, monkeypatch, message=request)
    original = TimeoutError("bounded physical timeout")
    script = CommonSDK(request=request, candidate="", steps=(Step("primary", failure=original),))
    script.bind(composer, monkeypatch)
    sdk = AsyncMock(spec=litellm.acompletion, side_effect=script.__call__)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    common = AsyncMock(spec=composer._compose_loop, wraps=composer._compose_loop)
    monkeypatch.setattr(composer, "_compose_loop", common)

    with pytest.raises(ComposerConvergenceError) as caught:
        await composer.compose(
            request,
            [],
            _empty_state(),
            session_id=str(session.id),
            user_id="planner-user",
            user_message_id=str(user.id),
        )

    assert caught.value.budget_exhausted == "timeout" and caught.value.max_turns == 0
    common.assert_awaited_once()
    script.assert_done()
    assert sdk.await_count == 1 and script.phases == []
    assert_initial_wire(script.requests[0], text=request, state=_empty_state(), session_id=str(session.id))
    assert_actual_audits(engine, script.requests, ("timeout",), session_id=str(session.id))
    assert await sessions.list_composition_proposals(session.id) == []
    _assert_no_pipeline_side_effects(engine)

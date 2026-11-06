"""Provider-authored pipelines through the current Continuation application route."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.execution.completion_gates import CompletionGateFacts, completion_gates_meta_from_facts
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import chat_messages_table, composition_states_table, quota_provider_attempts_table
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    semantic_primary_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)


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
                                name="set_pipeline",
                                arguments=json.dumps({"pipeline": pipeline}),
                            ),
                        )
                    ],
                )
            )
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
    )


# Retired: direct compose-loop audit, history, cancellation and timeout tests used
# the removed entry point; current terminal coverage lives in the driver cohorts.
# Retired: same-loop object identity asserted the deleted loop's construction
# contract. Saved-base, prior-message and actual served-endpoint contracts stay.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_mode", "changed_mode", "persisted_base", "prior_history"),
    [
        ("explicit_approve", None, False, False),
        ("auto_commit", None, False, False),
        ("auto_commit", "explicit_approve", False, False),
        ("explicit_approve", "auto_commit", False, False),
        ("explicit_approve", None, True, False),
        ("auto_commit", None, True, False),
        ("explicit_approve", None, False, True),
    ],
    ids=["explicit", "auto", "downgrade-during-sdk", "upgrade-during-sdk", "explicit-saved-base", "auto-saved-base", "prior-two-users"],
)
async def test_provider_pipeline_respects_request_ceiling_and_current_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_mode: str,
    changed_mode: str | None,
    persisted_base: bool,
    prior_history: bool,
) -> None:
    """Only unchanged auto-commit authority may publish the provider's graph.

    Preference changes occur at the actual external SDK boundary, after request
    admission. The real worker, P4 owner and proposal authority do the staging.
    """
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sessions = harness.app.state.session_service
        prior = [
            ("user", "Read input.csv and write the rows as JSONL."),
            ("assistant", "I can prepare that pipeline."),
            ("user", "Keep every input column in the output."),
            ("assistant", "Understood."),
        ]
        base = None
        if persisted_base or prior_history:
            seed = sessions.session_operation_authority.acquire(
                session_id=harness.session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id="current-pipeline-seed",
                lease_seconds=30,
            )
            try:
                if prior_history:
                    for role, content in prior:
                        await sessions.add_message(
                            harness.session_id,
                            role,
                            content,
                            writer_principal="route_user_message" if role == "user" else "compose_loop",
                            session_operation_context=seed,
                        )
                if persisted_base:
                    base = await sessions.save_composition_state(
                        harness.session_id,
                        CompositionStateData(
                            metadata_={"name": "Untitled Pipeline", "description": ""},
                            composer_meta={"completion_gates": completion_gates_meta_from_facts(CompletionGateFacts(None))},
                        ),
                        provenance="session_seed",
                        session_operation_context=seed,
                    )
            finally:
                sessions.session_operation_authority.release(seed)
        with harness.app.state.session_engine.connect() as conn:
            seed_rows = (
                conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(harness.session_id)))
                .mappings()
                .all()
            )
        blob_dir = tmp_path / "blobs" / str(harness.session_id)
        blob_dir.mkdir(parents=True, exist_ok=True)
        (blob_dir / "input.csv").write_text("name\nAda\n", encoding="utf-8")
        (tmp_path / "outputs" / str(harness.session_id)).mkdir(parents=True, exist_ok=True)
        await sessions.update_composer_preferences(harness.session_id, trust_mode=initial_mode, density_default="high", actor="test")
        authored = _pipeline(tmp_path, str(harness.session_id))
        auto_commit = initial_mode == "auto_commit" and changed_mode is None

        def author(app):
            return "Building the requested pipeline.", [tool_call(app, "current_author", "set_pipeline", authored)]

        steps = [("primary", author)]
        if auto_commit:
            steps.append(
                ("early", {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The draft is coherent.", "note": None})
            )
        steps.extend(
            [
                ("semantic", semantic_primary_response),
                ("primary", ("The requested pipeline is ready.", None)),
                ("semantic", semantic_review_response),
                ("end", end_admit("The provider's pipeline and reply satisfy the request.")),
                ("semantic", semantic_stop_response),
            ]
        )
        sdk = ScriptedSDK(steps, app_ref=[harness.app])
        transitions = []

        async def complete(**kwargs):
            if changed_mode is not None and not sdk.requests:
                transitions.append(
                    await sessions.update_composer_preferences(
                        harness.session_id, trust_mode=changed_mode, density_default="high", actor="test"
                    )
                )
            response = await sdk(**kwargs)
            assert isinstance(response, ModelResponse)
            return response.model_copy(update={"provider": "DeepInfra", "id": f"current-pipeline-{len(sdk.requests)}"})

        install_sdk(monkeypatch, complete)
        request = "Build the requested pipeline." if prior_history else "Build a CSV to JSONL pipeline."
        body = message_body(request, operation_id=operation_id)
        body["state_id"] = None if base is None else str(base.id)
        terminal = await submit_and_settle(harness, body, rounds=720)
        assert terminal.status == "completed", terminal.error
        assert terminal.result is not None
        assert sdk.roles == [role for role, _payload in steps] and not sdk.steps
        if prior_history:
            for physical_primary in (item for item in sdk.requests if item["role"] == "primary"):
                messages = physical_primary["messages"]
                positions = [
                    next(i for i, item in enumerate(messages) if item["role"] == role and item["content"] == content)
                    for role, content in prior
                ]
                assert positions == sorted(set(positions)) and len(positions) == 4
                (current_position,) = [i for i, item in enumerate(messages) if item["role"] == "user" and item["content"] == request]
                assert current_position > positions[-1]
        if changed_mode is None:
            assert transitions == []
        else:
            (transition,) = transitions
            assert transition.prior.trust_mode == initial_mode
            assert transition.current.trust_mode == changed_mode
        assert (await sessions.get_composer_preferences(harness.session_id)).trust_mode == (changed_mode or initial_mode)
        proposals = await sessions.list_composition_proposals(harness.session_id)
        with harness.app.state.session_engine.connect() as conn:
            states = (
                conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(harness.session_id)))
                .mappings()
                .all()
            )
            attempts = (
                conn.execute(
                    select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(harness.session_id))
                )
                .mappings()
                .all()
            )
            audit_rows = conn.execute(
                select(chat_messages_table).where(
                    chat_messages_table.c.session_id == str(harness.session_id), chat_messages_table.c.role == "audit"
                )
            ).all()
        calls = [envelope["call"] for row in audit_rows for envelope in (row.tool_calls or ()) if envelope.get("_kind") == "llm_call_audit"]
        assert len(calls) == len(sdk.requests)
        assert all(call["provider_served"] == "DeepInfra" for call in calls)
        assert {call["provider_request_id"] for call in calls} == {f"current-pipeline-{index}" for index in range(1, len(sdk.requests) + 1)}
        if base is not None:
            assert [row for row in states if row["id"] == str(base.id)] == seed_rows
        new_states = [row for row in states if base is None or row["id"] != str(base.id)]
        assert len(attempts) == len(sdk.requests)
        assert len({row["attempt_id"] for row in attempts}) == len(attempts)
        assert all(row["settled_at"] is not None and row["ledger_entry_id"] is not None for row in attempts)
        retained = effects(harness.app, operation_id)
        assert any(row.owner == "tool_dispatch" and row.disposition == "committed" for row in retained)
        assert any(row.effect_key.endswith(":p4") and row.disposition == "committed" for row in retained)
        if auto_commit:
            assert proposals == [] and not terminal.result.proposals
            (state,) = new_states
            assert state["provenance"] == "tool_call" and state["derived_from_state_id"] == (None if base is None else str(base.id))
            assert state["creator_operation_id"] == operation_id
            assert terminal.result.state is not None
            assert terminal.result.state.sources["source"].plugin == "csv"
            assert terminal.result.state.outputs[0].plugin == "json"
        else:
            assert new_states == []
            (proposal,) = proposals
            assert len(terminal.result.proposals) == 1
            assert proposal.status == "pending" and proposal.tool_name == "set_pipeline"
            assert proposal.tool_call_id == "current_author"
            assert proposal.base_state_id == (None if base is None else base.id) and proposal.committed_state_id is None
            assert deep_thaw(proposal.arguments_json) == authored
            metadata = proposal.pipeline_metadata
            expected_base = (
                {"kind": "absent"}
                if base is None
                else {
                    "kind": "present",
                    "state_id": str(base.id),
                    "composition_content_hash": composition_content_hash(state_from_record(base)),
                }
            )
            assert metadata is not None and metadata.base == expected_base
            authority = await sessions.get_authoritative_pipeline_proposal(session_id=harness.session_id, proposal_id=proposal.id)
            assert authority.row == proposal
            assert deep_thaw(authority.proposal.pipeline) == authored
            assert authority.proposal.draft_hash == metadata.draft_hash

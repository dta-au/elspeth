"""The colour incident crosses provider authoring, review custody and execution.

Only planner/advisor completions and the runtime SDK are scripted. The tools,
session database, review resolver, graph validator and executor remain real.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from litellm import ModelResponse
from sqlalchemy import Engine, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import execute_tool
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, SOURCE_AUTHORING_KEY, materialize_state_for_execution
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.integration.pipeline.test_colour_multi_query_contract_repair import ColourScenario, run_colour_pipeline
from tests.integration.web.composer.test_prompt_review_card_end_to_end import _accept_as_drafted, _persist, _run_surfacer
from tests.unit.web.composer.conftest import _fake_llm_response, _make_settings, build_test_sessions_service

_COLOURS = ("red", "blue", "green", "yellow", "purple")
_FIELDS = ("colour", "good_colour_pair_answer", "approximate_hex_answer")
_USER_MESSAGE = (
    "Invent a small CSV with five different basic colour names in a colour column. "
    "For each colour, use one multi-query LLM node with two separate prompts: one asks "
    "for a good colour pairing and the other for an approximate hex value. Save exactly "
    "colour, good_colour_pair_answer, and approximate_hex_answer to colour_answers.csv. "
    "Preserve source-validation failures and LLM failures in separate outputs."
)


@dataclass(frozen=True)
class _Harness:
    engine: Engine
    sessions: SessionServiceImpl
    service: ComposerServiceImpl
    settings: WebSettings
    session_id: UUID
    user_message_id: UUID


async def _lease(harness: _Harness) -> SessionOperationLease:
    return await SessionOperationLease.acquire(
        harness.sessions.session_operation_authority,
        session_id=harness.session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=harness.sessions.session_operation_owner_instance_id,
        lease_seconds=harness.sessions.session_operation_lease_seconds,
    )


async def _harness(tmp_path: Path) -> _Harness:
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        grant_test_pipeline_user(conn, identity_id="alice")
    sessions = build_test_sessions_service(engine=engine, data_dir=tmp_path)
    session = await sessions.create_session(user_id="alice", title="Colour contracts", auth_provider_type="local")
    await sessions.update_composer_preferences(session.id, trust_mode="auto_commit", density_default="high", actor="user:alice")
    lease = await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=sessions.session_operation_lease_seconds,
    )
    async with lease:
        message = await sessions.add_message(
            session.id, "user", _USER_MESSAGE, writer_principal="route_user_message", session_operation_context=lease.context
        )
    settings = _make_settings(tmp_path)
    with patch(
        "elspeth.web.composer.service.compute_availability",
        return_value=ComposerAvailability(available=True, model="test-model", provider="test"),
    ):
        service = ComposerServiceImpl.for_trained_operator(
            catalog=create_catalog_service(), settings=settings, sessions_service=sessions, session_engine=engine
        )
    return _Harness(engine, sessions, service, settings, session.id, message.id)


def _pipeline(tmp_path: Path, session_id: UUID, content: str) -> dict[str, Any]:
    fields = [f"{name}: str" for name in _FIELDS]
    options = {
        "provider": "bedrock",
        "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
        "region_name": "us-east-1",
        "max_capacity_retry_seconds": 30,
        "system_prompt": "Give concise factual colour answers in the requested format.",
        "schema": {"mode": "flexible", "fields": ["colour: str"]},
        "required_input_fields": ["colour"],
        "queries": {
            name: {
                "input_fields": {"colour": "colour"},
                "template": template,
                "response_format": "structured",
                "output_fields": [{"suffix": "answer", "type": "string"}],
            }
            for name, template in (
                ("good_colour_pair", "What colour pairs well with {{ row.colour }}?"),
                ("approximate_hex", "What is an approximate hex value for {{ row.colour }}?"),
            )
        },
    }
    output_dir = tmp_path / "outputs" / str(session_id)
    return {
        "source": {
            "plugin": "csv",
            "on_success": "colour_rows",
            "on_validation_failure": "source_failures",
            "options": {"schema": {"mode": "fixed", "fields": ["colour: str"]}},
            "inline_blob": {"filename": "colours.csv", "mime_type": "text/csv", "content": content, "description": "Invented colours"},
        },
        "nodes": [
            {
                "id": "answer_colour_questions",
                "node_type": "transform",
                "plugin": "llm",
                "input": "colour_rows",
                "on_success": "answers",
                "on_error": "llm_failures",
                "options": options,
            },
            {
                "id": "select_csv_columns",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "answers",
                "on_success": "colour_answers",
                "on_error": "llm_failures",
                "options": {
                    "mapping": {name: name for name in _FIELDS},
                    "select_only": True,
                    "schema": {"mode": "flexible", "fields": fields},
                },
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "colour_answers",
                "plugin": "csv",
                "on_write_failure": "discard",
                "options": {
                    "path": str(output_dir / "colour_answers.csv"),
                    "mode": "write",
                    "collision_policy": "fail_if_exists",
                    "schema": {"mode": "fixed", "fields": fields},
                },
            },
            *[
                {
                    "sink_name": name,
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": {
                        "path": str(output_dir / f"{name}.jsonl"),
                        "format": "jsonl",
                        "mode": "write",
                        "collision_policy": "fail_if_exists",
                        "schema": {"mode": "observed"},
                    },
                }
                for name in ("source_failures", "llm_failures")
            ],
        ],
        "metadata": {"name": "Colour answers"},
    }


async def _author(harness: _Harness, tmp_path: Path, content: str) -> CompositionState:
    """A planner proposes the bad contract, sees its rejection, then repairs it."""
    repaired = _pipeline(tmp_path, harness.session_id, content)
    malformed = deepcopy(repaired)
    malformed["nodes"][0]["options"]["schema"]["fields"] = [f"{name}: str" for name in _FIELDS]
    malformed["nodes"][0]["options"]["required_input_fields"] = list(_FIELDS)
    # This incident begins with an existing source; the provider replaces it
    # with its invented inline CSV through the real set_pipeline dispatcher.
    existing = tmp_path / "blobs" / str(harness.session_id) / "existing.csv"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("colour\nwhite\n", encoding="utf-8")
    base = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="existing_rows",
            options={"path": str(existing), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    responses = [
        _fake_llm_response(tool_calls=({"id": "bad_contract", "name": "set_pipeline", "arguments": malformed},)),
        _fake_llm_response(tool_calls=({"id": "repaired_contract", "name": "set_pipeline", "arguments": repaired},)),
        _fake_llm_response(
            tool_calls=(
                {
                    "id": "review_source",
                    "name": "request_interpretation_review",
                    "arguments": {"affected_node_id": "source", "kind": "invented_source", "user_term": "inline_source_data"},
                },
                {
                    "id": "review_model",
                    "name": "request_interpretation_review",
                    "arguments": {
                        "affected_node_id": "answer_colour_questions",
                        "kind": "llm_model_choice",
                        "user_term": "llm_model_choice:answer_colour_questions",
                    },
                },
            )
        ),
        _fake_llm_response(content="Review the invented colours and both prompts before execution."),
    ]
    snapshots: list[list[dict[str, Any]]] = []

    async def planner(messages: list[dict[str, Any]], _tools: Any) -> Any:
        snapshots.append(deepcopy(messages))
        assert responses, messages[-1]["content"]
        return _admit_composer_llm_completion(responses.pop(0))

    async def advisor(**_kwargs: Any) -> ModelResponse:
        return ModelResponse(
            model="test-advisor",
            choices=[{"message": {"role": "assistant", "content": "CLEAN"}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=planner),
        patch("elspeth.web.composer.provider_gateway._litellm_acompletion", new=advisor),
    ):
        result = await harness.service.compose(
            _USER_MESSAGE, [], base, session_id=str(harness.session_id), user_id="alice", user_message_id=str(harness.user_message_id)
        )
    assert len(snapshots) >= 2
    bad_feedback = next(message for message in snapshots[1] if message.get("tool_call_id") == "bad_contract")
    feedback = json.loads(bad_feedback["content"])
    assert feedback["success"] is False
    assert feedback["validation"]["errors"][0]["error_code"] == "plugin_options_invalid"
    assert "generated by this node but also required as input" in feedback["validation"]["errors"][0]["message"]
    assert "answer_colour_questions" in bad_feedback["content"]
    assert any(invocation.tool_call_id == "repaired_contract" for invocation in result.tool_invocations)
    state = result.state
    malformed_state = replace(state, nodes=(replace(state.nodes[0], options=malformed["nodes"][0]["options"]), *state.nodes[1:]))
    assert any(
        error.error_code == "query_generated_fields_required" and error.component == "node:answer_colour_questions"
        for error in malformed_state.validate().errors
    ), malformed_state.validate().errors
    assert len([node for node in state.nodes if node.plugin == "llm"]) == 1
    assert deep_thaw(state.nodes[0].options["queries"]) == repaired["nodes"][0]["options"]["queries"]
    assert deep_thaw(state.nodes[0].options["schema"]) == {"mode": "flexible", "fields": ["colour: str"]}
    assert deep_thaw(state.nodes[0].options["required_input_fields"]) == ["colour"]
    assert deep_thaw(state.nodes[1].options) == repaired["nodes"][1]["options"]
    assert deep_thaw(state.outputs[0].options["schema"]) == repaired["outputs"][0]["options"]["schema"]
    return state


async def _approve(harness: _Harness, state: CompositionState) -> CompositionState:
    record = await _persist(harness.sessions, harness.session_id, state)
    await _run_surfacer(harness.sessions, harness.session_id, record)
    events = await harness.sessions.list_interpretation_events(harness.session_id, status="all")
    pending = [event for event in events if event.choice is InterpretationChoice.PENDING]
    assert InterpretationKind.INVENTED_SOURCE in {event.kind for event in pending}
    assert InterpretationKind.LLM_PROMPT_TEMPLATE in {event.kind for event in pending}
    for event in pending:
        _, state = await _accept_as_drafted(harness.sessions, harness.session_id, event)
    record = await harness.sessions.get_current_state(harness.session_id)
    assert record is not None
    return state_from_record(record)


async def _change_retention(harness: _Harness, state: CompositionState, tmp_path: Path) -> CompositionState:
    """A new source failure destination does not invalidate approved bytes."""
    source_options = deep_thaw(state.sources["source"].options)
    prior_events = await harness.sessions.list_interpretation_events(harness.session_id, status="all")
    source_events = [event for event in prior_events if event.kind is InterpretationKind.INVENTED_SOURCE]
    assert len(source_events) == 1
    source_event = source_events[0]
    assert source_event.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    assert source_options[SOURCE_AUTHORING_KEY]["review_event_id"] == str(source_event.id)
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    lease = await _lease(harness)
    async with lease:
        common = {
            "plugin_snapshot": snapshot,
            "data_dir": str(tmp_path),
            "session_engine": harness.engine,
            "session_id": str(harness.session_id),
            "session_operation_context": lease.context,
            "session_operation_authority": harness.sessions.session_operation_authority,
            "validate_arguments": True,
            "require_data_dir_for_paths": True,
        }
        retained = execute_tool(
            "set_output",
            {
                "sink_name": "source_failures_revised",
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(tmp_path / "outputs" / str(harness.session_id) / "source_failures_revised.jsonl"),
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "fail_if_exists",
                    "schema": {"mode": "observed"},
                },
            },
            state,
            policy,
            **common,
        )
        assert retained.success, retained.validation
        rebound = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": source_options["blob_ref"],
                "on_success": "colour_rows",
                "on_validation_failure": "source_failures_revised",
                "options": {"schema": source_options["schema"]},
            },
            retained.updated_state,
            policy,
            **common,
        )
        assert rebound.success, rebound.validation
        routed = rebound.updated_state
        if any(output.name == "source_failures" for output in routed.outputs):
            removed = execute_tool("remove_output", {"sink_name": "source_failures"}, routed, policy, **common)
            assert removed.success, removed.validation
            routed = removed.updated_state
    record = await _persist(harness.sessions, harness.session_id, routed)
    reloaded = await harness.sessions.get_current_state(harness.session_id)
    assert reloaded is not None and reloaded.id == record.id
    state = state_from_record(reloaded)
    new_source = state.sources["source"]
    assert new_source.on_validation_failure == "source_failures_revised"
    # Rebinding adds the explicit blob use mode; every existing option,
    # including the full resolver-owned proof, must remain byte-equivalent.
    assert deep_thaw(new_source.options) == {**source_options, "mode": "bind_source"}
    await _run_surfacer(harness.sessions, harness.session_id, reloaded)
    events = await harness.sessions.list_interpretation_events(harness.session_id, status="all")
    assert [(event.id, event.choice) for event in events] == [(event.id, event.choice) for event in prior_events]
    assert [event for event in events if event.kind is InterpretationKind.INVENTED_SOURCE] == source_events
    assert isinstance(materialize_state_for_execution(state), CompositionState)
    return state


@pytest.mark.asyncio
async def test_same_bytes_new_blob_keeps_actual_resolved_event_after_public_rebind_and_reload(tmp_path: Path) -> None:
    harness = await _harness(tmp_path)
    content = "colour\nred\nblue\n"
    state = await _approve(harness, await _author(harness, tmp_path, content))
    old_options = deep_thaw(state.sources["source"].options)
    prior_events = await harness.sessions.list_interpretation_events(harness.session_id, status="all")
    source_events = [event for event in prior_events if event.kind is InterpretationKind.INVENTED_SOURCE]
    assert len(source_events) == 1
    assert source_events[0].choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    assert old_options[SOURCE_AUTHORING_KEY]["review_event_id"] == str(source_events[0].id)
    with harness.engine.connect() as conn:
        original_blob = conn.execute(select(blobs_table).where(blobs_table.c.id == old_options["blob_ref"])).one()
    assert original_blob.creation_modality == "llm_generated"
    assert original_blob.creating_model_identifier
    assert original_blob.creating_model_version
    assert original_blob.creating_provider
    assert original_blob.creating_composer_skill_hash

    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    lease = await _lease(harness)
    async with lease:
        common = {
            "plugin_snapshot": snapshot,
            "data_dir": str(tmp_path),
            "session_engine": harness.engine,
            "session_id": str(harness.session_id),
            "session_operation_context": lease.context,
            "session_operation_authority": harness.sessions.session_operation_authority,
            "validate_arguments": True,
            "require_data_dir_for_paths": True,
        }
        create_args = {"filename": "same-bytes-rebound.csv", "mime_type": "text/csv", "content": content}
        created = execute_tool(
            "create_blob",
            create_args,
            state,
            policy,
            **common,
            user_message_id=str(harness.user_message_id),
            user_message_content=_USER_MESSAGE,
            composer_model_identifier=original_blob.creating_model_identifier,
            composer_model_version=original_blob.creating_model_version,
            composer_provider=original_blob.creating_provider,
            composer_skill_hash=original_blob.creating_composer_skill_hash,
            tool_arguments_hash=composer_authority_hash(create_args),
        )
        assert created.success, created.validation
        assert created.data["blob_id"] != old_options["blob_ref"]
        assert created.data["content_hash"] == old_options[SOURCE_AUTHORING_KEY]["content_hash"]
        rebound = execute_tool(
            "set_source_from_blob",
            {"blob_id": created.data["blob_id"], "on_success": "colour_rows", "options": {"schema": old_options["schema"]}},
            state,
            policy,
            **common,
        )
        assert rebound.success, rebound.validation
    rebound_options = deep_thaw(rebound.updated_state.sources["source"].options)
    assert rebound_options["blob_ref"] == created.data["blob_id"]
    assert rebound_options["path"] != old_options["path"]
    assert rebound_options[INTERPRETATION_REQUIREMENTS_KEY] == old_options[INTERPRETATION_REQUIREMENTS_KEY]
    assert rebound_options[SOURCE_AUTHORING_KEY] == old_options[SOURCE_AUTHORING_KEY]

    record = await _persist(harness.sessions, harness.session_id, rebound.updated_state)
    reloaded = await harness.sessions.get_current_state(harness.session_id)
    assert reloaded is not None and reloaded.id == record.id
    restored = state_from_record(reloaded)
    assert deep_thaw(restored.sources["source"].options[INTERPRETATION_REQUIREMENTS_KEY]) == old_options[INTERPRETATION_REQUIREMENTS_KEY]
    assert deep_thaw(restored.sources["source"].options[SOURCE_AUTHORING_KEY]) == old_options[SOURCE_AUTHORING_KEY]
    await _run_surfacer(harness.sessions, harness.session_id, reloaded)
    all_events = await harness.sessions.list_interpretation_events(harness.session_id, status="all")
    assert [(event.id, event.choice) for event in all_events] == [(event.id, event.choice) for event in prior_events]
    assert [event for event in all_events if event.kind is InterpretationKind.INVENTED_SOURCE] == source_events
    assert isinstance(materialize_state_for_execution(restored), CompositionState)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["happy", "missing_answer", "ragged_csv"])
async def test_provider_repair_preserves_typed_consumers_and_approved_source_retention(tmp_path: Path, scenario: ColourScenario) -> None:
    harness = await _harness(tmp_path)
    content = "colour\n" + "\n".join(_COLOURS) + "\n" + ("invalid-colour,unexpected-cell\n" if scenario == "ragged_csv" else "")
    state = await _author(harness, tmp_path, content)
    state = await _approve(harness, state)
    source_options = state.sources["source"].options
    assert source_options[SOURCE_AUTHORING_KEY]["content_hash"] == hashlib.sha256(content.encode()).hexdigest()
    source_requirement = next(row for row in source_options[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "invented_source")
    assert source_requirement["accepted_artifact_hash"] == source_options[SOURCE_AUTHORING_KEY]["content_hash"]
    state = await _change_retention(harness, state, tmp_path)
    state = await _change_retention(harness, state, tmp_path)
    validation = validate_pipeline_for_trained_operator(state, harness.settings, yaml_generator, session_id=str(harness.session_id))
    assert validation.is_valid, [error.message for error in validation.errors]
    executable = materialize_state_for_execution(state)
    assert isinstance(executable, CompositionState)
    output_dir = tmp_path / "outputs" / str(harness.session_id)
    run_colour_pipeline(
        tmp_path,
        yaml_generator.generate_yaml(executable),
        scenario,
        output_path=output_dir / "colour_answers.csv",
        source_error_path=output_dir / "source_failures_revised.jsonl",
        llm_error_path=output_dir / "llm_failures.jsonl",
    )

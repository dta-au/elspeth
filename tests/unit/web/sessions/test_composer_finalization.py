"""Actual V1 finalization route and retained source/budget custody controls."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.compartments import chat_ingress_input
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions import composer_finalization, composer_turn
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.schemas import MessageWithStateResponse
from elspeth.web.sessions.state_envelope import unwrap_state_column
from tests.unit.web.sessions.test_routes import _make_app, _make_composer_mock
from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job


@pytest.mark.asyncio
async def test_actual_v1_turn_calls_shared_finalizer_with_original_owners_and_durable_response(tmp_path, monkeypatch):
    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock()
    session = await service.create_session("alice", "Finalization custody", "local")
    observed = []
    original = composer_finalization.settle_composer_result

    async def settle(inputs, result):
        assert type(inputs) is composer_finalization.ComposerFinalizationInputs
        assert inputs.service is service
        assert inputs.session.id == session.id
        assert inputs.turn is job.turn
        assert inputs.lease is job.lease
        assert inputs.running is job.running
        assert inputs.observation is job.observation
        assert inputs.budget_anchor is job.budget_anchor
        assert inputs.required_work is job.observation.required_work
        assert result is job.observation.compose_result
        assert inputs.provider_binding.coordinator is inputs.required_work
        observed.append((inputs, result))
        return await original(inputs, result)

    monkeypatch.setattr(composer_turn, "settle_composer_result", settle)
    async with _running_job(app, service, session.id, kind="compose_message", content="Give a plain reply.") as job:
        terminal = await _run(app, job)
        assert len(observed) == 1
        assert terminal.status == "completed"
        assert job.observation.audit_cohort_durable
        persisted = job.authority.get(session_id=session.id, operation_id=job.turn.operation_id)
        assert persisted is not None and persisted.result_json == terminal.result_json
        response = MessageWithStateResponse.model_validate_json(persisted.result_json)
        rows = await service.get_messages(session.id, limit=None)
        assert [row.content for row in rows if str(row.id) == response.message.id] == [response.message.content]


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", (False, True))
@pytest.mark.parametrize("preflight_failure", (False, True))
async def test_finalization_snapshots_nested_provenance_and_thaws_only_at_real_write_boundaries(
    tmp_path, monkeypatch, marked, preflight_failure
):
    app, service = _make_app(tmp_path)
    changed = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=2)
    app.state.composer_service = _make_composer_mock(state=changed)
    session = await service.create_session("alice", "Provenance snapshot", "local")
    expected = []
    captured = []
    boundaries = []
    original_failure = ValueError("original post-compose preflight failure")
    actual_settle = composer_finalization.settle_composer_result
    actual_merge = composer_finalization.merge_composer_meta_updates
    actual_handler = composer_finalization._handle_runtime_preflight_failure

    def assert_plain_provenance(ingress, inputs):
        assert type(ingress) is dict and type(ingress["foreign_compartment_ids"]) is list
        assert type(inputs) is list and all(type(entry) is dict for entry in inputs)
        assert all(type(entry["foreign_compartment_ids"]) is list for entry in inputs)
        assert (ingress, inputs) == expected[0]

    def observe_merge(current, updates):
        assert_plain_provenance(updates["ingress"], updates["chat_ingress_inputs"])
        boundaries.append("metadata")
        return actual_merge(current, updates)

    async def observe_handler(*args, **kwargs):
        assert_plain_provenance(kwargs["ingress"], kwargs["chat_ingress_inputs"])
        boundaries.append("preflight")
        return await actual_handler(*args, **kwargs)

    async def settle_snapshot(inputs, result):
        borrowed_ingress = deep_thaw(inputs.chat_ingress)
        borrowed_inputs = deep_thaw(inputs.chat_ingress_inputs)
        expected.append((deep_thaw(inputs.chat_ingress), deep_thaw(inputs.chat_ingress_inputs)))
        snapshot = replace(inputs, chat_ingress=borrowed_ingress, chat_ingress_inputs=borrowed_inputs)
        assert snapshot.service is inputs.service and snapshot.session is inputs.session
        assert snapshot.lease is inputs.lease and snapshot.running is inputs.running
        assert snapshot.observation is inputs.observation and snapshot.required_work is inputs.required_work
        assert snapshot.provider_binding is inputs.provider_binding and snapshot.budget_anchor is inputs.budget_anchor
        assert (deep_thaw(snapshot.chat_ingress), deep_thaw(snapshot.chat_ingress_inputs)) == expected[0]
        assert bool(snapshot.chat_ingress["foreign_compartment_ids"]) is marked
        empty = replace(inputs, chat_ingress_inputs=[])
        assert deep_thaw(empty.chat_ingress_inputs) == [] and type(empty.chat_ingress_inputs) is tuple
        borrowed_ingress["foreign_compartment_ids"].append("later-borrowed-marking")
        borrowed_inputs[0]["foreign_compartment_ids"].append("later-borrowed-history-marking")
        borrowed_inputs.append(chat_ingress_input("late-borrowed-row", "late borrowed text", own_compartment_id=None))
        assert (deep_thaw(snapshot.chat_ingress), deep_thaw(snapshot.chat_ingress_inputs)) == expected[0]
        with pytest.raises(TypeError):
            snapshot.chat_ingress["text_sha256"] = "cannot-rewrite-snapshot"
        with pytest.raises(TypeError):
            snapshot.chat_ingress["foreign_compartment_ids"][0] = "cannot-rewrite-marking"
        with pytest.raises(TypeError):
            snapshot.chat_ingress_inputs[0]["foreign_compartment_ids"][0] = "cannot-rewrite-history"
        captured.append(snapshot)
        return await actual_settle(snapshot, result)

    monkeypatch.setattr(composer_turn, "settle_composer_result", settle_snapshot)
    monkeypatch.setattr(composer_finalization, "merge_composer_meta_updates", observe_merge)
    monkeypatch.setattr(composer_finalization, "_handle_runtime_preflight_failure", observe_handler)
    if preflight_failure:

        async def refuse_post_compose(*args, **kwargs):
            raise ComposerRuntimePreflightError(original_exc=original_failure, partial_state=None)

        monkeypatch.setattr(composer_finalization, "_state_data_from_composer_state", refuse_post_compose)
    content = ("# compartment_id: external-source\n" if marked else "") + "Retain this input provenance."
    async with _running_job(app, service, session.id, kind="compose_message", content=content) as job:
        if preflight_failure:
            with pytest.raises(HTTPException) as caught:
                await _run(app, job)
            assert caught.value.status_code == 500 and caught.value.__cause__ is original_failure
            assert boundaries == ["metadata", "preflight"]
            assert job.observation.audit_cohort_durable
            assert job.authority.get(session_id=session.id, operation_id=job.turn.operation_id).status == "running"
        else:
            terminal = await _run(app, job)
            assert terminal.status == "completed" and boundaries == ["metadata"]
            saved = await service.get_current_state(session.id)
            assert saved is not None
            with app.state.session_engine.connect() as conn:
                stored_meta = conn.execute(
                    select(composition_states_table.c.composer_meta).where(composition_states_table.c.id == str(saved.id))
                ).scalar_one()
            stored_meta = unwrap_state_column(stored_meta)
            assert_plain_provenance(stored_meta["ingress"], stored_meta["chat_ingress_inputs"])
            assert (deep_thaw(saved.composer_meta["ingress"]), deep_thaw(saved.composer_meta["chat_ingress_inputs"])) == expected[0]
            persisted = job.authority.get(session_id=session.id, operation_id=job.turn.operation_id)
            assert persisted.result_json == terminal.result_json and job.observation.audit_cohort_durable
        assert len(captured) == 1

"""Refused model writes cannot become recovery targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.turn_audit import _state_payload_for_compose_turn
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions._persist_payload import RedactedToolRow
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.graph_recovery import checkpoint_data
from elspeth.web.sessions.models import composition_states_table
from tests.unit.web.composer.test_planner_authoring_aids import _catalog_with_llm_source
from tests.unit.web.composer.test_server_staged_review_preservation import (
    CODE,
    KEY,
    _build,
    _row,
    _write,
)
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store_fixture
from tests.unit.web.coordination.test_graph_recovery_persistence import begin, finish
from tests.unit.web.sessions.test_composer_recovery_security import _checkpoint, _park


@pytest.fixture
def operation_store(tmp_path: Path):
    yield from _operation_store_fixture.__wrapped__(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset_to_turn_start", "reset_to_safe_checkpoint", "restore_parked_graph", "park"])
@pytest.mark.parametrize("mutation", ["strip_row", "remove_node", "change_plugin"])
async def test_refused_write_never_becomes_recovery_snapshot(tmp_path: Path, operation_store: Any, action: str, mutation: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    engine, repository, _authority, service, session_id = operation_store
    state_id = _checkpoint(engine, session_id, state=state)
    park_id = _park(engine, session_id, state_id) if action == "restore_parked_graph" else None
    if mutation == "strip_row":
        arguments = {"node_id": node_id, "patch": {KEY: []}}
        tool = "patch_node_options"
    elif mutation == "remove_node":
        arguments = {"id": node_id}
        tool = "remove_node"
    else:
        catalog = _catalog_with_llm_source()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        view = PolicyCatalogView.for_trained_operator(catalog, snapshot)
        node = next(item for item in state.nodes if item.id == node_id)
        arguments = {
            "id": node_id,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": node.input,
            "on_success": node.on_success,
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        }
        tool = "upsert_node"
    before = _row(state, node_id)
    with engine.connect() as conn:
        recorded_before = tuple(conn.execute(select(composition_states_table.c.id)).scalars())
    result = _write(tool, arguments, state, view, snapshot)
    tool_call_payload = _state_payload_for_compose_turn(result) if result.updated_state.version > state.version else None
    with engine.connect() as conn:
        assert tuple(conn.execute(select(composition_states_table.c.id)).scalars()) == recorded_before
    running, admission = await begin(operation_store, state_id)
    try:
        await service.persist_compose_turn_async(
            session_id=str(session_id),
            assistant_content="The requested edit was refused.",
            redacted_assistant_tool_calls=(
                {"id": f"refused-{mutation}", "type": "function", "function": {"name": tool, "arguments": "{}"}},
            ),
            redacted_tool_rows=(RedactedToolRow(f"refused-{mutation}", "{}", tool_call_payload),),
            private_tool_evidence=(
                build_private_tool_evidence(
                    session_id=str(session_id),
                    operation_id=running.session_operation_context.fence.operation_id,
                    operation_epoch=running.session_operation_context.fence.operation_epoch,
                    tool_call_id=f"refused-{mutation}",
                    tool_name=tool,
                    status="refused",
                    arguments_canonical="{}",
                    result_canonical="{}",
                ),
            ),
            parent_composition_state_id=str(state_id),
            expected_current_state_id=str(state_id),
            writer_principal="compose_loop",
            plugin_crash_pending=False,
            session_operation_context=running.session_operation_context,
        )
        with engine.connect() as conn:
            assert tuple(conn.execute(select(composition_states_table.c.id)).scalars()) == recorded_before
        assert not result.success, result.to_dict()
        assert CODE in {error.error_code for error in result.validation.errors}
        assert result.updated_state.to_dict() == state.to_dict()
        # Both projection helpers consume only the published result.
        tool_call_data = _state_payload_for_compose_turn(result).data
        post_compose_data = checkpoint_data(result.updated_state)
        for projected in (tool_call_data, post_compose_data):
            assert next(node for node in projected.nodes if node["id"] == node_id)["options"][KEY][0] == before
        settlement = await service.prepare_graph_recovery(
            session_id,
            context=admission.context,
            intent=GraphRecoveryIntent(action, "Restore the recorded draft.", park_id, f"refused-{mutation}"),
            session_operation_context=running.session_operation_context,
            candidate_state=result.updated_state if action == "park" else None,
        )
        assert settlement.failure_reason is None
        assert settlement.data is not None
        assert (await finish(operation_store, running, settlement)).status == "completed"
        latest = await service.get_current_state(session_id)
        assert latest is not None
        restored = state_from_record(latest)
        assert _row(restored, node_id) == before
        assert any(node.id == node_id for node in restored.nodes)
    finally:
        repository.release(running.session_operation_context)
    if action == "park":
        assert settlement.park_id is not None
        next_running, next_admission = await begin(operation_store, latest.id)
        try:
            restored_park = await service.prepare_graph_recovery(
                session_id,
                context=next_admission.context,
                intent=GraphRecoveryIntent(
                    "restore_parked_graph", "Restore the parked draft.", str(settlement.park_id), f"restore-{mutation}"
                ),
                session_operation_context=next_running.session_operation_context,
            )
            assert restored_park.failure_reason is None
            assert (await finish(operation_store, next_running, restored_park)).status == "completed"
            reread = await service.get_current_state(session_id)
            assert reread is not None
            assert _row(state_from_record(reread), node_id) == before
        finally:
            repository.release(next_running.session_operation_context)

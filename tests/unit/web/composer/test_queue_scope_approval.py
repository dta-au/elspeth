"""Approval previews refuse scope-bearing queues before staging proposals."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select

from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web.composer._helpers import _make_settings
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean as _stub_advisor_end_gate_clean
from tests.unit.web.composer.conftest import _fake_llm_response, _FakeComposeLLM
from tests.unit.web.composer.test_queue_scope_admission import QUEUE, SCOPE, complete_queue, private_snapshot
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{}, *({key: value} for key, value in SCOPE.items()), SCOPE])
async def test_real_explicit_approval_previews_changed_queue_without_committing_composition(
    tmp_path: Path,
    override: dict[str, Any],
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    sessions = composer_service_with_real_sessions._require_sessions_service()
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=create_catalog_service(),
        settings=_make_settings(data_dir=tmp_path),
        sessions_service=sessions,
    )
    session_id = result_session_id
    session_uuid = UUID(session_id)
    _, built, _ = complete_queue(tmp_path, session_id=session_id)
    data = built.to_dict()
    saved = await _save_composition_state(
        sessions,
        session_uuid,
        CompositionStateData(
            sources=data["sources"], nodes=data["nodes"], edges=data["edges"], outputs=data["outputs"], metadata_=data["metadata"]
        ),
        provenance="session_seed",
    )
    state = state_from_record(saved)
    before = private_snapshot(state)
    await sessions.update_composer_preferences(
        session_uuid, trust_mode="explicit_approve", density_default="high", actor="user:phase3-test-user"
    )
    arguments = {**QUEUE, "description": "Changed queue description", **override}
    llm = _FakeComposeLLM(
        (
            _fake_llm_response(tool_calls=({"id": "queue_edit", "name": "upsert_node", "arguments": arguments},)),
            _fake_llm_response(content="The queue edit has been considered."),
        )
    )
    result = await composer._run_one_turn_for_test(
        llm=llm,
        session_id=session_id,
        current_state_id=str(saved.id),
        initial_state=state,
    )
    proposals = await sessions.list_composition_proposals(session_uuid)
    assert len(result.tool_outcomes) == 1
    feedback = result.tool_outcomes[0].response
    assert isinstance(feedback, ToolResult)
    if override:
        assert proposals == []
        assert not feedback.success
        assert feedback.validation.errors[0].error_code == "queue_config_invalid"
        assert feedback.validation.errors[0].rejected_component == "node:inbound"
    else:
        assert len(proposals) == 1 and proposals[0].status == "pending"
        assert proposals[0].tool_name == "upsert_node"
        assert proposals[0].arguments_json["description"] == "Changed queue description"
    current = await sessions.get_current_state(session_uuid)
    assert current is not None and current.id == saved.id
    assert private_snapshot(state_from_record(current)) == before
    assert private_snapshot(state) == before
    assert result.tool_outcomes[0].post_version == state.version
    with sessions._engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 1

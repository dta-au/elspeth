"""The exact prompt edit proposal rechecks its captured base at acceptance."""

import asyncio
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_service import _make_llm_response, _make_settings
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state
from tests.unit.web.sessions.test_routes import _make_app

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools import execute_tool as production_execute_tool
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes.composer import proposals as proposal_routes


def _args(text: str, state_id: UUID, old: str, replacement: str) -> dict[str, object]:
    return {
        "target": {"kind": "node_option", "node_id": "code_themes", "option": "prompt_template"},
        "expected_sha256": sha256(text.encode("utf-8")).hexdigest(),
        "expected_state_id": str(state_id),
        "edits": [{"old": old, "replacement": replacement, "replace_all": False}],
    }


async def _preview_proposal(
    composer: ComposerServiceImpl,
    sessions: object,
    session_id: UUID,
    state: object,
    state_id: UUID,
    arguments: dict[str, object],
    call_id: str,
) -> object:
    snapshot, policy_catalog = composer._policy_context.build(None)
    async with acquire_compose_context(sessions, session_id) as operation_context:
        outcome, _ = await composer._dispatch_tool_batch(
            call_model=_CallModelOutcome(
                completion=_make_llm_response(tool_calls=[{"id": call_id, "name": "change_substring", "arguments": arguments}])
            ),
            state=state,
            last_validation=None,
            last_runtime_preflight=None,
            llm_messages=[],
            recorder=BufferingRecorder(),
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache={},
            session_id=str(session_id),
            session_operation_context=operation_context,
            user_id="alice",
            user_message_id=None,
            user_message_content="Edit the prompt",
            current_state_id=str(state_id),
            actor="composer-web:user:alice",
            initial_version=state.version,
            deadline=asyncio.get_running_loop().time() + 60,
            progress=None,
            session_scope="prompt-edit-approval-test",
            advisor_calls_used=0,
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=snapshot,
            policy_catalog=policy_catalog,
        )
    assert outcome.state is state
    proposals = await sessions.list_composition_proposals(session_id, status="pending")
    return next(proposal for proposal in proposals if proposal.tool_call_id == call_id)


@pytest.mark.asyncio
async def test_change_substring_preview_stale_accept_then_fresh_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service = _make_app(tmp_path)
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=_make_settings(data_dir=tmp_path), sessions_service=service, session_engine=service._engine
    )

    def trained_dispatch(name: str, arguments: dict[str, object], state: object, _route_catalog: object, **kwargs: Any) -> object:
        return production_execute_tool(name, arguments, state, policy_catalog, **{**kwargs, "plugin_snapshot": snapshot})

    monkeypatch.setattr(proposal_routes, "execute_tool", trained_dispatch)
    session = await service.create_session("alice", "Prompt edit proposals", "local")
    await service.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="user:alice")
    state = _approved_state()
    before = state.to_dict()
    saved = await _save_composition_state(
        service,
        session.id,
        CompositionStateData(
            sources=before["sources"],
            nodes=before["nodes"],
            edges=before["edges"],
            outputs=before["outputs"],
            metadata_=before["metadata"],
            is_valid=False,
        ),
        provenance="session_seed",
    )
    text = state.nodes[0].options["prompt_template"]
    first_args = _args(text, saved.id, "Classify", "Assess")
    stale_proposal = await _preview_proposal(composer, service, session.id, state, saved.id, first_args, "stale-edit")
    assert (await service.get_current_state(session.id)).id == saved.id

    advance_args = _args(text, saved.id, "Classify", "Review")
    advance = execute_tool("change_substring", advance_args, state, _mock_catalog(), current_state_id=str(saved.id))
    assert advance.success
    advanced = advance.updated_state.to_dict()
    current = await _save_composition_state(
        service,
        session.id,
        CompositionStateData(
            sources=advanced["sources"],
            nodes=advanced["nodes"],
            edges=advanced["edges"],
            outputs=advanced["outputs"],
            metadata_=advanced["metadata"],
            is_valid=False,
        ),
        provenance="tool_call",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        stale = await client.post(f"/api/sessions/{session.id}/proposals/{stale_proposal.id}/accept")
        assert stale.status_code == 409, stale.text
        assert (await service.get_current_state(session.id)).id == current.id
        rejected = await client.post(f"/api/sessions/{session.id}/proposals/{stale_proposal.id}/reject", json={})
        assert rejected.status_code == 200, rejected.text

        current_text = advance.updated_state.nodes[0].options["prompt_template"]
        fresh_args = _args(current_text, current.id, "Review", "Assess")
        fresh_proposal = await _preview_proposal(composer, service, session.id, advance.updated_state, current.id, fresh_args, "fresh-edit")
        accepted = await client.post(f"/api/sessions/{session.id}/proposals/{fresh_proposal.id}/accept")
        assert accepted.status_code == 200, accepted.text
    final = await service.get_current_state(session.id)
    assert final is not None and final.id != current.id

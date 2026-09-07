"""Operation authority must survive production tool and planner constructors."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools import _dispatch
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from tests.unit.web.composer.test_pipeline_planner import (
    _custody,
    _empty_state,
    _inline_pipeline,
    _plan,
    _response,
    _ScriptedCompletion,
    _session_context,
)


def test_proof_check_without_blob_effects_remains_available_without_sessions(
    composer_service_without_sessions_service: ComposerServiceImpl,
) -> None:
    messages: list[dict[str, Any]] = []
    outcome = composer_service_without_sessions_service._attempt_proof_repair(
        state=_empty_state(), llm_messages=messages, session_id=None, repair_turns_used=0
    )
    assert outcome.action == "clear"
    assert messages == []


def test_dispatch_preserves_operation_context_and_authority(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SessionOperationContext(
        SessionOperationFence(str(uuid4()), str(uuid4()), str(uuid4()), 1),
        SessionOperationKind.COMPOSE,
    )
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    authority = SQLiteLocalSessionOperationAuthority(engine)
    observed: list[ToolContext] = []
    handler = _dispatch._DISCOVERY_TOOLS["get_pipeline_state"]

    def observe(arguments: dict[str, Any], state: pipeline_planner.CompositionState, request_context: ToolContext) -> ToolResult:
        observed.append(request_context)
        return handler(arguments, state, request_context)

    monkeypatch.setattr(_dispatch, "_DISCOVERY_TOOLS", {**_dispatch._DISCOVERY_TOOLS, "get_pipeline_state": observe})
    try:
        result = _dispatch.execute_tool(
            "get_pipeline_state",
            {},
            _empty_state(),
            tool_context.catalog,
            plugin_snapshot=tool_context.plugin_snapshot,
            session_id=context.fence.session_id,
            session_operation_context=context,
            session_operation_authority=authority,
        )
        assert result.success
        assert len(observed) == 1
        assert observed[0].session_operation_context is context
        assert observed[0].session_operation_authority is authority
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_planner_preserves_authority_through_discovery_and_inline_publication(
    tmp_path: Path,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, origin = await _session_context()
    authority = SQLiteLocalSessionOperationAuthority(engine)
    context = authority.acquire(
        session_id=UUID(origin.session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="producer-regression",
        lease_seconds=120,
    )
    observed: list[ToolContext] = []
    discovery = pipeline_planner.execute_discovery_tool_with_context

    def observe(
        tool_name: str, arguments: dict[str, Any], state: pipeline_planner.CompositionState, request_context: ToolContext
    ) -> ToolResult:
        observed.append(request_context)
        return discovery(tool_name, arguments, state, request_context)

    monkeypatch.setattr(pipeline_planner, "execute_discovery_tool_with_context", observe)
    try:
        proposal = await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=_ScriptedCompletion(
                _response(("list_blobs", {})),
                _response(("emit_pipeline_proposal", {"pipeline": _inline_pipeline(tmp_path)})),
            ),
            originating_message=origin,
            custody_config=replace(
                _custody(tmp_path),
                session_engine=engine,
                session_operation_context=context,
                session_operation_authority=authority,
            ),
        )
        assert observed
        assert all(item.session_operation_context is context for item in observed)
        assert all(item.session_operation_authority is authority for item in observed)
        assert proposal.proposal.to_dict()["pipeline"]["source"]["blob_id"]
    finally:
        authority.release(context)
        engine.dispose()

"""Operation authority must survive production tool and planner constructors."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools import _dispatch
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.composer.tools.generation import ProofNotApplicable
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from tests.unit.web.composer._scripted_sdk import (
    _empty_state,
)


def test_proof_check_without_blob_effects_remains_available_without_sessions(
    composer_service_without_sessions_service: ComposerServiceImpl,
) -> None:
    assert composer_service_without_sessions_service._sessions_service is None
    proof = composer_service_without_sessions_service._completion._assess_source_proof(
        _empty_state(), session_id=None, session_operation_context=None
    )
    assert type(proof) is ProofNotApplicable


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

"""Detached failure settlement cannot certify an unpersisted audit cohort."""

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _persist_llm_calls, _persist_tool_invocations, _persist_turn_audit_cohort
from tests.unit.web.sessions.test_turn_audit_cohort import _CohortCapturingService, _compose_context, _llm_call, _tool_invocation


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("llm", "tool", "cohort"))
async def test_required_unwind_audit_storage_failure_is_not_swallowed(kind: str) -> None:
    service = cast(SessionServiceProtocol, _CohortCapturingService(raise_on_call=OperationalError("INSERT", {}, Exception("db down"))))
    sid = uuid4()
    with pytest.raises(AuditIntegrityError):
        if kind == "llm":
            await _persist_llm_calls(
                service,
                sid,
                (_llm_call(),),
                None,
                plugin_crash_pending=True,
                required_audit=True,
                session_operation_context=_compose_context(sid),
            )
        elif kind == "tool":
            await _persist_tool_invocations(
                service,
                sid,
                (_tool_invocation(),),
                None,
                plugin_crash_pending=True,
                required_audit=True,
                session_operation_context=_compose_context(sid),
            )
        else:
            await _persist_turn_audit_cohort(
                service,
                sid,
                (_tool_invocation(),),
                (_llm_call(),),
                tool_composition_state_id=None,
                llm_composition_state_id=None,
                plugin_crash_pending=True,
                required_audit=True,
                session_operation_context=_compose_context(sid),
            )

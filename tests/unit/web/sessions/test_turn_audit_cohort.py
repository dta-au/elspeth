"""Mechanism coverage for :func:`_persist_turn_audit_cohort`.

One turn's tool rows and LLM sidecars must reach the service as ONE
``add_messages_atomic`` cohort (elspeth-90231248dc) — not as two
independently-committing transactions — while each draft group keeps its
own composition-state binding via the per-draft override.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _persist_turn_audit_cohort


@dataclass
class _CapturedCohort:
    session_id: UUID
    drafts: tuple[AuditMessageDraft, ...]
    writer_principal: str
    composition_state_id: UUID | None


@dataclass
class _CohortCapturingService:
    """Records each ``add_messages_atomic`` CALL, never flattening the cohort."""

    cohorts: list[_CapturedCohort] = field(default_factory=list)
    raise_on_call: Exception | None = None

    async def add_messages_atomic(
        self,
        session_id: UUID,
        drafts: Sequence[AuditMessageDraft],
        *,
        writer_principal: str,
        composition_state_id: UUID | None = None,
    ) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.cohorts.append(
            _CapturedCohort(
                session_id=session_id,
                drafts=tuple(drafts),
                writer_principal=writer_principal,
                composition_state_id=composition_state_id,
            )
        )


def _tool_invocation(tool_name: str = "preview_pipeline") -> ComposerToolInvocation:
    arguments_canonical = canonical_json({"detail": "args"})
    result_canonical = canonical_json({"status": "SUCCESS"})
    return ComposerToolInvocation(
        tool_call_id=f"call_{tool_name}_1",
        tool_name=tool_name,
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode("utf-8")).hexdigest(),
        result_canonical=result_canonical,
        result_hash=hashlib.sha256(result_canonical.encode("utf-8")).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=3,
        version_after=3,
        started_at=datetime(2026, 8, 8, tzinfo=UTC),
        finished_at=datetime(2026, 8, 8, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )


def _llm_call():
    return build_llm_call_record(
        model_requested="test/model",
        messages=[{"role": "user", "content": "prompt"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime(2026, 8, 8, tzinfo=UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        error_class=None,
        error_message=None,
    )


@pytest.mark.asyncio
async def test_tool_and_llm_groups_settle_as_one_cohort_with_their_own_state_ids() -> None:
    service = _CohortCapturingService()
    session_id = uuid4()
    post_state_id = uuid4()
    pre_state_id = uuid4()
    assistant_id = uuid4()

    await _persist_turn_audit_cohort(
        cast(SessionServiceProtocol, service),
        session_id,
        (_tool_invocation(),),
        (_llm_call(),),
        tool_composition_state_id=post_state_id,
        llm_composition_state_id=pre_state_id,
        parent_assistant_id=assistant_id,
        plugin_crash_pending=False,
    )

    # ONE service call for the whole turn — the mechanism under test.
    (cohort,) = service.cohorts
    assert cohort.session_id == session_id
    assert cohort.writer_principal == "compose_loop"
    # The cohort-level state id stays None; each group binds per-draft.
    assert cohort.composition_state_id is None
    tool_draft, llm_draft = cohort.drafts
    assert tool_draft.role == "tool"
    assert tool_draft.tool_call_id == "call_preview_pipeline_1"
    assert tool_draft.parent_assistant_id == str(assistant_id)
    assert tool_draft.composition_state_id == str(post_state_id)
    assert llm_draft.role == "audit"
    assert llm_draft.tool_call_id is None
    assert llm_draft.parent_assistant_id is None
    assert llm_draft.composition_state_id == str(pre_state_id)
    (envelope,) = llm_draft.tool_calls or ()
    assert envelope["_kind"] == "llm_call_audit"


@pytest.mark.asyncio
async def test_no_parent_assistant_uses_audit_role_for_tool_rows() -> None:
    service = _CohortCapturingService()

    await _persist_turn_audit_cohort(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (_tool_invocation(),),
        (),
        tool_composition_state_id=None,
        llm_composition_state_id=None,
        plugin_crash_pending=True,
    )

    ((tool_draft,),) = [c.drafts for c in service.cohorts]
    assert tool_draft.role == "audit"
    assert tool_draft.tool_call_id is None
    assert tool_draft.parent_assistant_id is None


@pytest.mark.asyncio
async def test_both_groups_empty_is_a_noop() -> None:
    service = _CohortCapturingService()

    result = await _persist_turn_audit_cohort(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (),
        (),
        tool_composition_state_id=uuid4(),
        llm_composition_state_id=uuid4(),
        plugin_crash_pending=False,
    )

    assert result == ()
    assert service.cohorts == []


@pytest.mark.asyncio
async def test_unwind_failure_swallows_and_returns_no_bindings() -> None:
    service = _CohortCapturingService(raise_on_call=OperationalError("INSERT", {}, Exception("db down")))

    result = await _persist_turn_audit_cohort(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (_tool_invocation(),),
        (_llm_call(),),
        tool_composition_state_id=None,
        llm_composition_state_id=None,
        plugin_crash_pending=True,
    )

    assert result == ()


@pytest.mark.asyncio
async def test_success_path_failure_is_tier1_audit_corruption() -> None:
    service = _CohortCapturingService(raise_on_call=OperationalError("INSERT", {}, Exception("db down")))

    with pytest.raises(AuditIntegrityError, match="composer_turn_audit_cohort_persist_failed"):
        await _persist_turn_audit_cohort(
            cast(SessionServiceProtocol, service),
            uuid4(),
            (_tool_invocation(),),
            (_llm_call(),),
            tool_composition_state_id=None,
            llm_composition_state_id=None,
            plugin_crash_pending=False,
        )

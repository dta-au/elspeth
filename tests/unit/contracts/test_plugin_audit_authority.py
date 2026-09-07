"""Authority forwarding and context lifetime at the plugin boundary.

These subjects use mock repositories; their explicit tokens are mock-only
identities, never authority presented to a Landscape database.
"""

from typing import cast
from unittest.mock import Mock

import pytest

from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_context import plugin_context_scope
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.plugins.infrastructure.clients.base import AuditedClientBase
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from tests.fixtures.factories import make_context


def test_follower_readiness_forwards_actual_membership_without_leader() -> None:
    ctx = make_context(run_id="run-1")
    follower = WorkerMembershipToken(run_id="run-1", worker_id="follower-1")
    ctx.coordination_token = None
    ctx.member_token = follower

    ctx.record_readiness_check(name="rag", collection="docs", reachable=True, count=3, message="ready")

    ctx.landscape.record_readiness_check.assert_called_once_with(
        name="rag", collection="docs", reachable=True, count=3, message="ready", member_token=follower
    )
    assert ctx.landscape.record_readiness_check.call_args.kwargs["member_token"] is follower


def test_audit_enabled_context_without_membership_refuses_readiness() -> None:
    ctx = make_context()
    ctx.coordination_token = None
    ctx.member_token = None

    with pytest.raises(FrameworkBugError, match="member token"):
        ctx.record_readiness_check(name="rag", collection="docs", reachable=True, count=3, message="ready")

    ctx.landscape.record_readiness_check.assert_not_called()


def test_row_claim_scope_restores_authority_after_exception() -> None:
    ctx = make_context()
    claim = Mock(spec=TokenWorkItem)
    ctx.work_item = None

    with pytest.raises(RuntimeError, match="plugin failed"), plugin_context_scope(ctx, work_item=claim):
        assert ctx.require_work_item() is claim
        cloned = ctx.for_contract(None)
        assert cloned.require_work_item() is claim
        raise RuntimeError("plugin failed")

    assert ctx.work_item is None
    assert cloned.require_work_item() is claim


def test_row_client_forwards_captured_claim_and_membership() -> None:
    recorder = Mock(spec=CallRecorder)
    recorder.allocate_call_index.return_value = 7
    member = WorkerMembershipToken(run_id="run-1", worker_id="follower-1")
    claim = Mock(spec=TokenWorkItem)
    client = AuditedClientBase(recorder, "state-1", "run-1", lambda event: None, member_token=member, work_item=claim)

    assert client._next_call_index() == 7

    recorder.allocate_call_index.assert_called_once_with("state-1", member_token=member, work_item=claim)
    assert recorder.allocate_call_index.call_args.kwargs["member_token"] is member
    assert recorder.allocate_call_index.call_args.kwargs["work_item"] is claim


def test_row_client_refuses_missing_claim_before_repository_call() -> None:
    recorder = Mock(spec=CallRecorder)
    member = WorkerMembershipToken(run_id="run-1", worker_id="follower-1")
    client = AuditedClientBase(recorder, "state-1", "run-1", lambda event: None, member_token=member)

    with pytest.raises(FrameworkBugError, match="claimed work item"):
        client._next_call_index()

    recorder.allocate_call_index.assert_not_called()


def test_llm_operation_parent_forwards_leader_authority() -> None:
    recorder = Mock(spec=CallRecorder)
    recorder.allocate_operation_call_index.return_value = 4
    token = CoordinationToken(run_id="run-1", worker_id="leader-1", leader_epoch=2)
    parent = LLMAuditParent.for_operation(operation_id="operation-1", coordination_token=token)

    assert parent.allocate_call_index(recorder) == 4

    recorder.allocate_operation_call_index.assert_called_once_with("operation-1", coordination_token=token)
    assert recorder.allocate_operation_call_index.call_args.kwargs["coordination_token"] is token


def test_mutated_context_rejects_non_nominal_member_before_readiness_write() -> None:
    ctx = make_context()
    ctx.member_token = cast(WorkerMembershipToken, object())

    with pytest.raises(FrameworkBugError, match="member token"):
        ctx.record_readiness_check(name="rag", collection="docs", reachable=True, count=3, message="ready")

    ctx.landscape.record_readiness_check.assert_not_called()


def test_row_client_rejects_non_nominal_claim_before_repository_call() -> None:
    recorder = Mock(spec=CallRecorder)
    member = WorkerMembershipToken(run_id="run-1", worker_id="follower-1")
    client = AuditedClientBase(
        recorder, "state-1", "run-1", lambda event: None, member_token=member, work_item=cast(TokenWorkItem, object())
    )

    with pytest.raises(FrameworkBugError, match="claimed work item"):
        client._next_call_index()

    recorder.allocate_call_index.assert_not_called()


def test_llm_operation_parent_rejects_non_nominal_leader_before_repository_call() -> None:
    recorder = Mock(spec=CallRecorder)
    parent = LLMAuditParent.for_operation(operation_id="operation-1", coordination_token=cast(CoordinationToken, object()))

    with pytest.raises(RuntimeError, match="leader authority"):
        parent.allocate_call_index(recorder)

    recorder.allocate_operation_call_index.assert_not_called()

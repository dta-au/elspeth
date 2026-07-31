"""Tests for session record dataclasses and protocol definition."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast, get_args
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import (
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationKind,
)
from elspeth.web.sessions import protocol as session_protocol
from elspeth.web.sessions.protocol import (
    LEGAL_RUN_TRANSITIONS,
    OPERATOR_COMPLETION_RUN_STATUS_VALUES,
    SESSION_RUN_STATUS_VALUES,
    SESSION_TERMINAL_RUN_STATUS_VALUES,
    AuditAccessLogAuthority,
    ChatMessageRecord,
    CompositionStateData,
    CompositionStateRecord,
    GuidedOperationFence,
    GuidedPipelineProposalAcceptCommand,
    RunAlreadyActiveError,
    RunEventRecord,
    RunRecord,
    SessionForkAuthority,
    SessionForkChildCreation,
    SessionForkCreationTransaction,
    SessionForkParentAuthority,
    SessionOperationAuthority,
    SessionRecord,
    SessionRunStatus,
)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("id", "not-a-uuid"),
        ("run_id", "not-a-uuid"),
        ("sequence", True),
        ("sequence", 0),
        ("timestamp", datetime.now(UTC).replace(tzinfo=None)),
        ("event_type", "unknown"),
        ("data", []),
    ),
)
def test_run_event_record_rejects_malformed_audit_values(field: str, value: object) -> None:
    values: dict[str, object] = {
        "id": uuid4(),
        "run_id": uuid4(),
        "sequence": 1,
        "timestamp": datetime.now(UTC),
        "event_type": "progress",
        "data": {},
    }
    values[field] = value

    with pytest.raises(AuditIntegrityError):
        RunEventRecord(**values)  # type: ignore[arg-type]


def test_run_event_record_normalizes_time_and_detaches_nested_payload() -> None:
    payload = {"nested": {"items": [1, 2]}}
    record = RunEventRecord(
        id=uuid4(),
        run_id=uuid4(),
        sequence=1,
        timestamp=datetime.now(timezone(timedelta(hours=9))),
        event_type="progress",
        data=payload,
    )

    payload["nested"]["items"][0] = 9
    assert record.timestamp.tzinfo is UTC
    assert record.data["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        record.data["nested"]["items"][0] = 9  # type: ignore[index]


def _session_context(
    session_id: str,
    *,
    operation_id: str = "session-operation",
    epoch: int = 2,
    kind: SessionOperationKind = SessionOperationKind.SESSION_FORK,
) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id,
            operation_id=operation_id,
            lease_token=f"lease-{session_id}",
            operation_epoch=epoch,
        ),
        operation_kind=kind,
    )


def _guided_fence(session_id: str, *, operation_id: str = "guided-operation") -> GuidedOperationFence:
    return GuidedOperationFence(
        session_id=uuid4().__class__(session_id),
        operation_id=operation_id,
        lease_token="guided-lease",
        attempt=1,
    )


def test_session_fork_parent_authority_is_exact_immutable_and_cross_bound() -> None:
    parent_id = str(uuid4())
    guided = _guided_fence(parent_id)
    authority = SessionForkParentAuthority(
        parent_context=_session_context(parent_id),
        guided_fence=guided,
    )

    assert authority.parent_context.fence.session_id == parent_id
    assert authority.guided_fence is guided
    with pytest.raises(AttributeError):
        authority.guided_fence = guided  # type: ignore[misc]
    with pytest.raises(AuditIntegrityError, match="same parent session"):
        SessionForkParentAuthority(
            parent_context=_session_context(parent_id),
            guided_fence=_guided_fence(str(uuid4())),
        )


def test_session_fork_parent_authority_requires_exact_session_fork_context() -> None:
    parent_id = str(uuid4())
    with pytest.raises(AuditIntegrityError, match="SESSION_FORK"):
        SessionForkParentAuthority(
            parent_context=_session_context(parent_id, kind=SessionOperationKind.ARCHIVE),
            guided_fence=_guided_fence(parent_id),
        )
    with pytest.raises(AuditIntegrityError, match="exact"):
        SessionForkParentAuthority(  # type: ignore[arg-type]
            parent_context=object(),
            guided_fence=_guided_fence(parent_id),
        )


def test_session_fork_authority_exactly_binds_parent_child_and_guided() -> None:
    parent_id = str(uuid4())
    child_id = str(uuid4())
    parent = SessionForkParentAuthority(
        parent_context=_session_context(parent_id),
        guided_fence=_guided_fence(parent_id),
    )
    authority = SessionForkAuthority(
        parent=parent,
        child_context=_session_context(child_id),
    )

    assert authority.parent is parent
    assert authority.child_context.fence.session_id == child_id
    with pytest.raises(AuditIntegrityError, match="different sessions"):
        SessionForkAuthority(parent=parent, child_context=_session_context(parent_id))
    with pytest.raises(AuditIntegrityError, match="epoch 2"):
        SessionForkAuthority(
            parent=parent,
            child_context=_session_context(child_id, epoch=1),
        )


def test_guided_pipeline_accept_command_has_no_dead_proposal_projection() -> None:
    assert "proposal_payload" not in inspect.signature(GuidedPipelineProposalAcceptCommand).parameters


def test_session_operation_authority_exposes_context_as_sole_action_capability() -> None:
    SessionForkChildMutations = session_protocol.SessionForkChildMutations
    SessionForkParentGuidedMutations = session_protocol.SessionForkParentGuidedMutations
    acquire = inspect.signature(SessionOperationAuthority.acquire)
    assert acquire.return_annotation == "SessionOperationContext"

    for method_name in ("renew", "compare_and_swap", "mutate", "release", "archive_delete"):
        signature = inspect.signature(getattr(SessionOperationAuthority, method_name))
        assert "context" in signature.parameters
        assert "fence" not in signature.parameters

    assert inspect.signature(SessionOperationAuthority.renew).return_annotation == "SessionOperationContext"
    reconciliation = inspect.signature(SessionOperationAuthority.reconcile_archive_delete)
    assert reconciliation.return_annotation == "ArchiveDeleteReconciliation"
    assert tuple(reconciliation.parameters) == ("self", "context")

    fork_creation = inspect.signature(SessionOperationAuthority.mutate_fork_creation)
    assert "connection" not in fork_creation.parameters
    assert "engine" not in fork_creation.parameters
    assert tuple(fork_creation.parameters) == ("self", "parent_authority", "child", "mutation")
    assert fork_creation.parameters["mutation"].annotation == ("Callable[[SessionForkCreationTransaction, SessionForkAuthority], T]")

    transaction_surface = {
        name for name, value in inspect.getmembers(SessionForkCreationTransaction, inspect.isfunction) if not name.startswith("_")
    }
    assert transaction_surface == {
        "count_parent_proposal_terminal_events",
        "read_child_snapshot",
        "read_parent_guided_root_authority",
        "read_parent_message",
        "read_parent_proposal",
        "read_parent_proposal_creation_events",
        "read_parent_ready_blobs",
        "read_parent_session",
        "read_parent_state",
        "require_parent_guided_operation",
    }
    assert isinstance(SessionForkCreationTransaction.__dict__["child_mutations"], property)
    assert isinstance(SessionForkCreationTransaction.__dict__["parent_guided_mutations"], property)
    assert "execute" not in transaction_surface

    child_surface = {name for name, value in inspect.getmembers(SessionForkChildMutations, inspect.isfunction) if not name.startswith("_")}
    assert child_surface == {"append_child_messages", "insert_child_state"}
    parent_guided_surface = {
        name for name, value in inspect.getmembers(SessionForkParentGuidedMutations, inspect.isfunction) if not name.startswith("_")
    }
    assert parent_guided_surface == {"bind_guided_fork"}
    assert tuple(inspect.signature(SessionForkParentGuidedMutations.bind_guided_fork).parameters) == (
        "self",
        "originating_message_id",
    )

    for protocol in (SessionForkChildMutations, SessionForkParentGuidedMutations):
        assert getattr(protocol, "_is_runtime_protocol", False) is False
        with pytest.raises(TypeError, match="runtime_checkable"):
            isinstance(object(), protocol)
    assert getattr(SessionForkCreationTransaction, "_is_runtime_protocol", False) is True

    forbidden_names = {"connection", "engine", "execute"}
    for protocol in (SessionForkCreationTransaction, SessionForkChildMutations, SessionForkParentGuidedMutations):
        assert forbidden_names.isdisjoint(protocol.__dict__)


def test_audit_access_log_authority_is_handle_free_and_pins_server_owned_fields() -> None:
    signature = inspect.signature(AuditAccessLogAuthority.record_audit_grade_view)

    assert tuple(signature.parameters) == (
        "self",
        "session_id",
        "requesting_principal",
        "auth_provider_type",
        "request_path",
        "query_args",
        "ip_address",
    )
    assert "engine" not in signature.parameters
    assert "connection" not in signature.parameters
    assert "timestamp" not in signature.parameters
    assert "writer_principal" not in signature.parameters
    assert signature.return_annotation == "AuditAccessLogRecord"


def test_fork_creation_transaction_server_mints_child_authority() -> None:
    fork_creation = inspect.signature(SessionOperationAuthority.mutate_fork_creation)
    assert "candidate_child_session_id" not in fork_creation.parameters
    assert "owner_instance_id" not in fork_creation.parameters
    assert "lease_seconds" not in fork_creation.parameters
    assert {"candidate_child_session_id", "owner_instance_id", "lease_seconds"}.isdisjoint(
        inspect.signature(SessionForkChildCreation).parameters
    )


def _run_record(**overrides: object) -> RunRecord:
    data = {
        "id": uuid4(),
        "session_id": uuid4(),
        "state_id": uuid4(),
        "status": "running",
        "started_at": datetime.now(UTC),
        "finished_at": None,
        "rows_processed": 0,
        "rows_succeeded": 0,
        "rows_failed": 0,
        "rows_routed_success": 0,
        "rows_routed_failure": 0,
        "rows_quarantined": 0,
        "error": None,
        "landscape_run_id": None,
        "pipeline_yaml": None,
    }
    data.update(overrides)
    return RunRecord(**data)  # type: ignore[arg-type]


class TestSessionRecord:
    def test_frozen_immutability(self) -> None:
        record = SessionRecord(
            id=uuid4(),
            user_id="alice",
            auth_provider_type="local",
            title="Test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        with pytest.raises(AttributeError):
            record.title = "Changed"  # type: ignore[misc]


class TestChatMessageRecord:
    def test_tool_calls_frozen_when_present(self) -> None:
        record = ChatMessageRecord(
            id=uuid4(),
            session_id=uuid4(),
            role="assistant",
            content="Hello",
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "search", "arguments": '{"q":"test"}'}}],
            created_at=datetime.now(UTC),
            writer_principal="compose_loop",
        )
        with pytest.raises(TypeError):
            record.tool_calls[0]["new_key"] = "value"  # type: ignore[index]

    def test_tool_calls_none_is_fine(self) -> None:
        record = ChatMessageRecord(
            id=uuid4(),
            session_id=uuid4(),
            role="user",
            content="Hello",
            tool_calls=None,
            created_at=datetime.now(UTC),
            writer_principal="route_user_message",
        )
        assert record.tool_calls is None

    def test_invalid_role_raises_audit_integrity_error(self) -> None:
        with pytest.raises(AuditIntegrityError, match=r"chat_messages\.role is 'root'"):
            ChatMessageRecord(
                id=uuid4(),
                session_id=uuid4(),
                role="root",  # type: ignore[arg-type]
                content="Hello",
                tool_calls=None,
                created_at=datetime.now(UTC),
                writer_principal="compose_loop",
            )

    def test_audit_role_is_accepted(self) -> None:
        """Rev-4: ``audit`` is a valid internal-only role for breadcrumb rows
        that have no real assistant parent."""
        record = ChatMessageRecord(
            id=uuid4(),
            session_id=uuid4(),
            role="audit",
            content='{"_kind":"llm_call_audit","status":"ok"}',
            tool_calls=None,
            created_at=datetime.now(UTC),
            writer_principal="compose_loop",
        )
        assert record.role == "audit"

    def test_tool_call_linkage_fields_are_exposed(self) -> None:
        """Rev-4: ``tool_call_id`` and ``parent_assistant_id`` are scalar
        linkage fields on the record. They must be accessible without
        requiring a freeze guard (CLAUDE.md "Scalar-Only Fields Need No
        Guard")."""
        parent_id = uuid4()
        record = ChatMessageRecord(
            id=uuid4(),
            session_id=uuid4(),
            role="tool",
            content='{"ok":true}',
            tool_calls=None,
            created_at=datetime.now(UTC),
            writer_principal="compose_loop",
            tool_call_id="call_abc",
            parent_assistant_id=parent_id,
        )
        assert record.tool_call_id == "call_abc"
        assert record.parent_assistant_id == parent_id

    def test_writer_principal_is_required(self) -> None:
        """Rev-4 breaking change: ``writer_principal`` has no default; it
        must be supplied at construction time so that fork-copy and
        ``get_messages`` hydration cannot silently fabricate provenance."""
        with pytest.raises(TypeError, match="writer_principal"):
            ChatMessageRecord(  # type: ignore[call-arg]
                id=uuid4(),
                session_id=uuid4(),
                role="user",
                content="hi",
                created_at=datetime.now(UTC),
            )


class TestCompositionStateData:
    def test_mutable_inputs_are_frozen(self) -> None:
        source = {"type": "csv", "path": "/data/test.csv"}
        nodes = [{"id": "n1", "type": "source"}]
        data = CompositionStateData(
            source=source,
            nodes=nodes,
            is_valid=True,
        )
        # Original dicts should not affect the frozen copy
        source["type"] = "json"
        assert data.sources is not None
        assert data.sources["source"]["type"] == "csv"
        # Frozen containers should reject mutation
        with pytest.raises(TypeError):
            cast(Any, data.sources["source"])["new_key"] = "value"
        with pytest.raises((TypeError, AttributeError)):
            data.nodes.append({"id": "n2"})  # type: ignore[union-attr]

    def test_none_fields_not_frozen(self) -> None:
        data = CompositionStateData(is_valid=False)
        assert data.sources is None
        assert data.nodes is None

    def test_frozen_immutability(self) -> None:
        data = CompositionStateData(is_valid=True)
        with pytest.raises(AttributeError):
            data.is_valid = False  # type: ignore[misc]


class TestCompositionStateRecord:
    def test_mutable_fields_are_frozen(self) -> None:
        record = CompositionStateRecord(
            id=uuid4(),
            session_id=uuid4(),
            version=1,
            source={"type": "csv"},
            nodes=[{"id": "n1"}],
            edges=None,
            outputs=None,
            metadata_=None,
            is_valid=True,
            validation_errors=None,
            created_at=datetime.now(UTC),
            derived_from_state_id=None,
        )
        with pytest.raises(TypeError):
            record.source["new"] = "value"  # type: ignore[index]


class TestRunRecord:
    def test_frozen_immutability(self) -> None:
        record = _run_record()
        with pytest.raises(AttributeError):
            record.status = "completed"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "field_name",
        [
            "rows_processed",
            "rows_succeeded",
            "rows_failed",
            "rows_routed_success",
            "rows_routed_failure",
            "rows_quarantined",
        ],
    )
    def test_run_counters_reject_negative_values(self, field_name: str) -> None:
        with pytest.raises(AuditIntegrityError, match=rf"runs\.{field_name} must be >= 0"):
            _run_record(**{field_name: -1})

    @pytest.mark.parametrize(
        ("field_name", "bad_value", "type_name"),
        [
            ("rows_processed", True, "bool"),
            ("rows_succeeded", "1", "str"),
            ("rows_failed", 1.0, "float"),
            ("rows_routed_success", False, "bool"),
            ("rows_routed_failure", "0", "str"),
            ("rows_quarantined", 0.5, "float"),
        ],
    )
    def test_run_counters_reject_non_integer_values(self, field_name: str, bad_value: object, type_name: str) -> None:
        with pytest.raises(AuditIntegrityError, match=rf"runs\.{field_name} must be int, got {type_name}"):
            _run_record(**{field_name: bad_value})

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"rows_succeeded": 1, "rows_routed_success": 2}, "rows_routed_success must be a subset of rows_succeeded"),
            ({"rows_failed": 1, "rows_routed_failure": 2}, "rows_routed_failure must be a subset of rows_failed"),
            ({"rows_failed": 1, "rows_quarantined": 2}, "rows_quarantined must be a subset of rows_failed"),
        ],
    )
    def test_routed_and_quarantine_counters_must_be_subsets(self, overrides: dict[str, int], message: str) -> None:
        with pytest.raises(AuditIntegrityError, match=message):
            _run_record(**overrides)

    def test_run_status_literal_and_transition_table_share_one_source_of_truth(self) -> None:
        assert frozenset(get_args(SessionRunStatus)) == SESSION_RUN_STATUS_VALUES
        assert frozenset(LEGAL_RUN_TRANSITIONS.keys()) == SESSION_RUN_STATUS_VALUES
        assert all(allowed.issubset(SESSION_RUN_STATUS_VALUES) for allowed in LEGAL_RUN_TRANSITIONS.values())
        # Phase 2.2 (elspeth-0de989c56d): four-value terminal taxonomy.
        # `completed_with_failures` and `empty` join `completed` / `failed` /
        # `cancelled` as terminal states so operators can distinguish "ran
        # cleanly" from "ran but no row succeeded".
        assert frozenset({"completed", "completed_with_failures", "failed", "empty", "cancelled"}) == SESSION_TERMINAL_RUN_STATUS_VALUES
        assert frozenset({"completed", "completed_with_failures", "empty"}) == OPERATOR_COMPLETION_RUN_STATUS_VALUES
        assert OPERATOR_COMPLETION_RUN_STATUS_VALUES.issubset(SESSION_TERMINAL_RUN_STATUS_VALUES)

    def test_running_can_transition_to_new_terminal_states(self) -> None:
        """Phase 2.2: `running` MUST be able to transition to every terminal state."""
        running_targets = LEGAL_RUN_TRANSITIONS["running"]
        # The four-value taxonomy plus cancelled.
        for terminal in ("completed", "completed_with_failures", "failed", "empty", "cancelled"):
            assert terminal in running_targets, f"running must allow transition to {terminal!r}"

    def test_pending_can_transition_to_empty(self) -> None:
        """Phase 2.2: a run that begins and immediately finds an empty source
        skips ``running`` — pending must allow direct transition to ``empty``.
        """
        # Plus failed/cancelled which were already there.
        pending_targets = LEGAL_RUN_TRANSITIONS["pending"]
        assert "empty" in pending_targets

    def test_completed_with_failures_requires_landscape_run_id(self) -> None:
        """Same audit invariant as `completed` — the run executed, so the
        Landscape ID exists.  A future read-back without it would mean the
        engine wrote audit data we cannot correlate to the session."""
        with pytest.raises(AuditIntegrityError, match="landscape_run_id"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="completed_with_failures",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                rows_processed=10,
                rows_succeeded=7,
                rows_failed=3,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )

    def test_empty_requires_landscape_run_id(self) -> None:
        """The `empty` outcome is recorded only when the source ran and emitted
        zero rows — the run still produced a Landscape audit record."""
        with pytest.raises(AuditIntegrityError, match="landscape_run_id"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="empty",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )

    def test_completed_with_failures_terminal_requires_finished_at(self) -> None:
        with pytest.raises(AuditIntegrityError, match="finished_at"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="completed_with_failures",
                started_at=datetime.now(UTC),
                finished_at=None,
                rows_processed=10,
                rows_succeeded=7,
                rows_failed=3,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id="landscape-1",
                pipeline_yaml=None,
            )

    def test_empty_terminal_requires_finished_at(self) -> None:
        with pytest.raises(AuditIntegrityError, match="finished_at"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="empty",
                started_at=datetime.now(UTC),
                finished_at=None,
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id="landscape-1",
                pipeline_yaml=None,
            )

    def test_completed_with_failures_legal_construction(self) -> None:
        """Smoke test: a fully-populated COMPLETED_WITH_FAILURES record constructs."""
        record = RunRecord(
            id=uuid4(),
            session_id=uuid4(),
            state_id=uuid4(),
            status="completed_with_failures",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            rows_processed=10,
            rows_succeeded=7,
            rows_failed=3,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
            error=None,
            landscape_run_id="landscape-1",
            pipeline_yaml=None,
        )
        assert record.status == "completed_with_failures"

    def test_empty_legal_construction(self) -> None:
        record = RunRecord(
            id=uuid4(),
            session_id=uuid4(),
            state_id=uuid4(),
            status="empty",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            rows_processed=0,
            rows_succeeded=0,
            rows_failed=0,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
            error=None,
            landscape_run_id="landscape-1",
            pipeline_yaml=None,
        )
        assert record.status == "empty"

    def test_invalid_status_raises_audit_integrity_error(self) -> None:
        with pytest.raises(AuditIntegrityError, match=r"runs\.status is 'ready'"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="ready",  # type: ignore[arg-type]
                started_at=datetime.now(UTC),
                finished_at=None,
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )

    def test_completed_requires_landscape_run_id(self) -> None:
        with pytest.raises(AuditIntegrityError, match="landscape_run_id"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="completed",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                rows_processed=1,
                rows_succeeded=1,
                rows_failed=0,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )

    def test_failed_requires_error(self) -> None:
        with pytest.raises(AuditIntegrityError, match="missing error"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="failed",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                rows_processed=1,
                rows_succeeded=0,
                rows_failed=1,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )

    def test_terminal_status_requires_finished_at(self) -> None:
        with pytest.raises(AuditIntegrityError, match="finished_at"):
            RunRecord(
                id=uuid4(),
                session_id=uuid4(),
                state_id=uuid4(),
                status="cancelled",
                started_at=datetime.now(UTC),
                finished_at=None,
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
                rows_routed_success=0,
                rows_routed_failure=0,
                rows_quarantined=0,
                error=None,
                landscape_run_id=None,
                pipeline_yaml=None,
            )


class TestRunAlreadyActiveError:
    def test_construction_and_message(self) -> None:
        err = RunAlreadyActiveError("session-123")
        assert err.session_id == "session-123"
        assert "session-123" in str(err)
        assert isinstance(err, Exception)

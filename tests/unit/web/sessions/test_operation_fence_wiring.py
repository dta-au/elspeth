"""Authoritative wiring gate for the session-operation lease lifetime.

The AST inventory proves which database statements exist.  This gate proves
that the public orchestration seams cannot call those writers without the
exact renewable authority that owns the complete logical operation.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import types
import typing
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import pytest

from elspeth.contracts.blobs import (
    BlobCreationObligation,
    BlobDeletionPlan,
    BlobGuidedOperationWriteFence,
    BlobRecord,
    BlobReplacementPlan,
    BlobRunLinkDirection,
    BlobRunLinkRecord,
)
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.web.composer import tool_batch
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination import repository as coordination_repository
from elspeth.web.coordination.contracts import SessionOperationContext
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions import protocol as sessions_protocol
from elspeth.web.sessions.protocol import RunEventRecord, SessionServiceProtocol
from elspeth.web.sessions.routes import interpretation as interpretation_routes
from elspeth.web.sessions.routes import messages as message_routes
from elspeth.web.sessions.routes.guided_operations import reserve_or_replay_guided_operation
from elspeth.web.sessions.service import SessionServiceImpl


def _required_parameter(owner: type[Any] | Any, method_name: str, parameter_name: str) -> inspect.Parameter:
    method = getattr(owner, method_name)
    parameter = inspect.signature(method).parameters.get(parameter_name)
    assert parameter is not None, f"{owner.__name__}.{method_name} has no {parameter_name} authority parameter"
    assert parameter.default is inspect.Parameter.empty, f"{owner.__name__}.{method_name}.{parameter_name} is optional"
    return parameter


@pytest.mark.parametrize("owner", [SessionServiceProtocol, SessionServiceImpl])
def test_guided_reservation_requires_the_exact_parent_session_context(owner: type[Any]) -> None:
    parameter = _required_parameter(owner, "reserve_guided_operation", "session_operation_context")
    assert parameter.annotation is SessionOperationContext or parameter.annotation == "SessionOperationContext"


def test_route_guided_reservation_adapter_cannot_omit_parent_authority() -> None:
    source = textwrap.dedent(inspect.getsource(reserve_or_replay_guided_operation))
    assert "SessionOperationLease.acquire" in source
    assert "SessionOperationKind.COMPOSE" in source
    assert "SessionOperationKind.SESSION_FORK" in source
    assert "session_operation_context=session_lease.context" in source


def test_send_message_acquires_compose_authority_before_state_or_message_access() -> None:
    source = textwrap.dedent(inspect.getsource(message_routes.register_message_routes))
    tree = ast.parse(source)
    send_message = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_message")
    compose_scope = next(
        node
        for node in ast.walk(send_message)
        if isinstance(node, ast.AsyncWith) and any(ast.unparse(item.context_expr) == "compose_lock" for item in node.items)
    )
    lease_item = next(item for item in compose_scope.items if "SessionOperationLease.acquire" in ast.unparse(item.context_expr))
    assert isinstance(lease_item.optional_vars, ast.Name)
    assert lease_item.optional_vars.id == "compose_operation_lease"

    state_read = next(
        node for node in ast.walk(compose_scope) if isinstance(node, ast.Call) and ast.unparse(node.func) == "service.get_current_state"
    )
    transcript_write = next(
        node
        for node in ast.walk(compose_scope)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "service.add_message_with_transcript"
    )
    assert lease_item.context_expr.lineno < state_read.lineno < transcript_write.lineno
    assert any(
        keyword.arg == "session_operation_context" and ast.unparse(keyword.value) == "compose_operation_lease.context"
        for keyword in transcript_write.keywords
    )


@pytest.mark.parametrize("owner", [SessionServiceProtocol, SessionServiceImpl])
def test_combined_message_transcript_write_requires_exact_compose_context(owner: type[Any]) -> None:
    context = _required_parameter(owner, "add_message_with_transcript", "session_operation_context")
    assert context.annotation is SessionOperationContext or context.annotation == "SessionOperationContext"


@pytest.mark.parametrize("owner", [SessionServiceProtocol, SessionServiceImpl])
def test_composition_state_write_requires_exact_compose_context(owner: type[Any]) -> None:
    context = _required_parameter(owner, "save_composition_state", "session_operation_context")
    assert context.annotation is SessionOperationContext or context.annotation == "SessionOperationContext"


@pytest.mark.parametrize(
    "method_name",
    ("record_session_interpretation_opt_out", "record_auto_interpreted_no_surfaces_event"),
)
@pytest.mark.parametrize("owner", [SessionServiceProtocol, SessionServiceImpl])
def test_simple_interpretation_writes_require_exact_compose_context(owner: type[Any], method_name: str) -> None:
    context = _required_parameter(owner, method_name, "session_operation_context")
    assert context.annotation is SessionOperationContext or context.annotation == "SessionOperationContext"


def test_opt_out_route_owns_process_lock_and_compose_lease() -> None:
    source = textwrap.dedent(inspect.getsource(interpretation_routes.register_interpretation_routes))
    tree = ast.parse(source)
    endpoint = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "opt_out_of_interpretations")
    compose_scope = next(
        node
        for node in ast.walk(endpoint)
        if isinstance(node, ast.AsyncWith) and any(ast.unparse(item.context_expr) == "compose_lock" for item in node.items)
    )
    lease = next(item for item in compose_scope.items if "SessionOperationLease.acquire" in ast.unparse(item.context_expr))
    assert "SessionOperationKind.COMPOSE" in ast.unparse(lease.context_expr)
    writer = next(
        node
        for node in ast.walk(compose_scope)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "service.record_session_interpretation_opt_out"
    )
    assert any(
        keyword.arg == "session_operation_context" and ast.unparse(keyword.value) == "compose_operation_lease.context"
        for keyword in writer.keywords
    )


def test_rate_cap_no_surfaces_write_reuses_compose_context() -> None:
    batch_source = textwrap.dedent(inspect.getsource(tool_batch.run_tool_batch))
    assert "session_operation_context=ctx.session_operation_context" in batch_source
    dispatch_source = textwrap.dedent(inspect.getsource(ComposerServiceImpl._dispatch_session_aware_tool))
    assert "session_operation_context=session_operation_context" in dispatch_source


def test_auto_title_is_owned_by_and_reuses_the_compose_lease() -> None:
    route_source = textwrap.dedent(inspect.getsource(message_routes.register_message_routes))
    route_tree = ast.parse(route_source)
    auto_title_call = next(
        node for node in ast.walk(route_tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == "maybe_auto_title_session"
    )
    assert any(
        keyword.arg == "session_operation_context" and ast.unparse(keyword.value) == "compose_operation_lease.context"
        for keyword in auto_title_call.keywords
    )
    parent = next(
        node
        for node in ast.walk(route_tree)
        if isinstance(node, ast.Call) and auto_title_call in ast.walk(node) and node is not auto_title_call
    )
    assert ast.unparse(parent.func) == "compose_operation_lease.create_task"

    title_source = textwrap.dedent(inspect.getsource(_auto_title.maybe_auto_title_session))
    assert "session_operation_context=session_operation_context" in title_source


def _resolved_signature(member: Any) -> tuple[tuple[tuple[str, inspect._ParameterKind, Any], ...], Any]:
    signature = inspect.signature(member)
    hints = typing.get_type_hints(member, include_extras=True)
    parameters = tuple(parameter for parameter in signature.parameters.values() if parameter.name != "self")
    assert all(parameter.default is inspect.Parameter.empty for parameter in parameters)
    return (
        tuple((parameter.name, parameter.kind, hints[parameter.name]) for parameter in parameters),
        hints["return"],
    )


def _top_level_types(annotation: Any) -> tuple[Any, ...]:
    origin = typing.get_origin(annotation)
    if origin in {typing.Union, types.UnionType}:
        return typing.get_args(annotation)
    return (annotation,)


def _assert_no_authority_escape(*, owner: type[Any], member_name: str, member: Any) -> None:
    hints = typing.get_type_hints(member, include_extras=True)
    forbidden_names = {"conn", "connection", "cursor", "engine", "query", "sql", "statement", "transaction", "tx"}
    assert not forbidden_names.intersection(member_name.lower().split("_"))
    for name, annotation in hints.items():
        if name != "return":
            assert not forbidden_names.intersection(name.lower().split("_")), f"{owner.__name__}.{member_name}.{name} is an escape"
        for candidate in _top_level_types(annotation):
            assert candidate not in {Any, object}, f"{owner.__name__}.{member_name}.{name} is generic"
            module = getattr(candidate, "__module__", "")
            assert not module.startswith("sqlalchemy"), f"{owner.__name__}.{member_name}.{name} exposes SQLAlchemy"


def test_fenced_unit_of_work_exposes_only_exact_composed_capabilities() -> None:
    """Every callback surface is an exact domain capability, never generic SQL."""
    archive_disposition = getattr(sessions_protocol, "SessionArchiveDisposition", None)
    session_protocol = getattr(sessions_protocol, "SessionOperationSessionMutations", None)
    composition_protocol = getattr(sessions_protocol, "SessionOperationCompositionMutations", None)
    interpretation_protocol = getattr(sessions_protocol, "SessionOperationInterpretationMutations", None)
    run_protocol = getattr(sessions_protocol, "SessionOperationRunMutations", None)
    blob_protocol = getattr(sessions_protocol, "SessionOperationBlobMutations", None)
    progress_protocol = getattr(sessions_protocol, "SessionOperationComposerProgressMutations", None)
    completion_protocol = getattr(sessions_protocol, "SessionOperationComposerCompletionMutations", None)
    assert archive_disposition is not None
    assert session_protocol is not None
    assert composition_protocol is not None
    assert interpretation_protocol is not None
    assert run_protocol is not None
    assert blob_protocol is not None
    assert progress_protocol is not None
    assert completion_protocol is not None

    implementation_types = (
        coordination_repository._RepositoryMutationTransaction,
        getattr(coordination_repository, "_RepositorySessionMutations", None),
        getattr(coordination_repository, "_RepositoryRunMutations", None),
        getattr(coordination_repository, "_RepositoryBlobMutations", None),
        getattr(coordination_repository, "_RepositoryComposerCompletionMutations", None),
        getattr(coordination_repository, "_RepositoryInterpretationMutations", None),
        getattr(coordination_repository, "_RepositoryCompositionStateMutations", None),
    )
    assert all(owner is not None for owner in implementation_types)

    expected_outer = {
        "database_now": datetime,
        "session": session_protocol,
        "composition_states": composition_protocol,
        "interpretations": interpretation_protocol,
        "runs": run_protocol,
        "blobs": blob_protocol,
        "composer_progress": progress_protocol,
        "composer_completion": completion_protocol,
    }
    for outer_owner in (sessions_protocol.SessionOperationMutationTransaction, implementation_types[0]):
        public = {name for name in dir(outer_owner) if not name.startswith("_")}
        assert public == set(expected_outer)
        for name, return_type in expected_outer.items():
            descriptor = inspect.getattr_static(outer_owner, name)
            assert isinstance(descriptor, property)
            assert descriptor.fget is not None
            if outer_owner is implementation_types[0] and name == "composer_completion":
                return_type = implementation_types[4]
            assert _resolved_signature(descriptor.fget) == ((), return_type)
            _assert_no_authority_escape(owner=outer_owner, member_name=name, member=descriptor.fget)

    expected_capabilities = (
        (
            (session_protocol, implementation_types[1]),
            {
                "record_plugin_crash_breadcrumb": (
                    (),
                    type(None),
                ),
                "decide_and_soft_archive": (
                    (("archived_at", inspect.Parameter.KEYWORD_ONLY, datetime),),
                    archive_disposition,
                ),
            },
        ),
        (
            (composition_protocol, implementation_types[6]),
            {
                "append_state": (
                    (("creation", inspect.Parameter.POSITIONAL_OR_KEYWORD, sessions_protocol.SessionCompositionStateCreation),),
                    sessions_protocol.CompositionStateRecord,
                ),
            },
        ),
        (
            (run_protocol, implementation_types[2]),
            {
                "create_pending_run": (
                    (
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("state_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("pipeline_yaml", inspect.Parameter.KEYWORD_ONLY, str | None),
                        ("started_at", inspect.Parameter.KEYWORD_ONLY, datetime),
                    ),
                    sessions_protocol.RunRecord,
                ),
                "transition_run_status": (
                    (
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("status", inspect.Parameter.KEYWORD_ONLY, sessions_protocol.SessionRunStatus),
                        ("error", inspect.Parameter.KEYWORD_ONLY, str | None),
                        ("landscape_run_id", inspect.Parameter.KEYWORD_ONLY, str | None),
                        ("rows_processed", inspect.Parameter.KEYWORD_ONLY, int | None),
                        ("rows_succeeded", inspect.Parameter.KEYWORD_ONLY, int | None),
                        ("rows_failed", inspect.Parameter.KEYWORD_ONLY, int | None),
                        ("rows_routed_success", inspect.Parameter.KEYWORD_ONLY, int | None),
                        ("rows_routed_failure", inspect.Parameter.KEYWORD_ONLY, int | None),
                        ("rows_quarantined", inspect.Parameter.KEYWORD_ONLY, int | None),
                    ),
                    type(None),
                ),
                "append_run_event": (
                    (
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("timestamp", inspect.Parameter.KEYWORD_ONLY, datetime),
                        ("event_type", inspect.Parameter.KEYWORD_ONLY, sessions_protocol.SessionRunEventType),
                        ("data", inspect.Parameter.KEYWORD_ONLY, Mapping[str, Any]),
                    ),
                    RunEventRecord,
                ),
                "list_run_events_after": (
                    (
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("after_sequence", inspect.Parameter.KEYWORD_ONLY, int),
                    ),
                    tuple[RunEventRecord, ...],
                ),
            },
        ),
        (
            (blob_protocol, implementation_types[3]),
            {
                "abort_blob_deletion": (
                    (("plan", inspect.Parameter.KEYWORD_ONLY, BlobDeletionPlan),),
                    bool,
                ),
                "abort_blob_replacement": (
                    (("plan", inspect.Parameter.KEYWORD_ONLY, BlobReplacementPlan),),
                    bool,
                ),
                "commit_blob_deletion": (
                    (
                        ("plan", inspect.Parameter.KEYWORD_ONLY, BlobDeletionPlan),
                        ("accepting_proposal_id", inspect.Parameter.KEYWORD_ONLY, UUID | None),
                    ),
                    BlobDeletionPlan,
                ),
                "commit_blob_replacement": (
                    (
                        ("plan", inspect.Parameter.KEYWORD_ONLY, BlobReplacementPlan),
                        ("max_storage_per_session", inspect.Parameter.KEYWORD_ONLY, int),
                        ("accepting_proposal_id", inspect.Parameter.KEYWORD_ONLY, UUID | None),
                    ),
                    BlobReplacementPlan,
                ),
                "discard_pending_blob": (
                    (
                        ("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        (
                            "guided_operation_write_fence",
                            inspect.Parameter.KEYWORD_ONLY,
                            BlobGuidedOperationWriteFence | None,
                        ),
                    ),
                    bool,
                ),
                "finalize_pending_output_blob": (
                    (
                        ("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("status", inspect.Parameter.KEYWORD_ONLY, Literal["ready", "error"]),
                        ("size_bytes", inspect.Parameter.KEYWORD_ONLY, int | None),
                        ("content_hash", inspect.Parameter.KEYWORD_ONLY, str | None),
                        ("max_storage_per_session", inspect.Parameter.KEYWORD_ONLY, int),
                    ),
                    BlobRecord,
                ),
                "insert_blob_run_link": (
                    (
                        ("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("direction", inspect.Parameter.KEYWORD_ONLY, BlobRunLinkDirection),
                    ),
                    bool,
                ),
                "list_abandoned_blob_reservations": (
                    (),
                    tuple[BlobCreationObligation, ...],
                ),
                "list_blob_deletions": (
                    (),
                    tuple[BlobDeletionPlan, ...],
                ),
                "list_blob_replacements": (
                    (),
                    tuple[BlobReplacementPlan, ...],
                ),
                "list_blob_run_links": (
                    (("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),),
                    tuple[BlobRunLinkRecord, ...],
                ),
                "list_pending_run_output_blobs": (
                    (("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),),
                    tuple[BlobRecord, ...],
                ),
                "list_run_output_blobs": (
                    (("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),),
                    tuple[BlobRecord, ...],
                ),
                "mark_blob_deletion_staged": (
                    (("plan", inspect.Parameter.KEYWORD_ONLY, BlobDeletionPlan),),
                    BlobDeletionPlan,
                ),
                "mark_blob_replacement_staged": (
                    (("plan", inspect.Parameter.KEYWORD_ONLY, BlobReplacementPlan),),
                    BlobReplacementPlan,
                ),
                "mark_blob_ready": (
                    (
                        ("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        (
                            "guided_operation_write_fence",
                            inspect.Parameter.KEYWORD_ONLY,
                            BlobGuidedOperationWriteFence | None,
                        ),
                    ),
                    BlobRecord,
                ),
                "mark_run_output_blob_error": (
                    (
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                    ),
                    BlobRecord,
                ),
                "mark_run_output_blob_ready": (
                    (
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("size_bytes", inspect.Parameter.KEYWORD_ONLY, int),
                        ("content_hash", inspect.Parameter.KEYWORD_ONLY, str),
                        ("max_storage_per_session", inspect.Parameter.KEYWORD_ONLY, int),
                    ),
                    BlobRecord,
                ),
                "prepare_blob_deletion": (
                    (
                        ("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("tombstone_path", inspect.Parameter.KEYWORD_ONLY, str),
                        ("blob_snapshot_hash", inspect.Parameter.KEYWORD_ONLY, str),
                        ("expected_file_present", inspect.Parameter.KEYWORD_ONLY, bool),
                        ("expected_file_size", inspect.Parameter.KEYWORD_ONLY, int | None),
                        ("expected_file_hash", inspect.Parameter.KEYWORD_ONLY, str | None),
                        ("accepting_proposal_id", inspect.Parameter.KEYWORD_ONLY, UUID | None),
                    ),
                    BlobDeletionPlan,
                ),
                "prepare_blob_replacement": (
                    (
                        ("replacement_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("expected", inspect.Parameter.KEYWORD_ONLY, BlobRecord),
                        ("replacement", inspect.Parameter.KEYWORD_ONLY, BlobRecord),
                        ("staging_path", inspect.Parameter.KEYWORD_ONLY, str),
                        ("backup_path", inspect.Parameter.KEYWORD_ONLY, str),
                        ("max_storage_per_session", inspect.Parameter.KEYWORD_ONLY, int),
                        ("accepting_proposal_id", inspect.Parameter.KEYWORD_ONLY, UUID | None),
                    ),
                    BlobReplacementPlan,
                ),
                "read_blob": (
                    (("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),),
                    BlobRecord,
                ),
                "read_blob_deletion": (
                    (("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),),
                    BlobDeletionPlan | None,
                ),
                "read_blob_replacement": (
                    (("blob_id", inspect.Parameter.KEYWORD_ONLY, UUID),),
                    BlobReplacementPlan | None,
                ),
                "reserve_blob": (
                    (
                        ("record", inspect.Parameter.KEYWORD_ONLY, BlobRecord),
                        ("max_storage_per_session", inspect.Parameter.KEYWORD_ONLY, int),
                        ("idempotent", inspect.Parameter.KEYWORD_ONLY, bool),
                        (
                            "guided_operation_write_fence",
                            inspect.Parameter.KEYWORD_ONLY,
                            BlobGuidedOperationWriteFence | None,
                        ),
                    ),
                    bool,
                ),
                "reserve_pending_output_blob": (
                    (("record", inspect.Parameter.KEYWORD_ONLY, BlobRecord),),
                    BlobRecord,
                ),
                "retire_abandoned_blob_reservation": (
                    (("obligation", inspect.Parameter.KEYWORD_ONLY, BlobCreationObligation),),
                    bool,
                ),
                "retire_blob_deletion": (
                    (("plan", inspect.Parameter.KEYWORD_ONLY, BlobDeletionPlan),),
                    bool,
                ),
                "retire_blob_replacement": (
                    (("plan", inspect.Parameter.KEYWORD_ONLY, BlobReplacementPlan),),
                    bool,
                ),
                "insert_blob_inline_resolutions": (
                    (
                        ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("attempt", inspect.Parameter.KEYWORD_ONLY, int),
                        ("resolutions", inspect.Parameter.KEYWORD_ONLY, Sequence[ResolvedBlobContent]),
                        ("resolved_at", inspect.Parameter.KEYWORD_ONLY, datetime),
                    ),
                    type(None),
                ),
            },
        ),
        (
            (interpretation_protocol, implementation_types[5]),
            {
                "record_session_opt_out": (
                    (
                        ("event_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("actor", inspect.Parameter.KEYWORD_ONLY, str),
                        ("opted_out_at", inspect.Parameter.KEYWORD_ONLY, datetime),
                    ),
                    tuple[sessions_protocol.InterpretationEventRecord, bool],
                ),
                "record_auto_interpreted_no_surfaces_event": (
                    (
                        ("event_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("actor", inspect.Parameter.KEYWORD_ONLY, str),
                        ("kind", inspect.Parameter.KEYWORD_ONLY, sessions_protocol.InterpretationKind),
                        ("model_identifier", inspect.Parameter.KEYWORD_ONLY, str),
                        ("model_version", inspect.Parameter.KEYWORD_ONLY, str),
                        ("provider", inspect.Parameter.KEYWORD_ONLY, str),
                        ("composer_skill_hash", inspect.Parameter.KEYWORD_ONLY, str),
                        ("created_at", inspect.Parameter.KEYWORD_ONLY, datetime),
                    ),
                    sessions_protocol.InterpretationEventRecord,
                ),
            },
        ),
        (
            (completion_protocol, implementation_types[4]),
            {
                "mark_ready_for_review": (
                    (
                        ("composition_state_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("actor", inspect.Parameter.KEYWORD_ONLY, str),
                        ("created_at", inspect.Parameter.KEYWORD_ONLY, datetime),
                        ("payload_digest", inspect.Parameter.KEYWORD_ONLY, str),
                        ("expires_at", inspect.Parameter.KEYWORD_ONLY, datetime),
                    ),
                    type(None),
                ),
                "record_yaml_export": (
                    (
                        ("composition_state_id", inspect.Parameter.KEYWORD_ONLY, UUID),
                        ("actor", inspect.Parameter.KEYWORD_ONLY, str),
                        ("created_at", inspect.Parameter.KEYWORD_ONLY, datetime),
                    ),
                    type(None),
                ),
            },
        ),
    )
    for owners, expected in expected_capabilities:
        for owner in owners:
            assert owner is not None
            public = {name for name in dir(owner) if not name.startswith("_")}
            assert public == set(expected)
            for name, signature in expected.items():
                member = getattr(owner, name)
                assert _resolved_signature(member) == signature
                _assert_no_authority_escape(owner=owner, member_name=name, member=member)


def test_execute_transfers_one_renewable_lease_to_background_completion() -> None:
    lease = _required_parameter(ExecutionServiceImpl, "execute", "session_operation_lease")
    assert lease.annotation is SessionOperationLease or lease.annotation == "SessionOperationLease"
    worker_lease = _required_parameter(ExecutionServiceImpl, "_run_pipeline", "session_operation_lease")
    assert worker_lease.annotation is SessionOperationLease or worker_lease.annotation == "SessionOperationLease"
    assert "session_operation_context" not in inspect.signature(ExecutionServiceImpl._run_pipeline).parameters
    completion_lease = _required_parameter(ExecutionServiceImpl, "_on_pipeline_done", "session_operation_lease")
    assert completion_lease.annotation is SessionOperationLease or completion_lease.annotation == "SessionOperationLease"

    execute_source = textwrap.dedent(inspect.getsource(ExecutionServiceImpl.execute))
    assert "session_operation_context = session_operation_lease.context" in execute_source
    assert "session_operation_lease=session_operation_lease" in execute_source
    assert "_executor.submit" in execute_source
    assert "future.add_done_callback" in execute_source
    worker_source = textwrap.dedent(inspect.getsource(ExecutionServiceImpl._run_pipeline))
    assert "session_operation_context = session_operation_lease.context" in worker_source
    assert "session_operation_lease.guard_external_effect" in worker_source
    completion_source = textwrap.dedent(inspect.getsource(ExecutionServiceImpl._on_pipeline_done))
    assert "session_operation_lease" in completion_source
    assert ".close" in completion_source


def test_composer_progress_is_durable_and_exactly_fenced_before_publish() -> None:
    _required_parameter(ComposerProgressRegistry, "__init__", "engine")
    publish_context = _required_parameter(ComposerProgressRegistry, "publish", "session_operation_context")
    assert publish_context.annotation is SessionOperationContext or publish_context.annotation == "SessionOperationContext"

    source = textwrap.dedent(inspect.getsource(ComposerProgressRegistry))
    assert "self._engine" in source
    assert "composer_progress" in source
    assert "session_operation_context" in source
    assert "self._session_operation_authority.mutate" in source
    assert "self._snapshots" not in source

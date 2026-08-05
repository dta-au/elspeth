"""Run diagnostics projection for the web execution UI.

This module reads Landscape, the audit source of truth, and returns a bounded
operator-facing snapshot. It intentionally omits row payloads and node context
JSON; those are audit/debug payloads, not safe default UI material.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final, Literal, get_args

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import TransformErrorCategory
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.export_mappers import artifact_producer_kind, validate_artifact_publication_projection
from elspeth.core.landscape.schema import (
    artifacts_table,
    node_states_table,
    operations_table,
    rows_table,
    token_outcomes_table,
    tokens_table,
)
from elspeth.web.config import WebSettings
from elspeth.web.execution.discard_summary import _sqlite_database_file_missing
from elspeth.web.execution.schemas import (
    RunDiagnosticArtifact,
    RunDiagnosticFailureDetail,
    RunDiagnosticNodeState,
    RunDiagnosticOperation,
    RunDiagnosticsResponse,
    RunDiagnosticSummary,
    RunDiagnosticToken,
)
from elspeth.web.sessions.protocol import SessionRunStatus

_DEFAULT_DIAGNOSTIC_LIMIT = 50
_MAX_DIAGNOSTIC_LIMIT = 100
_OPERATION_PREVIEW_LIMIT = 20
_ARTIFACT_PREVIEW_LIMIT = 20

# How many of the most recent failed node_states to consider when correlating a
# failed operation to the node that raised it. A run can accumulate many failed
# states that never failed the run (rows diverted by on_error), so the scan is
# bounded rather than unbounded, and finding no match is a normal outcome.
_FAILED_STATE_CORRELATION_SCAN_LIMIT = 50

_SafeFailureClassification = Literal[
    "authentication_failed",
    "authorization_denied",
    "connectivity_failure",
    "provider_unavailable",
    "rate_limited",
    "source_object_unreadable",
    "timeout",
]
_SAFE_TRANSFORM_ERROR_REASONS: Final[frozenset[object]] = frozenset(get_args(TransformErrorCategory))
_PROVIDER_FAILURE_CLASSIFICATIONS: Final[dict[tuple[str, str, str], _SafeFailureClassification]] = {
    ("submit_failed", "service_error", "AccessDeniedException"): "authorization_denied",
    ("submit_failed", "service_error", "ThrottlingException"): "rate_limited",
    ("poll_failed", "service_error", "AccessDeniedException"): "authorization_denied",
    ("poll_failed", "service_error", "ThrottlingException"): "rate_limited",
}
_EXCEPTION_FAILURE_CLASSIFICATIONS: Final[dict[str, _SafeFailureClassification]] = {
    "ConnectionResetError": "connectivity_failure",
    "PermissionError": "authorization_denied",
    "TimeoutError": "timeout",
}
_TEXTRACT_OBJECT_UNREADABLE_TUPLE: Final[tuple[str, str, str, str]] = (
    "submit_failed",
    "service_error",
    "InvalidS3ObjectException",
    "s3_object_unreadable",
)
_TEXTRACT_OBJECT_UNREADABLE_REMEDIATION: Final = (
    "Check that the pipeline AWS role can read the referenced S3 object and that the object is in the Amazon Textract endpoint region."
)
_STATUS_FAILURE_CLASSIFICATIONS: Final[dict[int, _SafeFailureClassification]] = {
    401: "authentication_failed",
    403: "authorization_denied",
    408: "timeout",
    429: "rate_limited",
}
_JSON_PAYLOAD_TYPE_NAMES: Final[dict[type[object], str]] = {
    bool: "bool",
    dict: "dict",
    float: "float",
    int: "int",
    list: "list",
    str: "str",
}


def llm_safe_diagnostics_snapshot(diagnostics: RunDiagnosticsResponse) -> dict[str, Any]:
    """Return a diagnostics projection safe to embed in an LLM prompt.

    The REST diagnostics response intentionally keeps raw operation error text
    for the operator UI. The diagnostics explanation LLM is a different
    boundary: provider/runtime error bodies are untrusted text, so they are
    replaced with generated summaries before prompt construction.
    """
    snapshot = diagnostics.model_dump(mode="json")
    for token in snapshot["tokens"]:
        for state in token["states"]:
            state["error"] = _summarize_error_payload_for_llm(state["error"])
    for operation in snapshot["operations"]:
        operation["error_message"] = _redacted_error_message_for_llm(operation["error_message"])
    failure_detail = snapshot["failure_detail"]
    if failure_detail is not None:
        failure_detail["error_message"] = _redacted_error_message_for_llm(failure_detail["error_message"])
    return snapshot


def _redacted_error_message_for_llm(error_message: str | None) -> str | None:
    if error_message is None:
        return None
    return f"[diagnostic error text redacted before LLM prompt; chars={len(error_message)}; lines={len(error_message.splitlines()) or 1}]"


def _summarize_error_payload_for_llm(error_payload: Any | None) -> dict[str, object] | None:
    if error_payload is None:
        return None
    try:
        serialized_chars = len(json.dumps(error_payload, sort_keys=True, allow_nan=False))
    except (RecursionError, TypeError, UnicodeError, ValueError):
        serialized_chars = None
    summary: dict[str, object] = {
        "redacted": True,
        "payload_type": _JSON_PAYLOAD_TYPE_NAMES.get(type(error_payload), "unknown"),
        "serialized_chars": serialized_chars,
    }
    if type(error_payload) is dict:
        reason = error_payload.get("reason")
        if type(reason) is str and reason in _SAFE_TRANSFORM_ERROR_REASONS:
            summary["reason"] = reason

        valid_statuses: list[int] = []
        for field_name in ("status_code", "http_status"):
            status = error_payload.get(field_name)
            if type(status) is int and 100 <= status <= 599:
                summary[field_name] = status
                valid_statuses.append(status)

        classification: _SafeFailureClassification | None = None
        remediation: str | None = None
        error_type = error_payload.get("error_type")
        code = error_payload.get("code")
        cause = error_payload.get("cause")
        if type(reason) is str and type(error_type) is str and type(code) is str:
            if type(cause) is str and (reason, error_type, code, cause) == _TEXTRACT_OBJECT_UNREADABLE_TUPLE:
                classification = "source_object_unreadable"
                remediation = _TEXTRACT_OBJECT_UNREADABLE_REMEDIATION
            else:
                classification = _PROVIDER_FAILURE_CLASSIFICATIONS.get((reason, error_type, code))
        if classification is None and "reason" not in error_payload:
            exception_type = error_payload.get("type")
            if type(exception_type) is str and type(error_payload.get("exception")) is str:
                classification = _EXCEPTION_FAILURE_CLASSIFICATIONS.get(exception_type)
        if classification is None and valid_statuses and all(status == valid_statuses[0] for status in valid_statuses):
            status = valid_statuses[0]
            classification = _STATUS_FAILURE_CLASSIFICATIONS.get(status)
            if classification is None and 500 <= status <= 599:
                classification = "provider_unavailable"
        if classification is not None:
            summary["failure_classification"] = classification
            if remediation is not None:
                summary["remediation"] = remediation
    return summary


def _bounded_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("diagnostics limit must be >= 1")
    return min(limit, _MAX_DIAGNOSTIC_LIMIT)


def _decode_json(value: str | None) -> Any | None:
    if value is None:
        return None
    return json.loads(value)


def _node_state_error_explains_operation(error_json: str | None, operation_error_message: str | None) -> bool:
    """Whether this failed node_state records the exception that failed the operation.

    There is no persisted linkage from an operation to the node_state that
    aborted it, so correlate on the recorded error instead. Both sides render
    the same exception through the same scrubber: ``operations.error_message``
    is ``scrub_text_for_audit(str(exc))`` (``core/operations.py``
    ``_render_exception``) and ``ExecutionError.exception`` is the same scrub of
    the same string (``contracts/errors.py`` ``__post_init__``). So when the
    state is the operation's cause, its ``exception`` appears verbatim inside
    the operation's message. Substring rather than equality: the operation
    message may carry wrapper/cause chain text around it.

    ``exception`` is the field to key on — ``ExecutionError`` requires it
    non-empty, whereas ``context`` is an optional structured payload only
    populated for exceptions exposing ``to_audit_dict()``. It is read as a
    fallback for envelopes predating that guarantee.

    Returns False for anything it cannot positively correlate — an absent,
    malformed, or unrecognised envelope, or an empty message that would match
    indiscriminately. A false negative costs scope-owner attribution (the prior
    behaviour); a false positive names an unrelated node, which is the defect
    this exists to prevent.
    """
    if not operation_error_message:
        return False
    try:
        payload = _decode_json(error_json)
    except (TypeError, ValueError):
        # error_json is a Text column: a malformed value must degrade this
        # projection to scope attribution, never fail the diagnostics read.
        return False
    if not isinstance(payload, dict):
        return False
    candidate = payload.get("exception")
    if not isinstance(candidate, str) or not candidate.strip():
        context = payload.get("context")
        candidate = context.get("message") if isinstance(context, dict) else None
    if not isinstance(candidate, str) or not candidate.strip():
        return False
    return candidate in operation_error_message


def _max_datetime(values: list[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present)


def _count_by_value(conn: Connection, table: Any, column: Any, *, run_id: str) -> dict[Any, Any]:
    stmt = select(column, func.count().label("count")).where(table.c.run_id == run_id).group_by(column)
    return {row[0]: row._mapping["count"] for row in conn.execute(stmt)}


def _empty_diagnostics(
    *,
    run_id: str,
    landscape_run_id: str,
    run_status: SessionRunStatus,
    cancel_requested: bool = False,
    limit: int,
) -> RunDiagnosticsResponse:
    preview_limit = _bounded_limit(limit)
    return RunDiagnosticsResponse(
        run_id=run_id,
        landscape_run_id=landscape_run_id,
        run_status=run_status,
        cancel_requested=cancel_requested,
        summary=RunDiagnosticSummary(
            token_count=0,
            preview_limit=preview_limit,
            preview_truncated=False,
            state_counts={},
            operation_counts={},
            latest_activity_at=None,
        ),
        tokens=[],
        operations=[],
        artifacts=[],
    )


def load_run_diagnostics_for_settings(
    settings: WebSettings,
    *,
    run_id: str,
    landscape_run_id: str,
    run_status: SessionRunStatus,
    cancel_requested: bool = False,
    limit: int = _DEFAULT_DIAGNOSTIC_LIMIT,
) -> RunDiagnosticsResponse:
    """Open Landscape from web settings and return a bounded run snapshot."""
    landscape_url = settings.get_landscape_url()
    if _sqlite_database_file_missing(landscape_url):
        return _empty_diagnostics(
            run_id=run_id,
            landscape_run_id=landscape_run_id,
            run_status=run_status,
            cancel_requested=cancel_requested,
            limit=limit,
        )

    db = LandscapeDB.from_url(
        landscape_url,
        passphrase=settings.landscape_passphrase,
        create_tables=False,
        read_only=True,
    )
    try:
        return load_run_diagnostics_from_db(
            db,
            run_id=run_id,
            landscape_run_id=landscape_run_id,
            run_status=run_status,
            cancel_requested=cancel_requested,
            limit=limit,
        )
    finally:
        db.close()


def load_run_diagnostics_from_db(
    db: LandscapeDB,
    *,
    run_id: str,
    landscape_run_id: str,
    run_status: SessionRunStatus,
    cancel_requested: bool = False,
    limit: int = _DEFAULT_DIAGNOSTIC_LIMIT,
) -> RunDiagnosticsResponse:
    """Return a bounded diagnostics snapshot for one Landscape run."""
    preview_limit = _bounded_limit(limit)

    with db.read_only_connection() as conn:
        token_count = int(
            conn.execute(select(func.count()).select_from(tokens_table).where(tokens_table.c.run_id == landscape_run_id)).scalar_one()
        )

        token_stmt = (
            select(
                tokens_table.c.token_id,
                tokens_table.c.row_id,
                rows_table.c.row_index,
                tokens_table.c.branch_name,
                tokens_table.c.fork_group_id,
                tokens_table.c.join_group_id,
                tokens_table.c.expand_group_id,
                tokens_table.c.step_in_pipeline,
                tokens_table.c.created_at,
                token_outcomes_table.c.outcome.label("terminal_outcome"),
            )
            .select_from(
                tokens_table.join(
                    rows_table,
                    and_(
                        tokens_table.c.row_id == rows_table.c.row_id,
                        tokens_table.c.run_id == rows_table.c.run_id,
                    ),
                ).outerjoin(
                    token_outcomes_table,
                    and_(
                        token_outcomes_table.c.token_id == tokens_table.c.token_id,
                        token_outcomes_table.c.run_id == tokens_table.c.run_id,
                        token_outcomes_table.c.completed == 1,
                    ),
                )
            )
            .where(tokens_table.c.run_id == landscape_run_id)
            .order_by(rows_table.c.row_index.asc(), tokens_table.c.created_at.asc(), tokens_table.c.token_id.asc())
            .limit(preview_limit)
        )
        token_rows = list(conn.execute(token_stmt))
        token_ids = tuple(row.token_id for row in token_rows)

        states_by_token: dict[str, list[RunDiagnosticNodeState]] = {token_id: [] for token_id in token_ids}
        if token_ids:
            state_stmt = (
                select(
                    node_states_table.c.state_id,
                    node_states_table.c.token_id,
                    node_states_table.c.node_id,
                    node_states_table.c.step_index,
                    node_states_table.c.attempt,
                    node_states_table.c.status,
                    node_states_table.c.duration_ms,
                    node_states_table.c.started_at,
                    node_states_table.c.completed_at,
                    node_states_table.c.error_json,
                    node_states_table.c.success_reason_json,
                )
                .where(
                    and_(
                        node_states_table.c.run_id == landscape_run_id,
                        node_states_table.c.token_id.in_(token_ids),
                    )
                )
                .order_by(
                    node_states_table.c.token_id.asc(),
                    node_states_table.c.step_index.asc(),
                    node_states_table.c.attempt.asc(),
                    node_states_table.c.started_at.asc(),
                )
            )
            for row in conn.execute(state_stmt):
                states_by_token[row.token_id].append(
                    RunDiagnosticNodeState(
                        state_id=row.state_id,
                        token_id=row.token_id,
                        node_id=row.node_id,
                        step_index=row.step_index,
                        attempt=row.attempt,
                        status=row.status,
                        duration_ms=row.duration_ms,
                        started_at=row.started_at,
                        completed_at=row.completed_at,
                        error=_decode_json(row.error_json),
                        success_reason=_decode_json(row.success_reason_json),
                    )
                )

        operation_stmt = (
            select(
                operations_table.c.operation_id,
                operations_table.c.node_id,
                operations_table.c.operation_type,
                operations_table.c.sink_effect_id,
                operations_table.c.status,
                operations_table.c.duration_ms,
                operations_table.c.started_at,
                operations_table.c.completed_at,
                operations_table.c.error_message,
            )
            .where(operations_table.c.run_id == landscape_run_id)
            .order_by(operations_table.c.started_at.asc(), operations_table.c.operation_id.asc())
            .limit(_OPERATION_PREVIEW_LIMIT)
        )
        operations = [
            RunDiagnosticOperation(
                operation_id=row.operation_id,
                node_id=row.node_id,
                operation_type=row.operation_type,
                sink_effect_id=row.sink_effect_id,
                status=row.status,
                duration_ms=row.duration_ms,
                started_at=row.started_at,
                completed_at=row.completed_at,
                error_message=row.error_message,
            )
            for row in conn.execute(operation_stmt)
        ]

        # Latest failed operation — surfaces the cause when the operations
        # preview limit (_OPERATION_PREVIEW_LIMIT) would have buried it. We pick
        # the most recently completed failed op with a non-empty error_message;
        # the Landscape recorder writes the full exception chain text there
        # (wrapper + cause + any HTTP response body captured at the wrap site).
        failure_stmt = (
            select(
                operations_table.c.operation_id,
                operations_table.c.node_id,
                operations_table.c.operation_type,
                operations_table.c.error_message,
                operations_table.c.completed_at,
                operations_table.c.started_at,
            )
            .where(
                and_(
                    operations_table.c.run_id == landscape_run_id,
                    operations_table.c.status == "failed",
                    operations_table.c.error_message.isnot(None),
                )
            )
            .order_by(
                operations_table.c.completed_at.desc().nulls_last(),
                operations_table.c.started_at.desc(),
                operations_table.c.operation_id.asc(),
            )
            .limit(1)
        )
        failure_row = conn.execute(failure_stmt).first()
        failure_detail: RunDiagnosticFailureDetail | None = None
        if failure_row is not None:
            failed_at = failure_row.completed_at or failure_row.started_at
            failure_node_id = failure_row.node_id

            # Attribution: operations.node_id names the node OWNING the
            # operation scope, not the node that raised. A source_load scope
            # spans the whole row loop, so a transform crash propagating up
            # through it lands on an operation row carrying the SOURCE node
            # (elspeth-8e5cc5ced0). The failed node_state written by
            # NodeStateGuard carries the true raising node, so prefer it.
            # Queried independently of the preview-limited token query above:
            # the failing token may sort outside the preview window.
            #
            # But ONLY when that state is the operation's own cause. "Latest
            # failed node_state" is a positional guess, not a causal link, and
            # a run can hold a failed state that never failed the run at all —
            # a row diverted by on_error, say — while the operation aborted for
            # an unrelated reason. Attributing the source's own crash to a
            # discarded row's transform is the original defect in the opposite
            # direction. There is no persisted operation->state linkage to join
            # on, so correlate on the recorded error: both sides render the
            # SAME exception through the audit scrubber, so the state's message
            # appears verbatim in operations.error_message when it is the cause.
            # No correlated state => keep the operation's owner, which is at
            # least the scope that aborted.
            failed_state_stmt = (
                select(
                    node_states_table.c.node_id,
                    node_states_table.c.completed_at,
                    node_states_table.c.error_json,
                )
                .where(
                    and_(
                        node_states_table.c.run_id == landscape_run_id,
                        node_states_table.c.status == "failed",
                    )
                )
                .order_by(
                    node_states_table.c.completed_at.desc().nulls_last(),
                    node_states_table.c.started_at.desc(),
                    node_states_table.c.state_id.asc(),
                )
                .limit(_FAILED_STATE_CORRELATION_SCAN_LIMIT)
            )
            for failed_state_row in conn.execute(failed_state_stmt):
                if not _node_state_error_explains_operation(failed_state_row.error_json, failure_row.error_message):
                    continue
                failure_node_id = failed_state_row.node_id
                if failed_state_row.completed_at is not None:
                    failed_at = failed_state_row.completed_at
                break

            failure_detail = RunDiagnosticFailureDetail(
                operation_id=failure_row.operation_id,
                node_id=failure_node_id,
                operation_type=failure_row.operation_type,
                error_message=failure_row.error_message,
                failed_at=failed_at,
            )

        artifact_stmt = (
            select(
                artifacts_table.c.artifact_id,
                artifacts_table.c.sink_node_id,
                artifacts_table.c.produced_by_state_id,
                artifacts_table.c.sink_effect_id,
                artifacts_table.c.artifact_type,
                artifacts_table.c.path_or_uri,
                artifacts_table.c.size_bytes,
                artifacts_table.c.publication_performed,
                artifacts_table.c.publication_evidence_kind,
                artifacts_table.c.created_at,
            )
            .where(artifacts_table.c.run_id == landscape_run_id)
            .order_by(artifacts_table.c.created_at.desc(), artifacts_table.c.artifact_id.asc())
            .limit(_ARTIFACT_PREVIEW_LIMIT)
        )
        artifacts: list[RunDiagnosticArtifact] = []
        for row in conn.execute(artifact_stmt):
            producer_kind = artifact_producer_kind(
                produced_by_state_id=row.produced_by_state_id,
                sink_effect_id=row.sink_effect_id,
            )
            validate_artifact_publication_projection(
                producer_kind=producer_kind,
                publication_performed=row.publication_performed,
                publication_evidence_kind=row.publication_evidence_kind,
            )
            artifacts.append(
                RunDiagnosticArtifact(
                    artifact_id=row.artifact_id,
                    sink_node_id=row.sink_node_id,
                    producer_kind=producer_kind,
                    produced_by_state_id=row.produced_by_state_id,
                    sink_effect_id=row.sink_effect_id,
                    artifact_type=row.artifact_type,
                    path_or_uri=row.path_or_uri,
                    size_bytes=row.size_bytes,
                    publication_performed=row.publication_performed,
                    publication_evidence_kind=row.publication_evidence_kind,
                    created_at=row.created_at,
                )
            )

        latest_candidates: list[datetime | None] = [
            conn.execute(select(func.max(tokens_table.c.created_at)).where(tokens_table.c.run_id == landscape_run_id)).scalar_one_or_none(),
            conn.execute(
                select(func.max(node_states_table.c.started_at)).where(node_states_table.c.run_id == landscape_run_id)
            ).scalar_one_or_none(),
            conn.execute(
                select(func.max(node_states_table.c.completed_at)).where(node_states_table.c.run_id == landscape_run_id)
            ).scalar_one_or_none(),
            conn.execute(
                select(func.max(operations_table.c.started_at)).where(operations_table.c.run_id == landscape_run_id)
            ).scalar_one_or_none(),
            conn.execute(
                select(func.max(operations_table.c.completed_at)).where(operations_table.c.run_id == landscape_run_id)
            ).scalar_one_or_none(),
            conn.execute(
                select(func.max(artifacts_table.c.created_at)).where(artifacts_table.c.run_id == landscape_run_id)
            ).scalar_one_or_none(),
        ]

        state_counts = _count_by_value(conn, node_states_table, node_states_table.c.status, run_id=landscape_run_id)
        operation_counts = _count_by_value(conn, operations_table, operations_table.c.operation_type, run_id=landscape_run_id)

    return RunDiagnosticsResponse(
        run_id=run_id,
        landscape_run_id=landscape_run_id,
        run_status=run_status,
        cancel_requested=cancel_requested,
        summary=RunDiagnosticSummary(
            token_count=token_count,
            preview_limit=preview_limit,
            preview_truncated=token_count > preview_limit,
            state_counts=state_counts,
            operation_counts=operation_counts,
            latest_activity_at=_max_datetime(latest_candidates),
        ),
        tokens=[
            RunDiagnosticToken(
                token_id=row.token_id,
                row_id=row.row_id,
                row_index=row.row_index,
                branch_name=row.branch_name,
                fork_group_id=row.fork_group_id,
                join_group_id=row.join_group_id,
                expand_group_id=row.expand_group_id,
                step_in_pipeline=row.step_in_pipeline,
                created_at=row.created_at,
                terminal_outcome=row.terminal_outcome,
                states=states_by_token[row.token_id],
            )
            for row in token_rows
        ],
        operations=operations,
        artifacts=artifacts,
        failure_detail=failure_detail,
    )

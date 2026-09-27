"""Pure request and persisted-row rules for guided operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from sqlalchemy.engine import RowMapping

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.web.sessions.protocol import (
    GUIDED_OPERATION_FAILURE_CODE_VALUES,
    GUIDED_OPERATION_KIND_VALUES,
    GuidedCompositionStateResult,
    GuidedDeclinedResult,
    GuidedFailureAuditCohort,
    GuidedOperationCompleted,
    GuidedOperationFailed,
    GuidedOperationFailureCode,
    GuidedOperationKind,
    GuidedOperationResult,
    GuidedPipelineProposalResult,
    GuidedSessionResult,
    validate_guided_failure_diagnostics,
)
from elspeth.web.sessions.time_normalization import restore_utc


class _GuidedOperationEventValues(TypedDict):
    """One immutable guided_operation_events row, built without owning DML."""

    session_id: str
    operation_id: str
    sequence: int
    event_kind: Literal["claimed", "renewed", "taken_over", "completed", "failed"]
    actor: str
    attempt: int
    prior_attempt: int | None
    lease_expires_at: datetime | None
    request_hash: str
    failure_audit_cohort: dict[str, object] | None
    occurred_at: datetime


def _validate_guided_hash(value: str, *, label: str) -> None:
    if not is_lower_sha256_hex(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


def _validate_guided_actor(actor: str) -> None:
    if type(actor) is not str or not actor or len(actor) > 128:
        raise ValueError("guided operation actor must be a non-empty string of at most 128 characters")


def _validate_guided_lease_seconds(lease_seconds: int) -> None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("guided operation lease_seconds must be an integer from 1 through 3600")


def _validate_guided_identity(*, operation_id: str, kind: GuidedOperationKind, request_hash: str) -> None:
    if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
        raise ValueError("guided operation id must be a non-empty string of at most 128 characters")
    if kind not in GUIDED_OPERATION_KIND_VALUES:
        raise ValueError("unsupported guided operation kind")
    _validate_guided_hash(request_hash, label="guided operation request_hash")


def _guided_operation_event_values(
    *,
    session_id: str,
    operation_id: str,
    sequence: int,
    event_kind: Literal["claimed", "renewed", "taken_over", "completed", "failed"],
    actor: str,
    attempt: int,
    prior_attempt: int | None,
    lease_expires_at: datetime | None,
    request_hash: str,
    failure_audit_cohort: GuidedFailureAuditCohort | None,
    occurred_at: datetime,
) -> _GuidedOperationEventValues:
    """Build one immutable event row without owning or executing DML."""
    if event_kind == "failed" and type(failure_audit_cohort) is not GuidedFailureAuditCohort:
        raise AuditIntegrityError("failed guided operation event must carry exactly one failure audit cohort commitment")
    if event_kind != "failed" and failure_audit_cohort is not None:
        raise AuditIntegrityError("non-failed guided operation event must not carry a failure audit cohort commitment")
    return {
        "session_id": session_id,
        "operation_id": operation_id,
        "sequence": sequence,
        "event_kind": event_kind,
        "actor": actor,
        "attempt": attempt,
        "prior_attempt": prior_attempt,
        "lease_expires_at": lease_expires_at,
        "request_hash": request_hash,
        "failure_audit_cohort": (failure_audit_cohort.envelope() if failure_audit_cohort is not None else None),
        "occurred_at": occurred_at,
    }


def _validate_guided_operation_row(
    row: RowMapping,
    *,
    expected_session_id: str,
    expected_operation_id: str,
) -> None:
    """Validate the complete Tier-1 row before replay/conflict classification."""

    if row["session_id"] != expected_session_id or row["operation_id"] != expected_operation_id:
        raise AuditIntegrityError("Tier 1: guided operation persisted identity does not match its lookup key")
    operation_id = row["operation_id"]
    if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
        raise AuditIntegrityError("Tier 1: guided operation operation_id is invalid")
    kind = row["kind"]
    if kind not in GUIDED_OPERATION_KIND_VALUES:
        raise AuditIntegrityError("Tier 1: guided operation kind is invalid")
    status = row["status"]
    if status not in {"in_progress", "completed", "failed"}:
        raise AuditIntegrityError("Tier 1: guided operation status is invalid")
    request_hash = row["request_hash"]
    if not is_lower_sha256_hex(request_hash):
        raise AuditIntegrityError("Tier 1: guided operation request_hash is invalid")
    attempt = row["attempt"]
    if type(attempt) is not int or attempt < 1:
        raise AuditIntegrityError("Tier 1: guided operation attempt is invalid")

    for field in ("originating_message_id", "proposal_id", "result_state_id", "result_message_id", "result_session_id"):
        value = row[field]
        if value is None:
            continue
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuditIntegrityError(f"Tier 1: guided operation {field} is not a UUID") from exc
        if str(parsed) != value:
            raise AuditIntegrityError(f"Tier 1: guided operation {field} is not a canonical UUID")

    created_at = row["created_at"]
    updated_at = row["updated_at"]
    if type(created_at) is not datetime or type(updated_at) is not datetime:
        raise AuditIntegrityError("Tier 1: guided operation timestamps are invalid")
    created_at = restore_utc(created_at)
    updated_at = restore_utc(updated_at)
    if updated_at < created_at:
        raise AuditIntegrityError("Tier 1: guided operation updated_at predates created_at")
    settled_at = row["settled_at"]
    if settled_at is not None:
        if type(settled_at) is not datetime:
            raise AuditIntegrityError("Tier 1: guided operation settled_at is invalid")
        if restore_utc(settled_at) < created_at:
            raise AuditIntegrityError("Tier 1: guided operation settled_at predates created_at")

    if status == "in_progress":
        _guided_in_progress_expiry(row)
    else:
        _validate_guided_terminal_bundle(row)


def _validate_guided_operation_admission_block_row(
    row: RowMapping,
    *,
    expected_session_id: str,
    expected_operation_id: str,
) -> None:
    """Validate one complete Tier-1 negative admission authority."""

    if row["session_id"] != expected_session_id or row["operation_id"] != expected_operation_id:
        raise AuditIntegrityError("Tier 1: guided operation admission block identity does not match its lookup key")
    operation_id = row["operation_id"]
    if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
        raise AuditIntegrityError("Tier 1: guided operation admission block operation_id is invalid")
    if row["kind"] != "guided_start":
        raise AuditIntegrityError("Tier 1: guided operation admission block kind is invalid")
    if row["failure_code"] != "request_cancelled":
        raise AuditIntegrityError("Tier 1: guided operation admission block failure code is invalid")
    try:
        _validate_guided_actor(row["actor"])
    except ValueError as exc:
        raise AuditIntegrityError("Tier 1: guided operation admission block actor is invalid") from exc
    if type(row["created_at"]) is not datetime:
        raise AuditIntegrityError("Tier 1: guided operation admission block timestamp is invalid")
    restore_utc(row["created_at"])


def _guided_in_progress_expiry(row: RowMapping) -> datetime:
    lease_token = row["lease_token"]
    lease_expires_at = row["lease_expires_at"]
    if type(lease_token) is not str or not 1 <= len(lease_token) <= 256 or type(lease_expires_at) is not datetime:
        raise AuditIntegrityError("Tier 1: in-progress guided operation has an invalid lease bundle")
    if any(
        row[field] is not None
        for field in (
            "settled_at",
            "result_kind",
            "result_message_id",
            "response_hash",
            "failure_code",
            "unproducible_output_fields",
            "failure_diagnostics",
        )
    ):
        raise AuditIntegrityError("Tier 1: in-progress guided operation retained terminal residue")
    kind = row["kind"]
    if row["result_session_id"] is not None and kind != "session_fork":
        raise AuditIntegrityError("Tier 1: in-progress guided operation has a mismatched session locator")
    if row["result_state_id"] is not None and kind == "session_fork":
        raise AuditIntegrityError("Tier 1: in-progress fork has a mismatched state locator")
    if row["proposal_id"] is not None and kind not in {"guided_respond", "guided_chat"}:
        raise AuditIntegrityError("Tier 1: in-progress guided operation has a mismatched proposal locator")
    return restore_utc(lease_expires_at)


def _guided_failure_unproducible_output_fields(row: RowMapping) -> tuple[str, ...]:
    raw_fields = row["unproducible_output_fields"]
    if raw_fields is None:
        return ()
    if type(raw_fields) is not list or not raw_fields or any(type(field) is not str for field in raw_fields):
        raise AuditIntegrityError("Tier 1: guided operation has malformed unproducible output fields")
    return tuple(raw_fields)


def _guided_failure_diagnostics(row: RowMapping) -> tuple[str, ...]:
    raw_notes = row["failure_diagnostics"]
    if raw_notes is None:
        return ()
    if type(raw_notes) is not list or not raw_notes:
        raise AuditIntegrityError("Tier 1: guided operation has malformed failure diagnostics")
    notes = tuple(raw_notes)
    validate_guided_failure_diagnostics(notes)
    return notes


def _validate_guided_terminal_bundle(row: RowMapping) -> None:
    status = row["status"]
    if status == "failed":
        failure_code = row["failure_code"]
        if failure_code not in GUIDED_OPERATION_FAILURE_CODE_VALUES:
            raise AuditIntegrityError("Tier 1: guided operation has an invalid terminal failure code")
        if any(
            row[field] is not None
            for field in (
                "lease_token",
                "lease_expires_at",
                "result_kind",
                "result_state_id",
                "result_message_id",
                "result_session_id",
                "proposal_id",
                "response_hash",
            )
        ):
            raise AuditIntegrityError("Tier 1: failed guided operation retained terminal failure residue")
        if type(row["settled_at"]) is not datetime:
            raise AuditIntegrityError("Tier 1: failed guided operation is missing settled_at")
        _guided_failure_unproducible_output_fields(row)
        _guided_failure_diagnostics(row)
        return
    if status != "completed":
        raise AuditIntegrityError("Tier 1: guided operation terminal decoder received a non-terminal row")
    if (
        row["lease_token"] is not None
        or row["lease_expires_at"] is not None
        or row["failure_code"] is not None
        or row["unproducible_output_fields"] is not None
        or row["failure_diagnostics"] is not None
    ):
        raise AuditIntegrityError("Tier 1: completed guided operation retained terminal residue")
    if type(row["settled_at"]) is not datetime:
        raise AuditIntegrityError("Tier 1: completed guided operation is missing settled_at")
    response_hash = row["response_hash"]
    if type(response_hash) is not str or not is_lower_sha256_hex(response_hash):
        raise AuditIntegrityError("Tier 1: completed guided operation is missing response_hash")
    try:
        result_kind = row["result_kind"]
        if result_kind == "composition_state":
            if row["kind"] in {"session_fork", "guided_plan"}:
                raise AuditIntegrityError("Tier 1: guided operation kind does not match its result locator")
            if row["result_message_id"] is not None or row["result_session_id"] is not None:
                raise AuditIntegrityError("Tier 1: state result retained a session locator")
            if row["proposal_id"] is not None and row["kind"] not in {"guided_respond", "guided_chat"}:
                raise AuditIntegrityError("Tier 1: state result retained an unsupported proposal locator")
            UUID(row["result_state_id"])
            if row["proposal_id"] is not None:
                UUID(row["proposal_id"])
        elif result_kind == "pipeline_proposal":
            if (
                row["kind"] != "guided_plan"
                or row["result_state_id"] is None
                or row["result_message_id"] is not None
                or row["proposal_id"] is None
                or row["result_session_id"] is not None
            ):
                raise AuditIntegrityError("Tier 1: guided plan operation has a malformed proposal locator")
            UUID(row["result_state_id"])
            UUID(row["proposal_id"])
        elif result_kind == "session":
            if (
                row["kind"] != "session_fork"
                or row["result_state_id"] is not None
                or row["result_message_id"] is not None
                or row["proposal_id"] is not None
            ):
                raise AuditIntegrityError("Tier 1: guided operation kind does not match its result locator")
            UUID(row["result_session_id"])
        elif result_kind == "declined":
            if (
                row["kind"] != "guided_plan"
                or row["result_state_id"] is None
                or row["result_message_id"] is None
                or row["proposal_id"] is not None
                or row["result_session_id"] is not None
            ):
                raise AuditIntegrityError("Tier 1: guided plan operation has a malformed decline locator")
            UUID(row["result_state_id"])
            UUID(row["result_message_id"])
        else:
            raise AuditIntegrityError("Tier 1: completed guided operation has an invalid result kind")
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("Tier 1: completed guided operation has a malformed replay locator") from exc


def _guided_terminal_outcome(row: RowMapping) -> GuidedOperationCompleted | GuidedOperationFailed:
    _validate_guided_terminal_bundle(row)
    if row["status"] == "failed":
        return GuidedOperationFailed(
            failure_code=cast("GuidedOperationFailureCode", row["failure_code"]),
            unproducible_output_fields=_guided_failure_unproducible_output_fields(row),
            failure_diagnostics=_guided_failure_diagnostics(row),
        )
    response_hash = cast("str", row["response_hash"])
    result_kind = row["result_kind"]
    if result_kind == "composition_state":
        result: GuidedOperationResult = GuidedCompositionStateResult(
            state_id=UUID(row["result_state_id"]),
            proposal_id=UUID(row["proposal_id"]) if row["proposal_id"] is not None else None,
        )
    elif result_kind == "pipeline_proposal":
        result = GuidedPipelineProposalResult(
            proposal_id=UUID(row["proposal_id"]),
            checkpoint_state_id=UUID(row["result_state_id"]),
        )
    elif result_kind == "session":
        result = GuidedSessionResult(session_id=UUID(row["result_session_id"]))
    elif result_kind == "declined":
        result = GuidedDeclinedResult(
            checkpoint_state_id=UUID(row["result_state_id"]),
            decline_message_id=UUID(row["result_message_id"]),
        )
    else:
        raise AuditIntegrityError("Tier 1: completed guided operation has an invalid result kind")
    return GuidedOperationCompleted(result=result, response_hash=response_hash)


def _merge_guided_binding(*, current: Any, requested: UUID | None, label: str) -> str | None:
    if requested is None:
        return cast("str | None", current)
    requested_value = str(requested)
    if current is not None and current != requested_value:
        raise AuditIntegrityError(f"Guided operation {label} is already bound to a different row")
    return requested_value


def _guided_completion_values(
    *,
    row: RowMapping,
    result: GuidedOperationResult,
) -> tuple[dict[str, str | None], GuidedOperationResult]:
    kind = row["kind"]
    if type(result) is GuidedCompositionStateResult:
        if kind in {"session_fork", "guided_plan"}:
            raise ValueError("guided operation kind requires a different result locator")
        if result.proposal_id is not None and kind not in {"guided_respond", "guided_chat"}:
            raise ValueError("only guided respond/chat state results may carry proposal_id")
        state_id = _merge_guided_binding(current=row["result_state_id"], requested=result.state_id, label="result state")
        proposal_id = _merge_guided_binding(current=row["proposal_id"], requested=result.proposal_id, label="proposal")
        if row["result_session_id"] is not None:
            raise AuditIntegrityError("State operation has a conflicting result-session binding")
        assert state_id is not None
        normalized: GuidedOperationResult = GuidedCompositionStateResult(
            state_id=UUID(state_id),
            proposal_id=UUID(proposal_id) if proposal_id is not None else None,
        )
        return (
            {
                "result_kind": "composition_state",
                "result_state_id": state_id,
                "result_message_id": None,
                "result_session_id": None,
                "proposal_id": proposal_id,
            },
            normalized,
        )
    if type(result) is GuidedPipelineProposalResult:
        if kind != "guided_plan":
            raise ValueError("only guided_plan may complete with a pipeline proposal locator")
        proposal_id = _merge_guided_binding(current=row["proposal_id"], requested=result.proposal_id, label="proposal")
        checkpoint_state_id = _merge_guided_binding(
            current=row["result_state_id"], requested=result.checkpoint_state_id, label="checkpoint state"
        )
        if row["result_session_id"] is not None:
            raise AuditIntegrityError("Guided plan operation has a conflicting result-session binding")
        assert proposal_id is not None and checkpoint_state_id is not None
        normalized = GuidedPipelineProposalResult(
            proposal_id=UUID(proposal_id),
            checkpoint_state_id=UUID(checkpoint_state_id),
        )
        return (
            {
                "result_kind": "pipeline_proposal",
                "result_state_id": checkpoint_state_id,
                "result_message_id": None,
                "result_session_id": None,
                "proposal_id": proposal_id,
            },
            normalized,
        )
    if type(result) is GuidedSessionResult:
        if kind != "session_fork":
            raise ValueError("only session_fork may complete with a session locator")
        session_id = _merge_guided_binding(current=row["result_session_id"], requested=result.session_id, label="result session")
        if row["result_state_id"] is not None or row["proposal_id"] is not None:
            raise AuditIntegrityError("Fork operation has a conflicting state/proposal binding")
        assert session_id is not None
        return (
            {
                "result_kind": "session",
                "result_state_id": None,
                "result_message_id": None,
                "result_session_id": session_id,
                "proposal_id": None,
            },
            GuidedSessionResult(session_id=UUID(session_id)),
        )
    if type(result) is GuidedDeclinedResult:
        if kind != "guided_plan":
            raise ValueError("only guided_plan may complete with a decline locator")
        checkpoint_state_id = _merge_guided_binding(
            current=row["result_state_id"], requested=result.checkpoint_state_id, label="checkpoint state"
        )
        decline_message_id = _merge_guided_binding(
            current=row["result_message_id"], requested=result.decline_message_id, label="decline message"
        )
        if row["result_session_id"] is not None or row["proposal_id"] is not None:
            raise AuditIntegrityError("Guided plan decline has a conflicting session/proposal binding")
        assert checkpoint_state_id is not None and decline_message_id is not None
        return (
            {
                "result_kind": "declined",
                "result_state_id": checkpoint_state_id,
                "result_message_id": decline_message_id,
                "result_session_id": None,
                "proposal_id": None,
            },
            GuidedDeclinedResult(
                checkpoint_state_id=UUID(checkpoint_state_id),
                decline_message_id=UUID(decline_message_id),
            ),
        )
    raise TypeError("unsupported guided operation result locator")

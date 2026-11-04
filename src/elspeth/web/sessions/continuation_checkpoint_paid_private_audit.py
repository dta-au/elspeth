"""Original checkpoint private audit cohort and bounded terminal cold proof."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256

from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, or_, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.audit_storage import private_diagnostic_drafts_for_source
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_codec import decode_retained_physical_audit_call
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, diagnostic_evidence_identity
from elspeth.web.composer.provider_audit_source import (
    ProviderAuditSource,
    audit_drafts_json,
    capture_provider_audit_source,
    provider_attempt_audit_drafts_for_source,
)
from elspeth.web.composer.visible_tool_event import validate_visible_audit_row
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.models import chat_messages_table as messages

# A closed checkpoint audit has one request and optionally one failure. Each
# member has at most one retained 1 MiB content string. Its outer JSON string
# can expand a UTF-8 byte by at most six escaped bytes; the remaining nominal
# evidence metadata is independently required to fit this 64 KiB skeleton.
# This is a SQL read ceiling, not a change to either private-content limit.
_OUTER_SKELETON_MAX_BYTES = 65_536
_OUTER_ROW_MAX_BYTES = _OUTER_SKELETON_MAX_BYTES + 6 * JSON_MAX_TOTAL_UTF8_BYTES
_PRIVATE_CONTENT = "Private credential-filtered diagnostic facts"


@dataclass(frozen=True, slots=True)
class CheckpointPaidAuditCohort:
    drafts: tuple[AuditMessageDraft, ...]
    private_sha256: str


@dataclass(slots=True)
class AdmittedCheckpointAuditSource:
    """One transaction's original paid evidence under its current write fence."""

    connection: Connection
    transaction: object
    context: SessionOperationContext
    source: ProviderAuditSource
    cohort: CheckpointPaidAuditCohort
    attempt_id: str
    active: bool = True

    def require(self, conn: Connection, context: SessionOperationContext, drafts: tuple[AuditMessageDraft, ...]) -> None:
        if (
            not self.active
            or conn is not self.connection
            or conn.get_transaction() is not self.transaction
            or context is not self.context
            or type(self.source) is not ProviderAuditSource
            or type(self.cohort) is not CheckpointPaidAuditCohort
            or audit_drafts_json(drafts) != audit_drafts_json(self.cohort.drafts)
        ):
            raise AuditIntegrityError("checkpoint paid audit escaped its admitted SQL transaction or original cohort")


def admit_checkpoint_paid_audit_source_on_connection(
    conn: Connection,
    *,
    service: object,
    context: SessionOperationContext,
    owed: object,
    call: ComposerLLMCall,
    cohort: CheckpointPaidAuditCohort,
    names: frozenset[str],
) -> AdmittedCheckpointAuditSource:
    """Bind one deep current H and original call before any consequence writes."""
    from elspeth.web.sessions.continuation_checkpoint_early_return import (
        VerifiedEarlyCompleteReturn,
        read_early_complete_return_on_connection,
    )
    from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed, read_v4_checkpoint_end_owed_on_connection
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(service) is not SessionServiceImpl
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or not isinstance(owed, (VerifiedCheckpointEndOwed, VerifiedEarlyCompleteReturn))
        or type(owed) not in (VerifiedCheckpointEndOwed, VerifiedEarlyCompleteReturn)
        or type(call) is not ComposerLLMCall
        or type(cohort) is not CheckpointPaidAuditCohort
        or type(names) is not frozenset
        or context.fence.session_id != owed.session_id
        or call.call_id != owed.attempt_id
    ):
        raise AuditIntegrityError("checkpoint paid audit lacks its actual current and original owners")
    service._assert_session_write_lock_held(conn, owed.session_id, caller="checkpoint_paid_audit_admission")
    service._require_session_write_authority_on_connection(conn, context, session_id=owed.session_id)
    fresh: VerifiedCheckpointEndOwed | VerifiedEarlyCompleteReturn | None
    if isinstance(owed, VerifiedEarlyCompleteReturn):
        fresh = read_early_complete_return_on_connection(
            conn,
            session_id=owed.session_id,
            operation_id=owed.operation_id,
            selected=owed.selected,
            names=names,
        )
    else:
        fresh = read_v4_checkpoint_end_owed_on_connection(
            conn,
            session_id=owed.session_id,
            operation_id=owed.operation_id,
            attempt_id=owed.attempt_id,
            names=names,
        )
    if (
        fresh != owed
        or decode_retained_physical_audit_call(owed.response) != call
        or checkpoint_paid_audit_cohort_for_hold(call, owed.new_hold) != cohort
    ):
        raise AuditIntegrityError("checkpoint paid audit changed its deep original H or provider cohort")
    source = ProviderAuditSource(owed.new_hold.session_id, owed.new_hold.session_operation_id, owed.new_hold.session_operation_epoch)
    return AdmittedCheckpointAuditSource(conn, conn.get_transaction(), context, source, cohort, owed.attempt_id)


def _private_kind(
    draft: AuditMessageDraft, *, source: ProviderTerminalHoldSource | None, call_id: str, provider_source: ProviderAuditSource
) -> str:
    if (
        type(draft) is not AuditMessageDraft
        or draft.role != "audit"
        or draft.content != _PRIVATE_CONTENT
        or draft.tool_calls is None
        or len(draft.tool_calls) != 1
        or draft.tool_call_id is not None
        or draft.parent_assistant_id is not None
        or draft.composition_state_id is not None
        or draft.inherit_composition_state is not False
    ):
        raise AuditIntegrityError("checkpoint private audit changed its closed draft shape")
    evidence = ComposerDiagnosticEvidence.from_envelope(
        draft.tool_calls[0],
        expected_session_id=provider_source.session_id,
        expected_operation_id=provider_source.operation_id,
        expected_tool_call_id=call_id,
    )
    if evidence.operation_epoch != provider_source.operation_epoch or (
        source is not None and source.session_operation_epoch != evidence.operation_epoch
    ):
        raise AuditIntegrityError("checkpoint private audit crossed its original operation epoch")
    if evidence.status == "provider_request" and evidence.tool_name == "composer_provider_context":
        kind = "request"
        identity = diagnostic_evidence_identity("provider-request", call_id)
        if evidence.result.content is not None:
            raise AuditIntegrityError("checkpoint private request lost its closed content shape")
    elif evidence.status == "provider_failure" and evidence.tool_name == "composer_failure":
        kind = "failure"
        identity = diagnostic_evidence_identity("provider-failure", call_id)
        if evidence.arguments.content is not None:
            raise AuditIntegrityError("checkpoint private failure lost its closed content shape")
    else:
        raise AuditIntegrityError("checkpoint private audit gained an unadmitted member")
    if evidence.evidence_id != identity:
        raise AuditIntegrityError("checkpoint private audit changed its original evidence identity")
    _require_outer_row_bound(draft)
    return kind


def _require_outer_row_bound(draft: AuditMessageDraft) -> None:
    if draft.tool_calls is None or len(draft.tool_calls) != 1:
        raise AuditIntegrityError("checkpoint private audit lost its one original envelope")
    original = audit_drafts_json((draft,))
    outer = deep_thaw(draft.tool_calls[0])
    if type(outer) is not dict or type(outer.get("evidence")) is not dict:
        raise AuditIntegrityError("checkpoint private audit lost its nominal envelope")
    evidence = outer["evidence"]
    for field in ("arguments", "result"):
        payload = evidence[field]
        if type(payload) is not dict:
            raise AuditIntegrityError("checkpoint private audit lost its nominal payload")
        if payload["content"] is not None:
            payload["content"] = ""
    skeleton = audit_drafts_json((replace(draft, tool_calls=(outer,)),))
    if len(skeleton.encode("utf-8")) > _OUTER_SKELETON_MAX_BYTES or len(original.encode("utf-8")) > _OUTER_ROW_MAX_BYTES:
        raise AuditIntegrityError("checkpoint private audit exceeds its proved outer representation bound")


def checkpoint_paid_audit_cohort(call: ComposerLLMCall, context: SessionOperationContext) -> CheckpointPaidAuditCohort:
    """Derive exact original private members before the terminal event exists."""
    if type(context) is not SessionOperationContext:
        raise AuditIntegrityError("checkpoint paid audit requires its original COMPOSE context")
    return _checkpoint_paid_audit_cohort_for_source(call, capture_provider_audit_source(context), hold=None)


def checkpoint_paid_audit_cohort_for_hold(call: ComposerLLMCall, hold: ProviderTerminalHoldSource) -> CheckpointPaidAuditCohort:
    """Project original paid evidence from deep H, without borrowing a live context."""
    if type(hold) is not ProviderTerminalHoldSource:
        raise AuditIntegrityError("checkpoint paid audit requires its original terminal hold")
    source = ProviderAuditSource(hold.session_id, hold.session_operation_id, hold.session_operation_epoch)
    return _checkpoint_paid_audit_cohort_for_source(call, source, hold=hold)


def _checkpoint_paid_audit_cohort_for_source(
    call: ComposerLLMCall, source: ProviderAuditSource, *, hold: ProviderTerminalHoldSource | None
) -> CheckpointPaidAuditCohort:
    if (
        type(call) is not ComposerLLMCall
        or type(source) is not ProviderAuditSource
        or type(call.call_id) is not str
        or call.private_request is None
        or call.private_request.disposition != "retained"
        or call.private_diagnostics != ()
        or call.status not in (ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.MALFORMED_RESPONSE)
    ):
        raise AuditIntegrityError("checkpoint paid audit lacks its closed original provider cohort")
    incomplete = call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
    if (call.private_failure is not None) != incomplete:
        raise AuditIntegrityError("checkpoint paid audit failure membership changed its original result")
    private = private_diagnostic_drafts_for_source(call, source=source)
    expected = ("request", "failure") if incomplete else ("request",)
    kinds = tuple(_private_kind(draft, source=hold, call_id=call.call_id, provider_source=source) for draft in private)
    if kinds != expected:
        raise AuditIntegrityError("checkpoint paid audit changed its original closed member order")
    return CheckpointPaidAuditCohort(
        provider_attempt_audit_drafts_for_source(call, source), sha256(audit_drafts_json(private).encode("utf-8")).hexdigest()
    )


def write_checkpoint_paid_audit_cohort_on_connection(
    conn: Connection,
    *,
    service: object,
    context: SessionOperationContext,
    cohort: CheckpointPaidAuditCohort,
    observed_at: datetime,
    admitted: AdmittedCheckpointAuditSource | None = None,
) -> None:
    """Persist the original visible/private drafts without a second usage owner."""
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or type(cohort) is not CheckpointPaidAuditCohort:
        raise AuditIntegrityError("checkpoint paid audit changed its original SQL writer")
    sid = context.fence.session_id
    service._assert_session_write_lock_held(conn, sid, caller="checkpoint_paid_audit")
    if admitted is not None:
        if type(admitted) is not AdmittedCheckpointAuditSource or admitted.cohort != cohort:
            raise AuditIntegrityError("checkpoint paid audit lost its admitted original cohort")
        admitted.require(conn, context, cohort.drafts)
        if admitted.source.session_id != sid:
            raise AuditIntegrityError("checkpoint paid audit crossed its original session")
    else:
        for draft in cohort.drafts:
            validate_visible_audit_row(
                envelopes=tuple(draft.tool_calls or ()),
                content=draft.content,
                role=draft.role,
                writer_principal="compose_loop",
                session_id=sid,
                context=context,
            )
    active = service._deduplicate_private_audit_drafts(
        conn,
        session_id=sid,
        drafts=cohort.drafts,
        writer_principal="compose_loop",
        session_operation_context=context,
        composition_state_id=None,
        admitted_checkpoint_source=admitted,
    )
    # This owner already settled its original physical attempt with the
    # requested-model terminal quota entry. `uncheckpointed_envelopes` would
    # settle it again from the public audit projection's different usage
    # source. The private deduplicator above still proves exact replay; a
    # duplicate public row is rejected by the terminal cold reader.
    base_sequence = service._reserve_sequence_range(conn, sid, count=len(active)) if active else 0
    for offset, draft in enumerate(active):
        service._insert_chat_message(
            conn,
            session_id=sid,
            role=draft.role,
            content=draft.content,
            raw_content=None,
            tool_calls=deep_thaw(draft.tool_calls) if draft.tool_calls else None,
            sequence_no=base_sequence + offset,
            writer_principal="compose_loop",
            composition_state_id=draft.effective_composition_state_id(None),
            tool_call_id=draft.tool_call_id,
            parent_assistant_id=draft.parent_assistant_id,
            created_at=observed_at,
            session_operation_context=context,
            audit_only=True,
        )
    with service._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:
        mutations.mark_session_updated(updated_at=observed_at)
    if admitted is not None:
        admitted.active = False


def require_checkpoint_paid_private_audit_on_connection(
    conn: Connection, *, source: ProviderTerminalHoldSource, attempt_id: str, private_sha256: str, incomplete: bool
) -> None:
    """Verify bounded original semantic private members against the terminal event."""
    if (
        type(source) is not ProviderTerminalHoldSource
        or type(attempt_id) is not str
        or type(private_sha256) is not str
        or len(private_sha256) != 64
        or any(char not in "0123456789abcdef" for char in private_sha256)
        or type(incomplete) is not bool
        or conn.dialect.name not in ("sqlite", "postgresql")
    ):
        raise AuditIntegrityError("checkpoint private audit lacks its exact cold source")
    request_id = diagnostic_evidence_identity("provider-request", attempt_id)
    failure_id = diagnostic_evidence_identity("provider-failure", attempt_id)
    text = cast(messages.c.tool_calls, Text)
    byte_length = func.octet_length(text) if conn.dialect.name == "postgresql" else func.length(cast(messages.c.tool_calls, LargeBinary))
    bounded = case((byte_length <= _OUTER_ROW_MAX_BYTES, messages.c.tool_calls), else_=None).label("tool_calls")
    rows = conn.execute(
        select(
            messages.c.id,
            messages.c.session_id,
            messages.c.role,
            messages.c.content,
            messages.c.raw_content,
            bounded,
            messages.c.tool_call_id,
            messages.c.parent_assistant_id,
            messages.c.composition_state_id,
            messages.c.writer_principal,
            byte_length.label("actual_byte_length"),
        )
        .where(
            messages.c.session_id == source.session_id,
            messages.c.role == "audit",
            messages.c.content == _PRIVATE_CONTENT,
            or_(text.like(f"%{attempt_id}%"), text.like(f"%{request_id}%"), text.like(f"%{failure_id}%")),
        )
        .limit(3)
    ).all()
    expected = ("request", "failure") if incomplete else ("request",)
    if len(rows) != len(expected):
        raise AuditIntegrityError("checkpoint private audit lost its exact original member count")
    found: dict[str, AuditMessageDraft] = {}
    for row in rows:
        if (
            row.actual_byte_length is None
            or row.actual_byte_length > _OUTER_ROW_MAX_BYTES
            or row.session_id != source.session_id
            or row.role != "audit"
            or row.content != _PRIVATE_CONTENT
            or row.raw_content is not None
            or row.tool_call_id is not None
            or row.parent_assistant_id is not None
            or row.composition_state_id is not None
            or row.writer_principal != "compose_loop"
            or type(row.tool_calls) is not list
            or len(row.tool_calls) != 1
        ):
            raise AuditIntegrityError("checkpoint private audit changed its bounded stored member")
        draft = AuditMessageDraft(role="audit", content=_PRIVATE_CONTENT, tool_calls=(row.tool_calls[0],), inherit_composition_state=False)
        envelope = ComposerDiagnosticEvidence.from_envelope(
            row.tool_calls[0],
            expected_session_id=source.session_id,
            expected_operation_id=source.session_operation_id,
            expected_tool_call_id=attempt_id,
        )
        if envelope.operation_epoch != source.session_operation_epoch:
            raise AuditIntegrityError("checkpoint private audit changed its original epoch")
        kind = "request" if envelope.status == "provider_request" else "failure" if envelope.status == "provider_failure" else "unexpected"
        identity = request_id if kind == "request" else failure_id if kind == "failure" else None
        if kind not in expected or kind in found or envelope.evidence_id != identity:
            raise AuditIntegrityError("checkpoint private audit changed its original cohort identity")
        _require_outer_row_bound(draft)
        found[kind] = draft
    if tuple(kind for kind in expected if kind in found) != expected:
        raise AuditIntegrityError("checkpoint private audit lost its expected semantic order")
    actual = sha256(audit_drafts_json(tuple(found[kind] for kind in expected)).encode("utf-8")).hexdigest()
    if actual != private_sha256:
        raise AuditIntegrityError("checkpoint private audit differs from its original terminal event digest")

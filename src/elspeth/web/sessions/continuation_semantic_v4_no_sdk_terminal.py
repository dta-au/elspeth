"""Cold proof of an original V4 semantic Advisor refusal before SDK entry."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import AdvisorFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkRefusalReason
from elspeth.web.coordination.quota_authority import _undispatched_cancellation_event_id, _usage_entry_id
from elspeth.web.sessions.composer_operations import COMPOSER_OPERATION_RESULT_SCHEMA_ERROR, composer_operation_result_hash
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
)
from elspeth.web.sessions.continuation_semantic_advisor import _decode, _descriptor_data, _NoSdk
from elspeth.web.sessions.continuation_semantic_paid_incomplete_tail import (
    prepare_semantic_paid_incomplete_tail_plan,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import (
    fixed_v4_semantic_no_sdk_error,
    prepare_semantic_no_sdk_tail_plan,
    reconstruct_semantic_no_sdk_tail_batch,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import prepare_semantic_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import _bounded_bytes, _stored_hold_source
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class VerifiedSemanticNoSdkTerminal:
    source: ProviderTerminalHoldSource
    receipt: _NoSdk
    terminal: StoredContinuationSnapshot
    provider_attempt_id: str
    hold_state: str


def read_v4_semantic_no_sdk_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> VerifiedSemanticNoSdkTerminal | None:
    """Reprove the exact no-SDK receipt, zero-use ledger, parent, root and H."""
    sid = str(session_id)
    source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id, effect_key=effect_key)
    if source is None:
        return None
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one()
    if effect.disposition != "refused":
        return None
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    terminal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == terminal.checkpoint_id,
        )
    ).one()
    admitted_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == source.source_cursor + 1
        )
    ).one_or_none()
    if admitted_row is None or admitted_row.kind != "transition" or terminal_row.kind != "transition":
        raise AuditIntegrityError("Semantic no-SDK terminal lost original dispatch and terminal heads")
    admitted_payload = _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    admitted = StoredContinuationSnapshot(admitted_payload, admitted_row.cursor, admitted_row.checkpoint_id)
    admitted_root = decode_continuation_snapshot(admitted.payload_json)
    frame = admitted_root.frames[-1]
    entries = tuple(item for item in admitted_root.effect_frontier if item.effect_id == effect_key)
    if (
        type(frame) is not AdvisorFrame
        or frame.provider_effect_key != effect_key
        or frame.provider_input_sha256 != source.source_input_sha256
        or frame.phase != "awaiting_response"
        or frame.response is not None
        or admitted_root.policy_hash != source.policy_sha256
        or admitted_root.logical.session_id != sid
        or admitted_root.logical.operation_id != operation_id
        or len(entries) != 1
        or entries[0].disposition != "dispatched"
    ):
        raise AuditIntegrityError("Semantic no-SDK terminal changed its original admitted Advisor")
    physical_input = _descriptor_data(conn, sid, operation_id, effect).physical_input
    if type(effect.provider_attempt_id) is not str or type(effect.result_chunk_key) is not str or type(effect.receipt_key) is not str:
        raise AuditIntegrityError("Semantic no-SDK terminal lost its physical/receipt identity")
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one()
    if (
        effect.owner != "provider"
        or effect.input_sha256 != source.source_input_sha256
        or effect.creator_sequence != source.creator_sequence
        or effect.creator_generation != source.creator_generation
        or effect.requested_model != physical_input.options["model"]
        or effect.response_finished_at is not None
        or hold.provider_attempt_id != effect.provider_attempt_id
        or physical_row is None
        or physical_row.ledger_entry_id is None
        or physical_row.settled_at is None
        or (physical_row.session_id, physical_row.source, physical_row.run_id, physical_row.operation_id, physical_row.operation_epoch)
        != (sid, "composer", None, source.session_operation_id, source.session_operation_epoch)
        or physical_row.identity_id != principal
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or (creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token)
        != (source.session_operation_id, source.session_operation_epoch, physical_row.lease_token)
        or creator.claim_attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("Semantic no-SDK terminal changed original effect, attempt or H")
    receipt_json = _bounded_bytes(conn, source, effect.result_chunk_key, "composer.semantic-no-sdk-refusal.v1").decode(
        "utf-8", errors="strict"
    )
    receipt = _decode(_NoSdk, receipt_json)
    if (
        receipt.session_id != sid
        or receipt.operation_id != operation_id
        or receipt.effect_key != effect_key
        or receipt.input_sha256 != effect.input_sha256
        or receipt.receipt_key != effect.receipt_key
        or receipt.undispatched_attempt_id != effect.provider_attempt_id
        or receipt.undispatched_model != effect.requested_model
        or receipt.creator_sequence != source.creator_sequence
        or receipt.creator_generation != source.creator_generation
    ):
        raise AuditIntegrityError("Semantic no-SDK terminal changed its original refusal receipt")
    reason = SemanticAdvisorNoSdkRefusalReason(receipt.reason)
    expected_failure = {
        SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED: "PromptGuardRefused",
        SemanticAdvisorNoSdkRefusalReason.COST_REFUSED: "CommonProviderCostRefused",
        SemanticAdvisorNoSdkRefusalReason.CHARGEABLE_REFUSED: "ChargeableAdmissionRefused",
        SemanticAdvisorNoSdkRefusalReason.PRE_SDK_DEADLINE: "TimeoutError",
        SemanticAdvisorNoSdkRefusalReason.CANCELLED_BEFORE_SDK: "CancelledError",
    }[reason]
    if receipt.original_failure_class != expected_failure:
        raise AuditIntegrityError("Semantic no-SDK terminal changed its original failure classification")
    if reason is SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED:
        if receipt.guard_audit_id is None or receipt.guard_disposition is None:
            raise AuditIntegrityError("Semantic no-SDK terminal lost its original guard audit locator")
        guard_rows = conn.execute(
            select(messages).where(messages.c.session_id == sid, messages.c.role == "audit", messages.c.writer_principal == "compose_loop")
        ).all()
        guard_matches = tuple(
            item["check"]
            for row in guard_rows
            if row.tool_calls is not None
            for item in row.tool_calls
            if item.get("_kind") == "prompt_guard_audit" and item["check"]["audit_id"] == receipt.guard_audit_id
        )
        if len(guard_matches) != 1 or (
            guard_matches[0]["role"],
            guard_matches[0]["disposition"],
            guard_matches[0]["operation_id"],
            guard_matches[0]["operation_epoch"],
        ) != ("advisor", receipt.guard_disposition, creator.session_operation_id, creator.session_operation_epoch):
            raise AuditIntegrityError("Semantic no-SDK terminal lost its original guard disposition")
    elif receipt.guard_audit_id is not None or receipt.guard_disposition is not None:
        raise AuditIntegrityError("Semantic no-SDK terminal invented guard evidence")
    paid_plan = prepare_semantic_paid_overflow_tail_plan(
        identity=PaidOverflowIdentity(
            session_id=sid,
            operation_id=operation_id,
            effect_key=effect_key,
            attempt_id=effect.provider_attempt_id,
            session_operation_id=source.session_operation_id,
            session_operation_epoch=source.session_operation_epoch,
            ownership_generation=source.creator_generation,
            lease_sha256=source.lease_sha256,
            policy_sha256=source.policy_sha256,
            source_checkpoint_id=source.source_checkpoint_id,
            source_cursor=source.source_cursor,
            source_snapshot_sha256=source.source_snapshot_sha256,
        ),
        admitted=admitted,
        creator_sequence=source.creator_sequence,
    )
    no_sdk_plan = prepare_semantic_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=effect.provider_attempt_id,
        requested_model=effect.requested_model,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
    )
    incomplete_plan = prepare_semantic_paid_incomplete_tail_plan(
        identity=paid_plan.identity, admitted=admitted, creator_sequence=source.creator_sequence
    )
    from elspeth.web.sessions.continuation_semantic_terminal_reservation import semantic_sdk_terminal_reservation

    reserved, combined_hash = semantic_sdk_terminal_reservation(
        paid_plan, incomplete_plan, no_sdk_plan, historical_contract_sha256=source.fallback_contract_sha256
    )
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != combined_hash:
        raise AuditIntegrityError("Semantic no-SDK terminal changed its original combined H")
    tail = reconstruct_semantic_no_sdk_tail_batch(
        no_sdk_plan,
        receipt_json=receipt_json,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        published_at=restore_utc(terminal_row.created_at),
    )
    if (
        tail.receipt_chunk_key != effect.result_chunk_key
        or terminal.cursor != admitted.cursor + 2
        or terminal.payload_json != tail.append.snapshot_json
        or terminal_row.snapshot_chunk_key != f"snapshot:{terminal.checkpoint_id}"
        or parent.status != "failed"
        or parent.settled_by != "owner_terminal"
        or parent.failure_code != "http_error"
        or parent.journal_cursor != terminal.cursor
        or parent.checkpoint_id != terminal.checkpoint_id
        or parent.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR
        or parent.result_json != canonical_json(fixed_v4_semantic_no_sdk_error(reason).model_dump(mode="json"))
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
    ):
        raise AuditIntegrityError("Semantic no-SDK terminal lost its exact failed parent/root")
    successors = conn.execute(
        select(checkpoints.c.checkpoint_id).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor > admitted.cursor
        )
    ).all()
    if tuple(row.checkpoint_id for row in successors) != (terminal.checkpoint_id,):
        raise AuditIntegrityError("Semantic no-SDK terminal has another successor")
    event_rows = conn.execute(
        select(events)
        .where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence > admitted.cursor)
        .order_by(events.c.sequence)
    ).all()
    if len(event_rows) != 2:
        raise AuditIntegrityError("Semantic no-SDK terminal changed event count")
    for row, expected in zip(event_rows, tail.append.events, strict=True):
        if (
            row.tag != expected.tag
            or row.payload_chunk_key != f"event:{row.sequence}"
            or _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("Semantic no-SDK terminal changed attribution event")
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=effect.provider_attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    audit = conn.execute(
        select(messages).where(messages.c.id == _undispatched_cancellation_event_id(effect.provider_attempt_id))
    ).one_or_none()
    expected_audit_content = canonical_json(
        {
            "kind": "provider_attempt.cancelled_before_dispatch",
            "attempt_id": effect.provider_attempt_id,
            "source": "composer",
            "run_id": None,
            "requested_model": effect.requested_model,
            "operation_id": source.session_operation_id,
            "operation_epoch": source.session_operation_epoch,
            "lease_token_sha256": source.lease_sha256,
        }
    )
    if (
        entry is None
        or audit is None
        or physical_row.ledger_entry_id != entry_id
        or (entry.identity_id, entry.session_id, entry.source, entry.run_id, entry.model)
        != (principal, sid, "composer", None, effect.requested_model)
        or (entry.prompt_tokens, entry.completion_tokens, entry.cached_prompt_tokens, entry.reasoning_tokens) != (0, 0, 0, 0)
        or restore_utc(entry.recorded_at) != restore_utc(physical_row.settled_at)
        or restore_utc(audit.created_at) != restore_utc(physical_row.settled_at)
        or (audit.session_id, audit.role, audit.writer_principal, audit.raw_content, audit.tool_calls)
        != (sid, "audit", "compose_loop", None, None)
        or audit.content != expected_audit_content
        or audit.composition_state_id is not None
        or audit.tool_call_id is not None
        or audit.parent_assistant_id is not None
        or restore_utc(physical_row.settled_at) < restore_utc(physical_row.started_at)
    ):
        raise AuditIntegrityError("Semantic no-SDK terminal lost zero-use quota/audit evidence")
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation_id, segments.c.sequence == source.creator_sequence
        )
    ).one()
    final_root = decode_continuation_snapshot(terminal.payload_json)
    final_frame = final_root.frames[-1]
    final_entries = tuple(item for item in final_root.effect_frontier if item.effect_id == effect_key)
    if (
        segment.state != "settled"
        or segment.generation != source.creator_generation
        or creator.outcome != "settled"
        or creator.revoked_at is None
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or final_root.pending_settlement_ids
        or final_root.pending_owner_results
        or type(final_frame) is not AdvisorFrame
        or final_frame.response is None
        or len(final_entries) != 1
        or (final_entries[0].disposition, final_entries[0].receipt_id) != ("refused", effect.receipt_key)
    ):
        raise AuditIntegrityError("Semantic no-SDK terminal lost closed segment or refused effect")
    return VerifiedSemanticNoSdkTerminal(source, receipt, terminal, effect.provider_attempt_id, hold.state)

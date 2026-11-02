"""Cold proof of an admitted V4 PRIMARY zero-use refusal and consumed H."""

from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkRefusalReason
from elspeth.web.coordination.composer_operation_authority import _record_from_row
from elspeth.web.coordination.quota_authority import _undispatched_cancellation_event_id, _usage_entry_id
from elspeth.web.sessions.composer_operations import composer_operation_result_hash
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, _effect_snapshot, load_snapshot, private_chunk_key
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_primary import (
    PrimaryNoSdkRefusalReceipt,
    _primary_frame,
    _PrimaryNoSdkRefusalData,
    _v4_dispatch_action_projection,
    stored_primary_input_data_on_connection,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk import (
    _shape,
    fixed_v4_primary_no_sdk_error,
    prepare_primary_no_sdk_tail_plan,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_primary_paid_overflow_tail import prepare_primary_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import _bounded_bytes, _stored_hold_source
from elspeth.web.sessions.continuation_primary_terminal_reservation import primary_terminal_reservation
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc


def read_v4_primary_no_sdk_refusal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> PrimaryNoSdkRefusalReceipt | None:
    sid = str(session_id)
    source = _stored_hold_source(conn, session_id=sid, operation_id=operation_id, effect_key=effect_key)
    if source is None:
        return None
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one_or_none()
    if effect is None or effect.disposition != "refused":
        return None
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    terminal_root = decode_continuation_snapshot(terminal.payload_json)
    frame = _primary_frame(terminal_root)
    terminal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == terminal.checkpoint_id,
        )
    ).one()
    admitted_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == source.source_cursor + 1,
        )
    ).one_or_none()
    if admitted_row is None or admitted_row.kind != "transition" or terminal_row.kind != "transition":
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original dispatch and terminal heads")
    admitted_text = _bounded_bytes(conn, source, admitted_row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    admitted = StoredContinuationSnapshot(admitted_text, admitted_row.cursor, admitted_row.checkpoint_id)
    admitted_root = decode_continuation_snapshot(admitted_text)
    admitted_frame = _primary_frame(admitted_root)
    if (
        terminal_root.logical != admitted_root.logical
        or terminal_root.policy_hash != source.policy_sha256
        or frame.provider_effect_key != effect_key
        or frame.provider_input_sha256 != source.source_input_sha256
        or effect.provider_attempt_id != hold.provider_attempt_id
        or type(effect.provider_attempt_id) is not str
        or type(effect.receipt_key) is not str
        or type(effect.result_chunk_key) is not str
        or effect.response_finished_at is not None
        or parent.status != "failed"
        or parent.settled_by != "owner_terminal"
        or parent.journal_cursor != terminal.cursor
        or parent.checkpoint_id != terminal.checkpoint_id
        or hold.state != "consumed"
        or terminal.cursor != admitted.cursor + 2
        or admitted_frame.provider_effect_key != effect_key
        or admitted_frame.provider_input_sha256 != source.source_input_sha256
        or admitted_frame.phase != "awaiting_response"
        or len(admitted_root.effect_frontier) != 1
        or (
            admitted_root.effect_frontier[0].effect_id,
            admitted_root.effect_frontier[0].disposition,
            admitted_root.effect_frontier[0].receipt_id,
        )
        != (effect_key, "dispatched", None)
        or len(terminal_root.effect_frontier) != 1
        or (
            terminal_root.effect_frontier[0].effect_id,
            terminal_root.effect_frontier[0].disposition,
            terminal_root.effect_frontier[0].receipt_id,
        )
        != (effect_key, "refused", effect.receipt_key)
        or _bounded_bytes(conn, source, terminal_row.snapshot_chunk_key, "snapshot.v2") != terminal.payload_json.encode("utf-8")
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original terminal/effect/H")
    try:
        receipt_text = _bounded_bytes(conn, source, effect.result_chunk_key, "composer.primary-no-sdk-refusal.v1").decode(
            "utf-8", errors="strict"
        )
        data = _PrimaryNoSdkRefusalData.model_validate_json(receipt_text, strict=True)
        reason = PrimaryNoSdkRefusalReason(data.reason)
    except (UnicodeDecodeError, ValueError, ValidationError) as error:
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its closed receipt") from error
    if (
        canonical_json(data.model_dump(mode="json")) != receipt_text
        or data.source_disposition != "dispatched"
        or (data.session_id, data.operation_id, data.effect_key, data.input_sha256) != (sid, operation_id, effect_key, effect.input_sha256)
        or (data.creator_sequence, data.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (data.claim_attempt, data.operation_epoch) != (source.claim_attempt, source.session_operation_epoch)
        or (data.source_checkpoint_id, data.source_cursor) != (admitted.checkpoint_id, admitted.cursor)
        or data.receipt_key != effect.receipt_key
        or data.undispatched_attempt_id != effect.provider_attempt_id
        or data.undispatched_model != effect.requested_model
        or data.public_error_json != canonical_json(fixed_v4_primary_no_sdk_error(reason).model_dump(mode="json"))
        or private_chunk_key(encode_private_chunk(codec="composer.primary-no-sdk-refusal.v1", canonical=receipt_text))
        != effect.result_chunk_key
        or parent.result_json != data.public_error_json
        or type(parent.result_json) is not str
        or parent.result_sha256 != composer_operation_result_hash(parent.result_json)
        or parent.failure_code != "http_error"
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use receipt contradicts its actual failed terminal")
    descriptor, _wire, _tools = stored_primary_input_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, effect=effect, frame=frame
    )
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    if physical_row is None or descriptor.model != data.undispatched_model or physical_row.settled_at is None:
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original physical request")
    require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=None
    )
    paid = prepare_primary_paid_overflow_tail_plan(
        identity=source_paid_identity(source, effect.provider_attempt_id),
        admitted=admitted,
        creator_sequence=source.creator_sequence,
    )
    refused_projection = _effect_snapshot_for_reader(admitted.payload_json, effect_key, data.receipt_key)
    _projected, action_chunk = _v4_dispatch_action_projection(
        conn,
        root=admitted_root,
        effect=effect,
        dispatched_json=refused_projection,
        disposition="refused",
    )
    no_sdk = prepare_primary_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=effect.provider_attempt_id,
        requested_model=descriptor.model,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=source.claim_attempt,
        operation_epoch=source.session_operation_epoch,
        action_chunk=action_chunk,
    )
    _incomplete_tail, reserved, combined_hash = primary_terminal_reservation(paid, no_sdk)
    projected, projected_events = _shape(
        admitted=admitted,
        receipt_key=data.receipt_key,
        receipt_chunk_key=effect.result_chunk_key,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        action_chunk=action_chunk,
    )
    expected_terminal = encode_continuation_snapshot(decode_continuation_snapshot(projected).model_copy(update={"cursor": terminal.cursor}))
    if (
        source.reserved_bytes != reserved
        or source.fallback_contract_sha256 != combined_hash
        or len(receipt_text.encode("utf-8")) > no_sdk.maximum_receipt_bytes
        or expected_terminal != terminal.payload_json
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original bounded tail")
    for index, expected in enumerate(projected_events, start=1):
        event = conn.execute(
            select(events).where(
                events.c.session_id == sid,
                events.c.operation_id == operation_id,
                events.c.sequence == admitted.cursor + index,
            )
        ).one_or_none()
        if (
            event is None
            or event.tag != expected.tag
            or _bounded_bytes(conn, source, event.payload_chunk_key, "event.v2") != expected.payload_json.encode("utf-8")
        ):
            raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its exact two-event journal")
    dispatch = conn.execute(
        select(events).where(
            events.c.session_id == sid,
            events.c.operation_id == operation_id,
            events.c.sequence == admitted.cursor,
        )
    ).one_or_none()
    if (
        dispatch is None
        or dispatch.tag != "may_dispatch"
        or _bounded_bytes(conn, source, dispatch.payload_chunk_key, "event.v2")
        != canonical_json({"effect_key": effect_key}).encode("utf-8")
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original dispatch event")
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=effect.provider_attempt_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()
    audit = conn.execute(
        select(messages).where(messages.c.id == _undispatched_cancellation_event_id(effect.provider_attempt_id))
    ).one_or_none()
    if (
        ledger is None
        or audit is None
        or physical_row.ledger_entry_id != entry_id
        or ledger.identity_id != physical_row.identity_id
        or ledger.model != descriptor.model
        or ledger.session_id != sid
        or ledger.source != "composer"
        or ledger.run_id is not None
        or any(
            value != 0 for value in (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens)
        )
        or audit.session_id != sid
        or audit.role != "audit"
        or audit.writer_principal != "compose_loop"
        or audit.tool_calls is not None
        or audit.raw_content is not None
        or audit.composition_state_id is not None
        or audit.tool_call_id is not None
        or audit.parent_assistant_id is not None
        or restore_utc(audit.created_at) != restore_utc(ledger.recorded_at)
        or restore_utc(audit.created_at) != restore_utc(physical_row.settled_at)
        or audit.content
        != canonical_json(
            {
                "kind": "provider_attempt.cancelled_before_dispatch",
                "attempt_id": effect.provider_attempt_id,
                "source": "composer",
                "run_id": None,
                "requested_model": descriptor.model,
                "operation_id": source.session_operation_id,
                "operation_epoch": source.session_operation_epoch,
                "lease_token_sha256": source.lease_sha256,
            }
        )
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its exact zero-token cancellation cohort")
    return PrimaryNoSdkRefusalReceipt(_record_from_row(parent), terminal, effect_key, data.receipt_key, effect.result_chunk_key)


def _effect_snapshot_for_reader(admitted_json: str, effect_key: str, receipt_key: str) -> str:
    return _effect_snapshot(admitted_json, effect_key=effect_key, disposition="refused", receipt_id=receipt_key)


def source_paid_identity(source: ProviderTerminalHoldSource, attempt_id: str) -> PaidOverflowIdentity:
    if type(source) is not ProviderTerminalHoldSource:
        raise TypeError("V4 PRIMARY no-SDK paid bound requires its original H")
    return PaidOverflowIdentity(
        session_id=source.session_id,
        operation_id=source.operation_id,
        effect_key=source.effect_key,
        attempt_id=attempt_id,
        session_operation_id=source.session_operation_id,
        session_operation_epoch=source.session_operation_epoch,
        ownership_generation=source.creator_generation,
        lease_sha256=source.lease_sha256,
        policy_sha256=source.policy_sha256,
        source_checkpoint_id=source.source_checkpoint_id,
        source_cursor=source.source_cursor,
        source_snapshot_sha256=source.source_snapshot_sha256,
    )

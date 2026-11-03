"""Witness-free cold proof of one V4 classifier complete result and consumed H."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from sqlalchemy import Connection, Row, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.provider_result_admission import require_retained_v4_provider_metadata_credentials
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_classifier_primary import _chain, _Source
from elspeth.web.sessions.continuation_classifier_primary_incomplete_tail import prepare_classifier_paid_incomplete_tail_plan
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import prepare_classifier_no_sdk_tail_plan
from elspeth.web.sessions.continuation_classifier_primary_paid_tail import prepare_classifier_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_classifier_primary_terminal_reservation import (
    ClassifierTerminalReservation,
    classifier_sdk_terminal_reservation,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_primary import _input_value
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import _bounded_bytes, _stored_hold_source
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical


def require_v4_classifier_complete_hold_on_connection(
    conn: Connection,
    *,
    source: _Source,
    dispatched: StoredContinuationSnapshot,
    effect: Row[Any],
    call: ComposerLLMCall,
    completion: _AdmittedLLMCompletion,
) -> None:
    """Rebuild original H reserve and prove its single complete-result consumption."""
    if (
        type(source) is not _Source
        or type(dispatched) is not StoredContinuationSnapshot
        or type(call) is not ComposerLLMCall
        or type(completion) is not _AdmittedLLMCompletion
    ):
        raise AuditIntegrityError("V4 classifier complete cold proof requires original nominal result")
    attempt_id = call.call_id
    if type(attempt_id) is not str:
        raise AuditIntegrityError("V4 classifier complete cold proof lost original paid attempt")
    sid, operation, effect_key = str(source.session_id), source.operation_id, source.effect_key
    prepared = _chain(conn, source, dispatched=False)
    if _chain(conn, source, dispatched=True) != dispatched:
        raise AuditIntegrityError("V4 classifier complete cold proof changed original dispatched head")
    root = decode_continuation_snapshot(prepared.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("V4 classifier complete cold proof changed admitted policy")
    hold_source = _stored_hold_source(conn, session_id=sid, operation_id=operation, effect_key=effect_key)
    if hold_source is None:
        raise AuditIntegrityError("V4 classifier complete result lost its original H")
    contract = _require_historical_hold_source(conn, hold_source)
    hold = _require_hold_fields(conn, source=hold_source, contract=contract)
    hold_row = conn.execute(
        select(holds.c.consumed_at).where(holds.c.session_id == sid, holds.c.operation_id == operation, holds.c.effect_key == effect_key)
    ).one()
    if (
        hold.state != "consumed"
        or hold_row.consumed_at is None
        or hold.provider_attempt_id != attempt_id
        or hold_source.source_checkpoint_id != prepared.checkpoint_id
        or hold_source.source_cursor != prepared.cursor
        or hold_source.source_snapshot_sha256 != sha256(prepared.payload_json.encode("utf-8")).hexdigest()
        or hold_source.source_input_sha256 != source.input_sha256
        or hold_source.policy_sha256 != root.policy_hash
        or (hold_source.creator_sequence, hold_source.creator_generation) != (source.creator_sequence, source.creator_generation)
        or effect.provider_attempt_id != attempt_id
    ):
        raise AuditIntegrityError("V4 classifier complete result changed its consumed physical/H source")
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == attempt_id)).one_or_none()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one_or_none()
    if physical_row is None or parent is None or physical_row.settled_at is None or physical_row.ledger_entry_id is None:
        raise AuditIntegrityError("V4 classifier complete result lost its settled physical ledger")
    descriptor = _input_value(
        _bounded_bytes(conn, hold_source, f"provider-input:{source.input_sha256}", "composer.provider-input.v1").decode(
            "utf-8", errors="strict"
        ),
        source.input_sha256,
    )
    requested_model = require_original_v4_primary_physical_source_on_connection(
        conn,
        source=hold_source,
        descriptor=descriptor,
        effect=effect,
        physical=physical_row,
        parent=parent,
        call=call,
    )
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation,
        effect_key=effect_key,
        attempt_id=attempt_id,
        session_operation_id=hold_source.session_operation_id,
        session_operation_epoch=hold_source.session_operation_epoch,
        ownership_generation=hold_source.creator_generation,
        lease_sha256=hold_source.lease_sha256,
        policy_sha256=hold_source.policy_sha256,
        source_checkpoint_id=hold_source.source_checkpoint_id,
        source_cursor=hold_source.source_cursor,
        source_snapshot_sha256=hold_source.source_snapshot_sha256,
    )
    paid = prepare_classifier_paid_overflow_tail_plan(identity=identity, admitted=dispatched, creator_sequence=hold_source.creator_sequence)
    incomplete = prepare_classifier_paid_incomplete_tail_plan(
        identity=identity, admitted=dispatched, creator_sequence=hold_source.creator_sequence
    )
    no_sdk = prepare_classifier_no_sdk_tail_plan(
        admitted=dispatched,
        attempt_id=attempt_id,
        requested_model=requested_model,
        input_sha256=source.input_sha256,
        effect_key=effect_key,
        creator_sequence=hold_source.creator_sequence,
        creator_generation=hold_source.creator_generation,
        claim_attempt=hold_source.claim_attempt,
        operation_epoch=hold_source.session_operation_epoch,
    )
    reservation = ClassifierTerminalReservation(
        *classifier_sdk_terminal_reservation(paid, incomplete, no_sdk, historical_contract_sha256=hold_source.fallback_contract_sha256)
    )
    if hold_source.reserved_bytes != reservation.reserved_bytes or hold_source.fallback_contract_sha256 != reservation.contract_sha256:
        raise AuditIntegrityError("V4 classifier complete result changed its original combined H reserve")
    require_retained_v4_provider_metadata_credentials(completion, surface="composer_classifier_complete_cold")

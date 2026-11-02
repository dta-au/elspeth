"""V4 classifier PRIMARY admission on its original applied P4 source."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_classifier_no_sdk import ClassifierNoSdkIssuer
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_json
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import ProviderAttempt, begin_provider_attempt_on_connection
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _require_current_continuation_running,
    load_snapshot,
    prepare_snapshot_append,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_classifier_primary import (
    PreparedClassifierPrimaryProvider,
    V4ClassifierProviderAdmission,
    _begin_classifier_primary_on_connection,
    _chain,
    _event,
    _read_source,
)
from elspeth.web.sessions.continuation_classifier_primary_paid_tail import prepare_classifier_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_paid_overflow_quota import preflight_v4_requested_model
from elspeth.web.sessions.continuation_primary import _input_value
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import _stored_hold_source
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
    bind_provider_terminal_hold_attempt,
    reserve_provider_terminal_hold,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl


def admit_v4_classifier_provider_on_connection(
    conn: Connection,
    service: SessionServiceImpl,
    prepared: PreparedClassifierPrimaryProvider,
    *,
    model: str,
    no_sdk_issuer: ClassifierNoSdkIssuer,
) -> V4ClassifierProviderAdmission:
    """Mint physical attempt, H, and exact classifier dispatch in one transaction."""
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import prepare_classifier_no_sdk_tail_plan
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(service) is not SessionServiceImpl
        or type(prepared) is not PreparedClassifierPrimaryProvider
        or type(no_sdk_issuer) is not ClassifierNoSdkIssuer
        or no_sdk_issuer.prepared_candidate is not prepared.prepared
    ):
        raise AuditIntegrityError("V4 classifier admission requires its original prepared and unentered owners")
    no_sdk_issuer.clock.require_preparation_time()
    original = prepared.prepared
    binding, current = original.binding, original.snapshot
    running = binding.running
    _require_current_continuation_running(conn, running)
    source = _read_source(conn, running.claim.session_id, running.claim.operation_id, binding.effect_key)
    selected = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
    )
    if selected != current or _chain(conn, source, dispatched=False) != selected:
        raise AuditIntegrityError("V4 classifier admission changed its original prepared source")
    root = decode_continuation_snapshot(current.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id,
            effects.c.effect_key == binding.effect_key,
        )
    ).one()
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or binding.family != 0
        or row.disposition != "prepared"
        or row.provider_attempt_id is not None
        or row.input_sha256 != binding.input_sha256
        or source.input_sha256 != binding.input_sha256
        or (row.creator_sequence, row.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise AuditIntegrityError("V4 classifier admission lost its current prepared family-0 effect")
    requested = preflight_v4_requested_model(model)
    request = prepared.request
    descriptor = continuation_provider_input_json(request.kwargs)
    retained = _input_value(descriptor, request.input_sha256)
    if (
        request.input_sha256 != binding.input_sha256
        or retained.model != requested.value
        or read_chunk(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            key=f"provider-input:{request.input_sha256}",
        ).decode("utf-8", errors="strict")
        != descriptor
    ):
        raise AuditIntegrityError("V4 classifier admission changed its actual prepared request or model")
    attempt = begin_provider_attempt_on_connection(
        conn,
        session_operation_context=running.session_operation_context,
        source="composer",
        policy=service._chargeable_admission_policy,
        run_id=None,
    )
    transition = ContinuationEvent("may_dispatch", _event(source))
    dispatched_json = _effect_snapshot(current.payload_json, effect_key=binding.effect_key, disposition="dispatched", receipt_id=None)
    published_at = database_now(conn)
    append = prepare_snapshot_append(current, snapshot_json=dispatched_json, events=(transition,), published_at=published_at)
    dispatched = StoredContinuationSnapshot(append.snapshot_json, current.cursor + 1, append.checkpoint_id)
    fence = running.session_operation_context.fence
    identity = PaidOverflowIdentity(
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        effect_key=binding.effect_key,
        attempt_id=attempt.attempt_id,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        ownership_generation=running.claim.ownership_generation,
        lease_sha256=sha256(fence.lease_token.encode("utf-8")).hexdigest(),
        policy_sha256=policy.policy_hash,
        source_checkpoint_id=current.checkpoint_id,
        source_cursor=current.cursor,
        source_snapshot_sha256=sha256(current.payload_json.encode("utf-8")).hexdigest(),
    )
    paid_tail = prepare_classifier_paid_overflow_tail_plan(
        identity=identity, admitted=dispatched, creator_sequence=running.claim.segment_sequence
    )
    no_sdk_tail = prepare_classifier_no_sdk_tail_plan(
        admitted=dispatched,
        attempt_id=attempt.attempt_id,
        requested_model=requested.value,
        input_sha256=binding.input_sha256,
        effect_key=binding.effect_key,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        operation_epoch=fence.operation_epoch,
    )
    reserved_bytes = max(paid_tail.reserved_bytes, no_sdk_tail.reserved_bytes)
    contract = canonical_json(
        {
            "version": "composer.classifier-primary-v4-combined-tail.v1",
            "paid": paid_tail.fallback_contract_sha256,
            "no_sdk": no_sdk_tail.contract_sha256,
            "reserved_bytes": reserved_bytes,
        }
    )
    hold = ProviderTerminalHoldSource(
        session_id=identity.session_id,
        operation_id=identity.operation_id,
        effect_key=identity.effect_key,
        policy_sha256=identity.policy_sha256,
        allowance_bytes=policy.max_retained_payload_bytes,
        source_checkpoint_id=identity.source_checkpoint_id,
        source_cursor=identity.source_cursor,
        source_snapshot_sha256=identity.source_snapshot_sha256,
        source_input_sha256=binding.input_sha256,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        lease_sha256=identity.lease_sha256,
        reserved_bytes=reserved_bytes,
        fallback_contract_sha256=sha256(contract.encode("utf-8")).hexdigest(),
    )
    prospective = (
        ProspectiveContinuationChunk(f"event:{dispatched.cursor}", transition.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{dispatched.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    reserve_provider_terminal_hold(conn, source=hold, prospective=prospective, created_at=published_at)
    actual = _begin_classifier_primary_on_connection(
        conn, original, attempt=attempt, requested_model=requested.value, prepared_append=append
    )
    if actual != dispatched:
        raise AuditIntegrityError("V4 classifier admission changed its exact projected dispatch")
    bind_provider_terminal_hold_attempt(conn, source=hold, provider_attempt_id=attempt.attempt_id)
    no_sdk_issuer.note_dispatched_candidate(actual)
    return V4ClassifierProviderAdmission(attempt, actual, hold, paid_tail, no_sdk_tail, binding.effect_key)


def read_v4_classifier_admission_on_connection(
    conn: Connection, prepared: PreparedClassifierPrimaryProvider, *, no_sdk_issuer: ClassifierNoSdkIssuer
) -> V4ClassifierProviderAdmission | None:
    """Reconcile one possibly committed admission after an uncertain async return."""
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import prepare_classifier_no_sdk_tail_plan

    if (
        type(prepared) is not PreparedClassifierPrimaryProvider
        or type(no_sdk_issuer) is not ClassifierNoSdkIssuer
        or no_sdk_issuer.prepared_candidate is not prepared.prepared
    ):
        raise TypeError("classifier admission readback needs its original prepared owner")
    original = prepared.prepared
    running, binding = original.binding.running, original.binding
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    source = _read_source(conn, running.claim.session_id, operation, binding.effect_key)
    hold = _stored_hold_source(conn, session_id=sid, operation_id=operation, effect_key=binding.effect_key)
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == binding.effect_key)
    ).one()
    if effect.disposition == "prepared" and hold is None:
        return None
    if hold is None or effect.disposition != "dispatched":
        raise AuditIntegrityError("classifier admission readback found an incomplete original physical/H cut")
    contract = _require_historical_hold_source(conn, hold)
    hold_row = _require_hold_fields(conn, source=hold, contract=contract)
    current = load_snapshot(conn, session_id=sid, operation_id=operation)
    dispatched = _chain(conn, source, dispatched=True)
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one_or_none()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    if (
        current != dispatched
        or dispatched.cursor != original.snapshot.cursor + 1
        or hold.source_checkpoint_id != original.snapshot.checkpoint_id
        or hold.source_cursor != original.snapshot.cursor
        or hold.source_snapshot_sha256 != sha256(original.snapshot.payload_json.encode("utf-8")).hexdigest()
        or hold.source_input_sha256 != binding.input_sha256
        or hold_row.state != "active"
        or hold_row.provider_attempt_id != effect.provider_attempt_id
        or physical_row is None
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
    ):
        raise AuditIntegrityError("classifier admission readback changed its original unresolved dispatch")
    descriptor = _input_value(
        read_chunk(conn, session_id=sid, operation_id=operation, key=f"provider-input:{binding.input_sha256}").decode(
            "utf-8", errors="strict"
        ),
        binding.input_sha256,
    )
    requested_model = require_original_v4_primary_physical_source_on_connection(
        conn, source=hold, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=None
    )
    if continuation_provider_input_json(prepared.request.kwargs) != descriptor.canonical:
        raise AuditIntegrityError("classifier admission readback changed its actual prepared wire")
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation,
        effect_key=binding.effect_key,
        attempt_id=physical_row.attempt_id,
        session_operation_id=hold.session_operation_id,
        session_operation_epoch=hold.session_operation_epoch,
        ownership_generation=hold.creator_generation,
        lease_sha256=hold.lease_sha256,
        policy_sha256=hold.policy_sha256,
        source_checkpoint_id=hold.source_checkpoint_id,
        source_cursor=hold.source_cursor,
        source_snapshot_sha256=hold.source_snapshot_sha256,
    )
    paid_tail = prepare_classifier_paid_overflow_tail_plan(identity=identity, admitted=dispatched, creator_sequence=hold.creator_sequence)
    no_sdk_tail = prepare_classifier_no_sdk_tail_plan(
        admitted=dispatched,
        attempt_id=identity.attempt_id,
        requested_model=requested_model,
        input_sha256=binding.input_sha256,
        effect_key=binding.effect_key,
        creator_sequence=hold.creator_sequence,
        creator_generation=hold.creator_generation,
        claim_attempt=hold.claim_attempt,
        operation_epoch=hold.session_operation_epoch,
    )
    reserved = max(paid_tail.reserved_bytes, no_sdk_tail.reserved_bytes)
    combined = sha256(
        canonical_json(
            {
                "version": "composer.classifier-primary-v4-combined-tail.v1",
                "paid": paid_tail.fallback_contract_sha256,
                "no_sdk": no_sdk_tail.contract_sha256,
                "reserved_bytes": reserved,
            }
        ).encode("utf-8")
    ).hexdigest()
    if hold.reserved_bytes != reserved or hold.fallback_contract_sha256 != combined:
        raise AuditIntegrityError("classifier admission readback changed its original combined H")
    candidate = no_sdk_issuer.dispatched_candidate
    if candidate is None or candidate != dispatched:
        raise AuditIntegrityError("classifier admission readback lost the original committed dispatch object")
    return V4ClassifierProviderAdmission(
        ProviderAttempt(physical_row.attempt_id, restore_utc(physical_row.started_at)),
        candidate,
        hold,
        paid_tail,
        no_sdk_tail,
        binding.effect_key,
    )

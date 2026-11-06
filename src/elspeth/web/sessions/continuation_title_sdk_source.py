"""Original TITLE custody and stored-source proofs for the shared SDK terminal."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
)
from elspeth.web.sessions.continuation_sdk_source import SdkTerminalLiveSource, SdkTerminalSource
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan
from elspeth.web.sessions.continuation_title_no_sdk_tail import prepare_title_no_sdk_tail_plan
from elspeth.web.sessions.continuation_title_paid_incomplete_tail import prepare_title_paid_incomplete_tail_plan
from elspeth.web.sessions.continuation_title_paid_overflow_source import require_original_v4_title_physical_source_on_connection
from elspeth.web.sessions.continuation_title_paid_overflow_tail import prepare_title_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_title_provider import (
    PreparedTitleProvider,
    V4TitleProviderAdmission,
    _current,
    _descriptor,
    _source_chain,
    title_input_descriptor,
    title_source_chain_data_on_connection,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical


def _sdk_recipe(admission: V4TitleProviderAdmission) -> tuple[int, str]:
    # The separate helper preserves exact three-tail historical H bytes. An
    # older H cannot gain SDK authority from a numerically sufficient reserve.
    from elspeth.web.sessions.continuation_title_terminal_reservation import title_sdk_terminal_reservation

    return title_sdk_terminal_reservation(admission.tail, admission.incomplete_tail, admission.no_sdk_tail)


def require_title_sdk_source_on_connection(
    conn: Connection, *, custody: ProviderCallCustody, observation: V4SdkFailureObservation
) -> SdkTerminalLiveSource:
    prepared, admission = custody.continuation_title, custody.continuation_title_v4_admission
    if type(prepared) is not PreparedTitleProvider or type(admission) is not V4TitleProviderAdmission:
        raise AuditIntegrityError("TITLE SDK terminal requires its original nominal prepared admission")
    require_original_v4_sdk_failure(observation, custody=custody)
    running = prepared.running
    source = admission.hold
    if (
        custody.continuation_title is not prepared
        or custody.continuation_title_v4_admission is not admission
        or custody.context is not running.session_operation_context
        or observation.attempt is not admission.attempt
        or custody.attempt is not admission.attempt
        or admission.selected != admission.tail.admitted
        or admission.tail.identity.attempt_id != admission.attempt.attempt_id
    ):
        raise AuditIntegrityError("TITLE SDK terminal changed its original custody or paid identity")
    _current(conn, running, admission.selected, owed=True)
    descriptor, reference, stage, _original = _source_chain(conn, running, admission.selected, names=prepared.names)
    if (
        stage != "admitted"
        or reference != prepared.input.reference
        or descriptor != _descriptor(prepared.input)
        or title_input_descriptor(prepared.composer, prepared.request, prepared_input=prepared.provider_input) != prepared.input
    ):
        raise AuditIntegrityError("TITLE SDK terminal changed its complete original prepared request")
    reserved, contract_hash = _sdk_recipe(admission)
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != contract_hash:
        raise AuditIntegrityError("TITLE SDK terminal requires its original SDK-capable hold recipe")
    identity = admission.tail.identity
    sid, operation_id = source.session_id, source.operation_id
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == "provider:3:0",
        )
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    requested_model = require_original_v4_title_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent
    )
    call = custody.call
    if (
        prepared.request.kwargs["model"] != requested_model
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != identity.attempt_id
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or hold.state != "active"
        or hold.provider_attempt_id != identity.attempt_id
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
        or (
            call is not None
            and (
                call.call_id != identity.attempt_id
                or call.model_requested != requested_model
                or call.started_at != admission.attempt.started_at
            )
        )
    ):
        raise AuditIntegrityError("TITLE SDK terminal lacks its original unsettled dispatched source")
    tail = prepare_sdk_tail_plan(
        identity=identity, admitted=admission.selected, creator_sequence=source.creator_sequence, family=SdkTerminalFamily.TITLE
    )
    return SdkTerminalLiveSource(SdkTerminalSource(source, admission.selected, requested_model, tail, prepared.names), running)


def require_title_sdk_cold_source_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    admitted: StoredContinuationSnapshot,
    identity: PaidOverflowIdentity,
    effect: Row[Any],
    physical_row: Row[Any],
    parent: Row[Any],
    names: frozenset[str],
) -> SdkTerminalSource:
    if (
        type(source) is not ProviderTerminalHoldSource
        or type(admitted) is not StoredContinuationSnapshot
        or type(identity) is not PaidOverflowIdentity
        or type(names) is not frozenset
        or any(type(name) is not str for name in names)
        or source.effect_key != "provider:3:0"
        or identity.effect_key != source.effect_key
        or identity.attempt_id != effect.provider_attempt_id
        or admitted.cursor != source.source_cursor + 1
    ):
        raise AuditIntegrityError("TITLE SDK cold source requires original named physical TITLE admission")
    descriptor, _reference, stage, _original = title_source_chain_data_on_connection(
        conn,
        session_id=UUID(source.session_id),
        operation_id=source.operation_id,
        sequence=source.creator_sequence,
        generation=source.creator_generation,
        selected=admitted,
        names=names,
        terminal_failed_request=True,
    )
    if stage != "admitted" or descriptor.input_sha256 != source.source_input_sha256:
        raise AuditIntegrityError("TITLE SDK cold source lost its original admitted request")
    requested_model = require_original_v4_title_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent
    )
    paid = prepare_title_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    incomplete = prepare_title_paid_incomplete_tail_plan(identity=identity, admitted=admitted, creator_sequence=source.creator_sequence)
    no_sdk = prepare_title_no_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, requested_model=requested_model
    )
    from elspeth.web.sessions.continuation_title_terminal_reservation import title_sdk_terminal_reservation

    reserved, contract_hash = title_sdk_terminal_reservation(paid, incomplete, no_sdk)
    if source.reserved_bytes != reserved or source.fallback_contract_sha256 != contract_hash:
        raise AuditIntegrityError("TITLE SDK cold source changed its original SDK-capable hold recipe")
    tail = prepare_sdk_tail_plan(
        identity=identity, admitted=admitted, creator_sequence=source.creator_sequence, family=SdkTerminalFamily.TITLE
    )
    return SdkTerminalSource(source, admitted, requested_model, tail, names)

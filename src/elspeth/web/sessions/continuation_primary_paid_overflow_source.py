"""Original V4 PRIMARY physical request proof without retaining oversized audit bytes."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from sqlalchemy import Connection, Row, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.sessions.continuation_primary import RetainedPrimaryInput, _original_wire
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.time_normalization import restore_utc


def require_original_v4_primary_physical_source_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    descriptor: RetainedPrimaryInput,
    effect: Row[Any],
    physical: Row[Any],
    parent: Row[Any],
    call: ComposerLLMCall | None,
) -> str:
    """Bind one successful observed audit to the immutable pre-SDK wire and actor."""
    if type(source) is not ProviderTerminalHoldSource or type(descriptor) is not RetainedPrimaryInput:
        raise TypeError("V4 PRIMARY source proof requires owned hold and retained request")
    if call is not None and (type(call) is not ComposerLLMCall or call.call_id is None):
        raise AuditIntegrityError("V4 PRIMARY source proof received a foreign physical call")
    wire, tools = _original_wire(descriptor)
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == source.session_id,
            attempts.c.operation_id == source.operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == source.session_id)).scalar_one()
    if (
        descriptor.model != effect.requested_model
        or (
            call is not None
            and (
                call.call_id != physical.attempt_id
                or call.model_requested != descriptor.model
                or call.temperature != descriptor.temperature
                or call.seed != descriptor.seed
                or call.messages_hash != stable_hash(wire)
                or call.tools_spec_hash != (stable_hash(tools) if tools else None)
                or call.private_request != capture_provider_context(messages=wire, tools=tools or None)
                or restore_utc(physical.started_at) != call.started_at
            )
        )
        or creator is None
        or creator.claim_attempt != source.claim_attempt
        or creator.session_operation_id != source.session_operation_id
        or creator.session_operation_epoch != source.session_operation_epoch
        or sha256(creator.session_operation_lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or parent.actor_user_id != principal
        or physical.identity_id != principal
        or physical.session_id != source.session_id
        or physical.source != "composer"
        or physical.run_id is not None
        or physical.operation_id != source.session_operation_id
        or physical.operation_epoch != source.session_operation_epoch
        or sha256(physical.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or (effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (effect.physical_sequence, effect.physical_generation) != (source.creator_sequence, source.creator_generation)
        or effect.provider_attempt_id != physical.attempt_id
        or effect.input_sha256 != source.source_input_sha256
    ):
        raise AuditIntegrityError("V4 PRIMARY paid attempt/model differs from its original complete physical request")
    return descriptor.model

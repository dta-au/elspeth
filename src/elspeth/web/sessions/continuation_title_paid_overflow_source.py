"""Original TITLE physical/request source proof for V4 terminal write and cold read."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Row, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_title_provider import TitleInputDescriptor
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


def require_original_v4_title_physical_source_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    descriptor: TitleInputDescriptor,
    effect: Row[Any],
    physical: Row[Any],
    parent: Row[Any],
) -> str:
    """Bind the paid row/model to the immutable pre-SDK TITLE wire and actor."""
    if type(source) is not ProviderTerminalHoldSource or type(descriptor) is not TitleInputDescriptor:
        raise TypeError("V4 TITLE source proof requires owned original hold and input descriptor")
    wire, error = parse_json_strict(descriptor.wire_json)
    if error is not None or type(wire) is not dict or type(wire.get("options")) is not dict:
        raise AuditIntegrityError("V4 TITLE original input lost its closed provider options")
    requested = wire["options"].get("model")
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == source.session_id,
            attempts.c.operation_id == source.operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one_or_none()
    if (
        type(requested) is not str
        or not requested
        or descriptor.input_sha256 != source.source_input_sha256
        or effect.input_sha256 != descriptor.input_sha256
        or effect.requested_model != requested
        or effect.provider_attempt_id != physical.attempt_id
        or (effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (effect.physical_sequence, effect.physical_generation) != (source.creator_sequence, source.creator_generation)
        or creator is None
        or creator.claim_attempt != source.claim_attempt
        or creator.session_operation_id != source.session_operation_id
        or creator.session_operation_epoch != source.session_operation_epoch
        or physical.source != "auto_title"
        or physical.run_id is not None
        or physical.identity_id != parent.actor_user_id
        or physical.session_id != source.session_id
        or physical.operation_id != creator.session_operation_id
        or physical.operation_epoch != creator.session_operation_epoch
        or physical.lease_token != creator.session_operation_lease_token
    ):
        raise AuditIntegrityError("V4 TITLE paid attempt/model differs from its original physical request and actor")
    return requested

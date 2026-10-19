"""Resolve readable retention causes from immutable scheduler handoff evidence."""

from __future__ import annotations

from hashlib import sha256

from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import SinkEffectCapabilityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.secret_scrub import REDACTED_SECRET_TEXT, scrub_text_for_audit
from elspeth.contracts.sink_effects import SinkEffectErrorReason
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine._error_hash import compute_error_hash


def resolve_sink_error_reason(
    factory: RecorderFactory,
    *,
    run_id: str,
    token_id: str,
    row_id: str,
    sink_node_id: str,
    sink_name: str | None,
    outcome: TerminalOutcome,
    path: TerminalPath,
    error_hash: str | None,
) -> SinkEffectErrorReason:
    if outcome is not TerminalOutcome.FAILURE or path not in (TerminalPath.ON_ERROR_ROUTED, TerminalPath.QUARANTINED_AT_SOURCE):
        raise SinkEffectCapabilityError("Readable CSV error retention requires an exact durable source or transform failure cause")
    items = factory.query.get_sink_error_handoffs(run_id=run_id, token_id=token_id, row_id=row_id)
    if len(items) != 1:
        raise LandscapeRecordError("Readable CSV error retention requires one exact durable sink handoff")
    item = items[0]
    if (
        item.run_id != run_id
        or item.token_id != token_id
        or item.row_id != row_id
        or (sink_name is not None and item.pending_sink_name != sink_name)
        or item.pending_outcome != outcome.value
        or item.pending_path != path.value
        or item.pending_error_hash != error_hash
        or item.status not in (TokenWorkStatus.PENDING_SINK, TokenWorkStatus.LEASED, TokenWorkStatus.TERMINAL)
    ):
        raise LandscapeRecordError("Readable CSV error retention handoff disagrees with its exact routing identity")
    message = item.pending_error_message
    if message is None or not message.strip() or compute_error_hash(message) != error_hash:
        raise LandscapeRecordError("Readable CSV error retention handoff lacks its exact recorded cause")
    if scrub_text_for_audit(message) != message or REDACTED_SECRET_TEXT in message or "credential_scrub_failure" in message:
        raise SinkEffectCapabilityError("Readable CSV error retention cause is unavailable under credential redaction")
    text = message if len(message) <= 2048 else message[:2036] + " [truncated]"
    witness_hash = stable_hash(
        {
            "schema": "sink-error-handoff-v1",
            "work_item_id": item.work_item_id,
            "run_id": run_id,
            "token_id": token_id,
            "row_id": row_id,
            "sink_node_id": sink_node_id,
            "sink_name": item.pending_sink_name,
            "outcome": outcome.value,
            "path": path.value,
            "error_hash": error_hash,
            "message_sha256": sha256(message.encode("utf-8")).hexdigest(),
        }
    )
    return SinkEffectErrorReason(text=text, witness_id=item.work_item_id, witness_hash=witness_hash)

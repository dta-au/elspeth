"""QuarantineRouter: route a validation-failed source row to its sink.

Extracted from ``SourceIterationDriver.handle_quarantine_row`` (archived issue
elspeth-27d7bfc14b). Source quarantine is a self-contained workflow — validate
the destination and the plugin's error text, sanitize the row at the Tier-3
boundary, then hand it to the processor's fenced quarantine ingest, which
records the row, its token, the FAILED source node_state, the DIVERT
routing_event and a durable PENDING_SINK handoff in ONE transaction, and
returns the token's sink-bound ``(FAILURE, QUARANTINED_AT_SOURCE)`` result for
the shared outcome accumulator. It emits ``RowCreated`` telemetry and holds no
cross-method state beyond the ``RunCeremony`` used for telemetry, so it lives
as a focused collaborator the driver delegates to and unit tests can drive in
isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from elspeth.contracts import SourceRow
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.events import RowCreated
from elspeth.contracts.types import NodeID
from elspeth.core.canonical import sanitize_for_canonical, stable_hash
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.engine.orchestrator.run_state import LoopContext
from elspeth.engine.orchestrator.types import RouteValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from elspeth.contracts import SourceProtocol
    from elspeth.contracts.results import RowResult

# Backstop cap for plugin-authored quarantine error text (elspeth-a300402c58).
# Source plugins own producing input-free error strings
# (contracts/safe_validation_errors.py); this bound stops an
# unbounded or input-echoing plugin string from flooding every audit
# surface the text lands on (node_states.error_json, the DIVERT routing
# reason, exports). Genuine validation messages are far shorter.
QUARANTINE_ERROR_MAX_CHARS = 2000


def _bound_quarantine_error(error_text: str) -> str:
    """Truncate over-long quarantine error text with an explicit marker."""
    if len(error_text) <= QUARANTINE_ERROR_MAX_CHARS:
        return error_text
    elided = len(error_text) - QUARANTINE_ERROR_MAX_CHARS
    return f"{error_text[:QUARANTINE_ERROR_MAX_CHARS]} [truncated {elided} chars]"


class QuarantineRouter:
    """Route a quarantined source row directly to its configured sink.

    Holds only the ``RunCeremony`` used to emit ``RowCreated`` telemetry; all
    per-row state arrives through ``route`` arguments.
    """

    def __init__(self, *, ceremony: RunCeremony) -> None:
        self._ceremony = ceremony

    def route(
        self,
        run_id: str,
        source_id: NodeID,
        source_item: SourceRow,
        row_index: int,
        source_row_index: int,
        ingest_sequence: int,
        edge_map: Mapping[tuple[NodeID, str], str],
        loop_ctx: LoopContext,
        *,
        active_source: SourceProtocol,
    ) -> RowResult:
        """Handle a quarantined source row: record it and hand it durably to its configured sink.

        Returns the token's sink-bound ``(FAILURE, QUARANTINED_AT_SOURCE)``
        result; the caller passes it to ``accumulate_row_outcomes``, which
        moves the counters and routes it into ``pending_tokens`` like every
        other sink-bound token. Nothing here mutates counters or the pending
        buckets directly.

        Workflow:
        1. Validate the quarantine destination and the plugin's error text
           (plugin bugs crash before any durable write)
        2. Bound the error text; resolve the ``__quarantine__`` DIVERT edge
        3. Sanitize the row at the Tier-3 boundary
        4. Fenced quarantine ingest (row + token + FAILED source state +
           DIVERT routing event + PENDING_SINK handoff, one transaction)
        5. Emit telemetry
        """

        config = loop_ctx.config
        processor = loop_ctx.processor

        # Route quarantined row to configured sink
        # Per docs/guides/data-trust-and-error-handling.md §Plugin Ownership:
        # plugin bugs must crash, no silent drops
        quarantine_sink = source_item.quarantine_destination

        # Validate destination exists - crash on plugin bug
        if not quarantine_sink:
            raise RouteValidationError(
                f"Source '{active_source.name}' yielded quarantined row "
                f"(source_row_index={source_row_index}, ingest_sequence={ingest_sequence}) "
                f"with missing quarantine_destination. "
                f"This is a plugin bug: quarantined rows MUST specify a destination. "
                f"Use SourceRow.quarantined(row, error, destination, source_row_index=...) factory method."
            )
        if quarantine_sink not in config.sinks:
            raise RouteValidationError(
                f"Source '{active_source.name}' yielded quarantined row "
                f"(source_row_index={source_row_index}, ingest_sequence={ingest_sequence}) "
                f"with invalid quarantine_destination='{quarantine_sink}'. "
                f"No sink named '{quarantine_sink}' exists. "
                f"Available sinks: {sorted(config.sinks.keys())}. "
                f"This is a plugin bug: quarantine_destination must match "
                f"source._on_validation_failure='{active_source._on_validation_failure}'."
            )
        quarantine_error_msg = source_item.quarantine_error
        if quarantine_error_msg is None or not quarantine_error_msg.strip():
            raise RouteValidationError(
                f"Source '{active_source.name}' yielded quarantined row "
                f"(source_row_index={source_row_index}, ingest_sequence={ingest_sequence}) "
                f"with missing quarantine_error. "
                f"This is a plugin bug: quarantined rows MUST specify a non-empty validation error. "
                f"Use SourceRow.quarantined(row, error, destination, source_row_index=...) factory method."
            )
        # Backstop length-bound (elspeth-a300402c58): applied BEFORE every use
        # below — node_state error, routing reason, pending-sink message and
        # error_hash all see the same bounded text, so the hash stays stable
        # for the persisted evidence.
        quarantine_error_msg = _bound_quarantine_error(quarantine_error_msg)

        # The __quarantine__ DIVERT edge MUST exist — DAG creates it in the
        # source quarantine edge block of from_plugin_instances().
        quarantine_edge_key = (source_id, "__quarantine__")
        try:
            quarantine_edge_id = edge_map[quarantine_edge_key]
        except KeyError as exc:
            raise OrchestrationInvariantError(
                f"Quarantine row reached orchestrator but no __quarantine__ "
                f"DIVERT edge exists in DAG for source '{source_id}'. "
                f"This is a DAG construction bug — "
                f"on_validation_failure should have created a DIVERT edge "
                f"in from_plugin_instances()."
            ) from exc

        validation_error_id = source_item.validation_error_id
        if validation_error_id is None:
            validation_error_id = loop_ctx.ctx.pop_pending_quarantine_validation_error_id(source_item.row)
        # Sanitize quarantine data at Tier-3 boundary: replace non-finite
        # floats (NaN, Infinity) with None so downstream canonical JSON
        # and stable_hash operations succeed. The quarantine_error records
        # what was originally wrong with the data.
        sanitized_row = sanitize_for_canonical(source_item.row)

        # ONE leader-fenced transaction (ADR-030 §C.4 row 9): the row, its
        # token, the FAILED source state, the DIVERT routing event and the
        # durable PENDING_SINK handoff to the quarantine sink. Its outcome is
        # recorded after sink durability, like every other sink-bound token.
        result = processor.ingest_quarantined_row(
            source_node_id=source_id,
            row_index=row_index,
            source_row_index=source_row_index,
            ingest_sequence=ingest_sequence,
            row=sanitized_row,
            validation_error_id=validation_error_id,
            quarantine_sink=quarantine_sink,
            quarantine_error=quarantine_error_msg,
            quarantine_edge_id=quarantine_edge_id,
        )

        # Emit RowCreated telemetry AFTER Landscape recording succeeds.
        # The row was already sanitized for Tier-3 non-canonical values
        # (NaN/Infinity -> None) above, so stable_hash gives a single deterministic
        # semantics for content_hash. No repr_hash fallback: after sanitization the
        # only residual stable_hash failure is a structurally non-serializable type,
        # which is a plugin-contract violation that must surface, not be masked by a
        # second, divergent hash function recorded under the same field name.
        self._ceremony.emit_telemetry(
            RowCreated(
                timestamp=datetime.now(UTC),
                run_id=run_id,
                row_id=result.token.row_id,
                token_id=result.token.token_id,
                content_hash=stable_hash(sanitized_row),
            )
        )
        return result

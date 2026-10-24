"""Complete ordinary runtime source read on the existing current SQL owner.

Returned values retain source and clock observations. They grant no action,
provider allowance, fresh inspection, mutation or audit-finisher authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_bootstrap import ContinuationBootstrapSnapshot, ContinuationBootstrapValues
from elspeth.web.composer.continuation_clock import ContinuationTimingObservation, freeze_observation
from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import (
    CurrentContinuationEnvInventory,
    require_current_env_inventory_on_connection,
)
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot
from elspeth.web.composer.continuation_iteration import ContinuationIterationSnapshot, decode_continuation_iteration
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicy, decode_continuation_policy
from elspeth.web.composer.continuation_prompt_producers import _CurrentProducerInputs, _read_current_inputs
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class ContinuationRuntimeSource:
    selected: SelectedContinuationState
    bootstrap: ContinuationBootstrapSnapshot
    original: ContinuationBootstrapValues
    iteration: ContinuationIterationSnapshot
    evidence: TurnEvidenceSnapshot
    policy: ContinuationAdmissionPolicy
    timing: ContinuationTimingObservation


def runtime_chunk(selected: SelectedContinuationState, reference: PrivateChunkRef) -> PrivateChunk:
    matches = tuple(chunk for chunk in selected.chunks if chunk.reference == reference)
    if len(matches) != 1:
        raise AuditIntegrityError("runtime lost one complete selected private source")
    verify_private_chunk(reference, matches[0].canonical)
    return matches[0]


def read_runtime_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> ContinuationRuntimeSource:
    if type(running) is not ComposerOperationRunning or type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("runtime read requires its actual current and selected owners")
    selected.__post_init__()
    # Reuse the installed full ingress/evidence/actor/creator/current-inventory
    # checks. No historical authorization class or second bootstrap grant.
    inputs = _read_current_inputs(conn, selected=selected, inventory=inventory)
    return _runtime_source_from_inputs_on_connection(conn, running, selected=selected, inventory=inventory, inputs=inputs)


def _runtime_source_from_inputs_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    inputs: _CurrentProducerInputs,
) -> ContinuationRuntimeSource:
    """Finish the same current SQL read using its already admitted DATA."""
    # Keep the later live claim/lease/CAS and explicit running-owner check.
    # The complete evidence graph was already validated on this connection;
    # the intervening bootstrap/transcript work only reads under the same lock.
    require_current_env_inventory_on_connection(
        conn,
        running,
        inventory=inventory,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
    )
    evidence = inputs.evidence
    current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
    if load_selected_private_chunks_on_connection(conn, running, current) != selected.chunks:
        raise AuditIntegrityError("runtime read changed its complete current SQL chunk cohort")
    frames = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(frames) != 1:
        raise AuditIntegrityError("runtime requires its one complete Compose owner")
    frame = frames[0]
    if frame.slice_sequence != running.claim.segment_sequence:
        raise AuditIntegrityError("runtime cannot reset a slice without its actual segment transition")
    if frame.composition_total < frame.composition_slice or frame.discovery_total < frame.discovery_slice:
        raise AuditIntegrityError("runtime lost its original accumulated logical counters")
    iteration = decode_continuation_iteration(runtime_chunk(selected, frame.iteration), env_ref_names=inventory.names)
    if iteration.producer_inputs != inputs.producer_inputs:
        raise AuditIntegrityError("runtime iteration differs from its actual current message inputs")
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == str(running.claim.session_id),
            segments.c.operation_id == running.claim.operation_id,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
    ).one_or_none()
    if segment is None or segment.deadline_at is None or segment.cooperative_at is None:
        raise AuditIntegrityError("runtime has no actual admitted segment clock")
    if segment.policy_sha256 != selected.stack.policy_hash:
        raise AuditIntegrityError("runtime segment changed its retained timing policy")
    timing = freeze_observation(
        sequence=segment.sequence,
        generation=segment.generation,
        now=database_now(conn),
        deadline=restore_utc(segment.deadline_at),
        cooperative=restore_utc(segment.cooperative_at),
        watermark=policy.segment.watermark,
    )
    return ContinuationRuntimeSource(selected, inputs.bootstrap, inputs.values, iteration, evidence, policy, timing)

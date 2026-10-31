"""Complete ordinary runtime source read on the existing current SQL owner.

Returned values retain source and clock observations. They grant no action,
provider allowance, fresh inspection, mutation or audit-finisher authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_attachment_history import ContinuationAttachmentHistory
from elspeth.web.composer.continuation_bootstrap import ContinuationBootstrapSnapshot, ContinuationBootstrapValues
from elspeth.web.composer.continuation_clock import ContinuationTimingObservation, freeze_observation
from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import (
    CurrentContinuationEnvInventory,
    require_current_env_inventory_on_connection,
)
from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence, TurnEvidenceSnapshot
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
    admitted_evidence: AdmittedTurnEvidence = field(compare=False)
    attachments: ContinuationAttachmentHistory
    evidence_env_ref_names: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        if type(self.admitted_evidence) is not AdmittedTurnEvidence or self.admitted_evidence.snapshot is not self.evidence:
            raise AuditIntegrityError("runtime source changed its resolved original evidence cohort")
        if type(self.attachments) is not ContinuationAttachmentHistory or (self.attachments.session_id, self.attachments.operation_id) != (
            self.selected.stack.logical.session_id,
            self.selected.stack.logical.operation_id,
        ):
            raise AuditIntegrityError("runtime source changed its complete original attachment history")
        # Independent current reads produce different local admission owners;
        # compare portable DATA and the actual credential policy, not a pointer.
        object.__setattr__(self, "evidence_env_ref_names", self.admitted_evidence.env_ref_names)


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
    if (
        type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
    ):
        raise AuditIntegrityError("runtime read requires its actual current and selected owners")
    selected.__post_init__()
    if (selected.stack.logical.session_id, selected.stack.logical.operation_id) != (
        str(running.claim.session_id),
        running.claim.operation_id,
    ):
        raise AuditIntegrityError("runtime read changed its actual logical request owner")
    frame = selected.stack.frames[-1]
    if type(frame) is ToolBatchFrame:
        from elspeth.web.composer.continuation_phase_transitions import (
            require_completed_runtime_prefix_on_connection,
            require_issued_tool_runtime_on_connection,
        )

        if frame.phase == "effect_reconciling" and frame.accumulator.codec == "composer.tool-batch.v1":
            require_issued_tool_runtime_on_connection(conn, running, selected=selected, env_ref_names=inventory.names)
        elif frame.phase == "tool_ready" and frame.accumulator.codec == "composer.coupled-batch-data.v1" and frame.next_tool_index:
            require_completed_runtime_prefix_on_connection(conn, running, selected=selected, env_ref_names=inventory.names)
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
    owed_tool_prefix: tuple[str, ...] | None = None,
    owed_tool_issue: str | None = None,
) -> ContinuationRuntimeSource:
    """Finish the same current SQL read using its already admitted DATA."""
    # Keep the later live claim/lease/CAS and explicit running-owner check.
    # The complete evidence graph was already validated on this connection;
    # the intervening bootstrap/transcript work only reads under the same lock.
    if owed_tool_prefix is not None and owed_tool_issue is not None:
        raise AuditIntegrityError("runtime read mixed completed-prefix and issued-call sources")
    if owed_tool_prefix is None and owed_tool_issue is None:
        require_current_env_inventory_on_connection(
            conn,
            running,
            inventory=inventory,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
        )
    else:
        from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
        from elspeth.web.composer.continuation_phase_transitions import (
            require_completed_tool_prefix_on_connection,
            require_issued_tool_runtime_on_connection,
        )

        if (
            type(inventory) is not CurrentContinuationEnvInventory
            or inventory.running is not running
            or inventory.selection.checkpoint_id != selected.checkpoint_id
            or inventory.selection.cursor != selected.stack.cursor
            or inventory.selection.snapshot_json != encode_continuation_snapshot(selected.stack)
        ):
            raise AuditIntegrityError("owed runtime changed its actual issued source/inventory")
        if owed_tool_prefix is not None:
            if (
                require_completed_tool_prefix_on_connection(conn, running, selected=selected, env_ref_names=inventory.names)
                != owed_tool_prefix
            ):
                raise AuditIntegrityError("owed runtime replaced its actual completed prefix")
        elif require_issued_tool_runtime_on_connection(conn, running, selected=selected, env_ref_names=inventory.names) != owed_tool_issue:
            raise AuditIntegrityError("owed runtime replaced its original issued call")
    evidence = inputs.evidence
    if evidence.application_entry is not None:
        from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_on_connection

        require_p4_application_journal_on_connection(
            conn, running, selected=selected, evidence=inputs.admitted_evidence, env_ref_names=inventory.names
        )
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
    from elspeth.web.sessions.continuation_attachment_application import load_attachment_history_sources_on_connection

    attachments = load_attachment_history_sources_on_connection(conn, running, selected=selected, names=inventory.names).history
    timing = freeze_observation(
        sequence=segment.sequence,
        generation=segment.generation,
        now=database_now(conn),
        deadline=restore_utc(segment.deadline_at),
        cooperative=restore_utc(segment.cooperative_at),
        watermark=policy.segment.watermark,
    )
    return ContinuationRuntimeSource(
        selected, inputs.bootstrap, inputs.values, iteration, evidence, policy, timing, inputs.admitted_evidence, attachments
    )


def read_owed_batch_runtime_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> ContinuationRuntimeSource:
    """Complete current DATA read admitted by actual pre-Stop issued work."""
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
    from elspeth.web.composer.continuation_phase_transitions import require_completed_tool_prefix_on_connection
    from elspeth.web.composer.continuation_prompt_producers import _inputs_from_current_evidence_on_connection

    if (
        type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
    ):
        raise AuditIntegrityError("owed runtime lost its actual current owners")
    prefix = require_completed_tool_prefix_on_connection(conn, running, selected=selected, env_ref_names=inventory.names)
    evidence = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=inventory.names)
    inputs = _inputs_from_current_evidence_on_connection(conn, selected=selected, inventory=inventory, evidence=evidence)
    return _runtime_source_from_inputs_on_connection(
        conn,
        running,
        selected=selected,
        inventory=inventory,
        inputs=inputs,
        owed_tool_prefix=prefix,
    )


def read_owed_tool_runtime_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> ContinuationRuntimeSource:
    """Read complete DATA for one real pre-Stop issued call's retained finish."""
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
    from elspeth.web.composer.continuation_phase_transitions import require_issued_tool_runtime_on_connection
    from elspeth.web.composer.continuation_prompt_producers import _inputs_from_current_evidence_on_connection

    if (
        type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
    ):
        raise AuditIntegrityError("issued runtime lost its actual current owners")
    issue = require_issued_tool_runtime_on_connection(conn, running, selected=selected, env_ref_names=inventory.names)
    evidence = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=inventory.names)
    inputs = _inputs_from_current_evidence_on_connection(conn, selected=selected, inventory=inventory, evidence=evidence)
    return _runtime_source_from_inputs_on_connection(
        conn, running, selected=selected, inventory=inventory, inputs=inputs, owed_tool_issue=issue
    )

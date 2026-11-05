"""Retained allowance selection for the implemented committed P4 recovery cuts.

This selector sizes the next segment. It grants no dispatch, current metadata,
decoded witness or business writer. Complete current result/journal admission
still runs after the actual new claim and physical fence have been established.
Other phase owners remain explicit refusals until their real producers exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.advisor_output import ContinuationAdvisorDecision, ReadContinuationAction, ReviewContinuationAction
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationFrameStack,
    EndFrame,
    InitializeFrame,
    ToolBatchFrame,
)
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_end_values import decode_end_candidate
from elspeth.web.composer.continuation_policy import (
    ContinuationActionPolicy,
    ContinuationAdmissionPolicy,
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
from elspeth.web.composer.continuation_selection import SelectedContinuationState

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation import StoredContinuationSnapshot
    from elspeth.web.sessions.continuation_checkpoint_end_retry import VerifiedEndFormatRetry
    from elspeth.web.sessions.continuation_iteration_return import IterationStageData


def unpaid_early_outer_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    sequence: int | None,
    generation: int | None,
    selected: SelectedContinuationState,
    env_ref_names: frozenset[str],
) -> str | None:
    """Read the exact prebind EARLY outer after full structural admission.

    This is SQL DATA, not a caller clearance. Both callers use the same locked
    transaction as complete structural proof; paid children keep their owners.
    A prepared-source bridge is proved by its exact retained event and input.
    """
    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
    from elspeth.web.sessions.continuation import _has_active_terminal_hold, load_snapshot
    from elspeth.web.sessions.continuation_checkpoint_paid import read_unpaid_early_prepared_source_on_connection
    from elspeth.web.sessions.continuation_iteration_return import read_iteration_stage_data_on_connection
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
    from elspeth.web.sessions.models import quota_provider_attempts_table as physical
    from elspeth.web.sessions.models import token_usage_ledger_table as ledger

    root = selected.stack
    top = root.frames[-1]
    if (
        type(top) is not ToolBatchFrame
        or top.phase != "result_retained"
        or top.accumulator.codec != "composer.applied-p4-batch.v2"
        or type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
    ):
        return None
    candidate = f"advance-early:{top.response.digest}"
    if tuple(item.effect_id for item in root.effect_frontier if item.disposition in ("prepared", "dispatched", "unknown")) != (candidate,):
        return None
    stored = load_snapshot(conn, session_id=str(session_id), operation_id=operation_id)
    if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(root):
        raise AuditIntegrityError("unpaid EARLY changed its actual selected parent")
    if sequence is None or generation is None:
        return None
    prepared = read_unpaid_early_prepared_source_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        selected=stored,
        sequence=sequence,
        generation=generation,
        env_ref_names=env_ref_names,
    )
    origin = stored if prepared is None else prepared.source
    stage = read_iteration_stage_data_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=origin)
    if stage is None or stage.stage not in ("early_dispatched", "early_retry_ready"):
        return None
    key = stage.early_effect_key
    if (
        not stage.early_required
        or (stage.creator_sequence, stage.creator_generation) != (sequence, generation)
        or root.pending_owner_results
        or root.pending_settlement_ids
        or key != candidate
        or _has_active_terminal_hold(conn, session_id=str(session_id), operation_id=operation_id)
    ):
        raise AuditIntegrityError("unpaid EARLY lost its original sole outer and empty paid custody")
    current_effects = conn.execute(
        select(effects).where(effects.c.session_id == str(session_id), effects.c.operation_id == operation_id)
    ).all()
    outer = tuple(row for row in current_effects if row.effect_key == key)
    if len(outer) != 1 or outer[0].disposition != "dispatched" or outer[0].receipt_key is not None or outer[0].result_chunk_key is not None:
        raise AuditIntegrityError("unpaid EARLY changed its original dispatched outer")
    # Previous PRIMARY calls are real paid work. Bind the complete physical
    # inventory to the logical operation's original fences and settled effects;
    # an orphan attempt cannot disappear merely because it lacks an effect link.
    actual = conn.execute(
        select(
            physical,
            attempts.c.sequence.label("attempt_sequence"),
            attempts.c.generation.label("attempt_generation"),
            attempts.c.session_operation_epoch.label("attempt_epoch"),
            attempts.c.session_operation_lease_token.label("attempt_lease"),
            ledger.c.entry_id.label("usage_entry"),
            ledger.c.identity_id.label("usage_identity"),
            ledger.c.session_id.label("usage_session"),
            ledger.c.source.label("usage_source"),
            ledger.c.run_id.label("usage_run"),
        )
        .outerjoin(ledger, ledger.c.entry_id == physical.c.ledger_entry_id)
        .join(
            attempts,
            (attempts.c.session_id == physical.c.session_id) & (attempts.c.session_operation_id == physical.c.operation_id),
        )
        .where(attempts.c.session_id == str(session_id), attempts.c.operation_id == operation_id)
    ).all()
    bound = tuple(row for row in current_effects if row.provider_attempt_id is not None)
    by_attempt = {row.provider_attempt_id: row for row in bound}
    if {row.attempt_id for row in actual} != set(by_attempt) or len(actual) != len(bound) or len(by_attempt) != len(bound):
        raise AuditIntegrityError("unpaid EARLY lost its complete prior physical inventory")
    for attempt in actual:
        linked = by_attempt[attempt.attempt_id]
        if (
            linked.disposition not in ("committed", "refused")
            or (linked.physical_sequence, linked.physical_generation) != (attempt.attempt_sequence, attempt.attempt_generation)
            or attempt.operation_epoch != attempt.attempt_epoch
            or attempt.lease_token != attempt.attempt_lease
            or attempt.settled_at is None
            or attempt.usage_entry is None
            or (attempt.usage_identity, attempt.usage_session, attempt.usage_source, attempt.usage_run)
            != (attempt.identity_id, attempt.session_id, attempt.source, attempt.run_id)
        ):
            raise AuditIntegrityError("unpaid EARLY contains an unsettled or foreign prior physical call")
    return key


def validate_continuation_structure_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    *,
    env_ref_names: frozenset[str],
) -> tuple[str, ...]:
    """Validate the actual full phase before granting an ambiguous disposition.

    This is a read facet under the caller's current transaction, never an
    exception-to-allowance conversion. Reuse the original graph/input/receipt
    readers; a matching effect label alone cannot validate corrupted DATA.
    """
    from elspeth.web.sessions.continuation import _require_current_continuation_running

    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    return validate_continuation_structure_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        sequence=running.claim.segment_sequence,
        generation=running.claim.ownership_generation,
        selected=selected,
        env_ref_names=env_ref_names,
    )


def validate_continuation_structure_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    sequence: int | None,
    generation: int | None,
    selected: SelectedContinuationState,
    env_ref_names: frozenset[str],
) -> tuple[str, ...]:
    """Same complete immutable admission under an independently proved SQL lock.

    IDs identify retained DATA only. This function grants no live custody,
    provider dispatch, filesystem use or mutation authority.
    """
    from elspeth.contracts.hashing import stable_hash
    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
    from elspeth.web.composer.continuation_prepared_p4 import p4_semantic_input
    from elspeth.web.composer.continuation_tool_batch import _read_json_value
    from elspeth.web.sessions.continuation import load_snapshot
    from elspeth.web.sessions.continuation_p4 import read_pending_p4_data_on_connection, selected_prepared_p4_data_on_connection
    from elspeth.web.sessions.continuation_primary import (
        selected_primary_effect_data_on_connection,
        stored_primary_input_data_on_connection,
    )
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
    from elspeth.web.sessions.continuation_title_provider import validate_title_source_data_on_connection
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects

    if type(selected) is not SelectedContinuationState or type(env_ref_names) is not frozenset:
        raise TypeError("structural admission requires its actual selected DATA and source names")
    selected.__post_init__()
    root = selected.stack
    if (root.logical.session_id, root.logical.operation_id) != (str(session_id), operation_id):
        raise AuditIntegrityError("structural admission changed its logical SQL identity")
    stored = load_snapshot(conn, session_id=root.logical.session_id, operation_id=root.logical.operation_id)
    if (
        stored.checkpoint_id != selected.checkpoint_id
        or stored.payload_json != encode_continuation_snapshot(root)
        or load_snapshot_private_data_on_connection(conn, session_id=str(session_id), operation_id=operation_id, selected=stored)
        != selected.chunks
    ):
        raise AuditIntegrityError("structural admission changed its actual full selected root")
    actual = conn.execute(
        select(effects).where(effects.c.session_id == root.logical.session_id, effects.c.operation_id == root.logical.operation_id)
    ).all()
    if {(row.effect_key, row.disposition, row.receipt_key) for row in actual} != {
        (entry.effect_id, entry.disposition, entry.receipt_id) for entry in root.effect_frontier
    }:
        raise AuditIntegrityError("structural admission lost its exact SQL effect frontier")
    ambiguous = tuple(entry.effect_id for entry in root.effect_frontier if entry.disposition in ("prepared", "dispatched", "unknown"))
    top = root.frames[-1]
    if type(top) is AdvisorFrame and top.provider_effect_key is not None:
        from elspeth.web.sessions.continuation_semantic_advisor import validate_semantic_advisor_source_data_on_connection

        unresolved = validate_semantic_advisor_source_data_on_connection(
            conn, session_id=session_id, operation_id=operation_id, selected=stored
        )
        if top.phase == "awaiting_response":
            return unresolved
    if type(top) is InitializeFrame and (top.provider_effect_key is not None or top.title_response is not None):
        # Includes original bootstrap, every stage/recovery event, descriptor,
        # frontier and the original physical provider/accounting cohort.
        if top.ingress_phase == "ready":
            from elspeth.web.sessions.continuation_title_application import read_applied_title_ready_immutable_data_on_connection

            read_applied_title_ready_immutable_data_on_connection(
                conn,
                session_id=session_id,
                operation_id=operation_id,
                sequence=sequence,
                generation=generation,
                selected=stored,
                names=env_ref_names,
            )
        else:
            validate_title_source_data_on_connection(
                conn,
                session_id=session_id,
                operation_id=operation_id,
                sequence=sequence,
                generation=generation,
                selected=stored,
                names=env_ref_names,
            )
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        next_continuation_action_policy(selected=selected, policy=policy)
        return tuple(entry.effect_id for entry in root.effect_frontier if entry.disposition in ("dispatched", "unknown"))
    compose = tuple(frame for frame in root.frames if type(frame) is ComposeFrame)
    admitted: AdmittedTurnEvidence | None = None
    if compose:
        # The existing reader proves full shared evidence/recorder/messages,
        # original actor/base/ingress, selected batch and prior receipts. DATA
        # stays source-admitted through the real AdmittedTurnEvidence flow.
        admitted = read_selected_turn_evidence_graph_data_on_connection(
            conn, session_id=session_id, operation_id=operation_id, selected=selected, names=env_ref_names
        )
        if admitted.snapshot.application_entry is not None:
            from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_data_on_connection

            require_p4_application_journal_data_on_connection(
                conn, session_id=session_id, operation_id=operation_id, selected=selected, evidence=admitted, env_ref_names=env_ref_names
            )
    from elspeth.web.composer.continuation_phase_transitions import validate_tool_effects_data_on_connection

    tool_unresolved = validate_tool_effects_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=selected, env_ref_names=env_ref_names
    )
    if type(top) is ToolBatchFrame and top.accumulator.codec == "composer.tool-batch.v1" and tool_unresolved:
        from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data

        chunk = next(chunk for chunk in selected.chunks if chunk.reference == top.accumulator)
        prepared_tool = decode_prepared_batch_data(chunk.canonical, env_ref_names=env_ref_names)
        call = prepared_tool.batch.current_call
        key = f"tool:{top.response.digest}:{top.next_tool_index}"
        if (
            top.phase != "effect_reconciling"
            or call is None
            or call.index != top.next_tool_index
            or call.effect_key is not None
            or ambiguous != (key,)
            or tool_unresolved != ambiguous
            or root.pending_owner_results
            or root.pending_settlement_ids
        ):
            raise AuditIntegrityError("structural admission lost its exact issued tool/complete prepared graph")
        return ambiguous
    if type(top) is ComposeFrame and top.phase == "awaiting_response":
        if (
            len(root.frames) != 1
            or top.provider_effect_key is None
            or top.provider_input_sha256 is None
            or top.pending_response is not None
            or root.pending_owner_results
            or root.pending_settlement_ids
            or (root.active_action_json is None) != (root.active_action_source is None)
            or root.semantic_frontier is None
            or ambiguous != (top.provider_effect_key,)
        ):
            raise AuditIntegrityError("structural admission lost the closed in-flight PRIMARY shape")
        _stored, _root, frame, effect = selected_primary_effect_data_on_connection(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            checkpoint_id=stored.checkpoint_id,
            cursor=stored.cursor,
            effect_key=top.provider_effect_key,
            input_sha256=top.provider_input_sha256,
        )
        stored_primary_input_data_on_connection(conn, session_id=session_id, operation_id=operation_id, effect=effect, frame=frame)
        return ambiguous
    if type(top) is ToolBatchFrame and top.accumulator.codec == "composer.p4-pending-batch.v1":
        key = f"batch:{top.response.digest}:p4"
        prepared = selected_prepared_p4_data_on_connection(
            conn, stored, session_id=session_id, operation_id=operation_id, effect_key=key, env_ref_names=env_ref_names
        )
        matching = tuple(row for row in actual if row.effect_key == key)
        if len(matching) != 1 or top.phase != "effect_reconciling" or root.pending_settlement_ids:
            raise AuditIntegrityError("structural admission lost the selected P4 effect/pending cut")
        effect = matching[0]
        if effect.owner != "session_sql" or effect.input_sha256 != stable_hash(
            _read_json_value(p4_semantic_input(prepared, env_ref_names=env_ref_names))
        ):
            raise AuditIntegrityError("structural admission changed the original P4 input/owner")
        p4_unresolved = key in ambiguous
        if p4_unresolved:
            if set(ambiguous) != {key, *tool_unresolved} or root.pending_owner_results or effect.receipt_key is not None:
                raise AuditIntegrityError("structural admission mixed unresolved P4 and another frontier")
            return ambiguous
        read_pending_p4_data_on_connection(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id,
            expected_cursor=stored.cursor,
            effect_key=key,
            env_ref_names=env_ref_names,
        )
        if tool_unresolved:
            if set(ambiguous) != set(tool_unresolved):
                raise AuditIntegrityError("structural admission mixed a tool result with another unresolved owner")
            return ambiguous
    if type(top) is EndFrame and ambiguous:
        from elspeth.web.sessions.continuation_checkpoint_end_recovery import read_current_end_h_recovery_on_connection
        from elspeth.web.sessions.continuation_checkpoint_end_retry import read_retry_ready_action_source_on_connection

        held_end = read_current_end_h_recovery_on_connection(conn, selected=stored, names=env_ref_names)
        if held_end is not None:
            if (
                root.pending_owner_results
                or root.pending_settlement_ids != (held_end.attempt_id,)
                or set(ambiguous) != {held_end.effect_key, held_end.outer_effect_key}
            ):
                raise AuditIntegrityError("structural END H changed its exact paid pending frontier")
            return ambiguous
        retry_ready = read_retry_ready_action_source_on_connection(
            conn,
            session_id=str(session_id),
            operation_id=operation_id,
            selected=stored,
            names=env_ref_names,
        )
        if retry_ready is not None:
            policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
            next_continuation_action_policy(selected=selected, policy=policy, retry_ready=retry_ready)
            # One original paid child is complete and its format-retry marker
            # is current. The outer D remains open by design; the next owner
            # may restore its original gate, but this read grants no SDK use.
            return ()
        from elspeth.web.composer.continuation_end_effects import end_effect_key
        from elspeth.web.composer.continuation_end_transitions import END_EFFECT_OWNER

        compose_root = root.frames[0]
        if type(compose_root) is not ComposeFrame or compose_root.pending_response is None:
            raise AuditIntegrityError("structural admission lost the END review's paid PRIMARY response")
        key = end_effect_key(compose_root.pending_response)
        if root.active_action_source is not None:
            from elspeth.web.sessions.continuation_semantic_actions import read_original_dispatched_semantic_review_on_connection

            admission = read_original_dispatched_semantic_review_on_connection(
                conn,
                session_id=session_id,
                operation_id=operation_id,
                dispatch_checkpoint_id=stored.checkpoint_id,
                semantic_effect_key=root.active_action_source.effect_key,
            )
            if admission.owner_effect_key is None:
                raise AuditIntegrityError("structural END source lost its original prescribed Review effect")
            key = admission.owner_effect_key
        review = tuple(row for row in actual if row.effect_key == key)
        if (
            len(root.frames) != 2
            or top.phase != "review_needed"
            or ambiguous != (key,)
            or tool_unresolved
            or root.pending_owner_results
            or root.pending_settlement_ids
            or len(review) != 1
            or review[0].owner != END_EFFECT_OWNER
            or review[0].receipt_key is not None
            or review[0].result_chunk_key is not None
        ):
            raise AuditIntegrityError("structural admission lost the closed in-flight END review shape")
        # The review made provider calls whose outcome is not durably known.
        # It is settled as unknown, never re-run by a new owner.
        return ambiguous
    if (
        type(top) is ToolBatchFrame
        and top.phase == "result_retained"
        and top.accumulator.codec == "composer.applied-p4-batch.v2"
        and ambiguous
        and not tool_unresolved
    ):
        early = unpaid_early_outer_on_connection(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            sequence=sequence,
            generation=generation,
            selected=selected,
            env_ref_names=env_ref_names,
        )
        if early is not None:
            return (early,)
        from elspeth.web.composer.continuation_iteration_transitions import (
            ITERATION_EFFECT_OWNER,
            advance_effect_key,
            applied_batch_cut,
        )

        _compose, batch = applied_batch_cut(root.frames)
        key = advance_effect_key(batch.response)
        phase = tuple(row for row in actual if row.effect_key == key)
        if (
            ambiguous != (key,)
            or root.pending_owner_results
            or root.pending_settlement_ids
            or len(phase) != 1
            or phase[0].owner != ITERATION_EFFECT_OWNER
            or phase[0].receipt_key is not None
            or phase[0].result_chunk_key is not None
        ):
            raise AuditIntegrityError("structural admission lost the closed in-flight post-batch shape")
        # The post-batch phase may have made Advisor calls; never re-run it.
        return ambiguous
    if tool_unresolved:
        if (
            type(top) is ToolBatchFrame
            and top.phase == "result_retained"
            and top.accumulator.codec == "composer.applied-p4-batch.v2"
            and set(ambiguous) == set(tool_unresolved)
            and not root.pending_owner_results
            and not root.pending_settlement_ids
            and admitted is not None
            and admitted.snapshot.application_entry is not None
        ):
            # The full application journal above proved the paid prefix and
            # its actual SQL receipt. An independently proved unknown tool
            # suffix remains unknown; it cannot turn that P4 into a handover.
            return ambiguous
        if (
            type(top) is not ToolBatchFrame
            or top.phase != "tool_ready"
            or top.accumulator.codec != "composer.coupled-batch-data.v1"
            or set(ambiguous) != set(tool_unresolved)
            or root.pending_owner_results
            or root.pending_settlement_ids
        ):
            raise AuditIntegrityError("structural admission lost its interrupted issued-tool prefix")
        return ambiguous
    if ambiguous:
        raise AuditIntegrityError("structural admission has no landed owner for this unresolved shape")
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    next_continuation_action_policy(selected=selected, policy=policy)
    return ()


class ContinuationCutNotResumable(AuditIntegrityError):
    """A structurally valid retained cut holds work no new owner may redo.

    The cut has an in-flight provider or business effect whose outcome is not
    durably known. A new generation could re-dispatch it, so the cut is never
    resumed: the operation is settled with the effect recorded as unknown.
    It subclasses ``AuditIntegrityError`` so every caller that treated this
    refusal as an integrity stop keeps doing so; only recovery and drain
    catch it specifically (design §3.4, §5.2).
    """

    def __init__(self, message: str, *, effect_ids: tuple[str, ...]) -> None:
        super().__init__(message)
        self.effect_ids = effect_ids


def require_resumable_cut(stack: ContinuationFrameStack) -> None:
    """Classify a decoded, chunk-verified stack against the explicit in-flight shapes.

    Runs after structural validation (codec decode and private-chunk digest
    checks) and before allowance selection. A match raises
    ``ContinuationCutNotResumable``; anything else falls through to the
    selector, whose own refusals stay plain ``AuditIntegrityError``.
    """
    unknown = tuple(item.effect_id for item in stack.effect_frontier if item.disposition == "unknown")
    if unknown:
        raise ContinuationCutNotResumable("retained cut holds an unknown effect outcome", effect_ids=unknown)
    top = stack.frames[-1]
    if type(top) is ComposeFrame and top.phase == "awaiting_response" and top.provider_effect_key is not None:
        in_flight = tuple(
            item.effect_id
            for item in stack.effect_frontier
            if item.effect_id == top.provider_effect_key and item.disposition in ("prepared", "dispatched")
        )
        if in_flight:
            raise ContinuationCutNotResumable("next allowance cannot retry an unresolved primary provider effect", effect_ids=in_flight)
    if type(top) is ToolBatchFrame:
        in_flight = tuple(item.effect_id for item in stack.effect_frontier if item.disposition in ("prepared", "dispatched"))
        if in_flight:
            raise ContinuationCutNotResumable("next allowance cannot retry a prepared or unknown business effect", effect_ids=in_flight)


def _is_first_compose_iteration(frame: ComposeFrame) -> bool:
    """The admitted first Compose cut: nothing has run in the request yet."""
    return (
        frame.slice_sequence == 1
        and frame.composition_total == 0
        and frame.discovery_total == 0
        and frame.advisor_calls == 0
        and frame.repair_turns == 0
        and frame.advisor_checkpoint_passes == 0
        and not frame.mutation_success_seen
        and frame.persisted_assistant_message_id is None
    )


def _has_committed_primary(root: ContinuationFrameStack) -> bool:
    """Structural sizing only; the current SQL owner must prove paid sources.

    A control can finish without spending a discovery/composition turn or
    writing an assistant chat row. Its retained PRIMARY still makes this a
    later cut; zero counters cannot turn it back into virgin initialization.
    """
    return any(
        item.effect_id.startswith("provider:0:") and item.disposition == "committed" and item.receipt_id is not None
        for item in root.effect_frontier
    )


def _require_quiescent_handoff_cut(root: ContinuationFrameStack) -> None:
    """A cut sized only for a handoff: no pending settlement, owner result or open effect."""
    if root.pending_settlement_ids or root.pending_owner_results or not root.is_handoff_safe:
        raise AuditIntegrityError("next allowance requires a quiescent handoff-safe cut")
    if any(item.disposition not in ("committed", "refused") for item in root.effect_frontier):
        raise AuditIntegrityError("next allowance cut still holds an open effect")


def handoff_cut_allowance(
    root: ContinuationFrameStack,
    policy: ContinuationAdmissionPolicy | ContinuationAdmissionPolicyV2 | ContinuationAdmissionPolicyV3 | ContinuationAdmissionPolicyV4,
) -> ContinuationActionPolicy | None:
    """Size the handoff-safe cuts no recovery owner produces; None for every other cut.

    These cuts exist only after a yield (design §2.4): a later Compose
    iteration, an Advisor input, an End phase, or a general ``tool_ready``.
    Sizing is structural and reads no private chunk; the owner of the cut
    re-proves it after the new claim and fence.
    """
    top = root.frames[-1]
    if type(top) is ComposeFrame and top.phase == "action_ready" and (not _is_first_compose_iteration(top) or _has_committed_primary(root)):
        _require_quiescent_handoff_cut(root)
        if len(root.frames) != 1 or top.provider_effect_key is not None or top.pending_response is not None or top.tools is None:
            raise AuditIntegrityError("next allowance lost its closed later Compose iteration cut")
        if root.semantic_frontier is None:
            raise AuditIntegrityError("later Compose iteration lost its retained semantic frontier")
        if (root.active_action_json is None) != (root.active_action_source is None):
            raise AuditIntegrityError("later Compose iteration lost its paired prescribed action source")
        return policy.actions.advisor if root.active_action_json is None else policy.actions.primary
    if type(top) is AdvisorFrame and top.phase == "input_ready":
        _require_quiescent_handoff_cut(root)
        if top.provider_effect_key is not None or top.response is not None:
            raise AuditIntegrityError("next allowance lost its closed Advisor input cut")
        return policy.actions.advisor
    if type(top) is EndFrame:
        _require_quiescent_handoff_cut(root)
        match top.phase:
            case "review_needed":
                return policy.actions.advisor
            case "repair_needed":
                return policy.actions.primary
            case "publication_ready":
                return policy.actions.session_sql
    if (
        type(top) is ToolBatchFrame
        and top.phase == "tool_ready"
        and top.accumulator.codec not in ("composer.coupled-batch-data.v1", "composer.tool-batch.v1")
        and len(root.frames) == 2
        and type(root.frames[0]) is ComposeFrame
    ):
        _require_quiescent_handoff_cut(root)
        # The next tool's arguments are not decoded before the new owner holds
        # current names, so the cut is sized for the largest local action.
        return max(
            (policy.actions.read, policy.actions.session_sql, policy.actions.blob, policy.actions.external),
            key=lambda action: action.total,
        )
    return None


def is_consumed_semantic_stop_cut(selected: SelectedContinuationState) -> bool:
    """Structural dispatch only; the final SQL owner must replay the real Stop."""
    end = selected.stack.frames[-1]
    if type(end) is not EndFrame or end.phase != "publication_ready":
        return False
    matches = tuple(chunk for chunk in selected.chunks if chunk.reference == end.candidate)
    if len(matches) != 1:
        raise AuditIntegrityError("semantic END classifier lost its complete candidate")
    candidate = decode_end_candidate(matches[0])
    if candidate.semantic_source is None or candidate.semantic_decision_json is None:
        return False
    decision = ContinuationAdvisorDecision.model_validate_json(candidate.semantic_decision_json, strict=True)
    if canonical_json(decision.model_dump(mode="json")) != candidate.semantic_decision_json:
        raise AuditIntegrityError("semantic END classifier changed its exact stored decision")
    return decision.decision == "STOP_CONFIRMED" and decision.scope == "whole_request" and decision.prescribed_action is None


def iteration_stage_action_policy(
    *,
    selected: StoredContinuationSnapshot,
    stage: IterationStageData | None,
    policy: ContinuationAdmissionPolicy | ContinuationAdmissionPolicyV2 | ContinuationAdmissionPolicyV3 | ContinuationAdmissionPolicyV4,
) -> ContinuationActionPolicy:
    """Size an original source stage; this DATA grants no paid execution."""
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
    from elspeth.web.sessions.continuation import StoredContinuationSnapshot
    from elspeth.web.sessions.continuation_iteration_return import IterationStageData

    if type(selected) is not StoredContinuationSnapshot or type(policy) not in (
        ContinuationAdmissionPolicy,
        ContinuationAdmissionPolicyV2,
        ContinuationAdmissionPolicyV3,
        ContinuationAdmissionPolicyV4,
    ):
        raise TypeError("iteration allowance requires its nominal selected source and original policy")
    root = decode_continuation_snapshot(selected.payload_json)
    applied_batch_cut(root.frames)
    if root.cursor != selected.cursor or decode_continuation_policy(root.admission_policy_json, root.policy_hash) != policy:
        raise AuditIntegrityError("iteration allowance changed its original admitted policy")
    if stage is None:
        require_resumable_cut(root)
        return policy.actions.session_sql
    if type(stage) is not IterationStageData or stage.current != selected:
        raise AuditIntegrityError("iteration allowance changed its exact original stage source")
    if stage.stage in ("early_ready", "early_retry_ready", "returned"):
        return policy.actions.advisor
    if stage.stage == "end_ready":
        return policy.actions.advisor
    if stage.stage in ("publication_ready", "reply_refusal_ready"):
        return policy.actions.session_sql
    if stage.stage == "classify_ready":
        return policy.actions.primary
    raise AuditIntegrityError("iteration allowance cannot replay unsupported dispatched work")


def next_continuation_action_policy(
    *,
    selected: SelectedContinuationState,
    policy: ContinuationAdmissionPolicy | ContinuationAdmissionPolicyV2 | ContinuationAdmissionPolicyV3 | ContinuationAdmissionPolicyV4,
    retry_ready: VerifiedEndFormatRetry | None = None,
) -> ContinuationActionPolicy:
    if type(selected) is not SelectedContinuationState or type(policy) not in (
        ContinuationAdmissionPolicy,
        ContinuationAdmissionPolicyV2,
        ContinuationAdmissionPolicyV3,
        ContinuationAdmissionPolicyV4,
    ):
        raise TypeError("next allowance requires its actual selected DATA and retained policy")
    selected.__post_init__()
    root = selected.stack
    if decode_continuation_policy(root.admission_policy_json, root.policy_hash) != policy:
        raise AuditIntegrityError("next allowance changed its retained complete policy")
    if retry_ready is not None:
        from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
        from elspeth.web.sessions.continuation_checkpoint_end_retry import VerifiedEndFormatRetry

        if (
            type(retry_ready) is not VerifiedEndFormatRetry
            or type(policy) is not ContinuationAdmissionPolicyV4
            or retry_ready.selected.checkpoint_id != selected.checkpoint_id
            or retry_ready.selected.payload_json != encode_continuation_snapshot(root)
            or retry_ready.marker.operation_id != root.logical.operation_id
            or retry_ready.marker.session_id != root.logical.session_id
        ):
            raise AuditIntegrityError("next END retry allowance lost its exact current V4 source")
    require_resumable_cut(root)
    top = root.frames[-1]
    if type(top) is EndFrame:
        if retry_ready is not None:
            if (
                len(root.frames) != 2
                or type(root.frames[0]) is not ComposeFrame
                or top.phase != "review_needed"
                or root.pending_owner_results
                or root.pending_settlement_ids
                or tuple(item.effect_id for item in root.effect_frontier if item.disposition in ("prepared", "dispatched", "unknown"))
                != (retry_ready.marker.outer_effect_key,)
                or root.active_action_json is not None
                or root.active_action_source is not None
            ):
                raise AuditIntegrityError("next END retry allowance changed its original review cut")
            return policy.actions.advisor
        if (
            len(root.frames) != 2
            or type(root.frames[0]) is not ComposeFrame
            or root.pending_owner_results
            or root.pending_settlement_ids
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("semantic END sizing lost its complete quiescent current source")
        if root.active_action_json is not None:
            if (
                root.active_action_source is None
                or top.phase != "review_needed"
                or type(decode_prescribed_continuation_action(root.active_action_json)) is not ReviewContinuationAction
            ):
                raise AuditIntegrityError("semantic END sizing lost its original prescribed Review")
            return policy.actions.advisor
        if root.active_action_source is not None:
            raise AuditIntegrityError("semantic END sizing lost its paired action")
        return policy.actions.session_sql if is_consumed_semantic_stop_cut(selected) else policy.actions.advisor
    handoff = handoff_cut_allowance(root, policy)
    if handoff is not None:
        return handoff
    top = root.frames[-1]
    if type(top) is AdvisorFrame and top.phase in ("response_retained", "decision_ready"):
        if (
            len(root.frames) not in (2, 3)
            or type(root.frames[0]) is not ComposeFrame
            or (
                len(root.frames) == 3
                and (
                    not (
                        type(root.frames[1]) is EndFrame and root.frames[1].phase in ("review_needed", "repair_needed", "publication_ready")
                    )
                    and not (
                        type(root.frames[1]) is ToolBatchFrame
                        and root.frames[1].phase == "result_retained"
                        and root.frames[1].accumulator.codec == "composer.applied-p4-batch.v2"
                        and root.frames[1].response == root.frames[0].pending_response
                        and f"batch:{root.frames[1].response.digest}:p4" in root.frames[1].settled_effect_ids
                    )
                )
            )
            or top.provider_effect_key is None
            or top.provider_input_sha256 is None
            or (top.phase == "decision_ready" and top.response is None)
            or root.pending_owner_results
            or root.pending_settlement_ids
            or any(item.disposition in ("prepared", "dispatched", "unknown") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("semantic next allowance lost its complete retained original source")
        if (
            top.phase == "decision_ready"
            and root.active_action_json is not None
            and type(decode_prescribed_continuation_action(root.active_action_json)) is ReadContinuationAction
        ):
            return policy.actions.read
        return policy.actions.session_sql
    if type(top) is AdvisorFrame and top.phase == "awaiting_response":
        raise AuditIntegrityError("semantic next allowance cannot replay unresolved prepared/begun provider work")
    if type(top) is InitializeFrame and top.provider_effect_key is not None:
        entries = root.effect_frontier
        if (
            len(root.frames) != 1
            or top.ingress_phase != "title_pending"
            or top.provider_effect_key != "provider:3:0"
            or top.provider_input_sha256 is None
            or top.bootstrap is None
            or top.bootstrap.codec != "composer.bootstrap.v1"
            or top.ingress_receipt_json is None
            or len(entries) != 1
            or entries[0].effect_id != top.provider_effect_key
            or root.pending_owner_results
            or root.semantic_frontier is not None
            or root.active_action_json is not None
            or root.active_action_source is not None
        ):
            raise AuditIntegrityError("next allowance lost its closed TITLE provider cut")
        entry = entries[0]
        if entry.disposition == "prepared":
            admitted = (
                top.title_attempt_id is None and top.title_response is None and not root.pending_settlement_ids and entry.receipt_id is None
            )
        elif entry.disposition == "dispatched":
            admitted = (
                top.title_attempt_id is not None
                and top.title_response is None
                and entry.receipt_id is None
                and root.pending_settlement_ids == (top.title_attempt_id,)
            )
        elif entry.disposition == "committed":
            admitted = (
                top.title_attempt_id is not None
                and top.title_response is not None
                and top.title_response.codec == "composer.title-provider-result.v1"
                and entry.receipt_id == f"title:{top.title_response.digest}"
                and root.pending_settlement_ids in ((), (top.title_attempt_id,))
            )
        else:
            admitted = False
        if not admitted:
            raise AuditIntegrityError("next allowance cannot redispatch an unresolved TITLE provider")
        # SQL-only wait/result/accounting sizing. The current TITLE source owner
        # rechecks every event, physical creator, original audit and ledger.
        # No old prepared/dispatched cut can mint another provider invocation.
        return policy.actions.session_sql
    if type(top) is InitializeFrame:
        if top.ingress_phase == "ready" and top.title_response is not None:
            if (
                len(root.frames) != 1
                or top.bootstrap is None
                or top.bootstrap.codec != "composer.bootstrap.v1"
                or top.ingress_receipt_json is None
                or top.title_attempt_id is None
                or top.title_response.codec != "composer.title-provider-result.v1"
                or top.provider_effect_key is not None
                or top.provider_input_sha256 is not None
                or root.pending_owner_results
                or root.pending_settlement_ids
                or root.active_action_json is not None
                or root.active_action_source is not None
                or root.semantic_frontier is not None
                or len(root.effect_frontier) != 1
                or root.effect_frontier[0].effect_id != "provider:3:0"
                or root.effect_frontier[0].disposition != "committed"
                or root.effect_frontier[0].receipt_id != f"title:{top.title_response.digest}"
            ):
                raise AuditIntegrityError("next allowance lost its closed applied TITLE ready cut")
            # SQL-only sizing. The actual current ready reader must prove the
            # application event, settled predecessor and all original sources.
            return policy.actions.session_sql
        published = top.ingress_phase in ("title_pending", "ready")
        if (
            len(root.frames) != 1
            or top.ingress_phase not in ("reserved", "inserted", "title_pending", "ready")
            or top.title_attempt_id is not None
            or (not published and top.bootstrap is not None)
            or (published and (top.bootstrap is None or top.bootstrap.codec != "composer.bootstrap.v1"))
            or top.title_response is not None
            or top.provider_effect_key is not None
            or top.provider_input_sha256 is not None
            or root.effect_frontier
            or root.pending_owner_results
            or root.pending_settlement_ids
            or root.active_action_json is not None
            or root.active_action_source is not None
            or root.semantic_frontier is not None
            or (top.ingress_phase == "reserved" and top.ingress_receipt_json is not None)
            or (top.ingress_phase != "reserved" and top.ingress_receipt_json is None)
        ):
            raise AuditIntegrityError("next allowance has no closed reserved/inserted ingress cut")
        if published:
            if len(selected.chunks) != 1 or selected.chunks[0].reference != top.bootstrap:
                raise AuditIntegrityError("next allowance lost its complete published bootstrap source")
            snapshot = decode_continuation_bootstrap(selected.chunks[0])
            values = bootstrap_values(snapshot)
            if (
                snapshot.logical != root.logical
                or values.retained_context is None
                or values.retained_context.availability != "available"
                or top.ingress_phase != ("title_pending" if snapshot.auto_title_eligible else "ready")
            ):
                # Keep the original refusal for a manufactured later phase
                # carrying no actual complete bounded bootstrap input.
                raise AuditIntegrityError("next allowance has no closed reserved/inserted ingress cut")
            if root.logical.kind == "compose_recompose":
                from elspeth.contracts.hashing import stable_hash
                from elspeth.web.sessions.continuation_recompose_source import _receipt as recompose_receipt

                receipt = recompose_receipt(top.ingress_receipt_json or "")
                if (
                    top.ingress_phase != "ready"
                    or snapshot.auto_title_eligible
                    or receipt.session_id != root.logical.session_id
                    or receipt.operation_id != root.logical.operation_id
                    or receipt.original_user_message_id != root.logical.original_user_message_id
                    or receipt.original_last_sequence != snapshot.original_last_sequence
                    or receipt.transcript_sha256 != stable_hash(snapshot.records_json)
                    or receipt.original_session_title != snapshot.original_session_title
                ):
                    raise AuditIntegrityError("next allowance lost its closed Recompose original-source cut")
            elif root.logical.kind != "compose_message":
                raise AuditIntegrityError("next allowance has no admitted Initialize request kind")
        # Fresh TITLE input includes its real provider allowance. Other ingress
        # cuts size SQL work only. Every real owner still rechecks the original
        # bootstrap, actor, current fence and may-dispatch boundary after start.
        return policy.actions.title if top.ingress_phase == "title_pending" else policy.actions.session_sql
    if type(top) is ComposeFrame and top.phase == "action_ready":
        if (
            len(root.frames) != 1
            or top.provider_effect_key is not None
            or top.provider_input_sha256 is not None
            or top.pending_response is not None
            or top.initial_state != top.state
            or top.tools is None
            or top.composition_slice != 0
            or top.discovery_slice != 0
            or top.composition_total != 0
            or top.discovery_total != 0
            or top.advisor_calls != 0
            or top.repair_turns != 0
            or top.advisor_checkpoint_passes != 0
            or top.mutation_success_seen
            or top.persisted_assistant_message_id is not None
            or top.persisted_assistant_content is not None
            or top.persisted_tool_call_turn
            or top.advisor_repair_context_introduced
            or top.pending_advisor_elision_indices
            or root.pending_settlement_ids
            or root.pending_owner_results
            or root.semantic_frontier is None
            or root.active_action_json is not None
            or root.active_action_source is not None
            or len(root.effect_frontier) > 1
            or any(
                item.effect_id != "provider:3:0"
                or item.disposition != "committed"
                or item.receipt_id is None
                or not item.receipt_id.startswith("title:")
                for item in root.effect_frontier
            )
        ):
            raise AuditIntegrityError("next allowance has no closed unconsumed first Compose cut")
        # Structural sizing only. Fresh current initialization admission must
        # prove the real event/ready predecessor and complete captured graph.
        # The original PRIMARY preparer/gateway/guard/quota still own dispatch.
        return policy.actions.primary
    if type(top) is ComposeFrame and top.phase == "response_retained":
        matches = tuple(item for item in root.effect_frontier if item.effect_id == top.provider_effect_key)
        if (
            len(root.frames) != 1
            or root.pending_settlement_ids
            or root.pending_owner_results
            or root.active_action_json is not None
            or root.active_action_source is not None
            or top.pending_response is None
            or top.pending_response.codec != "composer.response.v1"
            or top.provider_effect_key is None
            or not top.provider_effect_key.startswith("provider:0:")
            or top.provider_input_sha256 is None
            or len(matches) != 1
            or matches[0].disposition != "committed"
            or not matches[0].receipt_id
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("next allowance cannot retry an unresolved primary provider effect")
        # A structural allowance only. The new actual claim must use the SQL
        # primary result reader and its original audit/accounting source joins.
        # It receives no provider allowance and never dispatches this result.
        return policy.actions.session_sql
    if type(top) is not ToolBatchFrame or sum(type(frame) is ComposeFrame for frame in root.frames) != 1:
        raise AuditIntegrityError("next allowance has no implemented retained P4 recovery cut")
    if top.accumulator.codec == "composer.tool-batch.v1":
        prefix = f"attachment:{top.response.digest}:"
        pending = root.pending_owner_results
        if top.phase == "tool_ready":
            if (
                len(root.frames) != 2
                or type(root.frames[0]) is not ComposeFrame
                or root.frames[0].pending_response != top.response
                or pending
                or root.pending_settlement_ids
                or root.active_action_json is not None
                or root.active_action_source is not None
                or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
                or any(item.effect_id == f"{prefix}{top.next_tool_index}" for item in root.effect_frontier)
            ):
                raise AuditIntegrityError("next allowance lost its unissued attachment preparation cut")
            # Sizing only. The current SQL runtime reader must authenticate the
            # original preparation event and recovery chain, and prove no read
            # effect exists, before a fresh current preparation opens its timer.
            return max(
                (policy.actions.read, policy.actions.session_sql, policy.actions.blob, policy.actions.external),
                key=lambda value: value.total,
            )
        if top.phase == "effect_reconciling" and len(pending) == 1 and pending[0].kind == "read":
            attachment_key = pending[0].effect_key
            admitted = pending[0].result.codec == "composer.owner-result.v1" and attachment_key not in top.settled_effect_ids
        elif top.phase == "result_retained" and not pending and top.settled_effect_ids:
            attachment_key = top.settled_effect_ids[-1]
            admitted = top.settled_effect_ids.count(attachment_key) == 1
        else:
            raise AuditIntegrityError("next allowance cannot repeat an unresolved attachment read")
        frontier = tuple(item for item in root.effect_frontier if item.effect_id == attachment_key)
        if (
            not admitted
            or not attachment_key.startswith(prefix)
            or len(root.frames) != 2
            or type(root.frames[0]) is not ComposeFrame
            or root.active_action_json is not None
            or root.active_action_source is not None
            or root.pending_settlement_ids
            or len(frontier) != 1
            or frontier[0].disposition != "committed"
            or frontier[0].receipt_id is None
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("next allowance lost its exact committed attachment read cut")
        # Only immutable-result/current-version SQL is admitted here. The complete
        # actual call, inner DATA, creator and current root are checked after start.
        return policy.actions.session_sql
    if (
        top.phase == "tool_ready"
        and top.accumulator.codec == "composer.coupled-batch-data.v1"
        and all(key.startswith((f"tool:{top.response.digest}:", f"attachment:{top.response.digest}:")) for key in top.settled_effect_ids)
    ):
        if (
            len(root.frames) != 2
            or type(root.frames[0]) is not ComposeFrame
            or root.frames[0].pending_response != top.response
            or root.pending_owner_results
            or root.pending_settlement_ids
            or root.active_action_json is not None
            or root.active_action_source is not None
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
            or len(top.settled_effect_ids) != len(set(top.settled_effect_ids))
            or any(
                len(
                    tuple(
                        item
                        for item in root.effect_frontier
                        if item.effect_id == key and item.disposition == "committed" and item.receipt_id is not None
                    )
                )
                != 1
                for key in top.settled_effect_ids
            )
        ):
            raise AuditIntegrityError("next allowance lost its complete current tool-batch prefix")
        # Sizing does not replace actual source-name admission or decode tool
        # arguments with an invented empty inventory. The current graph/phase
        # reader proves the complete native/attachment prefix and chooses the
        # real next tool/P4 owner after the actual claim starts. A completed
        # attachment closes only its own call, including in a mixed batch.
        return max(
            (policy.actions.read, policy.actions.session_sql, policy.actions.blob, policy.actions.external), key=lambda value: value.total
        )
    effect_key = f"batch:{top.response.digest}:p4"
    if top.phase == "effect_reconciling" and top.accumulator.codec == "composer.p4-pending-batch.v1":
        pending = root.pending_owner_results
        if (
            len(pending) != 1
            or pending[0].kind != "persisted_turn"
            or pending[0].effect_key != effect_key
            or pending[0].result.codec != "composer.owner-result.v1"
            or effect_key in top.settled_effect_ids
        ):
            raise AuditIntegrityError("next allowance cannot retry a prepared or unknown business effect")
    elif top.phase == "result_retained" and top.accumulator.codec == "composer.applied-p4-batch.v2":
        if root.pending_owner_results or effect_key not in top.settled_effect_ids:
            raise AuditIntegrityError("next allowance lost its actual settled P4 application cut")
    else:
        raise AuditIntegrityError("next allowance has no implemented retained P4 recovery cut")
    # This conservative structural branch performs no context-sensitive DATA
    # decode before the new running owner can obtain actual current names.
    # Both supported cuts consume only the already-committed result or journal.
    return policy.actions.session_sql

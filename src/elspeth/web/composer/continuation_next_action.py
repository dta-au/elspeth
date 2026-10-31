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
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationFrameStack,
    EndFrame,
    InitializeFrame,
    ToolBatchFrame,
)
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationAdmissionPolicy, decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning


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
            or root.active_action_json is not None
            or root.active_action_source is not None
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


def _require_quiescent_handoff_cut(root: ContinuationFrameStack) -> None:
    """A cut sized only for a handoff: no pending settlement, owner result or open effect."""
    if root.pending_settlement_ids or root.pending_owner_results or not root.is_handoff_safe:
        raise AuditIntegrityError("next allowance requires a quiescent handoff-safe cut")
    if any(item.disposition not in ("committed", "refused") for item in root.effect_frontier):
        raise AuditIntegrityError("next allowance cut still holds an open effect")


def handoff_cut_allowance(root: ContinuationFrameStack, policy: ContinuationAdmissionPolicy) -> ContinuationActionPolicy | None:
    """Size the handoff-safe cuts no recovery owner produces; None for every other cut.

    These cuts exist only after a yield (design §2.4): a later Compose
    iteration, an Advisor input, an End phase, or a general ``tool_ready``.
    Sizing is structural and reads no private chunk; the owner of the cut
    re-proves it after the new claim and fence.
    """
    top = root.frames[-1]
    if type(top) is ComposeFrame and top.phase == "action_ready" and not _is_first_compose_iteration(top):
        _require_quiescent_handoff_cut(root)
        if len(root.frames) != 1 or top.provider_effect_key is not None or top.pending_response is not None or top.tools is None:
            raise AuditIntegrityError("next allowance lost its closed later Compose iteration cut")
        if root.semantic_frontier is None:
            raise AuditIntegrityError("later Compose iteration lost its retained semantic frontier")
        return policy.actions.primary
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
        and top.accumulator.codec != "composer.coupled-batch-data.v1"
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


def next_continuation_action_policy(
    *, selected: SelectedContinuationState, policy: ContinuationAdmissionPolicy
) -> ContinuationActionPolicy:
    if type(selected) is not SelectedContinuationState or type(policy) is not ContinuationAdmissionPolicy:
        raise TypeError("next allowance requires its actual selected DATA and retained policy")
    selected.__post_init__()
    root = selected.stack
    if decode_continuation_policy(root.admission_policy_json, root.policy_hash) != policy:
        raise AuditIntegrityError("next allowance changed its retained complete policy")
    require_resumable_cut(root)
    handoff = handoff_cut_allowance(root, policy)
    if handoff is not None:
        return handoff
    top = root.frames[-1]
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
                or root.logical.kind != "compose_message"
                or values.retained_context is None
                or values.retained_context.availability != "available"
                or top.ingress_phase != ("title_pending" if snapshot.auto_title_eligible else "ready")
            ):
                # Keep the original refusal for a manufactured later phase
                # carrying no actual complete bounded bootstrap input.
                raise AuditIntegrityError("next allowance has no closed reserved/inserted ingress cut")
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
        and (not top.settled_effect_ids or all(key.startswith(f"tool:{top.response.digest}:") for key in top.settled_effect_ids))
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
        ):
            raise AuditIntegrityError("next allowance lost its complete current tool-batch prefix")
        # Sizing does not replace actual source-name admission or decode tool
        # arguments with an invented empty inventory. The current graph/phase
        # reader chooses the real tool/P4 owner after the actual claim starts.
        return max(
            (policy.actions.read, policy.actions.session_sql, policy.actions.blob, policy.actions.external), key=lambda value: value.total
        )
    if top.phase == "tool_ready" and top.accumulator.codec == "composer.coupled-batch-data.v1":
        key = f"attachment:{top.response.digest}:{top.next_tool_index - 1}"
        frontier = tuple(item for item in root.effect_frontier if item.effect_id == key)
        if (
            len(root.frames) != 2
            or type(root.frames[0]) is not ComposeFrame
            or root.frames[0].pending_response != top.response
            or top.next_tool_index < 1
            or root.pending_owner_results
            or root.pending_settlement_ids
            or root.active_action_json is not None
            or root.active_action_source is not None
            or not top.settled_effect_ids
            or top.settled_effect_ids[-1] != key
            or top.settled_effect_ids.count(key) != 1
            or len(frontier) != 1
            or frontier[0].disposition != "committed"
            or not frontier[0].receipt_id
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("next allowance lost its completed attachment application cut")
        # SQL-only structural allowance. The actual current runtime reader must
        # reprove the immutable application event/checkpoints/result/receipt;
        # no subsequent read/provider/business action is admitted by this DATA.
        return policy.actions.session_sql
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

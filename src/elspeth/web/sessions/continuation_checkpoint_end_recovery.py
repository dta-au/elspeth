"""Prove the exact paid END H that a peer may carry across a claim change.

The H checkpoint remains the selected head. A recovery edge is stored on its
revoked attempt, since adding a journal event would spend bytes outside H's
already admitted retention bound and would hide H from cold consequence resume.
This reader grants only an unpaid segment allowance; it grants no SDK call.
"""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_action import (
    require_original_end_outer_creator_after_complete_repair_on_connection,
    require_original_end_outer_creator_after_paid_overflow_on_connection,
    require_original_end_outer_creator_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    VerifiedCheckpointEndOwed,
    _bounded_event_text,
    read_v4_checkpoint_end_owed_on_connection,
    read_v4_checkpoint_end_settled_owed_history_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import load_continuation_read_contract
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.models import user_secrets_table as secrets

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation import ComposerSegmentRecoveryReceipt


def read_current_end_h_recovery_on_connection(
    conn: Connection,
    *,
    selected: StoredContinuationSnapshot,
    names: frozenset[str] | None = None,
) -> VerifiedCheckpointEndOwed | None:
    """Return an original, unchanged, still-current END H or refuse its forgery.

    A result authorizes only the existing advisor timing allowance for the
    replacement claim. It never settles the paid child or starts another call.
    """
    root = decode_continuation_snapshot(selected.payload_json)
    sid, operation = root.logical.session_id, root.logical.operation_id
    if len(root.frames) != 2 or type(root.frames[0]) is not ComposeFrame or type(root.frames[1]) is not EndFrame:
        return None
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4 or not root.pending_settlement_ids:
        return None
    if (
        root.cursor != selected.cursor
        or root.frames[1].phase != "review_needed"
        or len(root.pending_settlement_ids) != 1
        or load_snapshot(conn, session_id=sid, operation_id=operation) != selected
    ):
        raise AuditIntegrityError("END H recovery lost its exact current selected source")
    principal = conn.execute(select(sessions.c.user_id, sessions.c.auth_provider_type).where(sessions.c.id == sid)).one_or_none()
    parent_actor = conn.execute(
        select(jobs.c.actor_user_id).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)
    ).one_or_none()
    if principal is None or parent_actor is None or parent_actor.actor_user_id != principal.user_id:
        raise AuditIntegrityError("END H recovery lost its original actor")
    current_names = frozenset(
        conn.execute(
            select(secrets.c.name).where(
                secrets.c.user_id == principal.user_id,
                secrets.c.auth_provider_type == principal.auth_provider_type,
            )
        ).scalars()
    )
    if names is not None and names != current_names:
        raise AuditIntegrityError("END H recovery substituted its current credential inventory")
    owed = read_v4_checkpoint_end_owed_on_connection(
        conn,
        session_id=root.logical.session_id,
        operation_id=operation,
        attempt_id=root.pending_settlement_ids[0],
        names=current_names,
    )
    if owed is None or owed.selected != selected or root.pending_settlement_ids != (owed.attempt_id,):
        raise AuditIntegrityError("END H recovery lacks its deep original paid hold")
    ambiguous = tuple(item.effect_id for item in root.effect_frontier if item.disposition in {"prepared", "dispatched", "unknown"})
    if len(ambiguous) != 2 or set(ambiguous) != {owed.effect_key, owed.outer_effect_key}:
        raise AuditIntegrityError("END H recovery changed its exact two unresolved effects")
    original_sequence, _original_generation, _ = require_original_end_outer_creator_on_connection(conn, owed=owed)
    hold = owed.new_hold
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == original_sequence,
            attempts.c.generation == hold.creator_generation,
        )
    ).one_or_none()
    segment = conn.execute(
        select(segments).where(segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == original_sequence)
    ).one_or_none()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one_or_none()
    entry = (
        conn.execute(
            select(checkpoints.c.cursor).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation,
                checkpoints.c.checkpoint_id == segment.checkpoint_id,
            )
        ).one_or_none()
        if segment is not None
        else None
    )
    if (
        creator is None
        or segment is None
        or parent is None
        or entry is None
        or owed.phase_source is None
        or creator.session_operation_epoch != hold.session_operation_epoch
        or creator.claim_attempt != hold.claim_attempt
        or sha256(creator.session_operation_lease_token.encode("utf-8")).hexdigest() != hold.lease_sha256
        # A running creator retains the segment-entry pointer (often cursor 0)
        # while the parent advances through D/P/A/H. Recovery itself rebinds
        # the segment pointer to H; every replacement must keep it there.
        or (segment.generation == hold.creator_generation and entry.cursor > owed.phase_source.cursor)
        or (segment.generation > hold.creator_generation and segment.checkpoint_id != selected.checkpoint_id)
        or parent.checkpoint_id != selected.checkpoint_id
        or parent.status != "running"
        or parent.current_segment_sequence != original_sequence
        or parent.ownership_generation != segment.generation
        or segment.state not in ("running", "interrupted")
        or segment.generation < hold.creator_generation
        or hold.creator_sequence != original_sequence
        or root.pending_owner_results
    ):
        raise AuditIntegrityError("END H recovery lost its original creator or current claim")
    require_end_h_anchored_generation_chain_on_connection(conn, owed=owed, generation=segment.generation)
    if segment.state == "running":
        predecessor = (
            conn.execute(
                select(attempts.c.session_operation_epoch).where(
                    attempts.c.session_id == sid,
                    attempts.c.operation_id == operation,
                    attempts.c.sequence == original_sequence,
                    attempts.c.generation == segment.generation - 1,
                )
            ).one_or_none()
            if segment.generation > hold.creator_generation
            else None
        )
        active = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == original_sequence,
                attempts.c.generation == segment.generation,
            )
        ).one_or_none()
        if (
            active is None
            or active.revoked_at is not None
            or active.outcome is not None
            or active.claim_attempt != parent.attempt
            or active.session_operation_id != parent.session_operation_id
            or active.session_operation_epoch != parent.session_operation_epoch
            or active.session_operation_lease_token != parent.session_operation_lease_token
            or (
                segment.generation > hold.creator_generation
                and (predecessor is None or active.session_operation_epoch <= predecessor.session_operation_epoch)
            )
        ):
            raise AuditIntegrityError("END H recovery lost the current replacement claim/fence")
    return owed


def require_end_h_anchored_generation_chain_on_connection(conn: Connection, *, owed: VerifiedCheckpointEndOwed, generation: int) -> None:
    """Every predecessor after the paid creator must carry the same H anchor."""
    hold = owed.new_hold
    if type(generation) is not int or generation < hold.creator_generation:
        raise AuditIntegrityError("END H recovery rewound its original paid creator")
    previous_epoch = hold.session_operation_epoch
    previous_operation_id = hold.session_operation_id
    previous_lease_sha256 = hold.lease_sha256
    for edge_generation in range(hold.creator_generation, generation):
        edge = conn.execute(
            select(attempts).where(
                attempts.c.session_id == owed.session_id,
                attempts.c.operation_id == owed.operation_id,
                attempts.c.sequence == hold.creator_sequence,
                attempts.c.generation == edge_generation,
            )
        ).one_or_none()
        if (
            edge is None
            or edge.recovered_end_h_checkpoint_id != owed.selected.checkpoint_id
            or edge.revoked_at is None
            or edge.outcome != "interrupted"
            or (
                edge_generation == hold.creator_generation
                and (
                    edge.session_operation_epoch != hold.session_operation_epoch
                    or edge.session_operation_id != hold.session_operation_id
                    or sha256(edge.session_operation_lease_token.encode("utf-8")).hexdigest() != hold.lease_sha256
                )
            )
            or (
                edge_generation > hold.creator_generation
                and (
                    edge.session_operation_epoch <= previous_epoch
                    or edge.session_operation_id == previous_operation_id
                    or sha256(edge.session_operation_lease_token.encode("utf-8")).hexdigest() == previous_lease_sha256
                )
            )
        ):
            raise AuditIntegrityError("END H recovery lost a contiguous anchored claim edge")
        previous_epoch = edge.session_operation_epoch
        previous_operation_id = edge.session_operation_id
        previous_lease_sha256 = sha256(edge.session_operation_lease_token.encode("utf-8")).hexdigest()


def verify_end_h_recovery_receipt_on_connection(conn: Connection, receipt: ComposerSegmentRecoveryReceipt) -> None:
    """Reprove an anchored H-only generation edge even after later progress."""
    if receipt.held_end_attempt_id is None or receipt.generation != receipt.previous_generation + 1:
        raise AuditIntegrityError("END H recovery receipt lost its exact attempt/generation")
    sid, operation = str(receipt.session_id), receipt.operation_id
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    if contract.version != "composer.continuation.policy.v4" or type(contract.allowance) is not int:
        raise AuditIntegrityError("END H recovery lost its bounded V4 read contract")
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == receipt.checkpoint_id,
        )
    ).one_or_none()
    if checkpoint is None or checkpoint.cursor != receipt.cursor:
        raise AuditIntegrityError("END H recovery receipt substituted its checkpoint")
    tag, text = _bounded_event_text(conn, session_id=sid, operation_id=operation, cursor=receipt.cursor, allowance=contract.allowance)
    value, error = parse_json_strict(text)
    if (
        tag != "checkpoint"
        or error is not None
        or type(value) is not dict
        or value.get("owner") != "composer_checkpoint_end_owed"
        or value.get("attempt_id") != receipt.held_end_attempt_id
        or type(value.get("end_env_ref_names")) is not list
        or any(type(name) is not str for name in value["end_env_ref_names"])
    ):
        raise AuditIntegrityError("END H recovery receipt lost its original owed event")
    names = frozenset(value["end_env_ref_names"])
    effect = conn.execute(
        select(effects.c.disposition).where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation,
            effects.c.effect_key == value.get("effect_key"),
        )
    ).one_or_none()
    hold = conn.execute(
        select(holds.c.state).where(
            holds.c.session_id == sid,
            holds.c.operation_id == operation,
            holds.c.effect_key == value.get("effect_key"),
        )
    ).one_or_none()
    if effect is None or hold is None:
        raise AuditIntegrityError("END H recovery lost its original paid effect/hold")
    if effect.disposition == "dispatched":
        owed = read_v4_checkpoint_end_owed_on_connection(
            conn, session_id=sid, operation_id=operation, attempt_id=receipt.held_end_attempt_id, names=names
        )
        if owed is None or hold.state != "active":
            raise AuditIntegrityError("END H recovery lost its unsettled original child")
    elif effect.disposition in {"committed", "paid_overflow"}:
        # Receipt readback is a post-commit proof. The in-transaction terminal
        # reader may temporarily inspect a settled child before consuming H,
        # but no committed recovery receipt may accept that partial cut.
        if hold.state != "consumed":
            raise AuditIntegrityError("END H recovery found a settled child with unconsumed hold")
        owed = read_v4_checkpoint_end_settled_owed_history_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            attempt_id=receipt.held_end_attempt_id,
            owed_checkpoint_id=receipt.checkpoint_id,
            names=names,
            child_disposition=effect.disposition,
            hold_state="consumed",
        )
    else:
        raise AuditIntegrityError("END H recovery changed its paid child disposition")
    if owed.selected.checkpoint_id != receipt.checkpoint_id or owed.selected.cursor != receipt.cursor:
        raise AuditIntegrityError("END H recovery substituted another complete paid response")
    if effect.disposition == "paid_overflow":
        require_original_end_outer_creator_after_paid_overflow_on_connection(conn, owed=owed, names=names)
    elif effect.disposition == "committed":
        require_original_end_outer_creator_after_complete_repair_on_connection(conn, owed=owed, names=names)
    else:
        require_original_end_outer_creator_on_connection(conn, owed=owed)
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == receipt.sequence,
            attempts.c.generation == receipt.previous_generation,
        )
    ).one_or_none()
    current = conn.execute(
        select(segments).where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == receipt.sequence,
        )
    ).one_or_none()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one_or_none()
    if (
        history is None
        or current is None
        or parent is None
        or receipt.sequence != owed.new_hold.creator_sequence
        or receipt.previous_generation < owed.new_hold.creator_generation
        or current.generation < receipt.generation
        or history.revoked_at is None
        or history.outcome != "interrupted"
        or history.recovered_end_h_checkpoint_id != receipt.checkpoint_id
    ):
        raise AuditIntegrityError("END H recovery lost its durable revoked edge")
    if (
        current.generation == receipt.generation
        and current.state == "interrupted"
        and (
            current.checkpoint_id != receipt.checkpoint_id
            or parent.checkpoint_id != receipt.checkpoint_id
            or parent.current_segment_sequence != receipt.sequence
            or parent.ownership_generation != receipt.generation
        )
    ):
        raise AuditIntegrityError("END H recovery changed its immediate interrupted H head")
    require_end_h_anchored_generation_chain_on_connection(conn, owed=owed, generation=receipt.generation)

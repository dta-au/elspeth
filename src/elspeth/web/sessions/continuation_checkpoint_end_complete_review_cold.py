"""Durable-only v2 Review result proof for a paid V4 END checkpoint."""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord, advisor_checkpoint_pass_audit_envelope
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, EndFrame, PrivateChunkRef
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    decode_frontier_chunk,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
    encode_frontier_chunk,
)
from elspeth.web.composer.continuation_end_values import decode_end_candidate, encode_end_candidate
from elspeth.web.composer.continuation_progress import ActionExecutionReceipt, record_action_receipt
from elspeth.web.coordination.quota_authority import _usage_entry_id, token_usage_entry_from_llm_call_envelope
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.continuation_checkpoint_end_owed import read_v4_checkpoint_end_settled_owed_history_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import require_checkpoint_paid_private_audit_on_connection
from elspeth.web.sessions.continuation_retained_capacity import (
    ProviderTerminalHoldSource,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.continuation_semantic_actions import installed_action_json, read_original_dispatched_semantic_review_on_connection
from elspeth.web.sessions.continuation_semantic_advisor import _historical_snapshot, _journal_data
from elspeth.web.sessions.continuation_semantic_review_result import (
    SemanticReviewCommitted,
    V4ReviewResultCommit,
    _commit_chunk_key,
    _review_result_outcome,
    decode_semantic_review_result,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc


def _event(conn: Connection, *, sid: str, operation: str, cursor: int) -> str:
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid,
            events.c.operation_id == operation,
            events.c.sequence == cursor,
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        raise AuditIntegrityError("paid END Review lost its exact checkpoint event")
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    if type(contract.allowance) is not int:
        raise AuditIntegrityError("paid END Review event lost its bounded policy allowance")
    bounded = read_bounded_retained_chunk(
        conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key, allowance=contract.allowance
    )
    if bounded is None or bounded.codec != "event.v2":
        raise AuditIntegrityError("paid END Review event changed its bounded payload")
    return bounded.payload.decode("utf-8", errors="strict")


def read_original_v4_end_complete_review_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    review_effect_key: str,
) -> SemanticReviewCommitted:
    """Replay dispatch, H, exact paid result, pass and completed action."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(review_effect_key) is not str:
        raise TypeError("paid END Review requires exact original identities")
    sid = str(session_id)
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == review_effect_key
        )
    ).one_or_none()
    if (
        effect is None
        or effect.owner != "composer_end"
        or effect.disposition != "committed"
        or effect.receipt_key != review_effect_key
        or type(effect.result_chunk_key) is not str
        or not effect.result_chunk_key.startswith("semantic-review-result:")
        or effect.provider_attempt_id is not None
    ):
        raise AuditIntegrityError("paid END Review lost its original action effect")
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation_id)
    if contract.version != "composer.continuation.policy.v4" or type(contract.allowance) is not int:
        raise AuditIntegrityError("paid END Review lost its admitted V4 policy")
    bounded = read_bounded_retained_chunk(
        conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key, allowance=contract.allowance
    )
    if bounded is None or bounded.codec != "composer.semantic-review-result.v2":
        raise AuditIntegrityError("paid END Review changed its bounded result chunk")
    canonical = bounded.payload.decode("utf-8", errors="strict")
    if _commit_chunk_key(canonical) != effect.result_chunk_key:
        raise AuditIntegrityError("paid END Review changed its result digest")
    try:
        commit = V4ReviewResultCommit.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("paid END Review lost its closed v2 receipt") from error
    if (
        canonical_json(commit.model_dump(mode="json")) != canonical
        or commit.session_id != session_id
        or commit.operation_id != operation_id
        or commit.owner_effect_key != review_effect_key
        or commit.owed_cursor <= commit.dispatch_cursor
    ):
        raise AuditIntegrityError("paid END Review changed its original dispatch/H identity")
    admission = read_original_dispatched_semantic_review_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        dispatch_checkpoint_id=commit.dispatch_checkpoint_id,
        semantic_effect_key=commit.semantic_effect_key,
    )
    candidate = admission.review_candidate
    if (
        candidate is None
        or admission.owner_effect_key != review_effect_key
        or admission.action_key != commit.action_key
        or candidate.response.digest != commit.candidate_digest
        or effect.action_json != installed_action_json(admission)
    ):
        raise AuditIntegrityError("paid END Review changed its original semantic action/candidate")
    require_no_credential_material(
        canonical,
        surface="composer_continuation_semantic_review_result",
        env_ref_names=admission.paid.invocation.source.env_ref_names,
        max_string_bytes=1_048_576,
    )
    dispatch = _historical_snapshot(conn, sid, operation_id, checkpoint_id=commit.dispatch_checkpoint_id)
    owed = _historical_snapshot(conn, sid, operation_id, checkpoint_id=commit.owed_checkpoint_id)
    completed = _historical_snapshot(conn, sid, operation_id, cursor=commit.owed_cursor + 1)
    if dispatch.cursor != commit.dispatch_cursor or owed.cursor != commit.owed_cursor:
        raise AuditIntegrityError("paid END Review changed its historical dispatch/H cut")
    historical_h = read_v4_checkpoint_end_settled_owed_history_on_connection(
        conn,
        session_id=sid,
        operation_id=operation_id,
        attempt_id=commit.provider_attempt_id,
        owed_checkpoint_id=commit.owed_checkpoint_id,
        names=admission.paid.invocation.source.env_ref_names,
        child_disposition="committed",
        hold_state="consumed",
    )
    if (
        historical_h.selected != owed
        or historical_h.phase_source != dispatch
        or historical_h.outer_effect_key != review_effect_key
        or historical_h.effect_key != commit.physical_effect_key
        or historical_h.attempt_id != commit.provider_attempt_id
    ):
        raise AuditIntegrityError("paid END Review lost its original semantic D and physical H chain")
    dispatch_root, owed_root = decode_continuation_snapshot(dispatch.payload_json), decode_continuation_snapshot(owed.payload_json)
    if owed_root.pending_settlement_ids != (commit.provider_attempt_id,) or owed_root.active_action_json != installed_action_json(
        admission
    ):
        raise AuditIntegrityError("paid END Review H changed its original dispatched root")
    owed_event = _event(conn, sid=sid, operation=operation_id, cursor=owed.cursor)
    owed_value, owed_error = parse_json_strict(owed_event)
    if (
        owed_error is not None
        or type(owed_value) is not dict
        or canonical_json(owed_value) != owed_event
        or owed_value.get("owner") != "composer_checkpoint_end_owed"
        or owed_value.get("version") != 1
        or owed_value.get("phase") != "end"
        or owed_value.get("attempt_id") != commit.provider_attempt_id
        or owed_value.get("effect_key") != commit.physical_effect_key
        or owed_value.get("outer_effect_key") != review_effect_key
        or owed_value.get("dispatched_checkpoint_id") != historical_h.original_selected.checkpoint_id
        or owed_value.get("dispatched_cursor") != historical_h.original_selected.cursor
        or owed_value.get("response_chunk_key") is None
    ):
        raise AuditIntegrityError("paid END Review lost its original raw H event")
    child = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == commit.physical_effect_key
        )
    ).one_or_none()
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == commit.provider_attempt_id)).one_or_none()
    hold = conn.execute(
        select(holds).where(
            holds.c.session_id == sid, holds.c.operation_id == operation_id, holds.c.effect_key == commit.physical_effect_key
        )
    ).one_or_none()
    if (
        child is None
        or child.owner != "provider"
        or child.disposition != "committed"
        or child.provider_attempt_id != commit.provider_attempt_id
        or type(child.receipt_key) is not str
        or child.result_chunk_key != owed_value["response_chunk_key"]
        or attempt is None
        or attempt.session_id != sid
        or attempt.settled_at is None
        or attempt.ledger_entry_id is None
        or hold is None
        or hold.provider_attempt_id != commit.provider_attempt_id
        or hold.state != "consumed"
    ):
        raise AuditIntegrityError("paid END Review lost its settled physical child/H")
    held = ProviderTerminalHoldSource(
        session_id=hold.session_id,
        operation_id=hold.operation_id,
        effect_key=hold.effect_key,
        policy_sha256=hold.policy_sha256,
        allowance_bytes=hold.allowance_bytes,
        source_checkpoint_id=hold.source_checkpoint_id,
        source_cursor=hold.source_cursor,
        source_snapshot_sha256=hold.source_snapshot_sha256,
        source_input_sha256=hold.source_input_sha256,
        creator_sequence=hold.creator_sequence,
        creator_generation=hold.creator_generation,
        claim_attempt=hold.claim_attempt,
        session_operation_id=hold.session_operation_id,
        session_operation_epoch=hold.session_operation_epoch,
        lease_sha256=hold.lease_sha256,
        reserved_bytes=hold.reserved_bytes,
        fallback_contract_sha256=hold.fallback_contract_sha256,
    )
    response = read_bounded_retained_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=owed_value["response_chunk_key"],
        allowance=contract.allowance,
    )
    if response is None or response.codec != "composer.response.v1":
        raise AuditIntegrityError("paid END Review lost its bounded original physical response")
    original_call = decode_retained_physical_audit_call(
        PrivateChunk(
            PrivateChunkRef(codec="composer.response.v1", digest=sha256(response.payload).hexdigest(), byte_length=len(response.payload)),
            response.payload.decode("utf-8", errors="strict"),
        )
    )
    usage = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(original_call))
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=commit.provider_attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    if (
        usage is None
        or entry is None
        or original_call.call_id != commit.checkpoint_call_id
        or entry.session_id != sid
        or entry.identity_id != attempt.identity_id
        or entry.source != "composer"
        or entry.run_id is not None
        or entry.model != usage.model
        or entry.prompt_tokens != usage.prompt_tokens
        or entry.completion_tokens != usage.completion_tokens
        or entry.cached_prompt_tokens != usage.cached_prompt_tokens
        or entry.reasoning_tokens != usage.reasoning_tokens
        or attempt.ledger_entry_id != entry_id
        or attempt.operation_id != hold.session_operation_id
        or attempt.operation_epoch != hold.session_operation_epoch
        or type(attempt.lease_token) is not str
        or sha256(attempt.lease_token.encode("utf-8")).hexdigest() != hold.lease_sha256
        or usage.recorded_at is None
        or restore_utc(entry.recorded_at) != restore_utc(usage.recorded_at)
    ):
        raise AuditIntegrityError("paid END Review lost its original quota ledger")
    require_checkpoint_paid_private_audit_on_connection(
        conn, source=held, attempt_id=commit.provider_attempt_id, private_sha256=commit.private_audit_sha256, incomplete=False
    )
    retained = decode_semantic_review_result(commit.result_json)
    if _review_result_outcome(retained, actual_disposition=commit.outcome) != commit.outcome:
        raise AuditIntegrityError("paid END Review changed its original complete result")
    result_call = tuple(call for call in retained.result.llm_calls if call.call_id == commit.checkpoint_call_id)
    if len(result_call) != 1 or result_call[0].call_id != commit.provider_attempt_id:
        raise AuditIntegrityError("paid END Review omitted its original complete checkpoint call")
    pass_row = conn.execute(
        select(messages).where(messages.c.session_id == sid, messages.c.id == commit.pass_audit_message_id)
    ).one_or_none()
    if (
        pass_row is None
        or pass_row.role != "audit"
        or pass_row.writer_principal != "compose_loop"
        or pass_row.raw_content is not None
        or type(pass_row.tool_calls) is not list
        or len(pass_row.tool_calls) != 1
    ):
        raise AuditIntegrityError("paid END Review lost its one original model pass row")
    try:
        pass_record = AdvisorCheckpointPassRecord.from_dict(pass_row.tool_calls[0]["pass"])
    except (KeyError, TypeError, ValueError) as error:
        raise AuditIntegrityError("paid END Review model pass is malformed") from error
    binding = retained.result.graph_review_identity
    if (
        binding is None
        or pass_record.phase != "end"
        or pass_record.source != "model"
        or pass_record.pass_index != commit.pass_index
        or (
            pass_record.publication_binding is not None
            if binding.source == "malformed"
            else pass_record.publication_binding != binding.binding
        )
        or pass_record.provider_attempts != historical_h.retry_ordinal + 1
        or pass_record.format_reprompt_sent != (historical_h.retry_ordinal == 1)
        or (
            historical_h.retry_ordinal == 1
            and (
                historical_h.retry_history is None
                or pass_record.first_attempt_schema_valid != historical_h.retry_history.marker.first_attempt_schema_valid
                or pass_record.first_attempt_accepted is not False
            )
        )
        or (pass_record.verdict, pass_record.publication_verdict)
        != (
            ("malformed", None)
            if binding.source == "malformed" and retained.result.advisor_terminal_publication is not None
            else ("clean", "ADMIT")
            if binding.source == "model" and retained.result.advisor_terminal_publication is None
            else ("flagged", "WITHHOLD")
        )
        or pass_row.tool_calls != [advisor_checkpoint_pass_audit_envelope(pass_record)]
        or pass_row.content != json.dumps(pass_row.tool_calls[0], sort_keys=True)
    ):
        raise AuditIntegrityError("paid END Review changed its bound original model pass")
    event = canonical_json(
        {
            "owner": "composer_semantic_review",
            "transition": "review_result_retained",
            "semantic_effect_key": commit.semantic_effect_key,
            "owner_effect_key": review_effect_key,
            "result_chunk_key": effect.result_chunk_key,
            "dispatch_checkpoint_id": commit.dispatch_checkpoint_id,
            "dispatch_cursor": commit.dispatch_cursor,
            "action_key": commit.action_key,
            "outcome": commit.outcome,
            "owed_checkpoint_id": commit.owed_checkpoint_id,
            "owed_cursor": commit.owed_cursor,
            "provider_attempt_id": commit.provider_attempt_id,
        }
    )
    _journal_data(conn, sid, operation_id, cursor=completed.cursor, tag="checkpoint", payload=event)
    dispatch_ref = dispatch_root.semantic_frontier
    if dispatch_ref is None:
        raise AuditIntegrityError("paid END Review lost its original semantic frontier")
    frontier_text = read_chunk(
        conn, session_id=sid, operation_id=operation_id, key=f"value:{dispatch_ref.codec}:{dispatch_ref.digest}"
    ).decode("utf-8", errors="strict")
    after_receipt = encode_frontier_chunk(
        record_action_receipt(
            decode_frontier_chunk(PrivateChunk(dispatch_ref, frontier_text)),
            ActionExecutionReceipt(commit.action_key, review_effect_key, "committed"),
            actual_owner_receipt_id=review_effect_key,
        )
    )
    if read_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=f"value:{after_receipt.reference.codec}:{after_receipt.reference.digest}",
    ) != after_receipt.canonical.encode("utf-8"):
        raise AuditIntegrityError("paid END Review changed its committed action receipt")
    frontier = tuple(
        EffectFrontierEntry(
            effect_id=item.effect_id,
            disposition="committed",
            receipt_id=child.receipt_key if item.effect_id == commit.physical_effect_key else review_effect_key,
        )
        if item.effect_id in (commit.physical_effect_key, review_effect_key)
        else item
        for item in owed_root.effect_frontier
    )
    advanced = owed_root.model_copy(update={"effect_frontier": frontier, "pending_settlement_ids": ()})
    end = advanced.frames[-1]
    completed_root = decode_continuation_snapshot(completed.payload_json)
    if type(end) is not EndFrame or len(completed_root.frames) != 2:
        raise AuditIntegrityError("paid END Review lost its original END result carrier")
    compose, completed_end = completed_root.frames
    if type(compose) is not ComposeFrame or type(completed_end) is not EndFrame:
        raise AuditIntegrityError("paid END Review changed its captured Compose/END pair")
    rebound = candidate.model_copy(update={"state": compose.state, "evidence": compose.evidence, "iteration": compose.iteration})
    rebound_chunk = encode_end_candidate(rebound)
    rebound_text = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=f"value:{rebound_chunk.reference.codec}:{rebound_chunk.reference.digest}",
    ).decode("utf-8", errors="strict")
    if decode_end_candidate(PrivateChunk(rebound_chunk.reference, rebound_text)) != rebound:
        raise AuditIntegrityError("paid END Review changed its rebound candidate")
    state_text = read_chunk(
        conn, session_id=sid, operation_id=operation_id, key=f"value:{compose.state.codec}:{compose.state.digest}"
    ).decode("utf-8", errors="strict")
    if state_text != canonical_json(retained.result.state.to_dict()):
        raise AuditIntegrityError("paid END Review changed its complete shared gate state")
    expected = advanced.model_copy(
        update={
            "cursor": completed.cursor,
            "semantic_frontier": after_receipt.reference,
            "active_action_json": None,
            "active_action_source": None,
            "frames": (
                compose,
                end.model_copy(
                    update={
                        "phase": "publication_ready" if commit.outcome == "clean" else "repair_needed",
                        "state": compose.state,
                        "candidate": rebound_chunk.reference,
                        "publication_binding": retained.publication_binding if commit.outcome == "clean" else None,
                    }
                ),
            ),
        }
    )
    if completed.payload_json != encode_continuation_snapshot(expected):
        raise AuditIntegrityError("paid END Review changed its complete committed successor root")
    original_advisor_call_id = admission.paid.audit_call.call_id
    if type(original_advisor_call_id) is not str or not original_advisor_call_id:
        raise AuditIntegrityError("paid END Review lost its original semantic Advisor call")
    return SemanticReviewCommitted(
        completed=completed,
        result=retained.result,
        publication_binding=retained.publication_binding if commit.outcome == "clean" else None,
        original_advisor_call_id=original_advisor_call_id,
        owner_effect_key=review_effect_key,
        action_key=commit.action_key,
        semantic_effect_key=commit.semantic_effect_key,
        outcome=commit.outcome,
        original_source=admission.paid.source,
        original_decision_json=admission.paid.decision_json,
    )

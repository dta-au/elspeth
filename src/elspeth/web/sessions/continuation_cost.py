"""Restore one Continuation cost owner from original fenced audit and ledger rows."""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Connection, Row

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import (
    ContinuationFrameStack,
    ContinuationProviderCostPolicyV2,
    PrivateChunkRef,
)
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.composer.provider_cost import (
    CommonProviderCostBudget,
    CommonProviderCostSnapshot,
    continuation_common_cost_policy,
)
from elspeth.web.composer.visible_tool_event import visible_response_gap_for_source
from elspeth.web.coordination.quota_authority import (
    _undispatched_cancellation_event_id,
    _usage_entry_id,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_segment_attempts_table as segments
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc

_CALL = TypeAdapter(ComposerLLMCall)


def _closed_call(row: Row[Any], source: ProviderAuditSource, attempt_id: str) -> ComposerLLMCall:
    calls = row.tool_calls
    if (
        row.role != "audit"
        or row.writer_principal != "compose_loop"
        or row.content != "Provider call result recorded."
        or row.raw_content is not None
        or row.tool_call_id is not None
        or row.parent_assistant_id is not None
        or row.composition_state_id is not None
        or type(calls) is not list
        or len(calls) != 2
        or type(calls[0]) is not dict
        or calls[0].get("_kind") != "llm_call_audit"
        or type(calls[0].get("call")) is not dict
        or calls[0]["call"].get("call_id") != attempt_id
    ):
        raise AuditIntegrityError("Continuation cost source lost its exact original audit row")
    try:
        call = _CALL.validate_json(canonical_json(calls[0]["call"]), strict=True, extra="forbid")
    except (ValidationError, ValueError, TypeError) as exc:
        raise AuditIntegrityError("Continuation cost audit violates its complete owned projection") from exc
    if (
        type(call) is not ComposerLLMCall
        or type(call.cost_response_accountable) is not bool
        or calls[0] != llm_call_audit_envelope(call)
        or calls[1] != visible_response_gap_for_source(call, source, calls[0])
    ):
        raise AuditIntegrityError("Continuation cost audit changed its original response-accountability or companion")
    return call


def _require_primary_effect_if_present(
    conn: Connection, stack: ContinuationFrameStack, attempt_id: str, public_call: ComposerLLMCall
) -> None:
    """A PRIMARY call with a retained effect must still match its complete source."""
    from elspeth.web.composer.continuation_codec import decode_retained_physical_audit_call
    from elspeth.web.sessions.continuation import read_chunk
    from elspeth.web.sessions.continuation_primary import _chunk_data, _physical_source_data

    logical = stack.logical
    found = conn.execute(
        select(effects).where(
            effects.c.session_id == logical.session_id,
            effects.c.operation_id == logical.operation_id,
            effects.c.owner == "provider",
            effects.c.provider_attempt_id == attempt_id,
        )
    ).all()
    if not found:
        return
    if len(found) != 1:
        raise AuditIntegrityError("Continuation cost source duplicated a PRIMARY physical effect")
    effect = found[0]
    parts = effect.effect_key.split(":") if type(effect.effect_key) is str else ()
    if (
        len(parts) != 3
        or parts[0] != "provider"
        or not parts[1].isascii()
        or not parts[1].isdigit()
        or str(int(parts[1])) != parts[1]
        or not parts[2].isascii()
        or not parts[2].isdigit()
        or str(int(parts[2])) != parts[2]
    ):
        raise AuditIntegrityError("Continuation cost source changed its canonical provider family/ordinal")
    if parts[1] != "0":
        # Family-1 Advisor and family-3 TITLE remain in the shared paid cost
        # ledger; their own response codecs are not PRIMARY response.v1.
        return
    key = effect.result_chunk_key
    prefix = "value:composer.response.v1:"
    if effect.disposition != "committed" or type(key) is not str or not key.startswith(prefix):
        raise AuditIntegrityError("Continuation cost source lost its committed PRIMARY response")
    raw = read_chunk(conn, session_id=logical.session_id, operation_id=logical.operation_id, key=key)
    digest = sha256(raw).hexdigest()
    if key != prefix + digest:
        raise AuditIntegrityError("Continuation cost source changed its PRIMARY response digest")
    ref = PrivateChunkRef(codec="composer.response.v1", digest=digest, byte_length=len(raw))
    response = _chunk_data(conn, UUID(logical.session_id), logical.operation_id, ref)
    complete_call = decode_retained_physical_audit_call(response)
    if (
        complete_call.call_id != attempt_id
        or llm_call_audit_envelope(complete_call) != llm_call_audit_envelope(public_call)
        or effect.receipt_key != attempt_id
        or effect.requested_model != complete_call.model_requested
        or effect.response_finished_at is None
        or restore_utc(effect.response_finished_at) != complete_call.finished_at
    ):
        raise AuditIntegrityError("Continuation cost source differs from its complete retained PRIMARY response")
    _physical_source_data(conn, UUID(logical.session_id), logical.operation_id, effect, complete_call)


def _require_ledger(conn: Connection, attempt: Row[Any], call: ComposerLLMCall) -> None:
    envelope = llm_call_audit_envelope(call)
    entry = token_usage_entry_from_llm_call_envelope(envelope)
    if entry is None:
        raise AuditIntegrityError("Continuation cost call lost its actual usage projection")
    if call.call_id is None:
        raise AuditIntegrityError("Continuation cost call lost its physical attempt identity")
    entry_id = _usage_entry_id(session_id=attempt.session_id, source="composer", run_id=None, call_id=call.call_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()
    if (
        ledger is None
        or attempt.ledger_entry_id != entry_id
        or attempt.settled_at is None
        or restore_utc(attempt.started_at) != call.started_at
        or (
            ledger.identity_id,
            ledger.session_id,
            ledger.source,
            ledger.run_id,
            ledger.model,
            ledger.prompt_tokens,
            ledger.completion_tokens,
            ledger.cached_prompt_tokens,
            ledger.reasoning_tokens,
            restore_utc(ledger.recorded_at),
        )
        != (
            attempt.identity_id,
            attempt.session_id,
            "composer",
            None,
            entry.model,
            entry.prompt_tokens,
            entry.completion_tokens,
            entry.cached_prompt_tokens,
            entry.reasoning_tokens,
            entry.recorded_at,
        )
    ):
        raise AuditIntegrityError("Continuation cost call lost its settled original ledger")


def _require_undispatched_cancellation(conn: Connection, attempt: Row[Any], source: ProviderAuditSource) -> bool:
    event = conn.execute(select(messages).where(messages.c.id == _undispatched_cancellation_event_id(attempt.attempt_id))).one_or_none()
    if event is None:
        return False
    ledger_id = _usage_entry_id(session_id=attempt.session_id, source="composer", run_id=None, call_id=attempt.attempt_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == ledger_id)).one_or_none()
    if ledger is None:
        raise AuditIntegrityError("Continuation no-SDK cancellation lost its usage row")
    value = {
        "kind": "provider_attempt.cancelled_before_dispatch",
        "attempt_id": attempt.attempt_id,
        "source": "composer",
        "run_id": None,
        "requested_model": ledger.model,
        "operation_id": source.operation_id,
        "operation_epoch": source.operation_epoch,
        "lease_token_sha256": sha256(attempt.lease_token.encode("utf-8")).hexdigest(),
    }
    if (
        attempt.ledger_entry_id != ledger_id
        or attempt.settled_at is None
        or event.session_id != attempt.session_id
        or event.role != "audit"
        or event.writer_principal != "compose_loop"
        or event.content != canonical_json(value)
        or event.tool_calls is not None
        or event.raw_content is not None
        or event.composition_state_id is not None
        or event.tool_call_id is not None
        or event.parent_assistant_id is not None
        or (ledger.identity_id, ledger.session_id, ledger.source, ledger.run_id)
        != (attempt.identity_id, attempt.session_id, "composer", None)
        or (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) != (0, 0, 0, 0)
        or restore_utc(event.created_at) != restore_utc(ledger.recorded_at)
        or restore_utc(event.created_at) != restore_utc(attempt.settled_at)
    ):
        raise AuditIntegrityError("Continuation no-SDK cancellation changed its original fenced proof")
    return True


def read_continuation_cost_snapshot_on_connection(conn: Connection, stack: ContinuationFrameStack) -> CommonProviderCostSnapshot | None:
    """Read all physical fences for one original logical operation, in audit order."""
    if type(stack) is not ContinuationFrameStack:
        raise AuditIntegrityError("Continuation cost source requires its actual selected root")
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    if not isinstance(policy, (ContinuationAdmissionPolicyV2, ContinuationAdmissionPolicyV3, ContinuationAdmissionPolicyV4)):
        if type(stack.provider_cost_policy) is not ContinuationProviderCostPolicyV2:
            return None
        raise AuditIntegrityError("legacy Continuation policy gained a current cost owner")
    if (
        type(stack.provider_cost_policy) is not ContinuationProviderCostPolicyV2
        or stack.provider_cost_policy.admission_policy_hash != policy.policy_hash
    ):
        raise AuditIntegrityError("Continuation cost source differs from its admitted immutable policy")
    logical = stack.logical
    principal = conn.execute(select(sessions.c.user_id).where(sessions.c.id == logical.session_id)).scalar_one()
    fences = conn.execute(
        select(segments).where(segments.c.session_id == logical.session_id, segments.c.operation_id == logical.operation_id)
    ).all()
    if not fences:
        raise AuditIntegrityError("Continuation cost source lost all original segment fences")
    attempts_by_id: dict[str, tuple[Row[Any], ProviderAuditSource]] = {}
    for fence in fences:
        source = ProviderAuditSource(logical.session_id, fence.session_operation_id, fence.session_operation_epoch)
        rows = conn.execute(
            select(physical).where(
                physical.c.session_id == logical.session_id,
                physical.c.operation_id == source.operation_id,
                physical.c.source == "composer",
            )
        ).all()
        for attempt in rows:
            if (
                attempt.identity_id != principal
                or attempt.run_id is not None
                or attempt.operation_epoch != source.operation_epoch
                or attempt.lease_token != fence.session_operation_lease_token
                or attempt.attempt_id in attempts_by_id
            ):
                raise AuditIntegrityError("Continuation cost source crossed its original physical fence or principal")
            attempts_by_id[attempt.attempt_id] = (attempt, source)
    audit_rows = conn.execute(
        select(messages).where(
            messages.c.session_id == logical.session_id,
            messages.c.role == "audit",
            messages.c.writer_principal == "compose_loop",
        )
    ).all()
    audited: dict[str, tuple[int, ComposerLLMCall]] = {}
    for row in audit_rows:
        calls = row.tool_calls
        if type(calls) is not list or not calls or type(calls[0]) is not dict or calls[0].get("_kind") != "llm_call_audit":
            continue
        value = calls[0].get("call")
        attempt_id = value.get("call_id") if type(value) is dict else None
        if attempt_id not in attempts_by_id:
            continue
        if attempt_id in audited:
            raise AuditIntegrityError("Continuation cost source duplicated one physical audit")
        attempt, source = attempts_by_id[attempt_id]
        call = _closed_call(row, source, attempt_id)
        _require_ledger(conn, attempt, call)
        _require_primary_effect_if_present(conn, stack, attempt_id, call)
        audited[attempt_id] = (row.sequence_no, call)
    for attempt_id, (attempt, source) in attempts_by_id.items():
        if attempt_id not in audited and not _require_undispatched_cancellation(conn, attempt, source):
            raise AuditIntegrityError("Continuation cost source has an unresolved physical attempt")
    budget = CommonProviderCostBudget(
        CommonProviderCostSnapshot(
            logical.session_id,
            logical.original_user_message_id,
            2,
            "continuation_v2",
            continuation_common_cost_policy(policy),
        )
    )
    for _, call in sorted(audited.values(), key=lambda item: item[0]):
        response_accountable = call.cost_response_accountable
        if response_accountable is None:
            raise AuditIntegrityError("Continuation cost call lost its response accountability")
        budget.dispatched()
        budget.consume_settled(call, response_accountable=response_accountable)
    return budget.snapshot()

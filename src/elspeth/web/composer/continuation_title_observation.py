"""Dormant TITLE result observation before required SQL settlement.

An overflow is a measured fact about a validated paid response. It is DATA,
not permission to finish an attempt or apply a title. No provider text enters
the overflow carrier. The V4 SQL owner must derive receipt identity anew.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_codec import encode_continuation_response, measure_continuation_response_size
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_provider_outcomes import (
    _physical_audit_json_value,
    measure_physical_audit_call_size,
    require_successful_response_audit,
)
from elspeth.web.composer.continuation_title_values import (
    CapturedTitleProviderResult,
    TitleProviderResultSnapshot,
    capture_title_provider_result,
    measure_title_provider_result_size,
)
from elspeth.web.composer.provider_audit_source import (
    ProviderAuditSource,
    audit_drafts_json,
    capture_provider_audit_source,
    provider_attempt_audit_drafts,
)
from elspeth.web.composer.provider_gateway import _require_no_credential_material_in_completion
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, encode_paid_overflow_cost_amount

_LIMIT = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
_MAX_LEDGER_TOKENS = 2**31 - 1


@dataclass(frozen=True, slots=True)
class TitleCompleteObservation:
    captured: CapturedTitleProviderResult


@dataclass(frozen=True, slots=True)
class TitleOverflowUsageObservation:
    """Ledger facts, with database completion time left to the SQL owner."""

    status: Literal["observed", "unavailable"]
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    reasoning_tokens: int | None


@dataclass(frozen=True, slots=True)
class TitleOverflowObservation:
    source: ProviderAuditSource
    attempt_id: str
    representation: Literal["physical_audit", "response"]
    document_codec: Literal["composer.physical-audit.v1", "composer.response.v1", "composer.title-provider-result.v1"]
    measurement: Literal["at_least"]
    observed_bytes: int
    limit_bytes: int
    cost: PaidOverflowCost
    usage: TitleOverflowUsageObservation


type TitlePaidObservation = TitleCompleteObservation | TitleOverflowObservation


def _cost_observation(call: ComposerLLMCall) -> PaidOverflowCost:
    if call.provider_cost is None:
        return PaidOverflowCost(status="unknown", amount=None, source="not_available")
    amount = encode_paid_overflow_cost_amount(call.provider_cost)
    return PaidOverflowCost(status="known", amount=amount, source=call.provider_cost_source)


def _usage_observation(call: ComposerLLMCall) -> TitleOverflowUsageObservation:
    model = call.model_returned if call.model_returned is not None else call.model_requested
    tokens = (call.prompt_tokens, call.completion_tokens, call.cached_prompt_tokens, call.reasoning_tokens)
    if (
        not model
        or len(model) > 256
        or any(value is not None and (type(value) is not int or value < 0 or value > _MAX_LEDGER_TOKENS) for value in tokens)
    ):
        return TitleOverflowUsageObservation("unavailable", None, None, None, None, None)
    return TitleOverflowUsageObservation("observed", model, *tokens)


def observe_title_provider_result(
    *,
    logical: LogicalRequestBinding,
    actor_user_id: str,
    effect_key: Literal["provider:3:0"],
    input_sha256: str,
    creator_sequence: int,
    creator_generation: int,
    completion: _AdmittedLLMCompletion,
    audit_call: ComposerLLMCall,
    context: SessionOperationContext,
    audit_drafts: tuple[AuditMessageDraft, ...],
    env_ref_names: frozenset[str],
    require_source_issued_v4: bool = False,
) -> TitlePaidObservation:
    """Classify only genuine, complete TITLE response/audit representations.

    The existing codec probes validate the full projection after excess. They
    saturate at limit + 1, so an excess is honestly recorded as at least that
    many bytes, never as a fabricated exact total.
    """
    if (
        type(logical) is not LogicalRequestBinding
        or type(completion) is not _AdmittedLLMCompletion
        or type(audit_call) is not ComposerLLMCall
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or audit_call.call_id is None
        or logical.session_id != context.fence.session_id
        or effect_key != "provider:3:0"
        or type(input_sha256) is not str
        or len(input_sha256) != 64
        or any(character not in "0123456789abcdef" for character in input_sha256)
        or type(creator_sequence) is not int
        or creator_sequence < 0
        or type(creator_generation) is not int
        or creator_generation < 0
        or not actor_user_id.strip()
    ):
        raise AuditIntegrityError("TITLE observation requires original complete nominal custody")
    if type(require_source_issued_v4) is not bool:
        raise AuditIntegrityError("TITLE source-issued capture requirement must be an exact boolean")
    if require_source_issued_v4:
        from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness

        witness = completion.provider_metadata.v4_completeness
        if type(witness) is not V4ProviderResultCompleteness:
            raise AuditIntegrityError("V4 TITLE observation lacks its source-issued complete return")
        witness.require_result(
            completion.provider_metadata,
            content=completion.message.content,
            tool_calls=completion.tool_batch.calls,
        )
    source = capture_provider_audit_source(context)
    # Complete non-state chunks scan their canonical document with this same
    # 1 MiB credential bound. Scan decoded fields here because the prospective
    # document may exceed 1 MiB while each original field is still valid.
    require_no_credential_material(
        {"logical": logical.model_dump(mode="json"), "actor_user_id": actor_user_id},
        surface="composer_title_overflow_identity",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    _require_no_credential_material_in_completion(completion, surface="composer_auto_title_response")
    require_successful_response_audit(
        audit_call,
        completion.provider_metadata,
        attempt_id=audit_call.call_id,
        started_at=audit_call.started_at,
        finished_at=audit_call.finished_at,
    )
    # The no-audit projection validates owned response shape and tool identity
    # even when the physical audit itself exceeds its private-document limit.
    measure_continuation_response_size(completion)
    require_no_credential_material(
        _physical_audit_json_value(audit_call),
        surface="composer_title_overflow_physical_audit",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    expected_drafts = provider_attempt_audit_drafts(audit_call, context)
    drafts_json = audit_drafts_json(audit_drafts)
    if drafts_json != audit_drafts_json(expected_drafts):
        raise AuditIntegrityError("TITLE observation changed its original audit draft cohort")
    drafts_value, drafts_error = parse_json_strict(drafts_json)
    if drafts_error is not None:
        raise AuditIntegrityError("TITLE observation lost its original audit draft JSON")
    require_no_credential_material(
        drafts_value,
        surface="composer_title_overflow_audit_drafts",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    attempt_id = audit_call.call_id
    if attempt_id is None:
        raise AuditIntegrityError("TITLE observation lost its original physical attempt")

    def excess(
        representation: Literal["physical_audit", "response"],
        document_codec: Literal["composer.physical-audit.v1", "composer.response.v1", "composer.title-provider-result.v1"],
    ) -> TitleOverflowObservation:
        return TitleOverflowObservation(
            source=source,
            attempt_id=attempt_id,
            representation=representation,
            document_codec=document_codec,
            measurement="at_least",
            observed_bytes=_LIMIT + 1,
            limit_bytes=_LIMIT,
            cost=_cost_observation(audit_call),
            usage=_usage_observation(audit_call),
        )

    if measure_physical_audit_call_size(audit_call).exceeded:
        return excess("physical_audit", "composer.physical-audit.v1")
    if measure_continuation_response_size(completion, audit_call=audit_call).exceeded:
        return excess("response", "composer.response.v1")
    response = encode_continuation_response(completion, audit_call=audit_call)
    snapshot = TitleProviderResultSnapshot(
        logical=logical,
        actor_user_id=actor_user_id,
        effect_key=effect_key,
        input_sha256=input_sha256,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        source=source,
        response_reference=response.reference,
        response_canonical=response.canonical,
        audit_drafts_json=drafts_json,
    )
    if measure_title_provider_result_size(snapshot).exceeded:
        return excess("response", "composer.title-provider-result.v1")
    captured = capture_title_provider_result(
        logical=logical,
        actor_user_id=actor_user_id,
        effect_key=effect_key,
        input_sha256=input_sha256,
        creator_sequence=creator_sequence,
        creator_generation=creator_generation,
        completion=completion,
        audit_call=audit_call,
        context=context,
        audit_drafts=audit_drafts,
        env_ref_names=env_ref_names,
    )
    return TitleCompleteObservation(captured)

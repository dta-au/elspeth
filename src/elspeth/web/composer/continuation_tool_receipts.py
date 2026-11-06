"""Complete completed-tool receipt DATA from actual recorded owners.

Capture never finishes an audit again or reconstructs an exception. Original
latency/UTC fields and private diagnostics are retained verbatim. Reading a
receipt does not authorize a tool, provider, writer or current evidence read.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, PrivateToolAuthoring
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.audit import _CANCELLATION_REASON_CODES, DispatchAudit, _normalize_audit_payload
from elspeth.web.composer.continuation_prepared_p4 import _p4_mapping_hash, _p4_safe_text
from elspeth.web.composer.continuation_result_credentials import require_retained_result_text
from elspeth.web.composer.continuation_tool_batch import (
    ToolResultValue,
    _decode_owned,
    _dump_owned,
    decode_tool_result_value,
    encode_tool_result_value,
)
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult


class RetainedToolReceipt(BaseModel):
    """Value receipt; result aliases belong to the separate coupled graph."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.tool-receipt.v1"] = "composer.tool-receipt.v1"
    effect_key: str
    tool_result: ToolResultValue | None
    invocation_json: str
    result_state_is_pre_state: bool | None


_VALUE = TypeAdapter(RetainedToolReceipt)
_INVOCATION = TypeAdapter(ComposerToolInvocation)


def _invocation(value: RetainedToolReceipt, env_ref_names: frozenset[str]) -> ComposerToolInvocation:
    invocation = _decode_owned(_INVOCATION, value.invocation_json)
    authoring = invocation.private_authoring
    if type(authoring) is not PrivateToolAuthoring:
        raise AuditIntegrityError("retained tool receipt lost its original private authoring")
    for text in (
        value.effect_key,
        invocation.tool_call_id,
        invocation.tool_name,
        invocation.actor,
        invocation.error_class,
        invocation.error_message,
    ):
        _p4_safe_text(text, env_ref_names=env_ref_names)
    if not value.effect_key or not invocation.tool_call_id or not invocation.tool_name:
        raise AuditIntegrityError("retained tool receipt lost its exact identity")
    for text in (
        authoring.raw_arguments,
        authoring.arguments_canonical,
        authoring.authority_arguments_canonical,
        invocation.arguments_canonical,
        invocation.authority_arguments_canonical,
    ):
        _p4_safe_text(text, env_ref_names=env_ref_names, tool_name=invocation.tool_name)
    require_retained_result_text(
        invocation.tool_name, invocation.result_canonical, env_ref_names=env_ref_names, outcome_status=invocation.status
    )
    if invocation.private_failure is not None:
        _p4_safe_text(invocation.private_failure.canonical, env_ref_names=env_ref_names)
    _p4_mapping_hash(invocation.arguments_canonical, invocation.arguments_hash)
    for encoded, digest in (
        (invocation.result_canonical, invocation.result_hash),
        (invocation.authority_arguments_canonical, invocation.authority_arguments_hash),
    ):
        if (encoded is None) != (digest is None):
            raise AuditIntegrityError("retained tool receipt lost a paired canonical hash")
        if encoded is not None and digest is not None:
            _p4_mapping_hash(encoded, digest)
    if (
        invocation.started_at.utcoffset() != timedelta(0)
        or invocation.finished_at.utcoffset() != timedelta(0)
        or type(invocation.latency_ms) is not int
        or invocation.latency_ms < 0
        or type(invocation.version_before) is not int
        or invocation.version_before < 1
    ):
        raise AuditIntegrityError("retained tool receipt lost its actual UTC/timing/version facts")
    # UTC wall time and monotonic elapsed have distinct meanings. Do not
    # recompute latency from these timestamps or invent a cross-process timer.
    if invocation.status is ComposerToolStatus.SUCCESS:
        if (
            value.tool_result is None
            or value.result_state_is_pre_state is None
            or invocation.error_class is not None
            or invocation.error_message is not None
            or invocation.private_failure is not None
        ):
            raise AuditIntegrityError("successful tool receipt lost its actual result")
        result = decode_tool_result_value(value.tool_result, env_ref_names=env_ref_names)
        if invocation.tool_name != value.tool_result.tool_name or invocation.version_after != result.updated_state.version:
            raise AuditIntegrityError("retained tool receipt changed its selected result identity/version")
        payload = {"success": result.success, "data": result.data, "cache_hit": True} if invocation.cache_hit else result.to_dict()
        if invocation.result_canonical != canonical_json(_normalize_audit_payload(payload)):
            raise AuditIntegrityError("retained tool receipt changed its actual completed result")
    elif (
        value.tool_result is not None
        or value.result_state_is_pre_state is not None
        or invocation.version_after is not None
        or invocation.error_class is None
        or invocation.error_message is None
    ):
        raise AuditIntegrityError("failed tool receipt invented a successful result")
    if invocation.status is not ComposerToolStatus.SUCCESS:
        if invocation.cache_hit:
            raise AuditIntegrityError("failed tool receipt invented a cache hit")
        if invocation.status is ComposerToolStatus.ARG_ERROR:
            if invocation.private_failure is not None:
                raise AuditIntegrityError("argument receipt invented plugin failure facts")
        else:
            if invocation.result_canonical is not None or invocation.result_hash is not None:
                raise AuditIntegrityError("cancelled/crashed receipt invented a completed payload")
            if invocation.status is ComposerToolStatus.CANCELLED:
                if (
                    invocation.private_failure is not None
                    or invocation.error_class != "CancelledError"
                    or invocation.error_message not in _CANCELLATION_REASON_CODES
                ):
                    raise AuditIntegrityError("cancelled receipt changed its actual closed failure shape")
            elif invocation.private_failure is None or invocation.error_message != invocation.error_class:
                raise AuditIntegrityError("plugin receipt lost its actual private failure shape")
    return invocation


def decode_retained_tool_receipt(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> RetainedToolReceipt:
    value = _decode_owned(_VALUE, encoded)
    _invocation(value, env_ref_names)
    return value


def encode_retained_tool_receipt(value: RetainedToolReceipt, *, env_ref_names: frozenset[str] = frozenset()) -> str:
    if type(value) is not RetainedToolReceipt:
        raise TypeError("tool receipt requires its exact data owner")
    encoded = _dump_owned(_VALUE, value)
    decode_retained_tool_receipt(encoded, env_ref_names=env_ref_names)
    return encoded


def capture_tool_receipt(
    *,
    effect_key: str,
    invocation: ComposerToolInvocation,
    result: ToolResult | AdmittedDiscoveryResult | None,
    pre_state: CompositionState,
    env_ref_names: frozenset[str] = frozenset(),
) -> RetainedToolReceipt:
    """Capture the recorded outcome, without running finish_success twice."""
    if type(invocation) is not ComposerToolInvocation or type(pre_state) is not CompositionState:
        raise TypeError("tool receipt capture requires actual completed invocation and pre-state")
    nominal = result.result if type(result) is AdmittedDiscoveryResult else result
    if nominal is not None and type(nominal) is not ToolResult:
        raise TypeError("tool receipt capture requires its actual nominal result")
    if invocation.version_before != pre_state.version:
        raise AuditIntegrityError("tool receipt changed its actual pre-version")
    if type(result) is AdmittedDiscoveryResult:
        if nominal is None:
            raise AuditIntegrityError("admitted discovery lost its original nominal result")
        # Complete admitted wire and original nominal result must still agree.
        # A stale cached contract/value is never granted current clearance.
        if result.response is not None:
            result.response.readmit(result.contract)
        if canonical_json(_normalize_audit_payload(result.to_dict())) != canonical_json(_normalize_audit_payload(nominal.to_dict())):
            raise AuditIntegrityError("admitted discovery result differs from its original nominal result")
    value = RetainedToolReceipt(
        effect_key=effect_key,
        tool_result=None if nominal is None else encode_tool_result_value(invocation.tool_name, nominal, env_ref_names=env_ref_names),
        invocation_json=_dump_owned(_INVOCATION, invocation),
        result_state_is_pre_state=None if nominal is None else nominal.updated_state is pre_state,
    )
    return decode_retained_tool_receipt(_dump_owned(_VALUE, value), env_ref_names=env_ref_names)


def require_receipt_result(
    value: RetainedToolReceipt,
    *,
    selected_call: _AdmittedToolCall,
    pre_state: CompositionState,
    result: ToolResult | AdmittedDiscoveryResult | None,
    env_ref_names: frozenset[str] = frozenset(),
) -> ComposerToolInvocation:
    """Join call/result values and the explicit pre-state alias relation.

    The recorded pre-state fact is a version plus alias relation, not a content
    hash. Full pre-state identity/content and current authorization belong to
    the separately admitted coupled graph/current-read owner.
    """
    admitted = decode_retained_tool_receipt(encode_retained_tool_receipt(value, env_ref_names=env_ref_names), env_ref_names=env_ref_names)
    invocation = _invocation(admitted, env_ref_names)
    if type(selected_call) is not _AdmittedToolCall or type(pre_state) is not CompositionState:
        raise TypeError("tool receipt join requires actual selected call and pre-state")
    if type(selected_call.function) is not _AdmittedToolFunction:
        raise TypeError("tool receipt join requires its actual selected function")
    authoring = invocation.private_authoring
    if (
        authoring is None
        or selected_call.id != invocation.tool_call_id
        or selected_call.function.name != invocation.tool_name
        or selected_call.function.arguments != authoring.raw_arguments
    ):
        raise AuditIntegrityError("tool receipt changed its exact admitted call/raw arguments")
    actual = capture_tool_receipt(
        effect_key=value.effect_key, invocation=invocation, result=result, pre_state=pre_state, env_ref_names=env_ref_names
    )
    if encode_retained_tool_receipt(actual, env_ref_names=env_ref_names) != encode_retained_tool_receipt(
        admitted, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("tool receipt changed its actual graph result or pre-state alias")
    return invocation


def invocation_from_receipt(
    receipt: RetainedToolReceipt, *, audit: DispatchAudit, env_ref_names: frozenset[str] = frozenset()
) -> ComposerToolInvocation:
    """Return original completed timing after matching the real admitted audit."""
    if type(audit) is not DispatchAudit:
        raise TypeError("receipt replay requires the separately readmitted actual dispatch audit")
    invocation = _invocation(
        decode_retained_tool_receipt(encode_retained_tool_receipt(receipt, env_ref_names=env_ref_names), env_ref_names=env_ref_names),
        env_ref_names,
    )
    if (
        invocation.tool_call_id != audit.tool_call_id
        or invocation.tool_name != audit.tool_name
        or invocation.arguments_canonical != audit.arguments_canonical
        or invocation.arguments_hash != audit.arguments_hash
        or invocation.authority_arguments_canonical != audit.authority_arguments_canonical
        or invocation.authority_arguments_hash != audit.authority_arguments_hash
        or invocation.started_at != audit.started_at
        or invocation.version_before != audit.version_before
        or invocation.actor != audit.actor
        or invocation.strict_sent is not audit.strict_sent
        or invocation.wire_conformant is not audit.wire_conformant
    ):
        raise AuditIntegrityError("retained invocation differs from its separately admitted dispatch")
    return invocation

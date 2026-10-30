"""Complete P4-pending DATA around the actual coupled batch/accumulator.

This is a selected writer input, not a writer grant. The shared graph codec
owns every batch/accumulator field and alias. P4 adds complete immutable input;
no callback, exception or committed witness is reconstructed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall
from elspeth.web.composer.audit import _normalize_audit_payload
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, decode_coupled_batch_data, encode_coupled_batch_data
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, owned_json, verify_private_chunk
from elspeth.web.composer.continuation_prepared_p4 import decode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_tool_batch import _decode_owned, _dump_owned
from elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState
from elspeth.web.composer.tools._common import ToolResult


class _P4PendingValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.p4-pending-batch.v1"] = "composer.p4-pending-batch.v1"
    coupled_json: str
    p4_input_json: str
    phase: Literal["p4_pending"] = "p4_pending"


_VALUE = TypeAdapter(_P4PendingValue)


def _joined(data: RestoredBatchData, *, env_ref_names: frozenset[str]) -> None:
    batch = data.batch
    from elspeth.web.composer.continuation_tool_failure import require_retained_failure_links

    require_retained_failure_links(data)
    if (
        batch.phase != "p4_pending"
        or batch.p4_input_json is None
        or batch.p4 is not None
        or batch.p4_receipt_json is not None
        or batch.p4_verification_failed
        or batch.current_call is not None
    ):
        raise AuditIntegrityError("P4 pending cut contains an unowned result or incomplete input")
    prepared = decode_prepared_p4(batch.p4_input_json, env_ref_names=env_ref_names)
    members = prepared_p4_members(prepared, env_ref_names=env_ref_names)
    failure = batch.retained_failure
    if failure is not None and failure.kind in ("plugin_crash", "advisor_failure"):
        last = batch.tool_outcomes[-1]
        matches = tuple(item for item in members.invocations if item.tool_call_id == last.call.id)
        if len(matches) != 1:
            raise AuditIntegrityError("P4 failure lost its actual prepared audit suffix")
        if failure.refusal_source is not None:
            if matches[0] != failure.refusal_source.invocation:
                raise AuditIntegrityError("P4 refusal changed its complete original prepared invocation")
        elif matches[0].private_failure is None or matches[0].error_class != failure.error_class:
            raise AuditIntegrityError("P4 crash lost its actual original private failure facts")
    # The actual driver treats plugin crashes and Advisor failures as pending
    # primary failures; ordinary argument/business refusals and the separate
    # Advisor timeout signal do not take that SQL-error disposition.
    retained = batch.retained_failure
    crash_pending = (
        batch.plugin_crash is not None
        or batch.advisor_failure is not None
        or (retained is not None and retained.kind in ("plugin_crash", "advisor_failure"))
    )
    if prepared.crash_pending is not crash_pending:
        raise AuditIntegrityError("P4 pending input changed its actual primary-failure disposition")
    prefix = tuple(data.calls[: batch.next_tool_index])
    if (
        tuple(row.tool_call_id for row in members.redacted_rows) != tuple(call.id for call in prefix)
        or tuple(item.tool_call_id for item in members.invocations) != tuple(call.id for call in prefix)
        or len(members.authorings) != len(prefix)
    ):
        raise AuditIntegrityError("P4 pending input changed its actual executed prefix")
    for call, authoring, invocation, outcome, row in zip(
        prefix, members.authorings, members.invocations, batch.tool_outcomes, members.redacted_rows, strict=True
    ):
        if (
            authoring != batch.private_authorings_by_call[call.id]
            or invocation.tool_name != call.function.name
            or authoring.tool_name != call.function.name
            or invocation.version_before != outcome.pre_version
            or invocation.strict_sent is not outcome.strict_sent
            or invocation.wire_conformant is not outcome.wire_conformant
        ):
            raise AuditIntegrityError("P4 pending input changed its actual call audit/authoring")
        state_payload = row.composition_state_payload
        if (state_payload is not None) != (outcome.post_version > outcome.pre_version):
            raise AuditIntegrityError("P4 pending payload differs from the actual state-changing outcome")
        if state_payload is not None:
            if type(outcome.response) is not ToolResult:
                raise AuditIntegrityError("P4 pending graph has no actual ToolResult state")
            actual_graph = outcome.response.updated_state.to_dict()
            del actual_graph["version"]
            retained_graph = {
                "sources": owned_json(state_payload.data.sources),
                "nodes": owned_json(state_payload.data.nodes),
                "edges": owned_json(state_payload.data.edges),
                "outputs": owned_json(state_payload.data.outputs),
                "metadata": owned_json(state_payload.data.metadata_),
            }
            if canonical_json(actual_graph) != canonical_json(retained_graph):
                raise AuditIntegrityError("P4 pending payload changed its actual dispatched graph")
        if outcome.error_class is None:
            if invocation.status is not ComposerToolStatus.SUCCESS or invocation.version_after != outcome.post_version:
                raise AuditIntegrityError("P4 pending success changed its actual dispatch status/version")
            response = outcome.response
            payload: Mapping[str, object]
            if type(response) is ToolResult:
                # Actual cache replay records this compact envelope, while a
                # fresh dispatch records the complete ToolResult projection.
                payload = (
                    {"success": response.success, "data": response.data, "cache_hit": True} if invocation.cache_hit else response.to_dict()
                )
            elif isinstance(response, Mapping):
                payload = response
            else:
                raise AuditIntegrityError("P4 pending success lost its actual result")
            if invocation.result_canonical != canonical_json(_normalize_audit_payload(payload)):
                raise AuditIntegrityError("P4 pending audit changed its actual selected result")
        elif (
            invocation.status not in (ComposerToolStatus.ARG_ERROR, ComposerToolStatus.PLUGIN_CRASH)
            or invocation.version_after is not None
            or invocation.error_class != outcome.error_class
            or invocation.error_category != outcome.error_category
            or invocation.error_message != outcome.error_message
        ):
            raise AuditIntegrityError("P4 pending failure changed its actual dispatch diagnosis")
    if members.authority_revocation != data.accumulator.authority_revocation:
        raise AuditIntegrityError("P4 pending input lost the actual accumulator revocation")
    if prepared.expected_current_state_id != batch.pre_state_id or prepared.parent_composition_state_id != batch.pre_state_id:
        raise AuditIntegrityError("P4 pending input changed its selected base state identity")


def encode_p4_pending_batch_data(
    *,
    batch: ContinuationBatchState,
    accumulator: BatchAccumulator,
    calls: tuple[_AdmittedToolCall, ...],
    env_ref_names: frozenset[str] = frozenset(),
) -> str:
    if type(batch) is not ContinuationBatchState or type(accumulator) is not BatchAccumulator:
        raise TypeError("P4 pending capture requires actual coupled owners")
    data = RestoredBatchData(batch, accumulator, calls)
    _joined(data, env_ref_names=env_ref_names)
    if batch.p4_input_json is None:
        raise AuditIntegrityError("P4 pending input is absent")
    # Only the outer phase/input fields differ. All retained member references
    # pass through the same graph; no per-state/result copies or equal-value
    # interning are introduced. The original live batch is never changed.
    quiescent = replace(batch, phase="dispatch_ready", p4_input_json=None)
    coupled = encode_coupled_batch_data(batch=quiescent, accumulator=accumulator, calls=calls, env_ref_names=env_ref_names)
    return _dump_owned(_VALUE, _P4PendingValue(coupled_json=coupled, p4_input_json=batch.p4_input_json))


def decode_p4_pending_batch_data(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> RestoredBatchData:
    value = _decode_owned(_VALUE, encoded)
    decoded = decode_coupled_batch_data(value.coupled_json, env_ref_names=env_ref_names)
    batch = replace(decoded.batch, phase="p4_pending", p4_input_json=value.p4_input_json)
    result = RestoredBatchData(batch, decoded.accumulator, decoded.calls)
    _joined(result, env_ref_names=env_ref_names)
    if _dump_owned(_VALUE, _P4PendingValue(coupled_json=value.coupled_json, p4_input_json=value.p4_input_json)) != encoded:
        raise AuditIntegrityError("P4 pending data changed its complete values or alias partition")
    return result


def encode_p4_pending_batch_chunk(
    *,
    batch: ContinuationBatchState,
    accumulator: BatchAccumulator,
    calls: tuple[_AdmittedToolCall, ...],
    env_ref_names: frozenset[str] = frozenset(),
) -> PrivateChunk:
    encoded = encode_p4_pending_batch_data(batch=batch, accumulator=accumulator, calls=calls, env_ref_names=env_ref_names)
    return encode_private_chunk(codec="composer.p4-pending-batch.v1", canonical=encoded, env_ref_names=env_ref_names)


def decode_p4_pending_batch_chunk(chunk: PrivateChunk, *, env_ref_names: frozenset[str] = frozenset()) -> RestoredBatchData:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.p4-pending-batch.v1":
        raise AuditIntegrityError("P4 pending chunk has another actual codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    return decode_p4_pending_batch_data(chunk.canonical, env_ref_names=env_ref_names)

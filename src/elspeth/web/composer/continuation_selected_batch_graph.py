"""Bind selected live-phase DATA to the single effects/recorder identity graph.

This is a DATA join inside the actual SQL reader, never a current authorization.
P4 selected phases retain their original separate receipt/SQL verifier.
"""

from __future__ import annotations

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_attachment_result_data import decode_attachment_history_batch
from elspeth.web.composer.continuation_batch_data import (
    RestoredPreparedBatchData,
    encode_coupled_batch_data,
    encode_restored_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import decode_continuation_response, verify_private_chunk
from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence, require_admitted_turn_evidence
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import _read_json_value


def require_selected_active_batch_graph(
    selected: SelectedContinuationState, evidence: AdmittedTurnEvidence, *, env_ref_names: frozenset[str]
) -> None:
    graph = require_admitted_turn_evidence(evidence, env_ref_names=env_ref_names).graph
    if graph is None or graph.selected.batch.phase in ("p4_pending", "complete"):
        return
    if type(selected) is not SelectedContinuationState:
        raise TypeError("selected graph requires its actual selected DATA owner")
    selected.__post_init__()
    frames = tuple(frame for frame in selected.stack.frames if type(frame) is ToolBatchFrame)
    if len(frames) != 1 or selected.stack.frames[-1] is not frames[0]:
        raise AuditIntegrityError("active evidence graph lost its selected issued batch")
    frame = frames[0]
    accumulators = tuple(chunk for chunk in selected.chunks if chunk.reference == frame.accumulator)
    responses = tuple(chunk for chunk in selected.chunks if chunk.reference == frame.response)
    if len(accumulators) != 1 or len(responses) != 1:
        raise AuditIntegrityError("active evidence graph lost its actual selected chunks")
    chunk = accumulators[0]
    verify_private_chunk(chunk.reference, chunk.canonical)
    payload = _read_json_value(chunk.canonical)
    if type(payload) is not dict:
        raise AuditIntegrityError("active evidence batch is not its closed object")
    selected_json = chunk.canonical
    if payload.get("schema_version") == "composer.attachment-history-batch.v1":
        if frame.phase != "result_retained":
            raise AuditIntegrityError("attachment history wrapper has another selected phase")
        selected_json = decode_attachment_history_batch(chunk, env_ref_names=env_ref_names).prepared_json
    if graph.selected_audit is not None and graph.selected_dialect is not None:
        if frame.accumulator.codec != "composer.tool-batch.v1":
            raise AuditIntegrityError("prepared evidence requires its actual selected tool-batch codec")
        expected = encode_restored_prepared_batch_data(
            RestoredPreparedBatchData(
                graph.selected.batch, graph.selected.accumulator, graph.selected.calls, graph.selected_audit, graph.selected_dialect
            ),
            env_ref_names=env_ref_names,
        )
    else:
        if frame.accumulator.codec != "composer.coupled-batch-data.v1":
            raise AuditIntegrityError("quiescent evidence requires its actual selected coupled codec")
        expected = encode_coupled_batch_data(
            batch=graph.selected.batch, accumulator=graph.selected.accumulator, calls=graph.selected.calls, env_ref_names=env_ref_names
        )
    if (
        selected_json != expected
        or frame.next_tool_index != graph.selected.batch.next_tool_index
        or decode_continuation_response(responses[0]).tool_batch.calls != graph.selected.calls
    ):
        raise AuditIntegrityError("selected batch differs from the complete shared evidence graph")

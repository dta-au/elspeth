"""Cold V4 complete value-batch proof without tool or provider execution."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
from elspeth.web.composer.continuation_phase_transitions import validate_tool_effects_data_on_connection
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result
from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_data_on_connection
from elspeth.web.sessions.continuation_primary import read_committed_primary_response_data_on_connection
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.models import composer_continuation_effects_table as effects


@dataclass(frozen=True, slots=True)
class VerifiedValueBatchApplication:
    snapshot: StoredContinuationSnapshot
    response: PrivateChunk
    tool_effect_keys: tuple[str, ...]
    applications: tuple[ReadmittedP4Result, ...]


def read_value_batch_application_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    expected: StoredContinuationSnapshot,
    env_ref_names: frozenset[str],
) -> VerifiedValueBatchApplication:
    """Reprove exact head, paid call order, results and public P4 journal.

    Caller holds the session lock in a separate post-commit read transaction.
    This reader grants no new dispatch permission or required-SQL ticket.
    """
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    current = require_snapshot_cas(
        conn, session_id=sid, operation_id=operation, expected_checkpoint_id=expected.checkpoint_id, expected_cursor=expected.cursor
    )
    if current != expected:
        raise AuditIntegrityError("value-batch cold read changed its actual committed snapshot")
    root = decode_continuation_snapshot(current.payload_json)
    frame = root.frames[-1]
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or type(frame) is not ToolBatchFrame
        or frame.phase != "result_retained"
        or root.pending_owner_results
    ):
        raise AuditIntegrityError("value-batch cold read requires its complete applied V4 frame")
    chunks = load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation, selected=current)
    selected = SelectedContinuationState(current.checkpoint_id, root, chunks)
    response = read_committed_primary_response_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, selected=selected, reference=frame.response
    )
    calls = decode_continuation_response(response).tool_batch.calls
    evidence = read_selected_turn_evidence_graph_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, selected=selected, names=env_ref_names
    )
    graph = evidence.graph
    if (
        graph is None
        or graph.selected.batch.phase != "complete"
        or graph.selected.calls != calls
        or len(graph.selected.batch.tool_outcomes) != len(calls)
        or frame.next_tool_index != len(calls)
    ):
        raise AuditIntegrityError("value-batch cold read omitted an original ordered call/result")
    if validate_tool_effects_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, selected=selected, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("value-batch cold read contains unresolved original tools")
    keys = tuple(f"tool:{frame.response.digest}:{index}" for index in range(len(calls)))
    if tuple(key for key in frame.settled_effect_ids if key.startswith("tool:")) != keys:
        raise AuditIntegrityError("value-batch cold read changed original settlement order")
    for index, key in enumerate(keys):
        row = conn.execute(
            select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == key)
        ).one_or_none()
        if row is None or row.owner != "tool_dispatch" or row.disposition not in ("committed", "refused") or row.result_chunk_key is None:
            raise AuditIntegrityError("value-batch cold read lost an actual completed original effect")
        encoded = read_chunk(conn, session_id=sid, operation_id=operation, key=row.result_chunk_key).decode("utf-8")
        chunk = encode_private_chunk(codec="composer.coupled-batch-data.v1", canonical=encoded, env_ref_names=env_ref_names)
        result = decode_coupled_batch_chunk(chunk, env_ref_names=env_ref_names)
        if result.calls != calls or result.batch.tool_outcomes != graph.selected.batch.tool_outcomes[: index + 1]:
            raise AuditIntegrityError("value-batch cold read substituted a completed result prefix")
    applications = require_p4_application_journal_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, selected=selected, evidence=evidence, env_ref_names=env_ref_names
    )
    if not applications:
        raise AuditIntegrityError("value-batch cold read lost its actual public application receipt")
    return VerifiedValueBatchApplication(current, response, keys, applications)

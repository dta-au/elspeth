"""Current-fence transactions for the real live batch's retained phase cuts.

These transactions never execute a tool or derive a pipeline. They retain the
actual existing phase owners' DATA and dispatch/result ordering under the same
session lock used by Stop, after rechecking the complete selected predecessor.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from sqlalchemy import Connection, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, ContinuationValue, EffectFrontierEntry, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    RestoredPreparedBatchData,
    decode_coupled_batch_chunk,
    decode_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_primary import (
    read_committed_primary_response_for_settlement_on_connection,
    read_committed_primary_response_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.time_normalization import restore_utc

type BatchTransition = Literal["batch_initialized", "tool_issued", "tool_completed", "tool_preparation_completed"]


class ToolIssueSource(ContinuationValue):
    schema_version: Literal["composer.tool-issue.v1"] = "composer.tool-issue.v1"
    previous_checkpoint_id: str
    cursor: int = Field(ge=1)
    sequence: int = Field(ge=1)
    generation: int = Field(ge=1)
    creator_operation_id: str
    creator_operation_epoch: int = Field(ge=1)
    response: PrivateChunkRef
    prepared: PrivateChunkRef
    tool_index: int = Field(ge=0)
    tool_call_id: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def read_tool_issue_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, effect_key: str, env_ref_names: frozenset[str]
) -> ToolIssueSource:
    """Read an actual creator's immutable dispatch source under current custody.

    The effect, immutable issue event/checkpoint and full original prepared
    call must agree. A caller's live frontier or a later marker is insufficient.
    """
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == effect_key)
    ).one_or_none()
    if effect is None or effect.owner != "tool_dispatch" or effect.action_json is None:
        raise AuditIntegrityError("owed retention has no exact current issued tool")
    try:
        issue = ToolIssueSource.model_validate_json(effect.action_json, strict=True)
    except ValueError as error:
        raise AuditIntegrityError("tool issue lost its complete source descriptor") from error
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one_or_none()
    if (
        canonical_json(issue.model_dump(mode="json")) != effect.action_json
        or (issue.sequence, issue.generation) != (effect.creator_sequence, effect.creator_generation)
        or creator is None
        or (issue.creator_operation_id, issue.creator_operation_epoch) != (creator.session_operation_id, creator.session_operation_epoch)
        or issue.input_sha256 != effect.input_sha256
        or effect_key != f"tool:{issue.response.digest}:{issue.tool_index}"
        or issue.prepared.codec != "composer.tool-batch.v1"
    ):
        raise AuditIntegrityError("tool issue changed its exact physical creator/input")
    event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == issue.cursor)
    ).one_or_none()
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor == issue.cursor
        )
    ).one_or_none()
    previous = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == issue.previous_checkpoint_id,
        )
    ).one_or_none()
    expected = canonical_json({"owner": "composer_tool_phase", "transition": "tool_issued", "issue": issue.model_dump(mode="json")})
    if (
        event is None
        or event.tag != "may_dispatch"
        or checkpoint is None
        or checkpoint.kind != "transition"
        or previous is None
        or previous.cursor + 1 != issue.cursor
        or read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key) != expected.encode("utf-8")
    ):
        raise AuditIntegrityError("tool issue lost its actual immutable event/checkpoint")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    if parent.cancel_requested_at is not None and restore_utc(event.created_at) > restore_utc(parent.cancel_requested_at):
        raise AuditIntegrityError("tool issue did not precede the durable Stop")
    root = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=operation, key=checkpoint.snapshot_chunk_key).decode("utf-8", errors="strict")
    )
    frame = root.frames[-1]
    previous_root = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=operation, key=previous.snapshot_chunk_key).decode("utf-8", errors="strict")
    )
    previous_frame = previous_root.frames[-1]
    current_root = decode_continuation_snapshot(load_snapshot(conn, session_id=sid, operation_id=operation).payload_json)
    if (
        (root.logical.session_id, root.logical.operation_id) != (sid, operation)
        or type(frame) is not ToolBatchFrame
        or frame.phase != "effect_reconciling"
        or frame.response != issue.response
        or frame.accumulator != issue.prepared
        or frame.next_tool_index != issue.tool_index
        or type(previous_frame) is not ToolBatchFrame
        or previous_frame.phase != "tool_ready"
        or previous_frame.response != issue.response
        or previous_frame.next_tool_index != issue.tool_index
        or previous_root.logical != root.logical
        or root.logical != current_root.logical
        or root.cursor != issue.cursor
        or previous_root.cursor != previous.cursor
        or tuple(entry for entry in root.effect_frontier if entry.effect_id != effect_key) != previous_root.effect_frontier
        or tuple(entry for entry in root.effect_frontier if entry.effect_id == effect_key)
        != (EffectFrontierEntry(effect_id=effect_key, disposition="dispatched", receipt_id=None),)
    ):
        raise AuditIntegrityError("tool issue changed its actual prepared checkpoint")
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
    from elspeth.web.sessions.continuation_selection import load_private_chunks

    # The immutable dispatch source includes the entire admitted graph, rather
    # than only a plausible prepared-call fragment. It remains DATA under this
    # caller's separately checked current physical custody.
    issued = SelectedContinuationState(
        checkpoint.checkpoint_id, root, load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root)
    )
    read_selected_turn_evidence_graph_on_connection(conn, running, selected=issued, names=env_ref_names)
    encoded = read_chunk(conn, session_id=sid, operation_id=operation, key=f"value:{issue.prepared.codec}:{issue.prepared.digest}").decode(
        "utf-8", errors="strict"
    )
    data = decode_prepared_batch_data(encoded, env_ref_names=env_ref_names)
    call = data.batch.current_call
    if (
        call is None
        or call.index != issue.tool_index
        or call.tool_call.id != issue.tool_call_id
        or call.effect_key is not None
        or stable_hash({"tool_call_id": call.tool_call.id, "name": call.tool_name, "arguments": call.private_arguments_canonical})
        != issue.input_sha256
        or (call.original_creator_id, call.original_creator_epoch) != (issue.creator_operation_id, issue.creator_operation_epoch)
    ):
        raise AuditIntegrityError("tool issue changed its source-authored call/argument identity")
    return issue


def validate_tool_effects_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    env_ref_names: frozenset[str],
) -> tuple[str, ...]:
    """Prove each actual tool issue/result before classifying an open effect."""
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    rows = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.owner == "tool_dispatch")
    ).all()
    unresolved: list[str] = []
    for effect in rows:
        issue = read_tool_issue_source_on_connection(conn, running, effect_key=effect.effect_key, env_ref_names=env_ref_names)
        read_committed_primary_response_for_settlement_on_connection(conn, running, selected=selected, reference=issue.response)
        if effect.disposition in ("dispatched", "unknown"):
            if effect.receipt_key is not None or effect.result_chunk_key is not None:
                raise AuditIntegrityError("unresolved tool effect carries a counterfeit committed result")
            unresolved.append(effect.effect_key)
            continue
        if effect.disposition != "committed" or type(effect.result_chunk_key) is not str:
            raise AuditIntegrityError("tool effect lost its actual dispatched or completed disposition")
        parts = effect.result_chunk_key.split(":")
        if len(parts) != 3 or parts[:2] != ["value", "composer.coupled-batch-data.v1"]:
            raise AuditIntegrityError("tool result changed its complete coupled source codec")
        encoded = read_chunk(conn, session_id=sid, operation_id=operation, key=effect.result_chunk_key).decode("utf-8", errors="strict")
        from elspeth.web.composer.continuation_codec import encode_private_chunk

        chunk = encode_private_chunk(codec="composer.coupled-batch-data.v1", canonical=encoded, env_ref_names=env_ref_names)
        if effect.result_chunk_key != private_chunk_key(chunk) or effect.receipt_key != f"tool-result:{chunk.reference.digest}":
            raise AuditIntegrityError("tool result changed its actual immutable receipt/chunk binding")
        completed = decode_coupled_batch_chunk(chunk, env_ref_names=env_ref_names)
        prepared_raw = read_chunk(
            conn, session_id=sid, operation_id=operation, key=f"value:{issue.prepared.codec}:{issue.prepared.digest}"
        ).decode("utf-8", errors="strict")
        prepared = decode_prepared_batch_data(prepared_raw, env_ref_names=env_ref_names)
        if (
            completed.calls != prepared.calls
            or completed.batch.next_tool_index != issue.tool_index + 1
            or completed.batch.tool_outcomes[:-1] != prepared.batch.tool_outcomes
            or completed.batch.tool_outcomes[-1].call != prepared.calls[issue.tool_index]
            or completed.batch.current_call is not None
        ):
            raise AuditIntegrityError("tool result changed its exact issued call/completed prefix")
    return tuple(unresolved)


def require_tool_issue_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, effect_key: str, env_ref_names: frozenset[str]
) -> ToolIssueSource:
    """A historical issue is DATA; owed completion still needs this exact creator."""
    issue = read_tool_issue_source_on_connection(conn, running, effect_key=effect_key, env_ref_names=env_ref_names)
    fence = running.session_operation_context.fence
    if (issue.sequence, issue.generation) != (running.claim.segment_sequence, running.claim.ownership_generation) or (
        issue.creator_operation_id,
        issue.creator_operation_epoch,
    ) != (fence.operation_id, fence.operation_epoch):
        raise AuditIntegrityError("owed tool completion substituted the original issuing attempt")
    return issue


def require_same_sequence_issue_history_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, issue: ToolIssueSource
) -> None:
    """Read an original issue through actual immutable same-sequence handovers."""
    sequence, generation = running.claim.segment_sequence, running.claim.ownership_generation
    if sequence is None or generation is None or issue.sequence != sequence or issue.generation > generation:
        raise AuditIntegrityError("paid prefix crossed its actual continuation sequence or generation")
    if issue.generation == generation:
        fence = running.session_operation_context.fence
        if (issue.creator_operation_id, issue.creator_operation_epoch) != (fence.operation_id, fence.operation_epoch):
            raise AuditIntegrityError("paid prefix changed its current issuing fence")
        return
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    history = conn.execute(
        select(attempts)
        .where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == sequence,
            attempts.c.generation >= issue.generation,
            attempts.c.generation < generation,
        )
        .order_by(attempts.c.generation)
    ).all()
    if len(history) != generation - issue.generation or any(
        row.generation != issue.generation + index for index, row in enumerate(history)
    ):
        raise AuditIntegrityError("paid prefix lost an actual predecessor attempt")
    expected = {
        canonical_json({"version": 2, "same_sequence": sequence, "previous_generation": prior, "generation": prior + 1}): prior
        for prior in (row.generation for row in history)
    }
    recovered: dict[int, int] = {}
    rows = conn.execute(
        select(events)
        .where(
            events.c.session_id == sid,
            events.c.operation_id == operation,
            events.c.tag == "recovered",
            events.c.sequence > issue.cursor,
            events.c.sequence <= selected.stack.cursor,
        )
        .order_by(events.c.sequence)
    ).all()
    for event in rows:
        payload = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode("utf-8", errors="strict")
        prior = expected.get(payload)
        if prior is None or prior in recovered:
            raise AuditIntegrityError("paid prefix has an unrelated or duplicated recovery transition")
        checkpoint = conn.execute(
            select(checkpoints).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation,
                checkpoints.c.cursor == event.sequence,
            )
        ).one_or_none()
        previous = conn.execute(
            select(checkpoints).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation,
                checkpoints.c.cursor == event.sequence - 1,
            )
        ).one_or_none()
        if checkpoint is None or previous is None or checkpoint.kind != "transition":
            raise AuditIntegrityError("paid prefix lost its actual recovery checkpoint/predecessor")
        root = decode_continuation_snapshot(
            read_chunk(conn, session_id=sid, operation_id=operation, key=checkpoint.snapshot_chunk_key).decode("utf-8", errors="strict")
        )
        predecessor = decode_continuation_snapshot(
            read_chunk(conn, session_id=sid, operation_id=operation, key=previous.snapshot_chunk_key).decode("utf-8", errors="strict")
        )
        if (
            root.cursor != event.sequence
            or predecessor.cursor != event.sequence - 1
            or root.logical != selected.stack.logical
            or root.model_copy(update={"cursor": predecessor.cursor}) != predecessor
        ):
            raise AuditIntegrityError("paid prefix recovery replaced its actual retained source")
        recovered[prior] = event.sequence
    if tuple(sorted(recovered)) != tuple(row.generation for row in history):
        raise AuditIntegrityError("paid prefix lost its committed same-sequence recovery events")
    previous_epoch = issue.creator_operation_epoch - 1
    previous_cursor = issue.cursor
    for row in history:
        if (
            row.outcome != "interrupted"
            or row.revoked_at is None
            or row.session_operation_id is None
            or row.session_operation_lease_token is None
            or row.session_operation_epoch is None
            or row.session_operation_epoch <= previous_epoch
            or recovered[row.generation] <= previous_cursor
        ):
            raise AuditIntegrityError("paid prefix predecessor lacks its actual revoked physical custody")
        previous_epoch, previous_cursor = row.session_operation_epoch, recovered[row.generation]
    if running.session_operation_context.fence.operation_epoch <= previous_epoch:
        raise AuditIntegrityError("paid prefix is not held by its actual later physical fence")


def require_completed_tool_prefix_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, env_ref_names: frozenset[str]
) -> tuple[str, ...]:
    """Prove the real same-creator issued prefix owed P4 after Stop.

    Every completed tool must have its actual immutable issue before Stop and
    exact committed result. A caller's frontier, pending flag or equal live
    batch alone cannot authorize this retained cleanup facet.
    """
    if type(selected) is not SelectedContinuationState or type(env_ref_names) is not frozenset:
        raise AuditIntegrityError("owed batch requires its complete actual selected DATA")
    selected.__post_init__()
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    root = selected.stack
    stored = load_snapshot(conn, session_id=root.logical.session_id, operation_id=root.logical.operation_id)
    if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(root):
        raise AuditIntegrityError("owed batch replaced its actual current selected root")
    frame = root.frames[-1]
    if type(frame) is not ToolBatchFrame or frame.phase != "tool_ready" or frame.accumulator.codec != "composer.coupled-batch-data.v1":
        raise AuditIntegrityError("owed batch lost its actual retained completed prefix")
    data = decode_coupled_batch_chunk(_selected_chunk(selected, frame), env_ref_names=env_ref_names)
    if data.batch.phase not in ("call_ready", "dispatch_ready") or data.batch.current_call is not None or not data.batch.tool_outcomes:
        raise AuditIntegrityError("owed P4 requires a real completed issued-tool prefix")
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection

    read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=env_ref_names)
    unresolved = validate_tool_effects_on_connection(conn, running, selected=selected, env_ref_names=env_ref_names)
    keys: list[str] = []
    for index, outcome in enumerate(data.batch.tool_outcomes):
        key = f"tool:{frame.response.digest}:{index}"
        issue = read_tool_issue_source_on_connection(conn, running, effect_key=key, env_ref_names=env_ref_names)
        require_same_sequence_issue_history_on_connection(conn, running, selected=selected, issue=issue)
        effect = conn.execute(
            select(effects).where(
                effects.c.session_id == root.logical.session_id,
                effects.c.operation_id == root.logical.operation_id,
                effects.c.effect_key == key,
            )
        ).one()
        if (
            issue.tool_call_id != outcome.call.id
            or issue.tool_index != index
            or issue.response != frame.response
            or effect.disposition != "committed"
            or key not in frame.settled_effect_ids
        ):
            raise AuditIntegrityError("owed P4 substituted an unissued or unfinished tool")
        keys.append(key)
    for key in unresolved:
        issue = require_tool_issue_on_connection(conn, running, effect_key=key, env_ref_names=env_ref_names)
        if not data.batch.stop_issued_batch or issue.tool_index < len(keys) or issue.response != frame.response:
            raise AuditIntegrityError("owed P4 mixed an unresolved tool into its completed prefix")
    return tuple(keys)


def require_completed_runtime_prefix_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, env_ref_names: frozenset[str]
) -> tuple[str, ...]:
    """Prove every ordinary completed outcome through its actual closed owner.

    An attachment application is independently read-owned; it cannot stand in
    for a native dispatch issue or authorize Stop cleanup. No result of this
    DATA verification supplies an issue/dispatch permission.
    """
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
    from elspeth.web.sessions.continuation_attachment_application import (
        completed_attachment_prefix_correspondence,
        require_attachment_applications_on_connection,
    )

    if type(selected) is not SelectedContinuationState or type(env_ref_names) is not frozenset:
        raise AuditIntegrityError("runtime prefix requires its complete selected source")
    selected.__post_init__()
    _require_current_continuation_running(conn, running)
    root = selected.stack
    stored = load_snapshot(conn, session_id=root.logical.session_id, operation_id=root.logical.operation_id)
    if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(root):
        raise AuditIntegrityError("runtime prefix changed its actual current selected source")
    frame = root.frames[-1]
    if type(frame) is not ToolBatchFrame or frame.phase != "tool_ready" or frame.accumulator.codec != "composer.coupled-batch-data.v1":
        raise AuditIntegrityError("runtime prefix lost its actual retained source")
    data = decode_coupled_batch_chunk(_selected_chunk(selected, frame), env_ref_names=env_ref_names)
    if (
        data.batch.phase not in ("call_ready", "dispatch_ready")
        or data.batch.current_call is not None
        or not data.batch.tool_outcomes
        or len(data.batch.tool_outcomes) != frame.next_tool_index
    ):
        raise AuditIntegrityError("runtime prefix lost its actual completed source positions")
    evidence = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=env_ref_names)
    # Full source proof, not presence of an entry or cached graph, precedes the
    # per-index correspondence. Every repeated read remains on this same owner,
    # connection and original deadline; no budget is renewed here.
    history = require_attachment_applications_on_connection(
        conn, running, selected=selected, evidence=evidence, env_ref_names=env_ref_names
    )
    attachment = completed_attachment_prefix_correspondence(selected=selected, data=data, sources=history, names=env_ref_names)
    keys: list[str] = []
    for index, outcome in enumerate(data.batch.tool_outcomes):
        native_key, read_key = f"tool:{frame.response.digest}:{index}", f"attachment:{frame.response.digest}:{index}"
        rows = conn.execute(
            select(effects).where(
                effects.c.session_id == root.logical.session_id,
                effects.c.operation_id == root.logical.operation_id,
                effects.c.effect_key.in_((native_key, read_key)),
            )
        ).all()
        if len(rows) != 1:
            raise AuditIntegrityError("runtime prefix has missing or dual completed source ownership")
        effect = rows[0]
        if index in attachment:
            if effect.effect_key != attachment[index] or effect.owner != "read" or effect.disposition != "committed":
                raise AuditIntegrityError("runtime attachment prefix lost its original read source")
            keys.append(attachment[index])
            continue
        if effect.effect_key != native_key or effect.owner != "tool_dispatch":
            raise AuditIntegrityError("runtime native prefix lost its actual issued source")
        issue = read_tool_issue_source_on_connection(conn, running, effect_key=native_key, env_ref_names=env_ref_names)
        require_same_sequence_issue_history_on_connection(conn, running, selected=selected, issue=issue)
        if (
            issue.tool_call_id != outcome.call.id
            or issue.tool_index != index
            or issue.response != frame.response
            or effect.disposition != "committed"
            or native_key not in frame.settled_effect_ids
        ):
            raise AuditIntegrityError("runtime native prefix substituted an unissued or unfinished source")
        keys.append(native_key)
    unresolved = validate_tool_effects_on_connection(conn, running, selected=selected, env_ref_names=env_ref_names)
    for key in unresolved:
        issue = require_tool_issue_on_connection(conn, running, effect_key=key, env_ref_names=env_ref_names)
        if not data.batch.stop_issued_batch or issue.tool_index < len(keys) or issue.response != frame.response:
            raise AuditIntegrityError("runtime prefix mixed an unresolved tool into its completed source")
    return tuple(keys)


def require_issued_tool_runtime_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, env_ref_names: frozenset[str]
) -> str:
    """Prove the actual current issued call before retaining its paid outcome."""
    if type(selected) is not SelectedContinuationState or type(env_ref_names) is not frozenset:
        raise AuditIntegrityError("issued runtime requires its complete current selected DATA")
    selected.__post_init__()
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    stored = load_snapshot(conn, session_id=selected.stack.logical.session_id, operation_id=selected.stack.logical.operation_id)
    if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(selected.stack):
        raise AuditIntegrityError("issued runtime replaced its actual current selected root")
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame or frame.phase != "effect_reconciling" or frame.accumulator.codec != "composer.tool-batch.v1":
        raise AuditIntegrityError("issued runtime lost its actual prepared current call")
    data = decode_prepared_batch_data(_selected_chunk(selected, frame).canonical, env_ref_names=env_ref_names)
    call = data.batch.current_call
    if call is None or call.effect_key is not None:
        raise AuditIntegrityError("issued runtime changed its pure prepared source")
    key = f"tool:{frame.response.digest}:{call.index}"
    issue = require_tool_issue_on_connection(conn, running, effect_key=key, env_ref_names=env_ref_names)
    if (
        issue.cursor != selected.stack.cursor
        or issue.prepared != frame.accumulator
        or issue.response != frame.response
        or issue.tool_index != frame.next_tool_index
        or issue.tool_call_id != call.tool_call.id
    ):
        raise AuditIntegrityError("issued runtime changed its actual prepared physical source")
    return key


def _selected_chunk(selected: SelectedContinuationState, frame: ToolBatchFrame) -> PrivateChunk:
    matches = tuple(chunk for chunk in selected.chunks if chunk.reference == frame.accumulator)
    if len(matches) != 1:
        raise AuditIntegrityError("phase transition lost its exact selected batch chunk")
    return matches[0]


def retain_batch_transition(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    captured: CapturedContinuationRuntime,
    inventory: CurrentContinuationEnvInventory,
    transition: BatchTransition,
) -> StoredContinuationSnapshot:
    if transition not in ("batch_initialized", "tool_issued", "tool_completed", "tool_preparation_completed"):
        raise ValueError("batch transition has no implemented source owner")
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(running) is not ComposerOperationRunning
        or type(captured) is not CapturedContinuationRuntime
        or type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
    ):
        raise AuditIntegrityError("phase transition lost its actual current source/claim owners")
    selected = captured.selected
    sequence, generation = running.claim.segment_sequence, running.claim.ownership_generation
    if sequence is None or generation is None:
        raise AuditIntegrityError("batch transition requires its actual V2 sequence and generation")
    following = SelectedContinuationState(selected.checkpoint_id, captured.stack, captured.chunks)
    before, after = selected.stack, following.stack
    if (
        after.logical != before.logical
        or after.mutation_ceiling != before.mutation_ceiling
        or after.admission_policy_json != before.admission_policy_json
        or after.policy_hash != before.policy_hash
        or after.provider_cost_policy != before.provider_cost_policy
        or after.cursor != before.cursor
        or len(after.frames) != 2
        or type(after.frames[0]) is not ComposeFrame
        or type(after.frames[-1]) is not ToolBatchFrame
    ):
        raise AuditIntegrityError("batch transition changed its immutable request or frame family")
    frame = after.frames[-1]
    names = inventory.names
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        if transition == "tool_completed":
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            if (
                inventory.selection.checkpoint_id != selected.checkpoint_id
                or inventory.selection.snapshot_json != encode_continuation_snapshot(before)
            ):
                raise AuditIntegrityError("owed tool completion changed its issued source inventory")
        else:
            _require_current_continuation_running(conn, running)
            require_current_env_inventory_on_connection(
                conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=before.cursor
            )
        stored = load_snapshot(conn, session_id=before.logical.session_id, operation_id=before.logical.operation_id)
        if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(before):
            raise AuditIntegrityError("batch transition replaced its actual selected predecessor")
        response = (
            read_committed_primary_response_for_settlement_on_connection(conn, running, selected=selected, reference=frame.response)
            if transition == "tool_completed"
            else read_committed_primary_response_on_connection(conn, running, selected=selected, reference=frame.response)
        )
        if after.frames[0].pending_response != frame.response:
            raise AuditIntegrityError("batch transition replaced its exact paid provider response")
        key: str | None = None
        issue: ToolIssueSource | None = None
        receipt: str | None = None
        data: RestoredBatchData | RestoredPreparedBatchData
        if transition == "batch_initialized":
            previous = before.frames[-1]
            data = decode_coupled_batch_chunk(_selected_chunk(following, frame), env_ref_names=names)
            if (
                len(before.frames) != 1
                or type(previous) is not ComposeFrame
                or previous.phase != "response_retained"
                or previous.pending_response != frame.response
                or frame.phase != "tool_ready"
                or frame.next_tool_index != 0
                or frame.settled_effect_ids
                or data.batch.phase not in ("call_ready", "dispatch_ready")
                or data.batch.next_tool_index != 0
                or data.batch.current_call is not None
                or after.effect_frontier != before.effect_frontier
            ):
                raise AuditIntegrityError("batch initialization is not its exact unconsumed provider response")
        elif transition == "tool_issued":
            previous = before.frames[-1]
            if type(previous) is not ToolBatchFrame:
                raise AuditIntegrityError("tool issue lost its actual completed-prefix predecessor")
            original = decode_coupled_batch_chunk(_selected_chunk(selected, previous), env_ref_names=names)
            if original.batch.tool_outcomes:
                require_completed_runtime_prefix_on_connection(conn, running, selected=selected, env_ref_names=names)
            data = decode_prepared_batch_data(_selected_chunk(following, frame).canonical, env_ref_names=names)
            call = data.batch.current_call
            if (
                previous.phase != "tool_ready"
                or previous.response != frame.response
                or previous.next_tool_index != frame.next_tool_index
                or original.batch.phase != "call_ready"
                or original.batch.current_call is not None
                or original.batch.next_tool_index != frame.next_tool_index
                or data.calls != original.calls
                or data.batch.tool_outcomes != original.batch.tool_outcomes
                or frame.phase != "effect_reconciling"
                or call is None
                or call.phase != "prepared"
                or call.index != frame.next_tool_index
                or (call.original_creator_id, call.original_creator_epoch)
                != (running.session_operation_context.fence.operation_id, running.session_operation_context.fence.operation_epoch)
            ):
                raise AuditIntegrityError("tool issue changed its actual prepared call or creator")
            key = f"tool:{frame.response.digest}:{call.index}"
            input_hash = stable_hash(
                {"tool_call_id": call.tool_call.id, "name": call.tool_name, "arguments": call.private_arguments_canonical}
            )
            if call.effect_key is not None:
                raise AuditIntegrityError("tool issue injected a SQL-owner locator into prepared DATA")
            issue = ToolIssueSource(
                previous_checkpoint_id=stored.checkpoint_id,
                cursor=stored.cursor + 1,
                sequence=sequence,
                generation=generation,
                creator_operation_id=running.session_operation_context.fence.operation_id,
                creator_operation_epoch=running.session_operation_context.fence.operation_epoch,
                response=frame.response,
                prepared=frame.accumulator,
                tool_index=call.index,
                tool_call_id=call.tool_call.id,
                input_sha256=input_hash,
            )
            expected = (*before.effect_frontier, EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None))
            if after.effect_frontier != expected or after.pending_owner_results or after.pending_settlement_ids:
                raise AuditIntegrityError("tool issue changed another frontier or pending owner")
            conn.execute(
                insert(effects).values(
                    session_id=before.logical.session_id,
                    operation_id=before.logical.operation_id,
                    effect_key=key,
                    input_sha256=input_hash,
                    owner="tool_dispatch",
                    action_json=canonical_json(issue.model_dump(mode="json")),
                    disposition="dispatched",
                    creator_sequence=running.claim.segment_sequence,
                    creator_generation=running.claim.ownership_generation,
                )
            )
        elif transition == "tool_preparation_completed":
            previous = before.frames[-1]
            if type(previous) is not ToolBatchFrame or previous.phase != "tool_ready":
                raise AuditIntegrityError("tool preparation result lost its actual unissued predecessor")
            original = decode_coupled_batch_chunk(_selected_chunk(selected, previous), env_ref_names=names)
            data = decode_coupled_batch_chunk(_selected_chunk(following, frame), env_ref_names=names)
            if (
                original.batch.current_call is not None
                or original.batch.phase != "call_ready"
                or data.calls != original.calls
                or frame.phase != "tool_ready"
                or previous.response != frame.response
                or frame.next_tool_index != previous.next_tool_index + 1
                or data.batch.next_tool_index != frame.next_tool_index
                or data.batch.tool_outcomes[:-1] != original.batch.tool_outcomes
                or data.batch.tool_outcomes[-1].call != original.calls[previous.next_tool_index]
                or data.batch.current_call is not None
                or after.effect_frontier != before.effect_frontier
                or frame.settled_effect_ids != previous.settled_effect_ids
                or after.pending_owner_results
                or after.pending_settlement_ids
            ):
                raise AuditIntegrityError("tool preparation result replaced its actual completed prefix")
        else:
            previous = before.frames[-1]
            if type(previous) is not ToolBatchFrame:
                raise AuditIntegrityError("tool completion lost its selected issued frame")
            prepared = decode_prepared_batch_data(_selected_chunk(selected, previous).canonical, env_ref_names=names)
            data = decode_coupled_batch_chunk(_selected_chunk(following, frame), env_ref_names=names)
            call = prepared.batch.current_call
            if (
                previous.phase != "effect_reconciling"
                or call is None
                or frame.phase != "tool_ready"
                or previous.response != frame.response
                or data.calls != prepared.calls
                or frame.next_tool_index != call.index + 1
                or data.batch.next_tool_index != frame.next_tool_index
                or data.batch.current_call is not None
                or len(data.batch.tool_outcomes) != len(prepared.batch.tool_outcomes) + 1
                or data.batch.tool_outcomes[-1].call != call.tool_call
            ):
                raise AuditIntegrityError("tool completion lost its exact issued call/result prefix")
            key = f"tool:{frame.response.digest}:{call.index}"
            receipt = f"tool-result:{frame.accumulator.digest}"
            require_tool_issue_on_connection(conn, running, effect_key=key, env_ref_names=names)
            expected = tuple(
                EffectFrontierEntry(effect_id=entry.effect_id, disposition="committed", receipt_id=receipt)
                if entry.effect_id == key
                else entry
                for entry in before.effect_frontier
            )
            if after.effect_frontier != expected or frame.settled_effect_ids != (*previous.settled_effect_ids, key):
                raise AuditIntegrityError("tool completion changed another effect or result order")
        from elspeth.web.composer.continuation_codec import decode_continuation_response

        if data.calls != decode_continuation_response(response).tool_batch.calls:
            raise AuditIntegrityError("batch transition changed its provider-authored tool calls")
        for chunk in captured.chunks:
            put_chunk(
                conn,
                session_id=before.logical.session_id,
                operation_id=before.logical.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        if transition == "tool_completed":
            changed = conn.execute(
                update(effects)
                .where(
                    effects.c.session_id == before.logical.session_id,
                    effects.c.operation_id == before.logical.operation_id,
                    effects.c.effect_key == key,
                    effects.c.owner == "tool_dispatch",
                    effects.c.disposition == "dispatched",
                    effects.c.creator_sequence == running.claim.segment_sequence,
                    effects.c.creator_generation == running.claim.ownership_generation,
                )
                .values(disposition="committed", receipt_key=receipt, result_chunk_key=private_chunk_key(_selected_chunk(following, frame)))
            )
            if changed.rowcount != 1:
                raise AuditIntegrityError("tool completion lost its exact current issued effect")
        policy = decode_continuation_policy(before.admission_policy_json, before.policy_hash)
        action = max(
            (policy.actions.read, policy.actions.session_sql, policy.actions.blob, policy.actions.external), key=lambda value: value.total
        )
        conn.execute(
            update(segments)
            .where(
                segments.c.session_id == before.logical.session_id,
                segments.c.operation_id == before.logical.operation_id,
                segments.c.sequence == running.claim.segment_sequence,
                segments.c.generation == running.claim.ownership_generation,
            )
            .values(next_action_json=action.model_dump_json())
        )
        payload = (
            {"owner": "composer_tool_phase", "transition": "tool_issued", "issue": issue.model_dump(mode="json")}
            if issue is not None
            else {
                "owner": "composer_tool_phase",
                "transition": transition,
                "effect_key": key,
                "response": frame.response.digest,
                "batch": frame.accumulator.digest,
            }
        )
        retained = append_complete_snapshot(
            conn,
            session_id=before.logical.session_id,
            operation_id=before.logical.operation_id,
            expected_checkpoint_id=stored.checkpoint_id,
            expected_cursor=stored.cursor,
            snapshot_json=encode_continuation_snapshot(after),
            event_batch=(
                ContinuationEvent(
                    "may_dispatch" if transition == "tool_issued" else "checkpoint",
                    canonical_json(payload),
                ),
            ),
            handoff=False,
        )
        # Validate the newly stored complete graph before this transaction may
        # commit. A malformed captured graph rolls back the whole transition.
        from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection

        current = SelectedContinuationState(retained.checkpoint_id, decode_continuation_snapshot(retained.payload_json), captured.chunks)
        read_selected_turn_evidence_graph_on_connection(conn, running, selected=current, names=names)
        return retained

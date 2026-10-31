"""Historical P4 projection into an existing live request evidence owner.

Every application reads the actual selected result under current SQL custody.
The cache is live exact-once projection state, not authorization, a durable
application checkpoint, or a replacement for complete cold TurnEvidence restore.
No committed witness, writer outcome, exception or provider budget is decoded.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame, InitializeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, RecorderSnapshot
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_codec import decode_continuation_response
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_owed_p4 import _require_current_read_owners, require_selected_owed_p4_inventory
from elspeth.web.composer.continuation_p4_batch import decode_p4_pending_batch_chunk, encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_p4_outcome import historical_checkpoint_record
from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Value, decode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.turn_effects import (
    PersistedBatchEffects,
    PersistedTurnEffects,
    TurnEvidence,
    _changed_fields,
    authored_state_hash,
)
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.schemas import SendMessageRequest


@dataclass(frozen=True, slots=True)
class HistoricalP4Application:
    """One live applied projection, retaining all original DATA owners."""

    checkpoint_id: str
    effect_key: str
    data: RestoredBatchData
    result: ReadmittedP4Result
    recorder: BufferingRecorder
    recorder_before: RecorderSnapshot
    recorder_after: RecorderSnapshot
    effects_before: PersistedTurnEffects
    effects_after: PersistedTurnEffects
    verification_failure: AuditIntegrityError | None


def selected_p4_batch_data(selected: SelectedContinuationState, *, env_ref_names: frozenset[str]) -> RestoredBatchData:
    """Decode one complete selected graph; preserve its shared accumulator."""
    if type(selected) is not SelectedContinuationState:
        raise TypeError("P4 application requires the actual complete selected DATA")
    selected.__post_init__()
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("P4 application requires a selected tool batch")
    chunk = next(item for item in selected.chunks if item.reference == frame.accumulator)
    response_chunk = next(item for item in selected.chunks if item.reference == frame.response)
    data = decode_p4_pending_batch_chunk(chunk, env_ref_names=env_ref_names)
    response = decode_continuation_response(response_chunk)
    if data.calls != response.tool_batch.calls or data.batch.next_tool_index != frame.next_tool_index:
        raise AuditIntegrityError("P4 application changed its selected response/cursor")
    return data


def _selected_request_content_hash(selected: SelectedContinuationState) -> str:
    """Use the real full bootstrap or the immutable pre-bootstrap send input."""
    logical = selected.stack.logical
    frame: ComposeFrame | InitializeFrame | None = None
    for retained_frame in selected.stack.frames:
        if type(retained_frame) is ComposeFrame or type(retained_frame) is InitializeFrame:
            frame = retained_frame
    if frame is None:
        raise AuditIntegrityError("P4 projection has no original request owner")
    if frame.bootstrap is not None:
        chunk = next(item for item in selected.chunks if item.reference == frame.bootstrap)
        bootstrap = decode_continuation_bootstrap(chunk)
        if bootstrap.logical != logical:
            raise AuditIntegrityError("P4 projection changed its complete original bootstrap binding")
        return stable_hash(bootstrap_values(bootstrap).message)
    if type(frame) is not InitializeFrame or logical.kind != "compose_message":
        raise AuditIntegrityError("P4 request content requires its actual retained bootstrap")
    try:
        request = SendMessageRequest.model_validate_json(frame.request_json, strict=True)
    except (ValueError, TypeError) as error:
        raise AuditIntegrityError("P4 original send request violates its owned schema") from error
    if (
        str(request.operation_id) != logical.operation_id
        or (str(request.state_id) if request.state_id is not None else None) != logical.base_state_id
        or composer_operation_v2_request_hash(session_id=UUID(logical.session_id), kind=logical.kind, request=request)
        != logical.request_hash
    ):
        raise AuditIntegrityError("P4 projection changed its immutable original send request")
    return stable_hash(request.content)


def _join_graph(selected: SelectedContinuationState, data: RestoredBatchData, *, env_ref_names: frozenset[str]) -> PreparedP4Value:
    if type(data) is not RestoredBatchData:
        raise TypeError("P4 projection requires the actual coupled graph")
    selected.__post_init__()
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("P4 application lost its selected frame")
    chunk = encode_p4_pending_batch_chunk(batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=env_ref_names)
    selected_chunk = next(item for item in selected.chunks if item.reference == frame.accumulator)
    response = decode_continuation_response(next(item for item in selected.chunks if item.reference == frame.response))
    if (
        chunk != selected_chunk
        or data.calls != response.tool_batch.calls
        or data.batch.next_tool_index != frame.next_tool_index
        or data.batch.p4_input_json is None
    ):
        raise AuditIntegrityError("P4 application changed a selected graph value or alias")
    prepared = decode_prepared_p4(data.batch.p4_input_json, env_ref_names=env_ref_names)
    if (
        prepared.session_id != selected.stack.logical.session_id
        or prepared_p4_members(prepared, env_ref_names=env_ref_names).mutation_ceiling != selected.stack.mutation_ceiling.materialize()
    ):
        raise AuditIntegrityError("P4 application changed its original mutation ceiling/session")
    return prepared


def _project_recorder(
    before: RecorderSnapshot, prepared: PreparedP4Value, result: ReadmittedP4Result, *, env_ref_names: frozenset[str]
) -> RecorderSnapshot:
    before.validate_links_and_cursors()
    members = prepared_p4_members(prepared, env_ref_names=env_ref_names)
    cursor = before.drained
    count = len(members.invocations)
    if (
        before.invocations[cursor.invocations : cursor.invocations + count] != members.invocations
        or before.private_authorings[cursor.private_authorings : cursor.private_authorings + len(members.authorings)] != members.authorings
        or before.withheld_replies[cursor.withheld_replies : cursor.withheld_replies + len(members.withheld_replies)]
        != members.withheld_replies
    ):
        raise AuditIntegrityError("P4 application lost its exact undrained private cohorts")
    expected_links = tuple(
        (cursor.invocations + index, row.visible_event) for index, row in enumerate(members.redacted_rows) if row.visible_event is not None
    )
    links = before.visible_tool_events[cursor.visible_tool_events : cursor.visible_tool_events + len(expected_links)]
    if tuple((link.invocation_index, link.event) for link in links) != expected_links:
        raise AuditIntegrityError("P4 application lost its exact original visible aliases")
    # A rolled-back writer publishes none of these channels, even when its
    # complete source input remains retained for the original failure path.
    if result.outcome.unwind_audit_failed:
        return before
    after = replace(
        before,
        drained=replace(
            cursor,
            invocations=cursor.invocations + count,
            private_authorings=cursor.private_authorings + len(members.authorings),
            withheld_replies=cursor.withheld_replies + len(members.withheld_replies),
            visible_tool_events=cursor.visible_tool_events + len(expected_links),
        ),
    )
    after.validate_links_and_cursors()
    return after


def _project_effects(
    before: PersistedTurnEffects,
    result: ReadmittedP4Result,
    data: RestoredBatchData,
    *,
    env_ref_names: frozenset[str],
    failure_codes: list[str] | None = None,
) -> tuple[PersistedTurnEffects, AuditIntegrityError | None]:
    receipt, outcome, historical = result.receipt, result.outcome, result.historical_effect
    if outcome.checkpoint_disposition != "committed":
        if historical is not None:
            raise AuditIntegrityError("nongraph P4 application invented historical graph evidence")
        return before, None
    checkpoint = outcome.checkpoint
    if (
        historical is None
        or checkpoint is None
        or outcome.assistant_id is None
        or historical.effect_key != receipt.effect_key
        or historical.input_sha256 != receipt.input_sha256
        or historical.creator_operation_id != receipt.creator_operation_id
        or historical.receipt_id != outcome.assistant_id
        or historical.inserted_state_ids != receipt.inserted_state_ids
        or historical.authored_state_hash != receipt.authored_state_hash
    ):
        raise AuditIntegrityError("P4 historical projection lost its checked original effect")
    inserted = tuple(str(item) for item in historical.inserted_state_ids)
    if set(inserted) & set(before.committed_revision_ids):
        raise AuditIntegrityError("P4 historical checkpoint already belongs to accepted history")
    # As in the live witness path, retain actual committed identity even when
    # its subsequent authored-state comparison fails. No witness is minted.
    committed = replace(
        before, latest_committed_state_id=str(checkpoint.id), committed_revision_ids=(*before.committed_revision_ids, *inserted)
    )
    failure_code = "checkpoint_decode_or_compare_failed"
    try:
        saved = state_from_record(historical_checkpoint_record(checkpoint, env_ref_names=env_ref_names))
        expected = next(
            (
                item.response.updated_state
                for item in reversed(data.batch.tool_outcomes)
                if item.post_version > item.pre_version and type(item.response) is ToolResult
            ),
            None,
        )
        if expected is None or authored_state_hash(saved) != authored_state_hash(expected):
            failure_code = "authored_state_mismatch"
            raise AuditIntegrityError("historical committed state differs from the selected dispatched graph")
        saved_hash = authored_state_hash(saved)
        changed = _changed_fields(before.initial_state, saved)
    except (ValueError, TypeError, AuditIntegrityError) as error:
        if failure_codes is not None:
            failure_codes.append(failure_code)
        if isinstance(error, AuditIntegrityError):
            failure = error
        else:
            failure = AuditIntegrityError("historical checkpoint could not be decoded or compared")
            failure.__cause__ = error
        return replace(committed, verification="unavailable"), failure
    batch = PersistedBatchEffects(
        assistant_message_id=outcome.assistant_id,
        state_id=str(checkpoint.id),
        version=checkpoint.version,
        tool_call_ids=tuple(item.call.id for item in data.batch.tool_outcomes),
        inserted_state_ids=inserted,
        authored_state_hash=saved_hash,
    )
    return replace(
        committed,
        latest_verified_state=saved,
        latest_verified_state_id=str(checkpoint.id),
        latest_authored_state_hash=saved_hash,
        net_changed_fields=changed,
        verification="verified",
        batches=(*before.batches, batch),
    ), None


async def apply_selected_p4_to_live_evidence(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    required_work: RequiredWorkBinding,
    inventory: CurrentContinuationEnvInventory,
    data: RestoredBatchData,
    evidence: TurnEvidence,
    recorder: BufferingRecorder,
    drained: RecorderDrainCursors,
) -> HistoricalP4Application:
    """Apply a historical read once to these EXISTING live owners.

    Retrying still requires a fresh current read. Callers must keep the returned
    cursors and same graph; this method never substitutes restored equal copies.
    This does not advance the SQL root, restore evidence, or run a finisher.
    """
    if (
        type(evidence) is not TurnEvidence
        or type(recorder) is not BufferingRecorder
        or type(drained) is not RecorderDrainCursors
        or type(inventory) is not CurrentContinuationEnvInventory
    ):
        raise TypeError("P4 application requires actual live evidence, recorder and current inventory")
    running = _require_current_read_owners(authority, selected, required_work)
    require_selected_owed_p4_inventory(selected, inventory, running)
    logical = selected.stack.logical
    request_content_hash = _selected_request_content_hash(selected)
    if (
        evidence.session_id != logical.session_id
        or evidence.operation_id != running.session_operation_context.fence.operation_id
        or evidence.request_id != logical.original_user_message_id
        or evidence.request_hash != request_content_hash
    ):
        raise AuditIntegrityError("historical P4 application changed its current evidence context")
    prepared = _join_graph(selected, data, env_ref_names=inventory.names)
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("P4 application has no selected frame")
    effect_key = f"batch:{frame.response.digest}:p4"
    before = evidence.effects
    if before.initial_state_id != logical.base_state_id:
        raise AuditIntegrityError("P4 projection changed its original request base identity")
    snapshot = recorder.snapshot_for_continuation(drained=drained)
    previous = evidence._historical_p4_applications.get(effect_key)

    def project(result: ReadmittedP4Result) -> HistoricalP4Application:
        if type(result) is not ReadmittedP4Result:
            raise AuditIntegrityError("P4 application returned another nominal result")
        if (
            evidence.effects is not before
            or recorder.snapshot_for_continuation(drained=drained) != snapshot
            or evidence._historical_p4_applications.get(effect_key) is not previous
            or evidence.session_id != logical.session_id
            or evidence.operation_id != running.session_operation_context.fence.operation_id
            or evidence.request_id != logical.original_user_message_id
            or evidence.request_hash != request_content_hash
        ):
            raise AuditIntegrityError("live P4 projection owners changed during the current read")
        if _join_graph(selected, data, env_ref_names=inventory.names) != prepared:
            raise AuditIntegrityError("selected P4 graph changed during the current read")
        if result.receipt.prepared_p4_json != data.batch.p4_input_json:
            raise AuditIntegrityError("current P4 result changed its complete selected input")
        if previous is not None:
            if (
                previous.checkpoint_id != selected.checkpoint_id
                or previous.data is not data
                or previous.recorder is not recorder
                or previous.result != result
                or previous.effects_after is not evidence.effects
                or previous.recorder_after != snapshot
            ):
                raise AuditIntegrityError("P4 retry changed its actual previously applied owners or cursors")
            return previous
        if before.latest_verified_state_id != data.batch.pre_state_id or before.latest_authored_state_hash != authored_state_hash(
            data.batch.entry_state
        ):
            raise AuditIntegrityError("P4 application changed its previously accepted state history")
        following_recorder = _project_recorder(snapshot, prepared, result, env_ref_names=inventory.names)
        following_effects, failure = _project_effects(before, result, data, env_ref_names=inventory.names)
        applied = HistoricalP4Application(
            selected.checkpoint_id, effect_key, data, result, recorder, snapshot, following_recorder, before, following_effects, failure
        )
        # No await/callback or exception reconstruction in the installation.
        # All complete graph/recorder/effect checks precede live mutation.
        evidence._historical_p4_applications[effect_key] = applied
        evidence.effects = following_effects
        return applied

    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.read_continuation_owed_p4_result,
            running,
            inventory=inventory,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            effect_key=effect_key,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    applied: HistoricalP4Application | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            applied = project(outcome.value)
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned(applied.verification_failure)
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("P4 application returned an unknown SQL outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Historical P4 application outcomes", errors)
    if applied is None:
        raise AuditIntegrityError("P4 application lost its actual applied result")
    return applied

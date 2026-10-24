"""Pure portable evidence/P4 projection. Current SQL admission belongs to callers.

Every retained application is recomputable from complete input/result DATA.
No API constructs a committed witness, original exception, timer or callback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any, Literal

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_application_graph import (
    ApplicationGraph,
    ApplicationGraphCut,
    decode_application_graph,
    encode_application_graph,
)
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, RecorderSnapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, RestoredPreparedBatchData
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_response, encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
from elspeth.web.composer.continuation_evidence import (
    RetainedBatchEffects,
    RetainedHistoricalEffect,
    RetainedTurnEffects,
    TurnEvidenceSnapshot,
    encode_turn_evidence,
)
from elspeth.web.composer.continuation_evidence_application_values import (
    P4EffectProducerData,
    PortableP4ApplicationEntry,
    PortableP4ApplicationRecord,
)
from elspeth.web.composer.continuation_p4_application import HistoricalP4Application, _project_effects, _project_recorder
from elspeth.web.composer.continuation_p4_batch import encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_prepared_p4 import decode_prepared_p4, p4_semantic_input
from elspeth.web.composer.continuation_tool_batch import _read_json_value
from elspeth.web.composer.turn_effects import PersistedTurnEffects, TurnEvidence, authored_state_hash
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result, decode_p4_receipt

Verification = Literal["verified", "not_applicable", "authored_state_mismatch", "checkpoint_decode_or_compare_failed"]


@dataclass(frozen=True, slots=True)
class PortableP4ApplicationBundle:
    entry: PortableP4ApplicationEntry
    evidence_after: TurnEvidenceSnapshot
    batch_after: RestoredBatchData
    recorder_after: RecorderSnapshot
    evidence_chunk: PrivateChunk
    batch_chunk: PrivateChunk
    audit_chunk: PrivateChunk
    state_chunk: PrivateChunk


def retained_effects(value: PersistedTurnEffects) -> RetainedTurnEffects:
    if type(value) is not PersistedTurnEffects:
        raise TypeError("portable effects require the actual nominal owner")
    return RetainedTurnEffects(
        initial_state_json=canonical_json(value.initial_state.to_dict()),
        initial_state_id=value.initial_state_id,
        latest_verified_state_json=canonical_json(value.latest_verified_state.to_dict()),
        latest_verified_state_id=value.latest_verified_state_id,
        latest_committed_state_id=value.latest_committed_state_id,
        verification=value.verification,
        initial_authored_state_hash=value.initial_authored_state_hash,
        latest_authored_state_hash=value.latest_authored_state_hash,
        net_changed_fields=value.net_changed_fields,
        batches=tuple(
            RetainedBatchEffects(
                assistant_message_id=item.assistant_message_id,
                state_id=item.state_id,
                version=item.version,
                tool_call_ids=item.tool_call_ids,
                inserted_state_ids=item.inserted_state_ids,
                authored_state_hash=item.authored_state_hash,
            )
            for item in value.batches
        ),
        committed_revision_ids=value.committed_revision_ids,
    )


def _record_effects_aliases(before: PersistedTurnEffects, after: PersistedTurnEffects, code: Verification) -> None:
    if after.initial_state is not before.initial_state:
        raise AuditIntegrityError("application changed its original initial-state identity")
    if code == "not_applicable" and after is not before:
        raise AuditIntegrityError("nongraph application copied its unchanged effects")
    if (
        code in ("authored_state_mismatch", "checkpoint_decode_or_compare_failed")
        and after.latest_verified_state is not before.latest_verified_state
    ):
        raise AuditIntegrityError("failed verification replaced the previous verified state")
    if code == "verified" and (
        after.latest_verified_state is before.initial_state or after.latest_verified_state is before.latest_verified_state
    ):
        raise AuditIntegrityError("verified application reused a pre-comparison state object")
    if len(after.batches) < len(before.batches) or any(
        left is not right for left, right in zip(before.batches, after.batches, strict=False)
    ):
        raise AuditIntegrityError("application copied its previously retained batch effects")


def _recorder_pairs(value: RecorderSnapshot) -> tuple[tuple[Any, ...], ...]:
    return (
        value.invocations,
        value.llm_calls,
        value.planner_attempts,
        value.withheld_replies,
        value.visible_tool_events,
        value.private_candidates,
        value.private_authorings,
    )


def _recorder_aliases(before: RecorderSnapshot, after: RecorderSnapshot) -> None:
    for index, (old, new) in enumerate(zip(_recorder_pairs(before), _recorder_pairs(after), strict=True)):
        if len(old) != len(new):
            raise AuditIntegrityError("application replaced retained recorder objects")
        if index == 4:
            if any(
                left.invocation_index != right.invocation_index or left.event is not right.event
                for left, right in zip(old, new, strict=True)
            ):
                raise AuditIntegrityError("application changed a real invocation/event alias")
        elif any(left is not right for left, right in zip(old, new, strict=True)):
            raise AuditIntegrityError("application replaced retained recorder objects")


def _project(
    graph: ApplicationGraph, result: ReadmittedP4Result, *, env_ref_names: frozenset[str]
) -> tuple[PersistedTurnEffects, RecorderSnapshot, Verification]:
    batch = graph.selected.batch
    if batch.p4_input_json is None:
        raise AuditIntegrityError("portable application lost its complete original P4 input")
    prepared = decode_prepared_p4(batch.p4_input_json, env_ref_names=env_ref_names)
    after_recorder = _project_recorder(graph.recorder, prepared, result, env_ref_names=env_ref_names)
    failures: list[str] = []
    after_effects, original_failure = _project_effects(
        graph.effects, result, graph.selected, env_ref_names=env_ref_names, failure_codes=failures
    )
    # The existing live helper creates/retains its original exception; this
    # DATA boundary deliberately keeps only its explicit producer branch code.
    if original_failure is not None:
        if failures == ["authored_state_mismatch"]:
            code: Verification = "authored_state_mismatch"
        elif failures == ["checkpoint_decode_or_compare_failed"]:
            code = "checkpoint_decode_or_compare_failed"
        else:
            raise AuditIntegrityError("portable verification lost its known original branch")
    else:
        code = "verified" if result.outcome.checkpoint_disposition == "committed" else "not_applicable"
    _record_effects_aliases(graph.effects, after_effects, code)
    # Pydantic's actual snapshot constructor may recreate visible link value
    # wrappers. Preserve those exact original immutable links explicitly after
    # the existing projection has validated their complete values.
    after_recorder = (
        graph.recorder
        if after_recorder is graph.recorder
        else replace(
            after_recorder,
            invocations=graph.recorder.invocations,
            llm_calls=graph.recorder.llm_calls,
            planner_attempts=graph.recorder.planner_attempts,
            withheld_replies=graph.recorder.withheld_replies,
            visible_tool_events=graph.recorder.visible_tool_events,
            private_candidates=graph.recorder.private_candidates,
            private_authorings=graph.recorder.private_authorings,
        )
    )
    _recorder_aliases(graph.recorder, after_recorder)
    return after_effects, after_recorder, code


def _checked_result(
    result_chunk: PrivateChunk, result: ReadmittedP4Result, producer: P4EffectProducerData, *, env_ref_names: frozenset[str]
) -> None:
    if (
        type(result_chunk) is not PrivateChunk
        or result_chunk.reference.codec != "composer.owner-result.v1"
        or type(result) is not ReadmittedP4Result
    ):
        raise TypeError("portable application requires the actual immutable receipt DATA")
    if type(producer) is not P4EffectProducerData:
        raise TypeError("portable application requires complete historical producer DATA")
    producer.__post_init__()
    verify_private_chunk(result_chunk.reference, result_chunk.canonical)
    receipt, outcome = decode_p4_receipt(result_chunk.canonical, env_ref_names=env_ref_names)
    if receipt != result.receipt or outcome != result.outcome:
        raise AuditIntegrityError("portable application result differs from its complete receipt bytes")
    prepared_json = receipt.prepared_p4_json
    if prepared_json is None:
        raise AuditIntegrityError("portable application lost its complete original P4 input")
    prepared = decode_prepared_p4(prepared_json, env_ref_names=env_ref_names)
    if (
        receipt.creator_operation_id != producer.creator_operation_id
        or prepared.creator_operation_epoch != producer.creator_operation_epoch
    ):
        raise AuditIntegrityError("portable application changed original producer identity")
    require_no_credential_material(producer.creator_operation_id, surface="portable P4 producer", env_ref_names=env_ref_names)


def _pending_chunk(data: RestoredBatchData, *, env_ref_names: frozenset[str]) -> PrivateChunk:
    return encode_p4_pending_batch_chunk(batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=env_ref_names)


def _require_input(
    logical: LogicalRequestBinding,
    response_chunk: PrivateChunk,
    pending_batch_chunk: PrivateChunk,
    graph: ApplicationGraph,
    result: ReadmittedP4Result,
    *,
    env_ref_names: frozenset[str],
) -> None:
    if type(logical) is not LogicalRequestBinding or graph.selected.batch.phase != "p4_pending":
        raise AuditIntegrityError("portable application requires its actual pending logical cut")
    response = decode_continuation_response(response_chunk)
    data = graph.selected
    if _pending_chunk(data, env_ref_names=env_ref_names) != pending_batch_chunk or data.calls != response.tool_batch.calls:
        raise AuditIntegrityError("portable application changed its selected complete batch/response")
    receipt = result.receipt
    if receipt.effect_key != f"batch:{response_chunk.reference.digest}:p4" or data.batch.p4_input_json != receipt.prepared_p4_json:
        raise AuditIntegrityError("portable application changed its actual effect/input")
    prepared_json = receipt.prepared_p4_json
    if prepared_json is None:
        raise AuditIntegrityError("portable application lost its complete original P4 input")
    prepared = decode_prepared_p4(prepared_json, env_ref_names=env_ref_names)
    from elspeth.contracts.hashing import stable_hash

    if (
        prepared.session_id != logical.session_id
        or stable_hash(_read_json_value(p4_semantic_input(prepared, env_ref_names=env_ref_names))) != receipt.input_sha256
    ):
        raise AuditIntegrityError("portable application changed its semantic input/session")
    if (
        graph.effects.initial_state_id != logical.base_state_id
        or graph.effects.latest_verified_state_id != data.batch.pre_state_id
        or graph.effects.latest_authored_state_hash != authored_state_hash(data.batch.entry_state)
    ):
        raise AuditIntegrityError("portable application changed previously accepted history")


def require_evidence_application_links(snapshot: TurnEvidenceSnapshot, *, env_ref_names: frozenset[str]) -> ApplicationGraph | None:
    if snapshot.application_entry is not None and snapshot.joint_graph_json is not None:
        raise AuditIntegrityError("evidence duplicated its journal graph outside its one owner")
    graph_json = snapshot.application_entry.graph_json if snapshot.application_entry is not None else snapshot.joint_graph_json
    if graph_json is None:
        return None
    graph = decode_application_graph(graph_json, env_ref_names=env_ref_names)
    if (
        retained_effects(graph.effects) != snapshot.effects
        or encode_recorder_snapshot(graph.recorder).canonical != snapshot.recorder_json
        or (graph.effects.initial_state is graph.effects.latest_verified_state) is not snapshot.initial_is_latest
    ):
        raise AuditIntegrityError("evidence differs from its actual shared effects/recorder graph")
    entry = snapshot.application_entry
    if entry is None:
        if (
            graph.cuts
            or graph.selected.batch.phase not in ("p4_pending", "call_active", "call_ready", "dispatch_ready")
            or snapshot.historical_application_count
        ):
            raise AuditIntegrityError("pending evidence invented applied history")
    elif (
        entry.logical != snapshot.logical
        or len(entry.applications) != len(graph.cuts)
        or snapshot.historical_application_count != len(graph.cuts)
    ):
        raise AuditIntegrityError("evidence journal lost its ordered graph/identity binding")
    if entry is not None:
        admit_journal_values(entry, env_ref_names=env_ref_names)
        if snapshot.effect_locators != journal_effect_locators(entry, env_ref_names=env_ref_names):
            raise AuditIntegrityError("evidence changed its exact historical graph locators")
    if graph.cuts:
        if graph.effects is not graph.cuts[-1].effects_after:
            raise AuditIntegrityError("evidence differs from the final applied frontier")
        _require_recorder_extension(graph.cuts[-1].recorder_after, graph.recorder)
        for prior, following in pairwise(graph.cuts):
            if following.effects_before is not prior.effects_after:
                raise AuditIntegrityError("application journal broke its actual effects identity chain")
    return graph


def append_primary_call_to_evidence(
    snapshot: TurnEvidenceSnapshot,
    *,
    call: ComposerLLMCall,
    recorder: RecorderSnapshot,
    env_ref_names: frozenset[str],
) -> TurnEvidenceSnapshot:
    """Extend the actual shared recorder graph without replacing any prior cut.

    Current SQL/physical admission belongs to the publication owner. This pure
    source update preserves original graph atom identities and immutable result
    records; it neither creates a P4 application nor authorizes a provider.
    """
    from elspeth.web.composer.continuation_evidence import recorder_from_evidence_json

    if type(snapshot) is not TurnEvidenceSnapshot or type(call) is not ComposerLLMCall or type(recorder) is not RecorderSnapshot:
        raise TypeError("primary evidence extension requires its complete actual nominal sources")
    before = recorder_from_evidence_json(snapshot.recorder_json)
    if (
        call.call_id is None
        or replace(before, llm_calls=(*before.llm_calls, call)) != recorder
        or not recorder.llm_calls
        or recorder.llm_calls[-1] is not call
        or any(item.call_id == call.call_id for item in before.llm_calls)
    ):
        raise AuditIntegrityError("primary evidence extension changed its exact original recorder prefix")
    graph = require_evidence_application_links(snapshot, env_ref_names=env_ref_names)
    updated = snapshot.model_copy(update={"recorder_json": encode_recorder_snapshot(recorder).canonical})
    if graph is not None:
        # The SQL snapshot and live recorder were independently decoded. Reuse
        # the actual graph's prefix objects so cut/batch/event aliases remain
        # shared; the one new atom is the actual original physical call.
        extended = replace(graph.recorder, llm_calls=(*graph.recorder.llm_calls, call))
        _require_recorder_extension(graph.recorder, extended)
        if encode_recorder_snapshot(extended) != encode_recorder_snapshot(recorder):
            raise AuditIntegrityError("primary evidence extension differs from its complete actual recorder")
        changed_graph = replace(graph, recorder=extended)
        graph_json = encode_application_graph(changed_graph, env_ref_names=env_ref_names)
        if snapshot.application_entry is not None:
            entry = snapshot.application_entry.model_copy(update={"graph_json": graph_json})
            admit_journal_values(entry, env_ref_names=env_ref_names)
            updated = updated.model_copy(update={"application_entry": entry})
        else:
            updated = updated.model_copy(update={"joint_graph_json": graph_json})
    require_evidence_application_links(updated, env_ref_names=env_ref_names)
    return updated


def build_portable_p4_application(
    *,
    logical: LogicalRequestBinding,
    checkpoint_id: str,
    cursor: int,
    response_chunk: PrivateChunk,
    pending_batch_chunk: PrivateChunk,
    evidence_snapshot: TurnEvidenceSnapshot,
    result_chunk: PrivateChunk,
    result: ReadmittedP4Result,
    producer: P4EffectProducerData,
    env_ref_names: frozenset[str],
) -> PortableP4ApplicationBundle:
    _checked_result(result_chunk, result, producer, env_ref_names=env_ref_names)
    encode_turn_evidence(evidence_snapshot, env_ref_names=env_ref_names)
    if evidence_snapshot.logical != logical:
        raise AuditIntegrityError("portable application changed the evidence logical binding")
    graph = require_evidence_application_links(evidence_snapshot, env_ref_names=env_ref_names)
    if graph is None:
        raise AuditIntegrityError("portable application requires the producer-captured joint identity graph")
    _require_input(logical, response_chunk, pending_batch_chunk, graph, result, env_ref_names=env_ref_names)
    previous = evidence_snapshot.application_entry
    records = () if previous is None else previous.applications
    if any(item.effect_key == result.receipt.effect_key for item in records):
        raise AuditIntegrityError("portable application effect already belongs to applied history")
    effects, recorder, code = _project(graph, result, env_ref_names=env_ref_names)
    cut = ApplicationGraphCut(graph.selected, graph.effects, effects, graph.recorder, recorder)
    complete = replace(
        graph.selected.batch,
        phase="complete",
        p4_receipt_json=result_chunk.canonical,
        p4_verification_failed=code in ("authored_state_mismatch", "checkpoint_decode_or_compare_failed"),
    )
    after_data = RestoredBatchData(complete, graph.selected.accumulator, graph.selected.calls)
    after_graph = ApplicationGraph(after_data, effects, recorder, (*graph.cuts, cut))
    graph_json = encode_application_graph(after_graph, env_ref_names=env_ref_names)
    prepared_json = result.receipt.prepared_p4_json
    if prepared_json is None:
        raise AuditIntegrityError("portable application lost its complete original P4 input")
    prepared_chunk = encode_private_chunk(codec="composer.prepared-p4.v1", canonical=prepared_json, env_ref_names=env_ref_names)
    record = PortableP4ApplicationRecord(
        checkpoint_id=checkpoint_id,
        cursor=cursor,
        effect_key=result.receipt.effect_key,
        input_sha256=result.receipt.input_sha256,
        receipt_id=str(result.receipt.assistant_id)
        if result.receipt.assistant_id is not None
        else f"unavailable:{result.receipt.effect_key}",
        creator_sequence=producer.creator_sequence,
        creator_generation=producer.creator_generation,
        creator_operation_id=producer.creator_operation_id,
        creator_operation_epoch=producer.creator_operation_epoch,
        response=response_chunk.reference,
        response_json=response_chunk.canonical,
        pending_batch=pending_batch_chunk.reference,
        pending_batch_json=pending_batch_chunk.canonical,
        prepared_input=prepared_chunk.reference,
        prepared_input_json=prepared_json,
        result=result_chunk.reference,
        result_json=result_chunk.canonical,
        verification=code,
    )
    entry = PortableP4ApplicationEntry(logical=logical, applications=(*records, record), graph_json=graph_json)
    locators = evidence_snapshot.effect_locators
    historical = result.historical_effect
    if historical is not None:
        locators = (
            *locators,
            RetainedHistoricalEffect(
                effect_key=historical.effect_key,
                input_sha256=historical.input_sha256,
                creator_operation_id=historical.creator_operation_id,
                receipt_id=historical.receipt_id,
                inserted_state_ids=historical.inserted_state_ids,
                authored_state_hash=historical.authored_state_hash,
            ),
        )
    after = evidence_snapshot.model_copy(
        update={
            "effects": retained_effects(effects),
            "initial_is_latest": effects.initial_state is effects.latest_verified_state,
            "recorder_json": encode_recorder_snapshot(recorder).canonical,
            "joint_graph_json": None,
            "application_entry": entry,
            "effect_locators": locators,
            "historical_application_count": len(entry.applications),
        }
    )
    require_portable_p4_application(entry, result_chunk=result_chunk, result=result, producer=producer, env_ref_names=env_ref_names)
    return PortableP4ApplicationBundle(
        entry,
        after,
        after_data,
        recorder,
        encode_turn_evidence(after, env_ref_names=env_ref_names),
        encode_private_chunk(codec="composer.applied-p4-batch.v1", canonical=graph_json, env_ref_names=env_ref_names),
        encode_recorder_snapshot(recorder),
        encode_private_chunk(
            codec="composer.state.v1", canonical=canonical_json(after_data.batch.state.to_dict()), env_ref_names=env_ref_names
        ),
    )


def require_portable_p4_application(
    entry: PortableP4ApplicationEntry,
    *,
    result_chunk: PrivateChunk,
    result: ReadmittedP4Result,
    producer: P4EffectProducerData,
    env_ref_names: frozenset[str],
) -> None:
    if type(entry) is not PortableP4ApplicationEntry:
        raise TypeError("portable application requires its closed journal owner")
    entry = PortableP4ApplicationEntry.model_validate_json(canonical_json(entry.model_dump(mode="json")))
    _checked_result(result_chunk, result, producer, env_ref_names=env_ref_names)
    admit_journal_values(entry, env_ref_names=env_ref_names)
    graph = decode_application_graph(entry.graph_json, env_ref_names=env_ref_names)
    if len(entry.applications) != len(graph.cuts):
        raise AuditIntegrityError("portable journal lost a complete projection cut")
    matches = [(index, item) for index, item in enumerate(entry.applications) if item.effect_key == result.receipt.effect_key]
    if len(matches) != 1:
        raise AuditIntegrityError("current result has no unique retained application")
    index, record = matches[0]
    if (
        record.result != result_chunk.reference
        or record.result_json != result_chunk.canonical
        or record.input_sha256 != result.receipt.input_sha256
        or (record.creator_sequence, record.creator_generation, record.creator_operation_id, record.creator_operation_epoch)
        != (producer.creator_sequence, producer.creator_generation, producer.creator_operation_id, producer.creator_operation_epoch)
    ):
        raise AuditIntegrityError("portable journal changed actual result/producer bytes")
    cut = graph.cuts[index]
    before = ApplicationGraph(cut.data, cut.effects_before, cut.recorder_before, graph.cuts[:index])
    response = PrivateChunk(record.response, record.response_json)
    pending = PrivateChunk(record.pending_batch, record.pending_batch_json)
    _require_input(entry.logical, response, pending, before, result, env_ref_names=env_ref_names)
    prepared_json = result.receipt.prepared_p4_json
    if prepared_json is None:
        raise AuditIntegrityError("portable application lost its complete original P4 input")
    prepared = encode_private_chunk(codec="composer.prepared-p4.v1", canonical=prepared_json, env_ref_names=env_ref_names)
    if record.prepared_input != prepared.reference or record.prepared_input_json != prepared.canonical:
        raise AuditIntegrityError("portable journal changed its complete prepared input locator")
    expected_effects, expected_recorder, code = _project(before, result, env_ref_names=env_ref_names)
    if (
        record.verification != code
        or retained_effects(cut.effects_after) != retained_effects(expected_effects)
        or encode_recorder_snapshot(cut.recorder_after) != encode_recorder_snapshot(expected_recorder)
    ):
        raise AuditIntegrityError("portable journal differs from actual historical projection")
    _record_effects_aliases(cut.effects_before, cut.effects_after, code)
    _recorder_aliases(cut.recorder_before, cut.recorder_after)
    if result.outcome.unwind_audit_failed and cut.recorder_after is not cut.recorder_before:
        raise AuditIntegrityError("rolled-back application copied its unchanged recorder frontier")
    expected_receipt = str(result.receipt.assistant_id) if result.receipt.assistant_id is not None else f"unavailable:{record.effect_key}"
    if record.receipt_id != expected_receipt:
        raise AuditIntegrityError("portable journal changed its actual receipt locator")
    if index == len(graph.cuts) - 1:
        if graph.effects is not cut.effects_after:
            raise AuditIntegrityError("portable journal changed the actual final frontier")
        _require_recorder_extension(cut.recorder_after, graph.recorder)
        if graph.selected.batch.phase == "complete" and (
            graph.selected.batch.p4_receipt_json != record.result_json
            or graph.selected.batch.p4_verification_failed != (code in ("authored_state_mismatch", "checkpoint_decode_or_compare_failed"))
        ):
            raise AuditIntegrityError("completed batch changed its actual result/verification")


def journal_effect_locators(entry: PortableP4ApplicationEntry, *, env_ref_names: frozenset[str]) -> tuple[RetainedHistoricalEffect, ...]:
    """Historical locator values only; no validated SQL owner is constructed."""
    locators = []
    for record in entry.applications:
        receipt, outcome = decode_p4_receipt(record.result_json, env_ref_names=env_ref_names)
        if outcome.checkpoint_disposition == "committed":
            if receipt.authored_state_hash is None:
                raise AuditIntegrityError("graph receipt lost its actual authored hash")
            locators.append(
                RetainedHistoricalEffect(
                    effect_key=record.effect_key,
                    input_sha256=record.input_sha256,
                    creator_operation_id=record.creator_operation_id,
                    receipt_id=record.receipt_id,
                    inserted_state_ids=receipt.inserted_state_ids,
                    authored_state_hash=receipt.authored_state_hash,
                )
            )
    return tuple(locators)


def admit_journal_values(entry: PortableP4ApplicationEntry, *, env_ref_names: frozenset[str]) -> None:
    """Complete owned DATA admission; root still owes every actual SQL join."""
    encoded = canonical_json(entry.model_dump(mode="json"))
    checked = PortableP4ApplicationEntry.model_validate_json(encoded)
    if canonical_json(checked.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("portable journal changed during complete inner admission")
    graph = decode_application_graph(entry.graph_json, env_ref_names=env_ref_names)
    if len(graph.cuts) != len(entry.applications):
        raise AuditIntegrityError("portable journal lost its exact ordered cuts")
    _require_initial_frontier(graph.cuts[0].effects_before, entry.logical)
    for prior, following in pairwise(graph.cuts):
        if following.effects_before is not prior.effects_after:
            raise AuditIntegrityError("portable journal broke its real effects identity chain")
        _require_recorder_extension(prior.recorder_after, following.recorder_before)
    if graph.effects is not graph.cuts[-1].effects_after:
        raise AuditIntegrityError("portable journal changed its current effects identity")
    _require_recorder_extension(graph.cuts[-1].recorder_after, graph.recorder)
    _require_completed_graph(graph, entry.applications[-1])
    for index, record in enumerate(entry.applications):
        response = PrivateChunk(record.response, record.response_json)
        decoded_response = decode_continuation_response(response)
        verify_private_chunk(record.pending_batch, record.pending_batch_json)
        verify_private_chunk(record.prepared_input, record.prepared_input_json)
        verify_private_chunk(record.result, record.result_json)
        if record.result.codec != "composer.owner-result.v1" or record.prepared_input.codec != "composer.prepared-p4.v1":
            raise AuditIntegrityError("portable journal substituted an immutable owner codec")
        receipt, outcome = decode_p4_receipt(record.result_json, env_ref_names=env_ref_names)
        prepared = decode_prepared_p4(record.prepared_input_json, env_ref_names=env_ref_names)
        cut = graph.cuts[index]
        if (
            _pending_chunk(cut.data, env_ref_names=env_ref_names) != PrivateChunk(record.pending_batch, record.pending_batch_json)
            or cut.data.calls != decoded_response.tool_batch.calls
            or record.effect_key != f"batch:{record.response.digest}:p4"
            or receipt.effect_key != record.effect_key
            or receipt.input_sha256 != record.input_sha256
            or receipt.prepared_p4_json != record.prepared_input_json
            or receipt.creator_operation_id != record.creator_operation_id
            or prepared.creator_operation_epoch != record.creator_operation_epoch
            or prepared.session_id != entry.logical.session_id
        ):
            raise AuditIntegrityError("portable journal changed its complete selected input/result joins")
        expected_locator = str(receipt.assistant_id) if receipt.assistant_id is not None else f"unavailable:{record.effect_key}"
        if record.receipt_id != expected_locator:
            raise AuditIntegrityError("portable journal changed its immutable receipt locator")
        if (outcome.checkpoint_disposition == "committed") != (record.verification != "not_applicable"):
            raise AuditIntegrityError("portable journal changed graph verification applicability")
        require_no_credential_material(
            [record.checkpoint_id, record.effect_key, record.receipt_id, record.creator_operation_id],
            surface="portable application metadata",
            env_ref_names=env_ref_names,
        )
        _record_effects_aliases(cut.effects_before, cut.effects_after, record.verification)
        _recorder_aliases(cut.recorder_before, cut.recorder_after)


def capture_joint_evidence_graph(
    *,
    owner: TurnEvidence,
    recorder: BufferingRecorder,
    drained: RecorderDrainCursors,
    data: RestoredBatchData | RestoredPreparedBatchData | None,
    env_ref_names: frozenset[str],
) -> tuple[str | None, PortableP4ApplicationEntry | None]:
    if type(owner) is not TurnEvidence or type(recorder) is not BufferingRecorder or type(drained) is not RecorderDrainCursors:
        raise TypeError("joint capture requires actual evidence/recorder owners")
    prior = owner._portable_p4_graph
    entry = owner._portable_p4_entry
    if (prior is None) != (entry is None):
        raise AuditIntegrityError("live evidence lost its paired portable graph/journal")
    if data is None:
        if prior is None:
            if owner._historical_p4_applications:
                raise AuditIntegrityError("warm applications require their actual portable producer journal")
            return None, None
        data = (
            RestoredPreparedBatchData(
                prior.selected.batch, prior.selected.accumulator, prior.selected.calls, prior.selected_audit, prior.selected_dialect
            )
            if prior.selected_audit is not None and prior.selected_dialect is not None
            else prior.selected
        )
    audit: PortableDispatchAudit | None = None
    dialect: ToolContractDialect | None = None
    if type(data) is RestoredPreparedBatchData:
        audit, dialect = data.audit, data.dialect
        data = RestoredBatchData(data.batch, data.accumulator, data.calls)
    elif type(data) is not RestoredBatchData:
        raise TypeError("joint capture requires the actual complete coupled batch")
    cuts = () if prior is None else prior.cuts
    snapshot = recorder.snapshot_for_continuation(drained=drained)
    graph = ApplicationGraph(data, owner.effects, snapshot, cuts, audit, dialect)
    graph_json = encode_application_graph(graph, env_ref_names=env_ref_names)
    if entry is not None:
        entry = entry.model_copy(update={"graph_json": graph_json})
        admit_journal_values(entry, env_ref_names=env_ref_names)
        for key, live in owner._historical_p4_applications.items():
            indexes = [index for index, item in enumerate(entry.applications) if item.effect_key == key]
            if len(indexes) != 1:
                raise AuditIntegrityError("warm application is absent from its portable journal")
            cut = cuts[indexes[0]]
            if (
                live.data is not cut.data
                or live.recorder is not recorder
                or live.effects_before is not cut.effects_before
                or live.effects_after is not cut.effects_after
                or live.recorder_before != cut.recorder_before
                or live.recorder_after != cut.recorder_after
            ):
                raise AuditIntegrityError("portable journal replaced an original warm application owner")
    elif owner._historical_p4_applications:
        raise AuditIntegrityError("warm applications cannot be silently omitted from portable history")
    return (graph_json if entry is None else None), entry


def retain_portable_application_on_live_evidence(
    *,
    owner: TurnEvidence,
    recorder: BufferingRecorder,
    application: HistoricalP4Application,
    bundle: PortableP4ApplicationBundle,
    env_ref_names: frozenset[str],
) -> RestoredBatchData:
    """Reconcile a real already-applied warm projection, preserving exceptions.

    Root invokes this after its real atomic commit and existing live projection.
    The bundle supplies DATA, never authority to apply an unperformed effect.
    """
    if type(owner) is not TurnEvidence or type(recorder) is not BufferingRecorder or type(application) is not HistoricalP4Application:
        raise TypeError("warm portable retention requires original actual owners")
    if (
        owner._historical_p4_applications.get(application.effect_key) is not application
        or application.recorder is not recorder
        or owner.effects is not application.effects_after
    ):
        raise AuditIntegrityError("warm portable retention has no actual completed projection")
    prior = owner._portable_p4_graph
    cuts = () if prior is None else prior.cuts
    if len(cuts) + 1 != len(bundle.entry.applications):
        raise AuditIntegrityError("warm portable retention changed its complete prefix")
    record = bundle.entry.applications[-1]
    if record.effect_key != application.effect_key or record.checkpoint_id != application.checkpoint_id:
        raise AuditIntegrityError("warm portable retention changed its actual selected application")
    if (application.verification_failure is not None) != (
        record.verification in ("authored_state_mismatch", "checkpoint_decode_or_compare_failed")
    ):
        raise AuditIntegrityError("warm portable retention changed its original verification failure")
    cut = ApplicationGraphCut(
        application.data, application.effects_before, application.effects_after, application.recorder_before, application.recorder_after
    )
    after = RestoredBatchData(
        replace(
            application.data.batch,
            phase="complete",
            p4_receipt_json=record.result_json,
            p4_verification_failed=application.verification_failure is not None,
        ),
        application.data.accumulator,
        application.data.calls,
    )
    graph = ApplicationGraph(after, application.effects_after, application.recorder_after, (*cuts, cut))
    if encode_application_graph(graph, env_ref_names=env_ref_names) != bundle.entry.graph_json:
        raise AuditIntegrityError("warm and retained projections differ in values or actual alias partition")
    owner._portable_p4_graph = graph
    owner._portable_p4_entry = bundle.entry
    return after


def require_selected_applied_batch_chunk(chunk: PrivateChunk, *, evidence: TurnEvidenceSnapshot, env_ref_names: frozenset[str]) -> None:
    """Bind the separate selected chunk to the ONE evidence graph restoration.

    Callers use RestoredTurnEvidence.batch after this check. Decoding a second
    equal batch would lose its actual aliases to evidence and recorder roots.
    """
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.applied-p4-batch.v1":
        raise AuditIntegrityError("selected applied batch has another actual codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    graph = require_evidence_application_links(evidence, env_ref_names=env_ref_names)
    if (
        graph is None
        or evidence.application_entry is None
        or graph.selected.batch.phase != "complete"
        or evidence.application_entry.graph_json != chunk.canonical
    ):
        raise AuditIntegrityError("selected applied batch changed its complete shared evidence graph")


def _require_initial_frontier(effects: PersistedTurnEffects, logical: LogicalRequestBinding) -> None:
    if (
        effects.initial_state_id != logical.base_state_id
        or effects.latest_verified_state_id != logical.base_state_id
        or effects.latest_committed_state_id != logical.base_state_id
        or effects.batches
        or effects.committed_revision_ids
        or effects.net_changed_fields
        or effects.verification == "not_persisted"
        or canonical_json(effects.initial_state.to_dict()) != canonical_json(effects.latest_verified_state.to_dict())
    ):
        raise AuditIntegrityError("portable journal invented history before its first actual application")


def _require_completed_graph(graph: ApplicationGraph, record: PortableP4ApplicationRecord) -> None:
    if graph.selected.batch.phase != "complete":
        return
    from elspeth.web.composer.continuation_batch_data import _members

    original = graph.cuts[-1].data
    if graph.selected.accumulator is not original.accumulator or graph.selected.calls is not original.calls:
        raise AuditIntegrityError("completed application replaced its actual accumulator/call roots")
    before = _members(original.batch)[1]
    after = _members(graph.selected.batch)[1]
    for name in before:
        if name in ("phase", "p4_receipt_json", "p4_verification_failed"):
            continue
        left, right = before[name], after[name]
        if left is None or type(left) in (str, int, float, bool):
            if type(right) is not type(left) or left != right:
                raise AuditIntegrityError("completed application changed an original batch scalar")
        elif left is not right:
            raise AuditIntegrityError("completed application replaced an original batch object")
    if graph.selected.batch.p4_receipt_json != record.result_json or graph.selected.batch.p4_verification_failed != (
        record.verification in ("authored_state_mismatch", "checkpoint_decode_or_compare_failed")
    ):
        raise AuditIntegrityError("completed application changed its actual receipt/verification")


def _require_recorder_extension(before: RecorderSnapshot, after: RecorderSnapshot) -> None:
    for index, (old, new) in enumerate(zip(_recorder_pairs(before), _recorder_pairs(after), strict=True)):
        if len(new) < len(old):
            raise AuditIntegrityError("application journal discarded previous recorder history")
        if index == 4:
            if any(
                left.invocation_index != right.invocation_index or left.event is not right.event
                for left, right in zip(old, new, strict=False)
            ):
                raise AuditIntegrityError("application journal replaced a previous visible event")
        elif any(left is not right for left, right in zip(old, new, strict=False)):
            raise AuditIntegrityError("application journal replaced a previous recorder object")
    before_cursor, after_cursor = before.drained, after.drained
    if any(
        left > right
        for left, right in zip(
            (
                before_cursor.invocations,
                before_cursor.llm_calls,
                before_cursor.planner_attempts,
                before_cursor.withheld_replies,
                before_cursor.visible_tool_events,
                before_cursor.private_candidates,
                before_cursor.private_authorings,
            ),
            (
                after_cursor.invocations,
                after_cursor.llm_calls,
                after_cursor.planner_attempts,
                after_cursor.withheld_replies,
                after_cursor.visible_tool_events,
                after_cursor.private_candidates,
                after_cursor.private_authorings,
            ),
            strict=True,
        )
    ):
        raise AuditIntegrityError("application journal regressed a previously drained channel")

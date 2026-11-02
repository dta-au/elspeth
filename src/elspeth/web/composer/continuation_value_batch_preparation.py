"""Original V4 value-tool preparation on independent mutable owners.

Prepared values are DATA. Only the session batch owner may adopt them after
current source proof and admission of every tool/P4 prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from weakref import ReferenceType, WeakKeyDictionary, ref

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_continuation_snapshot, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_metadata_preparation import PreparedMemoryToolCall
from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Value, encode_prepared_p4
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_preview_dispatch import IssuedPreviewDispatch
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.required_work import RequiredPreviewProduced
    from elspeth.web.sessions.continuation_preview_result import PreviewReturned


class ValueBatchPreparationRefused(ValueError):
    """The original batch requires an effect outside this complete value tail."""


@dataclass(frozen=True, slots=True)
class PreparedValueBatchCall(PreparedMemoryToolCall):
    """One actual prepared call, retaining all original source/audit owners."""


@dataclass(frozen=True, slots=True)
class ValueBatchComputation:
    result: ToolResult
    source_chunks: tuple[PrivateChunk, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedValueBatchStep:
    index: int
    issued: CapturedContinuationRuntime | None
    completed: CapturedContinuationRuntime
    memory_completion: ProspectiveContinuationChunk | None
    source_chunks: tuple[PrivateChunk, ...] = ()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class PreparedValueBatch:
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    requested_calls: tuple[_AdmittedToolCall, ...]
    steps: tuple[PreparedValueBatchStep, ...]
    pending_capture: CapturedContinuationRuntime
    pending_batch: PrivateChunk
    prepared: PreparedP4Value


_ORIGINAL_BATCHES: WeakKeyDictionary[PreparedValueBatch, str] = WeakKeyDictionary()
_ISSUED_PREVIEW_BATCHES: WeakKeyDictionary[
    PreparedValueBatch, tuple[ReferenceType[IssuedPreviewDispatch], ReferenceType[PreviewReturned]]
] = WeakKeyDictionary()


def _captured_data(value: CapturedContinuationRuntime) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    for chunk in value.chunks:
        verify_private_chunk(chunk.reference, chunk.canonical)
    return encode_continuation_snapshot(value.stack), tuple(
        (chunk.reference.codec, chunk.reference.digest, chunk.canonical) for chunk in value.chunks
    )


def _prepared_batch_data(value: PreparedValueBatch) -> str:
    if (
        type(value) is not PreparedValueBatch
        or not value.requested_calls
        or len(value.steps) != len(value.requested_calls)
        or tuple(step.index for step in value.steps) != tuple(range(len(value.requested_calls)))
        or value.pending_capture.selected is not value.selected
        or any(step.completed.selected is not value.selected for step in value.steps)
        or any(step.issued is not None and step.issued.selected is not value.selected for step in value.steps)
    ):
        raise AuditIntegrityError("value batch changed its original complete ordered source")
    steps = []
    for step in value.steps:
        for chunk in step.source_chunks:
            verify_private_chunk(chunk.reference, chunk.canonical)
        memory = step.memory_completion
        steps.append(
            {
                "index": step.index,
                "issued": None if step.issued is None else _captured_data(step.issued),
                "completed": _captured_data(step.completed),
                "memory": None if memory is None else (memory.key, memory.codec, memory.payload.decode("utf-8")),
                "sources": tuple((chunk.reference.codec, chunk.reference.digest, chunk.canonical) for chunk in step.source_chunks),
            }
        )
    verify_private_chunk(value.pending_batch.reference, value.pending_batch.canonical)
    for chunk in value.selected.chunks:
        verify_private_chunk(chunk.reference, chunk.canonical)
    running = value.inventory.running
    return canonical_json(
        {
            "owners": (
                str(id(value.selected)),
                str(id(value.inventory)),
                str(id(running)),
                str(id(running.session_operation_context)),
                str(running.claim.session_id),
                running.claim.operation_id,
                running.claim.segment_sequence,
                running.claim.ownership_generation,
                running.claim.attempt,
            ),
            "inventory_names": tuple(sorted(value.inventory.names)),
            "selected": (value.selected.checkpoint_id, encode_continuation_snapshot(value.selected.stack)),
            "original_chunks": tuple((chunk.reference.codec, chunk.reference.digest, chunk.canonical) for chunk in value.selected.chunks),
            "calls": tuple((call.id, call.function.name, call.function.arguments) for call in value.requested_calls),
            "steps": steps,
            "pending": _captured_data(value.pending_capture),
            "pending_batch": (value.pending_batch.reference.digest, value.pending_batch.canonical),
            "p4": encode_prepared_p4(value.prepared, env_ref_names=value.inventory.names),
        }
    )


def require_prepared_value_batch(value: PreparedValueBatch) -> None:
    """Reject copied/constructed or changed DATA; SQL admission remains separate."""
    if type(value) is not PreparedValueBatch:
        raise TypeError("value batch requires its actual original preparation")
    original = _ORIGINAL_BATCHES.get(value)
    if original is None or original != _prepared_batch_data(value):
        raise AuditIntegrityError("value batch lacks its original complete preparation")


async def prepare_value_batch(
    owner: ContinuationLivePhases,
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> PreparedValueBatch:
    """Run the original detached producers once; issue no SQL/tool authority."""
    from elspeth.web.composer.continuation_value_batch_builder import build_prepared_value_batch

    value = await build_prepared_value_batch(owner, runtime=runtime, selected=selected, inventory=inventory)
    _ORIGINAL_BATCHES[value] = _prepared_batch_data(value)
    return value


def prepare_issued_preview_batch(
    owner: ContinuationLivePhases,
    *,
    prepared: PreparedValueBatchCall,
    produced: RequiredPreviewProduced,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> PreparedValueBatch:
    """Seal only the actual issued preview's newly built full original tail."""
    from elspeth.web.composer.continuation_preview_dispatch import require_preview_produced
    from elspeth.web.composer.continuation_value_batch_builder import build_prepared_issued_preview_batch
    from elspeth.web.required_work import RequiredPreviewProduced
    from elspeth.web.sessions.continuation_preview_result import CompletePreviewReturn

    if type(produced) is not RequiredPreviewProduced or type(produced.returned) is not CompletePreviewReturn:
        raise AuditIntegrityError("issued preview tail requires its actual complete original return")
    require_preview_produced(produced.permit, produced.returned)
    value = build_prepared_issued_preview_batch(
        owner, prepared=prepared, permit=produced.permit, runtime=runtime, selected=selected, inventory=inventory
    )
    if len(value.steps) != 1:
        raise AuditIntegrityError("issued preview tail cannot replace another batch prefix")
    value = replace(value, steps=(replace(value.steps[0], source_chunks=(produced.returned.chunk,)),))
    _ORIGINAL_BATCHES[value] = _prepared_batch_data(value)
    _ISSUED_PREVIEW_BATCHES[value] = (ref(produced.permit), ref(produced.returned))
    return value


def require_prepared_issued_preview_batch(value: PreparedValueBatch, produced: RequiredPreviewProduced) -> None:
    from elspeth.web.composer.continuation_preview_dispatch import require_preview_produced
    from elspeth.web.required_work import RequiredPreviewProduced

    require_prepared_value_batch(value)
    if type(produced) is not RequiredPreviewProduced:
        raise AuditIntegrityError("issued preview tail replaced its actual produced carrier")
    actual = _ISSUED_PREVIEW_BATCHES.get(value)
    if actual is None or actual[0]() is not produced.permit or actual[1]() is not produced.returned:
        raise AuditIntegrityError("issued preview tail lacks its original returned-source seal")
    require_preview_produced(produced.permit, produced.returned)

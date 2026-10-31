"""Closed first-cut assembly through real current reads and separate handoffs.

Construction DATA never grants a dispatch or renewed allowance. The caller's
original retention clock includes both worker handoffs and the final bind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.composer_progress import ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.continuation_current_evidence import _finish_evidence_projection
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence_graph import (
    RestoredTurnEvidence,
    _begin_current_evidence,
    _finish_current_evidence,
)
from elspeth.web.composer.continuation_evidence_restore import _require_current_reader_service
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, _registered_read, _restore_live, _work_owners
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.continuation_first_runtime import FirstRuntimeSources, read_first_runtime_sources_on_connection
from elspeth.web.sessions.locking import locked_session_transaction

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl


@dataclass(frozen=True, slots=True)
class FirstRuntimeGraph:
    source: FirstRuntimeSources
    graph: RestoredTurnEvidence
    materialization: CurrentContinuationMessageProducers


def _require_same_sources(before: FirstRuntimeSources, after: FirstRuntimeSources) -> None:
    # Timing is intentionally observed freshly but never replaces the original
    # monotonic/deadline anchor. Every retained DATA/policy field is compared.
    if (
        not before.initial.same_original_source(after.initial)
        or before.inputs != after.inputs
        or before.runtime.selected != after.runtime.selected
        or before.runtime.bootstrap != after.runtime.bootstrap
        or before.runtime.original != after.runtime.original
        or before.runtime.iteration != after.runtime.iteration
        or before.runtime.evidence != after.runtime.evidence
        or before.runtime.policy != after.runtime.policy
    ):
        raise AuditIntegrityError("first runtime current source changed across actual reconstruction callbacks")


def _read_first_sources(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
) -> FirstRuntimeSources:
    _require_current_reader_service(service, authority)
    running = _work_owners(authority, inventory, work)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        return read_first_runtime_sources_on_connection(conn, service, running, selected=selected, inventory=inventory)


def _restore_first_graph(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    expected: FirstRuntimeSources,
) -> FirstRuntimeGraph:
    _require_current_reader_service(service, authority)
    running = _work_owners(authority, inventory, work)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        before = read_first_runtime_sources_on_connection(conn, service, running, selected=selected, inventory=inventory)
        _require_same_sources(expected, before)
    # Nothing from the ended SQL window is a live admission capability.
    # These are provisional values; all DATA are independently reread below.
    inputs, snapshot = before.inputs, before.runtime.evidence
    producer = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs)
    producer.require_context(
        session_id=selected.stack.logical.session_id,
        context=running.session_operation_context,
        actor_user_id=inputs.bootstrap.actor_user_id,
    )
    evidence, initial, latest = _begin_current_evidence(snapshot, context=running.session_operation_context)
    history = AdvisorTranscriptOwner._continuation_history(
        snapshot.transcript,
        session_id=snapshot.session_id,
        actor_user_id=snapshot.actor_user_id,
        request_id=snapshot.request_id,
        request=snapshot.transcript.request,
    )
    # Actual initial/policy/schema/content-scope callbacks remain outside SQL.
    shelf = producer._rebuild_shelf(snapshot.transcript, inputs=inputs)
    evidence.transcript, messages = AdvisorTranscriptOwner._from_continuation_shelf(
        snapshot.transcript, request=snapshot.transcript.request, history=history, shelf=shelf
    )
    reader = CurrentTranscriptBlobReader.from_service(
        service, session_id=str(running.claim.session_id), context=running.session_operation_context
    )
    graph = _finish_current_evidence(
        inputs.admitted_evidence,
        context=running.session_operation_context,
        owner=evidence,
        initial=initial,
        latest=latest,
        messages=messages,
        reader=reader,
        env_ref_names=inventory.names,
    )
    # This fresh complete SQL window is AFTER shelf/graph callbacks and BEFORE
    # any live callback. No earlier currentness/decode survives as authority.
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        after = read_first_runtime_sources_on_connection(conn, service, running, selected=selected, inventory=inventory)
        _require_same_sources(before, after)
    return FirstRuntimeGraph(expected, graph, producer)


async def _registered_graph(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    expected: FirstRuntimeSources,
) -> FirstRuntimeGraph:
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            _restore_first_graph,
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            work=work,
            expected=expected,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    # The original evidence projection owns nominality, error grouping and
    # identity-deduplicated deferred cancellation before any live submission.
    return _finish_evidence_projection(work, sql, projection, outcome, expected=FirstRuntimeGraph)


async def restore_first_continuation_runtime(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import ComposerServiceImpl

    _work_owners(authority, inventory, required_work)
    if (
        type(service) is not ComposerServiceImpl
        or type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not service._require_sessions_service()
    ):
        raise AuditIntegrityError("runtime lost actual current service/provider-work custody")
    monotonic_before_read = time.monotonic()
    source = await _registered_read(
        required_work,
        FirstRuntimeSources,
        lambda: _read_first_sources(service=service, authority=authority, selected=selected, inventory=inventory, work=required_work),
    )
    # Real pre-callback finish-once/projection handoff: cancellation retained
    # during source/loading work is raised BEFORE the graph worker can start.
    # The graph worker performs a NEW current read after this await; source is
    # comparison DATA only, never cross-window currentness or a renewed clock.
    restored = await _registered_graph(
        service=service, authority=authority, selected=selected, inventory=inventory, work=required_work, expected=source
    )
    # REQUIRED cancellation/error/projection handoff: the actual existing
    # finish-once owner raises originals BEFORE this second worker is submitted.
    # Never combine these operations in a single noncancellable worker.
    return await _registered_read(
        required_work,
        RestoredContinuationRuntime,
        lambda: _restore_live(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            work=required_work,
            provider_owner=provider_owner,
            source=restored.source.runtime,
            graph=restored.graph,
            materialization=restored.materialization,
            monotonic_before_read=monotonic_before_read,
            progress=progress,
        ),
    )

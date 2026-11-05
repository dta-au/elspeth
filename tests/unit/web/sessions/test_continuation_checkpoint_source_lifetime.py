"""Real checkpoint owners release their issued graphs without registry teardown."""

from __future__ import annotations

import asyncio
import gc
from copy import copy
from typing import Literal
from weakref import ReferenceType, WeakKeyDictionary, ref

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_checkpoint_early_adoption as adoption
from elspeth.web.composer import continuation_checkpoint_early_physical as early_physical
from elspeth.web.composer import continuation_checkpoint_end_complete_clean as end_clean
from elspeth.web.composer import continuation_checkpoint_end_complete_review as end_review
from elspeth.web.composer import continuation_checkpoint_end_complete_terminal as end_terminal
from elspeth.web.composer import continuation_checkpoint_end_first_return as end_first
from elspeth.web.composer import continuation_checkpoint_end_input as end_input
from elspeth.web.composer import continuation_checkpoint_end_locals as end_locals
from elspeth.web.composer import continuation_checkpoint_end_physical as end_physical
from elspeth.web.composer import continuation_checkpoint_end_usable as end_usable
from elspeth.web.composer import continuation_checkpoint_observation as observations
from elspeth.web.composer import continuation_checkpoint_projection as early_projection
from elspeth.web.composer.continuation_checkpoint_early_live import EarlyCheckpointLiveAdvanced
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointCompleteReview
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import continuation_checkpoint_early_owed_overflow as early_completion
from elspeth.web.sessions import continuation_checkpoint_early_return as early_return
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_semantic_review_result import read_original_semantic_review_result_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture
from tests.unit.web.sessions.test_continuation_checkpoint_early_owed_overflow import _original_h

operation_store = shared_fixture.operation_store
type Probes = tuple[tuple[str, ReferenceType[object]], ...]


def _probes(label: str, key: object, record: object) -> Probes:
    return ((label, ref(key)), (f"{label}.record", ref(record)))


def _early_probes(source, prepared, returned, projected) -> Probes:
    early_physical.require_original_checkpoint_early_physical_source(source)
    observations.require_original_v4_checkpoint_returned_response(
        returned, attempt_id=returned.attempt.attempt_id, request_sha256=prepared.request_sha256
    )
    admission = early_physical.checkpoint_early_physical_custody(source).continuation_checkpoint_v4_admission
    assert admission is not None
    early_projection.require_original_early_checkpoint_projection(projected, prepared=prepared, admission=admission, returned=returned)
    source_record = early_physical._original_source(source)
    returned_record = observations._original_return(returned)
    projected_record = early_projection._issued[projected]()
    assert source_record is source._issued_record
    assert returned_record is returned._issued_record
    assert projected_record is not None and projected_record is projected._issued_record
    return (
        *_probes("early.source", source, source_record),
        *_probes("early.returned", returned, returned_record),
        *_probes("early.projected", projected, projected_record),
    )


async def _released_collection(probes: Probes) -> None:
    # Let normally completed callback/task owners leave the ready queue first.
    await asyncio.sleep(0)
    gc.collect()
    alive = tuple(label for label, observed in probes if observed() is not None)
    assert alive == ()


async def _run_early(
    store,
    tmp_path,
    *,
    mode: Literal["adopt", "stop", "retain"],
    keeper: list[object] | None = None,
    keep: Literal["source", "custody"] = "source",
    shadow: WeakKeyDictionary | None = None,
) -> Probes:
    with pytest.MonkeyPatch.context() as patch:
        async with _original_h(store, tmp_path, patch) as (service, source, prepared, owed, returned, projected, sdk):
            probes = _early_probes(source, prepared, returned, projected)
            phases = source.phase_owner.phases
            budget = phases.provider_owner.cost_budget
            assert budget is not None
            before = budget.snapshot()
            result = await early_completion.complete_early_checkpoint_or_overflow(
                service=service, prepared=prepared, owed=owed, returned=returned, projected=projected
            )
            assert type(result) is EarlyCheckpointLiveAdvanced and result.source is source and result.returned is returned
            assert len(budget.snapshot().charges) == len(before.charges) + 1
            assert sum(charge.call_id == returned.audit_call.call_id for charge in budget.snapshot().charges) == 1
            settled = adoption._settled[source]()
            assert settled is not None and settled is source._settled_record
            probes = (*probes, ("early.settled.record", ref(settled)))
            if shadow is not None:
                shadow[source] = settled
            if mode == "adopt":
                await adoption.adopt_committed_early(source, advanced=result.advanced, returned=returned)
                assert source not in adoption._settled and source._settled_record is None
            elif mode == "stop":
                phases.observe_stop()
                with pytest.raises(asyncio.CancelledError, match="adoption stopped"):
                    await adoption.adopt_committed_early(source, advanced=result.advanced, returned=returned)
            if keeper is not None:
                keeper.append(source if keep == "source" else early_physical.checkpoint_early_physical_custody(source))
            with locked_session_transaction(store[0], str(prepared.running.claim.session_id)) as conn:
                assert (
                    load_snapshot(conn, session_id=str(prepared.running.claim.session_id), operation_id=prepared.running.claim.operation_id)
                    == result.advanced.completed
                )
                assert (
                    conn.execute(select(holds.c.state).where(holds.c.provider_attempt_id == returned.audit_call.call_id)).scalar_one()
                    == "consumed"
                )
                assert (
                    conn.execute(select(attempts.c.settled_at).where(attempts.c.attempt_id == returned.audit_call.call_id)).scalar_one()
                    is not None
                )
            assert sdk == [1]
    return probes


def _require_held_early(probes: Probes) -> None:
    source = dict(probes)["early.source"]()
    assert type(source) is early_physical.CheckpointEarlyPhysicalSource
    early_physical.require_original_checkpoint_early_physical_source(source)
    assert early_physical._original_source(source) is source._issued_record
    assert early_physical.checkpoint_early_physical_custody(source) is source._issued_record.custody
    copied = copy(source)
    assert copied._issued_record is source._issued_record
    with pytest.raises(AuditIntegrityError):
        early_physical.require_original_checkpoint_early_physical_source(copied)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("adopt", "stop"))
async def test_early_paid_graph_releases_after_actual_owner_scope(operation_store, tmp_path, mode):
    probes = await _run_early(operation_store, tmp_path, mode=mode)
    await _released_collection(probes)


@pytest.mark.asyncio
@pytest.mark.parametrize("keep", ("source", "custody"))
async def test_external_original_owner_keeps_record_functional_until_release(operation_store, tmp_path, keep):
    keeper = []
    probes = await _run_early(operation_store, tmp_path, mode="retain", keeper=keeper, keep=keep)
    await asyncio.sleep(0)
    gc.collect()
    _require_held_early(probes)
    keeper.clear()
    await _released_collection(probes)


@pytest.mark.asyncio
async def test_shadow_old_strong_registry_prevents_collection_until_only_shadow_releases(operation_store, tmp_path):
    shadow = WeakKeyDictionary()
    probes = await _run_early(operation_store, tmp_path, mode="stop", shadow=shadow)
    await asyncio.sleep(0)
    gc.collect()
    assert dict(probes)["early.source"]() is not None
    assert dict(probes)["early.settled.record"]() is not None
    shadow.clear()
    await _released_collection(probes)


async def _run_early_sql_failure(store, tmp_path, keeper: list[object]) -> Probes:
    with pytest.MonkeyPatch.context() as patch:
        async with _original_h(store, tmp_path, patch) as (service, source, prepared, owed, returned, projected, sdk):
            probes = _early_probes(source, prepared, returned, projected)
            original = RuntimeError("original registered EARLY projection write failure")
            actual_append = early_return.append_complete_snapshot

            def refuse_after_actual_append(*args, **kwargs):
                actual_append(*args, **kwargs)
                raise original

            patch.setattr(early_return, "append_complete_snapshot", refuse_after_actual_append)
            with pytest.raises(RuntimeError) as raised:
                await early_completion.complete_early_checkpoint_or_overflow(
                    service=service, prepared=prepared, owed=owed, returned=returned, projected=projected
                )
            assert raised.value is original
            keeper.append(original)
            with locked_session_transaction(store[0], str(prepared.running.claim.session_id)) as conn:
                assert (
                    load_snapshot(conn, session_id=str(prepared.running.claim.session_id), operation_id=prepared.running.claim.operation_id)
                    == owed.owed_selected
                )
                assert (
                    conn.execute(select(holds.c.state).where(holds.c.provider_attempt_id == returned.audit_call.call_id)).scalar_one()
                    == "active"
                )
                assert (
                    conn.execute(select(attempts.c.settled_at).where(attempts.c.attempt_id == returned.audit_call.call_id)).scalar_one()
                    is None
                )
            assert sdk == [1]
    return probes


@pytest.mark.asyncio
async def test_original_projection_sql_error_keeps_then_releases_actual_source(operation_store, tmp_path):
    keeper = []
    probes = await _run_early_sql_failure(operation_store, tmp_path, keeper)
    await asyncio.sleep(0)
    gc.collect()
    assert keeper[0].args == ("original registered EARLY projection write failure",)
    _require_held_early(probes)
    keeper.clear()
    await _released_collection(probes)


async def _run_end(store, tmp_path, *, blocked: bool) -> Probes:
    probes = []
    with pytest.MonkeyPatch.context() as patch:
        policy = continuation_policy_v4_from_settings(
            continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
        )
        assert policy is not None
        authority = ComposerAsyncOperationAuthority(
            store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
        )
        current_store = (store[0], store[1], authority, store[3], store[4])
        patch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
        actual_prepare = end_first.prepare_checkpoint_end_physical_source
        actual_project = end_usable.project_end_complete_review

        async def observe_source(**kwargs):
            source = await actual_prepare(**kwargs)
            end_physical.require_original_checkpoint_end_physical_source(source)
            source_record = end_physical._original_source(source)
            gate_record = end_locals._original_gate_locals(source.gate_locals)
            input_record = end_input._original_input(source.end_input)
            assert source_record is source._issued_record
            assert gate_record is source.gate_locals._issued_record
            assert input_record is source.end_input._issued_record
            probes.extend(
                (
                    *_probes("end.source", source, source_record),
                    *_probes("end.gate", source.gate_locals, gate_record),
                    *_probes("end.input", source.end_input, input_record),
                )
            )
            return source

        async def observe_projection(**kwargs):
            projected = await actual_project(**kwargs)
            assert type(projected) is end_review.ProjectedEndCompleteReview
            end_review.require_original_end_complete_review(
                projected, physical=kwargs["physical"], owed=kwargs["owed"], selected=kwargs["selected"], returned=kwargs["returned"]
            )
            record = end_review._issued[projected]()
            assert record is not None and record is projected._issued_record
            probes.extend(_probes("end.projected", projected, record))
            returned = kwargs["returned"]
            returned_record = observations._original_return(returned)
            assert returned_record is returned._issued_record
            observations.require_original_v4_checkpoint_returned_response(
                returned, attempt_id=returned.attempt.attempt_id, request_sha256=returned_record.request_sha256
            )
            probes.extend(_probes("end.returned", returned, returned_record))
            terminal = projected.terminal
            if blocked:
                assert type(terminal) is end_terminal.ProjectedEndCompleteTerminal
                end_terminal.require_original_end_complete_terminal(terminal, physical=kwargs["physical"], returned=returned)
                terminal_record = end_terminal._issued[terminal]()
            else:
                assert type(terminal) is end_clean.ProjectedEndCompleteClean
                end_clean.require_original_end_complete_clean(terminal, physical=kwargs["physical"], returned=returned)
                terminal_record = end_clean._issued[terminal]()
            assert terminal_record is not None and terminal_record is terminal._issued_record
            probes.extend(_probes("end.terminal", terminal, terminal_record))
            return projected

        patch.setattr(end_first, "prepare_checkpoint_end_physical_source", observe_source)
        patch.setattr(end_usable, "project_end_complete_review", observe_projection)
        if blocked:
            actual_settings = semantic_fixture._required_guard_settings

            def last_pass_settings(tmp_path, monkeypatch):
                settings, requests = actual_settings(tmp_path, monkeypatch)
                return settings.model_copy(update={"composer_advisor_checkpoint_max_passes": 1}), requests

            patch.setattr(semantic_fixture, "_required_guard_settings", last_pass_settings)
            flagged = shared_fixture._end_admit()
            flagged.update(verdict="FLAGGED", findings="The candidate does not meet the request.", publication_verdict="WITHHOLD")
            patch.setattr(shared_fixture, "_end_admit", lambda: flagged)
        async with shared_fixture.actual_shared_end_case(current_store, tmp_path, patch, timed_read=True) as case:
            assert type(case["execution"]) is EndCheckpointCompleteReview
            review = case["execution"].review
            assert review.outcome == ("blocked" if blocked else "clean")
            assert review.original_source == case["admission"].paid.source
            assert tuple(name for name, _ in case["sdk"]) == ("semantic", "end")
            running = case["running"]
            with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
                assert (
                    read_original_semantic_review_result_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        review_effect_key=review.owner_effect_key,
                    )
                    == review
                )
            assert tuple(label for label, _observed in probes) == (
                "end.source",
                "end.source.record",
                "end.gate",
                "end.gate.record",
                "end.input",
                "end.input.record",
                "end.projected",
                "end.projected.record",
                "end.returned",
                "end.returned.record",
                "end.terminal",
                "end.terminal.record",
            )
    return tuple(probes)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", (False, True), ids=("clean", "blocked"))
async def test_actual_end_review_releases_issued_source_and_projection_graph(operation_store, tmp_path, blocked):
    probes = await _run_end(operation_store, tmp_path, blocked=blocked)
    await _released_collection(probes)

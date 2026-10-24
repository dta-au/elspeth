"""Actual same-connection selection of the closed END/Advisor source cohort."""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import delete, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EndFrame
from elspeth.web.composer.continuation_advisor import (
    decode_continuation_advisor_invocation,
    encode_continuation_advisor_invocation,
    make_continuation_manual_advisor_invocation,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_end_values import decode_end_candidate, encode_end_candidate
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_continuation_nested_sources import manual_arguments, nested_source_values
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_prompt_producers import producer_case
from tests.unit.web.sessions.test_continuation_selection import rows

operation_store = test_composer_operation_authority.operation_store


async def nested_case(store, *, conflict=False):
    engine, _repository, _authority, sessions, sid = store
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(store)
    running, before = kwargs["inventory"].running, kwargs["selected"]
    (compose,) = before.stack.frames
    assert type(compose) is ComposeFrame
    candidate, advisor, leaves = nested_source_values(
        state=compose.state, evidence=compose.evidence, transcript=compose.transcript, iteration=compose.iteration
    )
    end = encode_end_candidate(candidate)
    if conflict:
        advisor = advisor.model_copy(update={"state": advisor.state.model_copy(update={"byte_length": advisor.state.byte_length + 1})})
    review = encode_continuation_advisor_invocation(advisor)
    root = before.stack.model_copy(
        update={
            "frames": (
                compose,
                EndFrame(
                    phase="review_needed", state=compose.state, candidate=end.reference, publication_binding=candidate.publication_binding
                ),
                AdvisorFrame(phase="input_ready", review=review.reference, response=None, coverage_binding=advisor.coverage_binding),
            )
        }
    )

    def persist():
        with locked_session_transaction(engine, str(sid)) as conn:
            for value in (end, review, *leaves):
                put_chunk(
                    conn,
                    session_id=str(sid),
                    operation_id=running.claim.operation_id,
                    key=private_chunk_key(value),
                    payload=value.canonical.encode("utf-8"),
                    codec=value.reference.codec,
                )
            return append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=before.checkpoint_id,
                expected_cursor=before.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root),
                event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"retained End and Advisor sources"}'),),
                handoff=False,
            )

    stored = await asyncio.to_thread(persist)
    # First occurrence is independently enumerated here: all existing direct
    # Compose inputs, then the two direct source chunks, then hidden END
    # response sources and Advisor frontier. Shared state/evidence refs recur
    # only inside the actual source values and do not duplicate chunk owners.
    expected = (*before.chunks, end, review, *leaves)
    return running, stored, expected, candidate, advisor, sessions


async def select_current(store, running, stored):
    work = inventory_work(running)
    try:
        selected = await load_selected_continuation(authority=store[2], running=running, selected=stored, required_work=work)
    finally:
        work.coordinator.assert_completed()
    return selected


@pytest.mark.asyncio
async def test_complete_nested_sql_cohort_keeps_order_alias_refs_and_current_inventory(operation_store):
    running, stored, expected, candidate, advisor, sessions = await nested_case(operation_store)
    try:
        before = rows(operation_store)
        selected = await select_current(operation_store, running, stored)
        assert selected.chunks == expected and rows(operation_store) == before
        compose, end, child = selected.stack.frames
        loaded = {value.reference: value for value in selected.chunks}
        assert decode_end_candidate(loaded[end.candidate]) == candidate
        assert decode_continuation_advisor_invocation(loaded[child.review]) == advisor
        assert loaded[candidate.state] is loaded[advisor.state] is loaded[compose.state]
        assert sum(value.reference == compose.state for value in selected.chunks) == 1
        assert candidate.publication_source.response in loaded and candidate.semantic_source.response in loaded
        assert advisor.premise.frontier in loaded
        manual = make_continuation_manual_advisor_invocation(
            arguments=manual_arguments(), tool_call_id="actual-manual-source", stack=selected.stack
        )
        assert (manual.state, manual.evidence, manual.transcript, manual.iteration) == (
            compose.state,
            compose.evidence,
            compose.transcript,
            compose.iteration,
        )
        assert decode_continuation_advisor_invocation(encode_continuation_advisor_invocation(manual)) == manual
        work = inventory_work(running)
        inventory = await materialize_continuation_env_inventory(
            advisor=inventory_advisor(sessions), authority=operation_store[2], selected=selected, required_work=work
        )
        work.coordinator.assert_completed()
        assert inventory.running is running
        assert inventory.selection.snapshot_json == encode_continuation_snapshot(selected.stack)
        assert rows(operation_store) == before
        assert (await select_current(operation_store, running, stored)).chunks == expected
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("missing", "reordered", "extra", "duplicate"))
async def test_nominal_selected_owner_refuses_incomplete_or_extra_closure_after_sql_positive(operation_store, change):
    running, stored, _expected, _candidate, _advisor, _sessions = await nested_case(operation_store)
    try:
        selected = await select_current(operation_store, running, stored)
        values = selected.chunks
        if change == "missing":
            values = values[:-1]
        elif change == "reordered":
            values = (*values[:-2], values[-1], values[-2])
        elif change == "extra":
            from elspeth.web.composer.continuation_codec import encode_private_chunk

            values = (*values, encode_private_chunk(codec="composer.messages.v1", canonical='{"unselected":"extra"}'))
        else:
            values = (*values, values[-1])
        with pytest.raises(AuditIntegrityError):
            SelectedContinuationState(selected.checkpoint_id, selected.stack, values)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("primary", "publication", "semantic", "frontier"))
@pytest.mark.parametrize("change", ("missing", "coherent_utf8", "coherent_bytes"))
async def test_nested_sql_bytes_cannot_be_replaced_or_omitted_after_positive(operation_store, target, change):
    engine, _repository, _authority, _sessions, sid = operation_store
    running, stored, _expected, candidate, advisor, _service = await nested_case(operation_store)
    try:
        await select_current(operation_store, running, stored)
        refs = {
            "primary": candidate.response,
            "publication": candidate.publication_source.response,
            "semantic": candidate.semantic_source.response,
            "frontier": advisor.premise.frontier,
        }
        reference = refs[target]
        predicate = (
            (chunks.c.session_id == str(sid))
            & (chunks.c.operation_id == running.claim.operation_id)
            & (chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}")
        )
        with engine.begin() as conn:
            if change == "missing":
                conn.execute(delete(chunks).where(predicate))
            else:
                payload = b"\xff" if change == "coherent_utf8" else b"{}"
                conn.execute(
                    update(chunks).where(predicate).values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
        before = rows(operation_store)
        with pytest.raises(AuditIntegrityError) as caught:
            await select_current(operation_store, running, stored)
        if change == "coherent_utf8":
            assert type(caught.value.__cause__) is UnicodeDecodeError
        assert rows(operation_store) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_conflicting_nested_byte_bound_refuses_under_real_sql_owner(operation_store):
    running, stored, _expected, _candidate, _advisor, _sessions = await nested_case(operation_store, conflict=True)
    try:
        before = rows(operation_store)
        with pytest.raises(AuditIntegrityError, match="nested private reference changed its exact retained byte bound"):
            await select_current(operation_store, running, stored)
        assert rows(operation_store) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_nested_selection_keeps_original_head_and_current_claim_refusals(operation_store):
    running, stored, _expected, _candidate, _advisor, _sessions = await nested_case(operation_store)
    try:
        await select_current(operation_store, running, stored)
        before = rows(operation_store)
        with pytest.raises(ContinuationCASConflict):
            await select_current(operation_store, running, replace(stored, cursor=stored.cursor + 1))
        foreign = replace(running, claim=replace(running.claim, claim_token="other-claim"))
        with pytest.raises(AuditIntegrityError):
            await asyncio.to_thread(operation_store[2].load_continuation_chunks, foreign, stored)
        assert rows(operation_store) == before
        assert decode_continuation_snapshot(stored.payload_json).cursor == stored.cursor
    finally:
        operation_store[1].release(running.session_operation_context)

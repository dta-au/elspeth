"""Real checkpoint capture keeps transactions, worker custody and DATA distinct."""

from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy import event, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_checkpoint_end_complete_review as end_review
from elspeth.web.composer import continuation_checkpoint_end_detached as detached_boundary
from elspeth.web.composer import continuation_checkpoint_projection as early_projection
from elspeth.web.composer.continuation_capture_overflow import ContinuationMandatoryCaptureOverflow
from elspeth.web.sessions import continuation_checkpoint_paid as paid_source
from elspeth.web.sessions import locking
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from tests.unit.web.sessions import test_continuation_checkpoint_early_owed_overflow as early_fixture
from tests.unit.web.sessions import test_continuation_checkpoint_early_projected_overflow as overflow_fixture
from tests.unit.web.sessions import test_continuation_checkpoint_source_lifetime as lifetime_fixture

operation_store = early_fixture.operation_store


async def _until_reached(task, reached):
    observation = asyncio.create_task(reached.wait())
    try:
        done, _pending = await asyncio.wait((task, observation), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            pytest.fail("actual checkpoint completed before the observed capture boundary")
        assert observation in done and reached.is_set()
    finally:
        observation.cancel()
        await asyncio.gather(observation, return_exceptions=True)


@pytest.mark.asyncio
async def test_original_early_capture_uses_one_worker_and_unlocked_live_cas(operation_store, tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    projection_entered = []
    observed = []
    connections = {}
    capture_ready, writer_entered, cas_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    allow_capture, release_writer = threading.Event(), threading.Event()
    actual_project = early_projection._project_early_checkpoint_result
    actual_pre, actual_post = detached_boundary._capture_owners, early_projection._capture_owners
    actual_cas = operation_store[1].compare_and_swap
    capture_phase = []

    def begin(conn):
        connections.setdefault(threading.get_ident(), set()).add(conn)

    def finish(conn):
        connections.setdefault(threading.get_ident(), set()).discard(conn)

    def project(**kwargs):
        projection_entered.append(kwargs)
        return actual_project(**kwargs)

    def observe(kind, actual, runtime, **kwargs):
        if projection_entered:
            thread = threading.get_ident()
            deadline = locking._CONTINUATION_SQL_DEADLINE.get()
            assert thread != loop_thread
            assert not connections.get(thread), "effectful capture still owns an outer SQL transaction"
            assert deadline is not None and deadline[0] is runtime.bindings.required_work.coordinator.sql_deadline_owner
            assert deadline[1] is False  # A registered read is not paid settlement.
            observed.append((kind, thread))
            capture_phase.append(kind)
            if kind == "post":
                loop.call_soon_threadsafe(capture_ready.set)
                allow_capture.wait()
            try:
                return actual(runtime, **kwargs)
            finally:
                capture_phase.pop()
        return actual(runtime, **kwargs)

    def compare(context):
        if capture_phase == ["post"]:
            loop.call_soon_threadsafe(cas_entered.set)
        return actual_cas(context)

    monkeypatch.setattr(early_projection, "_project_early_checkpoint_result", project)
    monkeypatch.setattr(detached_boundary, "_capture_owners", lambda runtime, **kw: observe("pre", actual_pre, runtime, **kw))
    monkeypatch.setattr(early_projection, "_capture_owners", lambda runtime, **kw: observe("post", actual_post, runtime, **kw))
    monkeypatch.setattr(operation_store[1], "compare_and_swap", compare)
    event.listen(operation_store[0], "begin", begin)
    event.listen(operation_store[0], "commit", finish)
    event.listen(operation_store[0], "rollback", finish)

    async def run():
        async with early_fixture._original_h(operation_store, tmp_path, monkeypatch) as case:
            assert case[5].captured is not None and case[6] == [1]

    def hold_writer():
        prepared = projection_entered[0]["prepared"]
        with locking.locked_session_transaction(operation_store[0], str(prepared.running.claim.session_id)):
            loop.call_soon_threadsafe(writer_entered.set)
            release_writer.wait()

    task = asyncio.create_task(run())
    writer = None
    try:
        await _until_reached(task, capture_ready)
        writer = asyncio.create_task(asyncio.to_thread(hold_writer))
        await _until_reached(writer, writer_entered)
        allow_capture.set()
        await _until_reached(task, cas_entered)
        # The real CAS is waiting on the competing writer, while this loop can
        # still run unrelated work. No clock or SQLite timeout was changed.
        heartbeat = []
        loop.call_soon(heartbeat.append, "responsive")
        await asyncio.sleep(0)
        assert heartbeat == ["responsive"] and not task.done()
        release_writer.set()
        await writer
        await task
        assert [kind for kind, _thread in observed] == ["pre", "post"]
        assert observed[0][1] == observed[1][1]
    finally:
        allow_capture.set()
        release_writer.set()
        await asyncio.gather(*(owned for owned in (task, writer) if owned is not None), return_exceptions=True)
        event.remove(operation_store[0], "begin", begin)
        event.remove(operation_store[0], "commit", finish)
        event.remove(operation_store[0], "rollback", finish)


@pytest.mark.asyncio
async def test_original_early_projector_rejects_mutation_after_pure_getter(operation_store, tmp_path, monkeypatch):
    actual = early_projection._project_early_checkpoint_result
    entered = []

    def project(**kwargs):
        entered.append(kwargs["physical_source"])
        kwargs["detached"].live.state.name = "Changed between original getter and projection"
        return actual(**kwargs)

    monkeypatch.setattr(early_projection, "_project_early_checkpoint_result", project)
    with pytest.raises(AuditIntegrityError, match="complete pre-admission capture"):
        async with early_fixture._original_h(operation_store, tmp_path, monkeypatch):
            pytest.fail("changed original detached graph reached a successful projection")
    assert len(entered) == 1 and entered[0].capture.claimed


@pytest.mark.asyncio
async def test_non_size_capture_refusal_keeps_original_error_order(operation_store, tmp_path, monkeypatch):
    actual_capture = early_projection._capture_owners
    actual_read = paid_source.require_current_checkpoint_dispatched_on_connection
    actual_bracket = early_projection._capture_runtime_between_reads
    reads, failures = [], []
    inside = []

    def bracket(**kwargs):
        inside.append(True)
        try:
            return actual_bracket(**kwargs)
        finally:
            inside.pop()

    def read(conn, running, admission, **kwargs):
        if inside:
            reads.append(admission.selected)
        return actual_read(conn, running, admission, **kwargs)

    def capture(runtime, **kwargs):
        runtime.live.tools = []
        try:
            return actual_capture(runtime, **kwargs)
        except AuditIntegrityError as original:
            failures.append(original)
            raise

    monkeypatch.setattr(early_projection, "_capture_owners", capture)
    monkeypatch.setattr(early_projection, "_capture_runtime_between_reads", bracket)
    monkeypatch.setattr(paid_source, "require_current_checkpoint_dispatched_on_connection", read)
    with pytest.raises(AuditIntegrityError, match="current policy/tools/state reacquisition") as raised:
        async with early_fixture._original_h(operation_store, tmp_path, monkeypatch):
            pytest.fail("changed tool definitions reached a successful capture")
    assert failures == [raised.value] and raised.value.__cause__ is None
    assert len(reads) == 1  # Ordinary policy refusal is not a size witness/final-read route.


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", (False, True), ids=("fitting-calibration", "overflow-and-final-refusal"))
async def test_actual_overflow_retains_original_error_when_final_source_refuses(operation_store, tmp_path, monkeypatch, oversized):
    actual_project = early_projection._project_early_checkpoint_result
    actual_bracket = early_projection._capture_runtime_between_reads
    originals, inputs = [], []

    def project(**kwargs):
        inputs.append(kwargs)
        return actual_project(**kwargs)

    def bracket(*, read_source, capture, changed_source_message):
        def capture_original():
            try:
                return capture()
            except BaseException as error:
                if type(error.__cause__) is not ContinuationMandatoryCaptureOverflow:
                    raise
                originals.append(error)
                prepared = inputs[-1]["prepared"]
                assert (
                    prepared.authority.request_cancel(
                        session_id=prepared.running.claim.session_id, operation_id=prepared.running.claim.operation_id
                    )
                    is not None
                )
                raise

        return actual_bracket(read_source=read_source, capture=capture_original, changed_source_message=changed_source_message)

    monkeypatch.setattr(early_projection, "_project_early_checkpoint_result", project)
    monkeypatch.setattr(early_projection, "_capture_runtime_between_reads", bracket)
    if not oversized:
        await overflow_fixture.test_original_early_projection_overflow_closes_d_without_retry(
            operation_store, tmp_path, monkeypatch, oversized=False
        )
        assert len(inputs) == 1 and originals == []
        return
    with pytest.raises(BaseExceptionGroup) as raised:
        await overflow_fixture.test_original_early_projection_overflow_closes_d_without_retry(
            operation_store, tmp_path, monkeypatch, oversized=True
        )
    assert len(inputs) == len(originals) == 1
    assert raised.value.exceptions[0] is originals[0]
    assert type(raised.value.exceptions[1]) is ComposerOperationCancelledDuringTurn
    prepared, admission, custody = (inputs[0][name] for name in ("prepared", "admission", "custody"))
    assert custody.continuation_checkpoint_v4 is prepared and custody.attempt is admission.attempt
    with locking.locked_session_transaction(operation_store[0], str(prepared.running.claim.session_id)) as conn:
        assert (
            load_snapshot(conn, session_id=str(prepared.running.claim.session_id), operation_id=prepared.running.claim.operation_id)
            == admission.selected
        )
        attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == admission.attempt.attempt_id)).one()
        held = conn.execute(select(holds).where(holds.c.provider_attempt_id == admission.attempt.attempt_id)).one()
        assert attempt.settled_at is None and attempt.ledger_entry_id is None and held.state == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", (False, True), ids=("clean", "blocked"))
async def test_actual_end_pre_and_post_capture_keep_registered_deadline(operation_store, tmp_path, monkeypatch, blocked):
    loop_thread = threading.get_ident()
    observed = []
    actual_pre, actual_post = detached_boundary._capture_owners, end_review._capture_owners

    def observe(kind, actual, runtime, **kwargs):
        scope = locking._CONTINUATION_SQL_DEADLINE.get()
        assert threading.get_ident() != loop_thread
        assert scope is not None and scope[0] is runtime.bindings.required_work.coordinator.sql_deadline_owner
        assert scope[1] is False
        observed.append(kind)
        return actual(runtime, **kwargs)

    monkeypatch.setattr(detached_boundary, "_capture_owners", lambda runtime, **kw: observe("pre", actual_pre, runtime, **kw))
    monkeypatch.setattr(end_review, "_capture_owners", lambda runtime, **kw: observe("post", actual_post, runtime, **kw))
    await lifetime_fixture._run_end(operation_store, tmp_path, blocked=blocked)
    assert observed == ["pre", "post"]

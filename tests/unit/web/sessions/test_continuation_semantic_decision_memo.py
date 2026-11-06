"""Current final Stop SQL retains pure DATA only until its real worker joins."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_semantic_advisor_phase as phase
from elspeth.web.continuation_retention_memo import (
    CanonicalTextEncodingKey,
    borrow_v4_preparation_memo_scope,
    current_retention_stage_memo,
    current_v4_preparation_memo,
)
from elspeth.web.sessions.continuation import load_snapshot
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    end_admit,
    install_sdk,
    semantic_read_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
)
from tests.unit.web.sessions.test_continuation_semantic_advisor_live import _observe_original_driver


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("success", "error", "cancel", "changed_postwrite_source"))
async def test_final_consume_original_sql_clock_child_and_joined_closure(tmp_path, monkeypatch, outcome):
    operation = str(uuid4())
    sdk = ScriptedSDK(
        [
            ("primary", ("The current pipeline has no configured nodes.", None)),
            ("semantic", semantic_read_response),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    original_consume = phase._consume_current_decision
    original_install = phase.install_current_semantic_decision_on_connection
    original_stop = phase.consume_semantic_advisor_end_stop_on_connection
    original_transaction = phase._transaction
    entered, release = threading.Event(), threading.Event()
    parents, children, clocks, errors, before, joined, rolled_back = [], [], [], [], [], [], []
    original_error = RuntimeError("original final consume SQL error")

    async def consume(phases, selected):
        if len(sdk.roles) != 5:
            return await original_consume(phases, selected)
        clock = phases.action_clock
        assert clock is not None and clock.policy == phases.runtime.source.policy.actions.session_sql
        assert current_v4_preparation_memo() is None and not clock._seen
        clocks.append(clock)
        bounds = clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline
        task = asyncio.current_task()
        assert task is not None

        async def cancel_running_worker():
            while not entered.is_set():
                await asyncio.sleep(0.001)
            assert parents[0].active and children[0].active
            task.cancel("final consume while original SQL is running")
            await asyncio.sleep(0)
            assert not task.done() and parents[0].active and children[0].active
            release.set()

        cancellation = asyncio.create_task(cancel_running_worker()) if outcome == "cancel" else None
        try:
            return await original_consume(phases, selected)
        except BaseException as error:
            errors.append(error)
            raise
        finally:
            release.set()
            if cancellation is not None:
                if not entered.is_set():
                    cancellation.cancel()
                cancellation_results = await asyncio.gather(cancellation, return_exceptions=True)
                assert cancellation_results == [None]
            assert bounds == (clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline)
            assert not clock._seen and current_v4_preparation_memo() is None
            assert parents and all(not memo.active for memo in (*parents, *children))

    def install(conn, running, *, selected, effect_key):
        if len(sdk.roles) != 5:
            return original_install(conn, running, selected=selected, effect_key=effect_key)
        child = current_retention_stage_memo()
        parent = current_v4_preparation_memo(clocks[0])
        assert parent is not None and child is not None and child is not parent
        parents.append(parent)
        children.append(child)
        before.append(selected)
        # Complete actual selected bytes are pure DATA, never a currentness receipt.
        key = CanonicalTextEncodingKey(selected.payload_json, len(selected.payload_json.encode("utf-8")))
        calls = []

        def encode(actual):
            calls.append(actual)
            return actual.canonical.encode("utf-8")

        encoded = child.canonical_text_proof(key, encode, hashlib.sha256)
        with borrow_v4_preparation_memo_scope() as other:
            assert other is not child and current_v4_preparation_memo(clocks[0]) is parent
            assert other.canonical_text_proof(key, encode, hashlib.sha256) is encoded
        assert calls == [key] and not other.active and child.active
        if outcome == "cancel":
            entered.set()
            assert release.wait(5), "controller did not release original worker"
            assert parent.active and child.active
        if outcome == "error":
            raise original_error
        result = original_install(conn, running, selected=selected, effect_key=effect_key)
        assert parent.active and child.active
        joined.append(result)
        return result

    def stop(conn, running, *, selected, effect_key):
        if len(sdk.roles) == 5:
            assert joined and selected == joined[-1]
            assert current_v4_preparation_memo(clocks[0]) is parents[0]
            if outcome == "changed_postwrite_source":
                # Original current/CAS reader must reject a substituted postwrite root.
                selected = replace(selected, checkpoint_id="different-checkpoint")
        return original_stop(conn, running, selected=selected, effect_key=effect_key)

    def transaction(phases, callback):
        try:
            return original_transaction(phases, callback)
        except BaseException:
            if before and outcome in {"error", "changed_postwrite_source"}:
                running = phases.work.running
                with phases.authority._engine.connect() as conn:
                    actual = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
                assert actual == before[0]
                rolled_back.append(actual)
            raise

    monkeypatch.setattr(phase, "_consume_current_decision", consume)
    monkeypatch.setattr(phase, "install_current_semantic_decision_on_connection", install)
    monkeypatch.setattr(phase, "consume_semantic_advisor_end_stop_on_connection", stop)
    monkeypatch.setattr(phase, "_transaction", transaction)
    observations = _observe_original_driver(monkeypatch, operation)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        result = await submit_and_settle(
            harness, message_body("Inspect the current pipeline and explain what it contains.", operation_id=operation), rounds=720
        )
    if not parents or not clocks:
        print(
            "Final decision target not reached: "
            + canonical_json(
                {
                    "status": result.status,
                    "sdk_roles": sdk.roles,
                    "parents_seen": len(parents),
                    "clocks_seen": len(clocks),
                    "driver_observation_count": len(observations),
                    "driver_observations": observations[-32:],
                }
            )
        )
    assert len(parents) == len(children) == len(clocks) == 1
    assert all(not memo.active and memo.cached_entry_count == 0 for memo in (*parents, *children))
    assert sdk.roles == ["primary", "semantic", "semantic", "end", "semantic"] and not sdk.steps
    if outcome == "success":
        assert result.status == "completed", result.error
        assert len(joined) == 1 and not errors
    elif outcome == "error":
        assert result.status != "completed" and errors == [original_error] and not joined
        assert rolled_back == before
    elif outcome == "cancel":
        assert len(joined) == 1 and len(errors) == 1 and isinstance(errors[0], asyncio.CancelledError)
    else:
        assert result.status != "completed" and len(joined) == 1 and len(errors) == 1
        assert isinstance(errors[0], AuditIntegrityError)
        assert rolled_back == before

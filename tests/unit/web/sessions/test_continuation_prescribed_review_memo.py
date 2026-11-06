"""Original prescribed Review owns preparation text, never SQL authority."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_semantic_advisor_review as review
from elspeth.web.continuation_retention_memo import current_retention_stage_memo, current_v4_preparation_memo
from elspeth.web.sessions import continuation_driver as driver
from elspeth.web.sessions import continuation_semantic_actions as semantic_actions
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("success", "failure", "cancel", "wrong_source"))
async def test_original_prescribed_review_transports_and_seals_preparation_memo(tmp_path, monkeypatch, outcome):
    operation = str(uuid4())
    parents, children, clocks, caught, attempts_before_dispatch = [], [], [], [], []
    original_entry = driver.review_prescribed_continuation_end
    original_admit = semantic_actions.require_consumed_semantic_review_on_connection
    original_execute = review.execute_continuation_end_review
    failure = RuntimeError("original admission worker failed") if outcome == "failure" else asyncio.CancelledError()

    async def entry(phases, services, selected):
        clock = phases.action_clock
        assert clock is not None and current_v4_preparation_memo() is None
        clocks.append(clock)
        bounds = clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline
        try:
            return await original_entry(phases, services, selected)
        except BaseException as error:
            caught.append(error)
            if outcome == "cancel" and error is failure:
                # The actual worker cancellation has escaped and its SQL/memo
                # scopes are closed. Stop the request through its real owner;
                # otherwise this quiescent cut legitimately hands over and
                # re-enters Review under a new generation and action clock.
                running = phases.work.running
                stopped = phases.authority.request_cancel(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
                assert stopped is not None and stopped.cancel_requested_at is not None
            raise
        finally:
            assert bounds == (clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline)
            assert current_v4_preparation_memo() is None
            assert parents and all(not memo.active for memo in (*parents, *children))

    def admit(conn, running, *, selected, effect_key):
        child = current_retention_stage_memo()
        assert child is not None and child is not parents[-1]
        assert current_v4_preparation_memo(clocks[-1]) is parents[-1]
        children.append(child)
        assert conn.in_transaction()
        assert not conn.execute(
            select(effects.c.effect_key).where(effects.c.operation_id == operation, effects.c.owner == "composer_end")
        ).all()
        attempts_before_dispatch.append(
            tuple(
                conn.execute(
                    select(attempts.c.attempt_id).where(attempts.c.operation_id == operation).order_by(attempts.c.attempt_id)
                ).scalars()
            )
        )
        if outcome in {"failure", "cancel"}:
            raise failure
        if outcome == "wrong_source":
            return original_admit(conn, running, selected=selected, effect_key="provider:1:999999")
        return original_admit(conn, running, selected=selected, effect_key=effect_key)

    async def execute(phases, selected):
        assert phases.action_clock is clocks[-1]
        parent = current_v4_preparation_memo(clocks[-1])
        assert parent is not None and current_retention_stage_memo() is parent
        assert parent.active and all(not child.active for child in children)
        parents.append(parent)
        return await original_execute(phases, selected)

    def end_response(request):
        assert parents and not parents[-1].active
        assert current_v4_preparation_memo() is None
        with harness.app.state.session_engine.connect() as conn:
            assert conn.execute(
                select(effects.c.effect_key).where(effects.c.operation_id == operation, effects.c.owner == "composer_end")
            ).all()
        return end_admit()

    monkeypatch.setattr(driver, "review_prescribed_continuation_end", entry)
    monkeypatch.setattr(semantic_actions, "require_consumed_semantic_review_on_connection", admit)
    monkeypatch.setattr(review, "execute_continuation_end_review", execute)
    sdk = ScriptedSDK(
        [
            ("primary", ("The current pipeline has no configured nodes.", None)),
            ("semantic", semantic_read_response),
            ("semantic", semantic_review_response),
            ("end", end_response),
            ("semantic", semantic_stop_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        result = await submit_and_settle(
            harness,
            message_body("Inspect the current pipeline and explain what it contains.", operation_id=operation),
            rounds=720,
        )
        if outcome != "success":
            with harness.app.state.session_engine.connect() as conn:
                assert not conn.execute(
                    select(effects.c.effect_key).where(effects.c.operation_id == operation, effects.c.owner == "composer_end")
                ).all()
                after = tuple(
                    conn.execute(
                        select(attempts.c.attempt_id).where(attempts.c.operation_id == operation).order_by(attempts.c.attempt_id)
                    ).scalars()
                )
                assert attempts_before_dispatch == [after]
    assert len(parents) == len(children) == len(clocks) == 1
    assert all(not memo.active and memo.cached_entry_count == 0 for memo in (*parents, *children))
    if outcome == "success":
        assert result.status == "completed", result.error
        assert sdk.roles == ["primary", "semantic", "semantic", "end", "semantic"] and not sdk.steps
        assert not caught and "preparation" in clocks[0]._seen
    else:
        assert result.status != "completed" and sdk.roles == ["primary", "semantic", "semantic"]
        assert len(caught) == 1 and "preparation" not in clocks[0]._seen
        if outcome == "wrong_source":
            assert isinstance(caught[0], AuditIntegrityError)
        else:
            assert caught[0] is failure

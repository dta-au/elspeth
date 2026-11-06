"""Final V2 SQL must preserve complete original provider checkpoints.

Use the canonical app/worker. Corruption happens after the actual cold source
has restored and immediately before the final terminal owner enters SQL.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_clock import SegmentFit
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, provider_attempt_audit_drafts_for_source
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions.continuation_driver import ContinuationSegmentHandedOver
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_segment_attempts_table as creators
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, submit_and_settle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ("warm", "cold", "missing-private-request", "private-digest", "public-gap-epoch", "physical-creator", "ledger-model"),
)
async def test_final_terminal_reproves_original_provider_checkpoint_on_its_current_transaction(tmp_path, monkeypatch, case):
    words = "The provider's actual final reply survives its original checkpoint."
    sdk = ScriptedSDK([("primary", (words, None)), ("end", end_admit())])
    install_sdk(monkeypatch, sdk)
    operation_id = str(uuid4())
    cold = case != "warm"
    armed = False
    forced = False
    drivers = []
    handovers = []
    captured_calls = []
    preserved_rows = []
    preserved_physical = []
    preserved_usage = []
    originals = []
    reached = []
    terminal_connections = []
    actual_advance = driver_module._SegmentDriver.advance
    actual_dispose = driver_module._SegmentDriver.dispose
    actual_fit = driver_module.admit_action
    actual_complete = SessionServiceImpl.complete_composer_async_operation

    async def observe_advance(driver, stored, selected):
        nonlocal armed
        if selected.stack.logical.operation_id != operation_id:
            return await actual_advance(driver, stored, selected)
        drivers[:] = [driver]
        result = await actual_advance(driver, stored, selected)
        top = selected.stack.frames[-1]
        if cold and not armed and type(top) is ComposeFrame and top.phase == "response_retained":
            following, current = await driver.selected()
            if type(current.stack.frames[-1]) is EndFrame:
                assert current.stack.frames[-1].phase == "review_needed"
                assert following.checkpoint_id == current.checkpoint_id != selected.checkpoint_id
                armed = True
        return result

    def refuse_one_fit(*args, **kwargs):
        nonlocal forced
        if armed and not forced:
            forced = True
            return SegmentFit.YIELD
        return actual_fit(*args, **kwargs)

    async def observe_dispose(driver, original=None):
        outcome = await actual_dispose(driver, original)
        if driver.running.claim.operation_id == operation_id:
            handovers.append(outcome)
        return outcome

    async def prepare_complete(service, running, **kwargs):
        if running.claim.operation_id != operation_id:
            return await actual_complete(service, running, **kwargs)
        assert not captured_calls, "the final terminal owner must run once"
        (driver,) = drivers
        assert driver.running is running
        runtime = driver.phases.runtime
        assert runtime is not None
        calls = runtime.live.recorder.llm_calls
        assert len(calls) == 2 and sdk.roles == ["primary", "end"] and not sdk.steps
        captured_calls.extend(calls)
        engine = service._engine
        sid = str(running.claim.session_id)
        with engine.connect() as conn:
            rows = conn.execute(select(messages).where(messages.c.session_id == sid).order_by(messages.c.sequence_no)).all()
            attempts = conn.execute(select(physical).where(physical.c.session_id == sid).order_by(physical.c.attempt_id)).all()
            ledger = conn.execute(select(usage).where(usage.c.session_id == sid).order_by(usage.c.entry_id)).all()
        assert {row.attempt_id for row in attempts} == {call.call_id for call in calls}
        assert {row.entry_id for row in ledger} == {row.ledger_entry_id for row in attempts}
        sources = {row.attempt_id: ProviderAuditSource(row.session_id, row.operation_id, row.operation_epoch) for row in attempts}
        first_source, last_source = (sources[call.call_id] for call in calls)
        assert last_source.operation_id == running.session_operation_context.fence.operation_id
        assert last_source.operation_epoch == running.session_operation_context.fence.operation_epoch
        assert (first_source != last_source) is cold
        assert running.claim.ownership_generation == (2 if cold else 1)
        original_drafts = provider_attempt_audit_drafts_for_source(calls[0], first_source)
        assert calls[0].private_request is not None
        assert original_drafts[1].tool_calls is not None
        assert original_drafts[1].tool_calls[0]["evidence"]["status"] == "provider_request"
        # Control the SQL selector against both actual provider cohorts before
        # corrupting any row. Each expected pair/private draft owns one row.
        checkpoint_rows = []
        for call in calls:
            for draft in provider_attempt_audit_drafts_for_source(call, sources[call.call_id]):
                matching = [row for row in rows if deep_thaw(row.tool_calls) == deep_thaw(draft.tool_calls)]
                assert len(matching) == 1
                (row,) = matching
                assert (row.role, row.writer_principal, row.composition_state_id) == ("audit", "compose_loop", None)
                checkpoint_rows.append(row)
        assert len({row.id for row in checkpoint_rows}) == len(checkpoint_rows)
        public = next(row for row in checkpoint_rows if deep_thaw(row.tool_calls) == deep_thaw(original_drafts[0].tool_calls))
        private = next(row for row in checkpoint_rows if deep_thaw(row.tool_calls) == deep_thaw(original_drafts[1].tool_calls))
        with engine.begin() as conn:
            if case == "missing-private-request":
                # Remove the expected private evidence while preserving the
                # message identity and unrelated row/sequence references.
                assert conn.execute(update(messages).where(messages.c.id == private.id).values(tool_calls=None)).rowcount == 1
                assert conn.execute(select(messages.c.tool_calls).where(messages.c.id == private.id)).scalar_one() is None
            elif case == "private-digest":
                envelopes = deep_thaw(private.tool_calls)
                payload = envelopes[0]["evidence"]["arguments"]
                assert payload["retained_sha256"] != "f" * 64
                payload["retained_sha256"] = "f" * 64
                assert conn.execute(update(messages).where(messages.c.id == private.id).values(tool_calls=envelopes)).rowcount == 1
            elif case == "public-gap-epoch":
                envelopes = deep_thaw(public.tool_calls)
                envelopes[1]["operation_epoch"] += 1000
                assert conn.execute(update(messages).where(messages.c.id == public.id).values(tool_calls=envelopes)).rowcount == 1
            elif case == "physical-creator":
                assert (
                    conn.execute(
                        update(creators)
                        .where(creators.c.session_id == sid, creators.c.session_operation_id == first_source.operation_id)
                        .values(session_operation_lease_token="different-original-token")
                    ).rowcount
                    == 1
                )
            elif case == "ledger-model":
                original_attempt = next(row for row in attempts if row.attempt_id == calls[0].call_id)
                assert (
                    conn.execute(
                        update(usage).where(usage.c.entry_id == original_attempt.ledger_entry_id).values(model="different-original-model")
                    ).rowcount
                    == 1
                )
        with engine.connect() as conn:
            preserved_rows.extend(
                conn.execute(select(messages).where(messages.c.id.in_([row.id for row in checkpoint_rows])).order_by(messages.c.id)).all()
            )
            preserved_physical.extend(
                conn.execute(select(physical).where(physical.c.session_id == sid).order_by(physical.c.attempt_id)).all()
            )
            preserved_usage.extend(conn.execute(select(usage).where(usage.c.session_id == sid).order_by(usage.c.entry_id)).all())
        reached.append("mutation committed; original private row identity preserved")
        # The existing terminal SQL's write-lock assertion exposes its actual
        # connection without a pre-read token or caller-supplied skip set.
        actual_assert_lock = service._assert_session_write_lock_held

        def observe_lock(conn, session_id, *, caller):
            result = actual_assert_lock(conn, session_id, caller=caller)
            if session_id == sid:
                terminal_connections.append(conn)
            return result

        monkeypatch.setattr(service, "_assert_session_write_lock_held", observe_lock)
        actual_terminal_sql = service._run_composer_terminal_sql

        async def observe_terminal_sql(owner, operation, **options):
            # The terminal owner may return a concurrently committed failed
            # record after its readback. Observe the actual SQL original
            # before that projection, without changing its disposition.
            def observe_original():
                reached.append("terminal SQL entered")
                try:
                    return operation()
                except BaseException as original:
                    reached.append(safe_failure(original))
                    if isinstance(original, AuditIntegrityError) and all(original is not earlier for earlier in originals):
                        originals.append(original)
                    raise

            return await actual_terminal_sql(owner, observe_original, **options)

        monkeypatch.setattr(service, "_run_composer_terminal_sql", observe_terminal_sql)
        try:
            return await actual_complete(service, running, **kwargs)
        except AuditIntegrityError as original:
            if all(original is not earlier for earlier in originals):
                originals.append(original)
            raise

    def safe_failure(original):
        frames = []
        traceback = original.__traceback__
        while traceback is not None:
            frames.append((traceback.tb_frame.f_code.co_name, traceback.tb_lineno))
            traceback = traceback.tb_next
        first = str(original).splitlines()[:1] if isinstance(original, (AuditIntegrityError, SQLAlchemyError)) else []
        return type(original).__name__, first, frames

    async def observe_complete(service, running, **kwargs):
        try:
            result = await prepare_complete(service, running, **kwargs)
        except BaseException as original:
            if running.claim.operation_id == operation_id:
                reached.append(safe_failure(original))
            raise
        if running.claim.operation_id == operation_id:
            reached.append(("terminal owner returned", result.status))
        return result

    monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
    monkeypatch.setattr(driver_module._SegmentDriver, "dispose", observe_dispose)
    monkeypatch.setattr(driver_module, "admit_action", refuse_one_fit)
    monkeypatch.setattr(SessionServiceImpl, "complete_composer_async_operation", observe_complete)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app_ref.append(harness.app)
        terminal = await submit_and_settle(harness, message_body("Explain the pipeline.", operation_id=operation_id))
        assert len(captured_calls) == 2 and sdk.roles == ["primary", "end"] and not sdk.steps
        if cold:
            assert forced and len(handovers) == 1
            assert type(handovers[0]) is ContinuationSegmentHandedOver
            assert (handovers[0].receipt.sequence, handovers[0].receipt.previous_generation, handovers[0].receipt.generation) == (1, 1, 2)
        else:
            assert not forced and not handovers
        if case in ("warm", "cold"):
            assert terminal.status == "completed", terminal.error
            assert terminal.result is not None and terminal.result.message.content.split("\n\n---\n\n")[0] == words
            assert not originals
            assert terminal_connections, "the actual terminal cohort must enter its locked SQL owner"
        else:
            if terminal.status != "failed" or len(originals) != 1:
                error = terminal.error
                pytest.fail(
                    "\n".join(
                        (
                            f"expected one original integrity refusal; status={terminal.status!r}; originals={len(originals)}",
                            f"error={(error.http_status, error.failure_code, error.error_type) if error is not None else None!r}",
                            *(f"reached[{index}]={entry!r}" for index, entry in enumerate(reached)),
                        )
                    ),
                    pytrace=False,
                )
            expected = {
                "missing-private-request": "private",
                "private-digest": "private",
                "public-gap-epoch": "companion",
                "physical-creator": "source",
                "ledger-model": "usage",
            }[case]
            assert expected in str(originals[0]).lower(), "the refusal must identify the deliberately corrupted source"
        with harness.app.state.session_engine.connect() as conn:
            assert (
                conn.execute(select(messages).where(messages.c.id.in_([row.id for row in preserved_rows])).order_by(messages.c.id)).all()
                == preserved_rows
            )
            assert (
                conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id)).order_by(physical.c.attempt_id)).all()
                == preserved_physical
            )
            assert (
                conn.execute(select(usage).where(usage.c.session_id == str(harness.session_id)).order_by(usage.c.entry_id)).all()
                == preserved_usage
            )
            assistants = conn.execute(
                select(messages.c.content).where(messages.c.session_id == str(harness.session_id), messages.c.role == "assistant")
            ).all()
            assert len(assistants) == (1 if case in ("warm", "cold") else 0)

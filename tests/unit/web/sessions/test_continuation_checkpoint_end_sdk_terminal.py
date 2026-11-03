"""Actual END SDK failures close one original paid child without an END verdict."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import litellm
import pytest
from httpx import ConnectError
from openai import OpenAIError
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer import advisor_checkpoint as advisor_module
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.provider_sdk_terminal import require_sdk_terminal_failure
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_cold import read_v4_sdk_terminal_verified_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store


def _group_members(error: BaseException) -> tuple[BaseException, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(member for item in error.exceptions for member in _group_members(item))
    return (error,)


@pytest.mark.parametrize(
    "kind",
    (
        "provider_failure",
        "transport_failure",
        "provider_timeout",
        "no_observed_result",
        "sdk_audit_companion",
        "after_outer_cas_rollback",
    ),
)
@pytest.mark.asyncio
async def test_actual_end_sdk_failure_closes_first_child_and_outer_without_retry(operation_store, tmp_path, monkeypatch, kind):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    runtime, work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    try:

        async def primary_completion(**_arguments):
            return _reply("The complete original PRIMARY candidate needs END review.")

        monkeypatch.setattr(litellm, "acompletion", primary_completion)
        phases = ContinuationLivePhases(
            service=runtime.materialization.service,
            authority=runtime.materialization.authority,
            work=work,
            provider_owner=runtime.bindings.provider_owner,
            lease=lease,
            progress=runtime.bindings.progress,
        )
        phases.bind_initial(runtime)
        primary = await run_continuation_primary(
            service=phases.service,
            authority=phases.authority,
            selected=runtime.source.selected,
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=work,
            provider_owner=runtime.bindings.provider_owner,
            timeout=runtime.source.policy.actions.primary.total,
        )
        assert type(primary) is PrimaryProviderResult
        phases.accept_primary(primary)
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(store[0], sid) as conn:
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
        selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None
        phases.service._advisor_checkpoint._prompt_guard_mount = PromptGuardMount(_GuardClient(PromptGuardDecision("allow", "clean")))

        original: BaseException = {
            "provider_failure": OpenAIError("private provider failure"),
            "transport_failure": ConnectError("private transport failure"),
            "provider_timeout": TimeoutError("private provider timeout"),
            "no_observed_result": asyncio.CancelledError("private cancellation"),
            "sdk_audit_companion": OpenAIError("private provider failure with audit companion"),
            "after_outer_cas_rollback": OpenAIError("private provider failure before rollback"),
        }[kind]
        audit_failure = AuditIntegrityError("private audit builder failure")
        calls: list[int] = []

        async def failed_completion(**_arguments):
            calls.append(1)
            raise original

        monkeypatch.setattr(litellm, "acompletion", failed_completion)
        if kind == "sdk_audit_companion":

            def fail_audit(**_arguments):
                raise audit_failure

            monkeypatch.setattr(advisor_module, "build_llm_call_record", fail_audit)
        observed_after_outer: list[tuple[str, ...]] = []
        if kind == "after_outer_cas_rollback":
            from elspeth.web.sessions import continuation_sdk_finalization as sdk_finalization

            def fail_after_outer(conn, **_arguments):
                interim = (
                    conn.execute(select(effects.c.disposition).where(effects.c.session_id == sid, effects.c.operation_id == operation))
                    .scalars()
                    .all()
                )
                assert interim.count("sdk_terminal") == 1 and interim.count("unknown") == 1
                observed_after_outer.append(tuple(interim))
                raise AuditIntegrityError("injected after original outer CAS")

            monkeypatch.setattr(sdk_finalization, "append_complete_snapshot", fail_after_outer)
        end_clock = ContinuationActionClock(entered.source.policy.actions.advisor)
        if work.coordinator.sql_deadline_owner is not None:
            work.coordinator.sql_deadline_owner.begin_action(end_clock.policy, started_at=end_clock.started_monotonic)
        phases.bind_action_clock(end_clock)
        if kind == "after_outer_cas_rollback":
            with pytest.raises(AuditIntegrityError, match="injected after original outer CAS"):
                await execute_continuation_end_review(phases, entered.source.selected)
            assert len(observed_after_outer) == 1 and calls == [1]
            with locked_session_transaction(store[0], sid) as conn:
                persisted = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).all()
                provider = next(row for row in persisted if row.effect_key.startswith("provider:1:"))
                outer = tuple(row for row in persisted if row.owner == "composer_end")
                hold = conn.execute(
                    select(holds).where(
                        holds.c.session_id == sid,
                        holds.c.operation_id == operation,
                        holds.c.effect_key == provider.effect_key,
                    )
                ).one()
                physical = conn.execute(select(attempts).where(attempts.c.attempt_id == provider.provider_attempt_id)).one()
                parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
                assert provider.disposition == "dispatched" and provider.receipt_key is None
                assert len(outer) == 1 and outer[0].disposition == "dispatched"
                assert physical.settled_at is None and physical.ledger_entry_id is None
                assert hold.state == "active" and hold.consumed_at is None
                assert parent.status == "running"
            return
        terminal = await execute_continuation_end_review(phases, entered.source.selected)
        if kind == "sdk_audit_companion":
            assert type(terminal) is SdkTerminalFailure
            require_sdk_terminal_failure(terminal)
            with pytest.raises(AuditIntegrityError, match="privately issued original failure"):
                require_sdk_terminal_failure(replace(terminal))
            members = _group_members(terminal.original)
            assert any(member is original for member in members)
            assert any(member is audit_failure for member in members)
        else:
            assert type(terminal) is SdkTerminalResult
            assert terminal.deferred_cancellations == ((original,) if kind == "no_observed_result" else ())
        assert terminal.family is SdkTerminalFamily.CHECKPOINT_END and calls == [1]
        verified, receipt = terminal.verified, terminal.verified.receipt
        assert receipt.producer == "checkpoint_advisor" and receipt.sdk_entered and not receipt.sdk_result_observed
        assert (
            receipt.observation
            == {
                "provider_failure": "provider_failure",
                "transport_failure": "provider_failure",
                "provider_timeout": "provider_timeout",
                "no_observed_result": "no_observed_result",
                "sdk_audit_companion": "provider_failure",
                "after_outer_cas_rollback": "provider_failure",
            }[kind]
        )
        with locked_session_transaction(store[0], sid) as conn:
            cold = read_v4_sdk_terminal_verified_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=operation,
                effect_key=verified.source.effect_key,
                family=SdkTerminalFamily.CHECKPOINT_END,
                names=verified.env_ref_names,
            )
            assert cold == verified and cold.hold_state == "consumed"
            rows = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).all()
            assert {(row.effect_key, row.disposition, row.receipt_key) for row in rows} == {
                (entry.effect_id, entry.disposition, entry.receipt_id)
                for entry in decode_continuation_snapshot(verified.terminal.payload_json).effect_frontier
            }
            provider = next(row for row in rows if row.effect_key == verified.source.effect_key)
            outer = tuple(row for row in rows if row.disposition == "unknown")
            family_children = tuple(row for row in rows if row.effect_key.startswith("provider:1:"))
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == verified.provider_attempt_id)).one()
            parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
            passes = conn.execute(
                select(messages.c.id).where(
                    messages.c.session_id == sid,
                    messages.c.role == "audit",
                    messages.c.tool_calls[0]["_kind"].as_string() == "advisor_checkpoint_pass_audit",
                )
            ).all()
            assert provider.disposition == "sdk_terminal" and len(outer) == 1 and len(family_children) == 1
            assert physical.settled_at is not None and physical.ledger_entry_id is not None
            assert parent.status == "failed" and "private" not in parent.result_json
            assert not passes
            root = decode_continuation_snapshot(verified.terminal.payload_json)
            assert type(root.frames[0]) is ComposeFrame and root.frames[0].advisor_checkpoint_passes == 0
            # A complete terminal requires the independently stored outer SQL
            # disposition as well as the sealed root. The savepoint restores
            # the real terminal before the fixture transaction closes.
            if kind == "provider_failure":
                savepoint = conn.begin_nested()
                try:
                    changed = conn.execute(
                        update(effects)
                        .where(
                            effects.c.session_id == sid,
                            effects.c.operation_id == operation,
                            effects.c.effect_key == outer[0].effect_key,
                            effects.c.disposition == "unknown",
                        )
                        .values(disposition="dispatched")
                    )
                    assert changed.rowcount == 1
                    with pytest.raises(AuditIntegrityError):
                        read_v4_sdk_terminal_verified_on_connection(
                            conn,
                            session_id=running.claim.session_id,
                            operation_id=operation,
                            effect_key=verified.source.effect_key,
                            family=SdkTerminalFamily.CHECKPOINT_END,
                            names=verified.env_ref_names,
                        )
                finally:
                    savepoint.rollback()
                assert (
                    read_v4_sdk_terminal_verified_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=operation,
                        effect_key=verified.source.effect_key,
                        family=SdkTerminalFamily.CHECKPOINT_END,
                        names=verified.env_ref_names,
                    )
                    == verified
                )
    finally:
        await lease.close()

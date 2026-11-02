"""Actual discovery wire retention and paid semantic read refusal controls."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_semantic_advisor_read as reader
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context
from elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions import continuation_sql_deadline as deadline_module
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationReadObservations,
    build_read_owner_result,
    decode_read_owner_result,
)
from elspeth.web.sessions.continuation_semantic_actions import (
    install_semantic_action_on_connection,
    prepare_semantic_read_on_connection,
    read_committed_semantic_read_on_connection,
)
from elspeth.web.sessions.continuation_semantic_advisor import retain_semantic_advisor_outcome_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.sessions.test_continuation_semantic_actions import _read_answer
from tests.unit.web.sessions.test_continuation_semantic_advisor import _begin, _invoke, _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_real_schema_read_wire_roundtrip_keeps_nominal_producer_and_rejects_joint_corruption(operation_store, tmp_path, monkeypatch):
    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        runtime = case["runtime"]
        state = runtime.live.state
        arguments = {"plugin_type": "source", "name": "csv"}
        original = execute_discovery_tool_with_context(
            "get_plugin_schema",
            arguments,
            state,
            ToolContext(catalog=runtime.bindings.policy_catalog, plugin_snapshot=runtime.bindings.plugin_snapshot),
        )
        assert original.success and original.updated_state is state
        retained = build_read_owner_result(
            tool_name="get_plugin_schema",
            arguments=arguments,
            result=original,
            pre_state=state,
            observations=ContinuationReadObservations(),
            env_ref_names=runtime.env_ref_names,
        )
        encoded = canonical_json(retained.model_dump(mode="json"))
        assert decode_read_owner_result(encoded, env_ref_names=runtime.env_ref_names) == retained
        wire = json.loads(retained.response_json)["data"]
        # Wire restoration is a distinct closed boundary. Producer admission
        # must still refuse the exact plain JSON returned by its own encoder.
        with pytest.raises(FrameworkBugError):
            PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(wire)
        for corruption in ("extra-field", "invalid-enum", "credential"):
            envelope = json.loads(retained.response_json)
            if corruption == "extra-field":
                envelope["data"]["unowned_field"] = "must not disappear"
            elif corruption == "invalid-enum":
                envelope["data"]["web_config_authority"] = "unowned_authority"
            else:
                envelope["data"]["description"] = "https://user:" + "x" * 32 + "@example.test/"
            nominal = json.loads(retained.tool_result_json)
            # Corrupt both retained channels consistently. A mere comparison
            # of the two wire copies cannot satisfy these negative controls.
            nominal["data_json"] = canonical_json(envelope["data"])
            changed = retained.model_copy(update={"response_json": canonical_json(envelope), "tool_result_json": canonical_json(nominal)})
            with pytest.raises((AuditIntegrityError, CredentialMaterialRefused)):
                decode_read_owner_result(canonical_json(changed.model_dump(mode="json")), env_ref_names=runtime.env_ref_names)
        assert decode_read_owner_result(encoded, env_ref_names=runtime.env_ref_names) == retained


@asynccontextmanager
async def _prepared_read(store, tmp_path, monkeypatch):
    # Compose the existing paid-SDK/fence/guard fixture; do not construct a
    # second app, driver, action permit or PRIMARY tool-call carrier.
    async with _semantic_case(store, tmp_path, monkeypatch, timed_read=True) as case:
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply(json.dumps(_read_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer, prepared, dispatched = _begin(store, case)
        observed = await _invoke(case, issuer)
        sid = str(store[4])
        with locked_session_transaction(store[0], sid) as conn:
            retained = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
        with locked_session_transaction(store[0], sid) as conn:
            installed = install_semantic_action_on_connection(
                conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
            selected = prepare_semantic_read_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
        assert len(physical) == 1
        yield case, selected, prepared.binding.effect_key, physical


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("stop", "handler"))
async def test_prepared_paid_read_preserves_stop_or_real_handler_original_without_result(operation_store, tmp_path, monkeypatch, failure):
    async with _prepared_read(operation_store, tmp_path, monkeypatch) as (case, selected, semantic_key, physical):
        runtime, running = case["runtime"], case["running"]
        authority = runtime.materialization.authority
        actual_execute = reader.execute_discovery_tool_with_context
        catalog_type = type(runtime.bindings.policy_catalog)
        actual_schema = catalog_type.get_schema
        executing = False
        calls = []
        original = RuntimeError("actual semantic read catalog failure")

        def actual_handler_failure(catalog, *args, **kwargs):
            if executing and failure == "handler":
                raise original
            return actual_schema(catalog, *args, **kwargs)

        def observe_dispatch(*args, **kwargs):
            nonlocal executing
            calls.append(args[0])
            executing = True
            try:
                return actual_execute(*args, **kwargs)
            finally:
                executing = False

        monkeypatch.setattr(catalog_type, "get_schema", actual_handler_failure)
        monkeypatch.setattr(reader, "execute_discovery_tool_with_context", observe_dispatch)
        if failure == "stop":
            stopped = authority.request_cancel(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
            assert stopped is not None and stopped.cancel_requested_at is not None
        expected = ComposerOperationCancelledDuringTurn if failure == "stop" else RuntimeError
        budget = case["provider"].cost_budget.snapshot()
        sql_deadline = case["sql_deadline"]
        assert sql_deadline is not None
        sql_deadline.begin_action(sql_deadline.policy.actions.read, started_at=time.monotonic())
        with pytest.raises(expected) as raised:
            await reader.execute_semantic_advisor_read(
                service=runtime.materialization.service,
                authority=authority,
                running=running,
                selected=selected,
                effect_key=semantic_key,
                runtime=runtime,
                required_work=case["work"],
            )
        if failure == "handler":
            assert raised.value is original and calls == ["get_plugin_schema"]
        else:
            assert calls == []
        assert len(physical) == 1 and case["provider"].cost_budget.snapshot() == budget
        with operation_store[0].connect() as conn:
            (row,) = conn.execute(
                select(effects).where(effects.c.operation_id == running.claim.operation_id, effects.c.owner == "read")
            ).all()
            assert row.disposition == "prepared" and row.receipt_key is row.result_chunk_key is None
        case["work"].coordinator.assert_completed()


@dataclass(frozen=True)
class _Clock:
    now: float

    def monotonic(self) -> float:
        return self.now


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("expired-before-dispatch", "expired-during-current-proof", "late-result-with-stop"))
async def test_prepared_semantic_read_uses_original_action_clock_and_retains_late_result(operation_store, tmp_path, monkeypatch, boundary):
    async with _prepared_read(operation_store, tmp_path, monkeypatch) as (case, selected, semantic_key, physical):
        runtime, running = case["runtime"], case["running"]
        sql_deadline = case["sql_deadline"]
        assert sql_deadline is not None
        action = sql_deadline.policy.actions.read
        sql_deadline.begin_action(
            action,
            started_at=time.monotonic() - (action.total + 1 if boundary == "expired-before-dispatch" else 0),
        )
        original_clock = sql_deadline.capture_read_result_deadline()
        actual_execute = reader.execute_discovery_tool_with_context
        returns = []
        if boundary == "expired-during-current-proof":
            actual_context = reader._read_context

            def expire_after_context(*args, **kwargs):
                context = actual_context(*args, **kwargs)
                monkeypatch.setattr(reader, "time", _Clock(original_clock.action_expires_at + 0.1))
                return context

            monkeypatch.setattr(reader, "_read_context", expire_after_context)

        def observe_original_return(*args, **kwargs):
            value = actual_execute(*args, **kwargs)
            returns.append(value)
            stopped = runtime.materialization.authority.request_cancel(
                session_id=running.claim.session_id, operation_id=running.claim.operation_id
            )
            assert stopped is not None and stopped.cancel_requested_at is not None
            monkeypatch.setattr(deadline_module, "time", _Clock(original_clock.action_expires_at + 0.1))
            return value

        monkeypatch.setattr(reader, "execute_discovery_tool_with_context", observe_original_return)
        arguments = {
            "service": runtime.materialization.service,
            "authority": runtime.materialization.authority,
            "running": running,
            "selected": selected,
            "effect_key": semantic_key,
            "runtime": runtime,
            "required_work": case["work"],
        }
        if boundary != "late-result-with-stop":
            with pytest.raises(TimeoutError):
                await reader.execute_semantic_advisor_read(**arguments)
            assert returns == []
            with operation_store[0].connect() as conn:
                (effect,) = conn.execute(
                    select(effects).where(effects.c.operation_id == running.claim.operation_id, effects.c.owner == "read")
                ).all()
                assert effect.disposition == "prepared" and effect.receipt_key is effect.result_chunk_key is None
        else:
            actual = await reader.execute_semantic_advisor_read(**arguments)
            assert len(returns) == 1 and actual.result is returns[0]
            assert actual.committed.receipt.disposition == "committed"
            assert actual.committed.original_result.prescribing_provider_call_id is not None
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                # The actual return is owed, while ordinary current reads
                # still refuse after Stop. The receipt grants no new read.
                with pytest.raises(ComposerOperationCancelledDuringTurn):
                    read_committed_semantic_read_on_connection(conn, running, selected=actual.committed.snapshot, effect_key=semantic_key)
                effect = conn.execute(select(effects).where(effects.c.effect_key == actual.committed.owner_effect_key)).one()
                assert effect.disposition == "committed" and effect.receipt_key == actual.committed.receipt.owner_receipt_id
                assert effect.result_chunk_key is not None
                encoded = read_chunk(
                    conn,
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    key=effect.result_chunk_key,
                ).decode("utf-8")
                assert decode_read_owner_result(encoded, env_ref_names=runtime.env_ref_names) == actual.committed.original_result
            assert sql_deadline.capture_read_result_deadline() == original_clock and not sql_deadline._settling
            with pytest.raises(TimeoutError):
                sql_deadline.remaining_statement(session_id=str(running.claim.session_id))
        assert len(physical) == 1
        case["work"].coordinator.assert_completed()

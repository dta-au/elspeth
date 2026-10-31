"""Real retained-primary finish-once preparation and current-source precedence."""

from datetime import timedelta
from threading import get_ident

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_primary as driver
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope, resolve_prompt_guard_user_prompt
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions import continuation_title_provider as title_sql
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_initialization import bootstrap_case, publish
from tests.unit.web.sessions.test_continuation_primary_result import primary_input, stored_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST
from tests.unit.web.sessions.test_continuation_title_provider import all_rows_on_connection

operation_store = operations.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["dispatch", "oversize", "changed_source"])
async def test_finish_once_uses_actual_bootstrap_prompt_despite_lost_async_context_and_preserves_source_first(
    operation_store, tmp_path, monkeypatch, scenario
):
    kwargs, produced, recorder, requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    service = kwargs["service"]
    work = kwargs["required_work"]
    async_thread = get_ident()
    observed = []
    original_freeze = driver._freeze_input

    def observe_actual_worker(**arguments):
        ambient = resolve_prompt_guard_user_prompt(None)
        current = arguments["producers"]._require_current()
        observed.append((get_ident(), ambient, current.values.message))
        captured = original_freeze(**arguments)
        assert captured.request.provider_input.genuine_user == current.values.message == REQUEST
        return captured

    monkeypatch.setattr(driver, "_freeze_input", observe_actual_worker)
    if scenario != "dispatch":
        gateway = service._provider_gateway
        gateway._settings = gateway._settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
    messages = [dict(message) for message in produced.messages]
    if scenario == "changed_source":
        messages[-1]["content"] += " changed actual current source"
    physical = []

    async def completion(**sdk_kwargs):
        _assert_required_guard_admitted(
            operation_store[0], running.session_operation_context, requests, role="composer", user_prompt=REQUEST
        )
        physical.append(sdk_kwargs)
        return ModelResponse(
            model="test/model",
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "Actual reply", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    before = stored_rows(operation_store[0])
    try:
        with prompt_guard_user_scope("Different ambient input that must never replace the admitted bootstrap"):
            arguments = {
                "service": service,
                "authority": kwargs["authority"],
                "selected": kwargs["selected"],
                "producers": produced.producers,
                "messages": messages,
                "tools": [],
                "recorder": recorder,
                "drained": RecorderDrainCursors(),
                "required_work": work,
                "provider_owner": ProviderInvocationOwner(service=operation_store[3], required_work=work),
                "timeout": 5,
            }
            if scenario == "dispatch":
                result = await driver.run_continuation_primary(**arguments)
                assert result.outcome.completion.message.content == "Actual reply"
            else:
                expected = AuditIntegrityError if scenario == "changed_source" else ComposerContextUnsupported
                with pytest.raises(expected) as caught:
                    await driver.run_continuation_primary(**arguments)
                assert type(caught.value) is expected
                if scenario == "oversize":
                    assert caught.value.reason == "prepared_request_bytes"
                else:
                    assert "complete current message producers" in str(caught.value)
                assert stored_rows(operation_store[0]) == before
        assert len(observed) == 1
        worker_thread, ambient, producer_prompt = observed[0]
        assert worker_thread != async_thread and ambient is None and producer_prompt == REQUEST
        assert len(physical) == int(scenario == "dispatch")
        if scenario != "dispatch":
            assert requests == [] and not recorder.llm_calls
        else:
            assert len(recorder.llm_calls) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["oversize", "policy", "timing", "ordinal"])
async def test_retained_title_checks_current_policy_timing_and_ordinal_before_size_and_descriptor(
    operation_store, tmp_path, monkeypatch, defect
):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    published = await publish(operation_store, running, completed, service)
    with operation_store[0].begin() as connection:
        grant_test_pipeline_user(connection, identity_id="alice")
        seed_token_policies(connection, identity_id="alice")
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    service._settings = settings
    service._provider_gateway = ProviderGateway(
        model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    work = inventory_work(running)
    selected = await load_selected_continuation(
        authority=operation_store[2], running=running, selected=published.selected, required_work=work
    )
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=operation_store[2], selected=selected, required_work=work
    )
    root = decode_continuation_snapshot(published.selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    actual_prepare = title_sql.prepare_provider_input
    actual_descriptor = title_sql.title_input_descriptor
    preparations, descriptors = [], []

    def observe_preparation(**arguments):
        preparations.append(arguments["max_bytes"])
        return actual_prepare(**arguments)

    def observe_descriptor(*arguments, **keywords):
        descriptors.append(keywords["prepared_input"])
        return actual_descriptor(*arguments, **keywords)

    monkeypatch.setattr(title_sql, "prepare_provider_input", observe_preparation)
    monkeypatch.setattr(title_sql, "title_input_descriptor", observe_descriptor)
    try:
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as connection:
            original_rows = all_rows_on_connection(connection)
            positive = connection.begin_nested()
            actual = title_sql.prepare_title_provider_on_connection(
                connection,
                service,
                running,
                selected=published.selected,
                clock=ContinuationActionClock(policy.actions.title),
                authority=operation_store[2],
                inventory=inventory,
                work=work,
            )
            assert descriptors == [actual.provider_input] and len(preparations) == 1
            assert actual.provider_input.genuine_user == actual.request.user_prompt
            positive.rollback()
            assert all_rows_on_connection(connection) == original_rows
            assert connection.execute(select(ordinals)).all() == []
            negative = connection.begin_nested()
            service._settings = settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
            clock = ContinuationActionClock(policy.actions.primary if defect == "policy" else policy.actions.title)
            if defect == "ordinal":
                connection.execute(
                    insert(ordinals).values(
                        session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, family=3, next_ordinal=1
                    )
                )
            elif defect == "timing":
                now = database_now(connection)
                connection.execute(
                    update(segments)
                    .where(
                        segments.c.session_id == str(running.claim.session_id),
                        segments.c.operation_id == running.claim.operation_id,
                        segments.c.sequence == running.claim.segment_sequence,
                    )
                    .values(
                        deadline_at=now + timedelta(seconds=policy.actions.title.total / 2),
                        cooperative_at=now + timedelta(seconds=policy.actions.title.total / 4),
                    )
                )
            baseline = (all_rows_on_connection(connection), tuple(connection.execute(select(ordinals)).all()))
            expected = ComposerContextUnsupported if defect == "oversize" else AuditIntegrityError
            with pytest.raises(expected) as caught:
                title_sql.prepare_title_provider_on_connection(
                    connection,
                    service,
                    running,
                    selected=published.selected,
                    clock=clock,
                    authority=operation_store[2],
                    inventory=inventory,
                    work=work,
                )
            assert type(caught.value) is expected
            if defect == "oversize":
                assert caught.value.reason == "prepared_request_bytes" and len(preparations) == 2
            else:
                assert len(preparations) == 1
                assert {"policy": "retained action allowance", "timing": "must yield", "ordinal": "durable family ordinal"}[defect] in str(
                    caught.value
                )
            assert descriptors == [actual.provider_input]
            assert (all_rows_on_connection(connection), tuple(connection.execute(select(ordinals)).all())) == baseline
            negative.rollback()
        assert requests == []
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)

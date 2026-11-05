"""Same-read context handoff keeps actual paid-source refusals and exact DATA."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_json
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.sessions import continuation_primary as primary_sql
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_primary_result import publish_actual

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.parametrize("with_tools", (False, True))
def test_original_wire_context_is_exact_and_local_to_each_request(with_tools):
    tools = [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}] if with_tools else []
    contexts = []
    for content in ("First café request", "Distinct second request"):
        messages = [{"role": "user", "content": content}]
        request = {"model": "test/model", "messages": messages}
        if with_tools:
            request["tools"] = tools
        text = continuation_provider_input_json(request)
        descriptor = primary_sql._input_value(text, stable_hash(json.loads(text)))
        wire, actual_tools, context = primary_sql._original_wire_with_context(descriptor)
        expected = capture_provider_context(messages=messages, tools=tools or None)
        assert context.disposition == "retained" and context == expected
        assert (wire, actual_tools) == (messages, tools)
        assert primary_sql._original_wire(descriptor) == (messages, tools)
        # Mutable decoded DATA can change later without changing the frozen
        # capture or the next fresh read of its original descriptor.
        wire[0]["content"] = "Changed after the paired return"
        actual_tools.clear()
        assert context == expected
        assert primary_sql._original_wire(descriptor) == (messages, tools)
        contexts.append(context)
    assert contexts[0] != contexts[1]


@pytest.mark.asyncio
async def test_actual_physical_reader_captures_once_and_refuses_wrong_valid_context(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        call = result.outcome.audit_call
        wrong = capture_provider_context(messages=[{"role": "user", "content": "Another valid request"}], tools=None)
        assert wrong.disposition == "retained" and wrong != call.private_request
        captured = []

        def observe_capture(*, messages, tools):
            value = capture_provider_context(messages=messages, tools=tools)
            captured.append(value)
            return value

        monkeypatch.setattr(primary_sql, "capture_provider_context", observe_capture)
        with operation_store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
            expected_entry = conn.execute(select(physical.c.ledger_entry_id).where(physical.c.attempt_id == call.call_id)).scalar_one()
            args = (conn, running.claim.session_id, running.claim.operation_id, effect)
            assert primary_sql._physical_source_data(*args, call) == expected_entry
            assert captured == [call.private_request]
            captured.clear()
            with pytest.raises(AuditIntegrityError, match="physical audit differs from its complete actual prepared input source"):
                primary_sql._physical_source_data(*args, replace(call, private_request=wrong))
            assert captured == [call.private_request]
            captured.clear()
            assert primary_sql._physical_source_data(*args, call) == expected_entry
            assert captured == [call.private_request]
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_physical_reader_refuses_unsafe_original_before_metadata_mismatch(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        call = result.outcome.audit_call
        with operation_store[0].begin() as conn:
            effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
            args = (conn, running.claim.session_id, running.claim.operation_id)
            positive = primary_sql._physical_source_data(*args, effect, call)
            descriptor = primary_sql._stored_descriptor_data(*args, effect)
            altered = json.loads(descriptor.canonical)
            altered["messages"] = [{"role": "user", "content": "data:image/png;base64,AAAA"}]
            altered["options"]["model"] = "different/model"
            text = canonical_json(altered)
            digest = stable_hash(altered)
            payload = text.encode("utf-8")
            # Coherently change the retained source and its descriptor identity
            # so the real capture, not a broken hash, must refuse it first.
            savepoint = conn.begin_nested()
            conn.execute(
                update(chunks)
                .where(chunks.c.chunk_key == f"provider-input:{effect.input_sha256}")
                .values(chunk_key=f"provider-input:{digest}", payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
            conn.execute(update(effects).where(effects.c.effect_key == result.effect_key).values(input_sha256=digest))
            altered_effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
            captured = []

            def observe_capture(*, messages, tools):
                value = capture_provider_context(messages=messages, tools=tools)
                captured.append(value)
                return value

            with monkeypatch.context() as patch:
                patch.setattr(primary_sql, "capture_provider_context", observe_capture)
                with pytest.raises(AuditIntegrityError, match="original input is not its safely retained complete source"):
                    primary_sql._physical_source_data(*args, altered_effect, call)
            assert len(captured) == 1
            assert captured[0].disposition == "unavailable" and captured[0].reasons == ("unsupported",)
            savepoint.rollback()
            assert primary_sql._physical_source_data(*args, effect, call) == positive
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)

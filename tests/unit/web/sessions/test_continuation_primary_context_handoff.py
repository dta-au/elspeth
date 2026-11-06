"""Historical request equality and fresh request admission have distinct owners."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.composer_provider_context import PrivateProviderContext
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.audit_storage import private_diagnostic_drafts
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_codec import decode_retained_physical_audit_call, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_json
from elspeth.web.composer.continuation_provider_outcomes import encode_physical_audit_call
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.sessions import continuation_primary as primary_sql
from elspeth.web.sessions.continuation import private_chunk_key
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.contracts.test_composer_llm_audit import _make_call
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_primary_result import primary_input, publish_actual

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.parametrize("with_tools", (False, True))
def test_original_wire_context_is_exact_and_local_to_each_request(with_tools):
    tools = (
        [
            {
                "type": "function",
                "function": {
                    "name": "inspect",
                    "strict": True,
                    "parameters": {"type": "object", "properties": {"api_key": {"type": "string"}}},
                },
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if with_tools
        else []
    )
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
        primary_sql._require_original_request_comparison(
            descriptor,
            replace(
                _make_call(),
                model_requested=descriptor.model,
                temperature=descriptor.temperature,
                seed=descriptor.seed,
                messages_hash=stable_hash(messages),
                tools_spec_hash=stable_hash(tools) if tools else None,
                declared_tool_names=("inspect",) if with_tools else (),
                private_request=expected,
            ),
        )
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


def test_historical_canonical_fragments_match_full_capture_and_hashes():
    wire = [
        {"role": "user", "content": 'Café 😀 \\ "quoted"\n},"tools":null,{', "cache_control": {"type": "ephemeral"}},
        {"role": "assistant", "content": None},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "inspect",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "\ue000": {"enum": [None, True, False, 0, -0.0, 1.0, 1.5, 1e-7, 1e30]},
                        "😀": {"type": "string", "description": 'Backslash \\ and quote "\n'},
                    },
                },
            },
        },
        {"type": "function", "function": {"name": "finish", "parameters": {"type": "object"}}},
    ]
    text = continuation_provider_input_json({"model": "test/model", "messages": wire, "tools": tools})
    source = primary_sql._input_value(text, stable_hash(json.loads(text)))
    expected = capture_provider_context(messages=wire, tools=tools)
    assert expected.disposition == "retained"
    call = _make_call(
        model_requested=source.model,
        temperature=source.temperature,
        seed=source.seed,
        messages_hash=stable_hash(wire),
        tools_spec_hash=stable_hash(tools),
        declared_tool_names=("inspect", "finish"),
        private_request=expected,
    )
    primary_sql._require_original_request_comparison(source, call)
    for changed in (
        replace(source, messages_json=canonical_json(list(reversed(wire)))),
        replace(source, tools_json=canonical_json(list(reversed(tools)))),
        replace(source, messages_json=canonical_json([{"role": "user", "content": "Different"}, wire[1]])),
    ):
        with pytest.raises(AuditIntegrityError, match="physical audit differs from its complete actual prepared input source"):
            primary_sql._require_original_request_comparison(changed, call)
    for changed_call in (replace(call, messages_hash="0" * 64), replace(call, tools_spec_hash="0" * 64)):
        with pytest.raises(AuditIntegrityError, match="physical audit differs from its complete actual prepared input source"):
            primary_sql._require_original_request_comparison(source, changed_call)
    primary_sql._require_original_request_comparison(source, call)


@pytest.mark.asyncio
async def test_actual_physical_reader_compares_without_recapture_and_refuses_wrong_context(operation_store, tmp_path, monkeypatch):
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
            assert captured == []
            captured.clear()
            for other_context in (wrong, None, PrivateProviderContext(disposition="unavailable", canonical=None, reasons=("unsupported",))):
                with pytest.raises(AuditIntegrityError, match="physical audit differs from its complete actual prepared input source"):
                    primary_sql._physical_source_data(*args, replace(call, private_request=other_context))
            assert captured == []
            captured.clear()
            assert primary_sql._physical_source_data(*args, call) == expected_entry
            assert captured == []
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_coherent_unsafe_historical_sql_request_compares_but_fresh_admission_refuses(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None
        original_call = result.outcome.audit_call
        assert original_call is not None
        with operation_store[0].begin() as conn:
            effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
            args = (conn, running.claim.session_id, running.claim.operation_id)
            positive = primary_sql._physical_source_data(*args, effect, original_call)
            descriptor = primary_sql._stored_descriptor_data(*args, effect)
            altered = json.loads(descriptor.canonical)
            altered["messages"] = [{"role": "user", "content": "data:image/png;base64,AAAA"}]
            tools = altered["tools"]
            unavailable = capture_provider_context(messages=altered["messages"], tools=tools)
            assert unavailable.disposition == "unavailable" and unavailable.reasons == ("unsupported",)
            # Deliberately represent an earlier defective producer's coherent
            # history. Constructing this DTO does not admit a new request.
            context = PrivateProviderContext(
                disposition="retained",
                canonical=canonical_json(
                    {
                        "schema_version": "composer.provider-context.v1",
                        "capture_boundary": "prepared_litellm_messages_and_tools",
                        "messages": altered["messages"],
                        "tools": tools,
                    }
                ),
            )
            changed_call = replace(original_call, messages_hash=stable_hash(altered["messages"]), private_request=context)
            changed_text = canonical_json(altered)
            changed_digest = stable_hash(altered)
            payload = changed_text.encode("utf-8")
            change = conn.begin_nested()
            conn.execute(
                update(chunks)
                .where(chunks.c.chunk_key == f"provider-input:{effect.input_sha256}")
                .values(
                    chunk_key=f"provider-input:{changed_digest}",
                    payload=payload,
                    sha256=sha256(payload).hexdigest(),
                    byte_length=len(payload),
                )
            )
            # Store the complete modified physical call in its actual response
            # chunk as well as changing the descriptor and public/private audit
            # cohort. This exercises the SQL physical reader, not just equality
            # between hand-built helper arguments. Selected-root reconstruction
            # is outside this test; the existing reader tests cover its joins.
            response_value = json.loads(result.response.canonical)
            response_value["audit_call_json"] = encode_physical_audit_call(changed_call)
            changed_response = encode_private_chunk(codec="composer.response.v1", canonical=canonical_json(response_value))
            conn.execute(
                insert(chunks).values(
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    chunk_key=private_chunk_key(changed_response),
                    codec=changed_response.reference.codec,
                    payload=changed_response.canonical.encode("utf-8"),
                    sha256=changed_response.reference.digest,
                    byte_length=changed_response.reference.byte_length,
                )
            )
            conn.execute(
                update(effects)
                .where(effects.c.effect_key == result.effect_key)
                .values(input_sha256=changed_digest, result_chunk_key=private_chunk_key(changed_response))
            )
            assert primary_sql._chunk_data(*args, result.response.reference) == result.response
            original_items = [llm_call_audit_envelope(original_call)]
            changed_items = [llm_call_audit_envelope(changed_call)]
            for call, items in ((original_call, original_items), (changed_call, changed_items)):
                for draft in private_diagnostic_drafts(call, context=running.session_operation_context):
                    if draft.tool_calls is not None:
                        items.extend(draft.tool_calls)
            replacements = {canonical_json(old): new for old, new in zip(original_items, changed_items, strict=True)}
            seen = []
            rows = conn.execute(
                select(messages).where(
                    messages.c.session_id == str(running.claim.session_id),
                    messages.c.role == "audit",
                    messages.c.writer_principal == "compose_loop",
                )
            ).all()
            for row in rows:
                if row.tool_calls is None:
                    continue
                updated = []
                for item in row.tool_calls:
                    key = canonical_json(item)
                    if key in replacements:
                        seen.append(key)
                        updated.append(replacements[key])
                    else:
                        updated.append(item)
                conn.execute(update(messages).where(messages.c.id == row.id).values(tool_calls=json.loads(canonical_json(updated))))
            assert sorted(seen) == sorted(replacements)
            changed_effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
            retained_response = primary_sql._chunk_data(*args, changed_response.reference)
            retained_call = decode_retained_physical_audit_call(retained_response)
            assert retained_call == changed_call
            assert primary_sql._physical_source_data(*args, changed_effect, retained_call) == positive
            retained_input = primary_sql._stored_descriptor_data(*args, changed_effect)
            with pytest.raises(AuditIntegrityError, match="not its safely retained complete source"):
                primary_sql._original_wire_with_context(retained_input)
            # Exact SQL accounting remains fresh even when the historical
            # content is deliberately no longer subjected to today's policy.
            broken_ledger = conn.begin_nested()
            conn.execute(update(usage).where(usage.c.entry_id == positive).values(prompt_tokens=999))
            with pytest.raises(AuditIntegrityError, match="original complete token ledger"):
                primary_sql._physical_source_data(*args, changed_effect, retained_call)
            broken_ledger.rollback()
            assert primary_sql._physical_source_data(*args, changed_effect, retained_call) == positive
            change.rollback()
            assert primary_sql._physical_source_data(*args, effect, original_call) == positive
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.parametrize(
    "changed",
    [
        {"messages_json": '[{"role":"user","role":"assistant","content":"hi"}]'},
        {"messages_json": '[ {"content":"hi","role":"user"}]'},
        {"messages_json": '["not a message"]'},
        {"messages_json": '[{"content":"hi","role":"user"}]{}'},
        {"messages_json": '[{"content":null,"role":"user"}'},
        {"messages_json": '[{"content":1.0,"role":"user"}]'},
        {"messages_json": '[{"content":-0.0,"role":"user"}]'},
        {"messages_json": '[{"content":"\\u00e9","role":"user"}]'},
        {"tools_present": True, "tools_json": "null"},
        {"tools_present": False, "tools_json": "[]"},
        {"tools_present": True, "tools_json": "[]"},
        {"tools_present": True, "tools_json": "[0]"},
        {"tools_present": True, "tools_json": '[{"type":"function","type":"function"}]'},
        {"tools_present": True, "tools_json": '[ {"type":"function"}]'},
    ],
)
def test_historical_comparison_keeps_strict_wire_shape_and_presence(changed):
    text = continuation_provider_input_json({"model": "test/model", "messages": [{"role": "user", "content": "hi"}]})
    source = primary_sql._input_value(text, stable_hash(json.loads(text)))
    with pytest.raises(AuditIntegrityError, match="complete retained SDK wire"):
        primary_sql._require_original_request_comparison(replace(source, **changed), _make_call())


def test_historical_comparison_preserves_large_integral_double_roundtrip_refusal():
    wire = [{"role": "user", "content": 1e17}]
    text = continuation_provider_input_json({"model": "test/model", "messages": wire})
    # The existing strict parser reads this emitted integer notation as int,
    # which the canonical roundtrip refuses. Fragment reuse must not bypass it.
    with pytest.raises(ValueError, match="exceeds safe integer domain"):
        primary_sql._input_value(text, sha256(text.encode("utf-8")).hexdigest())
    positive = continuation_provider_input_json({"model": "test/model", "messages": [{"role": "user", "content": "hi"}]})
    source = primary_sql._input_value(positive, stable_hash(json.loads(positive)))
    with pytest.raises(ValueError, match="exceeds safe integer domain"):
        primary_sql._require_original_request_comparison(replace(source, messages_json=canonical_json(wire)), _make_call())


@pytest.mark.parametrize("offset", (-1, 0, 1))
def test_historical_comparison_keeps_encoded_context_byte_ceiling(offset):
    wire = [{"role": "user", "content": ""}]
    envelope = {
        "schema_version": "composer.provider-context.v1",
        "capture_boundary": "prepared_litellm_messages_and_tools",
        "messages": wire,
        "tools": None,
    }
    content_bytes = JSON_MAX_TOTAL_UTF8_BYTES + offset - len(canonical_json(envelope).encode("utf-8"))
    wire[0]["content"] = "é" * (content_bytes // 2) + "x" * (content_bytes % 2)
    expected = canonical_json(envelope)
    assert len(expected.encode("utf-8")) == JSON_MAX_TOTAL_UTF8_BYTES + offset
    assert len(expected) < len(expected.encode("utf-8"))
    text = continuation_provider_input_json({"model": "test/model", "messages": wire})
    source = primary_sql._input_value(text, stable_hash(json.loads(text)))
    if offset > 0:
        with pytest.raises(AuditIntegrityError, match="context exceeds its encoded bound"):
            primary_sql._require_original_request_comparison(source, _make_call())
        return
    call = _make_call(
        model_requested=source.model,
        temperature=source.temperature,
        seed=source.seed,
        messages_hash=stable_hash(wire),
        tools_spec_hash=None,
        declared_tool_names=(),
        private_request=PrivateProviderContext(disposition="retained", canonical=expected),
    )
    primary_sql._require_original_request_comparison(source, call)


@pytest.mark.asyncio
async def test_actual_primary_preparation_still_admits_complete_selected_request_content(operation_store, tmp_path, monkeypatch):
    kwargs, produced, _recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    selected = kwargs["selected"]
    request = kwargs["service"]._provider_gateway.prepare_continuation_primary(list(produced.messages), [])
    original_text = continuation_provider_input_json(request.kwargs)
    original_root = encode_continuation_snapshot(selected.stack)
    try:
        with operation_store[0].begin() as conn:

            def prepare(text, root):
                return primary_sql.prepare_primary_provider_on_connection(
                    conn,
                    running,
                    input_sha256=stable_hash(json.loads(text)),
                    input_json=text,
                    snapshot_json=root,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.stack.cursor,
                )

            positive = conn.begin_nested()
            assert prepare(original_text, original_root).input_sha256 == request.input_sha256
            positive.rollback()
            wire = [{"role": "user", "content": "data:image/png;base64,AAAA"}]
            frame = selected.stack.frames[-1]
            source = encode_private_chunk(
                codec=frame.messages.codec,
                canonical=canonical_json({"messages_json": [canonical_json(wire[0])], "current_indexes": [0]}),
            )
            changed_root = encode_continuation_snapshot(
                selected.stack.model_copy(update={"frames": (frame.model_copy(update={"messages": source.reference}),)})
            )
            changed_input = json.loads(original_text)
            changed_input["messages"] = wire
            negative = conn.begin_nested()
            conn.execute(
                insert(chunks).values(
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    chunk_key=private_chunk_key(source),
                    payload=source.canonical.encode("utf-8"),
                    sha256=source.reference.digest,
                    byte_length=source.reference.byte_length,
                    codec=source.reference.codec,
                )
            )
            payload = changed_root.encode("utf-8")
            conn.execute(
                update(chunks)
                .where(chunks.c.chunk_key == f"snapshot:{selected.checkpoint_id}")
                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
            # The actual selected message and descriptor agree. Only full
            # current request capture, not a hash or wire mismatch, refuses.
            with pytest.raises(AuditIntegrityError, match="complete safely retainable original provider input"):
                prepare(canonical_json(changed_input), changed_root)
            assert conn.execute(select(effects)).all() == []
            negative.rollback()
            restored = conn.begin_nested()
            assert prepare(original_text, original_root).input_sha256 == request.input_sha256
            restored.rollback()
        kwargs["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_physical_reader_refuses_changed_unsafe_source_by_complete_comparison(operation_store, tmp_path, monkeypatch):
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
            # Coherently change the source and descriptor identity. Its actual
            # call still disagrees, independent of current content admission.
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
                with pytest.raises(AuditIntegrityError, match="physical audit differs from its complete actual prepared input source"):
                    primary_sql._physical_source_data(*args, altered_effect, call)
            assert captured == []
            savepoint.rollback()
            assert primary_sql._physical_source_data(*args, effect, call) == positive
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)

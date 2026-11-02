"""The paid model control reaches a real successor through the production worker."""

from __future__ import annotations

import json
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_controls import CONTINUATION_CONTROL_RESULT, CONTINUATION_CONTROL_TOOL
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from tests.helpers.composer_operations import message_body
from tests.unit.web.sessions.continuation_timeout_diagnostic import G1TimeoutProbe, _capture_error
from tests.unit.web.sessions.test_continuation_completion_owners import (
    _assistant_contents,
    _continuation_app,
    _effects,
    _end_admit,
    _LateToolSDK,
    _seed_csv_session,
    _submit_and_settle,
    _tool_call,
)

_CONTROL_ID = "c" * 256
_RAW_CONTROL = ' {\n "notes" : "  Continue the same productive request.  "\n}\n'


def _control(arguments: str = _RAW_CONTROL) -> dict:
    return {
        "id": _CONTROL_ID,
        "type": "function",
        "function": {"name": CONTINUATION_CONTROL_TOOL, "arguments": arguments},
    }


def _segment_rows(app, operation_id):
    with app.state.session_engine.connect() as conn:
        return tuple(conn.execute(select(segments).where(segments.c.operation_id == operation_id).order_by(segments.c.sequence)))


def _control_roots(app, operation_id):
    roots = {}
    with app.state.session_engine.connect() as conn:
        for checkpoint in conn.execute(
            select(checkpoints).where(checkpoints.c.operation_id == operation_id).order_by(checkpoints.c.cursor)
        ):
            root = decode_continuation_snapshot(
                read_chunk(conn, session_id=checkpoint.session_id, operation_id=operation_id, key=checkpoint.snapshot_chunk_key).decode(
                    "utf-8"
                )
            )
            assert (root.logical.session_id, root.logical.operation_id, root.cursor) == (
                checkpoint.session_id,
                operation_id,
                checkpoint.cursor,
            )
            event_query = select(events).where(
                events.c.session_id == checkpoint.session_id, events.c.operation_id == operation_id, events.c.sequence == checkpoint.cursor
            )
            if checkpoint.cursor == 0:
                # initialize_continuation writes the initial checkpoint but
                # no event; the journal schema permits only sequence > 0.
                assert checkpoint.kind == "transition"
                assert len(root.frames) == 1 and type(root.frames[0]) is InitializeFrame
                assert root.frames[0].ingress_phase == "reserved"
                assert conn.execute(event_query).one_or_none() is None
                continue
            # Every appended checkpoint ends at its last committed event.
            # Missing or duplicate positive-cursor joins remain hard failures.
            event = conn.execute(event_query).one()
            if event.tag not in ("checkpoint", "yielded"):
                continue
            payload = json.loads(read_chunk(conn, session_id=checkpoint.session_id, operation_id=operation_id, key=event.payload_chunk_key))
            owner = payload.get("owner", event.tag)
            if owner not in ("composer_control_intent", "composer_control_completion", "yielded"):
                continue
            assert owner not in roots
            roots[owner] = root
    return roots


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_read", [False, True])
async def test_paid_control_yields_once_then_cold_primary_sees_original_call_and_constant(tmp_path, monkeypatch, prior_read):
    app_ref = []
    words = "The same request continued after its retained checkpoint."
    sdk = _LateToolSDK(
        app_ref,
        [
            *(
                [("primary", lambda app: ("Checking the current state.", [_tool_call(app, "read_1", "get_pipeline_state", {})]))]
                if prior_read
                else []
            ),
            ("primary", ("Continuing this request.", [_control()])),
            ("primary", (words, None)),
            ("end", _end_admit()),
        ],
    )
    monkeypatch.setattr(litellm, "acompletion", sdk)
    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        app_ref.append(app)
        probe = G1TimeoutProbe(monkeypatch, app, sdk, session_id, operation_id)
        try:
            snapshot = await _submit_and_settle(
                app, token, session_id, message_body("Continue explaining this pipeline.", operation_id=operation_id)
            )
        except AssertionError as original:
            if str(original) == "Continuation request did not settle":
                try:
                    observed = probe.capture()
                except Exception as diagnostic_failure:
                    original.add_note(f"G1 timeout observation failed: {_capture_error(diagnostic_failure)!r}")
                else:
                    original.add_note(f"G1 timeout owners (no payload): {observed!r}")
            raise
        try:
            assert snapshot.status == "completed", snapshot.error
        except AssertionError as original:
            try:
                observed = probe.capture()
            except Exception as diagnostic_failure:
                original.add_note(f"G1 terminal observation failed: {_capture_error(diagnostic_failure)!r}")
            else:
                original.add_note(f"G1 terminal owners (no payload): {observed!r}")
            raise
        assert snapshot.result is not None
        assert [request["role"] for request in sdk.requests] == ["primary"] * (3 if prior_read else 2) + ["end"]
        assert not sdk.steps
        predecessor, successor = _segment_rows(app, operation_id)
        assert (predecessor.sequence, predecessor.state, predecessor.rollover_reason) == (1, "yielded", "model_request")
        assert (successor.sequence, successor.predecessor_sequence, successor.state) == (2, 1, "settled")
        assert successor.generation == predecessor.generation + 1
        assert successor.policy_sha256 == predecessor.policy_sha256
        effects = {row.effect_key: row for row in _effects(app, operation_id)}
        assert {key for key in effects if key.startswith("provider:")} == {f"provider:0:{index}" for index in range(3 if prior_read else 2)}
        assert [effects[f"provider:0:{index}"].creator_sequence for index in range(3 if prior_read else 2)] == (
            [1, 1, 2] if prior_read else [1, 2]
        )
        assert all(row.disposition == "committed" for row in effects.values())
        assert any(key.startswith("tool:") for key in effects) is prior_read
        roots = _control_roots(app, operation_id)
        before = roots["composer_control_intent"].frames[0]
        completed = roots["composer_control_completion"].frames[0]
        yielded = roots["yielded"].frames[0]
        counters = {
            "composition_total",
            "discovery_total",
            "advisor_calls",
            "repair_turns",
            "advisor_checkpoint_passes",
            "mutation_success_seen",
        }
        assert before.model_dump(include=counters) == completed.model_dump(include=counters) == yielded.model_dump(include=counters)
        assert yielded.slice_sequence == 2 and yielded.composition_slice == yielded.discovery_slice == 0
        assert (
            roots["composer_control_intent"].semantic_frontier
            == roots["composer_control_completion"].semantic_frontier
            == roots["yielded"].semantic_frontier
        )
        continuation_input = sdk.requests[2 if prior_read else 1]["messages"]
        original_calls = [
            call
            for message in continuation_input
            if message["role"] == "assistant"
            for call in message.get("tool_calls", ())
            if call["id"] == _CONTROL_ID
        ]
        assert original_calls == [_control()]
        replies = [message for message in continuation_input if message["role"] == "tool" and message["tool_call_id"] == _CONTROL_ID]
        assert len(replies) == 1 and replies[0]["content"] == CONTINUATION_CONTROL_RESULT
        with app.state.session_engine.connect() as conn:
            human = tuple(
                conn.execute(select(messages.c.content).where(messages.c.session_id == str(session_id), messages.c.role == "user"))
            )
        assert human == (("Continue explaining this pipeline.",),)
        published = snapshot.result.message.content
        assert published.split("\n\n---\n\n")[0] == words
        assert _assistant_contents(app, session_id)[-1] == published
        assert CONTINUATION_CONTROL_RESULT not in _assistant_contents(app, session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("control_first", [False, True])
async def test_mixed_control_and_valid_mutation_refuse_every_call_before_effects(tmp_path, monkeypatch, control_first):
    from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline

    app_ref, session_ref = [], []

    def mixed(app):
        mutation = _tool_call(app, "mutation_1", "set_pipeline", _pipeline(tmp_path, str(session_ref[0])))
        calls = [_control(), mutation] if control_first else [mutation, _control()]
        return "A refused mixed batch.", calls

    sdk = _LateToolSDK(app_ref, [("primary", mixed), ("primary", ("The mixed batch changed nothing.", None)), ("end", _end_admit())])
    monkeypatch.setattr(litellm, "acompletion", sdk)
    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        app_ref.append(app)
        session_ref.append(session_id)
        await _seed_csv_session(app, tmp_path, session_id, "auto_commit")
        snapshot = await _submit_and_settle(app, token, session_id, message_body("Inspect this pipeline.", operation_id=operation_id))
        assert snapshot.status == "completed", snapshot.error
        (segment,) = _segment_rows(app, operation_id)
        assert (segment.sequence, segment.state, segment.rollover_reason) == (1, "settled", None)
        assert {row.effect_key.split(":", 1)[0] for row in _effects(app, operation_id)} == {"provider", "end"}
        assert [request["role"] for request in sdk.requests] == ["primary", "primary", "end"] and not sdk.steps
        replies = [message for message in sdk.requests[1]["messages"] if message["role"] == "tool"]
        assert {message["tool_call_id"] for message in replies} == {_CONTROL_ID, "mutation_1"}
        assert all('"error_code":"CONTINUATION_CALL_NOT_ALONE"' in message["content"] for message in replies)


@pytest.mark.asyncio
async def test_stop_at_actual_model_yield_sql_refuses_successor_and_paid_replay(tmp_path, monkeypatch):
    sdk = _LateToolSDK([], [("primary", ("Checkpointing this request.", [_control()]))])
    monkeypatch.setattr(litellm, "acompletion", sdk)
    operation_id = str(uuid4())
    reached = []
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        repository = app.state.session_service.session_operation_authority
        authority = composer_app_services(app).composer_operation_authority
        original = type(repository).commit_checkpoint_and_successor

        def stop_then_yield(owner, running, **expectations):
            reached.append((running.claim.segment_sequence, expectations["reason"]))
            stopped = authority.request_cancel(session_id=session_id, operation_id=operation_id)
            assert stopped is not None and stopped.cancel_requested_at is not None
            return original(owner, running, **expectations)

        monkeypatch.setattr(type(repository), "commit_checkpoint_and_successor", stop_then_yield)
        snapshot = await _submit_and_settle(app, token, session_id, message_body("Continue this request.", operation_id=operation_id))
        assert reached == [(1, "model_request")]
        assert snapshot.status == "failed" and snapshot.error is not None
        assert snapshot.error.error_type == "request_cancelled"
        (segment,) = _segment_rows(app, operation_id)
        assert segment.sequence == 1 and segment.state == "settled"
        assert [request["role"] for request in sdk.requests] == ["primary"] and not sdk.steps
        assert {row.effect_key for row in _effects(app, operation_id)} == {"provider:0:0"}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"notes":"a","notes":"b"}', '{"notes":', '{"notes":""}'])
async def test_invalid_control_is_a_retained_normal_refusal_then_primary_continues(tmp_path, monkeypatch, arguments):
    sdk = _LateToolSDK(
        [],
        [
            ("primary", ("A refused control.", [_control(arguments)])),
            ("primary", ("The request continued safely.", None)),
            ("end", _end_admit()),
        ],
    )
    monkeypatch.setattr(litellm, "acompletion", sdk)
    operation_id = str(uuid4())
    async with _continuation_app(tmp_path, monkeypatch) as (app, token, session_id):
        snapshot = await _submit_and_settle(app, token, session_id, message_body("Explain this composer.", operation_id=operation_id))
        assert snapshot.status == "completed", snapshot.error
        (segment,) = _segment_rows(app, operation_id)
        assert segment.sequence == 1 and segment.state == "settled"
        assert {row.effect_key.split(":", 1)[0] for row in _effects(app, operation_id)} == {"provider", "end"}
        second = sdk.requests[1]["messages"]
        calls = [call for message in second if message["role"] == "assistant" for call in message.get("tool_calls", ())]
        assert calls == [_control(arguments)]
        replies = [message for message in second if message["role"] == "tool" and message["tool_call_id"] == _CONTROL_ID]
        assert len(replies) == 1 and '"error_code":"argument_error"' in replies[0]["content"]
        assert [request["role"] for request in sdk.requests] == ["primary", "primary", "end"] and not sdk.steps

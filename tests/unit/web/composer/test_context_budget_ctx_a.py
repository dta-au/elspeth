"""CTX-A: conservative admission and stable shedding regressions."""

import inspect
from copy import deepcopy
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.context_budget import (
    ContextBudgetPolicy,
    ContextBudgetRefusal,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
    escaped_bytes,
)
from elspeth.web.composer.context_budget_apply import request_wire_bytes
from elspeth.web.composer.context_budget_history import ContextBudgetHistory, request_anchor
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.service import _ContextBudgetRefusalScope
from tests.unit.web.composer.test_compose_loop_context_budget import _assistant, _empty_state, _service, _user
from tests.unit.web.composer.test_context_budget_history import _audit_row, _call
from tests.unit.web.composer.test_planner_context_budget import _earlier_call, _fit_hatch_request, _hatch_message


@pytest.mark.parametrize("count", [None, 0, 1, 1000000])
def test_invalid_anchor_counts_are_discarded(count):
    messages = [{"role": "system", "content": "normal prose " * 200}]
    call = replace(_earlier_call(()), prompt_tokens=count, private_request=capture_provider_context(messages=messages, tools=None))
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=messages, tools=None, mutable_positions=frozenset()
    )
    assert anchor.tokens is None
    assert anchor.reason == "invalid_prompt_count"


def test_anchor_excludes_mutable_content_and_matches_duplicate_messages_once():
    messages = [{"role": "system", "content": "normal prose " * 200}, {"role": "user", "content": "a" * 300}]
    call = replace(_earlier_call(()), prompt_tokens=500, private_request=capture_provider_context(messages=messages, tools=None))
    current = [*messages, messages[0]]
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=current, tools=None, mutable_positions=frozenset({1})
    )
    assert anchor.tokens == 500
    assert anchor.shared_bytes == request_wire_bytes(messages[:1], None) - len('{"messages":[],"tools":}')
    changed = request_anchor(
        calls=[call],
        model=call.model_requested,
        dialect=ToolContractDialect.NONE,
        messages=current,
        tools=None,
        mutable_positions=frozenset(),
    )
    assert changed.tokens is None
    assert changed.reason == "model_or_dialect_changed"


@pytest.mark.asyncio
async def test_live_compose_second_call_anchors_and_records_replay_figures(monkeypatch):
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
    from tests.unit.web.composer.test_compose_loop_context_budget import _compose, _RecordingProvider
    from tests.unit.web.composer.test_compose_loop_llm_audit import _make_llm_response

    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _service(1_000_000)
    provider = _RecordingProvider(
        _make_llm_response(
            tool_calls=[{"id": "anchor-call", "name": "set_metadata", "arguments": {"patch": {"name": "anchor"}}}], prompt_tokens=60000
        ),
        _make_llm_response(content="Done.", prompt_tokens=61000),
    )
    result = await _compose(service, session_id, [], provider)
    first, second = (call.context_budget for call in result.llm_calls[:2])
    assert first is not None and second is not None
    assert first.pricing_mode == "authored_prefix"
    assert second.pricing_mode == "anchored"
    assert second.anchor_prompt_tokens == 60000
    assert second.anchor_shared_bytes > 100000
    assert second.estimated_tokens_before == min(
        second.decision_wire_bytes, 60000 + second.decision_wire_bytes - second.anchor_shared_bytes
    )
    assert second.estimated_tokens_after == min(
        second.decision_wire_bytes_after, 60000 + second.decision_wire_bytes_after - second.anchor_shared_bytes
    )
    public = second.to_dict()
    assert public["anchor_prompt_tokens"] == 60000


def policy(cap=1000, byte_cap=1000000, high=0.85):
    return ContextBudgetPolicy(cap, byte_cap, high, 0.6)


def test_provider_anchor_prices_growth_without_average_ratio_credit():
    assert "anchor_tokens" in inspect.signature(decide_context_budget).parameters
    result = decide_context_budget(
        policy=policy(),
        request_bytes=4000,
        segments=[],
        previously_shed=frozenset(),
        anchor_tokens=500,
        anchor_shared_bytes=3700,
    )
    assert result.estimated_tokens_before == 800


def test_anchor_arrival_and_loss_never_release_sticky_history():
    assert "anchor_tokens" in inspect.signature(decide_context_budget).parameters
    segments = [SheddableSegment(SheddableKind.USER_REQUEST, "history:0", 300)]
    previous = frozenset({"history:0"})
    for anchor_tokens, shared in [(None, 0), (100, 600), (None, 0)]:
        result = decide_context_budget(
            policy=policy(),
            request_bytes=900,
            segments=segments,
            previously_shed=previous,
            anchor_tokens=anchor_tokens,
            anchor_shared_bytes=shared,
        )
        assert result.shed_keys == ("history:0",)
        previous = frozenset(result.shed_keys)


def test_anchor_cannot_overlap_sheddable_bytes():
    assert "anchor_tokens" in inspect.signature(decide_context_budget).parameters
    with pytest.raises(ValueError, match="shared"):
        decide_context_budget(
            policy=policy(),
            request_bytes=900,
            segments=[SheddableSegment(SheddableKind.USER_REQUEST, "a", 400)],
            previously_shed=frozenset(),
            anchor_tokens=100,
            anchor_shared_bytes=600,
        )


@given(st.lists(st.integers(1, 500), max_size=10), st.integers(1, 2000), st.integers(1, 2000), st.integers(1, 2000))
def test_anchored_decision_properties(savings, protected, count, cap):
    segments = tuple(SheddableSegment(SheddableKind.USER_REQUEST, str(i), saving) for i, saving in enumerate(savings))
    original = deepcopy(segments)
    arguments = {
        "policy": policy(cap),
        "request_bytes": protected + sum(savings),
        "segments": segments,
        "previously_shed": frozenset(),
        "anchor_tokens": count,
        "anchor_shared_bytes": protected,
        # Known evidence: the anchor carried no authored bytes. Omitting the
        # argument means unknown evidence, which prices the anchored arm alone.
        "anchor_authored_bytes": 0,
    }
    try:
        result = decide_context_budget(**arguments)
    except ContextBudgetRefusal as refusal:
        assert refusal.request_bytes == protected
        assert min(count, protected) > cap
        with pytest.raises(ContextBudgetRefusal):
            decide_context_budget(**arguments)
    else:
        assert result.estimated_tokens_after <= cap
        assert result == decide_context_budget(**arguments)
    assert segments == original


def test_dense_growth_after_prose_anchor_is_priced_per_byte():
    messages = [{"role": "system", "content": "Read the input data. " * 100}]
    call = replace(_earlier_call(()), prompt_tokens=500, private_request=capture_provider_context(messages=messages, tools=None))
    current = [*messages, {"role": "user", "content": "0123456789abcdef" * 100}]
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=current, tools=None, mutable_positions=frozenset()
    )
    with pytest.raises(ContextBudgetRefusal):
        decide_context_budget(
            policy=policy(),
            request_bytes=request_wire_bytes(current, None),
            segments=[],
            previously_shed=frozenset(),
            anchor_tokens=anchor.tokens,
            anchor_shared_bytes=anchor.shared_bytes,
        )


def decide(size, segments=(), previous=frozenset(), held=frozenset(), budget=None):
    return decide_context_budget(policy=budget or policy(), request_bytes=size, segments=segments, previously_shed=previous, held=held)


def test_dense_payload_has_byte_upper_bound():
    result = decide(900)
    assert result.estimated_tokens_before >= 900


def test_ratio_step_does_not_regrow_fifteen_messages():
    segments = [SheddableSegment(SheddableKind.ASSISTANT_PROSE, f"history:{i}", 6000) for i in range(20)]
    # The review's first turn at ratio 3 shed fifteen. Replay its persisted set.
    previous = frozenset(f"history:{i}" for i in range(15))
    result = decide(270000, segments, previous, budget=policy(300000))
    assert previous <= set(result.shed_keys)


def test_three_turn_ratio_step_keeps_the_same_yardstick():
    segments = [SheddableSegment(SheddableKind.ASSISTANT_PROSE, f"history:{i}", 6000) for i in range(20)]
    # Scale the review's cap by three for the new one-byte yardstick.
    first = decide(270000, segments, budget=policy(300000))
    second = decide(270000, segments, frozenset(first.shed_keys), budget=policy(300000))
    third = decide(400000, segments, frozenset(second.shed_keys), budget=policy(300000))
    assert len(first.shed_keys) == 15
    assert second.shed_keys == first.shed_keys
    assert len(third.shed_keys) == 20


def test_unreachable_low_mark_preserves_recent_history():
    segments = [SheddableSegment(SheddableKind.USER_REQUEST, f"history:{i}", 1400) for i in range(40)]
    result = decide(330506, segments, budget=policy(384000))
    assert result.shed_keys == tuple(f"history:{i}" for i in range(3))
    assert result.estimated_tokens_after <= 326400


@pytest.mark.parametrize(
    "size,segments",
    [
        (90, [SheddableSegment(SheddableKind.USER_REQUEST, "a", 500)]),
        (90, [SheddableSegment(SheddableKind.USER_REQUEST, key, 50) for key in ("a", "b")]),
        (150, [SheddableSegment(SheddableKind.USER_REQUEST, "a", 50)] * 2),
    ],
)
def test_invalid_savings_rejected(size, segments):
    with pytest.raises(ValueError):
        decide(size, segments)


@pytest.mark.parametrize(
    "size,cap,byte_cap,high", [(850, 1000, 10000, 0.85), (1000, 1000, 10000, 1.0), (700, 1000, 700, 0.85), (500, 10000, 500, 0.85)]
)
def test_equality_is_admitted_without_shedding(size, cap, byte_cap, high):
    result = decide(size, [SheddableSegment(SheddableKind.USER_REQUEST, "a", 300)], budget=policy(cap, byte_cap, high))
    assert result.shed_keys == ()


def test_byte_equality_stops_shedding_and_held_is_not_counted_twice():
    segments = [SheddableSegment(SheddableKind.TOOL_RESULT, str(i), 100) for i in range(3)]
    result = decide(700, segments, held=frozenset({"0"}), budget=policy(10000, 500))
    assert result.shed_keys == ("0", "1")
    assert result.request_bytes_after == 500


def test_non_ascii_wire_bytes():
    assert escaped_bytes("界") == 3


def test_release_at_byte_ceiling_equality():
    result = decide(500, [SheddableSegment(SheddableKind.USER_REQUEST, "a", 100)], previous=frozenset({"a"}), budget=policy(1000, 500))
    assert result.shed_keys == ()


@given(
    st.lists(st.integers(1, 500), max_size=12),
    st.integers(0, 2000),
    st.integers(1, 2000),
    st.integers(1, 2000),
    st.sets(st.integers(0, 11)),
    st.sets(st.integers(0, 11)),
)
def test_decision_properties(savings, protected, cap, byte_cap, previous_indices, held_indices):
    segments = [SheddableSegment(SheddableKind.USER_REQUEST, str(i), n) for i, n in enumerate(savings)]
    original = deepcopy(segments)
    size = protected + sum(savings)
    budget = policy(cap, byte_cap)
    previous = frozenset(map(str, previous_indices))
    held = frozenset(map(str, held_indices))
    try:
        result = decide(size, segments, previous=previous, held=held, budget=budget)
    except ContextBudgetRefusal as exc:
        assert exc.request_bytes == protected
        assert protected > min(cap, byte_cap)
        with pytest.raises(ContextBudgetRefusal) as repeated:
            decide(size, segments, previous=previous, held=held, budget=budget)
        assert (repeated.value.ceiling, repeated.value.estimated_tokens, repeated.value.request_bytes) == (
            exc.ceiling,
            exc.estimated_tokens,
            exc.request_bytes,
        )
    else:
        assert result.request_bytes_after <= min(cap, byte_cap)
        assert result == decide(size, segments, previous=previous, held=held, budget=budget)
        assert held & {segment.key for segment in segments} <= set(result.shed_keys)
    assert segments == original


def test_hatch_without_history_carries_sticky_set():
    transcript, _ = _hatch_message([], [])
    _, record, _ = _fit_hatch_request(transcript, earlier_calls=(_earlier_call(("history:1",)),), policy=policy(1000000))
    assert record.shed_history_indices == (1,)
    call = _call()
    call["context_budget"] = record.to_dict()
    history = ContextBudgetHistory.from_records([_audit_row(0, _call(shed=[1])), _audit_row(1, call)])
    assert history.previously_shed_history == frozenset({"history:1"})


def test_history_uses_newest_envelope_within_an_audit_row():
    older = _audit_row(0, _call(shed=[0]))
    newer = _audit_row(1, _call(shed=[1]))
    assert older.tool_calls is not None and newer.tool_calls is not None
    row = replace(older, tool_calls=[*older.tool_calls, *newer.tool_calls])
    assert ContextBudgetHistory.from_records([row]).previously_shed_history == frozenset({"history:1"})


def test_non_audit_row_cannot_supply_history_state():
    row = _audit_row(0, _call(shed=[0]))
    assert ContextBudgetHistory.from_records([row]).previously_shed_history == frozenset({"history:0"})
    assert ContextBudgetHistory.from_records([replace(row, role="user")]) == ContextBudgetHistory.empty()


def test_hatch_does_not_reshed_history_released_by_the_latest_call():
    transcript, _ = _hatch_message([], [])
    _, record, _ = _fit_hatch_request(transcript, earlier_calls=(_earlier_call(("history:1",)), _earlier_call(())), policy=policy(1000000))
    assert record.shed_history_indices == ()


@pytest.mark.parametrize("with_history", [True, False])
def test_reply_does_not_reshed_history_released_by_the_loop(with_history):
    service, _ = _service(10000000)
    snapshot, catalog = service._policy_context.build("test-user")
    state = _empty_state()
    chat = [_user(0, "Keep CSV"), _assistant("Earlier reply")] if with_history else []
    messages = service._build_messages(chat, state, "Current request", plugin_snapshot=snapshot, policy_catalog=catalog)
    frame = service._context_budget_frame(
        chat_history=chat,
        llm_messages=messages,
        state=state,
        session_id=None,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        history=ContextBudgetHistory(frozenset({"history:1"})),
    )
    recorder = BufferingRecorder()
    recorder.record_llm_call(_earlier_call(()))
    applied, record, _ = service._shed_to_context_budget(
        messages=messages,
        tools=[],
        frame=frame,
        recorder=recorder,
        refusal_scope=_ContextBudgetRefusalScope(state, state.version, 0, 0, None, recorder),
        reply_stage=True,
    )
    assert record.shed_history_indices == ()
    assert any(m["content"] == "Earlier reply" for m in applied) == with_history


@pytest.mark.parametrize("with_history", [True, False])
def test_reply_holds_history_even_when_its_smaller_request_fits_low_mark(with_history):
    service, _ = _service(10000000)
    snapshot, catalog = service._policy_context.build("test-user")
    state = _empty_state()
    chat = [_user(0, "Keep CSV"), _assistant("Earlier reply")] if with_history else []
    messages = service._build_messages(chat, state, "Current request", plugin_snapshot=snapshot, policy_catalog=catalog)
    frame = service._context_budget_frame(
        chat_history=chat,
        llm_messages=messages,
        state=state,
        session_id=None,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        history=ContextBudgetHistory(frozenset({"history:1"})),
    )
    recorder = BufferingRecorder()
    applied, record, _ = service._shed_to_context_budget(
        messages=messages,
        tools=[],
        frame=frame,
        recorder=recorder,
        refusal_scope=_ContextBudgetRefusalScope(state, state.version, 0, 0, None, recorder),
        reply_stage=True,
    )
    assert record.shed_history_indices == (1,)
    assert any(m["content"] == "Current request" for m in applied)
    assert not any(m["content"] == "Earlier reply" for m in applied)


def test_newest_usable_anchor_survives_interleaved_model_and_failure():
    from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus

    messages = [{"role": "system", "content": "normal prose " * 200}]
    call = replace(_earlier_call(()), prompt_tokens=500, private_request=capture_provider_context(messages=messages, tools=None))
    newer = replace(call, prompt_tokens=600)
    for interleaved in (
        replace(call, model_requested="another/model"),
        replace(call, status=ComposerLLMCallStatus.API_ERROR, error_class="APIError", error_message="failed"),
    ):
        anchor = request_anchor(
            calls=[call, newer, interleaved],
            model=call.model_requested,
            dialect=None,
            messages=messages,
            tools=None,
            mutable_positions=frozenset(),
        )
        assert anchor.tokens == 600


def test_static_authorship_prices_only_explicitly_labelled_bytes():
    assert "authored_bytes" in inspect.signature(decide_context_budget).parameters
    result = decide_context_budget(policy=policy(), request_bytes=2000, segments=[], previously_shed=frozenset(), authored_bytes=1800)
    assert result.estimated_tokens_after == 800
    with pytest.raises(ContextBudgetRefusal):
        decide(2000)


@pytest.mark.parametrize("model", ["anthropic/claude-sonnet-4.5", "bedrock/converse/claude-sonnet-4.5", "openai/gpt-4o"])
@pytest.mark.parametrize("cache_field", ["cached_prompt_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
def test_anchor_does_not_double_count_normalized_cached_usage(model, cache_field):
    messages = [{"role": "system", "content": "normal prose " * 200}]
    call = replace(
        _earlier_call(()),
        model_requested=model,
        prompt_tokens=500,
        private_request=capture_provider_context(messages=messages, tools=None),
        **{cache_field: 200},
    )
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=messages, tools=None, mutable_positions=frozenset()
    )
    assert anchor.tokens == 500


@pytest.mark.parametrize("numerator,accepted", [(1, False), (2, True)])
def test_anchor_plausibility_eight_byte_boundary(numerator, accepted):
    messages = [{"role": "system", "content": "normal prose " * 200}]
    wire = request_wire_bytes(messages, None)
    count = (wire + 7) // 8 + numerator - 2
    call = replace(_earlier_call(()), prompt_tokens=count, private_request=capture_provider_context(messages=messages, tools=None))
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=messages, tools=None, mutable_positions=frozenset()
    )
    assert (anchor.tokens is not None) == accepted


def test_changed_tools_never_receive_anchor_credit():
    from elspeth.core.canonical import canonical_json

    messages = [{"role": "system", "content": "normal prose " * 200}]
    old_tools = [{"type": "function", "function": {"name": "set_metadata", "description": "original"}}]
    new_tools = [{"type": "function", "function": {"name": "set_metadata", "description": "changed"}}]
    call = replace(_earlier_call(()), prompt_tokens=500, private_request=capture_provider_context(messages=messages, tools=old_tools))
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=messages, tools=new_tools, mutable_positions=frozenset()
    )
    assert anchor.tokens == 500
    assert anchor.shared_bytes == len(canonical_json(messages[0]).encode())


def test_refusal_has_replayable_pricing_and_byte_arm_is_named():
    with pytest.raises(ContextBudgetRefusal) as caught:
        decide_context_budget(
            policy=policy(100),
            request_bytes=500,
            segments=[],
            previously_shed=frozenset(),
            anchor_tokens=400,
            anchor_shared_bytes=200,
            anchor_wire_bytes=2000,
            anchor_status="anchored",
            anchor_authored_bytes=0,
        )
    evidence = caught.value.pricing
    assert evidence is not None
    assert evidence.mode == "conservative"
    assert (evidence.anchor_tokens, evidence.anchor_wire_bytes, evidence.shared_bytes, evidence.request_bytes) == (400, 2000, 200, 500)


def test_real_shipped_first_request_fits_200000_with_tokenizer_margin():
    import math

    import tiktoken

    from elspeth.contracts.prompt_guard import detach_prompt_guard_messages
    from elspeth.core.canonical import canonical_json
    from elspeth.web.composer.prompts import SYSTEM_PROMPT
    from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
    from elspeth.web.dependencies import create_catalog_service
    from tests.unit.web.composer._helpers import _composer_service_with_session, _make_settings

    service, _ = _composer_service_with_session(create_catalog_service(), _make_settings(composer_context_max_prompt_tokens=200000))
    snapshot, catalog = service._policy_context.build("test-user")
    state = _empty_state()
    messages = service._build_messages([], state, "Build a CSV pipeline", plugin_snapshot=snapshot, policy_catalog=catalog)
    tools = composer_loop_tool_definitions(service._planner_dialect)
    frame = service._context_budget_frame(
        chat_history=[],
        llm_messages=messages,
        state=state,
        session_id=None,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        history=ContextBudgetHistory.empty(),
    )
    recorder = BufferingRecorder()
    _, record, _ = service._shed_to_context_budget(
        messages=messages,
        tools=tools,
        frame=frame,
        recorder=recorder,
        refusal_scope=_ContextBudgetRefusalScope(state, state.version, 0, 0, None, recorder),
    )
    encoder = tiktoken.get_encoding("o200k_base")
    wire = canonical_json({"messages": detach_prompt_guard_messages(messages)[0], "tools": tools})
    real = len(encoder.encode(wire))
    # o200k is an instrument for one tokenizer family, not a universal proof.
    prefix_real = len(encoder.encode(canonical_json(SYSTEM_PROMPT)[1:-1])) + len(encoder.encode(canonical_json(tools)))
    prefix_price = math.ceil(record.authored_bytes / 3)
    assert prefix_price >= math.ceil(prefix_real * 1.4)
    assert real <= record.estimated_tokens_after <= 200000
    print(
        f"wire={len(wire.encode())} real={real} estimate={record.estimated_tokens_after} prefix_real={prefix_real} prefix_price={prefix_price} margin_1.4={prefix_price - math.ceil(prefix_real * 1.4)}"
    )


@pytest.mark.parametrize("reply_stage", [False, True])
def test_compose_anchor_excludes_state_and_reply_messages(reply_stage):
    from elspeth.contracts.prompt_guard import detach_prompt_guard_messages
    from elspeth.core.canonical import canonical_json
    from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers, supports_anthropic_prompt_cache_markers
    from elspeth.web.composer.service import _reply_stage_messages

    service, _ = _service(1000000)
    snapshot, catalog = service._policy_context.build("test-user")
    state = _empty_state()
    chat = [_user(0, "Keep CSV"), _assistant("Earlier reply")]
    messages = service._build_messages(chat, state, "Current request", plugin_snapshot=snapshot, policy_catalog=catalog)
    frame = service._context_budget_frame(
        chat_history=chat,
        llm_messages=messages,
        state=state,
        session_id=None,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        history=ContextBudgetHistory.empty(),
    )
    sent = _reply_stage_messages(messages) if reply_stage else messages
    tools = None
    if supports_anthropic_prompt_cache_markers(service._model):
        sent, tools = apply_anthropic_cache_markers(sent, [], mark_history_tail=True)
    sent = detach_prompt_guard_messages(sent)[0]
    call = replace(
        _earlier_call(()),
        model_requested=service._model,
        tool_contract_dialect=service._planner_dialect,
        strict_tool_count=0,
        prompt_tokens=60000,
        private_request=capture_provider_context(messages=sent, tools=tools or None),
    )
    recorder = BufferingRecorder()
    recorder.record_llm_call(call)
    _, record, _ = service._shed_to_context_budget(
        messages=messages,
        tools=[],
        frame=frame,
        recorder=recorder,
        refusal_scope=_ContextBudgetRefusalScope(state, state.version, 0, 0, None, recorder),
        reply_stage=reply_stage,
    )
    assert record.anchor_prompt_tokens == 60000
    expected = 2 + sum(len(canonical_json(sent[i]).encode()) for i in ((0, 1) if reply_stage else (0, 1, 5)))
    assert record.anchor_shared_bytes == expected
    assert record.pricing_mode == "anchored"


@pytest.mark.parametrize("reply_stage", [False, True])
def test_freeform_authored_allowance_excludes_sheddable_labels(reply_stage):
    from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY, PromptGuardContentPart, PromptGuardTrust
    from tests.unit.web.composer.test_compose_loop_context_budget import _tool_round

    service, _ = _service(10_000_000)
    service._context_budget_policy = replace(service._context_budget_policy, fallback_bytes_per_token=3.0)
    snapshot, catalog = service._policy_context.build("test-user")
    state = _empty_state()
    chat = [_user(0, "Earlier request " * 100), _assistant("Earlier reply " * 100)]
    base = service._build_messages(chat, state, "Current request", plugin_snapshot=snapshot, policy_catalog=catalog)
    frame = service._context_budget_frame(
        chat_history=chat,
        llm_messages=base,
        state=state,
        session_id=None,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        history=ContextBudgetHistory.empty(),
    )
    messages = [*base, *_tool_round("old-result", "older schema " * 200), *_tool_round("latest-result", "latest schema " * 100)]
    history_index = next(
        frame.state_index - len(frame.history_entries) + position
        for position, (_chat_index, kind) in enumerate(frame.history_entries)
        if kind is not None
    )
    older_result_index = next(index for index, message in enumerate(messages) if message.get("tool_call_id") == "old-result")
    latest_result_index = next(index for index, message in enumerate(messages) if message.get("tool_call_id") == "latest-result")

    def allowance(labelled: tuple[int, ...]) -> int:
        candidate = deepcopy(messages)
        for index in labelled:
            candidate[index][PROMPT_GUARD_CONTENT_PARTS_KEY] = (
                PromptGuardContentPart(candidate[index]["content"], PromptGuardTrust.REFERENCE),
            )
        recorder = BufferingRecorder()
        _, record, _ = service._shed_to_context_budget(
            messages=candidate,
            tools=[],
            frame=frame,
            recorder=recorder,
            refusal_scope=_ContextBudgetRefusalScope(state, state.version, 0, 0, None, recorder),
            reply_stage=reply_stage,
        )
        assert record.bytes_per_token == repr(3.0)
        return record.authored_bytes

    baseline = allowance(())
    assert allowance((history_index, older_result_index)) == baseline
    assert allowance((history_index, older_result_index, latest_result_index)) > baseline


@pytest.mark.parametrize("hatch", [False, True])
def test_planner_and_hatch_anchor_exclude_request_history(hatch):
    from elspeth.contracts.prompt_guard import detach_prompt_guard_messages
    from elspeth.core.canonical import canonical_json
    from elspeth.web.composer.pipeline_planner import _fit_planner_context_budget, _hatch_transcript_message, _planner_request_message
    from tests.unit.web.composer.test_planner_context_budget import _context

    context = _context(users={0: "Keep CSV"})
    messages = [
        {"role": "system", "content": "normal prose " * 200},
        {"role": "user", "content": "reference"},
        _planner_request_message({"intent": "Convert", "conversation_context": context.to_dict()}),
    ]
    transcript = None
    if hatch:
        transcript, _ = _hatch_message([], [])
        messages.append(_hatch_transcript_message(transcript))
    sent = detach_prompt_guard_messages(messages)[0]
    call = replace(_earlier_call(()), prompt_tokens=700, private_request=capture_provider_context(messages=sent, tools=[]))
    _, record = _fit_planner_context_budget(
        messages=messages,
        tools=[],
        request_conversation_context=context,
        hatch_transcript_index=3 if hatch else None,
        hatch_transcript=transcript,
        policy=policy(10000),
        history=ContextBudgetHistory.empty(),
        model=call.model_requested,
        earlier_calls=[call],
        mark_for_cache=False,
    )
    expected = 2 + sum(len(canonical_json(message).encode()) for i, message in enumerate(sent) if i != 2)
    assert record.anchor_prompt_tokens == 700
    assert record.anchor_shared_bytes == expected
    assert record.pricing_mode == "anchored"


@pytest.mark.parametrize("content,reason", [("data:image/png;base64,abc", "unsupported"), ("sk-" + "z" * 48, "recognized_credential")])
def test_unavailable_private_capture_reason_is_recorded(content, reason):
    messages = [{"role": "user", "content": content}]
    call = replace(_earlier_call(()), prompt_tokens=30, private_request=capture_provider_context(messages=messages, tools=None))
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=messages, tools=None, mutable_positions=frozenset()
    )
    assert anchor.tokens is None
    assert anchor.reason == reason


def test_unreachable_low_mark_bounds_growth_churn_without_restoring_history():
    # Reproduce 28 further crossings at a 400k cap. The protected floor is
    # 250k estimated tokens, above the 240k low mark. Mutable content cannot
    # receive shared-byte credit even after a provider reports a small count.
    segments = [SheddableSegment(SheddableKind.USER_REQUEST, f"history:{i}", 1000) for i in range(100)]
    previous = frozenset()
    changes = 0
    for turn in range(29):
        result = decide_context_budget(
            policy=policy(400000),
            request_bytes=400000 + turn * 1000,
            segments=segments,
            previously_shed=previous,
            anchor_tokens=50000,
            anchor_shared_bytes=100000,
            anchor_wire_bytes=300000,
            anchor_status="anchored",
        )
        current = frozenset(result.shed_keys)
        assert previous <= current
        assert result.estimated_tokens_after == 340000
        assert len(current) == 10 + turn
        if turn and current != previous:
            changes += 1
        previous = current
    # This is the documented limit: continued growth can change the prefix on
    # every crossing; the guarantee is no restore/shed oscillation, not silence.
    assert changes == 28


def test_non_manifest_tool_disables_anchor_with_capture_reason():
    messages = [
        {"role": "user", "content": "normal prose " * 200},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-unknown", "type": "function", "function": {"name": "not_a_manifest_tool", "arguments": "{}"}}],
        },
    ]
    tools = []
    private = capture_provider_context(messages=messages, tools=tools)
    assert private.canonical is None
    call = replace(_earlier_call(()), prompt_tokens=500, private_request=private)
    anchor = request_anchor(
        calls=[call], model=call.model_requested, dialect=None, messages=messages, tools=tools, mutable_positions=frozenset()
    )
    assert anchor.tokens is None
    assert anchor.reason == "unsupported"


def test_compose_rejects_a_sheddable_position_missing_from_mutable_set(monkeypatch):
    from elspeth.web.composer import service as service_module

    service, _ = _service(10_000_000)
    snapshot, catalog = service._policy_context.build("test-user")
    state = _empty_state()
    chat = [_user(0, "Earlier request"), _assistant("Earlier reply")]
    messages = service._build_messages(chat, state, "Current request", plugin_snapshot=snapshot, policy_catalog=catalog)
    frame = service._context_budget_frame(
        chat_history=chat,
        llm_messages=messages,
        state=state,
        session_id=None,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        history=ContextBudgetHistory.empty(),
    )
    original = service_module._sheddable_message_positions

    def dropped_position(frame, tool_positions):
        positions = original(frame, tool_positions)
        positions.remove(2)
        return positions

    monkeypatch.setattr(service_module, "_sheddable_message_positions", dropped_position)
    with pytest.raises(InvariantError, match="outside mutable messages"):
        service._shed_to_context_budget(
            messages=messages,
            tools=[],
            frame=frame,
            recorder=BufferingRecorder(),
            refusal_scope=_ContextBudgetRefusalScope(state, state.version, 0, 0, None, BufferingRecorder()),
        )


def test_planner_rejects_a_sheddable_position_missing_from_mutable_set(monkeypatch):
    from elspeth.web.composer import pipeline_planner
    from tests.unit.web.composer.test_planner_context_budget import _context

    context = _context(users={0: "Earlier request"})
    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Reference"},
        pipeline_planner._planner_request_message({"intent": "Convert", "conversation_context": context.to_dict()}),
    ]
    original = pipeline_planner._planner_mutable_positions

    def dropped_position(tool_positions, history_index_by_key, hatch_index_by_key, hatch_transcript_index):
        positions = original(tool_positions, history_index_by_key, hatch_index_by_key, hatch_transcript_index)
        positions.remove(2)
        return positions

    monkeypatch.setattr(pipeline_planner, "_planner_mutable_positions", dropped_position)
    with pytest.raises(FrameworkBugError, match="outside mutable messages"):
        pipeline_planner._fit_planner_context_budget(
            messages=messages,
            tools=[],
            request_conversation_context=context,
            hatch_transcript_index=None,
            hatch_transcript=None,
            policy=policy(10_000_000),
            history=ContextBudgetHistory.empty(),
            model="test/model",
            earlier_calls=[],
            mark_for_cache=False,
        )


def test_omitted_anchor_authored_evidence_fails_closed_like_unknown():
    """F9-1: omitting the anchor's authored bytes must not mean a measured zero.

    Zero authored bytes price the anchor's own request at its full wire size,
    which no plausible count can exceed, so a contradictory provider count
    would be discarded. Omission must behave exactly as explicit unknown
    evidence: the anchored arm alone.
    """
    arguments = {
        "policy": ContextBudgetPolicy(90_000, 1_000_000, 0.85, 0.6),
        "request_bytes": 159_396,
        "segments": (),
        "previously_shed": frozenset(),
        "anchor_tokens": 100_000,
        "anchor_shared_bytes": 141_989,
        "anchor_wire_bytes": 142_015,
        "anchor_status": "anchored",
        "authored_bytes": 121_325,
    }
    with pytest.raises(ContextBudgetRefusal) as omitted:
        decide_context_budget(**arguments)
    with pytest.raises(ContextBudgetRefusal) as unknown:
        decide_context_budget(**arguments, anchor_authored_bytes=None)
    assert omitted.value.estimated_tokens == unknown.value.estimated_tokens == 117_407

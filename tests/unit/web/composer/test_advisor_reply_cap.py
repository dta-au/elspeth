"""An advisor reply cut off at its completion cap is reported as that, not as a model fault.

A checkpoint reply that stops at the cap (finish reason ``length``) before it
states a verdict keeps the ``malformed`` cause, but the operator-facing
detail names ``composer_advisor_max_completion_tokens`` and the cap that was
sent, and every advisor call record carries the cap it sent beside the
provider's finish reason.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict
from elspeth.web.composer.advisor_policy import (
    _ADVISOR_SIGNOFF_BLOCKED_CODE,
    ADVISOR_MALFORMED_USER_DETAIL,
    advisor_reply_cut_off_detail,
)
from tests.unit.web.composer import test_advisor_checkpoint as checkpoint_fixtures

make_service = checkpoint_fixtures.make_service
simple_state = checkpoint_fixtures.simple_state
clean_runnable_state = checkpoint_fixtures.clean_runnable_state

_CAP = 12_000
_SETTING = "composer_advisor_max_completion_tokens"
# A reply whose reasoning used the whole cap has no text; one cut mid-verdict has some.
_EMPTY = ""
_CUT = '{"verdict": "CLE'


def _transport(monkeypatch: pytest.MonkeyPatch, replies: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Replace only the external SDK; the advisor call boundary and its audit record stay live."""
    pending = iter(replies)
    captured: list[dict[str, Any]] = []

    async def complete(*, on_provider_dispatch: Callable[[], None] | None = None, **kwargs: Any) -> Any:
        if on_provider_dispatch is not None:
            on_provider_dispatch()
        captured.append(kwargs)
        content, finish_reason = next(pending)
        return SimpleNamespace(
            model="advisor-test-model",
            choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content, tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=_CAP, total_tokens=_CAP + 10),
        )

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", complete)
    return captured


async def _end_review(service: Any, state: Any) -> tuple[AdvisorCheckpointVerdict, Any]:
    recorder = checkpoint_fixtures.make_recorder()
    verdict = await service._advisor_checkpoint._run_advisor_checkpoint(
        phase="end",
        state=state,
        recorder=recorder,
        **checkpoint_fixtures._fenced_session(service, review_state=state),
    )
    return verdict, recorder


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [_EMPTY, _CUT], ids=["all-reasoning", "cut-mid-verdict"])
async def test_a_reply_cut_off_at_the_cap_names_the_setting_and_the_cap_sent(make_service, simple_state, monkeypatch, reply) -> None:
    captured = _transport(monkeypatch, [(reply, "length"), (reply, "length")])
    service = make_service(composer_advisor_max_completion_tokens=_CAP)

    verdict, recorder = await _end_review(service, simple_state)

    assert verdict.ok is False
    assert verdict.failure_class == "malformed"
    assert _SETTING in verdict.findings_text
    assert f"{_CAP:,}" in verdict.findings_text
    assert "ran out of room before a verdict" in verdict.findings_text
    assert "malformed" not in verdict.findings_text
    # The cap the message names is the cap that was sent, and the record says so.
    assert [kwargs["max_tokens"] for kwargs in captured] == [_CAP, _CAP]
    assert [(call.max_completion_tokens_requested, call.finish_reason) for call in recorder.llm_calls] == [
        (_CAP, "length"),
        (_CAP, "length"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [_EMPTY, _CUT], ids=["empty", "unparseable"])
async def test_a_malformed_reply_that_was_not_cut_off_keeps_the_malformed_detail(make_service, simple_state, monkeypatch, reply) -> None:
    _transport(monkeypatch, [(reply, "stop"), (reply, "stop")])
    service = make_service(composer_advisor_max_completion_tokens=_CAP)

    verdict, recorder = await _end_review(service, simple_state)

    assert verdict.failure_class == "malformed"
    assert verdict.findings_text == ADVISOR_MALFORMED_USER_DETAIL
    assert [(call.max_completion_tokens_requested, call.finish_reason) for call in recorder.llm_calls] == [
        (_CAP, "stop"),
        (_CAP, "stop"),
    ]


@pytest.mark.asyncio
async def test_the_detail_describes_the_last_reply(make_service, simple_state, monkeypatch) -> None:
    _transport(monkeypatch, [(_EMPTY, "length"), (_CUT, "stop")])
    service = make_service(composer_advisor_max_completion_tokens=_CAP)

    verdict, _recorder = await _end_review(service, simple_state)

    assert verdict.findings_text == ADVISOR_MALFORMED_USER_DETAIL


@pytest.mark.asyncio
async def test_the_completion_blocker_tells_the_operator_the_cap_ran_out(make_service, clean_runnable_state, monkeypatch) -> None:
    _transport(monkeypatch, [(_CUT, "length"), (_CUT, "length")])
    service = make_service(composer_advisor_max_completion_tokens=_CAP)
    verdict, recorder = await _end_review(service, clean_runnable_state)

    result = service._advisor_checkpoint._advisor_blocked_result(
        reason="malformed",
        verdict=verdict,
        state=clean_runnable_state,
        assistant_message=None,
        recorder=recorder,
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=None,
        outstanding_findings=None,
    )

    fact = result.advisor_gate_decision.fact
    assert fact.cause.value == "malformed"
    (blocker,) = [b for b in result.runtime_preflight.readiness.blockers if b.code == _ADVISOR_SIGNOFF_BLOCKED_CODE]
    for detail in (fact.detail, blocker.detail):
        assert _SETTING in detail
        assert f"{_CAP:,}" in detail
        assert "malformed" not in detail


def test_a_planner_continuation_review_names_the_planner_cap_that_limited_it() -> None:
    detail = advisor_reply_cut_off_detail(cap=_CAP, planner_cap_applied=True)
    assert f"{_CAP:,}" in detail
    assert _SETTING in detail
    assert "composer_planner_max_completion_tokens" in detail
    assert "composer_planner_max_completion_tokens" not in advisor_reply_cut_off_detail(cap=_CAP, planner_cap_applied=False)


def _call(**fields: Any) -> ComposerLLMCall:
    now = datetime.now(UTC)
    return ComposerLLMCall(
        model_requested="advisor-test-model",
        model_returned=None,
        status=ComposerLLMCallStatus.SUCCESS,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        latency_ms=0,
        provider_request_id=None,
        messages_hash="a" * 64,
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=now,
        finished_at=now,
        error_class=None,
        error_message=None,
        temperature=None,
        seed=None,
        **fields,
    )


def test_a_call_record_may_carry_the_cap_it_sent_without_planner_evidence() -> None:
    assert _call(max_completion_tokens_requested=_CAP).max_completion_tokens_requested == _CAP


@pytest.mark.parametrize(
    "fields",
    [
        {"planner_policy_hash": "b" * 64, "planner_call_ordinal": 1},
        {"max_completion_tokens_requested": _CAP, "planner_policy_hash": "b" * 64},
        {"max_completion_tokens_requested": _CAP, "planner_call_ordinal": 1},
    ],
    ids=["planner-without-cap", "hash-without-ordinal", "ordinal-without-hash"],
)
def test_planner_evidence_still_travels_whole(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="planner request evidence"):
        _call(**fields)

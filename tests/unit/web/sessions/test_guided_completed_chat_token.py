"""The occurrence token a COMPLETED guided session's chat channel is bound to.

``guided_turn_token`` binds a Send to the current *unanswered* turn. A finished
session has none, so its conversation is bound to the record that closed the
build instead: the answered STEP_4 ``confirm_wiring`` occurrence, whose
``response_hash`` IS the token. These tests pin what that function accepts and,
more importantly, everything it refuses — the token is the only thing standing
between a stale or forged Send and a provider call on a settled pipeline.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
from elspeth.web.composer.guided.state_machine import (
    GuidedSession,
    TerminalKind,
    TerminalReason,
    TerminalState,
    TurnRecord,
)
from elspeth.web.sessions.guided_replay import guided_completed_chat_token, guided_turn_token

_CONFIRMATION_HASH = "c" * 64
_WIRE_PAYLOAD_HASH = "a" * 64


def _answered_wire_record(**overrides: object) -> TurnRecord:
    return replace(
        TurnRecord(
            step=GuidedStep.STEP_4_WIRE,
            turn_type=TurnType.CONFIRM_WIRING,
            payload_hash=_WIRE_PAYLOAD_HASH,
            response_hash=_CONFIRMATION_HASH,
            emitter="server",
            summary="Guided pipeline wiring confirmed.",
        ),
        **overrides,  # type: ignore[arg-type]
    )


def _completed(**overrides: object) -> GuidedSession:
    return replace(
        GuidedSession(
            step=GuidedStep.STEP_4_WIRE,
            history=(
                TurnRecord(
                    step=GuidedStep.STEP_3_TRANSFORMS,
                    turn_type=TurnType.PROPOSE_PIPELINE,
                    payload_hash="b" * 64,
                    response_hash="d" * 64,
                    emitter="server",
                ),
                _answered_wire_record(),
            ),
            terminal=TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml="pipeline: {}\n"),
        ),
        **overrides,  # type: ignore[arg-type]
    )


def test_token_is_the_confirmation_response_hash() -> None:
    """No wire change: the token is a hash the session already carries."""

    guided = _completed()

    assert guided_completed_chat_token(guided) == _CONFIRMATION_HASH
    assert guided_completed_chat_token(guided) == guided.history[-1].response_hash


def test_token_is_stable_across_appended_chat_turns() -> None:
    """Asking a question must not invalidate the token for the next question."""

    guided = _completed()
    before = guided_completed_chat_token(guided)

    after = guided_completed_chat_token(replace(guided, step=GuidedStep.STEP_4_WIRE))

    assert after == before


def test_token_changes_when_a_different_confirmation_settled() -> None:
    other = _completed(history=(_answered_wire_record(response_hash="e" * 64),))

    assert guided_completed_chat_token(other) != _CONFIRMATION_HASH


def test_the_two_token_functions_never_both_accept_a_session() -> None:
    """The completed token exists precisely where the turn token refuses."""

    guided = _completed()

    with pytest.raises(AuditIntegrityError):
        guided_turn_token(guided)
    assert guided_completed_chat_token(guided) == _CONFIRMATION_HASH


def test_non_terminal_session_has_no_completed_token() -> None:
    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(_completed(terminal=None))


def test_exited_to_freeform_session_has_no_completed_token() -> None:
    exited = _completed(
        terminal=TerminalState(
            kind=TerminalKind.EXITED_TO_FREEFORM,
            reason=TerminalReason.USER_PRESSED_EXIT,
            pipeline_yaml=None,
        )
    )

    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(exited)


def test_empty_history_has_no_completed_token() -> None:
    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(_completed(history=()))


def test_unanswered_final_record_has_no_completed_token() -> None:
    """A completed terminal whose wire turn was never answered is tampering."""

    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(_completed(history=(_answered_wire_record(response_hash=None),)))


def test_unanswered_earlier_record_has_no_completed_token() -> None:
    tampered = _completed(
        history=(
            TurnRecord(
                step=GuidedStep.STEP_3_TRANSFORMS,
                turn_type=TurnType.PROPOSE_PIPELINE,
                payload_hash="b" * 64,
                response_hash=None,
                emitter="server",
            ),
            _answered_wire_record(),
        )
    )

    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(tampered)


def test_wrong_final_turn_type_has_no_completed_token() -> None:
    wrong_type = _completed(
        history=(
            replace(
                _answered_wire_record(),
                turn_type=TurnType.REVIEW_COMPONENTS,
                step=GuidedStep.STEP_1_SOURCE,
            ),
        )
    )

    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(wrong_type)


def test_wrong_final_step_has_no_completed_token() -> None:
    wrong_step = _completed(history=(replace(_answered_wire_record(), step=GuidedStep.STEP_3_TRANSFORMS),))

    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(wrong_step)


def test_inexact_session_type_is_refused() -> None:
    """A look-alike is not a GuidedSession (ADR-032 nominal typing)."""

    class _LooksLikeAGuidedSession:
        terminal = TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml="pipeline: {}\n")
        history = (_answered_wire_record(),)

    with pytest.raises(TypeError):
        guided_completed_chat_token(_LooksLikeAGuidedSession())  # type: ignore[arg-type]


def test_malformed_confirmation_hash_is_refused() -> None:
    """The hash rides to the client as a 64-hex turn_token; prove it is one."""

    guided = _completed()
    object.__setattr__(guided.history[-1], "response_hash", "NOT-A-HASH")

    with pytest.raises(AuditIntegrityError):
        guided_completed_chat_token(guided)

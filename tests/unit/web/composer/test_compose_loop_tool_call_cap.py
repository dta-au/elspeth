"""Tests for the per-turn tool-call cap (spec §1.4 NFR / §5.2.1 Step 0)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.sessions.telemetry import build_sessions_telemetry, observed_value


async def _run_one_turn(service: object, *, llm: object, session_id: str) -> Any:
    driver = cast(Any, service)
    return await driver._run_one_turn_for_test(llm=llm, session_id=session_id)


@pytest.mark.parametrize(
    ("malformed_kind", "expected_message"),
    [
        ("duplicate", "Composer tool batch contains duplicate provider tool-call IDs"),
        ("missing", "Composer tool batch is missing a provider tool-call ID"),
        ("non_string", "Composer tool batch contains a non-string provider tool-call ID"),
        ("blank", "Composer tool batch contains a blank provider tool-call ID"),
        ("oversized", "Composer tool batch contains an oversized provider tool-call ID"),
    ],
)
@pytest.mark.asyncio
async def test_over_cap_identity_violation_precedes_cap_telemetry_and_dispatch(
    fake_composer_service: object,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    malformed_kind: str,
    expected_message: str,
) -> None:
    """Identity admission wins over the cap for every malformed-ID class."""

    telemetry = build_sessions_telemetry()
    fake_composer_service._telemetry = telemetry  # type: ignore[attr-defined]
    fake_composer_service._max_tool_calls_per_turn = 16  # type: ignore[attr-defined]
    tool_calls = [
        SimpleNamespace(
            id=f"call_{index}",
            function=SimpleNamespace(name="get_pipeline_state", arguments="{}"),
        )
        for index in range(17)
    ]
    if malformed_kind == "duplicate":
        tool_calls[1].id = tool_calls[0].id
    elif malformed_kind == "missing":
        del tool_calls[0].id
    elif malformed_kind == "non_string":
        tool_calls[0].id = 7
    elif malformed_kind == "blank":
        tool_calls[0].id = "\u2003"
    elif malformed_kind == "oversized":
        tool_calls[0].id = "x" * 257
    else:
        raise AssertionError(f"unhandled malformed kind: {malformed_kind}")

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=tool_calls,
                )
            )
        ]
    )

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return response

    handler_calls: list[str] = []

    def _unexpected_handler(tool_name: str, *args: Any, **kwargs: Any) -> Any:
        handler_calls.append(tool_name)
        raise AssertionError("identity-invalid over-cap batch reached tool dispatch")

    monkeypatch.setattr(tool_batch_module, "execute_tool", _unexpected_handler)
    caught: BaseException | None = None
    try:
        await _run_one_turn(
            fake_composer_service,
            llm=_fake_llm,
            session_id=result_session_id,
        )
    except BaseException as exc:
        caught = exc

    assert (
        type(caught),
        str(caught),
        observed_value(telemetry.tool_call_cap_exceeded_total),
        handler_calls,
    ) == (
        AuditIntegrityError,
        expected_message,
        0,
        [],
    )


@pytest.mark.asyncio
async def test_cap_exceeded_raises_before_any_tool_execution(
    fake_composer_service: object,
    fake_llm_emitting_n_tool_calls: Any,
    result_session_id: str,
) -> None:
    """The compose loop rejects over-cap turns before dispatching tools."""

    fake_llm = fake_llm_emitting_n_tool_calls(n=17)
    fake_composer_service._max_tool_calls_per_turn = 16  # type: ignore[attr-defined]

    with pytest.raises(ComposerConvergenceError) as excinfo:
        await _run_one_turn(fake_composer_service, llm=fake_llm, session_id=result_session_id)

    assert excinfo.value.reason == "tool_call_cap_exceeded"
    assert excinfo.value.evidence["observed"] == 17
    assert excinfo.value.evidence["cap"] == 16
    assert fake_llm.execute_tool_invocations == 0


@pytest.mark.asyncio
async def test_cap_exceeded_increments_counter(
    fake_composer_service: object,
    fake_llm_emitting_n_tool_calls: Any,
    result_session_id: str,
) -> None:
    """The cap breach increments the composer tool-call-cap counter."""

    telemetry = build_sessions_telemetry()
    fake_composer_service._telemetry = telemetry  # type: ignore[attr-defined]
    fake_composer_service._max_tool_calls_per_turn = 16  # type: ignore[attr-defined]
    fake_llm = fake_llm_emitting_n_tool_calls(n=17)

    with pytest.raises(ComposerConvergenceError):
        await _run_one_turn(fake_composer_service, llm=fake_llm, session_id=result_session_id)

    assert observed_value(telemetry.tool_call_cap_exceeded_total) == 1


@pytest.mark.asyncio
async def test_cap_not_exceeded_does_not_increment(
    fake_composer_service: object,
    fake_llm_emitting_n_tool_calls: Any,
    result_session_id: str,
) -> None:
    """At-cap turns are allowed and do not increment the cap counter."""

    telemetry = build_sessions_telemetry()
    fake_composer_service._telemetry = telemetry  # type: ignore[attr-defined]
    fake_composer_service._max_tool_calls_per_turn = 16  # type: ignore[attr-defined]
    fake_llm = fake_llm_emitting_n_tool_calls(n=16)

    await _run_one_turn(fake_composer_service, llm=fake_llm, session_id=result_session_id)

    assert observed_value(telemetry.tool_call_cap_exceeded_total) == 0

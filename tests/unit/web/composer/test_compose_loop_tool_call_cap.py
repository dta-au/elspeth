"""Tests for the per-turn tool-call cap (spec §1.4 NFR / §5.2.1 Step 0)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import provider_gateway
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry, observed_value
from tests.unit.web.composer._helpers import _empty_state

_UNSET = object()


def _real_litellm_tool_call(
    *,
    call_id: Any = "call_real_1",
    omit_id: bool = False,
    omit_function: bool = False,
    function: Any = _UNSET,
) -> Any:
    """Build a genuine ``litellm.types.utils.ChatCompletionMessageToolCall``.

    Not a dataclass stand-in. The real class declares NO pydantic model
    fields — ``id``/``type``/``function`` live in ``__pydantic_extra__`` and
    resolve only through ``BaseModel.__getattr__`` — which is exactly the
    access pattern the admission guard has to survive and which the typed
    fakes in ``_helpers.py`` cannot reproduce.

    Two shaping notes, both forced by the real class rather than chosen:

    * ``id`` cannot simply be omitted — the constructor substitutes a fresh
      UUID. ``omit_id`` therefore pops it back out of ``__pydantic_extra__``
      afterwards, producing an instance on which ``.id`` genuinely raises
      ``AttributeError`` through ``__getattr__``. That is the exact shape
      the ``_MISSING_TOOL_CALL_FIELD`` sentinel path exists to catch.
    * ``function`` is stored verbatim with NO validation, so a wrong-typed
      envelope (``None``, a bare string, or one carrying non-``str``
      ``name``/``arguments``) is passed in directly. ``Function`` itself
      coerces/validates those, so it cannot express the malformed cases.
      ``omit_function`` pops it the same way ``omit_id`` does, reaching the
      guard's other ``_MISSING_TOOL_CALL_FIELD`` branch (the one where the
      whole envelope is absent, not merely wrong-typed).

    ``litellm`` is imported inside the callable: it is a seconds-scale
    import and this is a unit-suite module.
    """
    from litellm.types.utils import ChatCompletionMessageToolCall, Function

    if function is _UNSET:
        function = Function(name="get_pipeline_state", arguments="{}")
    tool_call = ChatCompletionMessageToolCall(id=call_id, type="function", function=function)
    if omit_id:
        tool_call.__pydantic_extra__.pop("id")
    if omit_function:
        tool_call.__pydantic_extra__.pop("function")
    return tool_call


def test_tool_batch_admission_accepts_a_real_litellm_tool_call() -> None:
    """Regression for elspeth-9ea866438b: a REAL provider tool call is admitted.

    The guard previously probed with ``runtime_checkable`` ``Protocol``
    ``isinstance()`` checks. Since Python 3.12 those resolve members via
    ``inspect.getattr_static``, which bypasses ``__getattr__`` — so every
    genuine LiteLLM tool call, from every provider, was rejected as
    "missing a provider tool-call ID" while the suite's dataclass fakes
    (real attributes, statically resolvable) sailed through. This test is
    built from the real object precisely so that divergence cannot recur.
    """
    batch = tool_batch_module._admit_tool_batch((_real_litellm_tool_call(),))

    assert batch.call_ids == frozenset({"call_real_1"})
    assert len(batch.calls) == 1
    admitted = batch.calls[0]
    assert admitted.id == "call_real_1"
    assert admitted.function.name == "get_pipeline_state"
    assert admitted.function.arguments == "{}"


@pytest.mark.parametrize(
    ("kwargs", "expected_message"),
    (
        ({"omit_id": True}, "Composer tool batch is missing a provider tool-call ID"),
        ({"call_id": 7}, "Composer tool batch contains a non-string provider tool-call ID"),
        ({"call_id": "  "}, "Composer tool batch contains a blank provider tool-call ID"),
        ({"call_id": "x" * 257}, "Composer tool batch contains an oversized provider tool-call ID"),
        ({"omit_function": True}, "Composer tool batch contains malformed provider function metadata"),
        ({"function": None}, "Composer tool batch contains malformed provider function metadata"),
        ({"function": "not-an-envelope"}, "Composer tool batch contains malformed provider function metadata"),
        (
            {"function": SimpleNamespace(name="get_pipeline_state", arguments={"not": "a string"})},
            "Composer tool batch contains malformed provider function metadata",
        ),
        (
            {"function": SimpleNamespace(name=12, arguments="{}")},
            "Composer tool batch contains malformed provider function metadata",
        ),
        (
            {"function": SimpleNamespace(name=" ", arguments="{}")},
            "Composer tool batch contains blank provider function name",
        ),
    ),
)
def test_tool_batch_admission_still_rejects_malformed_real_litellm_tool_calls(
    kwargs: dict[str, Any],
    expected_message: str,
) -> None:
    """The Tier-3 posture is value-based, and survives on the REAL object.

    Admitting ``__getattr__``-resolved fields widened the *mechanism* the
    guard accepts, never the *values*: a missing field, a non-``str`` ID, a
    blank/oversized ID, a wrong-typed function envelope and non-``str``
    name/arguments are each still a hard ``AuditIntegrityError``, proven
    here against genuine ``ChatCompletionMessageToolCall`` instances rather
    than fakes.
    """
    with pytest.raises(AuditIntegrityError, match=expected_message):
        tool_batch_module._admit_tool_batch((_real_litellm_tool_call(**kwargs),))


def _raw_response_with_tool_calls(tool_calls: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=tool_calls))],
    )


def _provider_tool_call(call_id: str, name: str, arguments: str = "{}") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _enforce_cap(service: ComposerServiceImpl, *, observed: int, recorder: BufferingRecorder) -> None:
    """Drive the shared cap owner exactly as Continuation batch initialization does.

    ``continuation_live_phases._initialize_batch`` calls
    ``ComposerServiceImpl._enforce_tool_call_cap`` with the retained primary
    completion's admitted calls before any ``BatchAccumulator`` exists or any
    tool is dispatched.
    """
    completion = provider_gateway._admit_composer_llm_completion(
        _raw_response_with_tool_calls([_provider_tool_call(f"call_{index}", "get_pipeline_state") for index in range(observed)])
    )
    service._enforce_tool_call_cap(
        assistant_tool_calls=completion.tool_batch.calls,
        state=_empty_state(),
        initial_version=1,
        composition_turns_used=0,
        discovery_turns_used=0,
        recorder=recorder,
        failed_turn=None,
        persisted_tool_call_turn=False,
    )


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
    fake_composer_service: ComposerServiceImpl,
    monkeypatch: pytest.MonkeyPatch,
    malformed_kind: str,
    expected_message: str,
) -> None:
    """Identity admission wins over the cap for every malformed-ID class.

    The primary audit body (``_call_llm_outcome_with_audit_body``) is the one
    Continuation's ``continuation_primary`` reaches through
    ``_call_llm_outcome_with_audit``. It admits the tool batch before a
    completion exists, so the cap owner and tool dispatch are never reached.
    """

    telemetry = build_sessions_telemetry()
    fake_composer_service._telemetry = telemetry
    fake_composer_service._max_tool_calls_per_turn = 16
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

    response = _raw_response_with_tool_calls(tool_calls)

    async def _fake_transport(**_kwargs: Any) -> Any:
        return response

    handler_calls: list[str] = []

    def _unexpected_handler(tool_name: str, *args: Any, **kwargs: Any) -> Any:
        handler_calls.append(tool_name)
        raise AssertionError("identity-invalid over-cap batch reached tool dispatch")

    monkeypatch.setattr(tool_batch_module, "execute_tool", _unexpected_handler)
    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", _fake_transport)
    recorder = BufferingRecorder()
    caught: BaseException | None = None
    try:
        await fake_composer_service._provider_gateway._call_llm_with_audit(
            [{"role": "user", "content": "Build it."}],
            [],
            timeout=5,
            recorder=recorder,
        )
    except BaseException as exc:
        caught = exc

    assert (
        type(caught),
        str(caught),
        type(caught.__cause__) if caught is not None else None,
        str(caught.__cause__) if caught is not None else None,
        observed_value(telemetry.tool_call_cap_exceeded_total),
        handler_calls,
    ) == (
        provider_gateway._MalformedLLMResponseError,
        f"LLM tool batch failed admission: {expected_message}",
        AuditIntegrityError,
        expected_message,
        0,
        [],
    )
    attached_calls = caught.__dict__.get("llm_calls")
    assert type(attached_calls) is tuple
    assert len(attached_calls) == 1
    assert type(attached_calls[0]) is ComposerLLMCall
    assert attached_calls[0].status is ComposerLLMCallStatus.MALFORMED_RESPONSE
    assert attached_calls[0].error_message == "malformed_response"
    assert recorder.llm_calls == attached_calls


@pytest.mark.asyncio
async def test_cap_exceeded_raises_before_any_tool_execution(
    fake_composer_service: ComposerServiceImpl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared cap owner rejects an over-cap batch before dispatching tools."""

    fake_composer_service._max_tool_calls_per_turn = 16
    handler_calls: list[str] = []

    def _unexpected_handler(tool_name: str, *args: Any, **kwargs: Any) -> Any:
        handler_calls.append(tool_name)
        raise AssertionError("over-cap batch reached tool dispatch")

    monkeypatch.setattr(tool_batch_module, "execute_tool", _unexpected_handler)

    with pytest.raises(ComposerConvergenceError) as excinfo:
        _enforce_cap(fake_composer_service, observed=17, recorder=BufferingRecorder())

    assert excinfo.value.reason == "tool_call_cap_exceeded"
    assert excinfo.value.evidence["observed"] == 17
    assert excinfo.value.evidence["cap"] == 16
    assert handler_calls == []


def test_cap_exceeded_increments_counter(fake_composer_service: ComposerServiceImpl) -> None:
    """The cap breach increments the composer tool-call-cap counter."""

    telemetry = build_sessions_telemetry()
    fake_composer_service._telemetry = telemetry
    fake_composer_service._max_tool_calls_per_turn = 16

    with pytest.raises(ComposerConvergenceError):
        _enforce_cap(fake_composer_service, observed=17, recorder=BufferingRecorder())

    assert observed_value(telemetry.tool_call_cap_exceeded_total) == 1


def test_cap_not_exceeded_does_not_increment(fake_composer_service: ComposerServiceImpl) -> None:
    """At-cap batches are allowed and do not increment the cap counter."""

    telemetry = build_sessions_telemetry()
    fake_composer_service._telemetry = telemetry
    fake_composer_service._max_tool_calls_per_turn = 16

    _enforce_cap(fake_composer_service, observed=16, recorder=BufferingRecorder())

    assert observed_value(telemetry.tool_call_cap_exceeded_total) == 0

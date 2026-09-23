"""The pipeline planner's per-route tool-contract dialect (S1 T7).

Plan: ``docs/plans/2026-09-23-composer-strict-tool-contracts-s1.md`` T7.

``PlannerModelConfig`` carries one dialect for the ordinary planner route and
one for the escape-hatch route. The planner builds its discovery subset from
``wire_tool_definitions(dialect)`` and stamps its terminal with
``stamp_planner_terminal``. ``call_model`` selects the endpoint and the dialect
by the same condition that selects the model (a hatch turn or not), and
refuses to send a tool list whose stamps do not match that dialect, before the
capability manifest hashes it. Each discovery call whose name was in the list
sent on that call is decoded with ``decode_wire_arguments`` before
``_ParsedToolCall`` is built, so every pre-dispatch reader sees the semantic
form. A discovery name outside the sent palette is not decoded (D17); the
palette is not enforced at dispatch, so such a call is still dispatched, as
today (C21).

Every service route stays on ``none`` until T8 resolves the dialect from
settings; these tests set the two ``PlannerModelConfig`` dialects directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer import pipeline_planner as planner_module
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import (
    PLANNER_DISCOVERY_TOOL_NAMES,
    PlannerDiscoveryPolicy,
    PlannerTerminalContract,
    _parse_response_tool_calls,
    _ParsedToolCall,
    planner_terminal_tool_definition,
    planner_tool_definitions,
)
from elspeth.web.composer.pipeline_proposal import PlannerSurface
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.wire_projection import stamp_planner_terminal
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

_STRICT = ToolContractDialect.OPENAI_STRICT
_NONE = ToolContractDialect.NONE
_TERMINAL = "emit_pipeline_proposal"
# Not an Anthropic-family model: D8 means no production route carries both
# cache markers and strict stamps.
_PLANNER_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"
_HATCH_MODEL = "openrouter/advisor-under-test"


def _same(left: Any, right: Any) -> bool:
    """Type-sensitive structural equality: a tuple never equals a list, and key order counts."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return list(left) == list(right) and all(_same(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def _dialects(planner: ToolContractDialect, hatch: ToolContractDialect) -> dict[str, object]:
    return {"tool_contract_dialect": planner, "escape_hatch_tool_contract_dialect": hatch}


def _hatch_overrides(planner: ToolContractDialect, hatch: ToolContractDialect) -> dict[str, object]:
    return {
        **_dialects(planner, hatch),
        "model_identifier": _PLANNER_MODEL,
        "max_discovery_turns": 1,
        "escape_hatch_model": _HATCH_MODEL,
        "escape_hatch_provider": "openrouter",
        "api_base": "https://primary-gateway.example.test/v1",
        "api_key": "primary-bearer-token",  # secret-scan: allow-this-line
        "escape_hatch_api_base": "https://advisor-gateway.example.test/v1",
        "escape_hatch_api_key": "advisor-bearer-token",  # secret-scan: allow-this-line
    }


def _sent_names(request: Mapping[str, Any]) -> list[str]:
    return [tool["function"]["name"] for tool in request["tools"]]


def _strict_keys(request: Mapping[str, Any]) -> list[object]:
    return [tool["function"]["strict"] if "strict" in tool["function"] else "omitted" for tool in request["tools"]]


# ---------------------------------------------------------------- discovery decode


@pytest.mark.asyncio
async def test_strict_null_discovery_arguments_reach_dispatch_in_semantic_form(
    tmp_path: Path,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On openai_strict, ``list_models {provider: null, limit: null}`` is decoded to ``{}``.

    Every pre-dispatch reader of ``_ParsedToolCall.arguments`` (the information
    keys, the cycle guard's ``stable_hash``) and dispatch itself see the
    semantic form, and the invocation carries ``strict_sent: true,
    wire_conformant: true``. Control: skip decode in
    ``_parse_response_tool_calls`` and S rejects the ``null``.
    """
    seen: list[_ParsedToolCall] = []
    real_keys = planner_module.planner_discovery_information_keys

    def capture_keys(call: _ParsedToolCall) -> tuple[str, ...]:
        seen.append(call)
        return real_keys(call)

    monkeypatch.setattr(planner_module, "planner_discovery_information_keys", capture_keys)
    completion = _ScriptedCompletion(
        _response(("list_models", {"provider": None, "limit": None})),
        _response((_TERMINAL, {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        model_overrides={**_dialects(_STRICT, _NONE), "model_identifier": _PLANNER_MODEL},
    )

    assert "list_models" in _sent_names(completion.requests[0])
    [invocation] = recorder.invocations
    assert invocation.tool_name == "list_models"
    assert invocation.status is ComposerToolStatus.SUCCESS
    assert invocation.arguments_canonical == canonical_json({})
    assert (invocation.strict_sent, invocation.wire_conformant) == (True, True)
    envelope = invocation.to_dict()
    assert (envelope["strict_sent"], envelope["wire_conformant"]) == (True, True)
    list_models_calls = [call for call in seen if call.name == "list_models"]
    assert list_models_calls
    for call in list_models_calls:
        assert deep_thaw(call.arguments) == {}
        assert stable_hash(call.arguments) == stable_hash({})
        assert (call.strict_sent, call.wire_conformant) == (True, True)


@pytest.mark.asyncio
async def test_none_dialect_keeps_todays_rejection_of_a_null_discovery_argument(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    """On none, decode strips nothing: S rejects the ``null`` exactly as today.

    ``strict_sent`` is ``None`` (no key was sent, D16) and ``wire_conformant``
    is ``False`` (the none W is S, which rejects the null).
    """
    completion = _ScriptedCompletion(
        _response(("list_models", {"provider": None})),
        _response((_TERMINAL, {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        model_overrides={**_dialects(_NONE, _NONE), "model_identifier": _PLANNER_MODEL},
    )

    assert _strict_keys(completion.requests[0]) == ["omitted"] * len(completion.requests[0]["tools"])
    [invocation] = recorder.invocations
    assert invocation.status is ComposerToolStatus.ARG_ERROR
    assert invocation.arguments_canonical == canonical_json({"provider": None})
    assert (invocation.strict_sent, invocation.wire_conformant) == (None, False)


@pytest.mark.asyncio
async def test_get_pipeline_state_outside_the_freeform_palette_is_not_decoded(
    tmp_path: Path,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D17: ``get_pipeline_state`` is never in a production palette.

    On openai_strict with the real FREEFORM palette, the parsed call keeps
    ``component: null`` with no wire facts (``strict_sent=None``,
    ``wire_conformant=None``). On this palette the discovery no-gain guard
    then stops the call before dispatch, exactly as on the base (the call
    supplies no pending information), so it never reaches S here; the next
    test covers an unsent name that is dispatched. Control: pass the full 19
    as ``sent_tool_names`` and the null is stripped.
    """
    seen: list[_ParsedToolCall] = []
    real_keys = planner_module.planner_discovery_information_keys

    def capture_keys(call: _ParsedToolCall) -> tuple[str, ...]:
        seen.append(call)
        return real_keys(call)

    monkeypatch.setattr(planner_module, "planner_discovery_information_keys", capture_keys)
    completion = _ScriptedCompletion(
        _response(("get_pipeline_state", {"component": None})),
        _response((_TERMINAL, {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        information_aware=True,
        model_overrides={**_dialects(_STRICT, _NONE), "model_identifier": _PLANNER_MODEL},
    )

    assert "get_pipeline_state" not in _sent_names(completion.requests[0])
    parsed = [call for call in seen if call.name == "get_pipeline_state"]
    assert parsed
    for call in parsed:
        assert deep_thaw(call.arguments) == {"component": None}
        assert (call.strict_sent, call.wire_conformant) == (None, None)
    assert len(recorder.invocations) == 0


def test_the_parser_requires_the_dialect_and_the_sent_tool_names() -> None:
    """D10: no defaulted form, so no caller can get a parse that silently decodes nothing.

    A defaulted ``dialect`` / ``sent_tool_names`` would leave a strict W's
    ``null`` at a promoted position unstripped for any caller that omits
    them, and S would reject it (§1.6). Omitting either keyword is refused
    at the call. Control: give either keyword a default and this test goes
    red.
    """
    response = _response(("list_models", {"provider": None, "limit": None}))
    without_either: dict[str, Any] = {"max_tool_calls": 3}
    without_names: dict[str, Any] = {"max_tool_calls": 3, "dialect": _STRICT}
    without_dialect: dict[str, Any] = {"max_tool_calls": 3, "sent_tool_names": frozenset({"list_models"})}

    with pytest.raises(TypeError, match="'dialect' and 'sent_tool_names'"):
        _parse_response_tool_calls(response, **without_either)
    with pytest.raises(TypeError, match="'sent_tool_names'"):
        _parse_response_tool_calls(response, **without_names)
    with pytest.raises(TypeError, match="'dialect'"):
        _parse_response_tool_calls(response, **without_dialect)


def _palette_without(name: str) -> PlannerDiscoveryPolicy:
    """The ``_plan`` default full palette (no information manifest) minus one tool."""
    return planner_module.PlannerDiscoveryPolicy(
        manifest=planner_module.PlannerInformationManifest(supplied=frozenset()),
        discovery_tool_names=tuple(tool for tool in PLANNER_DISCOVERY_TOOL_NAMES if tool != name),
        unresolved_classes=(),
    )


@pytest.mark.asyncio
async def test_a_dispatched_discovery_name_outside_the_sent_palette_reaches_s_undecoded(
    tmp_path: Path,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C21: the palette is not enforced at dispatch, so an unsent discovery name is still dispatched.

    With ``list_models`` left out of the sent palette, its ``provider: null``
    is not decoded and S rejects it exactly as today, with no wire facts.
    Control: pass the full 19 as ``sent_tool_names`` and the call succeeds.
    """
    monkeypatch.setattr(planner_module.PlannerDiscoveryPolicy, "initial", lambda *args, **kwargs: _palette_without("list_models"))
    completion = _ScriptedCompletion(
        _response(("list_models", {"provider": None})),
        _response((_TERMINAL, {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        information_aware=True,
        model_overrides={**_dialects(_STRICT, _NONE), "model_identifier": _PLANNER_MODEL},
    )

    assert "list_models" not in _sent_names(completion.requests[0])
    [invocation] = recorder.invocations
    assert invocation.tool_name == "list_models"
    assert invocation.status is ComposerToolStatus.ARG_ERROR
    assert invocation.arguments_canonical == canonical_json({"provider": None})
    assert (invocation.strict_sent, invocation.wire_conformant) == (None, None)


# ---------------------------------------------------------------- stamping (characterization of what T7 builds)


def _base_planner_list(policy: PlannerDiscoveryPolicy | None = None) -> list[dict[str, Any]]:
    """The planner list as the base built it: registry entries wrapped, plus the unstamped terminal."""
    registered = {definition["name"]: definition for definition in get_tool_definitions()}
    names = PLANNER_DISCOVERY_TOOL_NAMES if policy is None else policy.discovery_tool_names
    discovery = [
        {
            "type": "function",
            "function": {
                "name": registered[name]["name"],
                "description": registered[name]["description"],
                "parameters": registered[name]["parameters"],
            },
        }
        for name in names
    ]
    return [*discovery, planner_terminal_tool_definition(dialect=_NONE)]


def test_policy_none_planner_list_is_19_strict_true_plus_a_strict_false_terminal() -> None:
    tools = planner_tool_definitions(dialect=_STRICT)
    assert [tool["function"]["name"] for tool in tools] == [*PLANNER_DISCOVERY_TOOL_NAMES, _TERMINAL]
    assert [tool["function"]["strict"] for tool in tools] == [True] * 19 + [False]


@pytest.mark.parametrize(
    "policy",
    [
        None,
        PlannerDiscoveryPolicy.initial(PlannerSurface.FREEFORM),
        PlannerDiscoveryPolicy.initial(PlannerSurface.TUTORIAL_PROFILE),
    ],
    ids=["policy_none", "freeform", "tutorial_profile"],
)
def test_none_planner_lists_carry_no_strict_key_and_equal_the_base_construction(policy: PlannerDiscoveryPolicy | None) -> None:
    tools = planner_tool_definitions(policy, dialect=_NONE)
    assert all("strict" not in tool["function"] for tool in tools)
    assert _same(tools, _base_planner_list(policy))


def _non_default_terminal_contract() -> PlannerTerminalContract:
    selected_schema = {
        "type": "object",
        "properties": {"route": {"type": "string"}},
        "required": ["route"],
        "additionalProperties": False,
    }
    return PlannerTerminalContract(schema=selected_schema, materialize=lambda delta: delta)


@pytest.mark.parametrize("contract", [None, _non_default_terminal_contract()], ids=["default", "selected"])
def test_terminal_stamping_keeps_todays_bytes_on_none_and_adds_only_strict_false(contract: PlannerTerminalContract | None) -> None:
    none_terminal = planner_terminal_tool_definition(contract, dialect=_NONE)
    strict_terminal = planner_terminal_tool_definition(contract, dialect=_STRICT)
    assert "strict" not in none_terminal["function"]
    assert _same(stamp_planner_terminal(none_terminal, _NONE), none_terminal)
    expected_strict = stamp_planner_terminal(none_terminal, _NONE)
    expected_strict["function"]["strict"] = False
    assert _same(strict_terminal, expected_strict)


# ---------------------------------------------------------------- the escape hatch follows its own route


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("planner", "hatch"),
    [(_STRICT, _NONE), (_NONE, _STRICT)],
    ids=["strict_planner_none_hatch", "none_planner_strict_hatch"],
)
async def test_hatch_turn_uses_the_hatch_dialect_and_endpoint(
    tmp_path: Path,
    tool_context: ToolContext,
    planner: ToolContractDialect,
    hatch: ToolContractDialect,
) -> None:
    """The hatch call's tools, audit dialect and endpoint follow the hatch route; ordinary calls follow the planner route.

    Control: select the endpoint by the planner condition and the hatch
    endpoint assertion goes red.
    """
    completion = _ScriptedCompletion(
        _response(("list_sources", {})),
        _response(("list_sinks", {})),
        _response((_TERMINAL, {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        model_overrides=_hatch_overrides(planner, hatch),
    )

    assert len(completion.requests) == 3
    ordinary_requests, hatch_request = completion.requests[:2], completion.requests[2]
    for request in ordinary_requests:
        assert request["api_base"] == "https://primary-gateway.example.test/v1"
        if planner is _STRICT:
            assert set(_strict_keys(request)) == {True, False}
        else:
            assert set(_strict_keys(request)) == {"omitted"}
    assert hatch_request["model"] == _HATCH_MODEL
    assert hatch_request["api_base"] == "https://advisor-gateway.example.test/v1"
    assert hatch_request["api_key"] == "advisor-bearer-token"  # secret-scan: allow-this-line
    assert _sent_names(hatch_request) == [_TERMINAL]
    assert _strict_keys(hatch_request) == ([False] if hatch is _STRICT else ["omitted"])
    assert [call.tool_contract_dialect for call in recorder.llm_calls] == [planner, planner, hatch]
    assert recorder.llm_calls[2].strict_tool_count == 0


# ---------------------------------------------------------------- the stamp assertion


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "built"),
    [(_STRICT, _NONE), (_NONE, _STRICT)],
    ids=["unstamped_list_on_strict", "stamped_list_on_none"],
)
async def test_a_list_stamped_for_another_dialect_is_refused_before_any_provider_call(
    tmp_path: Path,
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    configured: ToolContractDialect,
    built: ToolContractDialect,
) -> None:
    real_builder = planner_module.planner_tool_definitions

    def mismatched(
        policy: PlannerDiscoveryPolicy | None = None,
        *,
        dialect: ToolContractDialect,
        terminal_contract: PlannerTerminalContract | None = None,
    ) -> list[dict[str, Any]]:
        del dialect
        return real_builder(policy, dialect=built, terminal_contract=terminal_contract)

    monkeypatch.setattr(planner_module, "planner_tool_definitions", mismatched)
    completion = _ScriptedCompletion(_response((_TERMINAL, {"pipeline": _pipeline(tmp_path)})))

    with pytest.raises(AuditIntegrityError, match=r"planner tool list is (not stamped|stamped although)"):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            model_overrides={**_dialects(configured, _NONE), "model_identifier": _PLANNER_MODEL},
        )

    assert completion.requests == []

"""A valid message under the maximum always goes.

Each test drives the real compose loop against a recording
``litellm.acompletion`` fake at the shipped pricing. What is asserted is the
request the provider would have received and the call record that describes
it. The primary case is a pipeline of about 200 KB and 25 nodes on the first
call of a turn, at a token setting of 200,000.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from elspeth.core.canonical import canonical_json
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_TOOL_RESULT_STUB
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerHistoryMessage
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.composer.tools import ToolResult
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _composer_service_with_session
from tests.unit.web.composer._large_graph import (
    LARGE_GRAPH_NODE_COUNT,
    default_scan_finding,
    large_graph_dict,
    large_graph_state,
    state_bytes,
)
from tests.unit.web.composer.test_compose_loop_context_budget import (
    _assistant,
    _RecordingProvider,
    _tool_turn,
    _user,
    _wire_bytes,
)
from tests.unit.web.composer.test_compose_loop_llm_audit import (
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
    _passing_preflight,
)
from tests.unit.web.composer.test_tools import _llm_options_with_api_key
from tests.unit.web.composer.test_tools import _mock_catalog as _tools_catalog
from tests.unit.web.composer.test_tools import execute_tool as _execute_tool

_SETTING = 200_000
# A setting the fixed part of any request is over, at today's pricing and at one token per three bytes alike.
_SMALL_SETTING = 20_000
# An operator's explicit request byte maximum. The derived default (8 bytes per token of the
# setting, 160,000 B here) would refuse these requests: they cannot fit 20,000 tokens at any
# plausible density. Tests of the estimate alone set this so only the estimate is judged.
_OPERATOR_BYTE_MAXIMUM = 960 * 1024
_GENEROUS_SETTING = 10_000_000
_MESSAGE = "Add a step that flags records with a missing amount."


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; it returns CLEAN without a provider call."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


def _service(max_prompt_tokens: int = _SETTING, **overrides: Any) -> tuple[ComposerServiceImpl, str]:
    """The service at the shipped pricing: no byte-rate override."""
    return _composer_service_with_session(
        catalog=_mock_catalog(), settings=_make_settings(composer_context_max_prompt_tokens=max_prompt_tokens, **overrides)
    )


async def _compose(
    service: ComposerServiceImpl,
    session_id: str,
    provider: _RecordingProvider,
    *,
    state: CompositionState | None = None,
    history: list[ComposerHistoryMessage] | None = None,
    message: str = _MESSAGE,
    tool_data: dict[str, Any] | None = None,
) -> Any:
    results = iter(range(2, 100))

    def _execute(tool_name: str, arguments: dict[str, Any], current: CompositionState, *_args: Any, **_kwargs: Any) -> ToolResult:
        version = next(results)
        updated = replace(current, metadata=PipelineMetadata(name=f"v{version}"), version=version)
        return ToolResult(
            success=True,
            updated_state=updated,
            validation=ValidationSummary(is_valid=True, errors=(), warnings=(), suggestions=(), semantic_contracts=()),
            affected_nodes=(),
            data=None if tool_data is None else tool_data[arguments["patch"]["name"]],
        )

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=_execute),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        return await service.compose(message, history or [], state if state is not None else _empty_state(), session_id=session_id)


def _state_context(messages: list[dict[str, Any]]) -> dict[str, Any]:
    (state_message,) = [m for m in messages if m["role"] == "user" and m["content"].startswith(STATE_CONTEXT_PREFIX)]
    payload: dict[str, Any] = json.loads(state_message["content"][len(STATE_CONTEXT_PREFIX) + 1 :])
    return payload


def _current_request(request: dict[str, Any]) -> str:
    """The user's current message: the last user-role message (a system note about retained history follows it)."""
    content: str = [m for m in request["messages"] if m["role"] == "user"][-1]["content"]
    return content


def _node_ids(payload: Any) -> set[str]:
    """Every ``step_NNN`` id anywhere in the state message."""
    text = canonical_json(payload)
    return {f"step_{index:03d}" for index in range(200) if f'"step_{index:03d}"' in text}


def _history() -> list[ComposerHistoryMessage]:
    return [
        _user(0, "Please read the CSV file."),
        _assistant("I have set up the CSV source."),
        _user(2, "Now add a filter."),
        _assistant("The filter is in place."),
    ]


async def _reference(*, state: CompositionState | None = None, history: list[ComposerHistoryMessage] | None = None) -> dict[str, Any]:
    """The one request the same turn sends when nothing can be over any setting."""
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    await _compose(service, session_id, provider, state=state, history=history)
    (request,) = provider.requests
    return request


def test_the_fixture_is_the_shape_it_claims() -> None:
    state = large_graph_state()
    assert len(state.nodes) == LARGE_GRAPH_NODE_COUNT == 25
    assert 190_000 <= state_bytes(state) <= 220_000
    # Inside the scanner's default bounds at 25 nodes, outside them at 46: the two sizes the tests use.
    assert default_scan_finding(state) is None
    assert default_scan_finding(large_graph_state(46)) == "traversal_limit"


@pytest.mark.asyncio
async def test_a_first_call_carrying_a_200_kb_25_node_graph_is_sent() -> None:
    """The primary case: about 200 KB and 25 nodes, first call of a turn, setting 200,000."""
    service, session_id = _service()
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, provider, state=large_graph_state())

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert 440_000 <= _wire_bytes(request) <= 560_000
    assert _node_ids(_state_context(request["messages"])) == {f"step_{index:03d}" for index in range(25)}
    assert _current_request(request) == _MESSAGE
    (call,) = result.llm_calls
    assert call.context_budget is not None
    # The record says whether the estimate was exceeded, either way, and against what.
    assert call.context_budget.over_estimate in (True, False)
    assert call.context_budget.max_prompt_tokens == _SETTING


@pytest.mark.asyncio
async def test_later_calls_of_the_turn_with_the_large_graph_are_sent() -> None:
    """Tool rounds of the same turn: every call goes, and each carries the graph and its latest round whole."""
    tool_data = {"call-1": {"rows": "first result " * 200}, "call-2": {"rows": "second result " * 200}}
    service, session_id = _service()
    provider = _RecordingProvider(_tool_turn("call-1"), _tool_turn("call-2"), _make_llm_response(content="Done."))

    result = await _compose(service, session_id, provider, state=large_graph_state(), tool_data=tool_data)

    assert len(provider.requests) == len(result.llm_calls) == 3
    for request in provider.requests:
        assert len(_node_ids(_state_context(request["messages"]))) == 25
    # The latest round of each later call is whole: its result is what the tool returned, never the stub.
    for request, call_id, marker in ((provider.requests[1], "call-1", "first result"), (provider.requests[2], "call-2", "second result")):
        latest = request["messages"][-1]
        assert latest["role"] == "tool" and latest["tool_call_id"] == call_id
        assert marker in latest["content"]
        assert latest["content"] != CONTEXT_BUDGET_TOOL_RESULT_STUB


@pytest.mark.asyncio
async def test_a_first_call_just_over_the_estimate_boundary_is_sent() -> None:
    """Just past the point where today's estimate reaches the setting, with a small pipeline."""
    reference_service, reference_session = _service(_GENEROUS_SETTING)
    reference_provider = _RecordingProvider(_make_llm_response(content="Done."))
    reference = await _compose(reference_service, reference_session, reference_provider)
    budget = reference.llm_calls[0].context_budget
    assert budget is not None
    # Every byte of the current message costs one estimated token today, so this message puts the estimate 2,000 over.
    padding = _SETTING - budget.estimated_tokens_after + 2_000
    assert 0 < padding < 250_000
    message = _MESSAGE + " " + "x" * padding

    service, session_id = _service()
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    await _compose(service, session_id, provider, message=message)

    assert len(provider.requests) == 1
    assert _current_request(provider.requests[0]) == message


@pytest.mark.asyncio
async def test_must_go_content_just_under_the_byte_cap_is_sent() -> None:
    """The top of the range: over the estimate by any pricing, 4 KB under the maximum message size."""
    message = _MESSAGE + " " + "y" * 100_000
    reference = await _reference()
    reference_bytes = _wire_bytes(reference)
    max_request_bytes = reference_bytes + 100_000 + 4_096
    service, session_id = _service(_SMALL_SETTING, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    await _compose(service, session_id, provider, message=message)

    assert len(provider.requests) == 1
    assert max_request_bytes - 8_192 < _wire_bytes(provider.requests[0]) <= max_request_bytes
    assert _current_request(provider.requests[0]) == message


@pytest.mark.asyncio
async def test_control_must_go_content_just_over_the_byte_cap_is_refused_with_the_byte_refusal() -> None:
    message = _MESSAGE + " " + "y" * 100_000
    reference = await _reference()
    max_request_bytes = _wire_bytes(reference) + 100_000 - 4_096
    service, session_id = _service(_GENEROUS_SETTING, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider, message=message)

    assert provider.requests == []
    assert exc_info.value.reason == "context_budget_exceeded"
    assert exc_info.value.evidence["ceiling"] == "bytes"


@pytest.mark.asyncio
async def test_history_goes_to_zero_before_anything_protected_and_protected_content_is_byte_identical() -> None:
    """Must-go content alone is over the estimate: every history row is left out and nothing else changes."""
    reference = await _reference(history=_history())
    reference_messages = reference["messages"]
    # Instrument guard: the reference carries all four history rows.
    assert [m["content"] for m in reference_messages[2:6]] == [row["content"] for row in _history()]

    service, session_id = _service(_SMALL_SETTING, composer_planner_max_request_bytes=_OPERATOR_BYTE_MAXIMUM)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    result = await _compose(service, session_id, provider, history=_history())

    (request,) = provider.requests
    messages = request["messages"]
    history_texts = {row["content"] for row in _history()}
    assert [m for m in messages if m["content"] in history_texts] == []
    assert len(messages) == len(reference_messages) - 4
    # System, catalog, tools and the current message are byte-identical.
    assert canonical_json(messages[0]) == canonical_json(reference_messages[0])
    assert canonical_json(messages[1]) == canonical_json(reference_messages[1])
    assert canonical_json(request["tools"]) == canonical_json(reference["tools"])
    assert canonical_json(messages[-1]) == canonical_json(reference_messages[-1])
    # The state message differs only by the count of what was left out.
    shed_state, reference_state = _state_context(messages), _state_context(reference_messages)
    assert shed_state["conversation_omissions"] == {"prior_user_requests_omitted": 2, "prior_assistant_messages_omitted": 2}
    assert {key: value for key, value in shed_state.items() if key != "conversation_omissions"} == reference_state
    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == (0, 1, 2, 3)


@pytest.mark.asyncio
async def test_an_over_estimate_send_is_recorded_on_its_call() -> None:
    service, session_id = _service(_SMALL_SETTING, composer_planner_max_request_bytes=_OPERATOR_BYTE_MAXIMUM)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, provider)

    assert len(provider.requests) == 1
    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.estimated_tokens_after > _SMALL_SETTING
    assert call.context_budget.over_estimate is True
    assert call.context_budget.max_prompt_tokens == _SMALL_SETTING
    stored = call.context_budget.to_dict()
    assert stored["over_estimate"] is True
    assert stored["max_prompt_tokens"] == _SMALL_SETTING
    # The whole call record carries it into the audit envelope.
    assert call.to_dict()["context_budget"]["over_estimate"] is True


@pytest.mark.asyncio
async def test_must_go_content_over_960_kib_that_fits_the_setting_goes() -> None:
    # The graph and the turn's latest tool result are must-go: nothing of them is left out. Over
    # the former fixed 960 KiB maximum they were refused on bytes although a 1,000,000-token
    # setting takes them at any plausible density.
    rows = {f"rows_{part}": f"result row {part} " * 13_000 for part in "abc"}
    service, session_id = _service(1_000_000)
    provider = _RecordingProvider(_tool_turn("call-1"), _make_llm_response(content="Done."))

    result = await _compose(service, session_id, provider, state=large_graph_state(), tool_data={"call-1": rows})

    assert len(provider.requests) == len(result.llm_calls) == 2
    second = provider.requests[1]
    assert _wire_bytes(second) > 960 * 1024
    latest = second["messages"][-1]
    assert latest["role"] == "tool" and all(text in latest["content"] for text in rows.values())
    assert len(_node_ids(_state_context(second["messages"]))) == 25


@pytest.mark.asyncio
async def test_a_request_over_960_kib_that_fits_the_setting_goes() -> None:
    # A 1,000,000-token setting: the request is well under it at any plausible density, but over
    # the former fixed 960 KiB maximum, under which every earlier turn was left out although the
    # model could take the request whole.
    prose = " Each record names its source and amount." * 5_000
    history = [
        _user(0, "Please read the CSV file." + prose),
        _assistant("The source is set." + prose),
        _user(2, "Now add a filter." + prose),
    ]
    service, session_id = _service(1_000_000)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, provider, state=large_graph_state(), history=history)

    (request,) = provider.requests
    assert _wire_bytes(request) > 960 * 1024
    assert sum(1 for message in request["messages"] if prose in message["content"]) == 3
    (call,) = result.llm_calls
    assert call.context_budget is not None and call.context_budget.shed_history_indices == ()


@pytest.mark.asyncio
async def test_the_derived_byte_maximum_refuses_only_a_request_that_cannot_fit_the_setting() -> None:
    # Without an explicit maximum it is 8 bytes per token of the setting. This request is over
    # that with nothing left to leave out, so it cannot fit at any plausible density: nothing is sent.
    service, session_id = _service(_SMALL_SETTING)
    assert service._context_budget_policy.max_request_bytes == _SMALL_SETTING * 8
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    with pytest.raises(ComposerConvergenceError):
        await _compose(service, session_id, provider)

    assert provider.requests == []


@pytest.mark.asyncio
async def test_a_send_within_the_estimate_is_recorded_as_such() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, provider)

    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.over_estimate is False
    assert call.context_budget.to_dict()["over_estimate"] is False


@pytest.mark.asyncio
async def test_control_a_credential_in_a_small_graph_is_refused_before_any_provider_call() -> None:
    state_data = large_graph_dict(3)
    state_data["nodes"][2]["options"]["api_key"] = "sk-" + "a" * 24
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    with pytest.raises(CredentialMaterialRefused) as exc_info:
        await _compose(service, session_id, provider, state=CompositionState.from_dict(state_data))

    assert provider.requests == []
    assert exc_info.value.finding.category == "credential_field"


# --- A latest-round discovery result at its real size -------------------------------------------------
#
# The real plugin catalog, the real per-user policy view and the real tool executor. Two deployments:
# the DEFAULT allowlist (the required web plugins only, ``llm`` projected by one operator profile) and
# EVERY installed plugin allow-listed. Each test prints the result sizes it measured.


class _EverySecret:
    """Secret inventory in which every referenced credential is present (sizes do not depend on values)."""

    def has_server_ref(self, name: str) -> bool:
        return True

    def has_user_ref(self, principal: str, name: str) -> bool:
        return True

    def has_ref(self, principal: str, name: str) -> bool:
        return True

    def server_generation(self, name: str) -> str:
        return "generation"

    def user_generation(self, principal: str, name: str) -> str:
        return "generation"


def _web_policy_service(*, every_plugin: bool, max_prompt_tokens: int) -> tuple[ComposerServiceImpl, str]:
    """A composer service whose catalog is the web policy view for one user, as ``web/app.py`` wires it."""
    from pathlib import Path
    from uuid import uuid4

    import structlog
    from sqlalchemy.pool import StaticPool

    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.availability import build_plugin_snapshot
    from elspeth.web.plugin_policy.compiler import REQUIRED_WEB_PLUGIN_IDS, compile_web_plugin_policy
    from elspeth.web.plugin_policy.models import PluginId
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
    from elspeth.web.sessions.engine import create_session_engine
    from elspeth.web.sessions.schema import initialize_session_schema
    from elspeth.web.sessions.telemetry import build_sessions_telemetry
    from tests.fixtures.identities import grant_test_pipeline_user
    from tests.unit.web.conftest import _make_session
    from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

    catalog = create_catalog_service()
    installed = (
        [PluginId("source", item.name) for item in catalog.list_sources()]
        + [PluginId("transform", item.name) for item in catalog.list_transforms()]
        + [PluginId("sink", item.name) for item in catalog.list_sinks()]
    )
    allowlist = [f"{plugin.kind}:{plugin.name}" for plugin in installed if plugin not in REQUIRED_WEB_PLUGIN_IDS] if every_plugin else []
    # A capability with more than one authorized implementation needs a stated order; any complete order will do.
    registry = get_shared_plugin_manager()
    classes = {
        **{PluginId("source", cls.name): cls for cls in registry.get_sources()},
        **{PluginId("transform", cls.name): cls for cls in registry.get_transforms()},
        **{PluginId("sink", cls.name): cls for cls in registry.get_sinks()},
    }
    authorized = sorted(set(REQUIRED_WEB_PLUGIN_IDS) | (set(installed) if every_plugin else set()))
    by_capability: dict[Any, list[str]] = {}
    for plugin in authorized:
        for declaration in classes[plugin].policy_capabilities:
            by_capability.setdefault(declaration.capability, []).append(f"{plugin.kind}:{plugin.name}")
    settings = _make_settings(
        composer_context_max_prompt_tokens=max_prompt_tokens,
        plugin_allowlist=allowlist,
        plugin_preferences={capability: tuple(order) for capability, order in by_capability.items() if len(order) > 1},
        llm_profiles={
            "llm-default": {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0", "region_name": "us-east-1"}
        },
        default_llm_profile="llm-default",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=registry, settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:test-user",
        secret_inventory=_EverySecret(),
        generation_key=b"valid-message-always-goes-key",
    )
    engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    session_id = str(uuid4())
    with engine.begin() as conn:
        _make_session(conn, session_id=session_id, user_id="test-user")
        grant_test_pipeline_user(conn, identity_id="test-user")
    sessions = FencedSessionServiceHarness(
        engine, data_dir=Path(settings.data_dir), telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.composer.sessions")
    )
    service = ComposerServiceImpl(
        catalog=catalog,
        settings=settings,
        plugin_snapshot_factory=lambda _user_id: snapshot,
        operator_profile_registry=profiles,
        sessions_service=sessions,
        session_engine=engine,
    )
    return service, session_id


async def _compose_with_real_tools(service: ComposerServiceImpl, session_id: str, provider: _RecordingProvider) -> Any:
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        return await service.compose("Which transforms can I use?", [], _empty_state(), session_id=session_id, user_id="test-user")


def _discovery_turn(*calls: tuple[str, str, dict[str, Any]]) -> Any:
    return _make_llm_response(
        tool_calls=[{"id": call_id, "name": name, "arguments": arguments} for call_id, name, arguments in calls],
        request_id="chatcmpl-discovery",
    )


_LIST_TRANSFORMS = ("call-list", "list_transforms", {"_elspeth_no_arguments": True})
_LLM_SCHEMA = ("call-schema", "get_plugin_schema", {"plugin_type": "transform", "name": "llm"})


def _tool_result_sizes(request: dict[str, Any]) -> dict[str, int]:
    return {message["tool_call_id"]: len(message["content"]) for message in request["messages"] if message["role"] == "tool"}


async def _discovery_run(*, every_plugin: bool, max_prompt_tokens: int, calls: tuple[Any, ...]) -> tuple[_RecordingProvider, Any | None]:
    """One turn whose first reply is the discovery calls. Returns the provider and the result, or ``None`` if refused."""
    service, session_id = _web_policy_service(every_plugin=every_plugin, max_prompt_tokens=max_prompt_tokens)
    provider = _RecordingProvider(_discovery_turn(*calls), _make_llm_response(content="Done."))
    try:
        result = await _compose_with_real_tools(service, session_id, provider)
    except ComposerConvergenceError as refusal:
        print(
            f"MEASURED every_plugin={every_plugin} setting={max_prompt_tokens} REFUSED reason={refusal.reason} evidence={dict(refusal.evidence)}"
        )
        return provider, None
    sizes = _tool_result_sizes(provider.requests[1])
    budget = result.llm_calls[1].context_budget
    print(
        f"MEASURED every_plugin={every_plugin} setting={max_prompt_tokens} SENT result_chars={sizes} "
        f"first_request_bytes={_wire_bytes(provider.requests[0])} second_request_bytes={_wire_bytes(provider.requests[1])} "
        f"estimated_tokens_after={None if budget is None else budget.estimated_tokens_after}"
    )
    return provider, result


@pytest.mark.asyncio
@pytest.mark.parametrize("every_plugin", [False, True], ids=["default_allowlist", "every_plugin_allowlisted"])
async def test_instrument_the_real_discovery_result_sizes(every_plugin: bool) -> None:
    """Sizes with nothing able to refuse: the default allowlist's list is the smaller by a wide margin."""
    provider, result = await _discovery_run(
        every_plugin=every_plugin, max_prompt_tokens=_GENEROUS_SETTING, calls=(_LIST_TRANSFORMS, _LLM_SCHEMA)
    )

    assert result is not None
    sizes = _tool_result_sizes(provider.requests[1])
    # list_transforms is a selection index. MEASURED on this tree: 2,990 characters (default
    # allowlist) and 10,079 (every plugin), against 60,448 and 164,957 when it echoed every
    # PluginSummary (exported base 7ac78737e, same instrument).
    if every_plugin:
        assert 5_000 <= sizes["call-list"] < 20_000, sizes
    else:
        assert 1_000 <= sizes["call-list"] < 5_000, sizes


@pytest.mark.asyncio
async def test_default_allowlist_one_list_transforms_result_in_the_latest_round_is_sent() -> None:
    """DEFAULT allowlist, small pipeline, setting 200,000: the dispatch after one ``list_transforms`` is sent."""
    provider, result = await _discovery_run(every_plugin=False, max_prompt_tokens=_SETTING, calls=(_LIST_TRANSFORMS,))

    assert result is not None
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_every_plugin_allowlisted_one_list_transforms_result_in_the_latest_round_is_sent() -> None:
    """EVERY PLUGIN ALLOW-LISTED, small pipeline, setting 200,000.

    The selection index is about three times the default deployment's (10,079 against 2,990
    characters, measured). It was 164,957 characters while the listing echoed every PluginSummary;
    the dispatch after it is sent either way.
    """
    provider, result = await _discovery_run(every_plugin=True, max_prompt_tokens=_SETTING, calls=(_LIST_TRANSFORMS,))

    assert result is not None
    assert len(provider.requests) == 2
    assert 5_000 <= _tool_result_sizes(provider.requests[1])["call-list"] < 20_000
    budget = result.llm_calls[1].context_budget
    assert budget is not None
    assert budget.over_estimate is (budget.estimated_tokens_after > _SETTING)


@pytest.mark.asyncio
async def test_every_plugin_allowlisted_two_discovery_results_in_the_latest_round_are_sent() -> None:
    """EVERY PLUGIN ALLOW-LISTED: ``list_transforms`` and the ``llm`` schema together, at the edge of the estimate. Sent either way."""
    provider, result = await _discovery_run(every_plugin=True, max_prompt_tokens=_SETTING, calls=(_LIST_TRANSFORMS, _LLM_SCHEMA))

    assert result is not None
    assert len(provider.requests) == 2
    assert set(_tool_result_sizes(provider.requests[1])) == {"call-list", "call-schema"}
    budget = result.llm_calls[1].context_budget
    assert budget is not None
    assert budget.over_estimate is (budget.estimated_tokens_after > _SETTING)


# INLINE CONTROL / worst case: one LLM step carrying 32 inline queries of about 15.6 KB each, about 500 KB
# of options. Not a deployment's shape: deployed prompts are held in files and very large inline text is no
# longer supported. Each template is 15,854 UTF-8 bytes and holds one "{{" and no "{%", inside the 16 KiB
# per-template cap.
_INLINE_TEMPLATE = "Exact original synthetic prompt. " * 480 + "{{ row.text }}"
_INLINE_QUERY_COUNT = 32


def _inline_worst_case_state() -> CompositionState:
    """Built through ``upsert_node``, so the step passes the same validation an authored step does."""
    state = (
        _empty_state()
        .with_source(
            SourceSpec(
                plugin="csv",
                on_success="shared",
                options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
                on_validation_failure="quarantine",
            )
        )
        .with_output(
            OutputSpec(
                name="out_a",
                plugin="csv",
                options={"path": "outputs/out_a.csv"},
                on_write_failure="discard",
            )
        )
    )
    options = _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"})
    options["required_input_fields"] = ["text"]
    options["max_capacity_retry_seconds"] = 30
    options["queries"] = [
        {"name": f"q{index}", "input_fields": {"text": "text"}, "template": _INLINE_TEMPLATE} for index in range(_INLINE_QUERY_COUNT)
    ]
    upsert = {"id": "a", "node_type": "transform", "plugin": "llm", "input": "shared", "on_success": "out_a", "on_error": "discard"}
    result = _execute_tool("upsert_node", upsert | {"options": options}, state, _tools_catalog())
    assert result.success, [(error.error_code, error.message) for error in result.validation.errors]
    return result.updated_state


@pytest.mark.asyncio
async def test_control_inline_worst_case_a_turn_carrying_a_32_query_500_kb_llm_step_is_sent() -> None:
    """INLINE CONTROL / worst case at a setting of 200,000: every call is sent whole, over the estimate, under the byte maximum.

    The fixture's step carries a pending model-choice review, so the loop answers the model's "Done." with a
    review nudge and calls again; each of those calls carries the whole step too. Nothing is sheddable on
    the first call, so the size alone decides: about 770 KB, about 257,000 estimated tokens at 3.0 bytes per token.
    """
    state = _inline_worst_case_state()
    assert len(_INLINE_TEMPLATE.encode("utf-8")) == 15_854
    assert 500_000 <= state_bytes(state) <= 560_000
    service, session_id = _service()
    provider = _RecordingProvider(*[_make_llm_response(content="Done.") for _ in range(6)])

    result = await _compose(service, session_id, provider, state=state)

    assert 1 <= len(provider.requests) == len(result.llm_calls)
    assert _current_request(provider.requests[0]) == _MESSAGE
    for request, call in zip(provider.requests, result.llm_calls, strict=True):
        # Every query's template reaches the model whole.
        (step,) = [node for node in _state_context(request["messages"])["current_state"]["nodes"] if node["id"] == "a"]
        assert [query["template"] for query in step["options"]["queries"]] == [_INLINE_TEMPLATE] * _INLINE_QUERY_COUNT
        # The record: the request's size, and that it went over the estimate.
        budget = call.context_budget
        assert budget is not None
        assert budget.request_wire_bytes == _wire_bytes(request)
        assert 760_000 <= budget.request_wire_bytes <= 800_000
        assert budget.max_prompt_tokens == _SETTING
        assert budget.estimated_tokens_after > _SETTING
        assert budget.over_estimate is True

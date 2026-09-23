"""Wire fidelity matrix: what LiteLLM transmits for the composer's tool lists, route by route (S1 T11).

Every test here is a **characterization**: it pins what LiteLLM 1.102's
adapters put on the wire today, so each row is green on arrival. The rows
answer one question per route family: does the ``function.strict`` stamp
that ELSPETH builds (``openai_strict`` dialect: ``true`` on the 32
strict-capable tools, an explicit ``false`` on the 10 option tools) reach
the transmitted body unchanged, and does the ``none`` list (no ``strict``
key) stay today's bytes?

Instrument. Requests go to a loopback ``ThreadingHTTPServer`` on
``127.0.0.1:0`` that records each request's path, query and parsed JSON
body and answers a canned 400, so LiteLLM raises ``BadRequestError`` after
transmitting. Routes that cannot take a custom base (hosted
``api.openai.com``) are intercepted with ``respx``, whose default
``assert_all_mocked`` turns any other request into an error. Nothing leaves
the host. The assertions read the **recorded body**, never the kwargs.

The known-negative rows are what show that: an ECMA-only ``pattern`` that
the OpenAI-compatible chat adapter drops, a root ``oneOf`` the Azure chat
adapter flattens, an ``enum`` ``null`` the Bedrock converse adapter drops,
the ``strict`` stamp the native Anthropic adapter drops, and the
``strict: null`` the Responses bridge writes for a tool with no key. Each is
paired with a row that removes the transforming input (or sends the same
input to OpenRouter, which forwards it verbatim) and shows the untransformed
form, so a recorder that echoed the kwargs would turn the known negatives
red.

Every request is built the way production builds it: the route is resolved
by :func:`resolve_composer_tool_contract` under the row's **named**
``composer_strict_tools`` setting, the tool list is the one for the
resolved dialect, and the kwargs come from
``build_composer_loop_request_kwargs`` or ``build_planner_request_kwargs``.
A loopback base is a custom endpoint, which resolves to ``none`` under
``preferred`` (rows 5 and 8), and hosted OpenAI and Azure are ``none`` under
``preferred`` too (ruling 7). So each row that measures adapter forwarding
of a stamped list names ``forward_to_endpoint`` and first asserts the
resolved transport; each such family also has a ``preferred`` control row.
The resolver's own truth table is pinned in ``test_strict_transport.py``,
not repeated here.

Precondition: LiteLLM must have loaded its **local** model cost map (C18,
C24). The root ``tests/conftest.py`` calls ``configure_litellm_pricing()``
before anything imports LiteLLM so that this holds on purpose; the
``_cost_map_is_local`` fixture asserts it before every row.

A LiteLLM upgrade that turns a row red is fixed by re-measuring the route
and updating the row with the evidence, never by loosening the row
(CONTRIBUTING.md, whole-tree gates).
"""

from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.capability_skill import PLANNER_TERMINAL_TOOL_NAME
from elspeth.web.composer.pipeline_planner import build_planner_request_kwargs, planner_tool_definitions
from elspeth.web.composer.service import build_composer_loop_request_kwargs, composer_loop_tool_definitions
from elspeth.web.composer.strict_transport import (
    StrictTransport,
    resolve_composer_tool_contract,
    resolve_strict_transport,
)
from elspeth.web.config import WebSettings

_OPENROUTER_MODEL: Final = "openrouter/deepseek/deepseek-v4.1-flash"
_ADVISOR_MODEL: Final = "openrouter/z-ai/glm-5.3"
_ANTHROPIC_MODEL: Final = "anthropic/claude-sonnet-4-6"
_BEDROCK_MODEL: Final = "bedrock/global.anthropic.claude-sonnet-4-6"
_FAKE_KEY: Final = "fake-matrix-key"
_HOSTED_CHAT_URL: Final = "https://api.openai.com/v1/chat/completions"
_HOSTED_RESPONSES_URL: Final = "https://api.openai.com/v1/responses"
_ABSENT: Final = "<absent>"
_MESSAGES: Final = ({"role": "user", "content": "hi"},)

# Synthetic tools appended to a route's list (never stamped): each carries one
# schema feature that some LiteLLM adapter transforms.
_ECMA_PATTERN: Final = "^\\p{L}+$"
_PLAIN_PATTERN: Final = "^[a-z]+$"


def _pattern_tool(pattern: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "zz_pattern",
            "description": "Synthetic tool with one string pattern.",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "string", "pattern": pattern}},
                "required": ["x"],
                "additionalProperties": False,
            },
        },
    }


def _enum_tool(values: list[str | None]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "zz_enum",
            "description": "Synthetic tool with one enum.",
            "parameters": {
                "type": "object",
                "properties": {"e": {"enum": values}},
                "required": ["e"],
                "additionalProperties": False,
            },
        },
    }


_ONE_OF_BRANCH_A: Final = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
_ONE_OF_BRANCH_B: Final = {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"], "additionalProperties": False}


def _root_one_of_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "zz_one_of",
            "description": "Synthetic tool whose parameters root is a oneOf.",
            "parameters": {"oneOf": [copy.deepcopy(_ONE_OF_BRANCH_A), copy.deepcopy(_ONE_OF_BRANCH_B)]},
        },
    }


def _plain_object_tool() -> dict[str, Any]:
    """The oneOf tool's control: the same name with a plain object root (branch A)."""
    return {
        "type": "function",
        "function": {
            "name": "zz_one_of",
            "description": "Synthetic tool whose parameters root is a plain object.",
            "parameters": copy.deepcopy(_ONE_OF_BRANCH_A),
        },
    }


# ---------------------------------------------------------------------------
# Precondition and instrument
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cost_map_is_local() -> None:
    """Every row runs against LiteLLM's local cost map (C18, C24); never weaken this."""
    from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

    assert get_model_cost_map_source_info()["source"] == "local"


@dataclass(frozen=True, slots=True)
class _Recorded:
    """One request the loopback server received."""

    path: str
    query: dict[str, list[str]]
    body: dict[str, Any]


@dataclass(slots=True)
class _Loopback:
    """The loopback recorder: its base URL and the requests it has received."""

    base: str
    records: list[_Recorded] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


@pytest.fixture(scope="module")
def loopback() -> Iterator[_Loopback]:
    """A loopback HTTP server that records every POST and answers a canned 400."""
    holder: list[_Loopback] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["content-length"])
            raw = self.rfile.read(length)
            split = urlsplit(self.path)
            recorder = holder[0]
            with recorder.lock:
                recorder.records.append(_Recorded(path=split.path, query=parse_qs(split.query), body=json.loads(raw)))
            payload = b'{"error":{"message":"wire fidelity matrix","type":"invalid_request_error"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    holder.append(_Loopback(base=f"http://127.0.0.1:{server.server_port}"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10.0)


def _send_loopback(recorder: _Loopback, kwargs: Mapping[str, Any]) -> _Recorded:
    """Send one request to the loopback recorder and return exactly the one request it received."""
    import litellm

    with recorder.lock:
        recorder.records.clear()
    with pytest.raises(litellm.exceptions.BadRequestError):
        litellm.completion(**{**kwargs, "num_retries": 0})
    with recorder.lock:
        assert len(recorder.records) == 1
        return recorder.records[0]


def _send_hosted(kwargs: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Send one request to hosted OpenAI under respx; return the one body sent to ``url``."""
    import litellm

    seen: list[tuple[str, dict[str, Any]]] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(400, json={"error": {"message": "wire fidelity matrix", "type": "invalid_request_error"}})

    with respx.mock(assert_all_called=True) as router:
        router.post(url).mock(side_effect=_record)
        with pytest.raises(litellm.exceptions.BadRequestError):
            litellm.completion(**{**kwargs, "num_retries": 0})
    assert [sent_url for sent_url, _ in seen] == [url]
    return seen[0][1]


# ---------------------------------------------------------------------------
# Request building (the production builders, under the row's named setting)
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": Path("/data"),
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
        "composer_advisor_model": _ADVISOR_MODEL,
    }
    values.update(overrides)
    return WebSettings(**values)


def _route_settings(model: str, base: str | None, setting: str, **overrides: Any) -> WebSettings:
    if base is None:
        return _settings(composer_model=model, composer_strict_tools=setting, **overrides)
    return _settings(
        composer_model=model,
        composer_endpoint_base_url=base,
        composer_endpoint_api_key=_FAKE_KEY,
        composer_strict_tools=setting,
        **overrides,
    )


@dataclass(frozen=True, slots=True)
class _LoopRequest:
    """A compose-loop request as production builds it, plus a pristine copy of the list it sends."""

    transport: StrictTransport
    dialect: ToolContractDialect
    sent_tools: list[dict[str, Any]]
    kwargs: dict[str, Any]


def _loop_request(
    settings: WebSettings,
    *,
    extra_tools: tuple[dict[str, Any], ...] = (),
    forced_dialect: ToolContractDialect | None = None,
) -> _LoopRequest:
    """Resolve the planner route, build its loop list (plus synthetic tools) and the loop request kwargs.

    ``forced_dialect`` overrides the resolved dialect; only the Anthropic
    known-negative uses it, to send a stamped list to a route that resolves
    to ``none``.
    """
    contract = resolve_composer_tool_contract(settings, env=os.environ)
    dialect = contract.planner.dialect if forced_dialect is None else forced_dialect
    tools = [*composer_loop_tool_definitions(dialect), *extra_tools]
    kwargs = build_composer_loop_request_kwargs(
        model=settings.composer_model,
        messages=list(_MESSAGES),
        tools=tools,
        settings=settings,
        api_base=settings.composer_endpoint_base_url,
        api_key=_FAKE_KEY,
    )
    # A deep copy taken before LiteLLM sees the kwargs, so a transport that
    # mutated them in place could not make a body-versus-sent comparison
    # compare the transformed list with itself.
    return _LoopRequest(
        transport=contract.planner.resolution.transport,
        dialect=dialect,
        sent_tools=copy.deepcopy(tools),
        kwargs=kwargs,
    )


# ---------------------------------------------------------------------------
# Reading the recorded body (one reader per wire shape; no probing)
# ---------------------------------------------------------------------------


def _chat_functions(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Chat Completions shape: ``tools[i] = {"type": "function", "function": {...}}``."""
    functions: dict[str, dict[str, Any]] = {}
    for tool in body["tools"]:
        assert tool["type"] == "function"
        functions[tool["function"]["name"]] = tool["function"]
    return functions


def _responses_tools(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Responses shape (the bridge): ``tools[i] = {"type": "function", "name": ..., "strict": ...}``."""
    tools: dict[str, dict[str, Any]] = {}
    for tool in body["tools"]:
        assert tool["type"] == "function"
        assert "function" not in tool
        tools[tool["name"]] = tool
    return tools


def _anthropic_tools(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Anthropic Messages shape: ``tools[i] = {"name": ..., "input_schema": ...}``."""
    tools: dict[str, dict[str, Any]] = {}
    for tool in body["tools"]:
        assert "input_schema" in tool
        tools[tool["name"]] = tool
    return tools


def _converse_tool_specs(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Bedrock converse shape: ``toolConfig.tools[i] = {"toolSpec": {...}}``."""
    return {tool["toolSpec"]["name"]: tool["toolSpec"] for tool in body["toolConfig"]["tools"]}


def _strict_flags(entries: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    """Each entry's ``strict`` value, or ``_ABSENT`` when the key is not there."""
    return {name: (entry["strict"] if "strict" in entry else _ABSENT) for name, entry in entries.items()}


def _sent_flags(tools: list[dict[str, Any]]) -> dict[str, object]:
    """The ``strict`` stamp of each tool in a sent (Chat Completions shaped) list."""
    return _strict_flags({tool["function"]["name"]: tool["function"] for tool in tools})


def _count(flags: Mapping[str, object], value: object) -> int:
    return sum(1 for flag in flags.values() if flag is value)


def _assert_openai_strict_stamps(flags: Mapping[str, object], *, synthetic: int = 0) -> None:
    """The ``openai_strict`` loop list: 32 ``true``, 10 ``false``, and the unstamped synthetic tools."""
    assert _count(flags, True) == 32
    assert _count(flags, False) == 10
    assert sum(1 for flag in flags.values() if flag == _ABSENT) == synthetic


def _assert_no_strict_key(flags: Mapping[str, object], *, tool_count: int) -> None:
    assert len(flags) == tool_count
    assert set(flags.values()) == {_ABSENT}


# ---------------------------------------------------------------------------
# OpenRouter (resolver rows 4 and 5): forwards every stamp and every schema
# keyword verbatim. Known negative by contrast: the same ECMA pattern, root
# oneOf and enum null that other adapters transform arrive unchanged here.
# ---------------------------------------------------------------------------


def test_openrouter_forwards_the_stamped_list_verbatim(loopback: _Loopback) -> None:
    """Characterization, row 5 under ``forward_to_endpoint``: the whole tool list reaches the wire unchanged.

    The synthetic ECMA-only ``pattern``, root ``oneOf`` and ``enum`` ``null``
    are the inputs other adapters transform (see the known-negative rows);
    OpenRouter keeps all three, which is the cross-family difference that
    shows the recorder reads the transmitted body.
    """
    request = _loop_request(
        _route_settings(_OPENROUTER_MODEL, f"{loopback.base}/api/v1", "forward_to_endpoint"),
        extra_tools=(_pattern_tool(_ECMA_PATTERN), _root_one_of_tool(), _enum_tool(["a", "b", None])),
    )
    assert request.transport is StrictTransport.FORWARDING
    assert request.dialect is ToolContractDialect.OPENAI_STRICT

    recorded = _send_loopback(loopback, request.kwargs)

    assert recorded.path == "/api/v1/chat/completions"
    assert recorded.body["tools"] == request.sent_tools
    flags = _strict_flags(_chat_functions(recorded.body))
    assert flags == _sent_flags(request.sent_tools)
    _assert_openai_strict_stamps(flags, synthetic=3)


def test_openrouter_preferred_control_sends_no_strict_key(loopback: _Loopback) -> None:
    """Characterization, row 5 under ``preferred``: a custom OpenRouter base resolves to ``none``; today's bytes."""
    request = _loop_request(_route_settings(_OPENROUTER_MODEL, f"{loopback.base}/api/v1", "preferred"))
    assert request.transport is StrictTransport.NONE

    recorded = _send_loopback(loopback, request.kwargs)

    assert recorded.body["tools"] == request.sent_tools
    _assert_no_strict_key(_strict_flags(_chat_functions(recorded.body)), tool_count=42)


def test_openrouter_planner_list_forwards_its_stamps(loopback: _Loopback) -> None:
    """Characterization: the planner's request (``build_planner_request_kwargs``) forwards 19 ``true`` and the terminal ``false``."""
    settings = _route_settings(_OPENROUTER_MODEL, f"{loopback.base}/api/v1", "forward_to_endpoint")
    contract = resolve_composer_tool_contract(settings, env=os.environ)
    assert contract.planner.resolution.transport is StrictTransport.FORWARDING
    tools = planner_tool_definitions(dialect=contract.planner.dialect)
    sent = copy.deepcopy(tools)
    kwargs = build_planner_request_kwargs(
        model=settings.composer_model,
        messages=list(_MESSAGES),
        tools=tools,
        max_completion_tokens=16,
        temperature=None,
        seed=None,
        reasoning_effort="low",
        api_base=settings.composer_endpoint_base_url,
        api_key=_FAKE_KEY,
    )

    recorded = _send_loopback(loopback, kwargs)

    assert recorded.body["tools"] == sent
    flags = _strict_flags(_chat_functions(recorded.body))
    assert flags == _sent_flags(sent)
    assert flags.pop(PLANNER_TERMINAL_TOOL_NAME) is False
    assert len(flags) == 19
    assert set(flags.values()) == {True}


def test_openai_prefix_with_an_openrouter_host_resolves_forwarding() -> None:
    """Characterization, row 6: ``openai/`` + the OpenRouter host is ``FORWARDING`` under ``preferred``.

    Loopback cannot present the OpenRouter host, so the adapter half of this
    route is measured on ``openai/`` + loopback under ``forward_to_endpoint``
    (row 8, the next test); this row pins only the resolution.
    """
    resolution = resolve_strict_transport(model="openai/gpt-4.1", api_base="https://openrouter.ai/api/v1", setting="preferred", env={})
    assert resolution.transport is StrictTransport.FORWARDING


# ---------------------------------------------------------------------------
# OpenAI-compatible chat on a custom base (resolver row 8): ``openai/`` and
# bare model names. Forwards the stamps; known negative: drops an ECMA-only
# ``pattern`` that OpenRouter keeps.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["openai/gpt-4.1", "gpt-5.5"])
def test_openai_compatible_custom_base_forwards_the_stamps(loopback: _Loopback, model: str) -> None:
    """Characterization, row 8 under ``forward_to_endpoint``: 32 ``true`` and 10 ``false`` reach the wire."""
    request = _loop_request(_route_settings(model, f"{loopback.base}/v1", "forward_to_endpoint"))
    assert request.transport is StrictTransport.FORWARDING

    recorded = _send_loopback(loopback, request.kwargs)

    assert recorded.path == "/v1/chat/completions"
    flags = _strict_flags(_chat_functions(recorded.body))
    assert flags == _sent_flags(request.sent_tools)
    _assert_openai_strict_stamps(flags)


@pytest.mark.parametrize("model", ["openai/gpt-4.1", "gpt-5.5"])
def test_openai_compatible_custom_base_preferred_control_sends_no_strict_key(loopback: _Loopback, model: str) -> None:
    """Characterization, row 8 under ``preferred``: a custom endpoint resolves to ``none``; no tool carries ``strict``."""
    request = _loop_request(_route_settings(model, f"{loopback.base}/v1", "preferred"))
    assert request.transport is StrictTransport.NONE

    recorded = _send_loopback(loopback, request.kwargs)

    _assert_no_strict_key(_strict_flags(_chat_functions(recorded.body)), tool_count=42)


@pytest.mark.parametrize("model", ["openai/gpt-4.1", "gpt-5.5"])
def test_openai_compatible_chat_drops_an_ecma_only_pattern(loopback: _Loopback, model: str) -> None:
    """Characterization, known negative: the ECMA-only ``pattern`` is removed on the wire.

    The same synthetic tool reaches the wire verbatim on OpenRouter
    (``test_openrouter_forwards_the_stamped_list_verbatim``). The
    transformation is LiteLLM's regex sanitiser; S1's 32 strict tools carry
    no ``pattern`` (the ``pattern_census`` measurement), so it does not
    touch them.
    """
    request = _loop_request(_route_settings(model, f"{loopback.base}/v1", "preferred"), extra_tools=(_pattern_tool(_ECMA_PATTERN),))

    recorded = _send_loopback(loopback, request.kwargs)

    parameters = _chat_functions(recorded.body)["zz_pattern"]["parameters"]
    assert parameters["properties"]["x"] == {"type": "string"}


@pytest.mark.parametrize("model", ["openai/gpt-4.1", "gpt-5.5"])
def test_openai_compatible_chat_keeps_a_plain_pattern(loopback: _Loopback, model: str) -> None:
    """Characterization, the known negative's control: with the ECMA-only escape removed, the pattern is kept."""
    request = _loop_request(_route_settings(model, f"{loopback.base}/v1", "preferred"), extra_tools=(_pattern_tool(_PLAIN_PATTERN),))

    recorded = _send_loopback(loopback, request.kwargs)

    parameters = _chat_functions(recorded.body)["zz_pattern"]["parameters"]
    assert parameters["properties"]["x"] == {"type": "string", "pattern": _PLAIN_PATTERN}


# ---------------------------------------------------------------------------
# Hosted OpenAI (resolver row 7), intercepted with respx: Chat Completions
# for ``gpt-4.1``, the Responses bridge for ``gpt-5.5`` with tools.
# ---------------------------------------------------------------------------


def test_hosted_openai_chat_forwards_the_stamps() -> None:
    """Characterization, row 7 under ``forward_to_endpoint`` (``ENFORCING``): the stamps reach ``/v1/chat/completions``.

    The same adapter family as the custom-base chat rows: the synthetic
    ECMA-only ``pattern`` is dropped here too, which OpenRouter keeps.
    """
    request = _loop_request(_route_settings("gpt-4.1", None, "forward_to_endpoint"), extra_tools=(_pattern_tool(_ECMA_PATTERN),))
    assert request.transport is StrictTransport.ENFORCING

    body = _send_hosted(request.kwargs, _HOSTED_CHAT_URL)

    functions = _chat_functions(body)
    flags = _strict_flags(functions)
    assert flags == _sent_flags(request.sent_tools)
    _assert_openai_strict_stamps(flags, synthetic=1)
    assert functions["zz_pattern"]["parameters"]["properties"]["x"] == {"type": "string"}


def test_hosted_openai_chat_preferred_control_sends_no_strict_key() -> None:
    """Characterization, row 7 under ``preferred`` (ruling 7): ``none``; no tool carries ``strict``."""
    request = _loop_request(_route_settings("gpt-4.1", None, "preferred"))
    assert request.transport is StrictTransport.NONE

    body = _send_hosted(request.kwargs, _HOSTED_CHAT_URL)

    _assert_no_strict_key(_strict_flags(_chat_functions(body)), tool_count=42)


def test_hosted_responses_bridge_forwards_the_stamps() -> None:
    """Characterization, row 7 under ``forward_to_endpoint``: ``gpt-5.5`` with tools is bridged to ``/v1/responses`` with its stamps."""
    request = _loop_request(_route_settings("gpt-5.5", None, "forward_to_endpoint"))
    assert request.transport is StrictTransport.ENFORCING

    body = _send_hosted(request.kwargs, _HOSTED_RESPONSES_URL)

    flags = _strict_flags(_responses_tools(body))
    assert flags == _sent_flags(request.sent_tools)
    _assert_openai_strict_stamps(flags)


@pytest.mark.parametrize("setting", ["preferred", "off"])
def test_hosted_responses_bridge_writes_strict_null_for_an_unstamped_list(setting: str) -> None:
    """Characterization, known negative: for a tool with no ``strict`` key the bridge transmits ``strict: null``.

    Under ``preferred`` (ruling 7) and ``off`` the route resolves to
    ``none``, and the list sent carries no ``strict`` key on any tool, which
    is today's list. The bridge itself writes ``"strict": null`` on every
    tool, so on this route today's bytes are ``strict: null``, not an absent
    key. Its control is the forwarding row above: with the key present, the
    bridge sends the value that was set.
    """
    request = _loop_request(_route_settings("gpt-5.5", None, setting))
    assert request.transport is StrictTransport.NONE
    _assert_no_strict_key(_sent_flags(request.sent_tools), tool_count=42)

    body = _send_hosted(request.kwargs, _HOSTED_RESPONSES_URL)

    flags = _strict_flags(_responses_tools(body))
    assert len(flags) == 42
    assert set(flags.values()) == {None}


# ---------------------------------------------------------------------------
# Azure OpenAI (resolver row 9) on a loopback api_base, AZURE_API_VERSION
# unset: Chat Completions for ``azure/gpt-4.1``, the Responses bridge for
# ``azure/gpt-5.5``. Known negative: Azure chat flattens a root ``oneOf``.
# ---------------------------------------------------------------------------


def _azure_settings(model: str, base: str, setting: str) -> WebSettings:
    # LiteLLM rejects ``reasoning_effort`` for ``azure/gpt-4.1`` before
    # sending, and ``apply_reasoning_kwargs`` adds it for any ``azure/``
    # model at the default discovery effort, so the chat rows use the
    # operator's opt-out. The bridge rows keep the default.
    overrides: dict[str, Any] = {"composer_discovery_reasoning_effort": "none"} if model == "azure/gpt-4.1" else {}
    return _route_settings(model, base, setting, **overrides)


def test_azure_chat_forwards_the_stamps(loopback: _Loopback, monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization, row 9 under ``forward_to_endpoint`` (``ENFORCING``): the stamps reach the deployment's chat path."""
    monkeypatch.delenv("AZURE_API_VERSION", raising=False)
    request = _loop_request(_azure_settings("azure/gpt-4.1", loopback.base, "forward_to_endpoint"))
    assert request.transport is StrictTransport.ENFORCING

    recorded = _send_loopback(loopback, request.kwargs)

    assert recorded.path == "/openai/deployments/gpt-4.1/chat/completions"
    assert recorded.query == {"api-version": ["2025-02-01-preview"]}
    flags = _strict_flags(_chat_functions(recorded.body))
    assert flags == _sent_flags(request.sent_tools)
    _assert_openai_strict_stamps(flags)


def test_azure_chat_preferred_control_sends_no_strict_key(loopback: _Loopback, monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization, row 9 under ``preferred`` (ruling 7): ``none``; no tool carries ``strict``."""
    monkeypatch.delenv("AZURE_API_VERSION", raising=False)
    request = _loop_request(_azure_settings("azure/gpt-4.1", loopback.base, "preferred"))
    assert request.transport is StrictTransport.NONE

    recorded = _send_loopback(loopback, request.kwargs)

    _assert_no_strict_key(_strict_flags(_chat_functions(recorded.body)), tool_count=42)


def test_azure_chat_flattens_a_root_one_of(loopback: _Loopback, monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization, known negative: a synthetic root ``oneOf`` is flattened into one object.

    The branches' properties are merged and ``required`` and
    ``additionalProperties`` are lost. The same tool reaches the wire
    verbatim on OpenRouter. Hosted OpenAI chat applies the same transform
    (measured in the T11 probe log). S1 sends no root ``oneOf`` on any
    strict tool.
    """
    monkeypatch.delenv("AZURE_API_VERSION", raising=False)
    request = _loop_request(_azure_settings("azure/gpt-4.1", loopback.base, "preferred"), extra_tools=(_root_one_of_tool(),))

    recorded = _send_loopback(loopback, request.kwargs)

    parameters = _chat_functions(recorded.body)["zz_one_of"]["parameters"]
    assert parameters == {"type": "object", "properties": {"b": {"type": "integer"}, "a": {"type": "string"}}}


def test_azure_chat_keeps_a_plain_object_root(loopback: _Loopback, monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization, the known negative's control: without the root ``oneOf`` the parameters are sent unchanged."""
    monkeypatch.delenv("AZURE_API_VERSION", raising=False)
    request = _loop_request(_azure_settings("azure/gpt-4.1", loopback.base, "preferred"), extra_tools=(_plain_object_tool(),))

    recorded = _send_loopback(loopback, request.kwargs)

    assert _chat_functions(recorded.body)["zz_one_of"]["parameters"] == _ONE_OF_BRANCH_A


def test_azure_responses_bridge_forwards_the_stamps(loopback: _Loopback, monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization, row 9 under ``forward_to_endpoint``: ``azure/gpt-5.5`` is bridged to ``/openai/v1/responses`` with its stamps."""
    monkeypatch.delenv("AZURE_API_VERSION", raising=False)
    request = _loop_request(_azure_settings("azure/gpt-5.5", loopback.base, "forward_to_endpoint"))
    assert request.transport is StrictTransport.ENFORCING

    recorded = _send_loopback(loopback, request.kwargs)

    assert recorded.path == "/openai/v1/responses"
    assert recorded.query == {"api-version": ["preview"]}
    flags = _strict_flags(_responses_tools(recorded.body))
    assert flags == _sent_flags(request.sent_tools)
    _assert_openai_strict_stamps(flags)


def test_azure_responses_bridge_preferred_control_writes_strict_null(loopback: _Loopback, monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterization, row 9 under ``preferred``: ``none``; the list has no key and the bridge writes ``strict: null``.

    Today's bytes on the bridge (see the hosted bridge's known negative).
    """
    monkeypatch.delenv("AZURE_API_VERSION", raising=False)
    request = _loop_request(_azure_settings("azure/gpt-5.5", loopback.base, "preferred"))
    assert request.transport is StrictTransport.NONE
    _assert_no_strict_key(_sent_flags(request.sent_tools), tool_count=42)

    recorded = _send_loopback(loopback, request.kwargs)

    flags = _strict_flags(_responses_tools(recorded.body))
    assert len(flags) == 42
    assert set(flags.values()) == {None}


# ---------------------------------------------------------------------------
# Native Anthropic (resolver row 2, D8): always ``none``. Known negative: a
# forced stamped list loses its ``strict`` key on the wire.
# ---------------------------------------------------------------------------


def test_native_anthropic_drops_a_forced_strict_stamp(loopback: _Loopback) -> None:
    """Characterization, known negative: with a forced ``openai_strict`` list, no tool carries ``strict`` on the wire.

    This is why D8 resolves every Anthropic-family route to ``none``, even
    under ``forward_to_endpoint``. The same stamped list is forwarded
    verbatim on OpenRouter (``test_openrouter_forwards_the_stamped_list_verbatim``).
    """
    settings = _route_settings(_ANTHROPIC_MODEL, f"{loopback.base}/v1/messages", "forward_to_endpoint")
    request = _loop_request(settings, forced_dialect=ToolContractDialect.OPENAI_STRICT)
    assert request.transport is StrictTransport.NONE
    _assert_openai_strict_stamps(_sent_flags(request.sent_tools))

    recorded = _send_loopback(loopback, request.kwargs)

    assert recorded.path == "/v1/messages"
    _assert_no_strict_key(_strict_flags(_anthropic_tools(recorded.body)), tool_count=42)


def test_native_anthropic_resolved_list_control(loopback: _Loopback) -> None:
    """Characterization, the known negative's control: the resolved (``none``) list is sent as it was built.

    With the forced stamps removed there is nothing for the adapter to
    drop, and the wire matches the list sent: no tool carries ``strict``.
    """
    settings = _route_settings(_ANTHROPIC_MODEL, f"{loopback.base}/v1/messages", "forward_to_endpoint")
    request = _loop_request(settings)
    assert request.transport is StrictTransport.NONE
    assert request.dialect is ToolContractDialect.NONE

    recorded = _send_loopback(loopback, request.kwargs)

    flags = _strict_flags(_anthropic_tools(recorded.body))
    assert flags == _sent_flags(request.sent_tools)
    _assert_no_strict_key(flags, tool_count=42)


# ---------------------------------------------------------------------------
# Bedrock converse (resolver row 2, D8) on a loopback api_base: always
# ``none``. Known negative: an ``enum`` loses its ``null``.
# ---------------------------------------------------------------------------


def _bedrock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-matrix-access-key-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-matrix-secret-access-key")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")


@pytest.mark.parametrize(
    ("values", "expected_on_wire"),
    [
        pytest.param(["a", "b", None], ["a", "b"], id="known_negative_null_dropped"),
        pytest.param(["a", "b"], ["a", "b"], id="control_without_null"),
    ],
)
def test_bedrock_converse_enum_null(
    loopback: _Loopback,
    monkeypatch: pytest.MonkeyPatch,
    values: list[str | None],
    expected_on_wire: list[str],
) -> None:
    """Characterization, known negative: the converse adapter drops ``null`` from an ``enum``; no tool carries ``strict``.

    The ``null``-bearing enum reaches the wire verbatim on OpenRouter. The
    control row removes the ``null`` and shows the enum unchanged.
    """
    _bedrock_env(monkeypatch)
    request = _loop_request(_route_settings(_BEDROCK_MODEL, loopback.base, "forward_to_endpoint"), extra_tools=(_enum_tool(values),))
    assert request.transport is StrictTransport.NONE

    recorded = _send_loopback(loopback, request.kwargs)

    assert recorded.path == f"/model/{_BEDROCK_MODEL.removeprefix('bedrock/')}/converse"
    specs = _converse_tool_specs(recorded.body)
    _assert_no_strict_key(_strict_flags(specs), tool_count=43)
    assert specs["zz_enum"]["inputSchema"]["json"]["properties"]["e"] == {"enum": expected_on_wire}

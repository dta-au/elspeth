"""The guard's physical requests and recorded decisions have stable wire form.

Two goldens, both produced from the reference tree and compared on this one:

- ``azure_wire.json``: complete guard checks through ``enforce_prompt_guard``;
- ``azure_direct_wire.json``: one owned request sent through the Azure adapter, for batching the guard core
  never produces (the core packs short units, so a call never holds more than two packed documents).
  Recorded from the adapter's previous whole-request inspection, which no longer exists. This golden is frozen:
  do not regenerate it, and change it only by a deliberate, reviewed re-derivation. Setting
  ``WRITE_GUARD_WIRE_GOLDEN`` rewrites only ``azure_wire.json``.

Every scenario states the request count and decision it exists to show; a scenario that silently stops before
the wire fails here before any golden comparison.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from elspeth.contracts.prompt_guard import PromptGuardDecision, PromptGuardRequest
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, enforce_prompt_guard
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.models import chat_messages_table
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service

GOLDEN_DIR = Path(__file__).parents[3] / "golden/web/prompt_guard"
CHECK_GOLDEN = GOLDEN_DIR / "azure_wire.json"
DIRECT_GOLDEN = GOLDEN_DIR / "azure_direct_wire.json"
_HEADERS = ("accept-encoding", "content-type", "content-length")
# Headers whose relative order on the wire is recorded (names only, so the key's value never reaches the golden).
_ORDERED_HEADERS = frozenset({"ocp-apim-subscription-key", *_HEADERS})
_PROMPT = "Build a graph"
_SIX_THOUSAND = 6000

# Scenarios allowed to send nothing, each for its stated reason.
ZERO_REQUEST_SCENARIOS = frozenset(
    {
        "oversize_unit_enforced",  # refused before the wire: a unit over the character limit
        "document_over_limit_is_refused",  # refused before the wire: a unit over the character limit
        "oversize_unit_not_enforced",  # no guard mounted: nothing to send
        "client_open_error",  # the transport fails before the first request
    }
)


class LifecycleTransport(httpx.MockTransport):
    """A recording transport whose open or close can fail like a broken connection pool."""

    def __init__(self, handler: Callable[[httpx.Request], Awaitable[httpx.Response]], *, fail_on: str | None) -> None:
        super().__init__(handler)
        self._fail_on = fail_on

    async def __aenter__(self) -> LifecycleTransport:
        if self._fail_on == "open":
            raise httpx.ConnectError("transport open failed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        # "close_on_clean_exit" fails only when the client closes without an error in flight.
        if self._fail_on == "close_on_clean_exit" and exc_type is None:
            raise httpx.CloseError("transport close failed")
        await super().__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        if self._fail_on == "close":
            raise httpx.CloseError("transport close failed")


def recorded(sent: httpx.Request) -> dict[str, object]:
    return {
        "method": sent.method,
        "url": str(sent.url),
        "headers": {key: sent.headers[key] for key in _HEADERS if key in sent.headers},
        "header_order": [key for key, _value in sent.headers.multi_items() if key in _ORDERED_HEADERS],
        "subscription_key_present": "ocp-apim-subscription-key" in sent.headers,
        "body_sha256": hashlib.sha256(sent.content).hexdigest(),
    }


def answer(body: dict[str, object], *, attack: bool) -> httpx.Response:
    documents = body["documents"]
    assert type(documents) is list
    return httpx.Response(
        200,
        json={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": attack} for _ in documents],
        },
    )


def settings_for(timeout_seconds: float = 10.0) -> PromptGuardSettings:
    return PromptGuardSettings.model_validate(
        {"endpoint": "https://guard.example.test", "subscription_key": "fixture-guard-credential", "timeout_seconds": timeout_seconds}
    )


@dataclass(frozen=True)
class CheckScenario:
    messages: tuple[dict[str, str], ...]
    prompt: str
    expected_requests: int
    expected: tuple[str, str]
    trusted: frozenset[int] = frozenset()
    configured: bool = True
    repeats: int = 1
    fail_on: str | None = None
    answer_mode: str = "clean"


def _tool(content: str) -> dict[str, str]:
    return {"role": "tool", "content": content}


def _at_limit_content(extra: int) -> str:
    overhead = len(json.dumps(_tool(""), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return "x" * (10_000 - overhead + extra)


def _large(count: int) -> tuple[dict[str, str], ...]:
    return tuple(_tool(chr(65 + index) * _SIX_THOUSAND) for index in range(count))


CHECK_SCENARIOS: dict[str, CheckScenario] = {
    "prompt_only": CheckScenario(({"role": "user", "content": _PROMPT},), _PROMPT, 1, ("allow", "clean")),
    "documents_only": CheckScenario((_tool("Graph evidence"),), "", 1, ("allow", "clean")),
    "prompt_plus_documents": CheckScenario((_tool("Graph evidence"), {"role": "user", "content": _PROMPT}), _PROMPT, 2, ("allow", "clean")),
    "five_large_documents_split_by_characters": CheckScenario(_large(5), "", 5, ("allow", "clean")),
    "six_large_documents_split_by_characters": CheckScenario(_large(6), "", 6, ("allow", "clean")),
    "attack_on_second_document_call_stops_later_calls": CheckScenario(
        _large(6), "", 2, ("blocked", "policy_blocked"), answer_mode="attack_on_second"
    ),
    "document_at_limit_is_sent": CheckScenario((_tool(_at_limit_content(0)),), "", 1, ("allow", "clean")),
    "document_over_limit_is_refused": CheckScenario((_tool(_at_limit_content(1)),), "", 0, ("unavailable", "unsupported_input")),
    "oversize_unit_enforced": CheckScenario(
        (_tool("x" * 10_001), {"role": "user", "content": _PROMPT}), _PROMPT, 0, ("unavailable", "unsupported_input")
    ),
    "oversize_unit_not_enforced": CheckScenario(
        (_tool("x" * 10_001), {"role": "user", "content": _PROMPT}),
        _PROMPT,
        0,
        ("debug_bypass", "guard_not_configured"),
        configured=False,
    ),
    "non_ascii_text": CheckScenario((_tool("Café 🚀"), {"role": "user", "content": "Résumé"}), "Résumé", 2, ("allow", "clean")),
    "trusted_beside_untrusted": CheckScenario(
        ({"role": "system", "content": "Instruction"}, _tool("Evidence")), "", 1, ("allow", "clean"), trusted=frozenset({0})
    ),
    "second_check_reuses_units": CheckScenario(
        (_tool("Graph evidence"), {"role": "user", "content": _PROMPT}), _PROMPT, 2, ("allow", "clean"), repeats=2
    ),
    "non_200_answer": CheckScenario(
        ({"role": "user", "content": _PROMPT},), _PROMPT, 1, ("unavailable", "service_error"), answer_mode="non_200"
    ),
    "invalid_body": CheckScenario(
        ({"role": "user", "content": _PROMPT},), _PROMPT, 1, ("unavailable", "invalid_response"), answer_mode="invalid"
    ),
    "timeout": CheckScenario(({"role": "user", "content": _PROMPT},), _PROMPT, 1, ("unavailable", "timeout"), answer_mode="slow"),
    "client_open_error": CheckScenario(
        (_tool("Graph evidence"), {"role": "user", "content": _PROMPT}),
        _PROMPT,
        0,
        ("unavailable", "service_error"),
        fail_on="open",
    ),
    "client_close_error": CheckScenario(
        (_tool("Graph evidence"), {"role": "user", "content": _PROMPT}),
        _PROMPT,
        2,
        ("unavailable", "service_error"),
        fail_on="close",
    ),
    "call_timeout_reaches_an_exit_sensitive_close": CheckScenario(
        ({"role": "user", "content": _PROMPT},),
        _PROMPT,
        1,
        ("unavailable", "timeout"),
        fail_on="close_on_clean_exit",
        answer_mode="read_timeout",
    ),
}


async def _serve(sent: httpx.Request, *, mode: str, requests: list[dict[str, object]]) -> httpx.Response:
    requests.append(recorded(sent))
    if mode == "slow":
        await asyncio.sleep(0.03)
    if mode == "non_200":
        return httpx.Response(503)
    if mode == "invalid":
        return httpx.Response(200, content=b"invalid")
    if mode == "read_timeout":
        raise httpx.ReadTimeout("read timed out", request=sent)
    return answer(json.loads(sent.content), attack=mode == "attack_on_second" and len(requests) == 2)


@pytest.mark.asyncio
async def test_azure_check_wire_equivalence(tmp_path: Path) -> None:
    actual: dict[str, object] = {}
    for name, scenario in CHECK_SCENARIOS.items():
        (tmp_path / name).mkdir()
        engine, authority, service = _quota_service(tmp_path / name)
        session = authority.create_session_with_initial_fence(
            user_id="alice", title="Guard", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
        )
        context = authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
        )
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
        )
        owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
        requests: list[dict[str, object]] = []

        async def serve(
            sent: httpx.Request, *, mode: str = scenario.answer_mode, sink: list[dict[str, object]] = requests
        ) -> httpx.Response:
            return await _serve(sent, mode=mode, requests=sink)

        transport = LifecycleTransport(serve, fail_on=scenario.fail_on)
        client = AzurePromptGuardClient(settings_for(0.005 if scenario.answer_mode == "slow" else 10.0), http_transport=transport)
        # One mount per scenario: allowed units are reused within a mount, as across a session's checks.
        mount = PromptGuardMount(client) if scenario.configured else None
        for _ in range(scenario.repeats):
            with suppress(PromptGuardRefused):
                await enforce_prompt_guard(
                    mount=mount,
                    mode="required" if scenario.configured else "debug-when-unconfigured",
                    role="composer",
                    provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
                    messages=list(scenario.messages),
                    user_prompt=scenario.prompt,
                    trusted_message_indexes=scenario.trusted,
                )
        with engine.connect() as connection:
            rows = connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
        outcomes = [
            {
                key: row.tool_calls[0]["check"][key]
                for key in ("disposition", "reason", "units_scanned", "units_reused", "policy_fingerprint", "adapter", "schema_version")
            }
            for row in rows
        ]
        assert len(requests) == scenario.expected_requests, (name, len(requests))
        assert (outcomes[0]["disposition"], outcomes[0]["reason"]) == scenario.expected, (name, outcomes[0])
        assert len(outcomes) == scenario.repeats, name
        actual[name] = {"requests": requests, "outcomes": outcomes}
        coordinator.assert_completed()
        authority.release(context)
        engine.dispose()
    _compare(actual, CHECK_GOLDEN)


def test_only_named_scenarios_may_send_nothing() -> None:
    silent = {name for name, scenario in CHECK_SCENARIOS.items() if scenario.expected_requests == 0}
    assert silent == ZERO_REQUEST_SCENARIOS
    golden = json.loads(CHECK_GOLDEN.read_text())
    assert set(golden) == set(CHECK_SCENARIOS)
    assert {name for name, entry in golden.items() if not entry["requests"]} == ZERO_REQUEST_SCENARIOS


# Direct adapter requests: one owned request, the batching the guard core cannot produce.
DIRECT_SCENARIOS: dict[str, tuple[PromptGuardRequest, int, tuple[str, str]]] = {
    "five_short_documents_share_one_call": (
        PromptGuardRequest("composer", "", tuple(f"Document {index}" for index in range(5)), "a" * 64),
        1,
        ("allow", "clean"),
    ),
    "six_short_documents_split_after_five": (
        PromptGuardRequest("composer", "", tuple(f"Document {index}" for index in range(6)), "a" * 64),
        2,
        ("allow", "clean"),
    ),
    "documents_totalling_the_character_limit_share_one_call": (
        PromptGuardRequest("composer", "", ("a" * 4000, "b" * 6000), "a" * 64),
        1,
        ("allow", "clean"),
    ),
    "documents_one_character_over_the_limit_split_into_two_calls": (
        PromptGuardRequest("composer", "", ("a" * 4001, "b" * 6000), "a" * 64),
        2,
        ("allow", "clean"),
    ),
    "attack_on_second_of_three_calls_stops_the_third": (
        PromptGuardRequest("composer", "", tuple(f"Document {index}" for index in range(11)), "a" * 64),
        2,
        ("blocked", "policy_blocked"),
    ),
    "prompt_rides_beside_every_document_call": (
        PromptGuardRequest("composer", _PROMPT, tuple(f"Document {index}" for index in range(7)), "a" * 64),
        2,
        ("allow", "clean"),
    ),
}

DirectEntry = Callable[[AzurePromptGuardClient, PromptGuardRequest], Awaitable[PromptGuardDecision]]


async def direct_wire(entry: DirectEntry) -> dict[str, object]:
    """Send each direct scenario through ``entry`` and record the wire; ``entry`` is the tree's request path."""
    actual: dict[str, object] = {}
    for name, (request, expected_requests, expected) in DIRECT_SCENARIOS.items():
        requests: list[dict[str, object]] = []
        mode = "attack_on_second" if name.startswith("attack_") else "clean"

        async def serve(sent: httpx.Request, *, answer_mode: str = mode, sink: list[dict[str, object]] = requests) -> httpx.Response:
            return await _serve(sent, mode=answer_mode, requests=sink)

        client = AzurePromptGuardClient(settings_for(), http_transport=httpx.MockTransport(serve))
        decision = await entry(client, request)
        assert len(requests) == expected_requests, (name, len(requests))
        assert (decision.disposition, decision.reason) == expected, (name, decision)
        actual[name] = {"requests": requests, "decision": [decision.disposition, decision.reason]}
    return actual


@pytest.mark.asyncio
async def test_azure_direct_wire_equivalence() -> None:
    from elspeth.web.composer.prompt_guard import inspect_prompt_guard_request

    _compare(await direct_wire(inspect_prompt_guard_request), DIRECT_GOLDEN, writable=False)


def _compare(actual: dict[str, object], golden: Path, *, writable: bool = True) -> None:
    if writable and os.environ.get("WRITE_GUARD_WIRE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    assert json.loads(golden.read_text()) == actual

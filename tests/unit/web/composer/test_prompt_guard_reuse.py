"""Prompt guard reuse: only text not already allowed in this session is sent.

Every test drives the real Azure client over an HTTP-only simulation and the
real fenced audit path. Each test ends by proving a reuse hit, so the
instrument that shows a re-scan is also shown able to show its absence.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from elspeth.contracts.prompt_guard import PromptGuardClient, PromptGuardContentPart, PromptGuardDecision, PromptGuardRequest
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer import prompt_guard
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, enforce_prompt_guard
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily, ProviderInvocationOwner
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

_MARKER = re.compile(r"EVT-\d{3}")


class _Shield:
    """HTTP-only Prompt Shields simulation recording every physical call."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.blocked_markers: set[str] = set()
        self.failure: str | None = None
        self.entered = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.payloads.append(payload)
        if self.failure == "service_error":
            return httpx.Response(500)
        if self.failure == "invalid_response":
            return httpx.Response(200, json={"unexpected": True})
        if self.failure == "timeout":
            raise httpx.ReadTimeout("simulated", request=request)
        if self.failure == "slow":
            await asyncio.sleep(0.3)
        if self.failure == "hang" or self.failure == ("hang_documents" if payload["documents"] else "hang_prompt"):
            self.entered.set()
            await asyncio.Event().wait()
        documents = payload["documents"]
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": any(marker in payload["userPrompt"] for marker in self.blocked_markers)},
                "documentsAnalysis": [
                    {"attackDetected": any(marker in document for marker in self.blocked_markers)} for document in documents
                ],
            },
        )

    def client(self, endpoint: str = "https://guard.example.test", *, timeout_seconds: float = 10.0) -> AzurePromptGuardClient:
        settings = PromptGuardSettings(endpoint=endpoint, subscription_key="fixture-guard-key", timeout_seconds=timeout_seconds)
        return AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(self))

    def since(self, mark: int) -> list[dict[str, Any]]:
        return self.payloads[mark:]


class _SwitchablePolicyClient(PromptGuardClient):
    """Same transport, operator-switchable policy identity (one shared mount)."""

    def __init__(self, inner: AzurePromptGuardClient) -> None:
        self._inner = inner
        self.fingerprint = inner.policy_fingerprint

    @property
    def adapter(self) -> str:
        return self._inner.adapter

    @property
    def policy_fingerprint(self) -> str:
        return self.fingerprint

    @property
    def timeout_seconds(self) -> float:
        return self._inner.timeout_seconds

    async def inspect(self, request: PromptGuardRequest) -> PromptGuardDecision:
        return await self._inner.inspect(request)


@pytest.fixture
def guard_sessions(tmp_path: Any) -> Iterator[tuple[Any, Any]]:
    engine, authority, service = _quota_service(tmp_path)
    contexts = []

    def open_owner() -> ProviderInvocationOwner:
        session = authority.create_session_with_initial_fence(
            user_id="alice", title="Guard reuse", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
        )
        context = authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
        )
        contexts.append(context)
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
        )
        return ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))

    yield engine, open_owner
    for context in contexts:
        authority.release(context)
    engine.dispose()


def _event(index: int, suffix: str = "") -> str:
    return f"EVT-{index:03d} observed field value {index}{suffix}\n"


async def _check(
    mount: PromptGuardMount,
    owner: ProviderInvocationOwner,
    *,
    texts: Iterable[str],
    user: str = "Rename the finding field.",
    role: Any = "advisor",
    mode: Any = "required",
    custody_out: list[ProviderCallCustody] | None = None,
) -> ProviderCallCustody:
    parts = tuple(PromptGuardContentPart(text) for text in texts)
    custody = owner.mint(ProviderInvocationFamily.ADVISOR)
    if custody_out is not None:
        custody_out.append(custody)
    await enforce_prompt_guard(
        mount=mount,
        mode=mode,
        role=role,
        provider_custody=custody,
        messages=[{"role": "system", "content": "TRUSTED-RUBRIC"}, {"role": "user", "content": "".join(part.text for part in parts)}],
        user_prompt=user,
        trusted_message_indexes=frozenset({0}),
        content_parts=(None, parts),
    )
    return custody


def _markers(payloads: list[dict[str, Any]]) -> list[str]:
    return sorted({marker for payload in payloads for document in payload["documents"] for marker in _MARKER.findall(document)})


def _last_check(engine: Any, custody: ProviderCallCustody) -> dict[str, Any]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(chat_messages_table)
            .where(chat_messages_table.c.session_id == custody.context.fence.session_id)
            .order_by(chat_messages_table.c.sequence_no)
        ).all()
    checks = [row.tool_calls[0]["check"] for row in rows if row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"]
    return checks[-1]


@pytest.mark.asyncio
async def test_documents_sent_per_check_stay_flat_as_the_transcript_grows(guard_sessions) -> None:
    engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    calls_per_check: list[int] = []
    recorded_counts: list[tuple[object, object]] = []
    for turn in range(1, 13):
        mark = len(shield.payloads)
        custody = await _check(mount, owner, texts=[_event(index) for index in range(turn)], user=f"Turn {turn}: rename the field.")
        sent = shield.since(mark)
        assert _markers(sent) == [f"EVT-{turn - 1:03d}"], f"turn {turn} re-sent earlier allowed units"
        calls_per_check.append(len(sent))
        check = _last_check(engine, custody)
        assert (check["disposition"], check["reason"]) == ("allow", "clean")
        recorded_counts.append((check.get("units_scanned"), check.get("units_reused")))
    # One user prompt call and one document call per check, flat over turns.
    assert calls_per_check == [2] * 12
    # New per check: the user prompt and one event (plus the shadow message on
    # turn 1); every earlier event is reused.
    assert recorded_counts == [(3, 0)] + [(2, turn) for turn in range(2, 13)]


@pytest.mark.asyncio
async def test_shifted_position_label_does_not_defeat_reuse(guard_sessions) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    texts = [_event(index) for index in range(1, 5)]
    await _check(mount, owner, texts=texts)
    mark = len(shield.payloads)
    # A new leading unit shifts every later "content part N" label by one.
    await _check(mount, owner, texts=[_event(0), *texts])
    sent = shield.since(mark)
    assert [document for payload in sent for document in payload["documents"]] == ["Message 1, content part 0:\n" + _event(0)]


@pytest.mark.asyncio
async def test_check_with_nothing_new_makes_no_shield_call_and_says_so(guard_sessions) -> None:
    engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    texts = [_event(index) for index in range(4)]
    await _check(mount, owner, texts=texts)
    mark = len(shield.payloads)
    custody = await _check(mount, owner, texts=texts)
    assert shield.since(mark) == []
    check = _last_check(engine, custody)
    assert (check["disposition"], check["reason"], check["adapter"]) == ("allow", "clean", "azure_prompt_shields")
    assert check["units_scanned"] == 0
    assert check["units_reused"] == 6  # user prompt, shadow message, four events
    assert check["schema_version"] == 2


@pytest.mark.asyncio
async def test_no_document_call_carries_the_user_prompt(guard_sessions) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    user = "Please rename the field EVT-USER."
    await _check(mount, owner, texts=[_event(index) for index in range(3)], user=user)
    prompt_calls = [payload for payload in shield.payloads if payload["userPrompt"]]
    document_calls = [payload for payload in shield.payloads if payload["documents"]]
    assert prompt_calls and document_calls
    assert all(payload["documents"] == [] and payload["userPrompt"] == user for payload in prompt_calls)
    assert all(payload["userPrompt"] == "" for payload in document_calls)
    assert all("EVT-USER" not in document for payload in document_calls for document in payload["documents"])


@pytest.mark.asyncio
async def test_one_changed_byte_rescans_only_that_unit(guard_sessions) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    texts = [_event(index) for index in range(5)]
    await _check(mount, owner, texts=texts)
    changed = [*texts[:2], _event(2, "!"), *texts[3:]]
    mark = len(shield.payloads)
    await _check(mount, owner, texts=changed)
    assert _markers(shield.since(mark)) == ["EVT-002"]
    assert all(payload["userPrompt"] == "" for payload in shield.since(mark))
    mark = len(shield.payloads)
    await _check(mount, owner, texts=changed)
    assert shield.since(mark) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["session", "policy", "mode", "role", "slot"])
async def test_reuse_never_crosses_session_policy_mode_role_or_slot(guard_sessions, dimension: str) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    client = _SwitchablePolicyClient(shield.client())
    mount = PromptGuardMount(client)
    owner = open_owner()
    texts = [_event(index) for index in range(3)]
    await _check(mount, owner, texts=texts)
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts)
    assert shield.since(mark) == [], "control: identical check reuses everything"
    kwargs: dict[str, Any] = {"texts": texts}
    if dimension == "session":
        owner = open_owner()
    elif dimension == "policy":
        client.fingerprint = "b" * 64
    elif dimension == "mode":
        kwargs["mode"] = "debug-when-unconfigured"
    elif dimension == "role":
        kwargs["role"] = "composer"
    else:
        # The exact user prompt text, now a document, is still sent as one.
        kwargs = {"texts": [*texts, "Rename the finding field."]}
    mark = len(shield.payloads)
    await _check(mount, owner, **kwargs)
    sent = shield.since(mark)
    if dimension == "slot":
        assert [document for payload in sent for document in payload["documents"]] == [
            "Message 1, content part 3:\nRename the finding field."
        ]
    else:
        assert _markers(sent) == ["EVT-000", "EVT-001", "EVT-002"]
        assert any(payload["userPrompt"] == "Rename the finding field." for payload in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["blocked_document", "blocked_user_prompt", "partial_batch", "service_error", "invalid_response", "timeout", "unsupported_input"],
)
async def test_refused_or_unavailable_check_leaves_nothing_reusable(guard_sessions, outcome: str) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    # 9,000-character units: one per packed document and one per Azure call.
    texts = [_event(index) + "x" * 9_000 for index in range(7)]
    user = "Rename the finding field."
    if outcome == "blocked_document":
        shield.blocked_markers = {"EVT-003"}
    elif outcome == "blocked_user_prompt":
        shield.blocked_markers = {"Rename"}
    elif outcome == "partial_batch":
        shield.blocked_markers = {"EVT-006"}
    elif outcome == "unsupported_input":
        texts = [*texts, "EVT-099 " + "y" * 10_001]
    else:
        shield.failure = outcome
    with pytest.raises(PromptGuardRefused):
        await _check(mount, owner, texts=texts, user=user)
    if outcome == "partial_batch":
        allowed_before_refusal = _markers(shield.payloads[:-1])
        assert allowed_before_refusal == [f"EVT-{index:03d}" for index in range(6)], "earlier batches were allowed"
    shield.blocked_markers = set()
    shield.failure = None
    texts = texts[:7]
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts, user=user)
    sent = shield.since(mark)
    assert _markers(sent) == [f"EVT-{index:03d}" for index in range(7)]
    assert any(payload["userPrompt"] == user for payload in sent)
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts, user=user)
    assert shield.since(mark) == []


@pytest.mark.asyncio
async def test_cancelled_check_leaves_nothing_reusable(guard_sessions) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    texts = [_event(index) for index in range(3)]
    shield.failure = "hang"
    task = asyncio.create_task(_check(mount, owner, texts=texts))
    await shield.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    shield.failure = None
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts)
    assert _markers(shield.since(mark)) == ["EVT-000", "EVT-001", "EVT-002"]
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts)
    assert shield.since(mark) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "units_started"),
    [
        pytest.param("hang_prompt", 1, id="prompt-call-hangs"),
        pytest.param("hang_documents", 5, id="documents-call-hangs"),
        # Each call alone fits the deadline; the two together do not.
        pytest.param("slow", 5, id="both-calls-slow"),
    ],
)
async def test_one_deadline_covers_both_calls_and_a_timeout_admits_nothing(guard_sessions, failure: str, units_started: int) -> None:
    engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client(timeout_seconds=0.5))
    owner = open_owner()
    texts = [_event(index) for index in range(3)]
    user = "Rename the finding field."
    shield.failure = failure
    custodies: list[ProviderCallCustody] = []
    with pytest.raises(PromptGuardRefused):
        await _check(mount, owner, texts=texts, user=user, custody_out=custodies)
    check = _last_check(engine, custodies[0])
    assert (check["disposition"], check["reason"]) == ("unavailable", "timeout")
    # Units of the calls that started: the prompt alone, or the prompt and the
    # shadow message plus three events once the documents call started.
    assert (check.get("units_scanned"), check.get("units_reused")) == (units_started, 0)
    assert len(shield.payloads) == (1 if failure == "hang_prompt" else 2)
    shield.failure = None
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts, user=user)
    sent = shield.since(mark)
    # Nothing was admitted, including a prompt the first call allowed.
    assert {"userPrompt": user, "documents": []} in sent
    assert _markers(sent) == ["EVT-000", "EVT-001", "EVT-002"]
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts, user=user)
    assert shield.since(mark) == []


@pytest.mark.asyncio
async def test_refused_prompt_still_sends_documents_and_counts_what_was_submitted(guard_sessions) -> None:
    engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    texts = [_event(index) for index in range(3)]
    user = "Rename the finding field."
    shield.blocked_markers = {"Rename"}
    custodies: list[ProviderCallCustody] = []
    with pytest.raises(PromptGuardRefused):
        await _check(mount, owner, texts=texts, user=user, custody_out=custodies)
    sent = list(shield.payloads)
    assert sent[0] == {"userPrompt": user, "documents": []}
    assert [payload["userPrompt"] for payload in sent[1:]] == [""] * len(sent[1:]) and sent[1:]
    assert _markers(sent) == ["EVT-000", "EVT-001", "EVT-002"]
    check = _last_check(engine, custodies[0])
    assert (check["disposition"], check["reason"]) == ("blocked", "policy_blocked")
    # Submitted: the prompt, the shadow message and three events.
    assert (check.get("units_scanned"), check.get("units_reused")) == (5, 0)
    shield.blocked_markers = set()
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts, user=user)
    resent = shield.since(mark)
    assert {"userPrompt": user, "documents": []} in resent
    assert _markers(resent) == ["EVT-000", "EVT-001", "EVT-002"]
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts, user=user)
    assert shield.since(mark) == []


@pytest.mark.asyncio
async def test_failed_audit_write_leaves_nothing_reusable(guard_sessions, monkeypatch) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    mount = PromptGuardMount(shield.client())
    owner = open_owner()
    texts = [_event(index) for index in range(3)]
    original = prompt_guard._persist

    async def failing_persist(**_kwargs: Any) -> None:
        raise RuntimeError("simulated audit write failure")

    monkeypatch.setattr(prompt_guard, "_persist", failing_persist)
    with pytest.raises(RuntimeError, match="simulated audit write failure"):
        await _check(mount, owner, texts=texts)
    monkeypatch.setattr(prompt_guard, "_persist", original)
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts)
    assert _markers(shield.since(mark)) == ["EVT-000", "EVT-001", "EVT-002"]
    mark = len(shield.payloads)
    await _check(mount, owner, texts=texts)
    assert shield.since(mark) == []


@pytest.mark.asyncio
async def test_eviction_only_causes_a_rescan_of_the_evicted_unit(guard_sessions) -> None:
    _engine, open_owner = guard_sessions
    shield = _Shield()
    assert "reuse_capacity" in inspect.signature(PromptGuardMount).parameters, "the mount must take a reuse capacity"
    # One check admits three keys in order: user prompt, shadow message, EVT-000.
    mount = PromptGuardMount(shield.client(), reuse_capacity=2)
    owner = open_owner()
    await _check(mount, owner, texts=[_event(0)])
    assert mount.reused_unit_count == 2
    mark = len(shield.payloads)
    await _check(mount, owner, texts=[_event(0)])
    assert shield.since(mark) == [{"userPrompt": "Rename the finding field.", "documents": []}]

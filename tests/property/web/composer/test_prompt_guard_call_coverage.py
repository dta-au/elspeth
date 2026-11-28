"""Physical prompt guard calls carry complete, ordered inspection units."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service

from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust
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

_OwnerScope = Callable[[], AbstractContextManager[ProviderInvocationOwner]]


@pytest.fixture
def guard_sessions(tmp_path: Any) -> Iterator[_OwnerScope]:
    """Yield a scope that opens one session operation and releases it when the scope ends."""
    engine, authority, service = _quota_service(tmp_path)

    @contextmanager
    def owner_scope() -> Iterator[ProviderInvocationOwner]:
        session = authority.create_session_with_initial_fence(
            user_id="alice", title="Guard coverage", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
        )
        context = authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
        )
        try:
            coordinator = RequiredWorkCoordinator(
                RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
            )
            yield ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
        finally:
            # Releases this operation's own fence whether or not its lease deadline has passed.
            authority.release_at_close(context)

    try:
        yield owner_scope
    finally:
        engine.dispose()


def _unit(index: int, length: int, character: str, line_width: int = 0) -> str:
    """Part text whose labelled unit is ``length`` characters; a positive width puts a newline every ``line_width``."""
    label = f"Message 1, content part {index + 1}:\n"
    marker = f"unit-{index}:"
    size = length - len(label) - len(marker)
    return marker + "".join("\n" if line_width and (position + 1) % line_width == 0 else character for position in range(size))


def _labelled(texts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"Message 1, content part {index + 1}:\n{text}" for index, text in enumerate(texts))


def _client(recorded: list[bytes]) -> AzurePromptGuardClient:
    def serve(request: httpx.Request) -> httpx.Response:
        recorded.append(request.content)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    return AzurePromptGuardClient(
        PromptGuardSettings(endpoint="https://guard.example.test", subscription_key="fixture-key"),
        http_transport=httpx.MockTransport(serve),
    )


def _parts(texts: tuple[str, ...]) -> tuple[PromptGuardContentPart, ...]:
    return (PromptGuardContentPart("trusted reference", PromptGuardTrust.REFERENCE), *(PromptGuardContentPart(text) for text in texts))


async def _send(mount: PromptGuardMount, owner: ProviderInvocationOwner, parts: tuple[PromptGuardContentPart, ...], prompt: str) -> bool:
    custody = owner.mint(ProviderInvocationFamily.ADVISOR)
    try:
        await enforce_prompt_guard(
            mount=mount,
            mode="required",
            role="advisor",
            provider_custody=custody,
            messages=[
                {"role": "system", "content": "trusted instructions"},
                {"role": "user", "content": "".join(part.text for part in parts)},
            ],
            user_prompt=prompt,
            trusted_message_indexes=frozenset({0}),
            content_parts=(None, parts),
        )
    except PromptGuardRefused:
        return False
    return True


def _documents(bodies: list[dict[str, Any]]) -> list[str]:
    return [document for body in bodies for document in body["documents"]]


def _assert_calls(bodies: list[dict[str, Any]], prompt: str, units: tuple[str, ...]) -> None:
    """Calls keep the limits, send the prompt once in the first call, and carry each unit whole in one document, in order."""
    assert bodies
    assert [body["userPrompt"] for body in bodies] == ([prompt] if prompt else []) + [""] * (len(bodies) - bool(prompt))
    for body in bodies:
        assert len(body["documents"]) <= 5
        assert sum(len(document) for document in body["documents"]) <= 10_000
    documents = _documents(bodies)
    previous = (-1, 0)  # (document index, end offset) of the previous unit
    for unit in units:
        holders = [index for index, document in enumerate(documents) if unit in document]
        assert len(holders) == 1, f"unit {unit[:40]!r} is not whole inside exactly one document"
        index = holders[0]
        assert documents[index].count(unit) == 1
        start = documents[index].find(unit)
        assert (index, start) >= previous, f"unit {unit[:40]!r} is sent before the unit preceding it"
        previous = (index, start + len(unit))


@example(lengths=[10_000, 5_000, 4_999], character="é", prompt="", line_width=0)
@example(lengths=[10_000, 5_000, 5_000], character="a", prompt="Check the text", line_width=0)
@example(lengths=[64, 4_999, 5_000, 512], character="漢", prompt="", line_width=2)
@example(lengths=[48] * 12, character="漢", prompt="a different prompt", line_width=0)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    lengths=st.lists(st.sampled_from((48, 64, 512, 4_999, 5_000, 9_999, 10_000)), min_size=1, max_size=12),
    character=st.sampled_from(("a", "é", "漢", "🙂")),
    prompt=st.sampled_from(("", "Check the text", "Review 漢🙂")),
    line_width=st.sampled_from((0, 2, 79)),
)
def test_core_calls_cover_whole_units_with_limits_and_reuse(
    guard_sessions: _OwnerScope, lengths: list[int], character: str, prompt: str, line_width: int
) -> None:
    recorded: list[bytes] = []
    mount = PromptGuardMount(_client(recorded))
    texts = tuple(_unit(index, length, character, line_width) for index, length in enumerate(lengths))
    parts = _parts(texts)

    async def exercise(owner: ProviderInvocationOwner) -> None:
        allowed = await _send(mount, owner, parts, prompt)
        first = [json.loads(body) for body in recorded]
        _assert_calls(first, prompt, _labelled(texts))
        assert all("trusted reference" not in document for document in _documents(first))
        assert allowed

        recorded.clear()
        new_text = _unit(len(texts), 64, "ß", line_width)
        allowed = await _send(mount, owner, (*parts, PromptGuardContentPart(new_text)), prompt)
        second = [json.loads(body) for body in recorded]
        # Units allowed by the first check are not sent again: one call carries exactly the new unit.
        assert [(body["userPrompt"], body["documents"]) for body in second] == [("", [_labelled((*texts, new_text))[-1]])]
        assert allowed

    with guard_sessions() as owner:
        asyncio.run(exercise(owner))


@pytest.mark.parametrize(("joined_length", "joined"), [(10_000, True), (10_001, False)], ids=["at_limit", "over_limit"])
def test_units_share_one_document_only_while_their_joined_length_is_within_the_limit(
    guard_sessions: _OwnerScope, joined_length: int, joined: bool
) -> None:
    recorded: list[bytes] = []
    mount = PromptGuardMount(_client(recorded))
    # A unit at the full limit cannot share a document, so the two units after it start a new document together.
    left_length = 4_000
    texts = (_unit(0, 10_000, "a"), _unit(1, left_length, "b"), _unit(2, joined_length - 1 - left_length, "c"))
    labelled = _labelled(texts)
    assert len(labelled[1]) + 1 + len(labelled[2]) == joined_length

    async def exercise(owner: ProviderInvocationOwner) -> None:
        allowed = await _send(mount, owner, _parts(texts), "")
        bodies = [json.loads(body) for body in recorded]
        _assert_calls(bodies, "", labelled)
        documents = _documents(bodies)
        if joined:
            assert documents[-1] == labelled[1] + "\n" + labelled[2]
        else:
            assert documents[-2:] == [labelled[1], labelled[2]]
        assert allowed

    with guard_sessions() as owner:
        asyncio.run(exercise(owner))

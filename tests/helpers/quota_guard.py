"""Real required guard and fenced owner for quota integration tests."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.visible_tool_event import VISIBLE_RESPONSE_GAP_KIND, validate_visible_response_gap
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.service import SessionServiceImpl


def assert_provider_audit_companion(envelopes: Sequence[Mapping[str, Any]], context: SessionOperationContext) -> None:
    """Require exact physical-call coverage while keeping other rows singleton."""
    assert envelopes
    public = envelopes[0]
    if public["_kind"] != "llm_call_audit":
        assert len(envelopes) == 1
        return
    assert len(envelopes) == 2
    gap = envelopes[1]
    assert gap["_kind"] == VISIBLE_RESPONSE_GAP_KIND
    validate_visible_response_gap(gap, public)
    assert gap["session_id"] == context.fence.session_id
    assert gap["operation_id"] == context.fence.operation_id
    assert gap["operation_epoch"] == context.fence.operation_epoch


@dataclass
class GuardedQuotaTurn:
    engine: Engine
    context: SessionOperationContext
    owner: ProviderInvocationOwner
    coordinator: RequiredWorkCoordinator
    mount: PromptGuardMount
    user_prompt: str
    requests: list[httpx.Request]
    prior_rows: tuple

    def assert_audit(self, *, guards: int, calls: int, private: int) -> None:
        """Account for the entire new cohort, including every guard row."""
        with self.engine.connect() as connection:
            rows = tuple(
                connection.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == self.context.fence.session_id)
                    .order_by(chat_messages_table.c.sequence_no)
                ).all()
            )
        assert rows[: len(self.prior_rows)] == self.prior_rows
        added = rows[len(self.prior_rows) :]
        assert len(added) == guards + calls + private
        assert all(row.role == "audit" and row.writer_principal == "compose_loop" for row in added)
        for row in added:
            assert_provider_audit_companion(row.tool_calls, self.context)
        expected = Counter()
        if guards:
            expected["prompt_guard_audit"] = guards
        if calls:
            expected["llm_call_audit"] = calls
        if private:
            expected["composer_diagnostic_evidence"] = private
        assert Counter(row.tool_calls[0]["_kind"] for row in added) == expected
        checks = [row.tool_calls[0]["check"] for row in added if row.tool_calls[0]["_kind"] == "prompt_guard_audit"]
        assert len(checks) == guards
        assert len({check["audit_id"] for check in checks}) == guards
        for check in checks:
            assert set(check) == {
                "schema_version",
                "audit_id",
                "operation_id",
                "operation_epoch",
                "role",
                "adapter",
                "mode",
                "policy_fingerprint",
                "request_hash",
                "disposition",
                "reason",
                "started_at",
                "duration_ms",
            }
            assert check["schema_version"] == 1
            assert check["operation_id"] == self.context.fence.operation_id
            assert check["operation_epoch"] == self.context.fence.operation_epoch
            assert (check["adapter"], check["mode"], check["role"]) == ("azure_prompt_shields", "required", "composer")
            assert (check["disposition"], check["reason"]) == ("allow", "clean")
            assert check["policy_fingerprint"] == self.mount.client.policy_fingerprint
            assert len(check["request_hash"]) == 64
            assert all(character in "0123456789abcdef" for character in check["request_hash"])
        # A guard decision may batch documents into multiple real HTTP requests.
        assert len(self.requests) >= guards
        assert bool(self.requests) is bool(guards)
        for request in self.requests:
            assert request.method == "POST"
            assert request.url.host == "guard.example.test"
            payload = json.loads(request.content)
            assert payload["userPrompt"] == self.user_prompt
            assert set(payload) == {"userPrompt", "documents"}


@contextmanager
def guarded_quota_turn(
    engine: Engine,
    service: SessionServiceImpl,
    context: SessionOperationContext,
    *,
    user_prompt: str = "Build the requested pipeline.",
) -> Iterator[GuardedQuotaTurn]:
    """Use an actual Azure client with HTTP-only guard simulation and a real owner."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in payload["documents"]],
            },
        )

    mount = PromptGuardMount(
        AzurePromptGuardClient(
            PromptGuardSettings(endpoint="https://guard.example.test", subscription_key="fixture-guard-key"),
            http_transport=httpx.MockTransport(respond),
        )
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    with engine.connect() as connection:
        prior = tuple(
            connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == context.fence.session_id)
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        )
    turn = GuardedQuotaTurn(engine, context, owner, coordinator, mount, user_prompt, requests, prior)
    with prompt_guard_user_scope(user_prompt):
        try:
            yield turn
        except BaseException as primary:
            try:
                coordinator.assert_completed()
            except BaseException as incomplete:
                raise BaseExceptionGroup("Quota fixture body and incomplete required work", [primary, incomplete]) from None
            raise
        else:
            coordinator.assert_completed()

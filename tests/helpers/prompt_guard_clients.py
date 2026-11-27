"""One-call planning for test doubles that only need a decision."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from elspeth.contracts.prompt_guard import PromptGuardCall, PromptGuardCallPlan, PromptGuardClient, PromptGuardRequest


class SingleCallPromptGuardClient(PromptGuardClient):
    """Test convenience: carry a complete request in one physical call."""

    @asynccontextmanager
    async def request_context(self) -> AsyncIterator[None]:
        """No transport is held open for the request."""
        yield

    def plan_calls(self, request: PromptGuardRequest) -> PromptGuardCallPlan:
        payload = json.dumps(
            {
                "role": request.role,
                "user_prompt": request.user_prompt,
                "documents": request.documents,
                "request_hash": request.request_hash,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return PromptGuardCallPlan((PromptGuardCall(payload, tuple(range(len(request.documents))), bool(request.user_prompt)),))


def request_from_call(call: PromptGuardCall) -> PromptGuardRequest:
    """Recover the exact request carried by a one-call test plan."""
    body = json.loads(call.payload)
    return PromptGuardRequest(body["role"], body["user_prompt"], tuple(body["documents"]), body["request_hash"])

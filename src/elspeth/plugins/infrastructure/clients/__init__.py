"""Audited clients that automatically record external calls to the audit trail.

These clients wrap external service calls (LLM, HTTP) and ensure every
request/response is recorded to the Landscape audit trail for complete
traceability.

Example:
    from elspeth.contracts.chat_parts import ChatMessage
    from elspeth.plugins.infrastructure.clients import AuditedLLMClient, AuditedHTTPClient

    # Create audited LLM client
    llm_client = AuditedLLMClient(
        execution=execution,
        state_id=state_id,
        run_id=run_id,
        telemetry_emit=telemetry_emit,
        underlying_client=openai.OpenAI(),
        provider="openai",
    )

    # All calls are automatically recorded
    response = llm_client.chat_completion(
        model="gpt-4",
        messages=[ChatMessage(role="user", content="Hello")],
    )

Replay and verify modes each have a single authority in the engine:
``CallModeSession.replay_call`` and ``CallModeSession.verify_call``
(``elspeth.engine.orchestrator.call_mode_session``). They bind each source
call to its parent, enforce the Tier-1 status/payload/error invariants, and
record exact-hash evidence; the audited clients reach them through their
call-mode adapters. This package exports no separate replayer or verify
comparator.
"""

from elspeth.plugins.infrastructure.clients.base import AuditedClientBase
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from elspeth.plugins.infrastructure.clients.llm import (
    AuditedLLMClient,
    ContentPolicyError,
    ContextLengthError,
    LLMClientError,
    LLMResponse,
    NetworkError,
    RateLimitError,
    ServerError,
)

__all__ = [
    "AuditedClientBase",
    "AuditedHTTPClient",
    "AuditedLLMClient",
    "ContentPolicyError",
    "ContextLengthError",
    "LLMClientError",
    "LLMResponse",
    "NetworkError",
    "RateLimitError",
    "ServerError",
]

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

For replay mode, use CallReplayer to return recorded responses:

    from elspeth.plugins.infrastructure.clients import CallReplayer, ReplayMissError

    replayer = CallReplayer(execution, source_run_id="run-abc123")
    result = replayer.replay(call_type="llm", request_data={...})

Verify mode has a single authority: the engine's
``CallModeSession.verify_call`` (``elspeth.engine.orchestrator.call_mode_session``),
which binds each source call to its parent, compares call status and error
evidence, and records exact-hash verdicts. This package exports no separate
verify comparator.
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
from elspeth.plugins.infrastructure.clients.replayer import (
    CallReplayer,
    ReplayedCall,
    ReplayMissError,
    ReplayPayloadMissingError,
)

__all__ = [
    "AuditedClientBase",
    "AuditedHTTPClient",
    "AuditedLLMClient",
    "CallReplayer",
    "ContentPolicyError",
    "ContextLengthError",
    "LLMClientError",
    "LLMResponse",
    "NetworkError",
    "RateLimitError",
    "ReplayMissError",
    "ReplayPayloadMissingError",
    "ReplayedCall",
    "ServerError",
]

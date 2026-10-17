"""Dedicated prompt-guard admission and durable authoring audit custody."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from uuid import UUID, uuid4

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.contracts.prompt_guard import (
    PromptGuardClient,
    PromptGuardDecision,
    PromptGuardDisposition,
    PromptGuardMode,
    PromptGuardReason,
    PromptGuardRequest,
    PromptGuardRole,
)
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions._persist_payload import AuditMessageDraft

if TYPE_CHECKING:
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.prompt_guard_config import PromptGuardSettings

_USER_PROMPT: ContextVar[str | None] = ContextVar("composer_prompt_guard_user", default=None)


class _GuardAuditCheck(TypedDict):
    schema_version: Literal[1]
    audit_id: str
    operation_id: str
    operation_epoch: int
    role: PromptGuardRole
    adapter: str
    mode: PromptGuardMode
    policy_fingerprint: str
    request_hash: str
    disposition: PromptGuardDisposition
    reason: PromptGuardReason
    started_at: str
    duration_ms: int


class _GuardAuditEnvelope(TypedDict):
    _kind: Literal["prompt_guard_audit"]
    check: _GuardAuditCheck


@contextmanager
def prompt_guard_user_scope(user_prompt: str) -> Iterator[None]:
    """Keep explicit genuine user provenance task-local, without granting authority."""
    if type(user_prompt) is not str:
        raise TypeError("Prompt guard user provenance requires an exact string")
    token = _USER_PROMPT.set(user_prompt)
    try:
        yield
    finally:
        _USER_PROMPT.reset(token)


@dataclass(frozen=True, slots=True)
class _GuardRefusalEvidence:
    partial_state: CompositionState | None = None
    tool_invocations: tuple[ComposerToolInvocation, ...] = ()
    llm_calls: tuple[ComposerLLMCall, ...] = ()
    failed_turn: FailedTurnMetadata | None = None


class PromptGuardRefused(Exception):
    """Terminal authoring refusal, distinct from provider failures and retries."""

    def __init__(self, *, decision: PromptGuardDecision, audit_id: str, _evidence: _GuardRefusalEvidence | None = None) -> None:
        if decision.disposition not in ("blocked", "unavailable"):
            raise AuditIntegrityError("Only a terminal guard decision can refuse authoring")
        self._decision = decision
        self._audit_id = audit_id
        self._evidence = _GuardRefusalEvidence() if _evidence is None else _evidence
        super().__init__("Prompt guard refused protected authoring dispatch")

    @property
    def disposition(self) -> str:
        return self._decision.disposition

    @property
    def audit_id(self) -> str:
        return self._audit_id

    @property
    def partial_state(self) -> CompositionState | None:
        return self._evidence.partial_state

    @property
    def tool_invocations(self) -> tuple[ComposerToolInvocation, ...]:
        return self._evidence.tool_invocations

    @property
    def llm_calls(self) -> tuple[ComposerLLMCall, ...]:
        return self._evidence.llm_calls

    @property
    def failed_turn(self) -> FailedTurnMetadata | None:
        return self._evidence.failed_turn

    def with_evidence(
        self,
        *,
        state: CompositionState,
        initial_version: int,
        tool_invocations: tuple[ComposerToolInvocation, ...] = (),
        llm_calls: tuple[ComposerLLMCall, ...] = (),
        failed_turn: FailedTurnMetadata | None = None,
    ) -> PromptGuardRefused:
        """Carry only undrained work; the caller supplies its persisted baseline."""
        if type(initial_version) is not int or initial_version < 0:
            raise AuditIntegrityError("Guard refusal evidence requires an exact persisted version")
        if type(tool_invocations) is not tuple or type(llm_calls) is not tuple:
            raise AuditIntegrityError("Guard refusal evidence requires immutable audit sequences")
        return PromptGuardRefused(
            decision=self._decision,
            audit_id=self.audit_id,
            _evidence=_GuardRefusalEvidence(state if state.version > initial_version else None, tool_invocations, llm_calls, failed_turn),
        )

    def to_payload(self) -> dict[str, str]:
        blocked = self.disposition == "blocked"
        return {
            "error_type": "prompt_guard_blocked" if blocked else "prompt_guard_unavailable",
            "detail": (
                "The configured prompt guard blocked this authoring request. Review the input before resubmitting."
                if blocked
                else "The configured prompt guard could not check this authoring request. Contact the deployment operator."
            ),
            "audit_id": self.audit_id,
        }


class PromptGuardMount:
    """One independently configured dedicated-service mount."""

    def __init__(self, client: PromptGuardClient) -> None:
        if not isinstance(client, PromptGuardClient):
            raise TypeError("Prompt guard requires a nominal dedicated-service client")
        if not is_lower_sha256_hex(client.policy_fingerprint):
            raise AuditIntegrityError("Prompt guard policy lacks a stable fingerprint")
        if type(client.timeout_seconds) not in (int, float) or not math.isfinite(client.timeout_seconds) or client.timeout_seconds <= 0:
            raise AuditIntegrityError("Prompt guard requires a finite positive invocation timeout")
        self.client = client


def build_prompt_guard_mount(settings: PromptGuardSettings | None) -> PromptGuardMount | None:
    """Resolve configuration without granting an absent mount permission to send."""
    if settings is None:
        return None
    from elspeth.web.composer.prompt_guard_clients import build_prompt_guard_client

    return PromptGuardMount(build_prompt_guard_client(settings))


def _request(
    *,
    policy_fingerprint: str,
    role: PromptGuardRole,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    user_prompt: str | None,
    trusted_message_indexes: frozenset[int],
) -> tuple[PromptGuardRequest, bool]:
    """Bind exact supported wire input and preserve user/document provenance.

    Trust indexes come from nominal, in-process producer markers, never wire
    role strings or model JSON. Every other message field, including tool
    arguments, remains in its complete evidence document.
    """
    genuine_user = _USER_PROMPT.get() if user_prompt is None else user_prompt
    supported = genuine_user is not None
    user = genuine_user if genuine_user is not None else ""
    require_no_credential_material(user, surface="composer_prompt_guard_request")
    exact = {
        "role": role,
        "policy_fingerprint": policy_fingerprint,
        "user_prompt": genuine_user,
        "messages": list(messages),
        "tools": list(tools),
        "trusted_message_indexes": sorted(trusted_message_indexes),
    }
    canonical = json.dumps(exact, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    documents: list[str] = []
    # Exclude only the exact latest genuine user message from documents. Its
    # text is already checked as user input; earlier equal history is evidence.
    genuine_index: int | None = None
    for index, message in enumerate(messages):
        if message["role"] == "user" and "content" in message and message["content"] == user and genuine_user is not None:
            genuine_index = index
    for index, message in enumerate(messages):
        if index in trusted_message_indexes:
            continue
        if index == genuine_index:
            # Other fields are not discarded merely because text matches.
            extras = {key: value for key, value in message.items() if key not in ("role", "content")}
            if extras:
                documents.append(json.dumps(extras, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
            continue
        content = message["content"] if "content" in message else None
        if content is not None and type(content) is not str:
            if not isinstance(content, (list, tuple)):
                supported = False
            else:
                for block in content:
                    if (
                        not isinstance(block, Mapping)
                        or "type" not in block
                        or block["type"] != "text"
                        or "text" not in block
                        or type(block["text"]) is not str
                    ):
                        supported = False
        documents.append(json.dumps(dict(message), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return PromptGuardRequest(role, user, tuple(documents), request_hash), supported and bool(user or documents)


async def _persist(
    *,
    custody: ProviderCallCustody,
    envelope: _GuardAuditEnvelope,
) -> None:
    """Use the existing fenced required SQL path, including its actual outcome."""
    sql, projection = custody.required_work.reserve_pair(
        RequiredWorkSource.DISPATCH_AUDIT_SQL, RequiredWorkSource.DISPATCH_AUDIT_PROJECTION
    )
    try:
        await custody.service.add_messages_atomic(
            UUID(custody.context.fence.session_id),
            (AuditMessageDraft(role="audit", content=json.dumps(envelope, sort_keys=True), tool_calls=(envelope,)),),
            writer_principal="compose_loop",
            session_operation_context=custody.context,
            audit_only=True,
            required_work=sql,
        )
    except BaseException:
        projection.complete_owned()
        raise
    projection.complete_owned()


async def _join_audit(operation: Coroutine[Any, Any, None]) -> None:
    """Keep audit custody until the real write settles, including repeated Stop."""
    task = asyncio.create_task(operation)
    cancellations: list[asyncio.CancelledError] = []
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            cancellations.append(cancellation)
        except BaseException:
            break
    try:
        task.result()
    except BaseException as failure:
        if cancellations:
            raise BaseExceptionGroup("Prompt guard audit and original cancellation outcomes", [failure, *cancellations]) from None
        raise
    if cancellations:
        raise cancellations[0]


async def enforce_prompt_guard(
    *,
    mount: PromptGuardMount | None,
    mode: PromptGuardMode,
    role: PromptGuardRole,
    provider_custody: ProviderCallCustody | None,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] = (),
    user_prompt: str | None = None,
    trusted_message_indexes: frozenset[int] = frozenset(),
) -> None:
    """Audit a complete check before any protected model admission or dispatch."""
    if mode not in ("required", "debug-when-unconfigured"):
        raise AuditIntegrityError("Prompt guard mode is outside its closed vocabulary")
    if (mount is not None and type(mount) is not PromptGuardMount) or type(provider_custody) is not ProviderCallCustody:
        raise AuditIntegrityError("Configured prompt guard requires exact mount and fenced invocation custody")
    if type(trusted_message_indexes) is not frozenset or any(
        type(index) is not int or not 0 <= index < len(messages) for index in trusted_message_indexes
    ):
        raise AuditIntegrityError("Prompt guard trusted provenance lacks exact message positions")
    provider_custody.required_work.validate_context(provider_custody.context)
    started = datetime.now(UTC)
    started_ns = time.monotonic_ns()
    audit_id = str(uuid4())
    policy_fingerprint = (
        mount.client.policy_fingerprint
        if mount is not None
        else hashlib.sha256(json.dumps({"mount": None, "mode": mode}, sort_keys=True).encode("utf-8")).hexdigest()
    )
    request, supported = _request(
        policy_fingerprint=policy_fingerprint,
        role=role,
        messages=messages,
        tools=tools,
        user_prompt=user_prompt,
        trusted_message_indexes=trusted_message_indexes,
    )
    original_cancellation: asyncio.CancelledError | None = None
    if mount is None:
        decision = PromptGuardDecision("debug_bypass" if mode == "debug-when-unconfigured" else "unavailable", "guard_not_configured")
    elif not supported:
        decision = PromptGuardDecision("unavailable", "unsupported_input")
    else:
        try:
            async with asyncio.timeout(mount.client.timeout_seconds):
                decision = await mount.client.inspect(request)
        except TimeoutError:
            decision = PromptGuardDecision("unavailable", "timeout")
        except asyncio.CancelledError as cancellation:
            original_cancellation = cancellation
            decision = PromptGuardDecision("cancelled", "cancelled")
        if type(decision) is not PromptGuardDecision:
            raise AuditIntegrityError("Dedicated prompt guard returned a foreign decision")
        if original_cancellation is None and decision.disposition not in ("allow", "blocked", "unavailable"):
            raise AuditIntegrityError("Dedicated prompt guard authored an enforcer-only outcome")
    fence = provider_custody.context.fence
    envelope: _GuardAuditEnvelope = {
        "_kind": "prompt_guard_audit",
        "check": {
            "schema_version": 1,
            "audit_id": audit_id,
            "operation_id": fence.operation_id,
            "operation_epoch": fence.operation_epoch,
            "role": role,
            "adapter": mount.client.adapter if mount is not None else "unconfigured",
            "mode": mode,
            "policy_fingerprint": policy_fingerprint,
            "request_hash": request.request_hash,
            "disposition": decision.disposition,
            "reason": decision.reason,
            "started_at": started.isoformat(),
            "duration_ms": max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
        },
    }
    try:
        await _join_audit(_persist(custody=provider_custody, envelope=envelope))
    except BaseException as failure:
        if original_cancellation is not None and failure is not original_cancellation:
            raise BaseExceptionGroup("Prompt guard cancellation and required audit outcomes", [original_cancellation, failure]) from None
        raise
    if original_cancellation is not None:
        raise original_cancellation
    if decision.disposition == "allow" and mount is not None:
        return
    if decision.disposition == "debug_bypass" and mount is None and mode == "debug-when-unconfigured":
        return
    if decision.disposition not in ("allow", "debug_bypass"):
        if decision.disposition == "cancelled":
            raise AuditIntegrityError("Dedicated service cannot author caller cancellation")
        raise PromptGuardRefused(decision=decision, audit_id=audit_id)
    raise AuditIntegrityError("Prompt guard admission disagrees with configured policy")

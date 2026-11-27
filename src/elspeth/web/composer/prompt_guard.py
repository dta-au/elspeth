"""Dedicated prompt-guard admission and durable authoring audit custody."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from uuid import UUID, uuid4

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_MAX_TEXT_CHARACTERS,
    PromptGuardCallPlan,
    PromptGuardCallResult,
    PromptGuardClient,
    PromptGuardContentPart,
    PromptGuardDecision,
    PromptGuardDisposition,
    PromptGuardMode,
    PromptGuardReason,
    PromptGuardRequest,
    PromptGuardRequestUnavailable,
    PromptGuardRole,
    PromptGuardThinkingParts,
    PromptGuardToolArgumentParts,
    PromptGuardUnsupportedInput,
    validate_prompt_guard_content_parts,
    validate_prompt_guard_thinking_parts,
    validate_prompt_guard_tool_argument_parts,
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
    schema_version: Literal[2]
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
    units_scanned: int
    units_reused: int


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
    def http_status(self) -> Literal[422, 503]:
        """Project local input refusal separately from remote unavailability."""
        return 422 if self.disposition == "blocked" or self._decision.reason == "unsupported_input" else 503

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
        if self._decision.reason == "unsupported_input":
            return {
                "error_type": "prompt_guard_unsupported_input",
                "detail": (
                    "The authoring input is outside the supported prompt guard input envelope. The protected dispatch was not sent."
                ),
                "audit_id": self.audit_id,
            }
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


# (session_id, policy_fingerprint, mode, role, slot, sha256 of the inspected text)
type _AllowedUnitKey = tuple[str, str, PromptGuardMode, PromptGuardRole, Literal["user_prompt", "document"], bytes]

# Measured: a 50-turn edit session left 2,149 distinct keys on the busier
# mount (advisor) and 753 on the other; an entry holds about 270-350 bytes.
# 20,000 keys per mount keep about nine such sessions in a few megabytes.
# Eviction only causes a rescan.
PROMPT_GUARD_REUSE_CAPACITY = 20_000


class _AllowedUnitCache:
    """Bounded, process-local record of units the dedicated service allowed.

    Only digests are held. A key is compared by exact tuple equality, so a
    unit allowed in one session or under one policy can never satisfy a
    lookup for another. Eviction or a restart only causes a unit to be
    scanned again.
    """

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity < 0:
            raise AuditIntegrityError("Prompt guard reuse capacity must be a non-negative exact integer")
        self._capacity = capacity
        self._entries: OrderedDict[_AllowedUnitKey, None] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def allowed(self, key: _AllowedUnitKey) -> bool:
        with self._lock:
            if key not in self._entries:
                return False
            self._entries.move_to_end(key)
            return True

    def admit(self, keys: Sequence[_AllowedUnitKey]) -> None:
        with self._lock:
            for key in keys:
                self._entries[key] = None
                self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)


class PromptGuardMount:
    """One independently configured dedicated-service mount."""

    def __init__(self, client: PromptGuardClient, *, reuse_capacity: int = PROMPT_GUARD_REUSE_CAPACITY) -> None:
        if not isinstance(client, PromptGuardClient):
            raise TypeError("Prompt guard requires a nominal dedicated-service client")
        if not is_lower_sha256_hex(client.policy_fingerprint):
            raise AuditIntegrityError("Prompt guard policy lacks a stable fingerprint")
        if type(client.timeout_seconds) not in (int, float) or not math.isfinite(client.timeout_seconds) or client.timeout_seconds <= 0:
            raise AuditIntegrityError("Prompt guard requires a finite positive invocation timeout")
        self.client = client
        self._allowed_units = _AllowedUnitCache(reuse_capacity)

    @property
    def reused_unit_count(self) -> int:
        """Number of allowed unit keys currently held (diagnostic sizing only)."""
        return len(self._allowed_units)


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
    content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
    tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    thinking_parts: tuple[PromptGuardThinkingParts | None, ...] | None = None,
    enforce_azure_character_limits: bool = True,
    units_out: list[tuple[str, str]] | None = None,
) -> tuple[PromptGuardRequest, bool]:
    """Bind exact supported wire input and preserve user/document provenance.

    Trust indexes come from nominal, in-process producer markers, never wire
    role strings or model JSON. Every other message field, including tool
    arguments, remains in its complete evidence document.

    ``units_out`` receives every unpacked document unit in order as
    ``(sent_text, inspected_text)``: the positional label is part of the sent
    text only, so reuse is keyed on what the detector actually reads.
    """
    if type(enforce_azure_character_limits) is not bool:
        raise AuditIntegrityError("Prompt guard size policy requires an exact enforcement decision")
    genuine_user = _USER_PROMPT.get() if user_prompt is None else user_prompt
    supported = genuine_user is not None
    user = genuine_user if genuine_user is not None else ""
    require_no_credential_material(user, surface="composer_prompt_guard_request")
    partitions = tuple(None for _ in messages) if content_parts is None else content_parts
    validate_prompt_guard_content_parts(messages, partitions)
    argument_partitions = tuple(None for _ in messages) if tool_argument_parts is None else tool_argument_parts
    validate_prompt_guard_tool_argument_parts(messages, argument_partitions)
    thinking_partitions = tuple(None for _ in messages) if thinking_parts is None else thinking_parts
    validate_prompt_guard_thinking_parts(messages, thinking_partitions)
    if any(
        partitions[index] is not None or argument_partitions[index] is not None or thinking_partitions[index] is not None
        for index in trusted_message_indexes
    ):
        raise AuditIntegrityError("Prompt guard partition conflicts with whole-message trust")
    exact = {
        "role": role,
        "policy_fingerprint": policy_fingerprint,
        "enforce_azure_character_limits": enforce_azure_character_limits,
        "user_prompt": genuine_user,
        "messages": list(messages),
        "tools": list(tools),
        "trusted_message_indexes": sorted(trusted_message_indexes),
        "content_parts": [
            None
            if parts is None
            else [
                {"text": part.text, "trust": part.trust.value if part.trust is not None else None, "inspection_text": part.inspection_text}
                for part in parts
            ]
            for parts in partitions
        ],
        "tool_argument_parts": [
            None
            if entries is None
            else [
                {
                    "tool_call_index": entry.tool_call_index,
                    "parts": [{"text": part.text, "inspection_text": part.inspection_text} for part in entry.parts],
                }
                for entry in entries
            ]
            for entries in argument_partitions
        ],
    }
    if any(entry is not None for entry in thinking_partitions):
        exact["thinking_parts"] = [
            None if entry is None else [{"text": part.text, "inspection_text": part.inspection_text} for part in entry.parts]
            for entry in thinking_partitions
        ]
    canonical = json.dumps(exact, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    documents: list[str] = []
    inspected: list[str] = []
    # Exclude only the exact latest genuine user message from documents. Its
    # text is already checked as user input; earlier equal history is evidence.
    genuine_index: int | None = None
    for index, message in enumerate(messages):
        if message["role"] == "user" and "content" in message and message["content"] == user and genuine_user is not None:
            genuine_index = index
    for index, message in enumerate(messages):
        if index in trusted_message_indexes:
            continue
        parts = partitions[index]
        argument_entries = argument_partitions[index]
        thinking_entry = thinking_partitions[index]
        shadow = dict(message)
        if index == genuine_index:
            shadow = {key: value for key, value in shadow.items() if key not in ("role", "content")}
        elif parts is not None:
            shadow.pop("content")
        if thinking_entry is not None:
            shadow.pop("thinking_blocks")
        if argument_entries:
            shadow = deepcopy(shadow)
            for entry in argument_entries:
                # Only this exact, fully covered string moves to ordered units.
                # Every sibling function/tool-call field remains untrusted.
                shadow["tool_calls"][entry.tool_call_index]["function"]["arguments"] = ""
        if shadow:
            documents.append(json.dumps(shadow, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
            inspected.append(documents[-1])
        if parts is not None and index != genuine_index:
            for part_index, part in enumerate(parts):
                if part.trust is not None:
                    continue
                text = part.text if part.inspection_text is None else part.inspection_text
                require_no_credential_material(text, surface="composer_prompt_guard_request")
                documents.append(f"Message {index}, content part {part_index}:\n{text}")
                inspected.append(text)
        if argument_entries:
            for entry in argument_entries:
                for part_index, part in enumerate(entry.parts):
                    text = part.text if part.inspection_text is None else part.inspection_text
                    require_no_credential_material(text, surface="composer_prompt_guard_request")
                    documents.append(f"Message {index}, tool call {entry.tool_call_index}, argument part {part_index}:\n{text}")
                    inspected.append(text)
        if thinking_entry is not None:
            for part_index, part in enumerate(thinking_entry.parts):
                text = part.text if part.inspection_text is None else part.inspection_text
                documents.append(f"Message {index}, provider thinking part {part_index}:\n{text}")
                inspected.append(text)
        if parts is not None or index == genuine_index:
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
    if units_out is not None:
        units_out.extend(zip(documents, inspected, strict=True))
    packed = _pack_documents(documents)
    try:
        user.encode("utf-8")
        for document in packed:
            document.encode("utf-8")
    except UnicodeEncodeError:
        supported = False
    supported = (
        supported
        and (
            not enforce_azure_character_limits
            or (
                len(user) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS
                and all(len(document) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS for document in packed)
            )
        )
        and bool(user or any(packed))
    )
    return PromptGuardRequest(role, user, packed, request_hash), supported


def _pack_documents(units: Sequence[str]) -> tuple[str, ...]:
    """Coalesce complete units in order; an oversized unit is never sliced."""
    packed: list[str] = []
    current: str | None = None
    for unit in units:
        if current is None:
            current = unit
        elif len(current) + 1 + len(unit) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS:
            current += "\n" + unit
        else:
            packed.append(current)
            current = unit
    if current is not None:
        packed.append(current)
    return tuple(packed)


@dataclass(frozen=True, slots=True)
class _InspectionPlan:
    """The part of one logical check not already allowed in this session."""

    user_prompt: str | None
    documents: tuple[str, ...]
    new_keys: tuple[_AllowedUnitKey, ...]
    reused: int


def _inspection_plan(
    *,
    cache: _AllowedUnitCache,
    session_id: str,
    policy_fingerprint: str,
    mode: PromptGuardMode,
    role: PromptGuardRole,
    user_prompt: str,
    units: Sequence[tuple[str, str]],
) -> _InspectionPlan:
    """Select new units; identical units within one check are inspected once."""
    seen: set[_AllowedUnitKey] = set()
    new_keys: list[_AllowedUnitKey] = []
    new_units: list[str] = []
    reused = 0
    send_user: str | None = None
    if user_prompt:
        key: _AllowedUnitKey = (
            session_id,
            policy_fingerprint,
            mode,
            role,
            "user_prompt",
            hashlib.sha256(user_prompt.encode("utf-8")).digest(),
        )
        seen.add(key)
        if cache.allowed(key):
            reused += 1
        else:
            new_keys.append(key)
            send_user = user_prompt
    for sent, inspected in units:
        key = (session_id, policy_fingerprint, mode, role, "document", hashlib.sha256(inspected.encode("utf-8")).digest())
        if key in seen:
            continue
        seen.add(key)
        if cache.allowed(key):
            reused += 1
        else:
            new_keys.append(key)
            new_units.append(sent)
    return _InspectionPlan(send_user, _pack_documents(new_units), tuple(new_keys), reused)


async def inspect_prompt_guard_request(client: PromptGuardClient, request: PromptGuardRequest) -> PromptGuardDecision:
    """Send one planned request only when its calls cover every input."""
    call_plan = client.plan_calls(request)
    if type(call_plan) is not PromptGuardCallPlan:
        raise AuditIntegrityError("Prompt guard adapter returned a foreign call plan")
    calls = call_plan.calls
    if type(calls) is PromptGuardUnsupportedInput:
        return PromptGuardDecision("unavailable", "unsupported_input")
    if type(calls) is not tuple:
        raise AuditIntegrityError("Prompt guard adapter returned foreign physical calls")
    covered = {index for call in calls for index in call.document_indexes}
    if covered != set(range(len(request.documents))) or (bool(request.user_prompt) and not any(call.carries_user_prompt for call in calls)):
        return PromptGuardDecision("unavailable", "unsupported_input")
    try:
        async with client.request_context():
            for call in calls:
                result = await client.inspect_call(call)
                if type(result) is not PromptGuardCallResult:
                    raise AuditIntegrityError("Prompt guard adapter returned a foreign call result")
                if result.decision.disposition != "allow":
                    return result.decision
    except PromptGuardRequestUnavailable as failure:
        return failure.decision
    return PromptGuardDecision("allow", "clean")


async def _inspect_plan(
    client: PromptGuardClient, request: PromptGuardRequest, plan: _InspectionPlan, submitted: list[int]
) -> PromptGuardDecision:
    """Inspect the user prompt and the documents in separate calls.

    Documents are always judged without a user prompt, so a document's allow
    never depended on whichever prompt accompanied it and can be reused. The
    documents are submitted even when the prompt is refused; the first
    non-allow decision, in that order, is returned. ``submitted`` receives
    each call's unit count as the call starts, so a timeout or cancellation
    during the first call does not count the documents.
    """
    decisions: list[PromptGuardDecision] = []
    prompt_units = 0
    if plan.user_prompt is not None:
        prompt_units = 1
        submitted.append(prompt_units)
        decisions.append(
            await inspect_prompt_guard_request(client, PromptGuardRequest(request.role, plan.user_prompt, (), request.request_hash))
        )
    if plan.documents:
        submitted.append(len(plan.new_keys) - prompt_units)
        decisions.append(
            await inspect_prompt_guard_request(client, PromptGuardRequest(request.role, "", plan.documents, request.request_hash))
        )
    for decision in decisions:
        if type(decision) is not PromptGuardDecision or decision.disposition != "allow":
            return decision
    return PromptGuardDecision("allow", "clean")


def _audit_check(
    *,
    audit_id: str,
    custody: ProviderCallCustody,
    role: PromptGuardRole,
    adapter: str,
    mode: PromptGuardMode,
    policy_fingerprint: str,
    request_hash: str,
    decision: PromptGuardDecision,
    started: datetime,
    started_ns: int,
    units_scanned: int,
    units_reused: int,
) -> _GuardAuditCheck:
    """Build the one sanitized check record; reuse counts state what was sent.

    ``units_scanned`` counts distinct units in the service calls this check
    started (a refusal or timeout may end the client's own batching of the
    documents early);
    ``units_reused`` counts distinct units admitted because the same service
    policy allowed the exact same inspected text earlier in this session. A
    check with zero units scanned made no service call.
    """
    fence = custody.context.fence
    return {
        "schema_version": 2,
        "audit_id": audit_id,
        "operation_id": fence.operation_id,
        "operation_epoch": fence.operation_epoch,
        "role": role,
        "adapter": adapter,
        "mode": mode,
        "policy_fingerprint": policy_fingerprint,
        "request_hash": request_hash,
        "disposition": decision.disposition,
        "reason": decision.reason,
        "started_at": started.isoformat(),
        "duration_ms": max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
        "units_scanned": units_scanned,
        "units_reused": units_reused,
    }


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


def prompt_guard_enforces_character_limits(*, mount: PromptGuardMount | None, mode: PromptGuardMode) -> bool:
    """Whether a check with this mount and mode applies the service's per-text character limits.

    They apply whenever inspection is required: a guard is mounted, or the
    mode is ``required``. Only explicit unconfigured debug mode does not
    enforce them. :func:`enforce_prompt_guard` decides with this function, and
    so do producers that must know before rendering what the check will refuse.
    """
    return not (mount is None and mode == "debug-when-unconfigured")


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
    content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
    tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    thinking_parts: tuple[PromptGuardThinkingParts | None, ...] | None = None,
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
    # The service's character limits apply whenever inspection is required.
    # Explicit unconfigured debug mode still builds and validates the complete
    # request; its audit records skipped inspection rather than a clean result.
    enforce_azure_character_limits = prompt_guard_enforces_character_limits(mount=mount, mode=mode)
    policy_fingerprint = (
        mount.client.policy_fingerprint
        if mount is not None
        else hashlib.sha256(
            json.dumps(
                {
                    "mount": None,
                    "mode": mode,
                    "enforce_azure_character_limits": enforce_azure_character_limits,
                    "max_prompt_characters": PROMPT_GUARD_MAX_TEXT_CHARACTERS,
                    "max_document_characters": PROMPT_GUARD_MAX_TEXT_CHARACTERS,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    units: list[tuple[str, str]] = []
    request, supported = _request(
        policy_fingerprint=policy_fingerprint,
        role=role,
        messages=messages,
        tools=tools,
        user_prompt=user_prompt,
        trusted_message_indexes=trusted_message_indexes,
        content_parts=content_parts,
        tool_argument_parts=tool_argument_parts,
        thinking_parts=thinking_parts,
        enforce_azure_character_limits=enforce_azure_character_limits,
        units_out=units,
    )
    original_cancellation: asyncio.CancelledError | None = None
    plan: _InspectionPlan | None = None
    submitted: list[int] = []
    if not supported:
        decision = PromptGuardDecision("unavailable", "unsupported_input")
    elif mount is None:
        decision = PromptGuardDecision("debug_bypass" if mode == "debug-when-unconfigured" else "unavailable", "guard_not_configured")
    else:
        # Only text not already allowed under this exact session, policy,
        # mode, role and slot is sent; every local check above ran in full.
        plan = _inspection_plan(
            cache=mount._allowed_units,
            session_id=provider_custody.context.fence.session_id,
            policy_fingerprint=policy_fingerprint,
            mode=mode,
            role=role,
            user_prompt=request.user_prompt,
            units=units,
        )
        try:
            # One deadline covers the whole check: the prompt call and the
            # documents call together, not each call separately.
            async with asyncio.timeout(mount.client.timeout_seconds):
                decision = await _inspect_plan(mount.client, request, plan, submitted)
        except TimeoutError:
            decision = PromptGuardDecision("unavailable", "timeout")
        except asyncio.CancelledError as cancellation:
            original_cancellation = cancellation
            decision = PromptGuardDecision("cancelled", "cancelled")
        if type(decision) is not PromptGuardDecision:
            raise AuditIntegrityError("Dedicated prompt guard returned a foreign decision")
        if original_cancellation is None and (
            decision.disposition not in ("allow", "blocked", "unavailable") or decision.reason == "guard_not_configured"
        ):
            raise AuditIntegrityError("Dedicated prompt guard authored an enforcer-only outcome")
    envelope: _GuardAuditEnvelope = {
        "_kind": "prompt_guard_audit",
        "check": _audit_check(
            audit_id=audit_id,
            custody=provider_custody,
            role=role,
            adapter=mount.client.adapter if mount is not None else "unconfigured",
            mode=mode,
            policy_fingerprint=policy_fingerprint,
            request_hash=request.request_hash,
            decision=decision,
            started=started,
            started_ns=started_ns,
            units_scanned=sum(submitted),
            units_reused=plan.reused if plan is not None else 0,
        ),
    }
    try:
        await _join_audit(_persist(custody=provider_custody, envelope=envelope))
    except BaseException as failure:
        if original_cancellation is not None and failure is not original_cancellation:
            raise BaseExceptionGroup("Prompt guard cancellation and required audit outcomes", [original_cancellation, failure]) from None
        raise
    if original_cancellation is not None:
        raise original_cancellation
    if decision.disposition == "allow" and mount is not None and plan is not None:
        # Only a complete service allow whose audit record has settled may
        # be reused; refusals, unavailability and partial batches never are.
        mount._allowed_units.admit(plan.new_keys)
        return
    if decision.disposition == "debug_bypass" and mount is None and mode == "debug-when-unconfigured":
        return
    if decision.disposition not in ("allow", "debug_bypass"):
        if decision.disposition == "cancelled":
            raise AuditIntegrityError("Dedicated service cannot author caller cancellation")
        raise PromptGuardRefused(decision=decision, audit_id=audit_id)
    raise AuditIntegrityError("Prompt guard admission disagrees with configured policy")

"""Original selected V4 SDK-result admission, separate from lossy diagnostics.

These carriers prove a local capture, never SDK, SQL or retry authority. The
caller must bind the actual physical attempt and retain/dispose the result.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast
from weakref import WeakKeyDictionary, WeakValueDictionary

from pydantic import JsonValue

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.errors import AuditIntegrityError

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import (
        _AdmittedAssistantMessage,
        _AdmittedLLMCompletion,
        _AdmittedLLMProviderMetadata,
        _AdmittedToolCall,
    )

type V4ProviderResultField = Literal[
    "content",
    "tool_calls",
    "model_returned",
    "provider_request_id",
    "finish_reason",
    "reasoning_content",
    "reasoning_details",
    "thinking_blocks",
]
type V4ProviderResultLossReason = Literal[
    "missing",
    "invalid_type",
    "invalid_utf8",
    "nonfinite",
    "recursive",
    "unsupported",
    "depth_limit",
    "item_limit",
    "text_budget",
    "scalar_utf8_limit",
    "invalid_native",
    "invalid_tool_calls",
]


@dataclass(frozen=True, slots=True)
class V4ProviderSdkReturn:
    """DATA minted/owned by custody at the actual successful SDK await."""

    attempt_id: str
    response_identity: int


@dataclass(frozen=True, slots=True)
class V4ProviderResultLoss:
    field: V4ProviderResultField
    reason: V4ProviderResultLossReason
    # Only a measured, individually valid UTF-8 scalar has a numeric extent.
    # This is NOT a fabricated whole-document overflow observation.
    scalar_utf8_bytes: int | None = None


@dataclass(frozen=True, slots=True, eq=False)
class V4ProviderResultCompleteness:
    """Source-issued proof bound to one exact live owned metadata instance."""

    sdk_return: V4ProviderSdkReturn
    diagnostic_metadata: _AdmittedLLMProviderMetadata = field(repr=False)
    content: str | None = field(repr=False)
    tool_calls: tuple[_AdmittedToolCall, ...] = field(repr=False)

    def require_metadata(self, metadata: _AdmittedLLMProviderMetadata) -> None:
        from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata

        if (
            type(metadata) is not _AdmittedLLMProviderMetadata
            or metadata.v4_completeness is not self
            or _ISSUED_METADATA.get(id(self)) is not metadata
        ):
            raise AuditIntegrityError("V4 result lost its original issued metadata capture")

    def require_result(
        self, metadata: _AdmittedLLMProviderMetadata, *, content: str | None, tool_calls: tuple[_AdmittedToolCall, ...]
    ) -> None:
        from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction

        self.require_metadata(metadata)
        if (content is not None and type(content) is not str) or content != self.content or type(tool_calls) is not tuple:
            raise AuditIntegrityError("V4 result changed its original selected content")
        if len(tool_calls) != len(self.tool_calls) or any(type(call) is not _AdmittedToolCall for call in tool_calls):
            raise AuditIntegrityError("V4 result changed its original selected tool batch")
        for actual, original in zip(tool_calls, self.tool_calls, strict=True):
            if (
                type(actual.function) is not _AdmittedToolFunction
                or type(actual.id) is not str
                or type(actual.function.name) is not str
                or type(actual.function.arguments) is not str
                or (actual.id, actual.function.name, actual.function.arguments)
                != (original.id, original.function.name, original.function.arguments)
            ):
                raise AuditIntegrityError("V4 result changed its original selected tool fields")


# A weak identity join rejects fabricated/equal-copied witnesses or metadata.
# It retains neither SDK objects nor a history of completed provider calls.
_ISSUED_METADATA: WeakValueDictionary[int, _AdmittedLLMProviderMetadata] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class V4ProviderResultAdmission:
    message: _AdmittedAssistantMessage
    tool_calls: tuple[_AdmittedToolCall, ...]
    metadata: _AdmittedLLMProviderMetadata
    sdk_return: V4ProviderSdkReturn


class V4ProviderResultIncomplete(Exception):
    """One original returned paid result has unavailable mandatory evidence."""

    __slots__ = ("__weakref__",)

    def __init__(
        self, *, sdk_return: V4ProviderSdkReturn, metadata: _AdmittedLLMProviderMetadata, losses: tuple[V4ProviderResultLoss, ...]
    ) -> None:
        if not losses or any(type(loss) is not V4ProviderResultLoss for loss in losses):
            raise TypeError("V4 incomplete result requires original closed loss observations")
        super().__init__("Original V4 provider result has incomplete mandatory evidence")
        self.sdk_return = sdk_return
        self.metadata = metadata
        self.losses = losses


@dataclass(frozen=True, slots=True)
class _IssuedIncomplete:
    sdk_return: V4ProviderSdkReturn
    sdk_facts: tuple[str, int]
    metadata: _AdmittedLLMProviderMetadata = field(repr=False)
    metadata_atoms: tuple[object, ...] = field(repr=False)
    reasoning_details: object = field(repr=False)
    thinking_blocks: object = field(repr=False)
    losses: tuple[V4ProviderResultLoss, ...]
    loss_facts: tuple[tuple[str, str, int | None], ...]


# The exception itself is the weak key: neither copied state nor a reused
# numeric object ID can borrow another parser issuance. Values retain only
# its bounded immutable DATA, never the SDK response or exception/traceback.
_ISSUED_INCOMPLETE: WeakKeyDictionary[V4ProviderResultIncomplete, _IssuedIncomplete] = WeakKeyDictionary()


def _incomplete_metadata_atoms(metadata: _AdmittedLLMProviderMetadata) -> tuple[object, ...]:
    from elspeth.contracts.token_usage import TokenUsage
    from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata

    if type(metadata) is not _AdmittedLLMProviderMetadata or metadata.v4_completeness is not None or type(metadata.usage) is not TokenUsage:
        raise AuditIntegrityError("V4 incomplete result changed its original diagnostic metadata")
    text = (
        metadata.model_returned,
        metadata.provider_request_id,
        metadata.finish_reason,
        metadata.reasoning_content,
        metadata.provider_served,
    )
    usage = metadata.usage
    counts = (
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.reported_total,
        usage.cached_prompt_tokens,
        usage.cache_creation_input_tokens,
        usage.cache_read_input_tokens,
        usage.reasoning_tokens,
    )
    cost = metadata.provider_cost
    cost_source = cast(object, metadata.provider_cost_source)
    if (
        any(value is not None and type(value) is not str for value in text)
        or any(value is not None and (type(value) is not int or value < 0) for value in counts)
        or (cost is not None and type(cost) not in (int, float))
        or (type(cost) is float and not math.isfinite(cost))
        or type(cost_source) is not str
    ):
        raise AuditIntegrityError("V4 incomplete result changed its original accounting fields")
    return (*text, type(cost), cost, cost_source, *counts)


def _incomplete_loss_facts(losses: tuple[V4ProviderResultLoss, ...]) -> tuple[tuple[str, str, int | None], ...]:
    if type(losses) is not tuple or not losses:
        raise AuditIntegrityError("V4 incomplete result changed its original loss observations")
    facts = []
    for loss in losses:
        field_value = cast(object, loss.field)
        reason = cast(object, loss.reason)
        if (
            type(loss) is not V4ProviderResultLoss
            or type(field_value) is not str
            or type(reason) is not str
            or (loss.scalar_utf8_bytes is not None and type(loss.scalar_utf8_bytes) is not int)
        ):
            raise AuditIntegrityError("V4 incomplete result changed its original loss fields")
        facts.append((field_value, reason, loss.scalar_utf8_bytes))
    return tuple(facts)


def _issue_v4_incomplete(
    *, sdk_return: V4ProviderSdkReturn, metadata: _AdmittedLLMProviderMetadata, losses: tuple[V4ProviderResultLoss, ...]
) -> V4ProviderResultIncomplete:
    incomplete = V4ProviderResultIncomplete(sdk_return=sdk_return, metadata=metadata, losses=losses)
    _ISSUED_INCOMPLETE[incomplete] = _IssuedIncomplete(
        sdk_return,
        (sdk_return.attempt_id, sdk_return.response_identity),
        metadata,
        _incomplete_metadata_atoms(metadata),
        metadata.reasoning_details,
        metadata.thinking_blocks,
        losses,
        _incomplete_loss_facts(losses),
    )
    return incomplete


def require_issued_v4_incomplete(incomplete: V4ProviderResultIncomplete, *, sdk_return: V4ProviderSdkReturn | None = None) -> None:
    """Require exact parser-issued loss DATA before independent SQL custody proof.

    The SQL owner still proves its actual attempt/H/fence/audit and terminal
    disposition. This check creates no SDK origin or settlement authority.
    """
    if type(incomplete) is not V4ProviderResultIncomplete:
        raise AuditIntegrityError("V4 incomplete result is not its original parser issuance")
    issued = _ISSUED_INCOMPLETE.get(incomplete)
    if issued is None:
        raise AuditIntegrityError("V4 incomplete result is not its original parser issuance")
    actual = incomplete.sdk_return
    if (
        type(actual) is not V4ProviderSdkReturn
        or actual is not issued.sdk_return
        or (sdk_return is not None and actual is not sdk_return)
        or type(actual.attempt_id) is not str
        or type(actual.response_identity) is not int
        or (actual.attempt_id, actual.response_identity) != issued.sdk_facts
        or incomplete.metadata is not issued.metadata
        or _incomplete_metadata_atoms(incomplete.metadata) != issued.metadata_atoms
        or incomplete.metadata.reasoning_details is not issued.reasoning_details
        or incomplete.metadata.thinking_blocks is not issued.thinking_blocks
        or incomplete.losses is not issued.losses
        or _incomplete_loss_facts(incomplete.losses) != issued.loss_facts
    ):
        raise AuditIntegrityError("V4 incomplete result changed its original parser-issued DATA")


class _Unavailable(Exception):
    def __init__(self, reason: V4ProviderResultLossReason, scalar_utf8_bytes: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.scalar_utf8_bytes = scalar_utf8_bytes


@dataclass(slots=True)
class _Budget:
    nodes: int = 1
    text_bytes: int = 0

    def text(self, value: str) -> str:
        from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES

        try:
            size = utf8_size(value)
        except UnicodeEncodeError:
            raise _Unavailable("invalid_utf8") from None
        if size > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise _Unavailable("scalar_utf8_limit", size)
        self.text_bytes += size
        if self.text_bytes > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise _Unavailable("text_budget")
        return value


@dataclass(slots=True)
class _CapturedProviderValues:
    content: str | None
    model_returned: str | None
    provider_request_id: str | None
    finish_reason: str | None
    reasoning_content: str | None
    reasoning_details: JsonValue
    thinking_blocks: list[dict[str, str]] | None = None


def _copy(value: object, budget: _Budget, active: set[int], depth: int = 1) -> Any:
    from elspeth.web.composer.bounded_json import JSON_MAX_DEPTH, JSON_MAX_ITEMS
    from elspeth.web.composer.llm_response_parsing import _provider_artifact_owned_fields

    budget.nodes += 1
    if depth > JSON_MAX_DEPTH:
        raise _Unavailable("depth_limit")
    if budget.nodes > JSON_MAX_ITEMS:
        raise _Unavailable("item_limit")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _Unavailable("nonfinite")
        return value
    if type(value) is str:
        return budget.text(value)
    if id(value) in active:
        raise _Unavailable("recursive")
    active.add(id(value))
    try:
        if type(value) is dict:
            if len(value) > JSON_MAX_ITEMS:
                raise _Unavailable("item_limit")
            result = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise _Unavailable("invalid_type")
                result[budget.text(key)] = _copy(child, budget, active, depth + 1)
            return result
        if type(value) in (list, tuple):
            sequence = cast(list[object] | tuple[object, ...], value)
            if len(sequence) > JSON_MAX_ITEMS:
                raise _Unavailable("item_limit")
            return [_copy(child, budget, active, depth + 1) for child in sequence]
        fields = _provider_artifact_owned_fields(value)
        if fields is None:
            raise _Unavailable("unsupported")
        return _copy(dict(fields), budget, active, depth)
    finally:
        active.remove(id(value))


def diagnostic_provider_metadata(metadata: _AdmittedLLMProviderMetadata) -> _AdmittedLLMProviderMetadata:
    witness = metadata.v4_completeness
    if witness is None:
        return metadata
    if type(witness) is not V4ProviderResultCompleteness:
        raise AuditIntegrityError("V4 result has a foreign completeness witness")
    witness.require_metadata(metadata)
    return witness.diagnostic_metadata


def project_v4_provider_diagnostic_metadata(metadata: _AdmittedLLMProviderMetadata) -> _AdmittedLLMProviderMetadata:
    """Project owned full private DATA; prove neither its source nor completeness.

    A cold owner must already have proved its actual V4 retained source and
    receipt. This projection never creates or restores a live witness.
    """
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata
    from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE, JsonBoundaryError, require_bounded_text
    from elspeth.web.composer.llm_response_parsing import (
        _PROVIDER_IDENTIFIER_MAX_CHARS,
        _PROVIDER_TOKEN_MAX_CHARS,
        _bounded_provider_string,
        _json_safe_provider_artifact,
    )

    if type(metadata) is not _AdmittedLLMProviderMetadata:
        raise AuditIntegrityError("V4 diagnostic projection requires owned private metadata DATA")

    def short(value: str | None, limit: int) -> str | None:
        if value is not None and type(value) is not str:
            raise AuditIntegrityError("V4 private metadata changed its original text type")
        return _bounded_provider_string(value, limit=limit) if value is not None and value.strip() else None

    reasoning = metadata.reasoning_content
    if reasoning is not None and type(reasoning) is not str:
        raise AuditIntegrityError("V4 private reasoning changed its original text type")
    try:
        reasoning = require_bounded_text(reasoning, label="provider reasoning content") if reasoning is not None else None
    except JsonBoundaryError:
        reasoning = PROVIDER_ARTIFACT_UNAVAILABLE
    return replace(
        metadata,
        model_returned=short(metadata.model_returned, _PROVIDER_IDENTIFIER_MAX_CHARS),
        provider_request_id=short(metadata.provider_request_id, _PROVIDER_IDENTIFIER_MAX_CHARS),
        finish_reason=short(metadata.finish_reason, _PROVIDER_TOKEN_MAX_CHARS),
        reasoning_content=reasoning,
        reasoning_details=_json_safe_provider_artifact(deep_thaw(metadata.reasoning_details)),
        v4_completeness=None,
    )


def _require_v4_provider_result_credentials(value: object, *, surface: str) -> None:
    """Inspect only the bounded owned V4 provider projection, never tool policy.

    The private-result grammar permits 1 MiB text, depth64 and 10k nodes. This
    explicit per-result detector policy does not alter generic/control limits.
    Raw tool arguments retain the gateway's existing state-aware admission.
    """
    from elspeth.contracts.credential_material import CredentialTraversalPolicy, find_credential_material
    from elspeth.web.composer.bounded_json import JSON_MAX_DEPTH, JSON_MAX_ITEMS, JSON_MAX_TOTAL_UTF8_BYTES
    from elspeth.web.credential_guard import CredentialMaterialRefused

    finding = find_credential_material(
        value,
        CredentialTraversalPolicy(
            max_depth=JSON_MAX_DEPTH,
            max_nodes=JSON_MAX_ITEMS + 32,
            max_width=JSON_MAX_ITEMS,
            max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
        ),
    )
    if finding is not None:
        raise CredentialMaterialRefused(surface=surface, finding=finding)


def require_v4_provider_metadata_credentials(
    metadata: _AdmittedLLMProviderMetadata,
    *,
    content: str | None,
    tool_calls: Sequence[tuple[str, str]],
    surface: str,
) -> None:
    """Recheck the exact source-issued result under its provider-only policy."""
    witness = metadata.v4_completeness
    if type(witness) is not V4ProviderResultCompleteness:
        raise AuditIntegrityError("V4 credential admission requires its original result capture")
    witness.require_metadata(metadata)
    if content is not None and (type(content) is not str or content != witness.content):
        raise AuditIntegrityError("V4 credential admission changed original content")
    names = tuple((call.id, call.function.name) for call in witness.tool_calls)
    if tool_calls and tuple(tool_calls) != names:
        raise AuditIntegrityError("V4 credential admission changed original tool identity")
    _require_v4_provider_result_credentials(
        {
            "content": witness.content,
            "tool_calls": [{"id": call_id, "name": name} for call_id, name in names],
            "model_returned": metadata.model_returned,
            "provider_request_id": metadata.provider_request_id,
            "finish_reason": metadata.finish_reason,
            "reasoning_content": metadata.reasoning_content,
            "reasoning_details": metadata.reasoning_details,
        },
        surface=surface,
    )
    if metadata.thinking_blocks is not None:
        from elspeth.contracts.freeze import deep_thaw
        from elspeth.web.composer.provider_thinking import native_thinking_blocks

        native_thinking_blocks(deep_thaw(metadata.thinking_blocks))


def require_retained_v4_provider_metadata_credentials(completion: _AdmittedLLMCompletion, *, surface: str) -> None:
    """Validate retained full-result DATA without minting source authority.

    The cold caller independently proves its V4 source/bytes/receipt first.
    This function only checks the nominal decoded fields and credentials; it
    grants no completeness, SDK, application, retry or SQL permission.
    """
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer._compose_loop_carriers import (
        _AdmittedAssistantMessage,
        _AdmittedLLMCompletion,
        _AdmittedLLMProviderMetadata,
        _AdmittedToolBatch,
        _AdmittedToolCall,
        _AdmittedToolFunction,
    )
    from elspeth.web.composer.provider_thinking import native_thinking_blocks

    if (
        type(completion) is not _AdmittedLLMCompletion
        or type(completion.message) is not _AdmittedAssistantMessage
        or type(completion.provider_metadata) is not _AdmittedLLMProviderMetadata
        or type(completion.tool_batch) is not _AdmittedToolBatch
        or type(completion.tool_batch.calls) is not tuple
        or type(completion.tool_batch.call_ids) is not frozenset
        or (completion.message.content is not None and type(completion.message.content) is not str)
    ):
        raise AuditIntegrityError("V4 retained credential check requires an owned complete-result shape")
    calls = completion.tool_batch.calls
    if any(
        type(call) is not _AdmittedToolCall
        or type(call.function) is not _AdmittedToolFunction
        or type(call.id) is not str
        or type(call.function.name) is not str
        or type(call.function.arguments) is not str
        for call in calls
    ) or completion.tool_batch.call_ids != frozenset(call.id for call in calls):
        raise AuditIntegrityError("V4 retained credential check changed its original tool fields")
    if len(completion.tool_batch.call_ids) != len(calls):
        raise AuditIntegrityError("V4 retained credential check repeats a tool identity")
    metadata = completion.provider_metadata
    if any(
        value is not None and type(value) is not str
        for value in (
            metadata.model_returned,
            metadata.provider_request_id,
            metadata.finish_reason,
            metadata.reasoning_content,
            metadata.provider_served,
        )
    ):
        raise AuditIntegrityError("V4 retained credential check changed its original metadata text types")
    _require_v4_provider_result_credentials(
        {
            "content": completion.message.content,
            "tool_calls": [{"id": call.id, "name": call.function.name, "arguments": call.function.arguments} for call in calls],
            "model_returned": metadata.model_returned,
            "provider_request_id": metadata.provider_request_id,
            "finish_reason": metadata.finish_reason,
            "reasoning_content": metadata.reasoning_content,
            "reasoning_details": metadata.reasoning_details,
            "provider_served": metadata.provider_served,
        },
        surface=surface,
    )
    if metadata.thinking_blocks is not None:
        native_thinking_blocks(deep_thaw(metadata.thinking_blocks))


def admit_v4_provider_result(
    response: Any,
    *,
    choice: Any | None,
    message: Any | None,
    sdk_return: V4ProviderSdkReturn,
    pricing_model: str | None = None,
    surface: str = "composer_provider_response",
    require_tool_calls_field: bool = True,
) -> V4ProviderResultAdmission:
    """Capture original selected fields once; never grant retry on lost output."""
    from elspeth.core.llm_pricing import _provider_field_map, _token_usage_from_usage, provider_cost_from_captured_usage
    from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdmittedLLMProviderMetadata
    from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE, JsonBoundaryError, require_bounded_text
    from elspeth.web.composer.llm_response_parsing import (
        _PROVIDER_IDENTIFIER_MAX_CHARS,
        _PROVIDER_TOKEN_MAX_CHARS,
        _admit_provider_served,
        _bounded_provider_string,
        _captured_field,
        _json_safe_provider_artifact,
    )
    from elspeth.web.composer.protocol import ComposerAdmissionRefused
    from elspeth.web.composer.provider_thinking import parse_native_thinking_blocks
    from elspeth.web.composer.tool_batch import _admit_tool_batch

    if (
        type(sdk_return) is not V4ProviderSdkReturn
        or type(sdk_return.attempt_id) is not str
        or not sdk_return.attempt_id
        or type(sdk_return.response_identity) is not int
        or sdk_return.response_identity != id(response)
    ):
        raise AuditIntegrityError("V4 result lacks its exact successful SDK-return witness")
    response_fields, choice_fields, message_fields = (
        _provider_field_map(response),
        _provider_field_map(choice),
        _provider_field_map(message),
    )
    usage_raw = _captured_field(response_fields, "usage")
    usage = _token_usage_from_usage(usage_raw)
    cost, cost_source = provider_cost_from_captured_usage(response, usage_raw, pricing_model=pricing_model)
    model = _captured_field(response_fields, "model")
    identifier = _captured_field(response_fields, "id")
    if type(identifier) is not str or not identifier.strip():
        identifier = _captured_field(response_fields, "request_id")
    finish = _captured_field(choice_fields, "finish_reason")
    reasoning = _captured_field(message_fields, "reasoning")
    if type(reasoning) is not str:
        reasoning = _captured_field(message_fields, "reasoning_content")
    details = _captured_field(message_fields, "reasoning_details")
    if details is None:
        specific = _provider_field_map(_captured_field(message_fields, "provider_specific_fields"))
        details = _captured_field(specific, "reasoning_details")
    native = _captured_field(message_fields, "thinking_blocks")
    missing = object()
    try:
        content = missing if message_fields is None else message_fields["content"]
    except KeyError:
        content = missing
    try:
        tool_calls = missing if message_fields is None else message_fields["tool_calls"]
    except KeyError:
        tool_calls = missing
    if tool_calls is missing and not require_tool_calls_field:
        tool_calls = None
    provider = _admit_provider_served(_captured_field(response_fields, "provider"))
    budget = _Budget()
    losses: list[V4ProviderResultLoss] = []

    def capture(name: V4ProviderResultField, raw: object, *, text: bool = False) -> Any:
        try:
            if raw is missing:
                raise _Unavailable("missing")
            if text and raw is not None and type(raw) is not str:
                raise _Unavailable("invalid_type")
            return _copy(raw, budget, set())
        except _Unavailable as failure:
            losses.append(V4ProviderResultLoss(name, failure.reason, failure.scalar_utf8_bytes))
            return None

    values = _CapturedProviderValues(
        content=capture("content", content, text=True),
        model_returned=capture("model_returned", model, text=True),
        provider_request_id=capture("provider_request_id", identifier, text=True),
        finish_reason=capture("finish_reason", finish, text=True),
        reasoning_content=capture("reasoning_content", reasoning, text=True),
        reasoning_details=capture("reasoning_details", details),
    )
    try:
        owned_native = None if native is None else parse_native_thinking_blocks(native)
    except ComposerAdmissionRefused:
        losses.append(V4ProviderResultLoss("thinking_blocks", "invalid_native"))
        owned_native = None
    values.thinking_blocks = capture("thinking_blocks", owned_native)
    calls: tuple[_AdmittedToolCall, ...] = ()
    try:
        if tool_calls is missing:
            raise _Unavailable("missing")
        if tool_calls is not None and type(tool_calls) not in (list, tuple):
            raise _Unavailable("invalid_type")
        admitted_batch = _admit_tool_batch(cast(Sequence[Any], tool_calls) if tool_calls is not None else ())
        calls = admitted_batch.calls
        for call in calls:
            capture("tool_calls", call.id, text=True)
            capture("tool_calls", call.function.name, text=True)
            capture("tool_calls", call.function.arguments, text=True)
    except _Unavailable as failure:
        losses.append(V4ProviderResultLoss("tool_calls", failure.reason, failure.scalar_utf8_bytes))
    except AuditIntegrityError:
        losses.append(V4ProviderResultLoss("tool_calls", "invalid_tool_calls"))

    def short(raw: object, limit: int) -> str | None:
        return _bounded_provider_string(raw, limit=limit) if type(raw) is str and raw.strip() else None

    try:
        diagnostic_reasoning = require_bounded_text(reasoning, label="provider reasoning content") if type(reasoning) is str else None
    except JsonBoundaryError:
        diagnostic_reasoning = PROVIDER_ARTIFACT_UNAVAILABLE
    diagnostic = _AdmittedLLMProviderMetadata(
        model_returned=short(model, _PROVIDER_IDENTIFIER_MAX_CHARS),
        finish_reason=short(finish, _PROVIDER_TOKEN_MAX_CHARS),
        usage=usage,
        provider_cost=cost,
        provider_cost_source=cost_source,
        provider_request_id=short(identifier, _PROVIDER_IDENTIFIER_MAX_CHARS),
        reasoning_content=diagnostic_reasoning,
        reasoning_details=_json_safe_provider_artifact(values.reasoning_details)
        if not any(loss.field == "reasoning_details" for loss in losses)
        else PROVIDER_ARTIFACT_UNAVAILABLE,
        thinking_blocks=values.thinking_blocks
        if not any(loss.field == "thinking_blocks" for loss in losses)
        else PROVIDER_ARTIFACT_UNAVAILABLE,
        provider_served=provider,
    )
    if losses:
        raise _issue_v4_incomplete(sdk_return=sdk_return, metadata=diagnostic, losses=tuple(losses))
    _require_v4_provider_result_credentials(
        {
            "content": values.content,
            "model_returned": values.model_returned,
            "provider_request_id": values.provider_request_id,
            "finish_reason": values.finish_reason,
            "reasoning_content": values.reasoning_content,
            "reasoning_details": values.reasoning_details,
            "tool_calls": [{"id": call.id, "name": call.function.name} for call in calls],
        },
        surface=surface,
    )
    if values.thinking_blocks is not None:
        from elspeth.web.composer.provider_thinking import native_thinking_blocks

        native_thinking_blocks(values.thinking_blocks)
    original = _AdmittedLLMProviderMetadata(
        model_returned=values.model_returned,
        finish_reason=values.finish_reason,
        usage=usage,
        provider_cost=cost,
        provider_cost_source=cost_source,
        provider_request_id=values.provider_request_id,
        reasoning_content=values.reasoning_content,
        reasoning_details=values.reasoning_details,
        thinking_blocks=values.thinking_blocks,
        provider_served=provider,
    )
    witness = V4ProviderResultCompleteness(sdk_return, diagnostic, values.content, calls)
    original = replace(original, v4_completeness=witness)
    _ISSUED_METADATA[id(witness)] = original
    return V4ProviderResultAdmission(_AdmittedAssistantMessage(values.content), calls, original, sdk_return)

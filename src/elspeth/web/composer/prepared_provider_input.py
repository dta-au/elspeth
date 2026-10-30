"""One bounded, content-bound preparation for one physical provider attempt.

Borrowed projections describe the final cache-adapted wire before request-owned
copying. Existing source, provenance and credential failures retain precedence
over ordinary byte refusal. A preparation is neither inspection nor admission;
the physical owner still runs fresh guard, required audit and quota controls.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import cast

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, bounded_canonical_json_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_PROVENANCE_KEYS,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    DetachedPromptGuardEvidence,
    GuardedProviderMessage,
    PromptGuardContentPart,
    PromptGuardMode,
    PromptGuardRole,
    PromptGuardThinkingParts,
    PromptGuardToolArgumentParts,
    validate_prompt_guard_content_parts,
    validate_prompt_guard_messages,
    validate_prompt_guard_thinking_parts,
    validate_prompt_guard_tool_argument_parts,
)
from elspeth.contracts.readonly_json import JsonValue, ReadonlyJsonArray, ReadonlyJsonObject
from elspeth.web.composer.advisor_content import PreparedAdvisorContent, scan_prepared_advisor_content
from elspeth.web.composer.llm_response_parsing import AnthropicCacheMarkerPlan, plan_anthropic_cache_markers
from elspeth.web.composer.prompt_guard import (
    PromptGuardMount,
    resolve_prompt_guard_user_prompt,
    validate_prompt_guard_credentials,
)
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_thinking import scan_native_thinking_blocks
from elspeth.web.credential_guard import require_no_credential_material


@dataclass(frozen=True, slots=True)
class _ObjectView(ReadonlyJsonObject):
    source: GuardedProviderMessage = field(repr=False)
    excluded: frozenset[str] = frozenset()
    cache_marker: bool = False

    def __len__(self) -> int:
        return (
            len(self.source)
            - sum(key in self.source for key in self.excluded)
            + int(self.cache_marker and "cache_control" not in self.source)
        )

    def __iter__(self) -> Iterator[object]:
        for key in self.source:
            if key not in self.excluded:
                yield key
        if self.cache_marker and "cache_control" not in self.source:
            yield "cache_control"

    def __getitem__(self, key: object) -> object:
        if key in self.excluded:
            raise KeyError(key)
        if key == "cache_control" and self.cache_marker:
            return {"type": "ephemeral"}
        if not isinstance(key, str):
            # Preserve malformed keys for the original credential/JSON owner;
            # the view never coerces them into admitted string keys.
            for actual, value in self.source.items():
                if actual is key:
                    return value
            raise KeyError(key)
        return self.source[key]


@dataclass(frozen=True, slots=True)
class _ArrayView(ReadonlyJsonArray):
    source: Sequence[GuardedProviderMessage] = field(repr=False)
    excluded: frozenset[str]
    cache_indexes: tuple[int, ...] = ()

    def __len__(self) -> int:
        return len(self.source)

    def __iter__(self) -> Iterator[object]:
        for index, item in enumerate(self.source):
            yield _ObjectView(item, self.excluded, index in self.cache_indexes)


@dataclass(frozen=True, slots=True)
class _Provenance:
    trusted: frozenset[int]
    content: tuple[tuple[PromptGuardContentPart, ...] | None, ...] = field(repr=False)
    arguments: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] = field(repr=False)
    thinking: tuple[PromptGuardThinkingParts | None, ...] = field(repr=False)


def _validate_forwarded(
    messages: Sequence[GuardedProviderMessage],
    content: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None,
    arguments: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None,
    thinking: tuple[PromptGuardThinkingParts | None, ...] | None,
) -> None:
    if content is not None:
        validate_prompt_guard_content_parts(messages, content)
        for message, forwarded_content in zip(messages, content, strict=True):
            current = message[PROMPT_GUARD_CONTENT_PARTS_KEY] if PROMPT_GUARD_CONTENT_PARTS_KEY in message else None
            if current is not None and forwarded_content is not None and current != forwarded_content:
                raise AuditIntegrityError("Prompt guard content provenance changed before physical dispatch")
    if arguments is not None:
        validate_prompt_guard_tool_argument_parts(messages, arguments)
        for message, forwarded_arguments in zip(messages, arguments, strict=True):
            current = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message else None
            if current is not None and forwarded_arguments is not None and current != forwarded_arguments:
                raise AuditIntegrityError("Prompt guard tool argument provenance changed before physical dispatch")
    if thinking is not None:
        validate_prompt_guard_thinking_parts(messages, thinking)
        for message, forwarded_thinking in zip(messages, thinking, strict=True):
            current = message[PROMPT_GUARD_THINKING_PARTS_KEY] if PROMPT_GUARD_THINKING_PARTS_KEY in message else None
            if current is not None and forwarded_thinking is not None and current != forwarded_thinking:
                raise AuditIntegrityError("Provider thinking provenance changed before physical dispatch")


def _provenance(
    messages: Sequence[GuardedProviderMessage],
    trusted: frozenset[int],
    content: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None,
    arguments: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None,
    thinking: tuple[PromptGuardThinkingParts | None, ...] | None,
) -> _Provenance:
    if type(trusted) is not frozenset or any(type(index) is not int or not 0 <= index < len(messages) for index in trusted):
        raise AuditIntegrityError("Prompt guard trusted provenance lacks exact message positions")
    indexes = set(trusted)
    contents: list[tuple[PromptGuardContentPart, ...] | None] = []
    argument_entries: list[tuple[PromptGuardToolArgumentParts, ...] | None] = []
    thinking_entries: list[PromptGuardThinkingParts | None] = []
    for index, message in enumerate(messages):
        if PROMPT_GUARD_TRUST_KEY in message:
            indexes.add(index)
        current_content = message[PROMPT_GUARD_CONTENT_PARTS_KEY] if PROMPT_GUARD_CONTENT_PARTS_KEY in message else None
        current_arguments = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message else None
        current_thinking = message[PROMPT_GUARD_THINKING_PARTS_KEY] if PROMPT_GUARD_THINKING_PARTS_KEY in message else None
        contents.append(
            cast(tuple[PromptGuardContentPart, ...], current_content)
            if current_content is not None
            else content[index]
            if content is not None
            else None
        )
        argument_entries.append(
            cast(tuple[PromptGuardToolArgumentParts, ...], current_arguments)
            if current_arguments is not None
            else arguments[index]
            if arguments is not None
            else None
        )
        thinking_entries.append(
            cast(PromptGuardThinkingParts, current_thinking)
            if current_thinking is not None
            else thinking[index]
            if thinking is not None
            else None
        )
    result = _Provenance(frozenset(indexes), tuple(contents), tuple(argument_entries), tuple(thinking_entries))
    if any(
        result.content[index] is not None or result.arguments[index] is not None or result.thinking[index] is not None
        for index in result.trusted
    ):
        raise AuditIntegrityError("Prompt guard partition conflicts with whole-message trust")
    return result


def _credential_check(
    messages: _ArrayView,
    *,
    advisor: tuple[PreparedAdvisorContent, ...] | None,
    role: PromptGuardRole,
) -> None:
    # This is the existing credential-only projection, after bounded counting.
    # Native strings retain their complete closed-grammar scan; only that field
    # is removed from the generic residual tree. Tools retain their existing
    # separate ownership and are not added to this message-root traversal.
    residual: list[Mapping[str, JsonValue]] = []
    for index, source in enumerate(messages.source):
        if "thinking_blocks" in source:
            if source["role"] != "assistant":
                raise AuditIntegrityError("Provider thinking belongs to an assistant message")
            scan_native_thinking_blocks(source["thinking_blocks"])
            excluded = messages.excluded | {"thinking_blocks"}
        else:
            excluded = messages.excluded
        view = _ObjectView(source, excluded, index in messages.cache_indexes)
        residual.append(cast(dict[str, JsonValue], dict(view.items())))
    checked = scan_prepared_advisor_content(residual, advisor, role=role) if advisor is not None else residual
    require_no_credential_material(checked, surface="composer_provider_request")


def _materialize(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (Mapping, ReadonlyJsonObject)):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditIntegrityError("Prepared provider input lost its JSON object keys")
            result[key] = _materialize(item)
        return result
    if isinstance(value, (list, tuple, ReadonlyJsonArray)):
        return [_materialize(item) for item in value]
    raise AuditIntegrityError("Prepared provider input lost its validated JSON values")


def _text_binding(update: Callable[[bytes], None], text: str | None) -> None:
    if text is None:
        update(b"N;")
        return
    update(b"S")
    # This is a process-local provenance binding, not the wire size/hash.
    # Preserve every string codepoint so the actual guard retains its existing
    # unsupported-Unicode disposition for genuine/inspection-only text.
    update(str(len(text)).encode("ascii"))
    update(b":")
    for start in range(0, len(text), 4096):
        update(text[start : start + 4096].encode("utf-8", errors="surrogatepass"))
    update(b";")


def _binding(evidence: DetachedPromptGuardEvidence, tools: list[dict[str, JsonValue]] | None, genuine_user: str | None) -> str:
    digest = hashlib.sha256(b"composer.prepared-provider-input.v1\x00")
    wire = {"messages": evidence.messages, "tools": tools if tools is not None else [], "tools_present": tools is not None}
    digest.update(canonical_json(wire).encode("utf-8"))
    _text_binding(digest.update, genuine_user)
    for index in sorted(evidence.trusted_message_indexes):
        digest.update(f"T{index};".encode("ascii"))

    def parts(units: tuple[PromptGuardContentPart, ...] | None) -> None:
        if units is None:
            digest.update(b"N;")
            return
        digest.update(b"[")
        for part in units:
            _text_binding(digest.update, part.text)
            _text_binding(digest.update, part.trust.value if part.trust is not None else None)
            _text_binding(digest.update, part.inspection_text)
        digest.update(b"]")

    for content in evidence.content_parts:
        parts(content)
    digest.update(b"A")
    for entries in evidence.tool_argument_parts:
        if entries is None:
            digest.update(b"N;")
            continue
        digest.update(b"[")
        for entry in entries:
            digest.update(f"I{entry.tool_call_index};".encode("ascii"))
            parts(entry.parts)
        digest.update(b"]")
    digest.update(b"H")
    for thinking_entry in evidence.thinking_parts:
        parts(thinking_entry.parts if thinking_entry is not None else None)
    return digest.hexdigest()


@dataclass(slots=True)
class ProviderDispatchInput:
    """One isolated SDK copy, still bound to its original preparation."""

    evidence: DetachedPromptGuardEvidence = field(repr=False)
    tools: list[dict[str, JsonValue]] | None = field(repr=False)


@dataclass(slots=True)
class PreparedProviderInput:
    """Process-local input ownership, consumed by exactly one physical attempt."""

    evidence: DetachedPromptGuardEvidence = field(repr=False)
    tools: list[dict[str, JsonValue]] | None = field(repr=False)
    byte_count: int
    byte_limit: int
    model: str
    role: PromptGuardRole
    mount: PromptGuardMount | None = field(repr=False)
    mode: PromptGuardMode
    genuine_user: str | None = field(repr=False)
    cache_plan: AnthropicCacheMarkerPlan
    advisor_content: tuple[PreparedAdvisorContent, ...] | None = field(default=None, repr=False)
    _seal: str = field(init=False, repr=False)
    _limit: int = field(init=False, repr=False)
    _model: str = field(init=False, repr=False)
    _role: PromptGuardRole = field(init=False, repr=False)
    _mount: PromptGuardMount | None = field(init=False, repr=False)
    _mode: PromptGuardMode = field(init=False, repr=False)
    _cache_plan: AnthropicCacheMarkerPlan = field(init=False, repr=False)
    _guard_client: object = field(init=False, repr=False)
    _guard_adapter: str | None = field(init=False, repr=False)
    _guard_fingerprint: str | None = field(init=False, repr=False)
    _guard_timeout: int | float | None = field(init=False, repr=False)
    _advisor_content: tuple[PreparedAdvisorContent, ...] | None = field(init=False, repr=False)
    _consumed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.evidence) is not DetachedPromptGuardEvidence or type(self.byte_count) is not int:
            raise AuditIntegrityError("Prepared provider input lacks exact owned evidence and byte count")
        result = bounded_canonical_json_size(
            {"messages": self.evidence.messages, "tools": self.tools if self.tools is not None else []}, limit=self.byte_limit
        )
        if result.exceeded or result.admitted_bytes != self.byte_count:
            raise AuditIntegrityError("Prepared provider input lacks its exact admitted aggregate size")
        self._seal = _binding(self.evidence, self.tools, self.genuine_user)
        self._limit, self._model, self._role, self._mount, self._mode = (
            self.byte_limit,
            self.model,
            self.role,
            self.mount,
            self.mode,
        )
        self._cache_plan = self.cache_plan
        self._guard_client = self.mount.client if self.mount is not None else None
        self._guard_adapter = self.mount.client.adapter if self.mount is not None else None
        self._guard_fingerprint = self.mount.client.policy_fingerprint if self.mount is not None else None
        self._guard_timeout = self.mount.client.timeout_seconds if self.mount is not None else None
        self._advisor_content = self.advisor_content

    @property
    def tools_present(self) -> bool:
        return self.tools is not None

    def verify(
        self,
        *,
        model: str,
        role: PromptGuardRole,
        mount: PromptGuardMount | None,
        mode: PromptGuardMode,
        user_prompt: str | None,
        dispatch: ProviderDispatchInput | None = None,
    ) -> None:
        if type(self.byte_count) is not int or type(self.byte_limit) is not int:
            raise AuditIntegrityError("Prepared provider input changed its exact size metadata")
        if (
            self.model != self._model
            or model != self._model
            or self.role != self._role
            or role != self._role
            or self.mount is not self._mount
            or mount is not self._mount
            or self.mode != self._mode
            or mode != self._mode
            or self.byte_limit != self._limit
            or self.cache_plan != self._cache_plan
            or self.advisor_content != self._advisor_content
            or resolve_prompt_guard_user_prompt(user_prompt) != self.genuine_user
        ):
            raise AuditIntegrityError("Prepared provider input changed its selected request owner")
        if self.mount is not None and (
            self.mount.client is not self._guard_client
            or self.mount.client.adapter != self._guard_adapter
            or self.mount.client.policy_fingerprint != self._guard_fingerprint
            or self.mount.client.timeout_seconds != self._guard_timeout
        ):
            raise AuditIntegrityError("Prepared provider input changed its selected guard mount")
        candidates = (
            ((self.evidence, self.tools),)
            if dispatch is None
            else (
                (self.evidence, self.tools),
                (dispatch.evidence, dispatch.tools),
            )
        )
        for evidence, tools in candidates:
            if (
                type(evidence) is not DetachedPromptGuardEvidence
                or type(evidence.messages) is not list
                or any(type(message) is not dict for message in evidence.messages)
                or type(evidence.trusted_message_indexes) is not frozenset
                or any(type(index) is not int or not 0 <= index < len(evidence.messages) for index in evidence.trusted_message_indexes)
                or (tools is not None and (type(tools) is not list or any(type(tool) is not dict for tool in tools)))
            ):
                raise AuditIntegrityError("Prepared provider input changed its owned wire/provenance containers")
            try:
                size = bounded_canonical_json_size(
                    {"messages": evidence.messages, "tools": tools if tools is not None else []}, limit=self._limit
                )
            except (TypeError, ValueError, RecursionError) as error:
                raise AuditIntegrityError("Prepared provider input lost its validated wire/provenance binding") from error
            if size.exceeded or size.admitted_bytes != self.byte_count:
                raise AuditIntegrityError("Prepared provider input changed after preparation")
            try:
                validate_prompt_guard_content_parts(evidence.messages, evidence.content_parts)
                validate_prompt_guard_tool_argument_parts(evidence.messages, evidence.tool_argument_parts)
                validate_prompt_guard_thinking_parts(evidence.messages, evidence.thinking_parts)
                if any(
                    evidence.content_parts[index] is not None
                    or evidence.tool_argument_parts[index] is not None
                    or evidence.thinking_parts[index] is not None
                    for index in evidence.trusted_message_indexes
                ):
                    raise AuditIntegrityError("Prompt guard partition conflicts with whole-message trust")
                actual = _binding(evidence, tools, self.genuine_user)
            except (TypeError, ValueError, RecursionError) as error:
                raise AuditIntegrityError("Prepared provider input lost its validated wire/provenance binding") from error
            if actual != self._seal:
                raise AuditIntegrityError("Prepared provider input changed after preparation")

    def consume(
        self,
        *,
        model: str,
        role: PromptGuardRole,
        mount: PromptGuardMount | None,
        mode: PromptGuardMode,
        user_prompt: str | None,
    ) -> ProviderDispatchInput:
        if self._consumed:
            raise AuditIntegrityError("Prepared provider input was reused for another physical attempt")
        self.verify(model=model, role=role, mount=mount, mode=mode, user_prompt=user_prompt)
        self._consumed = True
        # Preserve the existing SDK/caller alias boundary. Copying is now
        # bounded by admission; audit/capture retain the original preparation.
        evidence = DetachedPromptGuardEvidence(
            deepcopy(self.evidence.messages),
            self.evidence.trusted_message_indexes,
            self.evidence.content_parts,
            self.evidence.tool_argument_parts,
            self.evidence.thinking_parts,
        )
        return ProviderDispatchInput(evidence, deepcopy(self.tools))


def prepare_provider_input(
    *,
    messages: Sequence[GuardedProviderMessage],
    tools: Sequence[Mapping[str, JsonValue]] | None,
    tools_present: bool,
    model: str,
    role: PromptGuardRole,
    mount: PromptGuardMount | None,
    mode: PromptGuardMode,
    user_prompt: str | None,
    max_bytes: int,
    cache_markers: bool = False,
    mark_history_tail: bool = False,
    trusted_message_indexes: frozenset[int] = frozenset(),
    content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
    tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    thinking_parts: tuple[PromptGuardThinkingParts | None, ...] | None = None,
    advisor_content: tuple[PreparedAdvisorContent, ...] | None = None,
) -> PreparedProviderInput:
    """Resolve one final candidate, refuse valid excess, then detach it once."""
    if type(max_bytes) is not int or max_bytes < 1 or type(tools_present) is not bool:
        raise AuditIntegrityError("Provider preparation requires exact supported limit and tools-presence decisions")
    if (tools_present and tools is None) or (not tools_present and tools):
        raise AuditIntegrityError("Provider preparation changed exact omitted-tools identity")
    if role not in ("composer", "advisor") or mode not in ("required", "debug-when-unconfigured"):
        raise AuditIntegrityError("Provider preparation changed its selected guard role or mode")
    if (mount is not None and type(mount) is not PromptGuardMount) or type(model) is not str:
        raise AuditIntegrityError("Provider preparation requires its exact mount and selected model")
    validate_prompt_guard_messages(messages)
    _validate_forwarded(messages, content_parts, tool_argument_parts, thinking_parts)
    cache = (
        plan_anthropic_cache_markers(messages, tools, mark_history_tail=mark_history_tail)
        if cache_markers
        else AnthropicCacheMarkerPlan((), None)
    )
    message_view = _ArrayView(messages, PROMPT_GUARD_PROVENANCE_KEYS, cache.message_indexes)
    tool_view = _ArrayView(tools if tools is not None else (), frozenset(), (cache.tool_index,) if cache.tool_index is not None else ())
    size: BoundedCanonicalSize | None = None
    serialization_error: Exception | None = None
    try:
        size = bounded_canonical_json_size({"messages": message_view, "tools": tool_view if tools_present else []}, limit=max_bytes)
    except (TypeError, ValueError, RecursionError) as error:
        # Native/generic credential validation preceded request serialization
        # at the original physical owner. Preserve that failure ordering.
        serialization_error = error
    _credential_check(message_view, advisor=advisor_content, role=role)
    if serialization_error is not None:
        raise serialization_error
    if size is None:
        raise AuditIntegrityError("Provider preparation lost its exact aggregate size result")
    provenance = _provenance(messages, trusted_message_indexes, content_parts, tool_argument_parts, thinking_parts)
    genuine = resolve_prompt_guard_user_prompt(user_prompt)
    if size.exceeded:
        validate_prompt_guard_credentials(
            messages=messages,
            genuine_user=genuine,
            trusted_message_indexes=provenance.trusted,
            content_parts=provenance.content,
            tool_argument_parts=provenance.arguments,
            thinking_parts=provenance.thinking,
        )
        raise ComposerContextUnsupported("prepared_request_bytes")
    materialized_messages = _materialize(message_view)
    materialized_tools = _materialize(tool_view) if tools_present else None
    evidence = DetachedPromptGuardEvidence(
        cast(list[dict[str, JsonValue]], materialized_messages),
        provenance.trusted,
        provenance.content,
        provenance.arguments,
        provenance.thinking,
    )
    assert size.admitted_bytes is not None
    return PreparedProviderInput(
        evidence=evidence,
        tools=cast(list[dict[str, JsonValue]] | None, materialized_tools),
        byte_count=size.admitted_bytes,
        byte_limit=max_bytes,
        model=model,
        role=role,
        mount=mount,
        mode=mode,
        genuine_user=genuine,
        cache_plan=cache,
        advisor_content=advisor_content,
    )

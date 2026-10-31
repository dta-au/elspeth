"""Fresh mandatory continuation retention, separate from stored-data admission.

The producing owner first validates its complete evidence and credentials, then
measures the exact prospective canonical envelope. Borrowed JSON views may be
used before materialization. No stored decoder calls this sizing refusal path.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Self
from uuid import UUID

from pydantic import ValidationError

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, bounded_canonical_json_size, utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PROMPT_GUARD_PROVENANCE_KEYS
from elspeth.contracts.readonly_json import ReadonlyJsonArray, ReadonlyJsonObject, ReadonlyJsonString
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.protocol import ComposerContextUnsupported

type RetentionSizeReason = Literal["retained_history_bytes", "retained_chunk_bytes"]


def require_retention_capacity(measured: BoundedCanonicalSize, *, reason: RetentionSizeReason) -> int:
    """Classify an observed excess only after the owner finishes validation."""
    if measured.limit != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("mandatory retention measurement changed its actual byte limit")
    if measured.admitted_bytes is None:
        raise ComposerContextUnsupported(reason)
    return measured.admitted_bytes


def preflight_retained_json(value: object, *, reason: RetentionSizeReason) -> int:
    """Measure complete canonical bytes before constructing the encoding.

    Excess still traverses the complete value: malformed primitives, Unicode
    and cycles keep their original canonical failure instead of becoming an
    ordinary context refusal. This is only a size observation; graph/source
    integrity and credential admission remain with the producing owner.
    """
    measured = bounded_canonical_json_size(value, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    return require_retention_capacity(measured, reason=reason)


def bind_retained_json_encoding(canonical: str, *, expected_bytes: int) -> str:
    """Require the actual encoding to agree with its admitted complete size.

    The ordinary private-chunk encoder still proves canonical JSON, credentials
    and the content binding. A changed projection or encoder is an integrity
    defect, including when it unexpectedly produces an overlarge encoding.
    """
    if (
        type(expected_bytes) is not int
        or not 0 <= expected_bytes <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or utf8_size(canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) != expected_bytes
    ):
        raise AuditIntegrityError("private chunk encoding differs from its complete retention preflight")
    return canonical


class OrderedObjectJSON(ReadonlyJsonString):
    """The exact compact ordered-object encoding as a prospective JSON string.

    Its encoder has the same primitive algebra and numeric-domain check as
    encode_transcript_object_json. Strings are escaped in bounded pieces;
    neither the inner object text nor its escaped outer copy is allocated.
    """

    __slots__ = ("_initialized", "source")

    source: Mapping[str, object]
    _initialized: bool

    def __new__(cls, source: Mapping[str, object]) -> Self:
        instance = object.__new__(cls)
        object.__setattr__(instance, "_initialized", False)
        return instance

    def __init__(self, source: Mapping[str, object]) -> None:
        if self._initialized:
            raise AttributeError("OrderedObjectJSON cannot be reinitialized")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("OrderedObjectJSON has read-only bindings")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("OrderedObjectJSON has read-only bindings")

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        actual = other
        return (self.source,) == (actual.source,)

    def chunks(self) -> Iterator[str]:
        active: set[int] = set()

        def text(value: str) -> Iterator[str]:
            yield '"'
            for start in range(0, len(value), 4096):
                yield json.dumps(value[start : start + 4096], ensure_ascii=False, allow_nan=False)[1:-1]
            yield '"'

        def visit(value: object) -> Iterator[str]:
            if type(value) is str:
                yield from text(value)
            elif value is None or type(value) in (bool, int):
                bounded_canonical_json_size(value, limit=1)
                yield json.dumps(value, ensure_ascii=False, allow_nan=False)
            elif type(value) is float:
                if not math.isfinite(value):
                    raise AuditIntegrityError("continuation value is not finite JSON")
                bounded_canonical_json_size(value, limit=1)
                yield json.dumps(value, ensure_ascii=False, allow_nan=False)
            elif isinstance(value, (Mapping, tuple, list)):
                identity = id(value)
                if identity in active:
                    raise RecursionError("Circular reference in continuation ordered JSON")
                active.add(identity)
                try:
                    if isinstance(value, Mapping):
                        if any(type(key) is not str for key in value):
                            raise AuditIntegrityError("continuation object has a non-string key")
                        yield "{"
                        for index, (key, child) in enumerate(value.items()):
                            if index:
                                yield ","
                            yield from text(key)
                            yield ":"
                            yield from visit(child)
                        yield "}"
                    else:
                        yield "["
                        for index, child in enumerate(value):
                            if index:
                                yield ","
                            yield from visit(child)
                        yield "]"
                finally:
                    active.remove(identity)
            else:
                raise AuditIntegrityError("continuation value is outside the owned JSON algebra")

        yield from visit(self.source)

    def canonical_string_bytes(self, *, limit: int) -> int:
        size = 2
        for chunk in self.chunks():
            measured = bounded_canonical_json_size(chunk, limit=limit)
            size = min(limit + 1, size + (measured.admitted_bytes - 2 if measured.admitted_bytes is not None else limit + 1))
        return size

    def matches(self, encoded: str) -> bool:
        """Compare complete source text without a second complete encoding."""
        offset = 0
        same = True
        for chunk in self.chunks():
            if encoded[offset : offset + len(chunk)] != chunk:
                same = False
            offset += len(chunk)
        return same and offset == len(encoded)


type RetainedModel = ContinuationValue | AdvisorAbandonmentResponse


@dataclass(slots=True)
class _ModelProjection:
    """Memoize borrowed container identities so cycles cannot evade sizing."""

    projected: dict[int, object] = field(default_factory=dict)

    def value(self, source: object) -> object:
        if isinstance(source, UUID):
            return str(source)
        if not isinstance(source, (ContinuationValue, AdvisorAbandonmentResponse, Mapping, tuple, list)):
            return source
        identity = id(source)
        if identity not in self.projected:
            self.projected[identity] = (
                _RetainedModelView(source, self)
                if isinstance(source, (ContinuationValue, AdvisorAbandonmentResponse))
                else _RetainedObjectView(source, self)
                if isinstance(source, Mapping)
                else _RetainedArrayView(source, self)
            )
        return self.projected[identity]


@dataclass(frozen=True, slots=True)
class _RetainedModelView(ReadonlyJsonObject):
    source: RetainedModel = field(repr=False)
    projection: _ModelProjection = field(repr=False)

    def __len__(self) -> int:
        return len(type(self.source).model_fields)

    def __iter__(self) -> Iterator[object]:
        yield from type(self.source).model_fields

    def __getitem__(self, key: object) -> object:
        if not isinstance(key, str) or key not in type(self.source).model_fields:
            raise KeyError(key)
        return self.projection.value(self.source.__dict__[key])


class _RetainedObjectView(ReadonlyJsonObject):
    """Read-only bindings over the original live object and projection owner."""

    __slots__ = ("_initialized", "projection", "source")

    source: Mapping[str, object]
    projection: _ModelProjection
    _initialized: bool

    def __new__(cls, source: Mapping[str, object], projection: _ModelProjection) -> Self:
        instance = object.__new__(cls)
        object.__setattr__(instance, "_initialized", False)
        return instance

    def __init__(self, source: Mapping[str, object], projection: _ModelProjection) -> None:
        if self._initialized:
            raise AttributeError("_RetainedObjectView cannot be reinitialized")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "projection", projection)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("_RetainedObjectView has read-only bindings")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("_RetainedObjectView has read-only bindings")

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        actual = other
        return (self.source, self.projection) == (actual.source, actual.projection)

    def __len__(self) -> int:
        return len(self.source)

    def __iter__(self) -> Iterator[object]:
        yield from self.source

    def __getitem__(self, key: object) -> object:
        if not isinstance(key, str):
            raise AuditIntegrityError("retention object has a non-string key")
        return self.projection.value(self.source[key])


class _RetainedArrayView(ReadonlyJsonArray):
    """Read-only bindings over the original live sequence and projection owner."""

    __slots__ = ("_initialized", "projection", "source")

    source: Sequence[object]
    projection: _ModelProjection
    _initialized: bool

    def __new__(cls, source: Sequence[object], projection: _ModelProjection) -> Self:
        instance = object.__new__(cls)
        object.__setattr__(instance, "_initialized", False)
        return instance

    def __init__(self, source: Sequence[object], projection: _ModelProjection) -> None:
        if self._initialized:
            raise AttributeError("_RetainedArrayView cannot be reinitialized")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "projection", projection)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("_RetainedArrayView has read-only bindings")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("_RetainedArrayView has read-only bindings")

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        actual = other
        return (self.source, self.projection) == (actual.source, actual.projection)

    def __len__(self) -> int:
        return len(self.source)

    def __iter__(self) -> Iterator[object]:
        for item in self.source:
            yield self.projection.value(item)


def retained_model_json_view(model: RetainedModel) -> object:
    """Borrow the current continuation model's complete mode=json field set.

    These owned continuation models use default field serialization. The actual
    model encoder remains authoritative and must bind its resulting size.
    """
    return _ModelProjection().value(model)


def validate_retained_model(model: RetainedModel) -> None:
    """Revalidate every owned model without serializing the complete envelope.

    Reconstructing a shallow field mapping runs scalar and model validators;
    visiting nested models separately prevents unchecked model_copy updates
    from becoming size refusals. Existing string sources keep their decoders.
    """
    active: set[int] = set()

    def visit(value: object) -> None:
        if not isinstance(value, (ContinuationValue, AdvisorAbandonmentResponse, Mapping, tuple, list)):
            return
        identity = id(value)
        if identity in active:
            raise AuditIntegrityError("retained model contains a circular value")
        active.add(identity)
        try:
            if isinstance(value, (ContinuationValue, AdvisorAbandonmentResponse)):
                fields = type(value).model_fields
                data = {(info.validation_alias or name): value.__dict__[name] for name, info in fields.items()}
                type(value).model_validate(data)
                for child in value.__dict__.values():
                    visit(child)
            elif isinstance(value, Mapping):
                for child in value.values():
                    visit(child)
            else:
                for child in value:
                    visit(child)
        finally:
            active.remove(identity)

    try:
        visit(model)
    except (ValidationError, ValueError, TypeError, KeyError) as error:
        raise AuditIntegrityError("retention preflight found invalid complete owned model data") from error


def require_retention_message_credentials(message: Mapping[str, object], *, env_ref_names: frozenset[str]) -> None:
    """Keep complete native scanning separate from generic control traversal."""
    from elspeth.web.composer.provider_thinking import scan_native_thinking_blocks
    from elspeth.web.credential_guard import require_no_credential_material

    excluded = PROMPT_GUARD_PROVENANCE_KEYS
    if "thinking_blocks" in message:
        if message["role"] != "assistant":
            raise AuditIntegrityError("Provider thinking belongs to an assistant message")
        scan_native_thinking_blocks(message["thinking_blocks"])
        excluded = excluded | {"thinking_blocks"}
    require_no_credential_material(
        {key: value for key, value in message.items() if key not in excluded},
        surface="composer_continuation_private_chunk",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )

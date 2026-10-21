"""Complete graph-value identities; these never grant inspection permission.

Values remain in their original composition. A manifest describes complete
values in one session's exact authored graph, without storing provider-native
history, credentials, detector verdicts, or reusable dispatch permissions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypedDict
from uuid import UUID

from pydantic import ConfigDict, JsonValue, TypeAdapter, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import FrozenJsonArray
from elspeth.contracts.hashing import is_lower_sha256_hex

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue, config=ConfigDict(strict=True, allow_inf_nan=False))


class CompleteValueReferencePayload(TypedDict):
    reference_id: str
    path: list[str | int]
    content_hash: str
    utf8_bytes: int


class CompleteValueManifestPayload(TypedDict):
    schema_version: Literal[1]
    session_id: str
    composition_content_hash: str
    values: list[CompleteValueReferencePayload]


def _canonical_value(value: object, *, sort_keys: bool = False) -> str:
    """Encode compact graph JSON while retaining authored member order."""
    try:
        admitted = _JSON_VALUE.validate_python(value, strict=True)
        encoded = json.dumps(admitted, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")
    except (ValidationError, TypeError, ValueError):
        raise AuditIntegrityError("Complete composition value is not finite UTF-8 JSON") from None
    return encoded


def _payload_object(value: object, keys: frozenset[str]) -> dict[str, JsonValue]:
    try:
        admitted = _JSON_VALUE.validate_python(value, strict=True)
    except ValidationError:
        raise AuditIntegrityError("Complete-value metadata is not strict JSON") from None
    if type(admitted) is not dict or set(admitted) != keys:
        raise AuditIntegrityError("Complete-value metadata has an unsupported shape")
    return admitted


def _require_hash(value: object) -> None:
    if type(value) is not str or not is_lower_sha256_hex(value):
        raise AuditIntegrityError("Complete-value identity lacks an exact content hash")


@dataclass(frozen=True, slots=True)
class ComposerContentScope:
    """Identity of authored content, separate from live operation custody.

    The hash must come from ``composition_content_hash``. That authority binds
    authored source/branch order and deliberately excludes checkpoint version.
    Revert to identical content can restore identity, never an old permission.
    """

    session_id: UUID
    composition_content_hash: str

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID:
            raise AuditIntegrityError("Complete-value scope requires an owned session identity")
        _require_hash(self.composition_content_hash)


@dataclass(frozen=True, slots=True)
class CompleteValueReference:
    """One complete JSON value at an exact path in the scoped composition."""

    scope: ComposerContentScope
    path: tuple[str | int, ...]
    content_hash: str
    utf8_bytes: int

    def __post_init__(self) -> None:
        if type(self.scope) is not ComposerContentScope or type(self.path) is not tuple:
            raise AuditIntegrityError("Complete-value reference lacks owned immutable scope/path")
        for part in self.path:
            if type(part) is not str and (type(part) is not int or part < 0):
                raise AuditIntegrityError("Complete-value path is outside the JSON path vocabulary")
            if type(part) is str:
                try:
                    part.encode("utf-8")
                except UnicodeEncodeError:
                    raise AuditIntegrityError("Complete-value path is not UTF-8") from None
        _require_hash(self.content_hash)
        if type(self.utf8_bytes) is not int or self.utf8_bytes <= 0:
            raise AuditIntegrityError("Complete-value reference requires an exact positive byte size")

    @property
    def reference_id(self) -> str:
        identity = {
            "schema_version": 1,
            "session_id": str(self.scope.session_id),
            "composition_content_hash": self.scope.composition_content_hash,
            "path": list(self.path),
            "content_hash": self.content_hash,
            "utf8_bytes": self.utf8_bytes,
        }
        return hashlib.sha256(_canonical_value(identity, sort_keys=True).encode("utf-8")).hexdigest()

    def to_payload(self) -> CompleteValueReferencePayload:
        return {
            "reference_id": self.reference_id,
            "path": list(self.path),
            "content_hash": self.content_hash,
            "utf8_bytes": self.utf8_bytes,
        }

    @classmethod
    def from_payload(cls, scope: ComposerContentScope, value: object) -> CompleteValueReference:
        payload = _payload_object(value, frozenset({"reference_id", "path", "content_hash", "utf8_bytes"}))
        path, content_hash, size = payload["path"], payload["content_hash"], payload["utf8_bytes"]
        if type(path) is not list:
            raise AuditIntegrityError("Complete-value reference path is malformed")
        parts: list[str | int] = []
        for part in path:
            if type(part) is str or type(part) is int:
                parts.append(part)
            else:
                raise AuditIntegrityError("Complete-value reference path is malformed")
        if type(content_hash) is not str or type(size) is not int:
            raise AuditIntegrityError("Complete-value reference content binding is malformed")
        reference = cls(scope, tuple(parts), content_hash, size)
        if payload["reference_id"] != reference.reference_id:
            raise AuditIntegrityError("Complete-value reference identity does not match its binding")
        return reference


@dataclass(frozen=True, slots=True)
class CompleteValueManifest:
    """Closed stored metadata, with no detector result or authorization field."""

    scope: ComposerContentScope
    values: tuple[CompleteValueReference, ...]

    def __post_init__(self) -> None:
        if type(self.scope) is not ComposerContentScope or type(self.values) is not tuple:
            raise AuditIntegrityError("Complete-value manifest lacks owned immutable members")
        if any(type(value) is not CompleteValueReference or value.scope != self.scope for value in self.values):
            raise AuditIntegrityError("Complete-value manifest contains a foreign scope")
        paths = [value.path for value in self.values]
        unique_paths = set(paths)
        if len(unique_paths) != len(paths) or any(path[:length] in unique_paths for path in paths for length in range(len(path))):
            raise AuditIntegrityError("Complete-value manifest paths duplicate or overlap")

    def require_scope(self, scope: ComposerContentScope) -> None:
        """Reject stale/transplanted identity without making an access decision."""
        if type(scope) is not ComposerContentScope or self.scope != scope:
            raise AuditIntegrityError("Complete-value manifest does not match the current composition")

    def to_payload(self) -> CompleteValueManifestPayload:
        return {
            "schema_version": 1,
            "session_id": str(self.scope.session_id),
            "composition_content_hash": self.scope.composition_content_hash,
            "values": [value.to_payload() for value in self.values],
        }

    @classmethod
    def from_payload(cls, value: object) -> CompleteValueManifest:
        payload = _payload_object(value, frozenset({"schema_version", "session_id", "composition_content_hash", "values"}))
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise AuditIntegrityError("Complete-value manifest schema version is unsupported")
        session_id, state_hash, values = payload["session_id"], payload["composition_content_hash"], payload["values"]
        if type(session_id) is not str or type(state_hash) is not str or type(values) is not list:
            raise AuditIntegrityError("Complete-value manifest identity/members are malformed")
        try:
            scope = ComposerContentScope(UUID(session_id), state_hash)
        except ValueError:
            raise AuditIntegrityError("Complete-value manifest session identity is malformed") from None
        if str(scope.session_id) != session_id:
            raise AuditIntegrityError("Complete-value manifest session identity is not canonical")
        return cls(scope, tuple(CompleteValueReference.from_payload(scope, item) for item in values))


def load_content_manifest(composer_meta: object) -> CompleteValueManifest | None:
    """Preserve historical absence; malformed present metadata is corruption."""
    if composer_meta is None:
        return None
    if type(composer_meta) is not dict and type(composer_meta) is not MappingProxyType:
        raise AuditIntegrityError("Composition metadata is not an owned mapping")
    if "content_manifest" not in composer_meta:
        return None
    manifest = composer_meta["content_manifest"]
    if type(manifest) is not dict and type(manifest) is not MappingProxyType:
        raise AuditIntegrityError("Complete-value manifest is not an owned mapping")
    # Thaw only the two declared array positions. General-purpose thawing
    # normalizes unordered sets into arrays, hiding malformed owned metadata.
    payload = dict(manifest)
    if "values" in payload:
        values = payload["values"]
        if type(values) is not list and type(values) is not tuple and type(values) is not FrozenJsonArray:
            raise AuditIntegrityError("Complete-value manifest members are not an ordered array")
        members = []
        for value in values:
            if type(value) is not dict and type(value) is not MappingProxyType:
                raise AuditIntegrityError("Complete-value reference is not an owned mapping")
            member = dict(value)
            if "path" in member:
                path = member["path"]
                if type(path) is not list and type(path) is not tuple and type(path) is not FrozenJsonArray:
                    raise AuditIntegrityError("Complete-value reference path is not an ordered array")
                member["path"] = list(path)
            members.append(member)
        payload["values"] = members
    return CompleteValueManifest.from_payload(payload)


@dataclass(frozen=True, slots=True)
class MaterializedCompleteValue:
    """Complete canonical graph value, not a trusted prompt or native replay."""

    reference: CompleteValueReference
    json_text: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.reference) is not CompleteValueReference or type(self.json_text) is not str:
            raise AuditIntegrityError("Complete-value materialization lacks owned members")
        try:
            raw = self.json_text.encode("utf-8")
            decoded = _JSON_VALUE.validate_json(self.json_text, strict=True)
        except (ValidationError, UnicodeEncodeError):
            raise AuditIntegrityError("Complete-value materialization is not strict UTF-8 JSON") from None
        if _canonical_value(decoded) != self.json_text:
            raise AuditIntegrityError("Complete-value materialization is not canonical JSON")
        if len(raw) != self.reference.utf8_bytes or hashlib.sha256(raw).hexdigest() != self.reference.content_hash:
            raise AuditIntegrityError("Complete-value materialization does not match its reference")

    def to_value(self) -> JsonValue:
        """Detach the complete admitted value without granting it prompt trust."""
        return _JSON_VALUE.validate_json(self.json_text, strict=True)

    @classmethod
    def from_value(cls, scope: ComposerContentScope, path: tuple[str | int, ...], value: object) -> MaterializedCompleteValue:
        encoded = _canonical_value(value)
        raw = encoded.encode("utf-8")
        reference = CompleteValueReference(scope, path, hashlib.sha256(raw).hexdigest(), len(raw))
        return cls(reference, encoded)

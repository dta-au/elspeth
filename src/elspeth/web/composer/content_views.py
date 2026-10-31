"""Bounded displays of owned graph JSON and complete-value resolution.

Callers supply their normal credential-screened/redacted graph projection and
the authoritative session/content scope. These displays are diagnostic; they
are not set_pipeline arguments, complete review evidence, or detector permits.
Native provider exchanges must keep their separate exact replay path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import JsonValue

from elspeth.contracts.composer_content import (
    CompleteValueManifest,
    CompleteValueReference,
    ComposerContentScope,
    MaterializedCompleteValue,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.composer.bounded_json import (
    JSON_MAX_TOTAL_UTF8_BYTES,
    JsonBoundaryError,
    _validate_json_structure,
    bounded_json_loads,
)

_INLINE_CHARACTERS = 2_048
_MAX_KEY_ENCODED_CHARACTERS = 2_048


class CompositionViewUnsupported(ValueError):
    """A complete diagnostic view cannot fit the existing finite envelope."""


class CompleteValueUnavailable(ValueError):
    """An identity is absent or does not resolve to the current complete value."""


def _encode(value: object) -> str:
    # Every caller below owns finite JSON or closed typed payload members.
    return json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"), allow_nan=False)


def _at_path(value: JsonValue, path: tuple[str | int, ...]) -> JsonValue:
    current = value
    for part in path:
        if type(part) is str:
            if type(current) is not dict or part not in current:
                raise CompleteValueUnavailable("Complete-value path is absent from the current graph projection")
            current = current[part]
        elif type(part) is int:
            if type(current) is not list or not 0 <= part < len(current):
                raise CompleteValueUnavailable("Complete-value path is absent from the current graph projection")
            current = current[part]
        else:
            raise CompleteValueUnavailable("Complete-value path is absent from the current graph projection")
    return current


def _marker(reference: CompleteValueReference) -> dict[str, JsonValue]:
    return {
        "complete_value_ref": reference.reference_id,
        "content_hash": reference.content_hash,
        "utf8_bytes": reference.utf8_bytes,
    }


@dataclass(frozen=True, slots=True)
class BoundedCompositionView:
    """Immutable display bytes plus exact owned locations of omitted values."""

    serialized: str = field(repr=False)
    manifest: CompleteValueManifest

    def __post_init__(self) -> None:
        if type(self.serialized) is not str or type(self.manifest) is not CompleteValueManifest:
            raise AuditIntegrityError("Bounded composition view lacks owned immutable members")
        try:
            size = len(self.serialized.encode("utf-8"))
        except UnicodeEncodeError:
            raise AuditIntegrityError("Bounded composition view is not UTF-8 JSON") from None
        if size > JSON_MAX_TOTAL_UTF8_BYTES:
            raise CompositionViewUnsupported("Bounded composition view exceeds the provider-context byte envelope")
        try:
            decoded = bounded_json_loads(self.serialized, label="Bounded composition view")
        except JsonBoundaryError:
            raise CompositionViewUnsupported(
                "Bounded composition view exceeds the provider-context byte envelope or structural budget"
            ) from None
        except ValueError:
            raise AuditIntegrityError("Bounded composition view is not UTF-8 JSON") from None
        # The owned JSON value boundary rejects malformed/duplicate/noncanonical
        # bytes. Reference-looking authored objects remain ordinary data unless
        # this independently held manifest owns their exact positions.
        admitted = MaterializedCompleteValue.from_value(self.manifest.scope, (), decoded).to_value()
        if type(admitted) is not dict or set(admitted) != {"schema_version", "data", "content_manifest"}:
            raise AuditIntegrityError("Bounded composition view has an unsupported shape")
        if admitted["schema_version"] != "composer.bounded-view.v1" or _encode(admitted) != self.serialized:
            raise AuditIntegrityError("Bounded composition view has an unsupported encoding/version")
        if CompleteValueManifest.from_payload(admitted["content_manifest"]) != self.manifest:
            raise AuditIntegrityError("Bounded composition view does not match its manifest")
        for reference in self.manifest.values:
            try:
                displayed = _at_path(admitted["data"], reference.path)
            except CompleteValueUnavailable:
                raise AuditIntegrityError("Bounded composition view omits an owned reference position") from None
            if _encode(displayed) != _encode(_marker(reference)):
                raise AuditIntegrityError("Bounded composition view does not cover its exact reference positions")


def build_bounded_composition_view(
    scope: ComposerContentScope,
    value: object,
    *,
    inline_characters: int = _INLINE_CHARACTERS,
) -> BoundedCompositionView:
    """Replace complete long scalars, preserving every structural position.

    This never cuts, pages, summarizes or marks authored data as trusted. The
    inline threshold bounds display scalars, not detector admission: final
    framing, companions, role, custody and policy still require fresh checks.
    """
    if type(inline_characters) is not int or not 0 < inline_characters <= PROMPT_GUARD_MAX_TEXT_CHARACTERS:
        raise ValueError("Composition view requires a positive supported inline threshold")
    # Establish finite depth/items before the recursive JSON adapter and
    # projector. Complete long scalars retain their own lossless value path.
    try:
        _validate_json_structure(value, label="Composition graph projection")
    except JsonBoundaryError:
        raise CompositionViewUnsupported(
            "Bounded composition view exceeds the provider-context byte envelope or structural budget"
        ) from None
    except ValueError:
        raise AuditIntegrityError("Composition graph projection is not strict finite JSON") from None
    admitted = MaterializedCompleteValue.from_value(scope, (), value).to_value()
    references: list[CompleteValueReference] = []

    def project(item: JsonValue, path: tuple[str | int, ...]) -> JsonValue:
        if type(item) is str and len(_encode(item)) > inline_characters:
            reference = MaterializedCompleteValue.from_value(scope, path, item).reference
            references.append(reference)
            return _marker(reference)
        if type(item) is dict:
            projected: dict[str, JsonValue] = {}
            for key, child in item.items():
                if len(_encode(key)) > _MAX_KEY_ENCODED_CHARACTERS:
                    raise CompositionViewUnsupported("A complete graph object key exceeds the bounded display envelope")
                projected[key] = project(child, (*path, key))
            return projected
        if type(item) is list:
            return [project(child, (*path, index)) for index, child in enumerate(item)]
        return item

    data = project(admitted, ())
    manifest = CompleteValueManifest(scope, tuple(references))
    serialized = _encode({"schema_version": "composer.bounded-view.v1", "data": data, "content_manifest": manifest.to_payload()})
    return BoundedCompositionView(serialized, manifest)


def materialize_composition_value(
    view: BoundedCompositionView,
    reference_id: str,
    *,
    current_scope: ComposerContentScope,
    current_projection: object,
) -> MaterializedCompleteValue:
    """Re-resolve exact current bytes on every use; never return old clearance.

    The caller still owns authentication/fencing and must inspect the actual
    resolved provider request before each dispatch. Even successfully resolved
    oversized values can remain unsupported by that final inspection.
    """
    if type(view) is not BoundedCompositionView:
        raise AuditIntegrityError("Complete-value resolution requires an owned bounded view")
    if type(reference_id) is not str or not is_lower_sha256_hex(reference_id):
        raise CompleteValueUnavailable("Complete-value reference has an unsupported identity")
    view.manifest.require_scope(current_scope)
    reference = next((item for item in view.manifest.values if item.reference_id == reference_id), None)
    if reference is None:
        raise CompleteValueUnavailable("Complete-value reference is not present in this composition view")
    current = MaterializedCompleteValue.from_value(current_scope, (), current_projection).to_value()
    resolved = MaterializedCompleteValue.from_value(current_scope, reference.path, _at_path(current, reference.path))
    if resolved.reference != reference:
        raise CompleteValueUnavailable("Complete-value reference does not match the current graph value")
    return resolved

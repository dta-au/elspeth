"""Pure readers and validators over guided reviewed-source option mappings.

Every guided review snapshot carries one untrusted option mapping per reviewed
component. Provider projections (chat revision context, planner context) and
the RESPOND transitions all have to answer the same questions about it — is it
bound to server-held blob storage, and what fields does its schema declare —
and each answer must be derived from ONE contract here rather than
re-implemented per projection. Three hand-maintained copies of these reads is
exactly how a projection came to report absence as fact (see
``reviewed_source_is_blob_bound``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.protocol import BLOB_REF_PATH_PREFIX

GUIDED_REVIEWED_BLOB_PATH_KEYS = ("path", "file")
GUIDED_REVIEWED_EXPLICIT_SCHEMA_MODES = ("fixed", "flexible")


def _is_blob_sentinel(value: object) -> bool:
    return type(value) is str and value.startswith(BLOB_REF_PATH_PREFIX)


@dataclass(frozen=True, slots=True)
class GuidedReviewedBlobBinding:
    """One validated reviewed-source custody claim and its path carriers."""

    blob_ref: str
    carriers: tuple[tuple[str, str], ...]
    is_sentinel: bool

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(value for _key, value in self.carriers)


def validate_guided_reviewed_blob_ref(value: object) -> str:
    """Return a canonical UUID string or fail closed without echoing it."""
    if type(value) is not str:
        raise AuditIntegrityError("guided reviewed source blob_ref must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise AuditIntegrityError("guided reviewed source blob_ref must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise AuditIntegrityError("guided reviewed source blob_ref must be a canonical UUID string")
    return value


def validate_guided_reviewed_blob_binding(options: Mapping[str, object]) -> GuidedReviewedBlobBinding | None:
    """Validate one explicit-ref or public-sentinel reviewed binding."""
    has_explicit_ref = "blob_ref" in options
    has_sentinel = any(_is_blob_sentinel(options[key]) for key in GUIDED_REVIEWED_BLOB_PATH_KEYS if key in options)
    if not has_explicit_ref and not has_sentinel:
        return None

    carriers: list[tuple[str, str]] = []
    for key in GUIDED_REVIEWED_BLOB_PATH_KEYS:
        if key not in options:
            continue
        value = options[key]
        if type(value) is not str or not value or "\x00" in value:
            raise AuditIntegrityError("guided reviewed blob source path carrier must be an exact non-empty string without NUL")
        carriers.append((key, value))
    if not carriers:
        raise AuditIntegrityError("guided reviewed blob source is missing a string path carrier")

    if has_sentinel:
        if any(not value.startswith(BLOB_REF_PATH_PREFIX) for _key, value in carriers):
            raise AuditIntegrityError("guided reviewed blob source mixes public sentinels and private paths")
        sentinel_ids = {validate_guided_reviewed_blob_ref(value.removeprefix(BLOB_REF_PATH_PREFIX)) for _key, value in carriers}
        if len(sentinel_ids) != 1:
            raise AuditIntegrityError("guided reviewed blob sentinel and blob_ref differ")
        blob_ref = next(iter(sentinel_ids))
        if has_explicit_ref and validate_guided_reviewed_blob_ref(options["blob_ref"]) != blob_ref:
            raise AuditIntegrityError("guided reviewed blob sentinel and blob_ref differ")
        return GuidedReviewedBlobBinding(blob_ref=blob_ref, carriers=tuple(carriers), is_sentinel=True)

    blob_ref = validate_guided_reviewed_blob_ref(options["blob_ref"])
    return GuidedReviewedBlobBinding(blob_ref=blob_ref, carriers=tuple(carriers), is_sentinel=False)


def validate_guided_reviewed_sentinel_source_mapping(
    binding: GuidedReviewedBlobBinding,
    *,
    source_name: str,
    live_source_options: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[str, str], ...]:
    """Validate the exact live source identity and carriers for a sentinel."""
    if not binding.is_sentinel:
        raise TypeError("sentinel source mapping requires a sentinel binding")
    if source_name not in live_source_options:
        raise AuditIntegrityError("guided blob source mapping is inconsistent")
    options = live_source_options[source_name]
    expected_keys = {key for key, _value in binding.carriers}
    live_keys = {key for key in GUIDED_REVIEWED_BLOB_PATH_KEYS if key in options}
    if live_keys != expected_keys:
        raise AuditIntegrityError("guided blob source mapping is inconsistent")

    live_carriers: list[tuple[str, str]] = []
    for key, _sentinel in binding.carriers:
        value = options[key]
        if type(value) is not str or not value or "\x00" in value or value.startswith(BLOB_REF_PATH_PREFIX):
            raise AuditIntegrityError("guided blob source mapping is inconsistent")
        live_carriers.append((key, value))
    if "blob_ref" in options and validate_guided_reviewed_blob_ref(options["blob_ref"]) != binding.blob_ref:
        raise AuditIntegrityError("guided blob source mapping is inconsistent")
    return tuple(live_carriers)


def validate_guided_reviewed_blob_source_mapping(
    reviewed_bindings: Sequence[tuple[str, frozenset[str]]],
    live_source_options: Mapping[str, Mapping[str, object]],
) -> None:
    """Fail closed unless every reviewed path maps uniquely to its live name."""
    live_carriers = {
        name: {value for key in GUIDED_REVIEWED_BLOB_PATH_KEYS if key in options and type(value := options[key]) is str}
        for name, options in live_source_options.items()
    }
    for reviewed_name, reviewed_paths in reviewed_bindings:
        if reviewed_name in live_carriers:
            if reviewed_paths != live_carriers[reviewed_name]:
                raise AuditIntegrityError("guided blob source mapping is inconsistent")
        elif any(reviewed_paths.intersection(paths) for paths in live_carriers.values()):
            raise AuditIntegrityError("guided blob source mapping is inconsistent")

    all_reviewed_paths = frozenset(path for _name, paths in reviewed_bindings for path in paths)
    for live_name, paths in live_carriers.items():
        live_reviewed_paths = paths.intersection(all_reviewed_paths)
        if not live_reviewed_paths:
            continue
        candidates = [
            reviewed_paths
            for reviewed_name, reviewed_paths in reviewed_bindings
            if reviewed_name == live_name and live_reviewed_paths <= reviewed_paths
        ]
        if len(candidates) != 1:
            raise AuditIntegrityError("guided blob source mapping is inconsistent")


def reviewed_source_is_blob_bound(options: Mapping[str, object]) -> bool:
    """Report whether a reviewed source reads server-held blob storage.

    Two writer families bind a reviewed source to a blob, and a projection that
    knows only one of them reports the other as unbound. Freeform / import /
    copy paths retain an explicit ``blob_ref`` custody key; every guided-native
    path instead carries the ``blob:<id>`` sentinel in a path knob (the guided
    emitters mint it, and the proposal custody boundary refuses a caller-
    supplied ``blob_ref``). Both are the same fact to a provider.

    Returns a bare boolean by design: the reference, the storage path, and the
    blob id are all withheld from provider-visible projections
    (elspeth-0762539db5), so this is the whole of what may be projected.
    """
    if "blob_ref" in options:
        return True
    for key in GUIDED_REVIEWED_BLOB_PATH_KEYS:
        if key not in options:
            continue
        value = options[key]
        if type(value) is str and value.startswith(BLOB_REF_PATH_PREFIX):
            return True
    return False


def reviewed_schema_mode(schema: object) -> str | None:
    """Return a reviewed source schema's declared mode, or ``None`` if unstated.

    ``schema`` is an untrusted authored option value, so a non-mapping schema
    or a non-string mode is honest absence rather than an error: callers
    project ``None`` rather than inventing a mode.
    """
    if not isinstance(schema, Mapping):
        return None
    mode = schema.get("mode")
    if type(mode) is not str:
        return None
    return mode


def reviewed_schema_declared_field_names(schema: object) -> tuple[str, ...]:
    """Return the ordered distinct field names an explicit schema declares.

    Explicit schemas (``mode`` ``fixed`` or ``flexible``) carry their field
    inventory under ``fields``, and those declared fields are implicitly
    guaranteed (see :class:`elspeth.contracts.schema.SchemaConfig`) — a
    projection that reads only ``guaranteed_fields`` therefore reports an
    explicit schema as having no fields at all. Observed schemas declare no
    fields here and legitimately return ``()``.

    Non-raising by contract: this feeds provider projections of untrusted
    authored options, so a malformed member is dropped rather than raised on.
    That is the one difference from ``source_inspection._declared_field_name``,
    which asserts against the same authoring shapes at the inspection
    boundary. Name extraction mirrors ``_normalize_field_spec``'s precedence
    exactly — an explicit ``name``/``type`` or ``name``/``field_type`` spec
    first, then the single-key YAML form — so a spec keyed literally ``name``
    resolves to the same field the config loader would build. Only the name is
    extracted: a raw ``"field: type"`` spec would match neither the observed
    columns nor the alias registry.
    """
    if reviewed_schema_mode(schema) not in GUIDED_REVIEWED_EXPLICIT_SCHEMA_MODES:
        return ()
    if not isinstance(schema, Mapping):  # pragma: no cover - a mode implies a mapping
        return ()
    fields = schema.get("fields")
    if not isinstance(fields, Sequence) or type(fields) is str:
        return ()
    names: list[str] = []
    for spec in fields:
        name = _declared_field_spec_name(spec)
        if name is not None and name not in names:
            names.append(name)
    return tuple(names)


def _declared_field_spec_name(spec: object) -> str | None:
    """Extract one declared field's name, or ``None`` for a malformed spec."""
    if type(spec) is str:
        return spec.split(":", 1)[0].strip() or None
    if not isinstance(spec, Mapping):
        return None
    if "name" in spec and ("type" in spec or "field_type" in spec):
        name = spec["name"]
        if type(name) is not str:
            return None
        return name.strip() or None
    if len(spec) == 1:
        key = next(iter(spec))
        if type(key) is not str:
            return None
        return key.strip() or None
    return None

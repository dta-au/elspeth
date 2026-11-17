"""Versioned evidence for a file-backed LLM prompt review.

The caller supplies a custody-authorized reader.  This module is the single
derivation of the text shown on the card and the bytes admitted to execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from elspeth.contracts.blobs import ALLOWED_MIME_TYPES, BlobRecord
from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.interpretation_state import approved_prompt_artifact_hash_from_options

PROMPT_REVIEW_DOMAIN = "elspeth.file-prompt-review.v1"
PROMPT_FILE_CHANGED_MESSAGE = "This prompt file changed since it was reviewed; review it again."


class PromptFileReviewStale(ValueError):
    """Current prompt bytes or binding differ from the reviewed evidence."""

    error_code = "prompt_file_review_stale"

    def __init__(self) -> None:
        super().__init__(PROMPT_FILE_CHANGED_MESSAGE)


@dataclass(frozen=True, slots=True)
class PromptFileBinding:
    field_path: str
    blob_id: UUID
    content_sha256: str
    encoding: str
    byte_length: int
    filename: str
    creation_modality: str
    created_by: str
    provenance: Mapping[str, str | None]
    text: str

    def anchor_payload(self) -> dict[str, object]:
        return {
            "field_path": self.field_path,
            "blob_id": str(self.blob_id),
            "content_sha256": self.content_sha256,
            "encoding": self.encoding,
            "byte_length": self.byte_length,
            "filename": self.filename,
            "creation_modality": self.creation_modality,
            "created_by": self.created_by,
            "provenance": dict(self.provenance),
        }

    def payload(self) -> dict[str, object]:
        return {**self.anchor_payload(), "text": self.text}


@dataclass(frozen=True, slots=True)
class PromptReviewSnapshot:
    session_id: UUID
    node_id: str
    authored_surface: Mapping[str, Any]
    bindings: tuple[PromptFileBinding, ...]
    anchor: str
    effective_artifact_hash: str | None
    version: int = 1

    def payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": str(self.session_id),
            "node_id": self.node_id,
            "authored_surface": dict(self.authored_surface),
            "bindings": [binding.payload() for binding in self.bindings],
            "anchor": self.anchor,
            "effective_artifact_hash": self.effective_artifact_hash,
        }


def verify_prompt_review_snapshot_payload(payload: object) -> PromptReviewSnapshot:
    """Parse a persisted snapshot and prove its text, anchor, and artifact."""
    if type(payload) is not dict:
        raise AuditIntegrityError("prompt review snapshot is not an object")
    value = payload
    try:
        if value["version"] != 1 or type(value["authored_surface"]) is not dict or type(value["bindings"]) is not list:
            raise ValueError("unsupported prompt review snapshot shape")
        bindings: list[PromptFileBinding] = []
        for raw in value["bindings"]:
            if type(raw) is not dict or type(raw["text"]) is not str or type(raw["provenance"]) is not dict:
                raise ValueError("invalid prompt review binding")
            text_bytes = raw["text"].encode("utf-8")
            if (
                raw["encoding"] != "utf-8"
                or raw["byte_length"] != len(text_bytes)
                or raw["content_sha256"] != sha256(text_bytes).hexdigest()
            ):
                raise ValueError("prompt review binding text disagrees with hash")
            bindings.append(
                PromptFileBinding(
                    field_path=raw["field_path"],
                    blob_id=UUID(raw["blob_id"]),
                    content_sha256=raw["content_sha256"],
                    encoding=raw["encoding"],
                    byte_length=raw["byte_length"],
                    filename=raw["filename"],
                    creation_modality=raw["creation_modality"],
                    created_by=raw["created_by"],
                    provenance=raw["provenance"],
                    text=raw["text"],
                )
            )
        if (
            not bindings
            or len({binding.field_path for binding in bindings}) != len(bindings)
            or tuple(sorted(binding.field_path for binding in bindings)) != tuple(binding.field_path for binding in bindings)
        ):
            raise ValueError("prompt review bindings are absent or unordered")
        anchor = stable_hash(
            {
                "domain": PROMPT_REVIEW_DOMAIN,
                "node_id": value["node_id"],
                "authored_surface": value["authored_surface"],
                "bindings": [binding.anchor_payload() for binding in bindings],
            }
        )
        if anchor != value["anchor"]:
            raise ValueError("prompt review anchor drifted")
        resolved_surface = deep_thaw(value["authored_surface"])
        marker_paths = {path for path, slot in _surface_slots(value["authored_surface"]) if is_widened_blob_ref(slot) is not None}
        if marker_paths != {binding.field_path for binding in bindings}:
            raise ValueError("prompt review binding set does not match authored markers")
        for binding in bindings:
            slot = next(slot for path, slot in _surface_slots(value["authored_surface"]) if path == binding.field_path)
            marker = is_widened_blob_ref(slot)
            if (
                marker is None
                or marker.mode != "inline_content"
                or marker.blob_id != binding.blob_id
                or marker.sha256 != binding.content_sha256
            ):
                raise ValueError("prompt review binding disagrees with authored marker")
            _replace_surface_slot(resolved_surface, binding.field_path, binding.text)
        if approved_prompt_artifact_hash_from_options(resolved_surface) != value["effective_artifact_hash"]:
            raise ValueError("prompt review effective artifact hash drifted")
        snapshot = PromptReviewSnapshot(
            session_id=UUID(value["session_id"]),
            node_id=value["node_id"],
            authored_surface=value["authored_surface"],
            bindings=tuple(bindings),
            anchor=anchor,
            effective_artifact_hash=value["effective_artifact_hash"],
        )
        if snapshot.payload() != value:
            raise ValueError("prompt review snapshot has unknown fields")
        return snapshot
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditIntegrityError("prompt review snapshot failed integrity verification") from exc


def _surface_slots(options: Mapping[str, Any]) -> list[tuple[str, object]]:
    slots: list[tuple[str, object]] = []
    for key in ("prompt_template", "system_prompt"):
        if key in options:
            slots.append((key, options[key]))
    queries = options["queries"] if "queries" in options else None
    if isinstance(queries, Mapping):
        for name, entry in queries.items():
            if type(name) is str and isinstance(entry, Mapping) and "template" in entry:
                slots.append((f"queries.{name}.template", entry["template"]))
    elif isinstance(queries, (list, tuple)):
        for index, entry in enumerate(queries):
            if isinstance(entry, Mapping) and "template" in entry:
                slots.append((f"queries.{index}.template", entry["template"]))
    return slots


def _replace_surface_slot(options: MutableMapping[str, Any], path: str, text: str) -> None:
    if path in ("prompt_template", "system_prompt"):
        options[path] = text
        return
    _, selector, _ = path.split(".", 2)
    queries = options["queries"]
    queries[int(selector) if isinstance(queries, list) else selector]["template"] = text


def prompt_file_binding_paths(options: Mapping[str, Any]) -> tuple[str, ...]:
    """Canonical scalar paths whose values are valid inline-content markers."""
    paths: list[str] = []
    for path, value in _surface_slots(options):
        marker = is_widened_blob_ref(deep_thaw(value))
        if marker is not None and marker.mode == "inline_content":
            paths.append(path)
    return tuple(sorted(paths))


def resolve_prompt_review_snapshot(
    *,
    session_id: UUID,
    node_id: str,
    options: Mapping[str, Any],
    read_blob: Callable[[UUID], tuple[BlobRecord, bytes]],
) -> PromptReviewSnapshot | None:
    """Read each referenced file once and derive the reviewed surface.

    The reader must hold this session's blob-custody authority.  A marker,
    metadata row, or physical content mismatch always refuses.
    """
    authored_surface = {key: deep_thaw(options[key]) for key in ("prompt_template", "system_prompt", "queries") if key in options}
    resolved_options = deep_thaw(dict(options))
    bindings: list[PromptFileBinding] = []
    records: dict[UUID, tuple[BlobRecord, bytes]] = {}
    for path, value in _surface_slots(options):
        marker = is_widened_blob_ref(deep_thaw(value))
        if marker is None or marker.mode != "inline_content":
            continue
        if marker.encoding != "utf-8":
            raise PromptFileReviewStale()
        if marker.blob_id not in records:
            records[marker.blob_id] = read_blob(marker.blob_id)
        record, content = records[marker.blob_id]
        digest = sha256(content).hexdigest()
        if (
            record.session_id != session_id
            or record.status != "ready"
            or record.mime_type not in ALLOWED_MIME_TYPES
            or record.content_hash != marker.sha256
            or digest != marker.sha256
            or record.size_bytes != len(content)
        ):
            raise PromptFileReviewStale()
        try:
            decoded = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PromptFileReviewStale() from exc
        binding = PromptFileBinding(
            field_path=path,
            blob_id=marker.blob_id,
            content_sha256=digest,
            encoding="utf-8",
            byte_length=len(content),
            filename=record.filename,
            creation_modality=record.creation_modality.value,
            created_by=record.created_by,
            provenance={
                "created_from_message_id": record.created_from_message_id,
                "creating_model_identifier": record.creating_model_identifier,
                "creating_model_version": record.creating_model_version,
                "creating_provider": record.creating_provider,
                "creating_composer_skill_hash": record.creating_composer_skill_hash,
                "creating_arguments_hash": record.creating_arguments_hash,
            },
            text=decoded,
        )
        bindings.append(binding)
        _replace_surface_slot(resolved_options, path, decoded)
    if not bindings:
        return None
    bindings.sort(key=lambda binding: binding.field_path)
    anchor = stable_hash(
        {
            "domain": PROMPT_REVIEW_DOMAIN,
            "node_id": node_id,
            "authored_surface": authored_surface,
            "bindings": [binding.anchor_payload() for binding in bindings],
        }
    )
    return PromptReviewSnapshot(
        session_id=session_id,
        node_id=node_id,
        authored_surface=authored_surface,
        bindings=tuple(bindings),
        anchor=anchor,
        effective_artifact_hash=approved_prompt_artifact_hash_from_options(resolved_options),
    )

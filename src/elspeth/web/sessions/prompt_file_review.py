"""Versioned evidence for an LLM prompt review, file-backed or inline.

An inline review stores its authored surface with no bindings.  For a
file-backed review the caller supplies a custody-authorized reader.  This
module is the single derivation of the text shown on the card and the bytes
admitted to execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID

from elspeth.contracts.blobs import ALLOWED_MIME_TYPES, BlobRecord
from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.hashing import stable_hash
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    InterpretationRequirement,
    PromptPart,
    _prompt_parts,
    approved_prompt_artifact_hash_from_options,
    parse_interpretation_requirements,
)

if TYPE_CHECKING:
    # protocol imports this module at runtime; the annotation alone needs it.
    from elspeth.web.sessions.protocol import CompositionStateRecord

PROMPT_REVIEW_DOMAIN = "elspeth.file-prompt-review.v1"
PROMPT_FILE_CHANGED_MESSAGE = "This prompt file changed since it was reviewed; review it again."
# The prompt text a card shows.  A structured node's surface also carries the
# frozen parts skeleton and the requirement linkage of each slot (never a slot
# value, status, draft or accepted value); the anchor covers all of it.
_TEXT_SURFACE_KEYS = ("prompt_template", "system_prompt", "queries")
_PART_KEYS = {"text": {"kind", "text"}, "interpretation_ref": {"kind", "requirement_id"}}
_REQUIREMENT_SKELETON_KEYS = {"id", "event_id", "user_term"}


class PromptFileReviewStale(ValueError):
    """Current prompt bytes or binding differ from the reviewed evidence."""

    error_code = "prompt_file_review_stale"

    def __init__(self, *, node_id: str | None = None) -> None:
        self.node_id = node_id
        super().__init__(f"Node {node_id}: {PROMPT_FILE_CHANGED_MESSAGE}" if node_id is not None else PROMPT_FILE_CHANGED_MESSAGE)


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

    def __post_init__(self) -> None:
        freeze_fields(self, "provenance")

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
            "provenance": deep_thaw(self.provenance),
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

    def __post_init__(self) -> None:
        freeze_fields(self, "authored_surface")

    def payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": str(self.session_id),
            "node_id": self.node_id,
            "authored_surface": deep_thaw(self.authored_surface),
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
        # An inline review has no bindings; the marker-set check below then
        # proves its authored surface carries no file marker either.
        if len({binding.field_path for binding in bindings}) != len(bindings) or tuple(
            sorted(binding.field_path for binding in bindings)
        ) != tuple(binding.field_path for binding in bindings):
            raise ValueError("prompt review bindings are duplicated or unordered")
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
        surface = value["authored_surface"]
        has_parts = PROMPT_TEMPLATE_PARTS_KEY in surface
        if has_parts != (INTERPRETATION_REQUIREMENTS_KEY in surface):
            raise ValueError("prompt review skeleton is half present")
        # A snapshot from before the skeleton existed has neither key and verifies.
        if has_parts:
            _verify_review_skeleton(surface[PROMPT_TEMPLATE_PARTS_KEY], surface[INTERPRETATION_REQUIREMENTS_KEY])
        resolved_surface = {key: deep_thaw(slot) for key, slot in value["authored_surface"].items() if key in _TEXT_SURFACE_KEYS}
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


def _review_skeleton(
    parts: tuple[PromptPart, ...], requirements: tuple[InterpretationRequirement, ...] | None
) -> tuple[list[dict[str, str]], list[dict[str, str | None]]]:
    """Freeze the parts skeleton and each referenced requirement's linkage.

    Copies only the part shape (text segments and slot requirement ids) and,
    per referenced requirement, its id, event id and staged ``user_term``.
    ``user_term`` is set when the requirement is staged and no resolve
    rewrites it; status, draft and accepted value are never copied.
    """
    part_rows: list[dict[str, str]] = []
    referenced: list[str] = []
    for part in parts:
        if part["kind"] == "text":
            part_rows.append({"kind": "text", "text": part["text"]})
        else:
            requirement_id = part["requirement_id"]
            part_rows.append({"kind": "interpretation_ref", "requirement_id": requirement_id})
            if requirement_id not in referenced:
                referenced.append(requirement_id)
    by_id = {requirement["id"]: requirement for requirement in requirements or ()}
    requirement_rows: list[dict[str, str | None]] = []
    for requirement_id in referenced:
        requirement = by_id[requirement_id] if requirement_id in by_id else None
        requirement_rows.append(
            {
                "id": requirement_id,
                "event_id": requirement["event_id"] if requirement is not None else None,
                "user_term": requirement["user_term"] if requirement is not None else None,
            }
        )
    return part_rows, requirement_rows


def _verify_review_skeleton(parts: object, requirements: object) -> None:
    """A persisted skeleton holds only the frozen shape: no slot value can ride in it."""
    if type(parts) is not list or type(requirements) is not list:
        raise ValueError("prompt review skeleton is not a list")
    referenced: list[str] = []
    for part in parts:
        if type(part) is not dict or part["kind"] not in _PART_KEYS or set(part) != _PART_KEYS[part["kind"]]:
            raise ValueError("prompt review part has an unexpected shape")
        slot = part["text"] if part["kind"] == "text" else part["requirement_id"]
        if type(slot) is not str:
            raise ValueError("prompt review part is not text")
        if part["kind"] == "interpretation_ref" and slot not in referenced:
            referenced.append(slot)
    for row in requirements:
        if type(row) is not dict or set(row) != _REQUIREMENT_SKELETON_KEYS or type(row["id"]) is not str:
            raise ValueError("prompt review requirement skeleton has an unexpected shape")
        if any(row[key] is not None and type(row[key]) is not str for key in ("event_id", "user_term")):
            raise ValueError("prompt review requirement linkage is not text")
    if [row["id"] for row in requirements] != referenced:
        raise ValueError("prompt review requirement skeleton does not match the slots")


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


def node_binds_prompt_file(state: CompositionStateRecord, node_id: str) -> bool:
    """Whether the node ``node_id`` in ``state`` reads any prompt slot from a file.

    A file-backed prompt review is never auto-resolved by a session's review
    opt-out: the opt-out lets ELSPETH stand in for inline text the planner
    showed, but no one has read the file's bytes until a card is approved.
    """
    node = next((item for item in state.nodes or () if item["id"] == node_id), None)
    return node is not None and bool(prompt_file_binding_paths(node["options"]))


def resolve_prompt_review_snapshot(
    *,
    session_id: UUID,
    node_id: str,
    options: Mapping[str, Any],
    read_blob: Callable[[UUID], tuple[BlobRecord, bytes]] | None,
    inline_when_unbound: bool = False,
) -> PromptReviewSnapshot | None:
    """Read each referenced file once and derive the reviewed surface.

    The reader must hold this session's blob-custody authority.  A marker,
    metadata row, or physical content mismatch always refuses, and so does a
    marker with no reader.  A surface with no file marker returns ``None``,
    unless ``inline_when_unbound``: surfacing then stores the inline surface
    with no bindings, so every prompt card has a snapshot whose anchor its
    approval must name.
    """
    authored_surface = {key: deep_thaw(options[key]) for key in _TEXT_SURFACE_KEYS if key in options}
    parts = _prompt_parts(options)
    if parts is not None:
        part_rows, requirement_rows = _review_skeleton(parts, parse_interpretation_requirements(options))
        authored_surface[PROMPT_TEMPLATE_PARTS_KEY] = part_rows
        authored_surface[INTERPRETATION_REQUIREMENTS_KEY] = requirement_rows
    resolved_options = deep_thaw(dict(options))
    bindings: list[PromptFileBinding] = []
    records: dict[UUID, tuple[BlobRecord, bytes]] = {}
    for path, value in _surface_slots(options):
        marker = is_widened_blob_ref(deep_thaw(value))
        if marker is None or marker.mode != "inline_content":
            continue
        if marker.encoding != "utf-8":
            raise PromptFileReviewStale()
        if read_blob is None:
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
    if not bindings and not inline_when_unbound:
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

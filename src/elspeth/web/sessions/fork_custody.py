"""Fork checkpoint rewriting, frozen blob plans, and child custody checks."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.blobs import BLOB_REF_PATH_PREFIX, BlobForkPlanEntry, fork_blob_id
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.sessions.models import blobs_table, chat_messages_table
from elspeth.web.sessions.protocol import CompositionValidationError, serialize_composition_validation_errors

_FORK_BLOB_PLAN_SCHEMA = "session-fork-blob-plan.v1"


def _fork_blob_plan_content(
    *,
    source_session_id: UUID,
    child_session_id: UUID,
    operation_id: str,
    entries: tuple[BlobForkPlanEntry, ...],
) -> str:
    return canonical_json(
        {
            "schema": _FORK_BLOB_PLAN_SCHEMA,
            "source_session_id": str(source_session_id),
            "child_session_id": str(child_session_id),
            "operation_id": operation_id,
            "source_blobs": [
                {
                    "source_blob_id": str(entry.source_blob_id),
                    "target_blob_id": str(entry.target_blob_id),
                    "content_hash": entry.content_hash,
                    "size_bytes": entry.size_bytes,
                }
                for entry in entries
            ],
        }
    )


def _fork_blob_plan_from_content(
    content: str,
    *,
    expected_source_session_id: UUID,
    expected_child_session_id: UUID,
    expected_operation_id: str,
) -> tuple[BlobForkPlanEntry, ...]:
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuditIntegrityError("staged fork blob plan is not valid JSON") from exc
    if type(raw) is not dict or set(raw) != {
        "schema",
        "source_session_id",
        "child_session_id",
        "operation_id",
        "source_blobs",
    }:
        raise AuditIntegrityError("staged fork blob plan has malformed keys")
    if (
        raw["schema"] != _FORK_BLOB_PLAN_SCHEMA
        or raw["source_session_id"] != str(expected_source_session_id)
        or raw["child_session_id"] != str(expected_child_session_id)
        or raw["operation_id"] != expected_operation_id
    ):
        raise AuditIntegrityError("staged fork blob plan has malformed custody binding")
    source_blobs = raw["source_blobs"]
    if type(source_blobs) is not list:
        raise AuditIntegrityError("staged fork blob plan source_blobs must be a list")
    entries: list[BlobForkPlanEntry] = []
    for item in source_blobs:
        if type(item) is not dict or set(item) != {
            "source_blob_id",
            "target_blob_id",
            "content_hash",
            "size_bytes",
        }:
            raise AuditIntegrityError("staged fork blob plan entry has malformed keys")
        try:
            raw_source_blob_id = item["source_blob_id"]
            raw_target_blob_id = item["target_blob_id"]
            if type(raw_source_blob_id) is not str or type(raw_target_blob_id) is not str:
                raise TypeError("fork blob ids must be exact strings")
            source_blob_id = UUID(raw_source_blob_id)
            target_blob_id = UUID(raw_target_blob_id)
        except (TypeError, ValueError) as exc:
            raise AuditIntegrityError("staged fork blob plan entry has malformed blob id") from exc
        if str(source_blob_id) != raw_source_blob_id or str(target_blob_id) != raw_target_blob_id:
            raise AuditIntegrityError("staged fork blob plan entry has non-canonical blob id")
        if target_blob_id != fork_blob_id(
            target_session_id=expected_child_session_id,
            source_blob_id=source_blob_id,
        ):
            raise AuditIntegrityError("staged fork blob plan entry has a non-deterministic target blob id")
        try:
            entry = BlobForkPlanEntry(
                source_blob_id=source_blob_id,
                target_blob_id=target_blob_id,
                content_hash=item["content_hash"],
                size_bytes=item["size_bytes"],
            )
        except (TypeError, ValueError) as exc:
            raise AuditIntegrityError("staged fork blob plan entry is malformed") from exc
        entries.append(entry)
    result = tuple(entries)
    if tuple(sorted(result, key=lambda entry: str(entry.source_blob_id))) != result:
        raise AuditIntegrityError("staged fork blob plan entries are not in canonical id order")
    if len({entry.source_blob_id for entry in result}) != len(result):
        raise AuditIntegrityError("staged fork blob plan repeats a source blob id")
    return result


def _fork_blob_plan_identity_from_content(content: str) -> tuple[UUID, UUID, str]:
    """Validate and return the custody identity of one retained fork-plan row."""
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuditIntegrityError("staged fork blob plan is not valid JSON") from exc
    if type(raw) is not dict or set(raw) != {
        "schema",
        "source_session_id",
        "child_session_id",
        "operation_id",
        "source_blobs",
    }:
        raise AuditIntegrityError("staged fork blob plan has malformed keys")
    raw_source_session_id = raw["source_session_id"]
    raw_child_session_id = raw["child_session_id"]
    operation_id = raw["operation_id"]
    if (
        raw["schema"] != _FORK_BLOB_PLAN_SCHEMA
        or type(raw_source_session_id) is not str
        or type(raw_child_session_id) is not str
        or type(operation_id) is not str
    ):
        raise AuditIntegrityError("staged fork blob plan has malformed custody binding")
    try:
        source_session_id = UUID(raw_source_session_id)
        child_session_id = UUID(raw_child_session_id)
    except ValueError as exc:
        raise AuditIntegrityError("staged fork blob plan has malformed custody binding") from exc
    if str(source_session_id) != raw_source_session_id or str(child_session_id) != raw_child_session_id:
        raise AuditIntegrityError("staged fork blob plan has non-canonical custody binding")
    return source_session_id, child_session_id, operation_id


def _settlement_fork_blob_plan(
    conn: Connection,
    *,
    parent_session_id: UUID,
    child_session_id: UUID,
    operation_id: str,
) -> tuple[BlobForkPlanEntry, ...]:
    candidates: list[tuple[BlobForkPlanEntry, ...]] = []
    for row in conn.execute(
        select(chat_messages_table.c.content).where(
            chat_messages_table.c.session_id == str(child_session_id),
            chat_messages_table.c.role == "audit",
            chat_messages_table.c.writer_principal == "session_fork",
        )
    ).all():
        row_source_session_id, row_child_session_id, row_operation_id = _fork_blob_plan_identity_from_content(row.content)
        retained_plan = _fork_blob_plan_from_content(
            row.content,
            expected_source_session_id=row_source_session_id,
            expected_child_session_id=row_child_session_id,
            expected_operation_id=row_operation_id,
        )
        if row_child_session_id == child_session_id and row_operation_id == operation_id:
            if row_source_session_id != parent_session_id:
                raise AuditIntegrityError("staged fork blob plan has malformed custody binding")
            candidates.append(retained_plan)
    if len(candidates) != 1:
        raise AuditIntegrityError("Fork settlement requires exactly one retained frozen blob plan")
    return candidates[0]


@trust_boundary(
    tier=3,
    source=(
        "arbitrary nested JSON-shaped values from persisted composition-state payloads and "
        "fork-plan content — composer-authored structures whose nesting no first-party "
        "contract bounds"
    ),
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns a pure boolean verdict (does any nested string reference a forbidden parent "
        "blob id); unrecognized leaf shapes are False, never raised — the caller "
        "(_verify_fork_settlement_blob_custody) enforces the custody decision"
    ),
    non_raising=True,
)
def _value_references_parent_blob(value: Any, forbidden: frozenset[str]) -> bool:
    if type(value) is str:
        return value in forbidden or (value.startswith(BLOB_REF_PATH_PREFIX) and value.removeprefix(BLOB_REF_PATH_PREFIX) in forbidden)
    if isinstance(value, Mapping):
        # Keys are custody carriers too: a mapping keyed by a parent blob id or
        # storage path names the parent exactly as a value does.
        return any(_value_references_parent_blob(key, forbidden) for key in value) or any(
            _value_references_parent_blob(item, forbidden) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_value_references_parent_blob(item, forbidden) for item in value)
    return False


def _free_text_embeds_parent_blob(value: Any, forbidden: frozenset[str]) -> bool:
    """Substring form of ``_value_references_parent_blob`` for FREE-TEXT carriers.

    ``validation_errors`` entries and ``metadata`` (pipeline name, description)
    are prose: a validator writes ``"source file not found: <path>"``, never the
    bare path, so whole-string equality can never see the parent there
    (round-two red-team sign-off S1). Blob ids are UUIDs and storage paths are
    session-scoped absolute paths, so a substring hit is unambiguous. This is
    deliberately NOT applied to the structured columns, where a key or value IS
    the reference and equality is the honest predicate. The carriers are tiny
    JSON columns, so they are thawed to exact ``dict``/``list``/``str`` first
    and walked by exact type, as the other persisted-JSON walks in this module
    do.
    """
    thawed = deep_thaw(value)
    if type(thawed) is str:
        return any(needle in thawed for needle in forbidden)
    if type(thawed) is dict:
        return any(_free_text_embeds_parent_blob(item, forbidden) for item in thawed.values())
    if type(thawed) is list:
        return any(_free_text_embeds_parent_blob(item, forbidden) for item in thawed)
    return False


# The ``composer_meta`` keys the fork rewriter (``routes/sessions.py::
# _rewrite_fork_state_blob_custody``) knows how to rebase onto the child:
# ``implicit_decisions`` is re-derived from the rewritten state. Every other key is carried forward
# verbatim by ``merge_composer_meta_updates``, so parent blob custody under it
# has no correction path and must refuse the fork BEFORE a child row exists.
# A key that gains a rewriter is added here in the same change.
FORK_REWRITTEN_COMPOSER_META_KEYS: frozenset[str] = frozenset({"implicit_decisions"})


def _refuse_unrewritable_fork_custody(
    *,
    composer_meta: Mapping[str, Any] | None,
    validation_errors: Sequence[CompositionValidationError] | None,
    metadata: Mapping[str, Any] | None,
    forbidden: frozenset[str],
) -> None:
    """Refuse a fork whose source state carries parent custody nothing can rebase.

    Runs inside ``fork_session``'s staging transaction before ``sessions`` is
    written, so the refusal leaves NO archived child behind -- unlike the
    rewrite-boundary backstop in the route, which runs after the child is
    committed and can only name the key. ``forbidden`` is every parent blob row
    (id and storage path, any status), the settlement verifier's own scope.
    Keys in ``FORK_REWRITTEN_COMPOSER_META_KEYS`` are skipped: their rewriters
    run later and the backstop judges their residue. The key itself is tested as
    well as its value (a mapping keyed by a parent blob id names the parent).
    ``validation_errors`` and ``metadata`` (name, description) are free text
    with no rewriter at all.
    """
    if not forbidden:
        return
    if composer_meta is not None:
        for meta_key, meta_value in composer_meta.items():
            if meta_key in FORK_REWRITTEN_COMPOSER_META_KEYS:
                continue
            if _value_references_parent_blob(meta_key, forbidden) or _value_references_parent_blob(meta_value, forbidden):
                raise AuditIntegrityError(
                    f"Tier 1 audit anomaly: fork source composer_meta key {meta_key!r} retains parent blob custody "
                    "the fork rewriter does not model -- teach the fork path this key before forking sessions that use it"
                )
    if validation_errors is not None and _free_text_embeds_parent_blob(
        serialize_composition_validation_errors(validation_errors), forbidden
    ):
        raise AuditIntegrityError(
            "Tier 1 audit anomaly: fork source validation_errors retains parent blob custody "
            "the fork rewriter does not model -- clear the validation errors before forking this session"
        )
    if metadata is not None and _free_text_embeds_parent_blob(metadata, forbidden):
        raise AuditIntegrityError(
            "Tier 1 audit anomaly: fork source metadata retains parent blob custody "
            "the fork rewriter does not model -- remove the blob reference from the pipeline name or description before forking"
        )


def _verify_fork_settlement_blob_custody(
    conn: Connection,
    *,
    parent_session_id: UUID,
    child_session_id: UUID,
    operation_id: str,
    state_payload: Mapping[str, Any],
) -> None:
    plan = _settlement_fork_blob_plan(
        conn,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        operation_id=operation_id,
    )
    actual_rows = conn.execute(
        select(
            blobs_table.c.id,
            blobs_table.c.status,
            blobs_table.c.content_hash,
            blobs_table.c.size_bytes,
        ).where(blobs_table.c.session_id == str(child_session_id))
    ).all()
    actual = {row.id: row for row in actual_rows}
    expected_ids = {str(entry.target_blob_id) for entry in plan}
    if set(actual) != expected_ids:
        raise AuditIntegrityError("Fork settlement child blob ids do not exactly match the frozen plan")
    for entry in plan:
        row = actual[str(entry.target_blob_id)]
        if row.status != "ready" or row.content_hash != entry.content_hash or row.size_bytes != entry.size_bytes:
            raise AuditIntegrityError("Fork settlement child blob status, hash, or size does not match the frozen plan")
    planned_parent_rows = (
        conn.execute(
            select(blobs_table.c.id, blobs_table.c.storage_path).where(
                blobs_table.c.session_id == str(parent_session_id),
                blobs_table.c.id.in_([str(entry.source_blob_id) for entry in plan]),
            )
        ).all()
        if plan
        else []
    )
    if len(planned_parent_rows) != len(plan):
        raise AuditIntegrityError("Fork settlement parent blob custody no longer matches the frozen plan")
    parent_rows = conn.execute(
        select(blobs_table.c.id, blobs_table.c.storage_path).where(blobs_table.c.session_id == str(parent_session_id))
    ).all()
    forbidden = frozenset(item for row in parent_rows for item in (row.id, row.storage_path))
    if _value_references_parent_blob(state_payload, forbidden):
        raise AuditIntegrityError("Fork settlement state retains parent blob custody")
    # The two free-text columns are prose; a parent path embedded in a sentence
    # is custody the equality walk above cannot see.
    for free_text_column in ("validation_errors", "metadata"):
        if free_text_column in state_payload and _free_text_embeds_parent_blob(state_payload[free_text_column], forbidden):
            raise AuditIntegrityError("Fork settlement state retains parent blob custody")

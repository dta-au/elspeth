"""Fork checkpoint rewriting, frozen blob plans, and child custody checks."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.blobs import BlobForkPlanEntry, fork_blob_id
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.composer.guided.protocol import BLOB_REF_PATH_PREFIX
from elspeth.web.sessions.models import blobs_table, chat_messages_table
from elspeth.web.sessions.protocol import ChatMessageRecord, CompositionValidationError, serialize_composition_validation_errors

_FORK_BLOB_PLAN_SCHEMA = "session-fork-blob-plan.v1"


def _strip_guided_profile_in_meta(
    composer_meta: Mapping[str, Any] | None,
    source_to_child_message_id: Mapping[str, str],
    source_messages_by_id: Mapping[str, ChatMessageRecord],
) -> dict[str, Any] | None:
    """Prepare one schema-10 guided checkpoint for a child session.

    A fork preserves reviewed facts and deferred intent, but proposal authority
    and parent-session chat identities cannot cross the session boundary. Parse
    through the strict schema-10 decoder, rebuild the typed checkpoint, and fail
    before the fork transaction if any message reference is outside the copied
    slice.
    """
    from elspeth.core.canonical import stable_hash as _message_content_hash
    from elspeth.web.composer.guided.errors import InvariantError
    from elspeth.web.composer.guided.profile import EMPTY_PROFILE
    from elspeth.web.composer.guided.protocol import GuidedStep, TurnType
    from elspeth.web.composer.guided.state_machine import GuidedSession

    if composer_meta is None:
        return None
    thawed: dict[str, Any] = dict(deep_thaw(composer_meta))
    guided_raw = thawed["guided_session"] if "guided_session" in thawed else None
    if guided_raw is None:
        return thawed
    if type(guided_raw) is not dict:
        raise AuditIntegrityError("fork guided metadata is not an exact schema-10 object")
    try:
        guided = GuidedSession.from_dict(guided_raw)
    except (InvariantError, KeyError, TypeError, ValueError) as exc:
        raise AuditIntegrityError("fork guided schema-10 authority is malformed") from exc

    unanswered_indices = [index for index, record in enumerate(guided.history) if record.response_hash is None]
    trailing = guided.history[-1] if guided.history else None
    has_exact_active_occurrence = bool(
        unanswered_indices == [len(guided.history) - 1]
        and trailing is not None
        and (
            (
                guided.step is GuidedStep.STEP_3_TRANSFORMS
                and trailing.step is GuidedStep.STEP_3_TRANSFORMS
                and trailing.turn_type is TurnType.PROPOSE_PIPELINE
            )
            or (
                guided.step is GuidedStep.STEP_4_WIRE
                and trailing.step is GuidedStep.STEP_4_WIRE
                and trailing.turn_type is TurnType.CONFIRM_WIRING
            )
        )
    )
    has_orphan_authority_occurrence = any(
        guided.history[index].turn_type in {TurnType.PROPOSE_PIPELINE, TurnType.CONFIRM_WIRING} for index in unanswered_indices
    )
    if (guided.active_proposal is not None and not has_exact_active_occurrence) or (
        guided.active_proposal is None and has_orphan_authority_occurrence
    ):
        raise AuditIntegrityError("fork guided proposal reference/history coupling is malformed")

    if set(source_to_child_message_id) != set(source_messages_by_id):
        raise AuditIntegrityError("fork guided message maps have different source keysets")

    def _child_user_message(source_message_id: str, field_name: str) -> tuple[str, ChatMessageRecord]:
        if source_message_id not in source_to_child_message_id or source_message_id not in source_messages_by_id:
            raise AuditIntegrityError(f"fork guided {field_name} references a message outside copied slice")
        child_message_id = source_to_child_message_id[source_message_id]
        source_message = source_messages_by_id[source_message_id]
        if source_message.role != "user":
            raise AuditIntegrityError("fork guided planner lineage must identify user messages")
        return child_message_id, source_message

    remapped_root = (
        _child_user_message(guided.root_intent_message_id, "root_intent_message_id")[0]
        if guided.root_intent_message_id is not None
        else None
    )
    remapped_deferred_list = []
    for intent in guided.deferred_intents:
        child_message_id, source_message = _child_user_message(
            intent.originating_message_id,
            "deferred_intents.originating_message_id",
        )
        if _message_content_hash(source_message.content) != intent.message_content_hash:
            raise AuditIntegrityError("fork guided deferred intent message content hash mismatch")
        remapped_deferred_list.append(
            replace(
                intent,
                originating_message_id=child_message_id,
            )
        )
    remapped_deferred = tuple(remapped_deferred_list)
    remapped_corrections = []
    for reference in guided.correction_messages:
        child_message_id, source_message = _child_user_message(
            str(reference.message_id),
            "correction_messages.message_id",
        )
        if _message_content_hash(source_message.content) != reference.content_hash:
            raise AuditIntegrityError("fork guided correction message content hash mismatch")
        remapped_corrections.append(replace(reference, message_id=UUID(child_message_id)))
    rewinds_to_topology = guided.active_proposal is not None or guided.step in {GuidedStep.STEP_3_TRANSFORMS, GuidedStep.STEP_4_WIRE}
    reconciled_history = guided.history
    if rewinds_to_topology:
        if len(unanswered_indices) > 1 or (unanswered_indices and unanswered_indices[0] != len(guided.history) - 1):
            raise AuditIntegrityError("fork guided topology rewind has malformed unanswered history")
        if unanswered_indices:
            reconciled_history = guided.history[:-1]
    # A proposal cannot cross the session boundary. Rewind to the reviewed
    # output boundary, whose existing ``finish`` action deterministically
    # re-enters the shared planner and stages child-local proposal authority.
    # Leaving the child at Step 3 with no proposal would produce no GET turn
    # and no legal POST, permanently stranding the fork.
    forked_step = GuidedStep.STEP_2_SINK if rewinds_to_topology else guided.step
    forked_guided = replace(
        guided,
        step=forked_step,
        history=reconciled_history,
        profile=EMPTY_PROFILE,
        terminal=None if rewinds_to_topology else guided.terminal,
        transition_consumed=False if rewinds_to_topology else guided.transition_consumed,
        deferred_intents=remapped_deferred,
        correction_messages=tuple(remapped_corrections),
        active_proposal=None,
        active_edit_target=None,
        root_intent_message_id=remapped_root,
    )
    thawed["guided_session"] = forked_guided.to_dict()
    return thawed


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
        raise AuditIntegrityError("Guided fork settlement requires exactly one retained frozen blob plan")
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
# ``guided_session`` is rewritten field by field and ``implicit_decisions`` is
# re-derived from the rewritten state. Every OTHER key is carried forward
# verbatim by ``merge_composer_meta_updates``, so parent blob custody under it
# has no correction path and must refuse the fork BEFORE a child row exists.
# A key that gains a rewriter is added here in the same change.
FORK_REWRITTEN_COMPOSER_META_KEYS: frozenset[str] = frozenset({"guided_session", "implicit_decisions"})


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
        raise AuditIntegrityError("Guided fork settlement child blob ids do not exactly match the frozen plan")
    for entry in plan:
        row = actual[str(entry.target_blob_id)]
        if row.status != "ready" or row.content_hash != entry.content_hash or row.size_bytes != entry.size_bytes:
            raise AuditIntegrityError("Guided fork settlement child blob status, hash, or size does not match the frozen plan")
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
        raise AuditIntegrityError("Guided fork settlement parent blob custody no longer matches the frozen plan")
    parent_rows = conn.execute(
        select(blobs_table.c.id, blobs_table.c.storage_path).where(blobs_table.c.session_id == str(parent_session_id))
    ).all()
    forbidden = frozenset(item for row in parent_rows for item in (row.id, row.storage_path))
    if _value_references_parent_blob(state_payload, forbidden):
        raise AuditIntegrityError("Guided fork settlement state retains parent blob custody")
    # The two free-text columns are prose; a parent path embedded in a sentence
    # is custody the equality walk above cannot see.
    for free_text_column in ("validation_errors", "metadata"):
        if free_text_column in state_payload and _free_text_embeds_parent_blob(state_payload[free_text_column], forbidden):
            raise AuditIntegrityError("Guided fork settlement state retains parent blob custody")

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from elspeth.web.blobs.protocol import (
    BlobContentMissingError,
    BlobError,
    BlobForkFenceLostError,
    BlobForkWriteFence,
    BlobIntegrityError,
    BlobQuotaExceededError,
    BlobRecord,
)
from elspeth.web.composer.guided.protocol import BLOB_REF_PATH_PREFIX
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.implicit_decisions import merge_implicit_decisions_meta
from elspeth.web.composer.state import CompositionState
from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFenceLost,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import (
    GuidedForkSettlementCommand,
    GuidedOperationFailureCode,
    GuidedOperationFence,
    GuidedOperationFenceLostError,
    GuidedSessionResult,
    SessionForkParentAuthority,
    SessionGuidedOperationInProgressError,
    SessionNotFoundError,
)
from elspeth.web.sessions.routes.guided_operations import (
    GuidedOperationLease,
    guided_response_hash,
    raise_guided_operation_failure,
    reserve_or_replay_guided_operation,
)
from elspeth.web.sessions.service import _free_text_embeds_parent_blob, _value_references_parent_blob
from elspeth.web.sessions.titles import mint_default_session_title

from ._helpers import (
    UUID,
    APIRouter,
    AuditIntegrityError,
    BlobServiceProtocol,
    ComposerProgressSnapshot,
    CompositionStateData,
    CreateSessionRequest,
    Depends,
    ForkSessionRequest,
    ForkSessionResponse,
    HTTPException,
    InvalidForkTargetError,
    Query,
    Request,
    SessionResponse,
    SessionServiceProtocol,
    SQLAlchemyError,
    UpdateSessionRequest,
    UserIdentity,
    _get_composer_progress_registry,
    _get_session_compose_lock_registry,
    _log_last_resort_diagnostic,
    _session_response,
    _verify_session_ownership,
    deep_thaw,
    get_current_user,
    slog,
)

# Upper bound on consecutive fence-loss rejoin attempts in the session-fork
# settlement loop; repeated losses are pathological lease churn and terminate
# in AuditIntegrityError instead of an unbounded retry (mirrors the guided
# START loop's bound in routes/composer/guided.py).
_FORK_FENCE_REJOIN_ATTEMPTS = 5


def _copied_blob_for_inline_marker(
    marker: Mapping[str, Any],
    blob_map: dict[UUID, BlobRecord],
    *,
    composition_state_id: UUID,
    new_session_id: UUID,
    field_path: str,
) -> BlobRecord:
    old_ref = marker["blob_ref"]
    if type(old_ref) is not str:
        raise AuditIntegrityError(
            f"Tier 1 audit anomaly: composition_state {composition_state_id} "
            f"has inline_content blob_ref type {type(old_ref).__name__} at "
            f"{field_path} (expected UUID string). Fork aborted to prevent "
            f"cross-session blob reference in forked session {new_session_id}."
        )
    try:
        old_uuid = UUID(old_ref)
    except ValueError as exc:
        raise AuditIntegrityError(
            f"Tier 1 audit anomaly: composition_state {composition_state_id} "
            f"has non-UUID inline_content blob_ref {old_ref!r} at {field_path}. "
            f"Fork aborted to prevent cross-session blob reference in forked "
            f"session {new_session_id}."
        ) from exc
    if old_uuid not in blob_map:
        raise AuditIntegrityError(
            f"Tier 1 audit anomaly: composition_state {composition_state_id} "
            f"has inline_content blob_ref {old_ref!r} at {field_path}, but "
            f"the source blob was not copied into forked session {new_session_id}."
        )
    copied_blob = blob_map[old_uuid]
    marker_hash = marker["sha256"]
    if type(marker_hash) is not str:
        raise AuditIntegrityError(
            f"Tier 1 audit anomaly: composition_state {composition_state_id} "
            f"has inline_content marker at {field_path} without string sha256."
        )
    if copied_blob.content_hash != marker_hash:
        raise AuditIntegrityError(
            f"Tier 1 audit anomaly: copied blob {copied_blob.id} hash does "
            f"not match inline_content marker at {field_path} in forked "
            f"session {new_session_id}."
        )
    return copied_blob


def _rewrite_inline_content_blob_refs(
    value: Any,
    blob_map: dict[UUID, BlobRecord],
    *,
    composition_state_id: UUID,
    new_session_id: UUID,
    field_path: str,
) -> bool:
    if type(value) is dict:
        mode = value["mode"] if "mode" in value else None
        if mode == "inline_content" and "blob_ref" in value:
            copied_blob = _copied_blob_for_inline_marker(
                value,
                blob_map,
                composition_state_id=composition_state_id,
                new_session_id=new_session_id,
                field_path=field_path,
            )
            value["blob_ref"] = str(copied_blob.id)
            return True

        rewritten = False
        for key, child in value.items():
            if type(key) is str:
                rewritten = (
                    _rewrite_inline_content_blob_refs(
                        child,
                        blob_map,
                        composition_state_id=composition_state_id,
                        new_session_id=new_session_id,
                        field_path=f"{field_path}.{key}",
                    )
                    or rewritten
                )
        return rewritten

    if type(value) is list:
        rewritten = False
        for index, child in enumerate(value):
            rewritten = (
                _rewrite_inline_content_blob_refs(
                    child,
                    blob_map,
                    composition_state_id=composition_state_id,
                    new_session_id=new_session_id,
                    field_path=f"{field_path}[{index}]",
                )
                or rewritten
            )
        return rewritten

    return False


def _rewrite_source_blob_options(
    options: object,
    blob_map: dict[UUID, BlobRecord],
    source_blob_path_map: dict[str, BlobRecord],
    *,
    field_path: str,
) -> tuple[dict[str, Any], bool]:
    """Strictly rebuild one source options object with parent blob custody rebased.

    Top-level id carriers (``blob_ref``, ``blob_id``, ``*_blob_id``) and path
    carriers (``path``, ``file``) are rewritten by name and re-bind the source's
    blob; the WHOLE options tree -- inline samples included -- is then walked by
    ``_rebase_known_parent_refs`` so a nested parent reference is rebased too.
    """
    if type(options) is not dict:
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path} must be an exact dict")
    rebuilt = deep_thaw(options)
    if type(rebuilt) is not dict:  # pragma: no cover - deep_thaw contract
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path} thaw did not produce a dict")
    targets: dict[UUID, BlobRecord] = {}
    id_option_keys = tuple(
        key
        for key, value in rebuilt.items()
        if value is not None and type(key) is str and (key in {"blob_ref", "blob_id"} or key.endswith("_blob_id"))
    )
    for key in id_option_keys:
        old_ref = rebuilt[key]
        if type(old_ref) is not str:
            raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path}.{key} must be a UUID string")
        try:
            old_blob_id = UUID(old_ref)
        except ValueError as exc:
            raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path}.{key} is not a UUID string") from exc
        try:
            copied = blob_map[old_blob_id]
        except KeyError:
            raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path}.{key} was absent from the frozen fork plan") from None
        targets[old_blob_id] = copied
        rebuilt[key] = str(copied.id)
    for carrier in ("path", "file"):
        if carrier not in rebuilt:
            continue
        value = rebuilt[carrier]
        if value is None:
            continue
        if type(value) is not str:
            raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path}.{carrier} must be a string")
        if value.startswith(BLOB_REF_PATH_PREFIX):
            try:
                old_blob_id = UUID(value.removeprefix(BLOB_REF_PATH_PREFIX))
            except ValueError as exc:
                raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path}.{carrier} has malformed blob sentinel") from exc
            try:
                copied = blob_map[old_blob_id]
            except KeyError:
                raise AuditIntegrityError(
                    f"Tier 1 audit anomaly: {field_path}.{carrier} blob sentinel was absent from the frozen fork plan"
                ) from None
            targets[old_blob_id] = copied
            rebuilt[carrier] = f"{BLOB_REF_PATH_PREFIX}{copied.id}"
        elif value in source_blob_path_map:
            copied = source_blob_path_map[value]
            targets[next(source_id for source_id, record in blob_map.items() if record.id == copied.id)] = copied
            rebuilt[carrier] = copied.storage_path
    if len(targets) > 1:
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path} binds more than one source blob")
    # The carriers handled above are TOP-LEVEL only. A source whose options nest
    # their blob reference (an S3-shaped ``options.dataset.path``, say) kept the
    # parent's path and leaked custody into the child. Rebasing the whole tree
    # is a no-op for the keys already rewritten -- they now name the CHILD, which
    # matches no parent key -- so this only reaches what the enumeration missed.
    # It deliberately does not participate in ``targets``/``blob_ref`` stamping:
    # a nested carrier does not re-bind which blob the source is bound to.
    rebuilt, nested_rewritten = _rebase_known_parent_refs(rebuilt, blob_map, source_blob_path_map)
    if type(rebuilt) is not dict:  # pragma: no cover - rebase preserves container type
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path} nested rebase did not produce a dict")
    target = next(iter(targets.values()), None)
    if target is None:
        return rebuilt, nested_rewritten
    rebuilt["blob_ref"] = str(target.id)
    if ("path" in rebuilt and not str(rebuilt["path"]).startswith(BLOB_REF_PATH_PREFIX)) or (
        "path" not in rebuilt and "file" not in rebuilt
    ):
        rebuilt["path"] = target.storage_path
    if "file" in rebuilt and not str(rebuilt["file"]).startswith(BLOB_REF_PATH_PREFIX):
        rebuilt["file"] = target.storage_path
    return rebuilt, True


def _rewrite_session_owned_sink_options(
    options: object,
    *,
    data_dir: Path,
    parent_session_id: UUID,
    child_session_id: UUID,
    field_path: str,
) -> tuple[dict[str, Any], bool]:
    """Rebase managed sink targets from the parent namespace to the child."""
    if type(options) is not dict:
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path} must be an exact dict")
    rebuilt = deep_thaw(options)
    if type(rebuilt) is not dict:  # pragma: no cover - deep_thaw contract
        raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path} thaw did not produce a dict")

    base = data_dir.resolve()
    rewritten = False
    for key in ("path", "file", "persist_directory"):
        if key not in rebuilt:
            continue
        value = rebuilt[key]
        if value is None:
            continue
        if type(value) is not str:
            raise AuditIntegrityError(f"Tier 1 audit anomaly: {field_path}.{key} must be a string")
        raw = Path(value)
        resolved = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
        for namespace in ("outputs", "blobs"):
            parent_root = base / namespace / str(parent_session_id)
            if not resolved.is_relative_to(parent_root):
                continue
            rebuilt[key] = str(base / namespace / str(child_session_id) / resolved.relative_to(parent_root))
            rewritten = True
            break
    return rebuilt, rewritten


def _rebase_known_parent_refs(
    value: object,
    blob_map: dict[UUID, BlobRecord],
    source_blob_path_map: dict[str, BlobRecord],
) -> tuple[object, bool]:
    """Return a copy of ``value`` with every KNOWN parent blob reference rebased
    onto its child copy, plus whether anything changed. The input is not mutated.

    The exact inverse of ``_value_references_parent_blob``:
    every shape those walks DETECT is a shape this walk CORRECTS -- a bare parent
    blob id, a raw parent ``storage_path``, and either of them behind the
    ``blob:`` sentinel prefix -- whether it appears as a str value or as a
    mapping KEY. Keeping detection and correction on one shape is what stops
    them drifting apart (the drift that produced elspeth-f478b01787).

    Deliberately conservative: it substitutes only where the fork plan already
    proves a parent->child mapping, and never invents structure, so it is safe
    to run over a nested options tree whose keys we do not model. It is NOT
    safe to run over ``composer_meta`` keys owned by other subsystems -- those
    fail closed at the rewrite boundary instead.

    Because this walk corrects and never raises, any residue it leaves inside
    ``sources`` / ``outputs`` is caught only by the settlement verifier, after
    staging. That is acceptable today ONLY because a source can bind a parent
    blob solely while that blob is ``ready`` (every binding tool in
    ``composer/tools/sources.py`` and ``composer/tools/blobs.py`` checks
    ``status == "ready"``) and ``ready`` is terminal (every status UPDATE in
    ``web/blobs/service.py`` requires the row to be ``pending`` first), so every
    blob a source can name is in the ``status == "ready"`` fork plan and hence
    in ``blob_map``. A change that re-quarantines a ready blob would silently
    reopen the after-staging failure class for sources and outputs.
    """
    if type(value) is str:
        for parent_id, copied in blob_map.items():
            if value == str(parent_id):
                return str(copied.id), True
            if value == f"{BLOB_REF_PATH_PREFIX}{parent_id}":
                return f"{BLOB_REF_PATH_PREFIX}{copied.id}", True
        if value in source_blob_path_map:
            return source_blob_path_map[value].storage_path, True
        if value.startswith(BLOB_REF_PATH_PREFIX) and value.removeprefix(BLOB_REF_PATH_PREFIX) in source_blob_path_map:
            return f"{BLOB_REF_PATH_PREFIX}{source_blob_path_map[value.removeprefix(BLOB_REF_PATH_PREFIX)].storage_path}", True
        return value, False
    if type(value) is dict:
        rebuilt_map: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            # Keys are custody carriers too: a str key is rebased by the same
            # predicate as a str value (any other key type falls through unchanged).
            rebuilt_key, key_changed = _rebase_known_parent_refs(key, blob_map, source_blob_path_map)
            rebuilt_map[rebuilt_key], item_changed = _rebase_known_parent_refs(item, blob_map, source_blob_path_map)
            changed = changed or key_changed or item_changed
        return rebuilt_map, changed
    if type(value) is list:
        rebuilt_list: list[Any] = []
        changed = False
        for item in value:
            rebuilt_item, item_changed = _rebase_known_parent_refs(item, blob_map, source_blob_path_map)
            rebuilt_list.append(rebuilt_item)
            changed = changed or item_changed
        return rebuilt_list, changed
    return value, False


def _rewrite_guided_blob_custody(
    composer_meta: Mapping[str, Any] | None,
    blob_map: dict[UUID, BlobRecord],
    source_blob_path_map: dict[str, BlobRecord],
    *,
    data_dir: Path,
    parent_session_id: UUID,
    child_session_id: UUID,
) -> tuple[dict[str, Any] | None, bool]:
    if composer_meta is None:
        return None, False
    if "guided_session" not in composer_meta:
        return dict(composer_meta), False
    guided_raw = composer_meta["guided_session"]
    if type(guided_raw) is not dict:
        raise AuditIntegrityError("Tier 1 audit anomaly: composer_meta.guided_session must be an exact dict")
    # Parse first and again after reconstruction: reviewed and pending sources
    # are schema-owned objects, not arbitrary JSON dictionaries.
    guided = GuidedSession.from_dict(guided_raw)
    rebuilt = guided.to_dict()
    rewritten = False
    for stable_id, reviewed in rebuilt["reviewed_sources"].items():
        reviewed["options"], changed = _rewrite_source_blob_options(
            reviewed["options"],
            blob_map,
            source_blob_path_map,
            field_path=f"guided_session.reviewed_sources[{stable_id!r}].options",
        )
        rewritten = rewritten or changed
    for stable_id, pending in rebuilt["pending_source_intents"].items():
        if pending["options"] is not None:
            pending["options"], changed = _rewrite_source_blob_options(
                pending["options"],
                blob_map,
                source_blob_path_map,
                field_path=f"guided_session.pending_source_intents[{stable_id!r}].options",
            )
            rewritten = rewritten or changed
        inspection = pending["inspection_facts"]
        if inspection is not None:
            identity = inspection["redacted_identity"]
            # ``_redacted_identity`` writes ``blob_id`` only for blob-backed
            # inspections, so absence is a real state; presence is a UUID
            # string by construction. Membership form mirrors the same read in
            # ``composer/guided/stage_transitions.py::_inspection_blob_id``.
            if "blob_id" in identity:
                old_ref = identity["blob_id"]
                try:
                    old_blob_id = UUID(old_ref)
                except (TypeError, ValueError) as exc:
                    raise AuditIntegrityError("Tier 1 audit anomaly: pending source inspection blob_id is not a UUID string") from exc
                try:
                    copied = blob_map[old_blob_id]
                except KeyError:
                    raise AuditIntegrityError(
                        "Tier 1 audit anomaly: pending source inspection blob_id was absent from the frozen fork plan"
                    ) from None
                identity["blob_id"] = str(copied.id)
                rewritten = True
    for stable_id, reviewed in rebuilt["reviewed_outputs"].items():
        reviewed["options"], changed = _rewrite_session_owned_sink_options(
            reviewed["options"],
            data_dir=data_dir,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            field_path=f"guided_session.reviewed_outputs[{stable_id!r}].options",
        )
        rewritten = rewritten or changed
    for stable_id, pending in rebuilt["pending_output_intents"].items():
        if pending["options"] is not None:
            pending["options"], changed = _rewrite_session_owned_sink_options(
                pending["options"],
                data_dir=data_dir,
                parent_session_id=parent_session_id,
                child_session_id=child_session_id,
                field_path=f"guided_session.pending_output_intents[{stable_id!r}].options",
            )
            rewritten = rewritten or changed
    rebuilt = GuidedSession.from_dict(rebuilt).to_dict()
    source_ids = frozenset(str(blob_id) for blob_id in blob_map)
    if _value_references_parent_blob(rebuilt, source_ids):
        raise AuditIntegrityError("Tier 1 audit anomaly: forked guided metadata retained a parent blob id")
    result = dict(composer_meta)
    result["guided_session"] = rebuilt
    return result, rewritten


def _rewrite_fork_state_blob_custody(
    state: Any,
    blob_map: dict[UUID, BlobRecord],
    source_blob_path_map: dict[str, BlobRecord],
    *,
    parent_blob_refs: frozenset[str],
    data_dir: Path,
    parent_session_id: UUID,
    child_session_id: UUID,
) -> CompositionStateData | None:
    """Rebase every parent blob reference in ``state`` onto the child's copies.

    ``blob_map`` / ``source_blob_path_map`` are the frozen fork plan (only
    ``status == "ready"`` parent blobs have a child copy) and drive CORRECTION.
    ``parent_blob_refs`` is every parent blob row's id and storage_path, ANY
    status -- the settlement verifier's own scope -- and drives the fail-closed
    DETECTION backstop over ``composer_meta``, so a parent blob the plan excluded
    is still named here instead of surfacing only at settlement.
    """
    if state is None:
        return None
    sources = deep_thaw(state.sources) if state.sources is not None else None
    if sources is None and state.source is not None:
        # Mirror ``sessions/converters.py::state_from_record``: a pre-migration
        # row carries its single source in the legacy ``source`` column. Promote
        # it under the converter's key so the rewrite, the re-derivation below,
        # and the returned CompositionStateData all see the same source -- not
        # an empty source set with the legacy column silently dropped.
        sources = {"source": deep_thaw(state.source)}
    nodes = deep_thaw(state.nodes)
    edges = deep_thaw(state.edges)
    outputs = deep_thaw(state.outputs)
    metadata = deep_thaw(state.metadata_)
    composer_meta = deep_thaw(state.composer_meta) if state.composer_meta is not None else None
    rewritten = False
    # ``options`` presence is asserted rather than defaulted past: every
    # persisted source and output dict is written from
    # ``CompositionState.to_dict``, which emits ``"options"`` unconditionally
    # for both (``description`` is the field it omits when absent). A row
    # missing the key is a Tier-1 audit anomaly on exactly the field this
    # function rewrites, so it must abort the fork with a named error rather
    # than be skipped and produce a child whose blob custody was never rebased.
    if sources is not None:
        if type(sources) is not dict:
            raise AuditIntegrityError("Tier 1 audit anomaly: forked composition sources must be an exact dict")
        for source_name, source in sources.items():
            if type(source) is not dict:
                raise AuditIntegrityError(f"Tier 1 audit anomaly: sources.{source_name} must be an exact dict")
            if "options" not in source:
                raise AuditIntegrityError(f"Tier 1 audit anomaly: sources.{source_name} carries no options field")
            if source["options"] is not None:
                source["options"], changed = _rewrite_source_blob_options(
                    source["options"],
                    blob_map,
                    source_blob_path_map,
                    field_path=f"sources.{source_name}.options",
                )
                rewritten = rewritten or changed
    if outputs is not None and type(outputs) is not list:
        raise AuditIntegrityError("Tier 1 audit anomaly: forked composition outputs must be an exact list")
    for index, output in enumerate(outputs or []):
        if type(output) is not dict:
            raise AuditIntegrityError(f"Tier 1 audit anomaly: outputs[{index}] must be an exact dict")
        if "options" not in output:
            raise AuditIntegrityError(f"Tier 1 audit anomaly: outputs[{index}] carries no options field")
        if output["options"] is not None:
            output["options"], changed = _rewrite_session_owned_sink_options(
                output["options"],
                data_dir=data_dir,
                parent_session_id=parent_session_id,
                child_session_id=child_session_id,
                field_path=f"outputs[{index}].options",
            )
            rewritten = rewritten or changed
    for field_name, value in (("sources", sources), ("nodes", nodes), ("outputs", outputs)):
        rewritten = (
            _rewrite_inline_content_blob_refs(
                value,
                blob_map,
                composition_state_id=state.id,
                new_session_id=child_session_id,
                field_path=field_name,
            )
            or rewritten
        )
    composer_meta, guided_rewritten = _rewrite_guided_blob_custody(
        composer_meta,
        blob_map,
        source_blob_path_map,
        data_dir=data_dir,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
    )
    rewritten = rewritten or guided_rewritten
    # ``implicit_decisions`` is not authored state: it is a pure PROJECTION of the
    # composition state, regenerated unconditionally on every save via
    # ``merge_implicit_decisions_meta``. A fork mints a NEW state row, so carrying
    # the parent's report onto it violates the atomicity those saves declare --
    # "the report is generated from the state that is about to be saved ... so the
    # new version and its disclosure are atomic" -- and strands parent blob ids and
    # raw parent storage paths the child must not name (elspeth-f478b01787; the
    # private-path disclosure on an ordinary 200 projection is elspeth-d178282593).
    # RE-DERIVING from the already-rewritten payload retires every stale class at
    # once, where a blob-id remap would have retired only the first.
    if composer_meta is not None and "implicit_decisions" in composer_meta:
        # Shape and Tier-1 posture both mirror ``sessions/converters.py::state_from_record``,
        # the canonical persisted-record -> CompositionState reconstruction: a row with
        # no ``metadata_`` is corruption or a migration gap there, and is corruption
        # here for the same reason. Such a row already fails every ordinary state read,
        # so refusing to fork it reports an existing defect rather than creating one --
        # and fabricating metadata to proceed would hide it.
        #
        # ``rederived_state`` is a THROWAWAY used only to compute the projection; the
        # state returned below keeps this row's own ``metadata_`` untouched.
        if metadata is None:
            raise AuditIntegrityError("Tier 1 audit anomaly: forked composition state carries no metadata to re-derive its disclosure from")
        rederived_state = CompositionState.from_dict(
            {
                "version": state.version,
                "sources": sources,
                "nodes": nodes if nodes is not None else [],
                "edges": edges if edges is not None else [],
                "outputs": outputs if outputs is not None else [],
                "metadata": metadata,
            }
        )
        composer_meta = merge_implicit_decisions_meta(composer_meta, rederived_state)
        rewritten = True
    # Fail-closed backstop over the OPEN ``composer_meta`` envelope. That envelope
    # has no schema (``Column("composer_meta", JSON)``, ``Mapping[str, Any]``) and
    # ``merge_composer_meta_updates`` is contractually REQUIRED to carry forward
    # keys owned by other subsystems -- so a field-targeted rewriter over it can
    # never be complete, while the settlement verifier walks it exhaustively. Any
    # residue therefore belongs to a key this function does not model (or to a
    # field of a modelled key its rewriter does not reach), and we must not
    # blind-rewrite another subsystem's data to silence it.
    #
    # What this buys, precisely: the route has ALREADY committed the staged child
    # (``service.fork_session``) before this function runs, so a raise here lands
    # in the same failure arm as a settlement abort and the child is retained
    # archived like any failed fork. It does NOT prevent that archived child. It
    # fails BEFORE blob settlement and NAMES the offending key in the error
    # message, which the route records as a last-resort diagnostic
    # (``session.fork_rewrite_integrity_error``) -- where a settlement abort
    # names nothing. Keys OUTSIDE ``FORK_REWRITTEN_COMPOSER_META_KEYS`` are
    # already refused inside ``fork_session`` before any child row exists
    # (``_refuse_unrewritable_fork_custody``); what reaches this backstop in
    # practice is residue in a modelled key after its rewriter ran.
    #
    # The needle set is ``parent_blob_refs`` -- every parent blob row, any
    # status -- and the predicate is the settlement verifier's own
    # ``_value_references_parent_blob``, applied to the top-level key as well
    # as its value: one definition of "references a parent blob", so the two
    # cannot drift. Correction (``_rebase_known_parent_refs``) still runs on
    # ``blob_map`` alone, because only planned blobs have a child copy to
    # rebase onto.
    if composer_meta is not None and parent_blob_refs:
        for meta_key, meta_value in composer_meta.items():
            if _value_references_parent_blob(meta_key, parent_blob_refs) or _value_references_parent_blob(meta_value, parent_blob_refs):
                raise AuditIntegrityError(
                    f"Tier 1 audit anomaly: forked composer_meta key {meta_key!r} retains parent blob custody "
                    "the fork rewriter did not rebase -- teach the fork path this key before forking sessions that use it"
                )
    # ``validation_errors`` is copied verbatim below and served on GET /state;
    # it is free text no rewriter can rebase, so parent custody in it is refused.
    if state.validation_errors and parent_blob_refs and _free_text_embeds_parent_blob(state.validation_errors, parent_blob_refs):
        raise AuditIntegrityError(
            "Tier 1 audit anomaly: forked validation_errors retains parent blob custody the fork rewriter did not rebase "
            "-- clear the validation errors before forking this session"
        )
    if not rewritten:
        return None
    return CompositionStateData(
        sources=sources,
        nodes=nodes,
        edges=edges,
        outputs=outputs,
        metadata_=metadata,
        is_valid=state.is_valid,
        validation_errors=list(state.validation_errors) if state.validation_errors else None,
        composer_meta=composer_meta,
    )


async def _close_fork_operation_leases(
    child: SessionOperationLease | None,
    parent: SessionOperationLease | None,
    primary: BaseException | None,
) -> None:
    """Reverse-close without replacing a stale retry or cancellation."""
    first_close_error: BaseException | None = None
    for lease in (child, parent):
        if lease is None or lease.closed:
            continue
        try:
            await lease.close()
        except BaseException as close_error:
            if primary is not None:
                primary.add_note(f"Fork lease reverse-close also failed with {type(close_error).__name__}.")
            elif first_close_error is None:
                first_close_error = close_error
    if first_close_error is not None:
        raise first_close_error


async def _await_fork_authority_adoption[T](
    awaitable: Awaitable[T],
) -> tuple[T, asyncio.CancelledError | None]:
    """Join stage+adoption before allowing request cancellation to unwind."""
    authority_task = asyncio.ensure_future(awaitable)
    caller_task = asyncio.current_task()
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(authority_task)
        except asyncio.CancelledError as exc:
            if authority_task.done() and authority_task.cancelled():
                if cancellation is not None:
                    cancellation.add_note("Fork staging/adoption was cancelled after request cancellation.")
                    raise cancellation from exc
                raise
            if caller_task is None or caller_task.cancelling() == 0:
                raise
            if cancellation is None:
                cancellation = exc
            if not authority_task.done():
                continue
            try:
                result = authority_task.result()
            except BaseException as failure:
                cancellation.add_note(f"Fork staging/adoption failed after request cancellation with {type(failure).__name__}.")
                raise cancellation from failure
        except Exception as failure:
            if cancellation is None:
                raise
            cancellation.add_note(f"Fork staging/adoption failed after request cancellation with {type(failure).__name__}.")
            raise cancellation from failure
        return result, cancellation


def register_session_routes(router: APIRouter) -> None:

    @router.post("", status_code=201, response_model=SessionResponse)
    async def create_session(
        body: CreateSessionRequest,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> SessionResponse:
        """Create a new session for the authenticated user."""
        service = request.app.state.session_service
        settings = request.app.state.settings
        title = body.title
        if title is None:
            # Mint the app-wide default title server-side (one convention,
            # elspeth-ef8c18a6cb). Archived sessions are included in the
            # collision set so an unarchive never resurfaces a duplicate
            # default row in the switcher. Local server time so the date in
            # the title matches the operator's wall clock, not UTC.
            existing = await service.list_sessions(
                user.user_id,
                settings.auth_provider,
                limit=200,
                offset=0,
                include_archived=True,
            )
            title = mint_default_session_title(
                datetime.now(UTC).astimezone(),
                (existing_session.title for existing_session in existing),
            )
        session = await service.create_session(
            user.user_id,
            title,
            settings.auth_provider,
        )
        return _session_response(session)

    @router.get("", response_model=list[SessionResponse])
    async def list_sessions(
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        include_archived: bool = Query(False),
    ) -> list[SessionResponse]:
        """List sessions for the authenticated user."""
        service = request.app.state.session_service
        settings = request.app.state.settings
        sessions = await service.list_sessions(
            user.user_id,
            settings.auth_provider,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )
        return [_session_response(s) for s in sessions]

    # NOTE: Registered before "/{session_id}" so FastAPI matches "_active"
    # against this exact-path route rather than attempting to parse "_active"
    # as a UUID (which would 422). The leading underscore also guarantees
    # the path can never collide with a real session id (UUIDs only contain
    # hex digits and hyphens).
    @router.get("/_active", response_model=list[ComposerProgressSnapshot])
    async def list_active_composer_requests(
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> list[ComposerProgressSnapshot]:
        """List in-flight composer requests for the authenticated user.

        Closes the operator-visibility gap captured in the source report:
        Uvicorn's access log only writes the POST line when the response
        completes, so an in-flight or client-cancelled composer request
        was previously invisible to operators unless they polled
        ``/composer-progress`` for a specific session id.

        Returns snapshots whose phase is in NON_TERMINAL_PROGRESS_PHASES
        (starting / calling_model / using_tools / validating / saving),
        ordered by ``updated_at`` ascending so the longest-running request
        is at the top — typical triage starting point. Filtered by the
        authenticated user's id against the registry's internal user
        index, so a caller cannot see other users' active sessions even
        when they share a server.

        ``cancelled``, ``failed``, ``complete``, and ``idle`` snapshots
        are intentionally excluded from this view: those requests are no
        longer in flight and the per-session ``/composer-progress`` GET
        is the right surface for inspecting a terminal outcome.
        """
        registry = _get_composer_progress_registry(request)
        snapshots = await registry.list_active(user_id=str(user.user_id))
        return list(snapshots)

    @router.get("/{session_id}", response_model=SessionResponse)
    async def get_session(
        session_id: UUID,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> SessionResponse:
        """Get a single session. IDOR-protected."""
        session = await _verify_session_ownership(session_id, user, request)
        return _session_response(session)

    @router.patch("/{session_id}", response_model=SessionResponse)
    async def update_session(
        session_id: UUID,
        body: UpdateSessionRequest,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> SessionResponse:
        """Update a session's user-visible metadata. IDOR-protected."""
        session = await _verify_session_ownership(session_id, user, request)
        service = request.app.state.session_service
        updated = await service.update_session_title(session.id, body.title)
        return _session_response(updated)

    @router.delete("/{session_id}", status_code=204)
    async def delete_session(
        session_id: UUID,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> None:
        """Archive (delete) a session and all associated data.

        Rejects deletion while a pipeline run is active — archive_session()
        would delete run rows and blob directories out from under the
        background worker, causing status update failures and data loss.
        """
        session = await _verify_session_ownership(session_id, user, request)
        service = request.app.state.session_service
        execution_service = request.app.state.execution_service
        session_key = str(session.id)
        execution_lock = execution_service.get_session_lock(session_key)

        async with execution_lock:
            active_run = await service.get_active_run(session.id)
            if active_run is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot delete session while a pipeline run is active. Cancel the run first.",
                )

            try:
                await service.archive_session(session.id)
            except SessionGuidedOperationInProgressError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot archive a session while a guided operation is in progress.",
                ) from exc
            # Archive is the durable boundary: preserve the live session's
            # ephemeral coordination state when it fails. Registry cleanup
            # retires held/waited locks only after their current users drain,
            # so deletion cannot split one session across old and new locks.
            execution_service.cleanup_session_lock(session_key)
            compose_lock_registry = _get_session_compose_lock_registry(request)
            await compose_lock_registry.cleanup_session_lock(session_key)
            progress_registry = _get_composer_progress_registry(request)
            await progress_registry.clear(session_key)

    @router.post(
        "/{session_id}/fork",
        status_code=201,
        response_model=ForkSessionResponse,
    )
    async def fork_from_message(
        session_id: UUID,
        body: ForkSessionRequest,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ForkSessionResponse:
        """Fork a session from a specific user message.

        Creates a new session inheriting history and composition state up to
        the fork point, with the edited message replacing the original.
        The original session is never mutated.
        """
        await _verify_session_ownership(session_id, user, request)
        service: SessionServiceProtocol = request.app.state.session_service
        blob_service: BlobServiceProtocol = request.app.state.blob_service

        # Give structurally invalid fork targets their stable public status
        # before reserving durable retry state. The service rechecks under the
        # parent write lock so this read is never relied on for integrity.
        parent_messages = await service.get_messages(session_id, limit=None)
        fork_target = next((message for message in parent_messages if message.id == body.from_message_id), None)
        if fork_target is None:
            raise HTTPException(status_code=404, detail=f"Message {body.from_message_id} not found")
        if fork_target.role != "user":
            raise HTTPException(status_code=422, detail=str(InvalidForkTargetError(str(fork_target.id), fork_target.role)))

        async def _replay(result: object) -> ForkSessionResponse:
            if type(result) is not GuidedSessionResult:
                raise AuditIntegrityError("Session fork replay locator has the wrong result kind")
            return ForkSessionResponse(session_id=result.session_id)

        # Bounded fence-loss rejoin (was ``while True``): each iteration either
        # returns a durable result, raises, or observes a lost fence and
        # rejoins through ``reserve_or_replay_guided_operation``. Repeated
        # losses are pathological lease churn and terminate in an explicit
        # integrity failure instead of an unbounded retry.
        for _fence_rejoin_attempt in range(_FORK_FENCE_REJOIN_ATTEMPTS):
            # Per-attempt lease state: hoisting these out of the loop would
            # leak the previous attempt's lease into the next one. The
            # reservation itself lives in the guarded block below, which is a
            # strict superset of the pre-fence form (it adds the
            # ``SessionOperationFenceLost``/``MISSING`` 404 arm and binds
            # ``parent_lease``) — reserving here as well would allocate two
            # operation rows per attempt.
            parent_lease: SessionOperationLease | None = None
            child_lease: SessionOperationLease | None = None
            staged = None
            close_primary: BaseException | None = None
            try:
                try:
                    reserved = await reserve_or_replay_guided_operation(
                        service=service,
                        session_id=session_id,
                        kind="session_fork",
                        request=body,
                        replay=_replay,
                    )
                except SessionOperationFenceLost as exc:
                    if exc.reason is FenceLossReason.MISSING:
                        raise HTTPException(status_code=404, detail="Session not found") from exc
                    raise
                except SessionNotFoundError as exc:
                    raise HTTPException(status_code=404, detail="Session not found") from exc
                if reserved is None:  # pragma: no cover - reservation is enabled
                    raise AuditIntegrityError("Session fork operation was not reserved")
                if not isinstance(reserved, GuidedOperationLease):
                    return reserved
                parent_lease = reserved.session_lease
                active_parent_lease = parent_lease

                fence = reserved.fence

                async def _stage_and_adopt_child_authority(
                    parent_context: SessionOperationContext = active_parent_lease.context,
                    guided_operation_fence: GuidedOperationFence = fence,
                ) -> None:
                    nonlocal child_lease, staged
                    staged = await service.fork_session(
                        SessionForkParentAuthority(
                            parent_context=parent_context,
                            guided_fence=guided_operation_fence,
                        ),
                        fork_message_id=body.from_message_id,
                        new_message_content=body.new_message_content,
                    )
                    child_lease = await SessionOperationLease.adopt_fork_child(
                        service.session_operation_authority,
                        staged.authority,
                        lease_seconds=service.session_operation_lease_seconds,
                    )

                _, staging_cancellation = await _await_fork_authority_adoption(_stage_and_adopt_child_authority())
                if staged is None or child_lease is None:
                    raise AuditIntegrityError("Fork staging completed without adopted child authority")
                active_child_lease = child_lease
                if staging_cancellation is not None:
                    raise staging_cancellation

                async def _checkpoint(
                    parent_operation_lease: SessionOperationLease = active_parent_lease,
                    child_operation_lease: SessionOperationLease = active_child_lease,
                ) -> None:
                    nonlocal fence
                    parent_operation_lease.raise_if_lost()
                    child_operation_lease.raise_if_lost()
                    fence = await service.renew_guided_operation(
                        fence,
                        actor="composer_route",
                        lease_seconds=300,
                        session_operation_context=parent_operation_lease.context,
                    )

                source_blobs = {
                    entry.source_blob_id: await blob_service.get_blob(
                        entry.source_blob_id,
                        session_operation_context=active_parent_lease.context,
                    )
                    for entry in staged.blob_plan
                }
                blob_map = await blob_service.copy_blobs_for_fork(
                    session_id,
                    staged.session.id,
                    staged.blob_plan,
                    BlobForkWriteFence(
                        source_session_id=session_id,
                        target_session_id=staged.session.id,
                        operation_id=fence.operation_id,
                        lease_token=fence.lease_token,
                        attempt=fence.attempt,
                    ),
                    checkpoint=_checkpoint,
                )
                source_blob_path_map = {source_blobs[source_id].storage_path: copied for source_id, copied in blob_map.items()}
                # Every parent blob row, ANY status: the settlement verifier's
                # own ``forbidden`` scope, so the rewrite-boundary backstop names
                # exactly what settlement would otherwise reject after staging.
                parent_blob_refs = frozenset(
                    ref
                    for parent_blob in await blob_service.list_blobs(session_id, limit=None)
                    for ref in (str(parent_blob.id), parent_blob.storage_path)
                )
                rewritten_state = _rewrite_fork_state_blob_custody(
                    staged.state,
                    blob_map,
                    source_blob_path_map,
                    parent_blob_refs=parent_blob_refs,
                    data_dir=Path(request.app.state.settings.data_dir),
                    parent_session_id=session_id,
                    child_session_id=staged.session.id,
                )
                response = ForkSessionResponse(session_id=staged.session.id)
                await _checkpoint()
                await service.settle_guided_fork_operation(
                    GuidedForkSettlementCommand(
                        authority=staged.authority,
                        expected_current_state_id=staged.state.id if staged.state is not None else None,
                        edited_message_id=staged.messages[-1].id,
                        rewritten_state_id=uuid4() if rewritten_state is not None else None,
                        rewritten_state=rewritten_state,
                        response_hash=guided_response_hash(response),
                        actor="composer_route",
                    )
                )
                await child_lease.close()
                child_lease = None
                await parent_lease.close()
                return response
            except (
                GuidedOperationFenceLostError,
                BlobForkFenceLostError,
                SessionOperationFenceLost,
            ) as retry_error:
                # A stale worker never cleans a child now owned by takeover.
                close_primary = retry_error
                continue
            except Exception as primary_exc:
                close_primary = primary_exc
                failure_code: GuidedOperationFailureCode = (
                    "quota_exceeded"
                    if isinstance(primary_exc, BlobQuotaExceededError)
                    else "integrity_error"
                    if isinstance(primary_exc, (AuditIntegrityError, BlobContentMissingError, BlobIntegrityError))
                    else "operation_failed"
                )
                if isinstance(primary_exc, AuditIntegrityError):
                    # The ONLY carrier of what failed is ``str(primary_exc)``:
                    # ``fail_guided_operation`` durably records a code, not a
                    # message, and ``raise_guided_operation_failure`` answers
                    # with a fixed envelope. For pre-staging custody detection
                    # (inside ``fork_session``, no child row yet) and for the
                    # rewrite-boundary backstop, that message NAMES the
                    # offending composer_meta key -- the whole point of failing
                    # there rather than at settlement -- so it gets a
                    # last-resort record; the failed operation (and, after
                    # staging, the archived child) is the audit evidence, this
                    # is the diagnostic that says which key.
                    #
                    # This record is taken BEFORE the ``staged is None`` arm
                    # below, which raises: the pre-staging case is the one that
                    # names a key no other surface can, so settling the
                    # operation first would discard exactly the diagnostic this
                    # exists for.
                    _log_last_resort_diagnostic(
                        slog.error,
                        "session.fork_rewrite_integrity_error",
                        session_id=str(session_id),
                        child_session_id=str(staged.session.id) if staged is not None else None,
                        operation_id=fence.operation_id,
                        exc_class=type(primary_exc).__name__,
                        message=str(primary_exc),
                    )

                if staged is None:
                    # Staging itself failed: the reserved row must still reach
                    # a terminal state under the parent authority, otherwise
                    # every replay joiner polls until the lease expires.
                    if parent_lease is None:
                        raise
                    try:
                        failed = await service.fail_guided_operation(
                            fence,
                            failure_code=failure_code,
                            actor="composer_route",
                            session_operation_context=parent_lease.context,
                        )
                    except (GuidedOperationFenceLostError, SessionOperationFenceLost) as failure_fence_error:
                        close_primary = failure_fence_error
                        continue
                    raise_guided_operation_failure(failed)

                cleanup_integrity_exc: AuditIntegrityError | BlobContentMissingError | BlobIntegrityError | None = None

                if staged is not None:
                    try:
                        cleanup = await blob_service.cleanup_blobs_for_fork(
                            session_id,
                            staged.session.id,
                            fence.operation_id,
                            live_write_fence=BlobForkWriteFence(
                                source_session_id=session_id,
                                target_session_id=staged.session.id,
                                operation_id=fence.operation_id,
                                lease_token=fence.lease_token,
                                attempt=fence.attempt,
                            ),
                        )
                    except BlobForkFenceLostError:
                        # A stale worker never cleans a child now owned by takeover.
                        continue
                    except (AuditIntegrityError, BlobContentMissingError, BlobIntegrityError) as integrity_exc:
                        # A Tier-1 integrity failure inside compensation
                        # outranks the primary coded failure and must reach
                        # the app-level ``AuditIntegrityError`` handler with
                        # its type intact; demoting it to a note under the
                        # generic terminal-failure envelope loses both the
                        # handler's structured record and the failed-turn
                        # metadata the client reads. The residue record still
                        # lands first, because leaked child blobs stay
                        # operator-actionable either way.
                        _log_last_resort_diagnostic(
                            slog.error,
                            "session.fork_blob_cleanup_failed",
                            session_id=str(session_id),
                            child_session_id=str(staged.session.id),
                            operation_id=fence.operation_id,
                            exc_class=type(integrity_exc).__name__,
                        )
                        cleanup_integrity_exc = integrity_exc
                        failure_code = "integrity_error"
                    except (BlobError, SQLAlchemyError, OSError) as cleanup_exc:
                        # The exception note alone is not a record: the tail
                        # below surfaces the PRIMARY failure through
                        # raise_guided_operation_failure, which raises a new
                        # HTTPException — FastAPI answers it without logging
                        # the chained context, so notes on primary_exc reach
                        # nobody. Leaked fork blobs are operator-actionable
                        # residue and get an explicit last-resort record.
                        primary_exc.add_note(
                            f"RecoveryFailed[{type(cleanup_exc).__name__}]: fork blob cleanup failed for "
                            f"child {staged.session.id} ({cleanup_exc})"
                        )
                        _log_last_resort_diagnostic(
                            slog.error,
                            "session.fork_blob_cleanup_failed",
                            session_id=str(session_id),
                            child_session_id=str(staged.session.id),
                            operation_id=fence.operation_id,
                            exc_class=type(cleanup_exc).__name__,
                        )
                    else:
                        for error in cleanup.errors:
                            primary_exc.add_note(
                                f"RecoveryFailed[{error.exc_type}]: could not delete fork blob {error.blob_id} "
                                f"from child {staged.session.id} ({error.detail})"
                            )
                            _log_last_resort_diagnostic(
                                slog.error,
                                "session.fork_blob_cleanup_failed",
                                session_id=str(session_id),
                                child_session_id=str(staged.session.id),
                                operation_id=fence.operation_id,
                                exc_class=error.exc_type,
                                blob_id=str(error.blob_id),
                            )
                    # The failed child is retained as archived audit evidence.
                    # Only its copied blobs are compensatable; deleting the
                    # session would also destroy the frozen plan envelope.

                try:
                    # A staged fork settles through the fork authority, not the
                    # bare guided fence: the parent context, the child context
                    # and the guided fence must all still be live for the CAS
                    # to win, and ``fail_guided_operation`` would settle under
                    # the guided fence alone. ``SessionOperationFenceLost`` is
                    # the second fence's loss signal and rejoins like the
                    # first: only the fail-CAS winner owns settlement.
                    failed = await service.fail_guided_fork_operation(
                        staged.authority,
                        failure_code=failure_code,
                        actor="composer_route",
                    )
                except (GuidedOperationFenceLostError, SessionOperationFenceLost) as failure_fence_error:
                    close_primary = failure_fence_error
                    continue

                if cleanup_integrity_exc is not None:
                    raise cleanup_integrity_exc from primary_exc

                raise_guided_operation_failure(failed)
            finally:
                await _close_fork_operation_leases(
                    child_lease,
                    parent_lease,
                    sys.exception() or close_primary,
                )
        raise AuditIntegrityError("Session fork lost its operation fence on every rejoin attempt without a joinable winner")

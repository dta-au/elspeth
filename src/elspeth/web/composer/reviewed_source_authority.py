"""Private verification for exact guided reviewed-source reuse."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.pipeline_proposal import reviewed_anchor_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ReviewedSourceAuthority
from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY
from elspeth.web.sessions.models import blobs_table, sessions_table

_BLOB_PATH_PREFIX = "blob:"
_BLOB_PATH_KEYS = frozenset({"path", "file"})


def _recorded_content_hash_prefix(raw_source: Mapping[str, Any], *, stable_id: str) -> str | None:
    """Parse the reviewed record's content identity anchor, if recorded.

    Guided review captures the inspected blob's ``content_hash`` prefix into
    the resolved source record; legacy records and non-blob sources carry
    ``None``.  A present-but-malformed anchor is an integrity anomaly, not
    an absent one.
    """
    value = raw_source["content_hash_prefix"] if "content_hash_prefix" in raw_source else None
    if value is None:
        return None
    if type(value) is not str or not value:
        raise AuditIntegrityError(f"reviewed_sources[{stable_id!r}].content_hash_prefix must be a non-empty string when present")
    return value


def _authoring_content_hash(options: Mapping[str, Any], *, stable_id: str) -> str | None:
    """Parse the full content hash pinned by source-authoring metadata."""
    authoring = options[SOURCE_AUTHORING_KEY] if SOURCE_AUTHORING_KEY in options else None
    if authoring is None:
        return None
    if not isinstance(authoring, Mapping):
        raise AuditIntegrityError(f"reviewed_sources[{stable_id!r}] source authoring metadata must be a mapping")
    value = authoring["content_hash"] if "content_hash" in authoring else None
    if value is None:
        return None
    if type(value) is not str or not value:
        raise AuditIntegrityError(f"reviewed_sources[{stable_id!r}] source authoring content_hash must be a non-empty string")
    return value


def _canonical_blob_id(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise AuditIntegrityError(f"{field_name} must be a canonical UUID string")
    try:
        blob_id = UUID(value)
    except ValueError as exc:
        raise AuditIntegrityError(f"{field_name} must be a canonical UUID string") from exc
    if str(blob_id) != value:
        raise AuditIntegrityError(f"{field_name} must be a canonical UUID string")
    return value


def resolve_reviewed_source_authority(
    *,
    engine: Engine | None,
    session_id: str,
    user_id: str | None,
    reviewed_facts: Mapping[str, Any],
    expected_reviewed_anchor_hash: str,
) -> ReviewedSourceAuthority | None:
    """Verify current blob custody before granting private source authority.

    Public proposal arguments remain unchanged.  Exact ``blob:<uuid>``
    sentinels are resolved only inside the candidate boundary through the
    returned private mapping.
    """

    if reviewed_anchor_hash(reviewed_facts) != expected_reviewed_anchor_hash:
        raise AuditIntegrityError("reviewed source authority does not match the current reviewed anchor")
    sources = reviewed_facts["reviewed_sources"] if "reviewed_sources" in reviewed_facts else None
    if not isinstance(sources, Mapping):
        return None
    if type(session_id) is not str or not session_id:
        raise AuditIntegrityError("reviewed source authority requires a non-empty session_id")
    if type(user_id) is not str or not user_id:
        raise AuditIntegrityError("reviewed source authority requires a non-empty user_id")

    if engine is None:
        raise AuditIntegrityError("reviewed source authority requires the session database")

    source_blob_ids: dict[str, str] = {}
    sentinel_blob_ids: dict[str, str] = {}
    raw_storage_paths: dict[str, tuple[str, ...]] = {}
    reviewed_hash_prefixes: dict[str, str] = {}
    authoring_hashes: dict[str, str] = {}
    for stable_id, raw_source in sources.items():
        if type(stable_id) is not str or not stable_id or not isinstance(raw_source, Mapping):
            raise AuditIntegrityError("reviewed source authority contains a malformed source record")
        options = raw_source["options"] if "options" in raw_source else None
        if not isinstance(options, Mapping):
            raise AuditIntegrityError("reviewed source authority contains malformed source options")
        recorded_prefix = _recorded_content_hash_prefix(raw_source, stable_id=stable_id)
        if recorded_prefix is not None:
            reviewed_hash_prefixes[stable_id] = recorded_prefix
        pinned_hash = _authoring_content_hash(options, stable_id=stable_id)
        if pinned_hash is not None:
            authoring_hashes[stable_id] = pinned_hash
        referenced_ids: set[str] = set()
        if "blob_ref" in options:
            referenced_ids.add(
                _canonical_blob_id(
                    options["blob_ref"],
                    field_name=f"reviewed_sources[{stable_id!r}].options.blob_ref",
                )
            )
        for option_name in _BLOB_PATH_KEYS:
            option_value = options[option_name] if option_name in options else None
            if type(option_value) is str and option_value.startswith(_BLOB_PATH_PREFIX):
                blob_id = _canonical_blob_id(
                    option_value.removeprefix(_BLOB_PATH_PREFIX),
                    field_name=f"reviewed_sources[{stable_id!r}].options.{option_name}",
                )
                referenced_ids.add(blob_id)
                sentinel_blob_ids[option_value] = blob_id
            elif type(option_value) is str:
                raw_storage_paths[stable_id] = (*raw_storage_paths.get(stable_id, ()), option_value)
        if len(referenced_ids) > 1:
            raise AuditIntegrityError("reviewed source blob custody fields disagree")
        if referenced_ids:
            source_blob_ids[stable_id] = next(iter(referenced_ids))

    verified_storage_by_blob_id: dict[str, str] = {}
    verified_hash_by_blob_id: dict[str, str] = {}
    with engine.connect() as conn:
        session_owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == session_id)).scalar_one_or_none()
        if session_owner != user_id:
            raise AuditIntegrityError("reviewed blob authority is not owned by the current user session")
        for blob_id in sorted(set(source_blob_ids.values())):
            row = conn.execute(
                select(
                    blobs_table.c.session_id,
                    blobs_table.c.status,
                    blobs_table.c.storage_path,
                    blobs_table.c.content_hash,
                ).where(blobs_table.c.id == blob_id)
            ).first()
            if row is None:
                raise AuditIntegrityError("reviewed blob authority references a missing blob")
            if row.session_id != session_id:
                raise AuditIntegrityError("reviewed blob authority references a blob owned by another session")
            if row.status != "ready":
                raise AuditIntegrityError("reviewed blob authority references a blob that is not ready")
            if type(row.storage_path) is not str or not row.storage_path:
                raise AuditIntegrityError("reviewed blob authority has a malformed storage path")
            if type(row.content_hash) is not str or not row.content_hash:
                raise AuditIntegrityError("reviewed blob authority references a ready blob without a content hash")
            verified_storage_by_blob_id[blob_id] = row.storage_path
            verified_hash_by_blob_id[blob_id] = row.content_hash

    # Content identity binding: authority is granted for the BYTES that were
    # reviewed, not for a blob id/path.  update_blob mutates bytes and hash
    # in place while keeping id, path, session, and ready status — every
    # location/lifecycle check above still passes — so the recorded review
    # anchors must match the live content hash or the stale reviewed facts
    # would silently auto-authorize unreviewed bytes (elspeth-b3feba9a7c).
    for stable_id, blob_id in source_blob_ids.items():
        live_hash = verified_hash_by_blob_id[blob_id]
        recorded_prefix = reviewed_hash_prefixes[stable_id] if stable_id in reviewed_hash_prefixes else None
        if recorded_prefix is not None and not live_hash.startswith(recorded_prefix):
            raise AuditIntegrityError(
                "reviewed blob authority no longer matches the reviewed content; the blob changed after review and requires re-review"
            )
        pinned_hash = authoring_hashes[stable_id] if stable_id in authoring_hashes else None
        if pinned_hash is not None and pinned_hash != live_hash:
            raise AuditIntegrityError(
                "reviewed source authoring content_hash no longer matches the live blob; the blob changed after review and requires re-review"
            )

    for stable_id, paths in raw_storage_paths.items():
        source_blob_id = source_blob_ids.get(stable_id)
        if source_blob_id is None:
            # A non-blob path is authorized only by the exact reviewed source
            # binding at the candidate boundary.  Session ownership was still
            # verified above, and the normal credential/plugin/S2 path checks
            # remain mandatory after that binding matches.
            continue
        if any(path != verified_storage_by_blob_id[source_blob_id] for path in paths):
            raise AuditIntegrityError("reviewed source raw storage path differs from its owned ready blob")

    return ReviewedSourceAuthority(
        session_id=session_id,
        reviewed_anchor_hash=expected_reviewed_anchor_hash,
        reviewed_sources=sources,
        verified_blob_paths={sentinel: verified_storage_by_blob_id[blob_id] for sentinel, blob_id in sentinel_blob_ids.items()},
    )


def resolve_owned_composition_source_authority(
    *,
    engine: Engine | None,
    session_id: str,
    user_id: str | None,
    state: CompositionState,
) -> ReviewedSourceAuthority:
    """Verify exact owned-state source bindings for private settlement.

    The synthesized reviewed-facts shape never leaves this function. It lets
    the existing candidate boundary reuse its closed, hash-matched
    ``ReviewedSourceAuthority`` contract while the proposal itself remains a
    freeform proposal with an empty public reviewed-facts anchor.
    """
    reviewed_sources = {
        source_name: {
            "name": source_name,
            "plugin": source.plugin,
            "options": deep_thaw(source.options),
            "observed_columns": [],
            "sample_rows": [],
            "on_validation_failure": source.on_validation_failure,
        }
        for source_name, source in state.sources.items()
    }
    private_facts = {"reviewed_sources": reviewed_sources}
    authority = resolve_reviewed_source_authority(
        engine=engine,
        session_id=session_id,
        user_id=user_id,
        reviewed_facts=private_facts,
        expected_reviewed_anchor_hash=reviewed_anchor_hash(private_facts),
    )
    if authority is None:
        raise AuditIntegrityError("owned composition source authority could not be established")
    return authority

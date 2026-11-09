"""``ShareableReviewService`` — business logic for the "Save for review" verb.

Responsibilities:

* Validate the composition before marking (validation + readiness gates).
* Freeze a complete snapshot of the composition at mark-time — including
  the readiness panel — into a content-addressable blob in the payload
  store.
* When the owner opts in to import, freeze every uploaded file the
  composition uses into the same content-addressed payload store and list
  them in the snapshot's private ``files`` manifest.
* Record the audit event in ``composer_completion_events_table`` BEFORE
  writing the blob (audit-first ordering).
* Mint a signed capability token that encodes the content-address.
* Resolve an inbound token to a public view derived from the authenticated
  frozen snapshot.

Audit-first ordering (load-bearing):

1. When import is allowed, read and verify every referenced file (refuse
   the whole share if any file is not shareable — nothing is stored).
2. Build snapshot dict in memory.
3. Canonical-JSON-serialize → compute ``payload_digest``.
4. Store the verified file bytes (content-addressed; the snapshot's
   ``files`` manifest names them by sha256).
5. INSERT ``composer_completion_events`` row (sync, crash-on-failure).
6. Write the snapshot blob to the payload store.
7. Sign token.
8. Return.

No token is minted unless every file is frozen. If the audit insert fails,
no snapshot blob is ever written (frozen file bytes may remain: they are
content-addressed copies of the owner's own uploads, reachable only through
a snapshot that names them). If the snapshot write fails after the audit
insert, the audit row stands as honest evidence of the attempt; no token is
returned.

Snapshot shapes: this release writes nine keys (the seven ``_BlobShape``
keys plus ``import_allowed`` and ``files``). The reader also accepts the previous
release's exact seven-key shape and treats it as view-only. Those are the
only two shapes; any other key set is refused.

Frozen-at-mark-time discipline (load-bearing):

* ``get_shareable_link`` may re-mint only when the current
  ``(session, state)`` already has a ``mark_ready_for_review`` audit row
  whose snapshot blob still exists. It does not create a new share decision.
* ``resolve_token`` verifies the token and content digest, reads the frozen
  evidence blob, then derives the current public projection in memory. It
  never re-calls ``ReadinessService.compute_snapshot``. This means:
    - Live state drift cannot change the response. Composition, YAML, and
      readiness are sanitized through the current public boundary.
    - The ``payload_digest`` authenticates the immutable mark-time evidence,
      including the readiness panel; resolve-time projection does not rewrite
      the stored blob.
    - The reviewer-vs-owner permission question for the readiness service
      never arises at resolve time.

Mark-time gate: ``mark_ready_for_review`` raises
``CompositionNotRunnableError`` if validation fails, completion readiness is
withheld, OR any readiness row has ``status == "error"``. ``status ==
"warning"`` (e.g. pending LLM interpretations) is permitted; the reviewer
sees the warning.

Layer: L3 (web application).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, TypedDict, cast
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine

from elspeth.contracts.blobs import (
    STORAGE_MIME_TYPES,
    BlobNotFoundError,
    BlobRecord,
    BlobServiceProtocol,
    BlobStateError,
)
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.canonical import canonical_json
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.audit_readiness.models import AuditReadinessSnapshot
from elspeth.web.compartments import is_compartment_id
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.telemetry_phase8 import (
    SessionsTelemetry,
    record_session_completed,
)
from elspeth.web.composer.yaml_generator import (
    generate_public_composition_dict,
    generate_public_yaml,
)
from elspeth.web.config import WebSettings
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.execution.completion_gates import CompletionGateFacts, parse_completion_gates
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import composer_completion_events_table
from elspeth.web.sessions.protocol import SessionOperationAuthority
from elspeth.web.shareable_reviews.file_manifest import (
    FileBinding,
    ShareManifestError,
    collect_file_bindings,
    file_binding_from_json,
    file_binding_to_json,
)
from elspeth.web.shareable_reviews.models import (
    CompositionStateResponse,
    MarkReadyForReviewResponse,
    ShareableLinkResponse,
    SharedInspectResponse,
)
from elspeth.web.shareable_reviews.signer import (
    InvalidToken,
    ShareTokenPayload,
    ShareTokenSigner,
)

# Path-only share URL — the frontend's `location.origin` plus this string
# resolves to a full clickable URL. Avoids adding yet another deployment-
# base-URL setting to ``WebSettings``. The ``/#/shared/`` shape matches the
# SPA hash route in ``hooks/useHashRouter.ts`` — the recipient's browser
# opens the SPA at the hash, which calls the
# ``GET /api/sessions/shared/{token}`` backend route.
_SHARE_URL_PREFIX = "/#/shared/"

# All payload-digest values on the wire carry the ``sha256:`` prefix so the
# hash algorithm is self-describing. The payload store accepts only the raw
# hex; the prefix is added/stripped at the service boundary.
_DIGEST_PREFIX = "sha256:"
_NOT_MARKED_READY_DETAIL = "current composition state has not been marked ready for review"


class CompositionNotRunnableError(Exception):
    """Raised when the composition fails the mark-for-review gates.

    Every failure mode carries the same exception type; the route layer
    maps them all to HTTP 409 and forwards ``detail`` as the user-facing
    message, so ``detail`` is written in user register.

    The ``reason`` field disambiguates for logs and tests:

    * ``"validation_failed"`` — ``ExecutionService.validate`` returned
      ``is_valid=False`` for a genuine validation failure.
    * ``"review_pending"`` — validation halted ONLY because interpretation
      review cards are pending (the soft-halt handoff shape); there are no
      errors to fix (elspeth-3830c620a2).
    * ``"completion_not_ready"`` — validation succeeded, but a completion
      gate (such as advisor sign-off) withheld ``completion_ready``.
    * ``"readiness_error_row"`` — ``ReadinessService.compute_snapshot``
      returned a row with ``status == "error"``. Sharing a known-broken
      readiness state is share-theatre; the gate refuses.
    * ``"not_marked_ready"`` — re-minting was requested before a
      successful ``mark_ready_for_review`` snapshot blob exists for the
      current state.
    * ``"readiness_state_drift"`` — validation and readiness were computed
      against different composition versions.
    * ``"completion_event_missing"`` — re-mint was requested before the
      current state had passed ``mark_ready_for_review``.
    * ``"file_not_shareable"`` — import was requested but a file the
      composition uses cannot be frozen for import (missing, not ready,
      assistant-written, or its bytes disagree with its record); ``detail``
      names the file.
    * ``"lifetime_exceeds_maximum"`` — the requested link lifetime is longer
      than the deployment's ``shareable_link_lifetime_seconds``. The route
      maps this one to HTTP 422.
    """

    def __init__(self, *, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# ── Injected dependency protocols ────────────────────────────────────────


class _SessionServiceLike(Protocol):
    """Subset of ``SessionServiceProtocol`` the service uses.

    Declared as a Protocol so tests can substitute mocks without inheriting
    the full session-service surface.
    """

    async def get_current_state(self, session_id: UUID) -> Any: ...
    async def get_session(self, session_id: UUID) -> Any: ...


class _ExecutionServiceLike(Protocol):
    async def validate(
        self,
        session_id: UUID,
        *,
        session_operation_context: SessionOperationContext,
        user_id: str | None = None,
    ) -> Any: ...
    async def validate_state(
        self,
        state: Any,
        *,
        user_id: str | None = None,
        session_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        completion_gates: CompletionGateFacts | None = None,
        fresh: bool = False,
    ) -> Any: ...


class _ReadinessServiceLike(Protocol):
    async def compute_snapshot(
        self,
        *,
        session_id: UUID,
        user_id: str,
        session_operation_context: SessionOperationContext,
        fresh: bool = False,
    ) -> AuditReadinessSnapshot: ...


# ── Helpers ──────────────────────────────────────────────────────────────


class _BlobShape(TypedDict):
    """The seven snapshot keys every readable share snapshot carries.

    This is the whole of the previous release's (view-only) snapshot and the
    common core of the current one; ``_SnapshotShape`` adds the import keys.
    """

    pipeline_metadata: Any  # CompositionObject (dict[str, JsonValue])
    composition_snapshot: Any  # CompositionObject
    yaml: str
    audit_readiness: Any  # AuditReadinessSnapshot.model_dump output
    created_by_user_id: str
    # The recipient needs the frozen human-readable username; the opaque id
    # alone does not identify the sender to someone without directory access.
    created_by_username: str
    compartment_id: str | None


class ShareFileEntry(TypedDict):
    """One frozen file in a snapshot's private ``files`` manifest.

    ``sha256`` is the bare hex content-address of the frozen bytes in the
    payload store (equal to the owner's blob row ``content_hash``). Blob ids
    and storage paths are deliberately absent: the import path re-creates
    the file in the recipient's session from these bytes.
    """

    sha256: str
    size_bytes: int
    filename: str
    mime_type: str
    bindings: list[dict[str, object]]  # file_binding_to_json output, one per binding site


class _SnapshotShape(_BlobShape):
    """Exact canonical-JSON shape produced by this release."""

    import_allowed: bool
    files: list[ShareFileEntry]  # sorted by sha256; [] when not importable


# Closed-set guards. The digest only proves bytes-on-disk integrity; it does
# NOT detect a producer drift that emits an extra or missing key (the buggy
# bytes round-trip cleanly through the digest). The producer guard in
# ``_build_snapshot`` catches that class of drift at the producer so the bug
# surfaces in the owner's request instead of several frames away in the
# reviewer's Pydantic construction. The reader accepts exactly these two
# closed sets — the previous release's seven keys (read as view-only) and
# the current nine — and nothing in between. Update the TypedDicts and these
# sets together.
_LEGACY_BLOB_KEYS: Final[frozenset[str]] = frozenset(
    {
        "pipeline_metadata",
        "composition_snapshot",
        "yaml",
        "audit_readiness",
        "created_by_user_id",
        "created_by_username",
        "compartment_id",
    }
)
_BLOB_KEYS: Final[frozenset[str]] = _LEGACY_BLOB_KEYS | {"import_allowed", "files"}
_FILE_ENTRY_KEYS: Final[frozenset[str]] = frozenset({"sha256", "size_bytes", "filename", "mime_type", "bindings"})
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ParsedSnapshot:
    """A verified share snapshot, as the resolve and import paths read it.

    ``blob`` carries the seven keys every readable snapshot has. A legacy
    seven-key snapshot reads as ``import_allowed=False`` with no files.
    """

    blob: _BlobShape
    import_allowed: bool
    files: tuple[ShareFileEntry, ...]


@dataclass(frozen=True, slots=True)
class _FrozenFile:
    """One verified file awaiting storage: its bytes and its manifest entry."""

    content: bytes
    entry: ShareFileEntry


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """In-memory snapshot built once at mark-time.

    Carries the serialized canonical bytes (for hashing + blob storage)
    plus the live AuditReadinessSnapshot (so the response can include it
    without round-tripping through JSON).
    """

    canonical_bytes: bytes
    payload_digest: str  # sha256:<hex>
    digest_hex: str  # bare hex for payload-store calls
    audit_readiness: AuditReadinessSnapshot
    state_id: UUID
    created_at: datetime


def _has_error_readiness_row(snapshot: AuditReadinessSnapshot) -> bool:
    """Mark-time gate: any row with status='error' blocks the share."""
    return any(row.status == "error" for row in snapshot.rows)


def _public_audit_readiness(snapshot: AuditReadinessSnapshot) -> AuditReadinessSnapshot:
    """Remove owner-global inventory detail from a share-scoped snapshot."""
    rows = tuple(row.model_copy(update={"detail": None}) if row.id == "secrets" else row for row in snapshot.rows)
    return snapshot.model_copy(update={"rows": rows})


def _check_shareable_blob(record: BlobRecord, content: bytes | None) -> str | None:
    """Return why ``record`` cannot travel with an importable share, or ``None``.

    The message is user register and names the file. ``content`` is the
    blob's bytes as read under the caller's session-operation context, or
    ``None`` when they were not read (a blob that is not ready is refused
    from its row alone). Shared by the mark-ready freeze and the owner's
    file preview, so both report the same verdict.
    """
    name = record.filename
    if record.status != "ready":
        return f"'{name}' is not ready to share (its upload is {record.status})"
    if record.content_hash is None:
        return f"'{name}' has no recorded content hash and cannot be shared for import"
    if record.creation_modality is not CreationModality.VERBATIM:
        return f"'{name}' was written by the assistant and cannot be shared for import"
    if content is None:
        return f"'{name}' could not be read and cannot be shared for import"
    if len(content) != record.size_bytes or hashlib.sha256(content).hexdigest() != record.content_hash:
        return f"'{name}' does not match its recorded content and cannot be shared for import"
    return None


def _binding_site(binding: FileBinding) -> str:
    """Name where a file is used, for a refusal that has no filename to give."""
    if binding.kind == "source_blob_ref":
        return f"source '{binding.source_name}'"
    if binding.kind == "source_blob_rows":
        return f"source '{binding.source_name}' (blobs[{binding.row_index}])"
    return f"option {binding.field_path}"


def _describe_lifetime(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return "1 day" if days == 1 else f"{days} days"
    return f"{seconds} seconds"


def _parse_file_manifest(value: object) -> tuple[ShareFileEntry, ...]:
    """Validate a snapshot's ``files`` manifest; raise ``InvalidToken`` on any drift.

    Tier-1 data (our own digest-verified blob), so every deviation from the
    producer's exact shape is corruption, not something to repair: entries
    are exact-keyed, strictly sorted by sha256 (which also forbids
    duplicates), and every binding must round-trip ``file_binding_from_json``.
    """
    if type(value) is not list:
        raise InvalidToken("share snapshot file manifest is invalid")
    entries: list[ShareFileEntry] = []
    previous_sha: str | None = None
    for raw in value:
        if type(raw) is not dict or frozenset(raw) != _FILE_ENTRY_KEYS:
            raise InvalidToken("share snapshot file entry shape is invalid")
        sha256 = raw["sha256"]
        if type(sha256) is not str or _SHA256_HEX.fullmatch(sha256) is None:
            raise InvalidToken("share snapshot file digest is invalid")
        if previous_sha is not None and sha256 <= previous_sha:
            raise InvalidToken("share snapshot files are not uniquely sorted by digest")
        previous_sha = sha256
        size_bytes = raw["size_bytes"]
        if type(size_bytes) is not int or size_bytes < 0:
            raise InvalidToken("share snapshot file size is invalid")
        filename = raw["filename"]
        if type(filename) is not str or not filename.strip():
            raise InvalidToken("share snapshot filename is invalid")
        mime_type = raw["mime_type"]
        if type(mime_type) is not str or mime_type not in STORAGE_MIME_TYPES:
            raise InvalidToken("share snapshot file type is invalid")
        raw_bindings = raw["bindings"]
        if type(raw_bindings) is not list or not raw_bindings:
            raise InvalidToken("share snapshot file bindings are invalid")
        try:
            bindings = [file_binding_to_json(file_binding_from_json(binding)) for binding in raw_bindings]
        except ValueError as exc:
            raise InvalidToken("share snapshot file binding is invalid") from exc
        entries.append(
            {
                "sha256": sha256,
                "size_bytes": size_bytes,
                "filename": filename,
                "mime_type": mime_type,
                "bindings": bindings,
            }
        )
    return tuple(entries)


def _build_snapshot(
    *,
    session_id: UUID,
    state_record: Any,
    audit_readiness: AuditReadinessSnapshot,
    created_by_user_id: str,
    created_by_username: str,
    compartment_id: str | None,
    import_allowed: bool,
    files: list[ShareFileEntry],
) -> _Snapshot:
    """Build the canonical-JSON snapshot blob and compute its content-address.

    The blob shape is the one the resolve and import paths read back —
    pipeline_metadata, composition_snapshot, yaml, audit_readiness,
    created_by_user_id, created_by_username, compartment_id, import_allowed
    and the private ``files`` manifest. This is the contractual shape for
    the share artifact; ``payload_digest`` binds the import decision and the
    manifest along with everything else.

    Both attribution fields are frozen into the blob: the identity id
    because the token signature binds it, and the username because it is
    the only one of the two a recipient can read. The username is a
    point-in-time copy on purpose — a later rename must not silently
    restate who signed this snapshot.
    """
    composition_state = state_from_record(state_record)
    yaml_text = generate_public_yaml(composition_state)
    composition_dict = generate_public_composition_dict(composition_state)
    # ``metadata`` is normalised to ``{"name", "description"}`` by
    # CompositionState.to_dict — that IS the pipeline_metadata wire shape.
    # Direct indexing per the engine-patterns-reference skill §Offensive
    # Programming Examples: ``to_dict`` always emits a ``metadata`` key.
    # KeyError here is a contract violation of CompositionState.to_dict.
    pipeline_metadata = composition_dict["metadata"]
    # The blob carries ONLY content-addressed content. Mark-time and
    # mint-time live in the token envelope (and the audit row), not here,
    # so two re-mints over an unchanged composition yield the same
    # payload_digest. Precisely: unchanged composition AND unchanged
    # attribution — ``created_by_username`` is blob content, so a rename
    # between marks legitimately produces a new digest. Nothing depends on
    # the digest surviving a rename: ``get_shareable_link`` re-mints against
    # the digest recorded on the audit row, not a recomputed one.
    #
    # The readiness snapshot is normalised: its ``checked_at`` is pinned
    # to the composition state's ``created_at`` rather than the live
    # ``datetime.now()``. Without this normalisation the
    # ``AuditReadinessSnapshot`` would carry a fresh wall-clock on each
    # re-mint, breaking digest stability even on an otherwise unchanged
    # composition. Pinning to state.created_at is semantically defensible:
    # the readiness panel describes "what readiness signal accompanied
    # the state when it was committed," not "when was this query run."
    public_audit_readiness = _public_audit_readiness(audit_readiness)
    audit_readiness_dict = public_audit_readiness.model_dump(mode="json")
    audit_readiness_dict["checked_at"] = state_record.created_at.isoformat()
    if compartment_id is not None and not is_compartment_id(compartment_id):
        raise ValueError("shareable-review compartment_id does not match the configured shape")
    if not import_allowed and files:
        raise ValueError("a view-only share snapshot must not carry a file manifest")
    blob: _SnapshotShape = {
        "pipeline_metadata": pipeline_metadata,
        "composition_snapshot": composition_dict,
        "yaml": yaml_text,
        "audit_readiness": audit_readiness_dict,
        "created_by_user_id": created_by_user_id,
        "created_by_username": created_by_username,
        "compartment_id": compartment_id,
        "import_allowed": import_allowed,
        "files": sorted(files, key=lambda entry: entry["sha256"]),
    }
    # Producer-side drift guard. ``_SnapshotShape: TypedDict`` is a static
    # type — Python does not enforce it at runtime. Without this assert,
    # a future refactor adding/dropping a key would produce a digest-stable
    # but shape-drifted blob; the bug would surface in Pydantic
    # construction in ``SharedInspectResponse`` several frames downstream.
    # Crash here so the bug surfaces at the producer.
    actual_keys = frozenset(blob.keys())
    if actual_keys != _BLOB_KEYS:
        missing = _BLOB_KEYS - actual_keys
        extra = actual_keys - _BLOB_KEYS
        raise RuntimeError(
            f"shareable-review snapshot blob shape drift: missing={sorted(missing)!r} extra={sorted(extra)!r}. "
            "Update _SnapshotShape and _BLOB_KEYS together."
        )
    canonical_str = canonical_json(blob)
    canonical_bytes = canonical_str.encode("utf-8")
    digest_hex = hashlib.sha256(canonical_bytes).hexdigest()
    return _Snapshot(
        canonical_bytes=canonical_bytes,
        payload_digest=_DIGEST_PREFIX + digest_hex,
        digest_hex=digest_hex,
        audit_readiness=public_audit_readiness,
        state_id=state_record.id,
        created_at=datetime.now(UTC),
    )


# ── Service ──────────────────────────────────────────────────────────────


class ShareableReviewService:
    """Orchestrator for the shareable-reviews capability.

    Dependencies are injected so tests can substitute mocks. In production
    the wiring lives in ``web/app.py``.
    """

    def __init__(
        self,
        *,
        session_service: _SessionServiceLike,
        execution_service: _ExecutionServiceLike,
        readiness_service: _ReadinessServiceLike,
        signer: ShareTokenSigner,
        settings: WebSettings,
        sessions_db_engine: Engine,
        session_operation_authority: SessionOperationAuthority,
        payload_store: FilesystemPayloadStore,
        telemetry: SessionsTelemetry,
        blob_service: BlobServiceProtocol,
    ) -> None:
        self._session_service = session_service
        self._execution_service = execution_service
        self._readiness_service = readiness_service
        self._signer = signer
        self._settings = settings
        self._sessions_db_engine = sessions_db_engine
        self._session_operation_authority = session_operation_authority
        self._payload_store = payload_store
        # Phase 8 Sub-task 7c — composer.session.completed_total counter.
        # Mirrors the sessions/service.py pattern at line 48/2446 (the
        # cohort b1 interpretation-opt-out emit). The container is owned
        # by the FastAPI app and threaded through app.state; we hold a
        # reference so the audit-row-then-counter sequence at
        # mark_ready_for_review can fire the helper after engine.begin()
        # exits cleanly.
        self._telemetry = telemetry
        # Reads the owner's uploaded files (under the route's COMPOSE
        # context) when a share opts in to import.
        self._blob_service = blob_service

    async def mark_ready_for_review(
        self,
        *,
        session_id: UUID,
        user_id: str,
        username: str,
        session_operation_context: SessionOperationContext,
        allow_import: bool = False,
        lifetime_seconds: int | None = None,
    ) -> MarkReadyForReviewResponse:
        """Build a signed share artifact for ``(session_id, current state)``.

        ``user_id`` is the opaque identity id — it scopes ownership, the
        audit row's actor, and the token signature. ``username`` is the
        same person's human-readable login name and is carried only so the
        recipient's banner can name who shared the pipeline; nothing
        authorises off it.

        ``allow_import`` lets recipients import a copy: every file the
        composition uses is verified and frozen into the payload store, and
        listed in the snapshot's private ``files`` manifest. ``lifetime_seconds``
        is the link's lifespan; ``None`` means the deployment maximum, and a
        longer value is refused (``lifetime_exceeds_maximum``).

        Sequence (audit-first ordering):

        1. Validate the composition and completion gates (raises
           ``CompositionNotRunnableError`` on failure).
        2. Compute the readiness snapshot using the OWNER's user_id and
           apply the mark-time gate (no row with status='error').
        3. When importing, read and verify every referenced file; any file
           that is not shareable refuses the whole share
           (``file_not_shareable``) before anything is stored.
        4. Build the canonical-JSON snapshot bytes; compute payload_digest.
        5. Store the verified file bytes.
        6. INSERT the audit row into ``composer_completion_events_table``.
        7. Write the snapshot blob to the payload store.
        8. Sign the token; return.

        No token is minted unless every file is frozen. If step (6) fails
        the request fails before the snapshot blob is written. If step (7)
        fails the audit row stands as honest evidence of the attempt; no
        token is returned.
        """
        max_lifetime_seconds = self._settings.shareable_link_lifetime_seconds
        if lifetime_seconds is None:
            lifetime_seconds = max_lifetime_seconds
        elif lifetime_seconds <= 0:
            raise ValueError(f"lifetime_seconds must be positive, got {lifetime_seconds}")
        elif lifetime_seconds > max_lifetime_seconds:
            raise CompositionNotRunnableError(
                reason="lifetime_exceeds_maximum",
                detail=f"a shared link can last at most {_describe_lifetime(max_lifetime_seconds)} on this deployment",
            )
        state_record = await self._session_service.get_current_state(session_id)
        if state_record is None:
            raise CompositionNotRunnableError(
                reason="state_missing",
                detail="No composition state exists for this session",
            )
        composition_state = state_from_record(state_record)
        # session_id scopes the sink path allowlist (blobs/<session_id>/) and
        # inline-blob metadata lookups; omitting it fails closed to outputs-only
        # and rejects states that /validate and /execute accept. Persisted
        # completion-gate facts (advisor sign-off, R2-F14) are threaded from the
        # record so the share-time validation reports the same readiness the
        # composer and /validate report.
        validation = await self._execution_service.validate_state(
            composition_state,
            user_id=user_id,
            session_id=session_id,
            session_operation_context=session_operation_context,
            completion_gates=parse_completion_gates(state_record.composer_meta),
            # The mark writes this verdict into a durable review record; an
            # audit-bearing record is never derived from the advisory memo.
            fresh=True,
        )
        if not validation.is_valid:
            # elspeth-3830c620a2: a pending-interpretation handoff is
            # ``is_valid=False`` by design (soft halt; authoring validated and
            # the only blocker is a resolvable review card). Refusing it as
            # "validation failed; fix errors" told the user to fix errors that
            # do not exist. The share stays blocked — a reviewer of an
            # un-reviewed composition would be attesting over unresolved
            # cards — but the reason tells the truth. Allowing
            # share-while-pending is a separate product decision (tracked on
            # the ticket); this branch does not foreclose it.
            if is_pending_interpretation_handoff(validation):
                raise CompositionNotRunnableError(
                    reason="review_pending",
                    detail="interpretation review cards are waiting; resolve them before sharing",
                )
            raise CompositionNotRunnableError(
                reason="validation_failed",
                detail="composition validation failed; fix errors before sharing",
            )
        if not validation.readiness.completion_ready:
            raise CompositionNotRunnableError(
                reason="completion_not_ready",
                detail="composition completion gates have not passed; resolve blockers before sharing",
            )

        audit_readiness = await self._readiness_service.compute_snapshot(
            session_id=session_id,
            user_id=user_id,
            session_operation_context=session_operation_context,
            # The snapshot is written into the durable review blob: its
            # validation row must not come from the advisory memo either.
            fresh=True,
        )
        if _has_error_readiness_row(audit_readiness):
            raise CompositionNotRunnableError(
                reason="readiness_error_row",
                detail="readiness panel reports an error; resolve before sharing",
            )
        if audit_readiness.composition_version != state_record.version:
            raise CompositionNotRunnableError(
                reason="readiness_state_drift",
                detail="composition changed while preparing the review snapshot; retry against the current state",
            )

        frozen_files = (
            await self._verify_shareable_files(composition_state, session_operation_context=session_operation_context)
            if allow_import
            else ()
        )

        snapshot = _build_snapshot(
            session_id=session_id,
            state_record=state_record,
            audit_readiness=audit_readiness,
            created_by_user_id=user_id,
            created_by_username=username,
            compartment_id=self._settings.compartment_id,
            import_allowed=allow_import,
            files=[frozen.entry for frozen in frozen_files],
        )

        # Stamp expiry now so the same value lands in both the audit row
        # and the signed token envelope. The validity period is set at
        # issue-time; the signer's verify() rejects after this point.
        # ``get_shareable_link`` re-derives the lifespan from the audit row
        # (expires_at - created_at), so a re-mint keeps the chosen lifespan.
        expires_at = snapshot.created_at + timedelta(seconds=lifetime_seconds)

        # Files before the audit row: the row's digest commits to a manifest
        # whose bytes are already frozen, so no audited share can name a
        # file the payload store never received.
        for frozen in frozen_files:
            stored_file_hex = self._payload_store.store(frozen.content)
            if stored_file_hex != frozen.entry["sha256"]:
                raise RuntimeError(
                    f"payload store returned digest {stored_file_hex} for a frozen share file whose verified sha256 is {frozen.entry['sha256']}"
                )

        # AUDIT FIRST under the exact COMPOSE authority already spanning
        # validation and readiness (a completion event is a write, so the
        # route holds writer authority). The facet re-proves that this is
        # still the current state in the same transaction as the insert.
        try:
            self._session_operation_authority.mutate(
                session_operation_context,
                lambda transaction: transaction.composer_completion.mark_ready_for_review(
                    composition_state_id=snapshot.state_id,
                    actor=user_id,
                    created_at=snapshot.created_at,
                    payload_digest=snapshot.payload_digest,
                    expires_at=expires_at,
                ),
            )
        except SessionDerivedCustodyError:
            raise CompositionNotRunnableError(
                reason="readiness_state_drift",
                detail="composition changed while preparing the review snapshot; retry against the current state",
            ) from None

        # Phase 8 Sub-task 7c (telemetry-backfill: phase-6).
        # Audit primacy: the helper runs AFTER the engine.begin() block
        # commits the audit row. If the INSERT above raises, control
        # never reaches this line and the counter stays at zero — the
        # superset rule (counter aggregates over committed audit rows)
        # is structurally enforced by the placement.
        record_session_completed(self._telemetry, completion_verb="mark_ready_for_review")

        # Then blob.
        stored_hex = self._payload_store.store(snapshot.canonical_bytes)
        # Defense in depth: the payload store recomputes the digest;
        # a mismatch here is a Tier-1 corruption event.
        if stored_hex != snapshot.digest_hex:
            raise RuntimeError(
                f"payload store returned digest {stored_hex} but we computed {snapshot.digest_hex} — possible canonicalisation drift"
            )

        token = self._sign_token(
            session_id=session_id,
            state_id=snapshot.state_id,
            payload_digest=snapshot.payload_digest,
            created_by_user_id=user_id,
            created_at=snapshot.created_at,
            expires_at=expires_at,
        )

        return MarkReadyForReviewResponse(
            token=token,
            share_url=_SHARE_URL_PREFIX + token,
            expires_at=expires_at,
            payload_digest=snapshot.payload_digest,
        )

    async def get_shareable_link(self, *, session_id: UUID, user_id: str) -> ShareableLinkResponse:
        """Re-mint a token for an already-marked current snapshot.

        Always mints a fresh token. Content-addressing guarantees that two
        calls on an unchanged state produce identical ``payload_digest``
        even though the token strings differ (the nonce in each token's
        envelope ensures the byte sequence varies).

        No audit row or blob is written here: this path is only allowed
        when a prior ``mark_ready_for_review`` row exists for the current
        ``(session, state)`` and the row's snapshot blob still exists. If
        the state drifts, the owner must mark ready again so the gate and
        audit row run on the new share decision.
        """
        state_record = await self._session_service.get_current_state(session_id)
        if state_record is None:
            raise CompositionNotRunnableError(
                reason="state_missing",
                detail="No composition state exists for this session",
            )
        event = self._latest_mark_ready_event(session_id=session_id, state_id=state_record.id, user_id=user_id)
        if event is None:
            raise CompositionNotRunnableError(
                reason="completion_event_missing",
                detail="mark this composition ready for review before requesting a shareable link",
            )
        payload_digest = str(event.payload_digest)
        digest_hex = payload_digest.removeprefix(_DIGEST_PREFIX)
        if not self._payload_store.exists(digest_hex):
            raise CompositionNotRunnableError(
                reason="not_marked_ready",
                detail=_NOT_MARKED_READY_DETAIL,
            )
        # Keep the lifespan the owner chose at mark time: a 1-day link must
        # not be stretched to the deployment maximum by "copy link again".
        # Clamped to the CURRENT maximum: if the operator lowered the cap
        # since the mark, the server still never issues a longer link.
        lifetime = min(
            event.expires_at - event.created_at,
            timedelta(seconds=self._settings.shareable_link_lifetime_seconds),
        )
        created_at = datetime.now(UTC)
        expires_at = created_at + lifetime
        token = self._sign_token(
            session_id=session_id,
            state_id=state_record.id,
            payload_digest=payload_digest,
            created_by_user_id=user_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        return ShareableLinkResponse(
            token=token,
            share_url=_SHARE_URL_PREFIX + token,
            expires_at=expires_at,
            state_id=str(state_record.id),
            payload_digest=payload_digest,
        )

    async def resolve_token(self, *, token: str, requesting_user_id: str) -> SharedInspectResponse:
        """Verify the token and return the frozen-at-mark-time snapshot.

        The audit_readiness is read directly from the blob — this method
        never calls ``ReadinessService.compute_snapshot``. Three reasons:
        audit-trail integrity (the reviewer sees what the owner saw at
        mark-time, not what readiness reports right now); content-addressing
        completeness (the payload_digest covers the readiness panel too);
        and permission-boundary collapse (resolve never calls the readiness
        service, so the reviewer-vs-owner permission question never arises
        at resolve time). ADR-022 §D4 documents the full reasoning.

        Raises:
            InvalidToken: signature mismatch, malformed envelope, or
                expired (per ``ShareTokenSigner.verify``).
            PayloadNotFoundError: token verifies but the payload-store
                blob has been reaped under the retention policy. The route
                layer maps this to HTTP 404 with a "ask the sender for a
                fresh link" message.
        """
        payload, snapshot = await self.load_snapshot(token=token)
        blob_dict = snapshot.blob
        # A valid digest authenticates the immutable evidence blob. Reconstruct
        # the frozen state and project it without rewriting those bytes.
        composition_state = CompositionState.from_dict(blob_dict["composition_snapshot"])
        public_composition = CompositionStateResponse.model_validate(generate_public_composition_dict(composition_state))
        public_yaml = generate_public_yaml(composition_state)
        # ``model_validate_json`` (not ``model_validate``) on the
        # audit_readiness sub-tree because the strict-mode model rejects
        # ISO-string datetimes and list-as-tuple after a JSON round-trip.
        # ``model_validate_json`` activates Pydantic's JSON validators
        # which DO coerce wire-format primitives back to native types.
        audit_readiness = _public_audit_readiness(AuditReadinessSnapshot.model_validate_json(json.dumps(blob_dict["audit_readiness"])))
        return SharedInspectResponse(
            session_id=str(payload.session_id),
            state_id=str(payload.state_id),
            pipeline_metadata=blob_dict["pipeline_metadata"],
            composition_snapshot=public_composition,
            yaml=public_yaml,
            audit_readiness=audit_readiness,
            created_by_user_id=blob_dict["created_by_user_id"],
            created_by_username=blob_dict["created_by_username"],
            # ``created_at`` lives in the token envelope rather than the blob —
            # the blob is content-addressed and must not carry mint-time data.
            created_at=payload.created_at,
            expires_at=payload.expires_at,
        )

    async def load_snapshot(self, *, token: str) -> tuple[ShareTokenPayload, ParsedSnapshot]:
        """Verify ``token`` and return its payload with the parsed frozen snapshot.

        The one loader for every reader of a share (inspect and import).

        Raises:
            InvalidToken: signature mismatch, malformed envelope, expired,
                or a snapshot whose shape this release does not read.
            PayloadNotFoundError: the snapshot blob is no longer stored.
        """
        payload = self._signer.verify(token)
        digest_hex = payload.payload_digest.removeprefix(_DIGEST_PREFIX)
        blob_bytes = self._payload_store.retrieve(digest_hex)
        # The payload store has already verified the digest matches —
        # any tampering on the filesystem path raises IntegrityError
        # before we get here.
        return payload, self._parse_blob(blob_bytes)

    # ── private helpers ─────────────────────────────────────────────

    async def _verify_shareable_files(
        self,
        composition_state: CompositionState,
        *,
        session_operation_context: SessionOperationContext,
    ) -> tuple[_FrozenFile, ...]:
        """Read and verify every file ``composition_state`` uses, once per blob.

        Raises ``CompositionNotRunnableError(reason="file_not_shareable")``
        naming the first file that cannot travel. Stores nothing: the caller
        stores the returned bytes only after every file has passed.

        Two blobs with identical bytes share one content address, so they
        become one manifest entry carrying both blobs' binding sites; the
        first blob's filename and MIME type name it.
        """
        try:
            bound = collect_file_bindings(composition_state)
        except ShareManifestError as exc:
            raise CompositionNotRunnableError(
                reason="file_not_shareable",
                detail=f"a file in this pipeline cannot be shared for import: {exc.detail}",
            ) from exc
        bindings_by_blob: dict[UUID, list[FileBinding]] = {}
        for item in bound:
            bindings_by_blob.setdefault(item.blob_id, []).append(item.binding)

        frozen_by_sha: dict[str, _FrozenFile] = {}
        for blob_id, bindings in bindings_by_blob.items():
            try:
                record = await self._blob_service.get_blob(blob_id, session_operation_context=session_operation_context)
            except BlobNotFoundError:
                raise CompositionNotRunnableError(
                    reason="file_not_shareable",
                    detail=f"the file used by {_binding_site(bindings[0])} no longer exists; reattach it before sharing for import",
                ) from None
            content: bytes | None = None
            if record.status == "ready":
                try:
                    content = await self._blob_service.read_blob_content(blob_id, session_operation_context=session_operation_context)
                except (BlobNotFoundError, BlobStateError):
                    raise CompositionNotRunnableError(
                        reason="file_not_shareable",
                        detail=f"'{record.filename}' changed while preparing the share; retry",
                    ) from None
            refusal = _check_shareable_blob(record, content)
            if refusal is not None:
                raise CompositionNotRunnableError(reason="file_not_shareable", detail=refusal)
            # _check_shareable_blob returned None, so the bytes were read and
            # match the row's non-null content_hash.
            if content is None or record.content_hash is None:
                raise RuntimeError("_check_shareable_blob passed a blob without verified content")
            binding_json = [file_binding_to_json(binding) for binding in bindings]
            if record.content_hash in frozen_by_sha:
                frozen_by_sha[record.content_hash].entry["bindings"].extend(binding_json)
                continue
            frozen_by_sha[record.content_hash] = _FrozenFile(
                content=content,
                entry={
                    "sha256": record.content_hash,
                    "size_bytes": record.size_bytes,
                    "filename": record.filename,
                    "mime_type": record.mime_type,
                    "bindings": binding_json,
                },
            )
        return tuple(frozen_by_sha[sha] for sha in sorted(frozen_by_sha))

    def _latest_mark_ready_event(self, *, session_id: UUID, state_id: UUID, user_id: str) -> Any | None:
        """Return the latest committed mark-ready event for this exact state."""
        with self._sessions_db_engine.begin() as conn:
            return conn.execute(
                select(composer_completion_events_table)
                .where(
                    composer_completion_events_table.c.session_id == str(session_id),
                    composer_completion_events_table.c.composition_state_id == str(state_id),
                    composer_completion_events_table.c.event_type == "mark_ready_for_review",
                    composer_completion_events_table.c.actor == user_id,
                )
                .order_by(desc(composer_completion_events_table.c.created_at))
                .limit(1)
            ).first()

    def _sign_token(
        self,
        *,
        session_id: UUID,
        state_id: UUID,
        payload_digest: str,
        created_by_user_id: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> str:
        return self._signer.sign(
            ShareTokenPayload(
                version=1,
                session_id=session_id,
                state_id=state_id,
                created_at=created_at,
                expires_at=expires_at,
                nonce_hex=secrets.token_hex(16),
                payload_digest=payload_digest,
                created_by_user_id=created_by_user_id,
            )
        )

    @staticmethod
    def _parse_blob(blob_bytes: bytes) -> ParsedSnapshot:
        """Decode the canonical-JSON blob back to the typed snapshot.

        Tier-1 input: the blob came out of OUR payload store, integrity
        verified by digest. Any decode failure here is a corruption event.
        Exactly two closed key sets are read: the current nine-key shape,
        and the previous release's seven-key shape, which predates the
        import opt-in and so reads as view-only with no files. Any other key
        set — including a partial or half-upgraded shape — is refused.
        """
        try:
            parsed = json.loads(blob_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidToken("share snapshot decode failed") from exc
        if type(parsed) is not dict:
            raise InvalidToken("unsupported share snapshot shape")
        keys = frozenset(parsed)
        if keys == _LEGACY_BLOB_KEYS:
            import_allowed = False
            files: tuple[ShareFileEntry, ...] = ()
        elif keys == _BLOB_KEYS:
            import_allowed = parsed["import_allowed"]
            if type(import_allowed) is not bool:
                raise InvalidToken("share snapshot import flag is invalid")
            files = _parse_file_manifest(parsed["files"])
            if not import_allowed and files:
                raise InvalidToken("view-only share snapshot carries a file manifest")
        else:
            raise InvalidToken("unsupported share snapshot shape")
        if type(parsed["created_by_username"]) is not str or not parsed["created_by_username"].strip():
            raise InvalidToken("share snapshot username is invalid")
        compartment_id = parsed["compartment_id"]
        if compartment_id is not None and not is_compartment_id(compartment_id):
            raise InvalidToken("share snapshot compartment is invalid")
        blob = cast(_BlobShape, {key: parsed[key] for key in _LEGACY_BLOB_KEYS})
        return ParsedSnapshot(blob=blob, import_allowed=import_allowed, files=files)

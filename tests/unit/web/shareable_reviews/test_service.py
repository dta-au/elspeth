"""Tests for ``ShareableReviewService`` — business logic for the
shareable-reviews capability.

Phase 6A Task 5 (UX redesign 2026-05).

Service contract (see plan §"Task 5"):

* ``mark_ready_for_review`` — build snapshot, compute payload_digest,
  INSERT audit row (audit-first), write blob, sign token, return.
* ``get_shareable_link`` — re-mint a fresh token over the current snapshot;
  content-addressing makes this idempotent at the blob level.
* ``resolve_token`` — verify token, read blob by digest, return the
  frozen-at-mark-time snapshot. Does NOT call the readiness service.

Audit-first ordering is load-bearing: if the audit insert fails, no blob
is ever written and no token is returned. If the blob write fails after
the audit insert, the audit row stands as honest evidence of the attempt;
no token is returned.

Frozen-at-mark-time discipline: the ``audit_readiness`` field is computed
once at ``mark_ready_for_review`` time using the OWNER's user_id and
embedded in the snapshot blob. ``resolve_token`` reads it back from the
blob; it never re-calls ``ReadinessService.compute_snapshot()``. This
eliminates the reviewer-vs-owner permission question and ensures
content-addressing covers the readiness fingerprint.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import select, text

from elspeth.contracts.blobs import BlobNotFoundError, BlobRecord, BlobStatus, StorageMimeType
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.core.canonical import canonical_json
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.audit_readiness.models import AuditReadinessSnapshot, ReadinessRow
from elspeth.web.coordination import repository as coordination_repository
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.schemas import (
    ADVISOR_SIGNOFF_BLOCKED_CODE,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    composer_completion_events_table,
    composition_states_table,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry, observed_value
from elspeth.web.shareable_reviews.models import CompositionStateResponse
from elspeth.web.shareable_reviews.service import (
    CompositionNotRunnableError,
    ParsedSnapshot,
    ShareableReviewService,
)
from elspeth.web.shareable_reviews.signer import InvalidToken, ShareTokenPayload, ShareTokenSigner
from tests.fixtures.identities import ensure_test_identity

_VALID_SIGNING_KEY = b"k" * 32

# The owner's human-readable login name, deliberately DIFFERENT from the
# ``session_record`` fixture's opaque ``user_id`` so a test that asserts on
# attribution cannot pass by accident when the two are conflated.
_OWNER_USERNAME = "alice-the-analyst"


def test_service_requires_explicit_session_operation_authority_dependency() -> None:
    parameters = inspect.signature(ShareableReviewService).parameters
    assert "session_operation_authority" in parameters


def _ready_readiness() -> ValidationReadiness:
    return ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[])


def _blocked_readiness() -> ValidationReadiness:
    return ValidationReadiness(authoring_valid=False, execution_ready=False, completion_ready=False, blockers=[])


def test_composition_snapshot_accepts_named_sources_payload() -> None:
    snapshot = CompositionStateResponse.model_validate(
        {
            "version": 1,
            "metadata": {"name": "legacy", "description": ""},
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "output",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [],
            "edges": [],
            "outputs": [
                {
                    "name": "output",
                    "plugin": "json",
                    "options": {"path": "out.jsonl"},
                    "on_write_failure": "discard",
                }
            ],
        }
    )

    assert tuple(snapshot.sources) == ("primary",)
    assert snapshot.sources["primary"].plugin == "csv"


# ── Minimal record shims ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _StateRecord:
    """Subset of CompositionStateRecord the service needs.

    The real CompositionStateRecord has many more fields; this shim carries
    only what ``state_from_record``-equivalent paths and the service's
    blob-normalisation path touch.
    """

    id: UUID
    session_id: UUID
    version: int
    metadata_: Any
    nodes: Any
    edges: Any
    outputs: Any
    source: Any
    sources: Any
    composer_meta: Any
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    id: UUID
    user_id: str


@dataclass(slots=True)
class _FakeSessionService:
    current_state: _StateRecord | None
    session: _SessionRecord | None
    get_current_state_await_count: int = 0
    get_session_await_count: int = 0

    async def get_current_state(self, session_id: UUID) -> _StateRecord | None:
        self.get_current_state_await_count += 1
        return self.current_state

    async def get_session(self, session_id: UUID) -> _SessionRecord | None:
        self.get_session_await_count += 1
        return self.session


@dataclass(slots=True)
class _FakeExecutionService:
    validation: ValidationResult
    validate_await_count: int = 0
    validate_state_await_count: int = 0
    # Session ids received by validate_state, in call order. The share gate
    # must thread the owning session id so the session-scoped sink allowlist
    # (blobs/<session_id>/) matches /validate and /execute.
    validate_state_session_ids: list[UUID | None] = field(default_factory=list)
    validate_state_operation_contexts: list[SessionOperationContext] = field(default_factory=list)
    validate_state_completion_gates: list[Any] = field(default_factory=list)
    validate_state_fresh: list[bool] = field(default_factory=list)

    async def validate(self, session_id: UUID, *, user_id: str | None = None) -> ValidationResult:
        self.validate_await_count += 1
        return self.validation

    async def validate_state(
        self,
        state: Any,
        *,
        user_id: str | None = None,
        session_id: UUID | None = None,
        session_operation_context: SessionOperationContext,
        completion_gates: Any = None,
        fresh: bool = False,
    ) -> ValidationResult:
        self.validate_state_await_count += 1
        self.validate_state_session_ids.append(session_id)
        self.validate_state_operation_contexts.append(session_operation_context)
        self.validate_state_completion_gates.append(completion_gates)
        self.validate_state_fresh.append(fresh)
        return self.validation


@dataclass(slots=True)
class _FakeReadinessService:
    snapshot: AuditReadinessSnapshot
    compute_snapshot_await_count: int = 0
    session_operation_contexts: list[SessionOperationContext] = field(default_factory=list)
    fresh: list[bool] = field(default_factory=list)

    async def compute_snapshot(
        self,
        *,
        session_id: UUID,
        user_id: str,
        session_operation_context: SessionOperationContext,
        fresh: bool = False,
    ) -> AuditReadinessSnapshot:
        self.compute_snapshot_await_count += 1
        self.session_operation_contexts.append(session_operation_context)
        self.fresh.append(fresh)
        return self.snapshot

    def reset_counts(self) -> None:
        self.compute_snapshot_await_count = 0


@dataclass(slots=True)
class _FakeSettings:
    shareable_link_lifetime_seconds: int = 30 * 24 * 3600
    compartment_id: str | None = None


@dataclass(slots=True)
class _FakeBlobService:
    """The two blob reads the share path makes, over in-memory rows.

    ``read_blob_content`` returns the stored bytes verbatim — it does NOT
    re-verify the hash, so a test can hand the service bytes that disagree
    with the row and prove the service's own check refuses them.
    """

    records: dict[UUID, BlobRecord] = field(default_factory=dict)
    contents: dict[UUID, bytes] = field(default_factory=dict)
    contexts: list[SessionOperationContext] = field(default_factory=list)
    reads: list[UUID] = field(default_factory=list)

    def add(
        self,
        session_id: UUID,
        *,
        filename: str,
        content: bytes,
        mime_type: StorageMimeType = "text/csv",
        status: BlobStatus = "ready",
        creation_modality: CreationModality = CreationModality.VERBATIM,
        content_hash: str | None = "computed",
        size_bytes: int | None = None,
    ) -> UUID:
        blob_id = uuid4()
        self.records[blob_id] = BlobRecord(
            id=blob_id,
            session_id=session_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(content) if size_bytes is None else size_bytes,
            content_hash=hashlib.sha256(content).hexdigest() if content_hash == "computed" else content_hash,
            storage_path=f"/srv/elspeth/blobs/{session_id}/{blob_id}_{filename}",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            created_by="user",
            source_description=None,
            status=status,
            creation_modality=creation_modality,
            created_from_message_id=None,
            creating_model_identifier=None,
            creating_model_version=None,
            creating_provider=None,
            creating_composer_skill_hash=None,
            creating_arguments_hash=None,
        )
        self.contents[blob_id] = content
        return blob_id

    async def get_blob(self, blob_id: UUID, *, session_operation_context: SessionOperationContext) -> BlobRecord:
        self.contexts.append(session_operation_context)
        if blob_id not in self.records:
            raise BlobNotFoundError(str(blob_id))
        return self.records[blob_id]

    async def read_blob_content(self, blob_id: UUID, *, session_operation_context: SessionOperationContext) -> bytes:
        self.contexts.append(session_operation_context)
        self.reads.append(blob_id)
        return self.contents[blob_id]


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def payload_store(tmp_path: Path) -> FilesystemPayloadStore:
    return FilesystemPayloadStore(tmp_path / "blobs")


@pytest.fixture
def signer() -> ShareTokenSigner:
    return ShareTokenSigner(_VALID_SIGNING_KEY)


@pytest.fixture
def session_id() -> UUID:
    return uuid4()


@pytest.fixture
def state_id() -> UUID:
    return uuid4()


@pytest.fixture
def session_record(session_id: UUID) -> _SessionRecord:
    return _SessionRecord(id=session_id, user_id="alice")


@pytest.fixture
def state_record(session_id: UUID, state_id: UUID) -> _StateRecord:
    return _StateRecord(
        id=state_id,
        session_id=session_id,
        version=3,
        metadata_=MappingProxyType({"name": "Demo Pipeline", "description": ""}),
        nodes=(),
        edges=(),
        outputs=(),
        source=None,
        sources=None,
        composer_meta=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def session_engine_with_row(
    engine,
    session_record: _SessionRecord,
    state_record: _StateRecord,
    monkeypatch: pytest.MonkeyPatch,
):
    """Insert parent sessions + composition_states rows so FK constraints on
    composer_completion_events resolve.
    """
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id=session_record.user_id)
    authority = SQLiteLocalSessionOperationAuthority(engine)
    monkeypatch.setattr(coordination_repository, "_new_session_id", lambda: session_record.id)
    created = authority.create_session_with_initial_fence(
        user_id=session_record.user_id,
        title="t",
        auth_provider_type="local",
        owner_instance_id="shareable-review-test",
        lease_seconds=30,
    )
    assert created.id == session_record.id
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            composition_states_table.insert().values(
                id=str(state_record.id),
                session_id=str(session_record.id),
                version=state_record.version,
                source=None,
                sources=None,
                nodes=[],
                edges=[],
                outputs=[],
                metadata_={"name": "Demo Pipeline", "description": ""},
                is_valid=True,
                validation_errors=None,
                composer_meta=None,
                created_at=now,
                derived_from_state_id=None,
                provenance="tool_call",
            )
        )
    return engine


@pytest.fixture
def session_operation_context(
    session_engine_with_row,
    session_record: _SessionRecord,
):
    """Acquire one real COMPOSE context for the full mark operation (a completion event is a write)."""
    authority = SQLiteLocalSessionOperationAuthority(session_engine_with_row)
    context = authority.acquire(
        session_id=session_record.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="shareable-review-test",
        lease_seconds=30,
    )
    try:
        yield context
    finally:
        with contextlib.suppress(SessionOperationFenceLost):
            authority.release(context)


def _ok_validation() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=_ready_readiness(),
        semantic_contracts=[],
    )


def _pending_review_validation() -> ValidationResult:
    """The pending-interpretation handoff as validate_pipeline actually emits it.

    is_valid=False is a SOFT halt: authoring validated and the only blocker is
    a resolvable review card. Producer-honest per elspeth-fa18d54eef: the
    strict ledger appends a SKIPPED advisor_signoff tail on every halted run,
    so the fixture carries it — a consumer that conflates skipped with failed
    fails here, not in production.
    """
    from elspeth.web.execution.schemas import (
        CHECK_ADVISOR_SIGNOFF,
        CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
        ValidationCheck,
        ValidationReadiness,
        ValidationReadinessBlocker,
    )
    from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_PENDING_CODE

    return ValidationResult(
        is_valid=False,
        checks=[
            ValidationCheck(
                name=CHECK_ADVISOR_SIGNOFF,
                passed=False,
                detail="Skipped: review_interpretations failed",
                affected_nodes=(),
                outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
            )
        ],
        errors=[
            ValidationError(
                component_id="assess",
                component_type="transform",
                message="llm_prompt_template review pending for transform 'assess'",
                suggestion=None,
                error_code=None,
            )
        ],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=False,
            completion_ready=True,
            blockers=[
                ValidationReadinessBlocker(
                    suggestion=None,
                    note=None,
                    code=INTERPRETATION_REVIEW_PENDING_CODE,
                    component_id="assess",
                    component_type="transform",
                    detail="llm_prompt_template review pending",
                )
            ],
        ),
        semantic_contracts=[],
    )


def _broken_validation() -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="node1",
                component_type="transform",
                message="boom",
                suggestion=None,
                error_code=None,
            )
        ],
        readiness=_blocked_readiness(),
        semantic_contracts=[],
    )


def _completion_blocked_validation() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    suggestion=None,
                    note=None,
                    code=ADVISOR_SIGNOFF_BLOCKED_CODE,
                    component_id="pipeline",
                    component_type="pipeline",
                    detail="Advisor sign-off is required before completion.",
                )
            ],
        ),
        semantic_contracts=[],
    )


def _readiness_snapshot(
    session_id: UUID,
    version: int = 3,
    *,
    error_row: bool = False,
    warning_row: bool = False,
) -> AuditReadinessSnapshot:
    """Build a minimal valid AuditReadinessSnapshot for tests."""

    def _row(row_id: str, status: str = "ok", summary: str = "ok") -> ReadinessRow:
        return ReadinessRow(
            id=row_id,  # type: ignore[arg-type]
            label=row_id,
            status=status,  # type: ignore[arg-type]
            summary=summary,
            detail=None,
            component_ids=(),
        )

    validation_status = "ok"
    if error_row:
        validation_status = "error"
    elif warning_row:
        validation_status = "warning"

    return AuditReadinessSnapshot(
        session_id=str(session_id),
        composition_version=version,
        checked_at=datetime.now(UTC),
        rows=(
            _row("validation", status=validation_status, summary="check"),
            _row("plugin_trust"),
            _row("provenance"),
            _row("retention"),
            _row("llm_interpretations"),
            _row("secrets"),
        ),
        validation_result=_ok_validation(),
    )


def _build_service(
    *,
    engine,
    payload_store: FilesystemPayloadStore,
    signer: ShareTokenSigner,
    session_record: _SessionRecord,
    state_record: _StateRecord,
    validation: ValidationResult,
    readiness: AuditReadinessSnapshot,
    blob_service: _FakeBlobService | None = None,
    settings: _FakeSettings | None = None,
) -> tuple[ShareableReviewService, _FakeSessionService, _FakeExecutionService, _FakeReadinessService]:
    session_service = _FakeSessionService(current_state=state_record, session=session_record)
    execution_service = _FakeExecutionService(validation=validation)
    readiness_service = _FakeReadinessService(snapshot=readiness)
    if settings is None:
        settings = _FakeSettings()

    # Phase 8 Sub-task 7c — the constructor now requires a telemetry
    # container. Build a fresh fake-counter container per service
    # instance (Q10 isolation per the Phase 8 telemetry test discipline).
    # Tests asserting on the counter retrieve it via
    # ``service._telemetry`` (the test file in
    # ``test_telemetry_session_completed.py`` exercises that path
    # explicitly).
    telemetry = build_sessions_telemetry()

    service = ShareableReviewService(
        session_service=session_service,
        execution_service=execution_service,
        readiness_service=readiness_service,
        signer=signer,
        settings=settings,
        sessions_db_engine=engine,
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
        payload_store=payload_store,
        telemetry=telemetry,
        blob_service=blob_service if blob_service is not None else _FakeBlobService(),
    )
    return service, session_service, execution_service, readiness_service


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_ready_for_review_happy_path(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    snapshot = _readiness_snapshot(session_record.id)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    assert response.token
    assert response.payload_digest.startswith("sha256:")
    # Blob is in the payload store (hex digest without prefix).
    digest_hex = response.payload_digest.removeprefix("sha256:")
    assert payload_store.exists(digest_hex)
    # Audit row landed.
    with session_engine_with_row.connect() as conn:
        rows = conn.execute(
            select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session_record.id))
        ).all()
    assert len(rows) == 1
    assert rows[0].event_type == "mark_ready_for_review"
    assert rows[0].actor == session_record.user_id
    assert rows[0].payload_digest == response.payload_digest
    # Token verifies through the same signer.
    payload = signer.verify(response.token)
    assert payload.session_id == session_record.id
    assert payload.payload_digest == response.payload_digest


@pytest.mark.asyncio
async def test_share_snapshot_marks_compartment_and_changes_digest(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    service._settings.compartment_id = "alpha"
    first = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    first_blob = json.loads(payload_store.retrieve(first.payload_digest.removeprefix("sha256:")))
    assert first_blob["compartment_id"] == "alpha"

    service._settings.compartment_id = "beta"
    second = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    second_blob = json.loads(payload_store.retrieve(second.payload_digest.removeprefix("sha256:")))
    assert second_blob["compartment_id"] == "beta"
    assert first.payload_digest != second.payload_digest


@pytest.mark.asyncio
async def test_mark_ready_for_review_passes_validation_authority_and_completion_gates(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """validate_state must receive the owning session context and persisted gates.

    The sink path allowlist is session-scoped (blobs/<session_id>/, see
    elspeth-bdc17cfdb1): validating with session_id=None fails closed to
    outputs-only and rejects a state whose sink writes to the session's own
    blob subtree — a state /validate and /execute both accept.
    """
    from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorSignoffGateFact
    from elspeth.web.execution.completion_gates import CompletionGateFacts

    snapshot = _readiness_snapshot(session_record.id)
    state_record = replace(
        state_record,
        composer_meta={
            "completion_gates": {
                "schema_version": 2,
                "advisor_signoff": {
                    "suggestion": None,
                    "status": "blocked",
                    "detail": "The advisor sign-off could not be obtained; the pipeline cannot complete.",
                    "for_graph": "0" * 64,
                    "note": None,
                    "cause": "unavailable",
                },
            }
        },
    )
    service, _, execution_service, readiness_service = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    assert execution_service.validate_state_session_ids == [session_record.id]
    assert execution_service.validate_state_operation_contexts == [session_operation_context]
    # The mark's verdict lands in a durable review record: never from the memo.
    assert execution_service.validate_state_fresh == [True]
    assert execution_service.validate_state_completion_gates == [
        CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                suggestion=None,
                detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
                for_graph="0" * 64,
                note=None,
                cause=AdvisorBlockCause.UNAVAILABLE,
            )
        )
    ]
    assert readiness_service.session_operation_contexts == [session_operation_context]
    # The readiness snapshot is also written into the review blob: fresh too.
    assert readiness_service.fresh == [True]


@pytest.mark.asyncio
async def test_mark_ready_for_review_yaml_strips_blob_bound_source_storage_path(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    storage_path = "/data/blobs/session/98b1357d_contact_form_submissions.csv"
    blob_id = "98b1357d-5aab-4fb3-85b4-5ad643912e84"
    blob_state_record = replace(
        state_record,
        source={
            "plugin": "csv",
            "on_success": "main",
            "options": {
                "path": storage_path,
                "blob_ref": blob_id,
                "mode": "bind_source",
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "quarantine",
        },
        outputs=[
            {
                "name": "main",
                "plugin": "csv",
                "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
    )
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=blob_state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )

    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )

    payload = json.loads(payload_store.retrieve(response.payload_digest.removeprefix("sha256:")).decode("utf-8"))
    assert storage_path not in payload["yaml"]
    options = yaml.safe_load(payload["yaml"])["sources"]["source"]["options"]
    assert "path" not in options
    assert "blob_ref" not in options
    assert "mode" not in options
    assert options["schema"] == {"mode": "observed"}


@pytest.mark.asyncio
async def test_share_snapshot_uses_one_recursive_public_projection(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Every stored/resolved representation must exclude private source facts.

    The three named sources pin all producer branches: a real blob binding,
    an explicit-null blob_ref, and a path-only source. The nested authoring
    metadata canary proves the projection is recursive rather than a shallow
    top-level key filter.
    """
    private_paths = {
        "/srv/elspeth/blobs/alice/blob-backed.csv",
        "/srv/elspeth/blobs/alice/explicit-null.csv",
        "/srv/elspeth/blobs/alice/path-only.csv",
        "/srv/elspeth/blobs/alice/nested-authoring.csv",
        "/srv/elspeth/indexes/alice/source-index",
        "/srv/elspeth/indexes/alice/vector-index",
        "/srv/elspeth/inputs/alice/node-input.csv",
        "/srv/elspeth/outputs/alice/out.csv",
    }
    blob_ids = {
        "98b1357d-5aab-4fb3-85b4-5ad643912e84",
        "20b944e3-fd46-434f-b9a2-4fb508db30f0",
        "30b944e3-fd46-434f-b9a2-4fb508db30f0",
    }
    canary_record = replace(
        state_record,
        source=None,
        sources={
            "blob_backed": {
                "plugin": "csv",
                "on_success": "main",
                "options": {
                    "path": "/srv/elspeth/blobs/alice/blob-backed.csv",
                    "blob_ref": "98b1357d-5aab-4fb3-85b4-5ad643912e84",
                    "mode": "bind_source",
                    "schema": {"mode": "observed"},
                },
                "on_validation_failure": "discard",
            },
            "explicit_null": {
                "plugin": "csv",
                "on_success": "main",
                "options": {
                    "path": "/srv/elspeth/blobs/alice/explicit-null.csv",
                    "blob_ref": None,
                    "schema": {"mode": "observed"},
                },
                "on_validation_failure": "discard",
            },
            "path_only": {
                "plugin": "csv",
                "on_success": "main",
                "options": {
                    "file": "/srv/elspeth/blobs/alice/path-only.csv",
                    SOURCE_AUTHORING_KEY: {
                        "path": "/srv/elspeth/blobs/alice/nested-authoring.csv",
                        "blob_ref": "20b944e3-fd46-434f-b9a2-4fb508db30f0",
                    },
                    "custody": {
                        "persist_directory": "/srv/elspeth/indexes/alice/source-index",
                        "blob_id": "30b944e3-fd46-434f-b9a2-4fb508db30f0",
                    },
                    "schema": {"mode": "observed"},
                },
                "on_validation_failure": "discard",
            },
        },
        nodes=[
            {
                "id": "pass",
                "node_type": "transform",
                "plugin": "llm",
                "input": "main",
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "profile": "operator-owned-alias",
                    "prompt_template": "{{ lookup.path }} {{ lookup.file }} {{ lookup.mode }}",
                    "lookup": {
                        "path": "north",
                        "file": "case.txt",
                        "mode": "bind_source",
                        "safe": "kept",
                    },
                    "provider_config": {
                        "persist_directory": "/srv/elspeth/indexes/alice/vector-index",
                        "nested": {"path": "/srv/elspeth/inputs/alice/node-input.csv"},
                    },
                },
            }
        ],
        outputs=[
            {
                "name": "main",
                "plugin": "csv",
                "options": {
                    "path": "/srv/elspeth/outputs/alice/out.csv",
                    "schema": {"mode": "observed"},
                },
                "on_write_failure": "discard",
            }
        ],
    )
    readiness = _readiness_snapshot(session_record.id)
    secret_row = ReadinessRow(
        id="secrets",
        label="Secrets",
        status="ok",
        summary="All secret references resolve",
        detail="17 secret(s) in your inventory",
        component_ids=(),
    )
    readiness = readiness.model_copy(update={"rows": (*readiness.rows[:-1], secret_row)})
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=canary_record,
        validation=_ok_validation(),
        readiness=readiness,
    )

    marked = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    stored = json.loads(payload_store.retrieve(marked.payload_digest.removeprefix("sha256:")))
    resolved = await service.resolve_token(token=marked.token, requesting_user_id="reviewer")
    representations = (
        stored,
        resolved.model_dump(mode="json"),
        yaml.safe_load(stored["yaml"]),
        stored["composition_snapshot"],
    )

    for representation in representations:
        serialized = json.dumps(representation, sort_keys=True)
        assert private_paths.isdisjoint(serialized.split('"'))
        assert blob_ids.isdisjoint(serialized.split('"'))
        assert "blob_ref" not in serialized
        assert "blob_id" not in serialized
        assert SOURCE_AUTHORING_KEY not in serialized
        assert "secret(s) in your inventory" not in serialized

    public_sources = stored["composition_snapshot"]["sources"]
    assert set(public_sources) == {"blob_backed", "explicit_null", "path_only"}
    assert all(source["plugin"] == "csv" for source in public_sources.values())
    assert all(source["on_success"] == "main" for source in public_sources.values())
    assert stored["composition_snapshot"]["outputs"][0]["name"] == "main"
    expected_lookup = {
        "path": "north",
        "file": "case.txt",
        "mode": "bind_source",
        "safe": "kept",
    }
    assert stored["composition_snapshot"]["nodes"][0]["options"]["lookup"] == expected_lookup
    assert yaml.safe_load(stored["yaml"])["transforms"][0]["options"]["lookup"] == expected_lookup
    assert resolved.composition_snapshot.nodes[0].options["lookup"] == expected_lookup


@pytest.mark.asyncio
async def test_share_content_is_invariant_to_unrelated_owner_secret_inventory(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Owner-global inventory changes must not alter composition-scoped shares."""

    def readiness_with_inventory_count(count: int) -> AuditReadinessSnapshot:
        snapshot = _readiness_snapshot(session_record.id)
        secret_row = ReadinessRow(
            id="secrets",
            label="Secrets",
            status="ok",
            summary="All secret references resolve",
            detail=f"{count} secret(s) in your inventory",
            component_ids=(),
        )
        return snapshot.model_copy(update={"rows": (*snapshot.rows[:-1], secret_row)})

    before, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=readiness_with_inventory_count(1),
    )
    before_mark = await before.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )

    after, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=readiness_with_inventory_count(9),
    )
    after_mark = await after.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )

    assert after_mark.payload_digest == before_mark.payload_digest
    assert payload_store.retrieve(after_mark.payload_digest.removeprefix("sha256:")) == payload_store.retrieve(
        before_mark.payload_digest.removeprefix("sha256:")
    )


@pytest.mark.asyncio
async def test_mark_ready_for_review_fails_validation(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_broken_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    with pytest.raises(CompositionNotRunnableError):
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )
    # No audit row was written, no blob was stored.
    with session_engine_with_row.connect() as conn:
        rows = conn.execute(select(composer_completion_events_table)).all()
    assert rows == []


@pytest.mark.asyncio
async def test_mark_ready_for_review_pending_review_gets_honest_reason(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context,
):
    """elspeth-3830c620a2: a composition whose ONLY blocker is a pending
    interpretation review must not be refused as "validation failed" —
    there are no errors to fix. The block stands (a share of an
    un-reviewed composition is not yet allowed) but the reason and detail
    tell the truth."""
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_pending_review_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )

    assert exc_info.value.reason == "review_pending"
    assert "review" in exc_info.value.detail
    assert "fix errors" not in exc_info.value.detail
    with session_engine_with_row.connect() as conn:
        rows = conn.execute(select(composer_completion_events_table)).all()
    assert rows == []


@pytest.mark.asyncio
async def test_mark_ready_for_review_genuine_failure_keeps_validation_failed(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context,
):
    """Negative guard: a genuinely broken composition still reports
    validation_failed — the handoff carve-out must not widen."""
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_broken_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )
    assert exc_info.value.reason == "validation_failed"


@pytest.mark.asyncio
async def test_mark_ready_for_review_rejects_completion_not_ready(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    monkeypatch,
    session_operation_context: SessionOperationContext,
):
    """A valid composition cannot be shared while completion is withheld."""
    store_calls: list[bytes] = []
    original_store = payload_store.store

    def tracking_store(data: bytes) -> str:
        store_calls.append(data)
        return original_store(data)

    monkeypatch.setattr(payload_store, "store", tracking_store)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_completion_blocked_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )

    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )

    assert exc_info.value.reason == "completion_not_ready"
    assert store_calls == []
    with session_engine_with_row.connect() as conn:
        rows = conn.execute(select(composer_completion_events_table)).all()
    assert rows == []


@pytest.mark.asyncio
async def test_mark_ready_for_review_blocks_error_readiness_row(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Sharing a state with status='error' on any readiness row is share-theatre."""
    snapshot = _readiness_snapshot(session_record.id, error_row=True)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    with pytest.raises(CompositionNotRunnableError):
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )


@pytest.mark.asyncio
async def test_mark_ready_for_review_allows_warning_readiness_row(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """status='warning' (e.g. pending llm_interpretations) is NOT a blocker."""
    snapshot = _readiness_snapshot(session_record.id, warning_row=True)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    assert response.token


@pytest.mark.asyncio
async def test_mark_ready_for_review_audit_first_ordering(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch,
):
    """If the audit insert fails, no blob is ever written.

    Audit-first ordering: the sequence is build → digest → audit INSERT →
    blob write → sign token. A failing audit must short-circuit before any
    file is written.
    """
    snapshot = _readiness_snapshot(session_record.id)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )

    # Track payload_store.store invocations so we can prove it was not called.
    original_store = payload_store.store
    store_calls: list[bytes] = []

    def tracking_store(content: bytes) -> str:
        store_calls.append(content)
        return original_store(content)

    monkeypatch.setattr(payload_store, "store", tracking_store)

    # Drop the sessions row to provoke an audit FK insert failure.
    with session_engine_with_row.begin() as conn:
        # The CHILD table has BEFORE DELETE ABORT trigger — sessions table
        # itself is not append-only, so we can manually break the FK by
        # inserting an event with a missing session_id. Easier: drop the
        # sessions row entirely so the FK fails on insert.
        conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_record.id)})

    with pytest.raises(Exception):  # noqa: B017 — any IntegrityError variant fails the request
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )
    # CRITICAL: no blob was written.
    assert store_calls == [], "audit insert must precede blob write — blob should not exist when audit fails"


@pytest.mark.asyncio
async def test_get_shareable_link_requires_mark_ready_for_current_snapshot(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    monkeypatch,
):
    """Re-minting before mark-ready must not create an unaudited share artifact."""
    snapshot = _readiness_snapshot(session_record.id)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )

    store_calls: list[bytes] = []

    def tracking_store(content: bytes) -> str:
        store_calls.append(content)
        return "unused"

    monkeypatch.setattr(payload_store, "store", tracking_store)

    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)

    assert exc_info.value.reason == "completion_event_missing"
    assert store_calls == []


@pytest.mark.asyncio
async def test_get_shareable_link_rejects_state_drift_even_when_digest_matches(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """The prior mark must match both current state_id and payload_digest."""
    snapshot = _readiness_snapshot(session_record.id)
    service, session_service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )

    await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )

    drifted_state = replace(state_record, id=uuid4())
    session_service.current_state = drifted_state

    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)

    assert exc_info.value.reason == "completion_event_missing"


@pytest.mark.asyncio
async def test_get_shareable_link_requires_existing_mark_ready_blob(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """An audit attempt row without the blob is not a successful share mark."""
    snapshot = _readiness_snapshot(session_record.id)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    payload_store.delete(response.payload_digest.removeprefix("sha256:"))

    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)

    assert exc_info.value.reason == "not_marked_ready"


@pytest.mark.asyncio
async def test_get_shareable_link_remints_with_stable_digest(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Two get_shareable_link calls on an unchanged state yield identical digests
    but different token strings (different nonce each call). Re-minting writes
    no new audit rows: the single mark_ready_for_review row remains the only
    share decision on record."""
    snapshot = _readiness_snapshot(session_record.id)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    marked = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    r1 = await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)
    r2 = await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)
    assert r1.payload_digest == marked.payload_digest
    assert r1.payload_digest == r2.payload_digest
    assert r1.token != r2.token

    with session_engine_with_row.connect() as conn:
        rows = conn.execute(
            select(
                composer_completion_events_table.c.payload_digest,
                composer_completion_events_table.c.expires_at,
            )
            .where(composer_completion_events_table.c.session_id == str(session_record.id))
            .where(composer_completion_events_table.c.event_type == "mark_ready_for_review")
            .order_by(composer_completion_events_table.c.created_at)
        ).all()

    assert [row.payload_digest for row in rows] == [marked.payload_digest]
    assert rows[0].payload_digest == r1.payload_digest
    assert rows[0].payload_digest == r2.payload_digest
    assert rows[0].expires_at.replace(tzinfo=UTC) == marked.expires_at


@pytest.mark.asyncio
async def test_get_shareable_link_requires_mark_ready_event(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    service, _ss, execution_service, readiness_service = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )

    with pytest.raises(CompositionNotRunnableError, match="mark this composition ready"):
        await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)

    assert execution_service.validate_state_await_count == 0
    assert readiness_service.compute_snapshot_await_count == 0


@pytest.mark.asyncio
async def test_mark_ready_for_review_rejects_readiness_snapshot_drift(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    service, _ss, _es, _rs = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id, version=state_record.version + 1),
    )

    with pytest.raises(CompositionNotRunnableError, match="composition changed"):
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )


@pytest.mark.asyncio
async def test_mark_ready_for_review_rejects_state_superseded_after_readiness_without_side_effects(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    service, _ss, _es, _readiness_service = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id, version=state_record.version),
    )
    original_store = payload_store.store
    store_calls: list[bytes] = []

    def tracking_store(content: bytes) -> str:
        store_calls.append(content)
        return original_store(content)

    async def supersede_after_readiness(self, **_kwargs):  # type: ignore[no-untyped-def]
        with session_engine_with_row.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=str(uuid4()),
                    session_id=str(session_record.id),
                    version=state_record.version + 1,
                    source=None,
                    sources=None,
                    nodes=[],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "Superseding state", "description": ""},
                    is_valid=True,
                    validation_errors=None,
                    composer_meta=None,
                    created_at=datetime.now(UTC),
                    derived_from_state_id=str(state_record.id),
                    provenance="tool_call",
                )
            )
        return self.snapshot

    monkeypatch.setattr(payload_store, "store", tracking_store)
    monkeypatch.setattr(_FakeReadinessService, "compute_snapshot", supersede_after_readiness)

    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
        )

    assert exc_info.value.reason == "readiness_state_drift"
    assert store_calls == []
    assert observed_value(service._telemetry.session_completed_total) == 0
    with session_engine_with_row.connect() as conn:
        assert conn.execute(select(composer_completion_events_table)).all() == []


@pytest.mark.asyncio
async def test_mark_ready_for_review_stale_authority_has_no_audit_blob_token_or_telemetry(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    authority = SQLiteLocalSessionOperationAuthority(session_engine_with_row)
    authority.release(session_operation_context)
    successor = authority.acquire(
        session_id=session_record.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="shareable-review-test-successor",
        lease_seconds=30,
    )
    store_calls: list[bytes] = []
    monkeypatch.setattr(payload_store, "store", lambda content: store_calls.append(content))
    try:
        with pytest.raises(SessionOperationFenceLost):
            await service.mark_ready_for_review(
                session_id=session_record.id,
                user_id=session_record.user_id,
                username=_OWNER_USERNAME,
                session_operation_context=session_operation_context,
            )
    finally:
        authority.release(successor)

    assert store_calls == []
    assert observed_value(service._telemetry.session_completed_total) == 0
    with session_engine_with_row.connect() as conn:
        assert conn.execute(select(composer_completion_events_table)).all() == []


@pytest.mark.asyncio
async def test_resolve_token_returns_frozen_snapshot(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """resolve_token returns the mark-time audit_readiness even if the live state shifts."""
    mark_time_snapshot = _readiness_snapshot(session_record.id, version=3)
    service, _ss, _es, readiness_service = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=mark_time_snapshot,
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    # Mutate the readiness fake so a re-fetch would return a different snapshot.
    later_snapshot = _readiness_snapshot(session_record.id, version=99)
    readiness_service.snapshot = later_snapshot
    readiness_service.reset_counts()

    resolved = await service.resolve_token(token=response.token, requesting_user_id="bob")
    # Frozen-at-mark-time: composition_version reflects the mark-time view, not the later one.
    assert resolved.audit_readiness.composition_version == 3
    # The readiness service was NOT called during resolve.
    assert readiness_service.compute_snapshot_await_count == 0


@pytest.mark.asyncio
async def test_resolve_token_projects_signed_blob_without_mutating_evidence(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Signed blobs are projected only after signature/digest verification."""
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    private_path = "/srv/elspeth/blobs/alice/private.csv"
    private_output = "/srv/elspeth/outputs/alice/private.csv"
    private_index = "/srv/elspeth/indexes/alice/private"
    blob_id = "98b1357d-5aab-4fb3-85b4-5ad643912e84"
    signed_readiness = _readiness_snapshot(session_record.id)
    secret_row = signed_readiness.rows[-1].model_copy(update={"detail": "17 secret(s) in your inventory"})
    signed_readiness = signed_readiness.model_copy(update={"rows": (*signed_readiness.rows[:-1], secret_row)})
    signed_blob = {
        "pipeline_metadata": {"name": "Legacy", "description": ""},
        "composition_snapshot": {
            "version": 3,
            "metadata": {"name": "Legacy", "description": ""},
            "sources": {
                "source": {
                    "plugin": "csv",
                    "on_success": "llm_in",
                    "options": {
                        "path": private_path,
                        "blob_ref": blob_id,
                        "mode": "bind_source",
                        "schema": {"mode": "observed"},
                    },
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "llm",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "llm_in",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": {
                        "prompt_template": "{{ lookup.path }}",
                        "lookup": {"path": "north", "file": "case.txt", "mode": "bind_source"},
                        "provider_config": {"persist_directory": private_index},
                    },
                }
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "main",
                    "plugin": "csv",
                    "options": {"path": private_output},
                    "on_write_failure": "discard",
                }
            ],
        },
        "yaml": f"private_path: {private_path}\nprivate_blob: {blob_id}\n",
        "audit_readiness": signed_readiness.model_dump(mode="json"),
        "created_by_user_id": "alice",
        "created_by_username": "Alice",
        "compartment_id": None,
    }
    signed_bytes = canonical_json(signed_blob).encode()
    digest_hex = payload_store.store(signed_bytes)
    token = signer.sign(
        ShareTokenPayload(
            version=1,
            session_id=session_record.id,
            state_id=state_record.id,
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            nonce_hex="ab" * 16,
            payload_digest=f"sha256:{digest_hex}",
            created_by_user_id="alice",
        )
    )

    resolved = await service.resolve_token(token=token, requesting_user_id="reviewer")

    serialized = resolved.model_dump_json()
    for private_value in (private_path, private_output, private_index, blob_id, "17 secret(s) in your inventory"):
        assert private_value not in serialized
    expected_lookup = {"path": "north", "file": "case.txt", "mode": "bind_source"}
    assert resolved.composition_snapshot.nodes[0].options["lookup"] == expected_lookup
    assert yaml.safe_load(resolved.yaml)["transforms"][0]["options"]["lookup"] == expected_lookup
    assert payload_store.retrieve(digest_hex) == signed_bytes


@pytest.mark.asyncio
async def test_mark_ready_freezes_username_alongside_the_opaque_identity(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context,
):
    """The snapshot carries BOTH attribution fields, and resolve surfaces both.

    ``created_by_user_id`` is the opaque identity id the token signature
    binds; it names nobody to a recipient who has no login. The username is
    frozen next to it so the shared view can say who shared the pipeline.
    """
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )

    blob = json.loads(payload_store.retrieve(response.payload_digest.removeprefix("sha256:")))
    assert blob["created_by_user_id"] == session_record.user_id
    assert blob["created_by_username"] == _OWNER_USERNAME

    resolved = await service.resolve_token(token=response.token, requesting_user_id="bob")
    assert resolved.created_by_user_id == session_record.user_id
    assert resolved.created_by_username == _OWNER_USERNAME


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_key", ["created_by_username", "compartment_id"])
async def test_resolve_token_rejects_obsolete_signed_blob_shape(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context,
    missing_key: str,
):
    """An old signed shape cannot enter the current public projection."""
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    marked = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    blob = json.loads(payload_store.retrieve(marked.payload_digest.removeprefix("sha256:")))
    del blob[missing_key]
    obsolete_hex = payload_store.store(canonical_json(blob).encode())
    now = datetime.now(UTC)
    obsolete_token = signer.sign(
        ShareTokenPayload(
            version=1,
            session_id=session_record.id,
            state_id=state_record.id,
            created_at=now,
            expires_at=now + timedelta(days=1),
            nonce_hex="ab" * 16,
            payload_digest=f"sha256:{obsolete_hex}",
            created_by_user_id=session_record.user_id,
        )
    )

    with pytest.raises(InvalidToken, match="unsupported share snapshot shape"):
        await service.resolve_token(token=obsolete_token, requesting_user_id="bob")


@pytest.mark.asyncio
async def test_resolve_token_rejects_tampered_token(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    tampered = response.token[:-2] + ("aa" if response.token[-2:] != "aa" else "bb")
    with pytest.raises(InvalidToken):
        await service.resolve_token(token=tampered, requesting_user_id="bob")


@pytest.mark.asyncio
async def test_resolve_token_rejects_expired_token(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Override the lifetime to negative so the minted token is born expired."""
    snapshot = _readiness_snapshot(session_record.id)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    service._settings.shareable_link_lifetime_seconds = -1  # type: ignore[attr-defined]
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    with pytest.raises(InvalidToken, match="expired"):
        await service.resolve_token(token=response.token, requesting_user_id="bob")


@pytest.mark.asyncio
async def test_resolve_token_blob_expired_raises_not_found(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Token verifies but payload_store has expired the blob → 404 path."""
    snapshot = _readiness_snapshot(session_record.id)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    # Delete the blob.
    digest_hex = response.payload_digest.removeprefix("sha256:")
    payload_store.delete(digest_hex)
    with pytest.raises(PayloadNotFoundError):
        await service.resolve_token(token=response.token, requesting_user_id="bob")


@pytest.mark.asyncio
async def test_resolve_token_does_not_call_readiness_service(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Frozen-at-mark-time discipline: resolve never calls compute_snapshot."""
    snapshot = _readiness_snapshot(session_record.id)
    service, _ss, _es, readiness_service = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=snapshot,
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    readiness_service.reset_counts()
    await service.resolve_token(token=response.token, requesting_user_id="bob")
    assert readiness_service.compute_snapshot_await_count == 0


# ── Snapshot v2: import opt-in, frozen files, per-link lifespan ─────────

_INLINE_SHA = "c" * 64


def _inline_marker(blob_id: UUID) -> dict[str, str]:
    return {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": _INLINE_SHA}


def _file_bound_state(state_record: _StateRecord, *, source_blob: UUID, node_value: object) -> _StateRecord:
    """A state whose source reads ``source_blob`` and whose node carries ``node_value``."""
    return replace(
        state_record,
        source={
            "plugin": "csv",
            "on_success": "lookup_in",
            "options": {
                "path": f"/srv/elspeth/blobs/alice/{source_blob}.csv",
                "blob_ref": str(source_blob),
                "schema": {"mode": "observed"},
            },
            "on_validation_failure": "discard",
        },
        nodes=[
            {
                "id": "lookup",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "lookup_in",
                "on_success": "main",
                "on_error": "discard",
                "options": {"tables": {"t": {"reference_content": node_value}}},
            }
        ],
        outputs=[
            {
                "name": "main",
                "plugin": "csv",
                "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
    )


def _completion_rows(engine, session_id: UUID) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session_id))
            ).all()
        )


def _stored_snapshot(payload_store: FilesystemPayloadStore, payload_digest: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(payload_store.retrieve(payload_digest.removeprefix("sha256:")))
    return loaded


def _track_store(payload_store: FilesystemPayloadStore, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    original_store = payload_store.store
    calls: list[bytes] = []

    def tracking_store(content: bytes) -> str:
        calls.append(content)
        return original_store(content)

    monkeypatch.setattr(payload_store, "store", tracking_store)
    return calls


@pytest.mark.asyncio
async def test_mark_ready_without_import_writes_nine_key_snapshot_with_empty_files(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=b"id\n1\n")
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value="plain"),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )

    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )

    blob = _stored_snapshot(payload_store, response.payload_digest)
    assert set(blob) == {
        "pipeline_metadata",
        "composition_snapshot",
        "yaml",
        "audit_readiness",
        "created_by_user_id",
        "created_by_username",
        "compartment_id",
        "import_allowed",
        "files",
    }
    assert blob["import_allowed"] is False
    assert blob["files"] == []
    # A view-only share never touches the uploaded files.
    assert blob_service.contexts == []
    assert not payload_store.exists(hashlib.sha256(b"id\n1\n").hexdigest())


@pytest.mark.asyncio
async def test_mark_ready_with_import_freezes_each_referenced_file_once(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    content = b"id,name\n1,Ada\n"
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=content)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value=_inline_marker(blob_id)),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )
    store_calls = _track_store(payload_store, monkeypatch)

    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        allow_import=True,
    )

    record = blob_service.records[blob_id]
    assert record.content_hash is not None
    assert payload_store.exists(record.content_hash)
    assert payload_store.retrieve(record.content_hash) == content
    # One read for one blob, even though it is bound twice, all under the
    # caller's COMPOSE context.
    assert blob_service.reads == [blob_id]
    assert set(blob_service.contexts) == {session_operation_context}
    # Files land before the snapshot: the snapshot is the last thing stored.
    assert store_calls[0] == content
    assert hashlib.sha256(store_calls[-1]).hexdigest() == response.payload_digest.removeprefix("sha256:")

    blob = _stored_snapshot(payload_store, response.payload_digest)
    assert blob["import_allowed"] is True
    assert blob["files"] == [
        {
            "sha256": record.content_hash,
            "size_bytes": len(content),
            "filename": "customers.csv",
            "mime_type": "text/csv",
            "bindings": [
                {"kind": "source_blob_ref", "source_name": "source", "row_index": None, "field_path": None, "encoding": None},
                {
                    "kind": "inline_content",
                    "source_name": None,
                    "row_index": None,
                    "field_path": "node:lookup.options.tables.t.reference_content",
                    "encoding": "utf-8",
                },
            ],
        }
    ]
    # The private manifest never names the owner's blob identity or storage path.
    serialized = json.dumps(blob["files"])
    assert str(blob_id) not in serialized
    assert "/srv/elspeth" not in serialized


@pytest.mark.asyncio
async def test_mark_ready_with_import_sorts_files_by_sha256(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    blob_service = _FakeBlobService()
    first = blob_service.add(session_record.id, filename="a.csv", content=b"alpha\n")
    second = blob_service.add(session_record.id, filename="b.txt", content=b"beta\n", mime_type="text/plain")
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=first, node_value=_inline_marker(second)),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )

    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        allow_import=True,
    )

    files = _stored_snapshot(payload_store, response.payload_digest)["files"]
    assert [entry["sha256"] for entry in files] == sorted([hashlib.sha256(b"alpha\n").hexdigest(), hashlib.sha256(b"beta\n").hexdigest()])
    assert {entry["filename"] for entry in files} == {"a.csv", "b.txt"}


async def _assert_refused_without_side_effects(
    service: ShareableReviewService,
    *,
    engine,
    payload_store: FilesystemPayloadStore,
    session_record: _SessionRecord,
    session_operation_context: SessionOperationContext,
    store_calls: list[bytes],
    reason: str,
    detail_fragment: str,
) -> None:
    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
            allow_import=True,
        )
    assert exc_info.value.reason == reason
    assert detail_fragment in exc_info.value.detail
    assert _completion_rows(engine, session_record.id) == []
    assert store_calls == []
    assert observed_value(service._telemetry.session_completed_total) == 0


@pytest.mark.asyncio
async def test_mark_ready_with_import_refuses_llm_authored_file(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    blob_service = _FakeBlobService()
    user_blob = blob_service.add(session_record.id, filename="customers.csv", content=b"id\n1\n")
    llm_blob = blob_service.add(
        session_record.id,
        filename="notes.txt",
        content=b"assistant wrote this\n",
        mime_type="text/plain",
        creation_modality=CreationModality.LLM_GENERATED,
    )
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=user_blob, node_value=_inline_marker(llm_blob)),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )
    store_calls = _track_store(payload_store, monkeypatch)

    await _assert_refused_without_side_effects(
        service,
        engine=session_engine_with_row,
        payload_store=payload_store,
        session_record=session_record,
        session_operation_context=session_operation_context,
        store_calls=store_calls,
        reason="file_not_shareable",
        detail_fragment="'notes.txt'",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "detail_fragment"),
    [
        pytest.param({"status": "pending"}, "not ready", id="pending"),
        pytest.param({"status": "error"}, "not ready", id="error"),
        pytest.param({"content_hash": None}, "no recorded content hash", id="no-hash"),
        pytest.param({"content_hash": "d" * 64}, "does not match", id="hash-mismatch"),
        pytest.param({"size_bytes": 999}, "does not match", id="size-mismatch"),
    ],
)
async def test_mark_ready_with_import_refuses_not_ready_or_hash_mismatched_file(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    detail_fragment: str,
):
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=b"id\n1\n", **overrides)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value="plain"),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )
    store_calls = _track_store(payload_store, monkeypatch)

    await _assert_refused_without_side_effects(
        service,
        engine=session_engine_with_row,
        payload_store=payload_store,
        session_record=session_record,
        session_operation_context=session_operation_context,
        store_calls=store_calls,
        reason="file_not_shareable",
        detail_fragment=detail_fragment,
    )
    if overrides.get("status") in {"pending", "error"}:
        # A blob that is not ready is refused from its row; its bytes are never read.
        assert blob_service.reads == []


@pytest.mark.asyncio
async def test_mark_ready_with_import_refuses_missing_file(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=uuid4(), node_value="plain"),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=_FakeBlobService(),
    )
    store_calls = _track_store(payload_store, monkeypatch)

    await _assert_refused_without_side_effects(
        service,
        engine=session_engine_with_row,
        payload_store=payload_store,
        session_record=session_record,
        session_operation_context=session_operation_context,
        store_calls=store_calls,
        reason="file_not_shareable",
        detail_fragment="source 'source'",
    )


@pytest.mark.asyncio
async def test_mark_ready_with_import_refuses_marker_in_list(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=b"id\n1\n")
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value=[_inline_marker(blob_id)]),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )
    store_calls = _track_store(payload_store, monkeypatch)

    await _assert_refused_without_side_effects(
        service,
        engine=session_engine_with_row,
        payload_store=payload_store,
        session_record=session_record,
        session_operation_context=session_operation_context,
        store_calls=store_calls,
        reason="file_not_shareable",
        detail_fragment="inside a list",
    )


@pytest.mark.asyncio
async def test_mark_ready_lifetime_sets_expiry(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )

    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        lifetime_seconds=86400,
    )

    payload = signer.verify(response.token)
    assert payload.expires_at == payload.created_at + timedelta(seconds=86400)
    assert response.expires_at == payload.expires_at
    (row,) = _completion_rows(session_engine_with_row, session_record.id)
    assert row.expires_at - row.created_at == timedelta(seconds=86400)


@pytest.mark.asyncio
async def test_mark_ready_lifetime_above_maximum_is_refused(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        settings=_FakeSettings(shareable_link_lifetime_seconds=604800),
    )
    store_calls = _track_store(payload_store, monkeypatch)

    with pytest.raises(CompositionNotRunnableError) as exc_info:
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
            lifetime_seconds=2592000,
        )

    assert exc_info.value.reason == "lifetime_exceeds_maximum"
    assert _completion_rows(session_engine_with_row, session_record.id) == []
    assert store_calls == []


@pytest.mark.asyncio
async def test_get_shareable_link_keeps_the_original_lifespan(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        lifetime_seconds=86400,
    )

    before = datetime.now(UTC)
    reminted = await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)
    after = datetime.now(UTC)

    assert before + timedelta(seconds=86400) <= reminted.expires_at <= after + timedelta(seconds=86400)
    payload = signer.verify(reminted.token)
    assert payload.expires_at - payload.created_at == timedelta(seconds=86400)


@pytest.mark.asyncio
async def test_preview_lists_each_file_once_with_every_use(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=b"id,name\n1,Ada\n")
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value=_inline_marker(blob_id)),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )

    response = await service.preview_shareable_files(session_id=session_record.id, session_operation_context=session_operation_context)

    assert response.importable is True
    assert response.not_importable_reason is None
    assert [(f.filename, f.size_bytes, f.used_by) for f in response.files] == [
        ("customers.csv", len(b"id,name\n1,Ada\n"), ["source 'source'", "node 'lookup' (tables.t.reference_content)"])
    ]
    assert blob_service.reads == [blob_id]
    assert set(blob_service.contexts) == {session_operation_context}


@pytest.mark.asyncio
async def test_preview_reports_llm_authored_file_as_not_importable(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    blob_service = _FakeBlobService()
    user_blob = blob_service.add(session_record.id, filename="customers.csv", content=b"id\n1\n")
    llm_blob = blob_service.add(
        session_record.id,
        filename="notes.txt",
        content=b"assistant wrote this\n",
        mime_type="text/plain",
        creation_modality=CreationModality.LLM_GENERATED,
    )
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=user_blob, node_value=_inline_marker(llm_blob)),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )

    response = await service.preview_shareable_files(session_id=session_record.id, session_operation_context=session_operation_context)

    assert response.importable is False
    assert response.not_importable_reason == "'notes.txt' was written by the assistant and cannot be shared for import"
    assert sorted(f.filename for f in response.files) == ["customers.csv", "notes.txt"]


def _v2_blob(**overrides: object) -> dict[str, Any]:
    blob: dict[str, Any] = {
        "pipeline_metadata": {"name": "Demo", "description": ""},
        "composition_snapshot": {},
        "yaml": "",
        "audit_readiness": {},
        "created_by_user_id": "alice",
        "created_by_username": "Alice",
        "compartment_id": None,
        "import_allowed": True,
        "files": [
            {
                "sha256": "a" * 64,
                "size_bytes": 5,
                "filename": "customers.csv",
                "mime_type": "text/csv",
                "bindings": [{"kind": "source_blob_ref", "source_name": "source", "row_index": None, "field_path": None, "encoding": None}],
            }
        ],
    }
    blob.update(overrides)
    return blob


def test_parse_blob_reads_legacy_seven_key_snapshot_as_view_only() -> None:
    legacy = _v2_blob()
    del legacy["import_allowed"]
    del legacy["files"]

    parsed = ShareableReviewService._parse_blob(canonical_json(legacy).encode())

    assert isinstance(parsed, ParsedSnapshot)
    assert parsed.import_allowed is False
    assert parsed.files == ()
    assert parsed.blob["created_by_username"] == "Alice"


def test_parse_blob_reads_v2_snapshot_files() -> None:
    parsed = ShareableReviewService._parse_blob(canonical_json(_v2_blob()).encode())

    assert parsed.import_allowed is True
    assert [entry["filename"] for entry in parsed.files] == ["customers.csv"]
    assert parsed.files[0]["sha256"] == "a" * 64


def _file_entry(**overrides: object) -> dict[str, Any]:
    entry: dict[str, Any] = dict(_v2_blob()["files"][0])
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"import_allowed": 1}, id="import-allowed-int"),
        pytest.param({"import_allowed": "true"}, id="import-allowed-str"),
        pytest.param({"import_allowed": None}, id="import-allowed-null"),
        pytest.param({"files": {}}, id="files-not-list"),
        pytest.param({"files": None}, id="files-null"),
        pytest.param({"import_allowed": False}, id="view-only-with-files"),
        pytest.param({"files": ["x"]}, id="entry-not-object"),
        pytest.param({"files": [{k: v for k, v in _file_entry().items() if k != "mime_type"}]}, id="entry-missing-key"),
        pytest.param({"files": [_file_entry(extra=1)]}, id="entry-extra-key"),
        pytest.param({"files": [_file_entry(sha256="A" * 64)]}, id="sha-uppercase"),
        pytest.param({"files": [_file_entry(sha256="a" * 63)]}, id="sha-short"),
        pytest.param({"files": [_file_entry(size_bytes=True)]}, id="size-bool"),
        pytest.param({"files": [_file_entry(size_bytes=-1)]}, id="size-negative"),
        pytest.param({"files": [_file_entry(filename="")]}, id="filename-blank"),
        pytest.param({"files": [_file_entry(filename=7)]}, id="filename-int"),
        pytest.param({"files": [_file_entry(mime_type="application/x-msdownload")]}, id="mime-unknown"),
        pytest.param({"files": [_file_entry(bindings=[])]}, id="bindings-empty"),
        pytest.param({"files": [_file_entry(bindings="source")]}, id="bindings-not-list"),
        pytest.param({"files": [_file_entry(bindings=[{"kind": "source_blob_ref"}])]}, id="binding-malformed"),
        pytest.param({"files": [_file_entry(sha256="b" * 64), _file_entry()]}, id="files-unsorted"),
        pytest.param({"files": [_file_entry(), _file_entry()]}, id="files-duplicate-sha"),
    ],
)
def test_parse_blob_rejects_non_bool_import_allowed_and_malformed_files(overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidToken):
        ShareableReviewService._parse_blob(canonical_json(_v2_blob(**overrides)).encode())


@pytest.mark.parametrize("missing_key", ["import_allowed", "files"])
def test_parse_blob_rejects_half_upgraded_snapshot(missing_key: str) -> None:
    blob = _v2_blob()
    del blob[missing_key]
    with pytest.raises(InvalidToken, match="unsupported share snapshot shape"):
        ShareableReviewService._parse_blob(canonical_json(blob).encode())


@pytest.mark.asyncio
async def test_load_snapshot_returns_verified_payload_and_frozen_manifest(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=b"id\n1\n")
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value="plain"),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )
    marked = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        allow_import=True,
    )

    payload, parsed = await service.load_snapshot(token=marked.token)

    assert payload.payload_digest == marked.payload_digest
    assert parsed.import_allowed is True
    assert [entry["sha256"] for entry in parsed.files] == [blob_service.records[blob_id].content_hash]
    with pytest.raises(InvalidToken):
        await service.load_snapshot(token=marked.token[:-2] + ("aa" if marked.token[-2:] != "aa" else "bb"))


@pytest.mark.asyncio
async def test_mark_ready_with_import_stores_files_before_the_audit_row(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    """A file that cannot be frozen leaves no audited share and no token."""
    content = b"id\n1\n"
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=content)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value="plain"),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )

    class _FileStoreBoom(Exception): ...

    original_store = payload_store.store

    def failing_file_store(data: bytes) -> str:
        if data == content:
            raise _FileStoreBoom("payload store refused the file")
        return original_store(data)

    monkeypatch.setattr(payload_store, "store", failing_file_store)

    with pytest.raises(_FileStoreBoom):
        await service.mark_ready_for_review(
            session_id=session_record.id,
            user_id=session_record.user_id,
            username=_OWNER_USERNAME,
            session_operation_context=session_operation_context,
            allow_import=True,
        )

    assert _completion_rows(session_engine_with_row, session_record.id) == []


@pytest.mark.asyncio
async def test_get_shareable_link_clamps_to_a_lowered_maximum(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """A re-mint never issues a link longer than the current maximum."""
    settings = _FakeSettings(shareable_link_lifetime_seconds=2592000)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        settings=settings,
    )
    await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        lifetime_seconds=2592000,
    )
    settings.shareable_link_lifetime_seconds = 604800

    before = datetime.now(UTC)
    reminted = await service.get_shareable_link(session_id=session_record.id, user_id=session_record.user_id)
    after = datetime.now(UTC)

    assert before + timedelta(seconds=604800) <= reminted.expires_at <= after + timedelta(seconds=604800)
    payload = signer.verify(reminted.token)
    assert payload.expires_at - payload.created_at == timedelta(seconds=604800)


@pytest.mark.asyncio
async def test_mark_ready_view_only_still_works_with_llm_authored_file(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    """Assistant-written files only block import; a view-only share is unaffected."""
    blob_service = _FakeBlobService()
    user_blob = blob_service.add(session_record.id, filename="customers.csv", content=b"id\n1\n")
    llm_blob = blob_service.add(
        session_record.id,
        filename="notes.txt",
        content=b"assistant wrote this\n",
        mime_type="text/plain",
        creation_modality=CreationModality.LLM_GENERATED,
    )
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=user_blob, node_value=_inline_marker(llm_blob)),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )

    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        allow_import=False,
    )

    blob = _stored_snapshot(payload_store, response.payload_digest)
    assert blob["import_allowed"] is False
    assert blob["files"] == []
    assert blob_service.contexts == []
    assert blob_service.reads == []
    assert len(_completion_rows(session_engine_with_row, session_record.id)) == 1


@pytest.mark.asyncio
async def test_resolve_token_exposes_import_flag_and_file_names_only(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
):
    content = b"id,name\n1,Ada\n"
    blob_service = _FakeBlobService()
    blob_id = blob_service.add(session_record.id, filename="customers.csv", content=content)
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=_file_bound_state(state_record, source_blob=blob_id, node_value="plain"),
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        blob_service=blob_service,
    )
    marked = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
        allow_import=True,
    )

    resolved = await service.resolve_token(token=marked.token, requesting_user_id="reviewer")

    assert resolved.import_allowed is True
    assert [(f.filename, f.size_bytes) for f in resolved.files] == [("customers.csv", len(content))]
    dumped = resolved.model_dump_json()
    content_hash = blob_service.records[blob_id].content_hash
    assert content_hash is not None
    assert content_hash not in dumped
    assert "sha256" not in dumped
    assert "mime_type" not in dumped
    assert "bindings" not in dumped


@pytest.mark.asyncio
async def test_resolve_token_legacy_snapshot_is_view_only_with_no_files(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
):
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
    )
    marked = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    payload, current = await service.load_snapshot(token=marked.token)
    legacy = ParsedSnapshot(blob=current.blob, import_allowed=False, files=())

    async def _load_legacy(*, token: str) -> tuple[Any, ParsedSnapshot]:
        return payload, legacy

    monkeypatch.setattr(service, "load_snapshot", _load_legacy)

    resolved = await service.resolve_token(token=marked.token, requesting_user_id="reviewer")

    assert resolved.import_allowed is False
    assert resolved.files == []

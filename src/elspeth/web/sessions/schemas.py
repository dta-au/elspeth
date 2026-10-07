"""Pydantic request/response models for all session API endpoints.

Response models in this module serialize **system-owned data** (Tier 1 in
docs/guides/data-trust-and-error-handling.md §The Three-Tier Trust Model).
They inherit from ``_StrictResponse`` so that
coercion and unknown fields crash rather than silently passing through —
the Landscape record and the HTTP response must agree exactly.

Request models keep normal ``BaseModel`` coercion semantics: client input
is Tier 3 and the boundary-layer coercion rules (documented in
docs/guides/data-trust-and-error-handling.md §Coercion Rules by Plugin Type)
apply.  They still reject unknown keys
mechanically so stale or typoed client payloads fail closed at the HTTP
boundary instead of being silently reinterpreted by the route layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import pydantic
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationKind,
    InterpretationSource,
    InterpretationSurfaceOrigin,
)
from elspeth.contracts.composer_progress import ComposerProgressPhase, ComposerProgressReason
from elspeth.contracts.tool_calls import PROVIDER_TOOL_CALL_ID_MAX_LENGTH
from elspeth.web.execution.schemas import (
    DiscardSummary,
    RunAccounting,
    RunAccountingCorruption,
    check_discard_summary_reconciliation,
)
from elspeth.web.sessions.composer_operations import (
    ComposerOperationError as ComposerOperationError,
)
from elspeth.web.sessions.composer_operations import (
    ComposerOperationKind,
    ComposerOperationStatus,
    require_canonical_operation_id,
)
from elspeth.web.sessions.protocol import (
    ComposerDensityDefault,
    ComposerTrustMode,
    ProposalEventType,
    ProposalLifecycleStatus,
    SessionRunStatus,
)
from elspeth.web.validation import (
    _validate_accepted_value_content,
    has_visible_content,
)


class _StrictResponse(BaseModel):
    """Base model for session response schemas — Tier 1 trust rules.

    ``strict=True`` rejects silent coercion (``"7"`` into an ``int`` field
    crashes instead of becoming ``7``).  ``extra="forbid"`` rejects
    unknown fields instead of dropping them.  Both are required for the
    audit-trail integrity contract: the HTTP response must not contain
    values the backend never emitted, and must not silently hide values
    the backend did emit.
    """

    model_config = ConfigDict(strict=True, extra="forbid")


class _RequestModel(BaseModel):
    """Tier 3 request base: allow coercion, reject unknown keys."""

    model_config = ConfigDict(extra="forbid")


class _SessionOperationRequest(BaseModel):
    """Strict boundary shared by retry-safe composer mutations."""

    model_config = ConfigDict(strict=True, extra="forbid")

    operation_id: str = pydantic.Field(min_length=36, max_length=36)

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("operation_id must be a canonical UUID") from exc
        if str(parsed) != value:
            raise ValueError("operation_id must be a canonical UUID")
        return value


def _require_visible_content(value: str, *, field_label: str) -> str:
    """Reject strings that contain no visible characters."""
    if not has_visible_content(value):
        raise ValueError(f"{field_label} must contain at least one visible character")
    return value


class CreateSessionRequest(_RequestModel):
    """Request body for POST /api/sessions.

    ``title`` is optional: when omitted (or explicitly null) the route
    mints the app-wide default ("Session — 2 Jul 2026", auto-disambiguated
    per user — see ``elspeth.web.sessions.titles``). The default is minted
    server-side so every client shares one naming convention
    (elspeth-ef8c18a6cb killed the frontend's competing "New session" /
    "Untitled" defaults).
    """

    title: str | None = pydantic.Field(default=None, min_length=1)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_visible_content(value, field_label="Session title")


class UpdateSessionRequest(_RequestModel):
    """Request body for PATCH /api/sessions/{id}."""

    title: str = pydantic.Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _require_visible_content(value, field_label="Session title")


class SessionResponse(_StrictResponse):
    """Response for session CRUD operations."""

    id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    archived: bool = False
    forked_from_session_id: str | None = None
    forked_from_message_id: str | None = None


def _canonical_request_uuid(value: object) -> UUID | None:
    if value is None or type(value) is UUID:
        return value
    if type(value) is not str:
        raise ValueError("Expected a canonical UUID")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("Expected a canonical UUID")
    return parsed


class SendMessageRequest(_SessionOperationRequest):
    """Immutable strict body for durable message admission."""

    content: str = Field(min_length=1, max_length=65536)
    state_id: UUID | None = None

    @field_validator("state_id", mode="before")
    @classmethod
    def _validate_state_id(cls, value: object) -> UUID | None:
        return _canonical_request_uuid(value)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        return _require_visible_content(value, field_label="Message content")


class RecomposeRequest(_SessionOperationRequest):
    """Immutable retry target and the exact head seen at the retry click."""

    expected_user_message_id: UUID
    state_id: UUID | None = None

    @field_validator("expected_user_message_id", "state_id", mode="before")
    @classmethod
    def _validate_uuid(cls, value: object) -> UUID | None:
        return _canonical_request_uuid(value)


type ToolCallObject = dict[str, JsonValue]
type ToolCallList = list[ToolCallObject]


class ChatMessageSegmentResponse(_StrictResponse):
    """One provenance-bearing visible segment in a chat message."""

    kind: Literal["text", "trusted_system_notice"]
    content: str


class ToolRejectionResponse(_StrictResponse):
    """The reason a refused composer tool call returned to the planner.

    Projected from ``composition_rejection_events`` (elspeth-3e28029d2f) onto
    ``role="tool"`` rows ONLY when the owner opts in with
    ``include_tool_rows=true&include_rejection_reasons=true`` on
    GET /api/sessions/{id}/messages (the access-logged audit-grade view). The
    chat tool row's ``content`` stays redacted; this is the unredacted
    session-data reason (operator ruling 2026-09-02). It never carries
    advisor output, and ``planner_payload`` is not projected.
    """

    tool_name: str
    error_code: str | None = None
    message: str
    composition_state_id: str | None = None
    created_at: datetime


class ChatMessageResponse(_StrictResponse):
    """Response for a single chat message.

    ``raw_content`` is the model's pre-synthesis prose for assistant turns
    where ``service._finalize_no_tool_response`` augmented the visible
    ``content`` with an operator-facing suffix or replaced it with a
    synthetic blocker message. It is ``null`` in the response by default
    (Tier 1 audit data; the conversation channel does not need it) and
    carries the original prose only when the caller passes
    ``include_raw_content=true`` on the GET endpoint. The field is always
    present in the response shape — Pydantic v2 with the default
    ``model_config`` does not enable ``exclude_none`` — so clients should
    test ``raw_content is not None`` rather than ``"raw_content" in body``.
    Eval/diagnosis tooling uses it to determine whether the model
    converged on useful output that the synthesizer hid.
    """

    id: str
    operation_id: str | None = None
    session_id: str
    role: str
    content: str
    raw_content: str | None = None
    segments: list[ChatMessageSegmentResponse]
    tool_calls: ToolCallList | None = None
    created_at: datetime
    composition_state_id: str | None = None
    tool_call_id: str | None = None
    parent_assistant_id: str | None = None
    sequence_no: int | None = None
    # Null unless the audit-grade view opts in with include_rejection_reasons
    # AND this is a tool row whose call was refused. Always present in the
    # shape (same posture as ``raw_content``): test ``rejection is not None``.
    rejection: ToolRejectionResponse | None = None


class MessageWithStateResponse(_StrictResponse):
    """Response for POST /api/sessions/{id}/messages.

    State is null when the composition version is unchanged; populated
    with the updated CompositionState when composition changes occur.
    """

    message: ChatMessageResponse
    state: CompositionStateResponse | None = None
    proposals: list[CompositionProposalResponse]


class ValidationEntryResponse(_StrictResponse):
    """A four-key projection of ``ValidationEntry.to_dict()`` for the HTTP surface.

    Carries ``component`` / ``message`` / ``severity`` / ``error_code`` only;
    the detail payloads (``contract``, ``row_union_schema``,
    ``coalesce_union_type``) and ``rejected_component`` are deliberately not
    on this surface. Constructed field by field in ``routes/_helpers``, so a
    new ``to_dict`` key never reaches it by accident — and never widens it
    either (elspeth-e405ad7cd2, systems ledger #44).
    """

    component: str
    message: str
    severity: str
    error_code: str | None = None


type CompositionObject = dict[str, JsonValue]
type CompositionObjectList = list[CompositionObject]


class ComposerPreferencesResponse(_StrictResponse):
    session_id: str
    trust_mode: ComposerTrustMode
    density_default: ComposerDensityDefault
    interpretation_review_disabled: bool
    updated_at: datetime


class UpdateComposerPreferencesRequest(_RequestModel):
    trust_mode: ComposerTrustMode
    density_default: ComposerDensityDefault


class PipelineProposalMetadataResponse(_StrictResponse):
    draft_hash: str
    base: CompositionObject
    repair_count: int
    skill_hash: str
    audit_payload_hash: str
    custody_result: Literal["not_required", "ready"]


class CompositionProposalResponse(_StrictResponse):
    id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    status: ProposalLifecycleStatus
    summary: str
    rationale: str
    affects: list[str]
    arguments_redacted_json: CompositionObject
    base_state_id: str | None = None
    committed_state_id: str | None = None
    audit_event_id: str | None = None
    pipeline_metadata: PipelineProposalMetadataResponse | None = None
    created_at: datetime
    updated_at: datetime


class AcceptProposalRequest(_RequestModel):
    draft_hash: str | None = None


class RejectProposalRequest(_RequestModel):
    reason: str | None = None


class ProposalEventResponse(_StrictResponse):
    id: str
    session_id: str
    proposal_id: str | None = None
    event_type: ProposalEventType
    actor: str
    payload: CompositionObject
    created_at: datetime


class PluginPolicyFindingResponse(_StrictResponse):
    """Sanitized current-policy finding for one persisted component."""

    component_id: str
    plugin_id: str
    reason_code: str
    snapshot_fingerprint: str


class CompositionValidationErrorResponse(_StrictResponse):
    """Closed diagnostic identity; nullable fields remain required on the wire."""

    message: str
    error_code: str | None
    component: str | None


class CompositionStateResponse(_StrictResponse):
    """Response for composition state endpoints."""

    id: str
    session_id: str
    version: int
    sources: dict[str, CompositionObject] | None = None
    nodes: CompositionObjectList | None = None
    edges: CompositionObjectList | None = None
    outputs: CompositionObjectList | None = None
    metadata: CompositionObject | None = None
    is_valid: bool
    validation_errors: list[CompositionValidationErrorResponse] | None = None
    validation_warnings: list[ValidationEntryResponse] | None = None
    validation_suggestions: list[ValidationEntryResponse] | None = None
    derived_from_state_id: str | None = None
    created_at: datetime
    # Operational/audit metadata produced by the composer pipeline.
    # Known keys include ``repair_turns_used`` and ``implicit_decisions``.
    # ``None`` is honest for revert/fork paths and
    # for historical states written before this surface existed.
    composer_meta: CompositionObject | None = None
    plugin_policy_findings: list[PluginPolicyFindingResponse] = pydantic.Field(default_factory=list)


class ForkSessionRequest(_SessionOperationRequest):
    """Request body for POST /api/sessions/{id}/fork."""

    from_message_id: UUID
    new_message_content: str = pydantic.Field(min_length=1)

    @field_validator("from_message_id", mode="before")
    @classmethod
    def _parse_from_message_id(cls, value: object) -> UUID:
        if type(value) is str:
            try:
                parsed = UUID(value)
            except ValueError as exc:
                raise ValueError("from_message_id must be a canonical UUID") from exc
            if str(parsed) != value:
                raise ValueError("from_message_id must be a canonical UUID")
            return parsed
        if type(value) is UUID:
            return value
        raise ValueError("from_message_id must be a canonical UUID")

    @field_validator("new_message_content")
    @classmethod
    def _validate_new_message_content(cls, value: str) -> str:
        return _require_visible_content(value, field_label="Fork message content")


class ForkSessionResponse(_StrictResponse):
    """Immutable replay locator for POST /api/sessions/{id}/fork."""

    session_id: UUID


class RevertStateRequest(_SessionOperationRequest):
    """Request body for POST /api/sessions/{id}/state/revert."""

    state_id: UUID

    @field_validator("state_id", mode="before")
    @classmethod
    def _parse_state_id(cls, value: object) -> UUID:
        if type(value) is str:
            try:
                parsed = UUID(value)
            except ValueError as exc:
                raise ValueError("state_id must be a canonical UUID") from exc
            if str(parsed) != value:
                raise ValueError("state_id must be a canonical UUID")
            return parsed
        if type(value) is UUID:
            return value
        raise ValueError("state_id must be a canonical UUID")


class RunResponse(_StrictResponse):
    """Response for GET /api/sessions/{id}/runs."""

    id: str
    session_id: str
    status: SessionRunStatus
    accounting: RunAccounting | None = None
    # Explicit per-run integrity failure (elspeth-d5578ccd98): when the run's
    # recorded token outcomes fail canonical validation, the list surface
    # ships this marker INSTEAD of accounting so the corrupt run stays
    # visible — and visibly corrupt — while healthy runs keep theirs.
    accounting_corruption: RunAccountingCorruption | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    composition_version: int
    discard_summary: DiscardSummary | None = None

    @model_validator(mode="after")
    def _check_discard_reconciliation(self) -> RunResponse:
        """Session-list carrier of the accounting/discard-summary pair.

        This surface attaches ``discard_summary`` on its own path
        (``sessions/routes/runs.py``), independent of the ``/api/runs/*``
        carriers — a validator there does not protect this one
        (elspeth-43f52d69a4).
        """
        check_discard_summary_reconciliation(self.accounting, self.discard_summary)
        if self.accounting_corruption is not None and self.accounting is not None:
            # The corruption marker exists to REPLACE accounting; carrying
            # both would let a corrupt run present validated-looking numbers.
            raise ValueError("accounting_corruption and accounting are mutually exclusive")
        return self


# ---------------------------------------------------------------------------
# Phase 5b — interpretation-event wire schemas (Task 3)
# ---------------------------------------------------------------------------
#
# These models mirror the Phase 5b interpretation-event contract types
# (``elspeth.contracts.composer_interpretation``).  The contract dataclass
# is the read-side type used inside the service layer; these pydantic
# models are the wire-side types used by HTTP routes.  Per project
# convention, both sides exist so that:
#
# - The DB row → record → HTTP response path passes through three
#   independent gates (DB CHECK constraint, dataclass validator,
#   pydantic strict-mode validator), each rejecting bad data with a
#   crash rather than silently coercing it.
# - The wire model can carry max-length caps without polluting the
#   contract dataclass (which models the DB row exactly).
#
# All field caps are documented in spec lines 1428-1459.


class InterpretationEventResponse(_StrictResponse):
    """Wire mirror of :class:`InterpretationEventRecord`.

    Read-side wire schema for a single row of the
    ``interpretation_events_table``.  Used by:

    - GET /api/sessions/{id}/interpretations  (list — wrapped in
      :class:`ListInterpretationEventsResponse`).
    - POST /api/sessions/{id}/interpretations/{event_id}/resolve  (the
      resolved row is returned alongside the new composition state in
      :class:`InterpretationResolveResponse`).

    The three structural row shapes (``user_approved``,
    ``auto_interpreted_opt_out``, ``auto_interpreted_no_surfaces``) are
    distinguished by ``interpretation_source``.  The nullable fields
    carry NULL for opted-out rows; ``llm_draft`` and ``user_term``
    additionally carry NULL for ``auto_interpreted_no_surfaces`` rows
    (rate-cap exhaustion — no draft was produced).  See the
    :class:`InterpretationEventRecord` docstring for the full per-shape
    field nullability table.

    ``frozen=True`` matches the read-side contract: clients receive an
    immutable view of an audit-table row.  ``extra="forbid"`` keeps the
    HTTP response surface aligned with the DB schema — adding a field
    requires a coordinated schema/contract/wire update.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    session_id: UUID
    # ``None`` for session-level ``auto_interpreted_opt_out`` marker rows
    # (no surfacing occurred). Surface-specific opt-out rows carry kind,
    # surface/provenance fields, accepted_value, arguments_hash, and
    # hash_domain_version='v2'.
    composition_state_id: UUID | None = None
    affected_node_id: str | None = Field(default=None, max_length=256)
    tool_call_id: str | None = Field(default=None, max_length=PROVIDER_TOOL_CALL_ID_MAX_LENGTH)
    user_term: str | None = Field(default=None, max_length=8192)
    kind: InterpretationKind | None = None
    llm_draft: str | None = Field(default=None, max_length=8192)
    accepted_value: str | None = Field(default=None, max_length=8192)
    choice: InterpretationChoice
    created_at: datetime
    resolved_at: datetime | None = None
    # Actor format mirrors ``ProposalEventRecord.actor``: originator:role:id
    # for request-scoped actors, system:{component} for system writers.
    actor: str = Field(min_length=1, max_length=256)
    interpretation_source: InterpretationSource
    # What raised the surface; NULL for rows with no surface (session-level
    # opt-out markers, ``auto_interpreted_no_surfaces``).
    surface_origin: InterpretationSurfaceOrigin | None = None
    # Audit-provenance fields — bound to which LLM produced the draft.
    # Exposed on the wire so the audit-readiness panel and any future
    # reviewer surface can render "drafted by claude-opus-4-7 v… on
    # 2026-05-18" without a second DB round-trip.  NULL when no LLM was
    # consulted: session-level opt-out markers, and surfaces a server route
    # raised (``surface_origin`` other than ``composer_llm``).
    model_identifier: str | None = Field(default=None, max_length=256)
    model_version: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=64)
    # hex SHA-256 of pipeline_composer.md content at draft time.
    composer_skill_hash: str | None = Field(default=None, max_length=64)
    # hex rfc8785-canonical hash over INTERPRETATION_HASH_DOMAIN_V2;
    # populated at resolve time, NULL until then and for opt-out rows.
    arguments_hash: str | None = Field(default=None, max_length=64)
    # ``v2`` once resolved (F-12); NULL until then and for marker opt-out rows.
    hash_domain_version: str | None = Field(default=None, max_length=16)
    # F-19: runtime model snapshot at resolve time (may differ from the
    # composer model that produced the draft if a model swap happened
    # between surfacing and resolution).
    runtime_model_identifier_at_resolve: str | None = Field(default=None, max_length=256)
    runtime_model_version_at_resolve: str | None = Field(default=None, max_length=128)
    # Cross-DB hash anchor (Option A): hex SHA-256 of the resolved
    # prompt-template string.  NULL until resolved; NULL for opted-out
    # rows (no prompt template is patched).  Exposed on the wire so
    # audit-tooling consumers can verify hash equality without a second
    # DB round-trip.
    approved_prompt_artifact_hash: str | None = Field(default=None, max_length=64)


class InterpretationResolveRequest(BaseModel):
    """Request body for POST /api/sessions/{id}/interpretations/{event_id}/resolve.

    Carries the user's resolution of a previously-surfaced interpretation
    event.  ``opted_out`` and ``abandoned`` are NOT valid resolve
    choices — opt-out goes through a separate route, and abandoned is
    written by the session-end cleanup job, not by user action.
    ``pending`` is the pre-resolve state and is also not a valid input.

    The ``amended_value`` field carries the user's edited interpretation
    when ``choice == "amended"``.  Schema-layer content checks
    (metacharacters, control chars, length caps, credential-shape
    prefilter) live in :func:`_validate_accepted_value_content` so the
    same regex set guards both the schema boundary (this validator) and
    the tool boundary (request_interpretation_review — Task 5).  The
    duplicated validation is intentional defense-in-depth for F-2
    (prompt-injection bypass on the accepted_as_drafted path).
    """

    model_config = ConfigDict(extra="forbid")

    choice: Literal["accepted_as_drafted", "amended"]
    # Optional from the wire perspective; the model_validator enforces
    # that ``amended`` requires it and ``accepted_as_drafted`` forbids
    # it.  ``max_length`` is the outer cap; the per-line cap (1024) is
    # enforced inside ``_validate_accepted_value_content`` to give a
    # specific error message rather than the generic ``max_length``
    # rejection.
    amended_value: str | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def _amended_value_consistency(self) -> InterpretationResolveRequest:
        # An empty string ("") is treated the same as None for the
        # required-when-amended check: a zero-length amendment cannot be
        # the user's intended interpretation — accepted_as_drafted is
        # the correct path for "no change".
        if self.choice == "amended" and not self.amended_value:
            raise ValueError("amended_value is required when choice == 'amended'")
        if self.choice == "accepted_as_drafted" and self.amended_value is not None:
            raise ValueError("amended_value must be omitted when choice == 'accepted_as_drafted'")
        return self

    @field_validator("amended_value", mode="after")
    @classmethod
    def _validate_amended_value_content(cls, v: str | None) -> str | None:
        """Reject template metacharacters, control chars, credential-shaped
        content, and overlength single-line strings.

        Delegates to :func:`_validate_accepted_value_content` so the
        schema layer and the tool boundary (Task 5) share one regex set
        for content checks.  Empty string and None bypass the content
        checks — the model_validator handles their consistency with the
        ``choice`` field.
        """
        if not v:
            return v
        _validate_accepted_value_content(v)
        return v


class InterpretationResolveResponse(_StrictResponse):
    """Response for POST /api/sessions/{id}/interpretations/{event_id}/resolve.

    The resolved event row PLUS the new composition state produced by
    patching the affected LLM transform (provenance:
    ``interpretation_resolve``).  Returning both in one envelope lets
    the frontend update its event-list view and its composition-state
    view atomically; a two-call shape would create a window where the
    UI shows a resolved event whose downstream pipeline patch is not
    yet visible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: InterpretationEventResponse
    new_state: CompositionStateResponse


class InterpretationOptOutResponse(_StrictResponse):
    """Response for POST /api/sessions/{id}/interpretations/opt_out.

    The opt-out route has no body fields beyond the implicit actor
    (carried by the auth middleware).  The response surfaces:

    - ``session_id`` — echoed so the caller can confirm the right
      session was modified.
    - ``interpretation_review_disabled`` — always ``True`` on success;
      surfaced explicitly so the caller can re-render the toggle
      without a follow-up GET.
    - ``opted_out_at`` — the persisted timestamp.  Tied to the
      ``interpretation_events_table`` opt-out row written in the same
      transaction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    interpretation_review_disabled: bool
    opted_out_at: datetime


class ListInterpretationEventsResponse(_StrictResponse):
    """Response for GET /api/sessions/{id}/interpretations.

    Wraps the list in an envelope object rather than returning a bare
    JSON array — consistent with every other list route on the session
    surface — so future
    pagination metadata can be added without a breaking wire change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: list[InterpretationEventResponse]


class OptOutSummaryResponse(_StrictResponse):
    """Response for GET /api/sessions/{id}/interpretations/opt_out_summary.

    Per F-22 of the Phase 5b backend spec: after a session has opted out
    of interpretation review, the composer-LLM continues to auto-bake
    interpretations (now flagged as ``auto_interpreted_opt_out``) and may
    also write ``auto_interpreted_no_surfaces`` rows when the rate cap is
    exhausted. This route lets a user retroactively review every
    auto-baked interpretation produced during the opted-out portion of
    the session, closing the audit gap of "click opt-out once, dozens of
    auto-interpretations accumulate invisibly."

    Returns rows of both ``auto_interpreted_opt_out`` and
    ``auto_interpreted_no_surfaces`` interpretation_source — the two
    structural row shapes that represent auto-baked interpretations —
    ordered by ``created_at``. ``user_approved`` rows are excluded; the
    standard ``GET /interpretations`` route is the right surface for
    those.

    Envelope shape matches :class:`ListInterpretationEventsResponse` so
    the two list routes have consistent wire ergonomics on the session
    surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: list[InterpretationEventResponse]


# Forward reference resolution
MessageWithStateResponse.model_rebuild()
ForkSessionResponse.model_rebuild()
InterpretationResolveResponse.model_rebuild()


class ComposerOperationAcceptedResponse(_StrictResponse):
    """Idempotent admission acknowledgement; the durable GET owns the answer."""

    operation_id: str = Field(min_length=36, max_length=36)
    kind: ComposerOperationKind
    status: ComposerOperationStatus
    poll_after_ms: int = Field(ge=100, le=60_000)

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: str) -> str:
        return require_canonical_operation_id(value)


class ComposerOperationStatusResponse(ComposerOperationAcceptedResponse):
    cancel_requested: bool
    deadline_at: datetime
    deadline_remaining_ms: int = Field(ge=0)
    result: MessageWithStateResponse | None = None
    error: ComposerOperationError | None = None

    @model_validator(mode="after")
    def _validate_status_bundle(self) -> ComposerOperationStatusResponse:
        if self.status in ("queued", "running"):
            if self.result is not None or self.error is not None:
                raise ValueError("Live operation cannot carry terminal result")
        elif self.status == "completed":
            if self.result is None or self.error is not None or self.cancel_requested or self.deadline_remaining_ms != 0:
                raise ValueError("Invalid completed operation response")
        elif self.error is None or self.result is not None or self.deadline_remaining_ms != 0:
            raise ValueError("Invalid failed operation response")
        return self


class ComposerOperationStreamStatusPayload(_StrictResponse):
    status: Literal["queued", "running"]
    cancel_requested: bool
    deadline_remaining_ms: int = Field(ge=0)


class ComposerOperationStreamTerminalPayload(_StrictResponse):
    status: Literal["completed", "failed"]


class ComposerOperationStreamProgressPayload(_StrictResponse):
    """Existing provider-safe vocabulary, bound to exactly one worker lease."""

    session_operation_id: str = Field(min_length=1, max_length=128)
    session_operation_epoch: int = Field(ge=1)
    request_token: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    phase: ComposerProgressPhase
    headline: str = Field(min_length=1, max_length=180)
    evidence: tuple[str, ...] = Field(default=(), max_length=4)
    likely_next: str | None = Field(default=None, min_length=1, max_length=180)
    reason: ComposerProgressReason | None = None
    updated_at: datetime

    @field_validator("evidence")
    @classmethod
    def _validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not 1 <= len(item) <= 180 for item in value):
            raise ValueError("Progress evidence must be bounded")
        return value

    @model_validator(mode="after")
    def _validate_reason(self) -> ComposerOperationStreamProgressPayload:
        if self.phase in ("failed", "cancelled") and self.reason is None:
            raise ValueError("Failed/cancelled progress requires a reason")
        return self


class _ComposerOperationStreamFrame(_StrictResponse):
    schema_version: Literal["composer-operation-stream.v1"] = "composer-operation-stream.v1"
    session_id: str = Field(min_length=36, max_length=36)
    operation_id: str = Field(min_length=36, max_length=36)
    sequence: int = Field(ge=0)

    @field_validator("session_id", "operation_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return require_canonical_operation_id(value)


class ComposerOperationStreamStatusFrame(_ComposerOperationStreamFrame):
    event: Literal["status"] = "status"
    payload: ComposerOperationStreamStatusPayload


class ComposerOperationStreamProgressFrame(_ComposerOperationStreamFrame):
    event: Literal["progress"] = "progress"
    payload: ComposerOperationStreamProgressPayload


class ComposerOperationStreamHeartbeatFrame(_ComposerOperationStreamFrame):
    event: Literal["heartbeat"] = "heartbeat"


class ComposerOperationStreamTerminalFrame(_ComposerOperationStreamFrame):
    event: Literal["terminal"] = "terminal"
    payload: ComposerOperationStreamTerminalPayload


type ComposerOperationStreamFrame = (
    ComposerOperationStreamStatusFrame
    | ComposerOperationStreamProgressFrame
    | ComposerOperationStreamHeartbeatFrame
    | ComposerOperationStreamTerminalFrame
)

COMPOSER_OPERATION_STREAM_FRAME_MAX_BYTES = 64 * 1024


def encode_composer_operation_stream_frame(frame: ComposerOperationStreamFrame) -> bytes:
    """Bound the actual UTF-8 SSE bytes, including framing."""
    exact = type(frame).model_validate(frame.model_dump(mode="python"), strict=True)
    encoded = f"event: {exact.event}\ndata: {exact.model_dump_json()}\n\n".encode()
    if len(encoded) > COMPOSER_OPERATION_STREAM_FRAME_MAX_BYTES:
        raise ValueError("Composer operation stream frame exceeds encoded byte bound")
    return encoded

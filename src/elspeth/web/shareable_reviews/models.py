"""Pydantic response models for the shareable-reviews endpoints.

Phase 6A Task 4 (UX redesign 2026-05). All response models inherit
``_StrictResponse`` from ``web/execution/schemas.py`` so the Tier-1 wire
discipline (``strict=True, extra="forbid"``) applies:

* Field types must match exactly — ``"7"`` into an ``int`` is rejected.
* Unexpected fields in the constructed model crash rather than being silently
  dropped — drift between producer and consumer fails at construction.

The ``SharedInspectResponse`` re-uses ``AuditReadinessSnapshot`` verbatim so
the shared inspect view shows the same six-row readiness panel the owner
sees in the composer. Phase 2 / Phase 18 own that model; Phase 6 is a
consumer, not a co-owner.

``pipeline_metadata`` and ``composition_snapshot`` are typed as strict
Pydantic mirrors of the ``CompositionState`` / ``PipelineMetadata``
dataclasses defined in ``web/composer/state.py``. The underlying
authoritative state is a frozen dataclass; the wire-side mirror is a
Pydantic model so producer/consumer drift crashes at construction — an
unknown key from a producer that has drifted ahead of the consumer, or
a wrong-type value, raises ``ValidationError`` at
``SharedInspectResponse(...)``.

The wire shape mirrored here is exactly the one emitted by
``CompositionState.to_dict()`` (the producer in
``shareable_reviews/service.py::_build_snapshot``). Optional ``NodeSpec``
fields are modelled with ``= None`` defaults so producer-side keys that
are absent (e.g. ``condition`` on a transform node) do not trip the
``extra="forbid"`` guard — Pydantic's behaviour is "unknown key crashes,
missing-but-defaulted is fine."

Layer: L3 (web application).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from elspeth.web.audit_readiness.models import AuditReadinessSnapshot
from elspeth.web.execution.schemas import _StrictResponse
from elspeth.web.sessions.schemas import CompositionObject


class MarkReadyForReviewRequest(_StrictResponse):
    """Optional body of ``POST /api/sessions/{session_id}/mark-ready-for-review``.

    An absent body keeps the original behaviour: a view-only link that lasts
    the deployment's ``shareable_link_lifetime_seconds``.

    ``lifetime_seconds`` is the closed set the dialog offers (1, 7 or 30
    days); anything else is a 422 from validation. ``None`` means the
    deployment maximum. A listed value above the deployment maximum is
    refused by the service (also 422) — the browser is not trusted to
    enforce the operator's cap.
    """

    allow_import: bool = False
    lifetime_seconds: Literal[86400, 604800, 2592000] | None = None


class MarkReadyForReviewResponse(_StrictResponse):
    """Returned by ``POST /api/sessions/{session_id}/mark-ready-for-review``.

    Carries the freshly-minted signed token, the URL the operator can copy
    out of the UI, the absolute expiry, and the content-address of the
    snapshot blob the token resolves to. ``payload_digest`` is exposed on
    the wire so the operator UI can correlate "the share URL I just got"
    against "the audit row recorded server-side" without an extra round
    trip.
    """

    token: str
    share_url: str
    expires_at: datetime
    payload_digest: str


class ShareableLinkResponse(_StrictResponse):
    """Returned by ``GET /api/sessions/{session_id}/shareable-link``.

    Re-mints a fresh token only for the current ``(session, state)``
    snapshot that already has a matching ``mark_ready_for_review`` audit
    row and snapshot blob. Two calls in succession produce two different
    tokens (the nonce differs) but identical ``payload_digest``
    (content-addressing of an unchanged snapshot).

    The ``state_id`` is surfaced because the underlying composition may
    have advanced since the last call; the client uses this to confirm
    which version of the pipeline the link inspects.
    """

    token: str
    share_url: str
    expires_at: datetime
    state_id: str
    payload_digest: str


class SharedImportResponse(_StrictResponse):
    """Returned by ``POST /api/sessions/shared/{token}/import``.

    The recipient's new session and the composition state the import seeded
    into it.
    """

    session_id: UUID
    state_id: str


class ShareableFileView(_StrictResponse):
    """One file an importable share would carry, as the owner sees it.

    Deliberately carries no blob id, storage path or content hash.
    """

    filename: str
    size_bytes: int
    used_by: list[str]


class ShareableFilesResponse(_StrictResponse):
    """Returned by ``GET /api/sessions/{session_id}/shareable-files``."""

    files: list[ShareableFileView]
    importable: bool
    not_importable_reason: str | None
    # The deployment's ``shareable_link_lifetime_seconds``: the share
    # refuses any longer ``lifetime_seconds``, so the dialog offers only
    # lifetimes at or below it.
    max_lifetime_seconds: int


# ── Strict Pydantic mirrors of CompositionState dataclasses ──────────────
#
# These models are the wire-boundary contract for the inspect response.
# The authoritative in-process types are the frozen dataclasses in
# ``web/composer/state.py``. The mirror exists so producer/consumer drift
# crashes at construction rather than being silently accepted as a free-
# form dict.
#
# Field shapes track ``CompositionState.to_dict()`` exactly. Optional
# NodeSpec fields default to None — the producer omits the key when None,
# Pydantic accepts the missing key when a default is declared.


class PipelineMetadataResponse(_StrictResponse):
    """Strict wire mirror of ``PipelineMetadata`` (web/composer/state.py)."""

    name: str
    description: str


class SourceSpecResponse(_StrictResponse):
    """Strict wire mirror of ``SourceSpec`` (web/composer/state.py)."""

    plugin: str
    on_success: str
    # Plugin options are inherently free-form (each source plugin defines
    # its own option schema); tightening here would conflict with the
    # plugin system. The envelope keys are closed; the values are not.
    options: CompositionObject
    on_validation_failure: str
    description: str | None = None


class NodeSpecResponse(_StrictResponse):
    """Strict wire mirror of ``NodeSpec`` (web/composer/state.py).

    Optional fields (condition, routes, fork_to, branches, policy, merge,
    trigger, output_mode, expected_output_count, timeout_seconds, description,
    scope_name, scope_opener, scope_policy) default to None to match
    ``CompositionState.to_dict()`` which omits these keys when the
    underlying dataclass field is None.

    The field set is pinned REFLECTIVELY against ``dataclasses.fields(NodeSpec)``
    and ``node_type`` against ``NodeType`` (tests/unit/web/shareable_reviews/
    test_models.py) — a hand-listed restatement let ``queue``, ``description``
    and ``collector`` each drift past this mirror and 500 the share-resolve
    route (elspeth-a5b86149d4, elspeth-989d369d82).
    """

    id: str
    node_type: Literal["transform", "gate", "aggregation", "coalesce", "queue", "row_union", "collector"]
    plugin: str | None
    input: str
    on_success: str | None
    on_error: str | None
    options: CompositionObject
    condition: str | None = None
    routes: dict[str, str] | None = None
    fork_to: list[str] | None = None
    # ``branches`` serialises as ``dict[str, str]`` (mapping form) or
    # ``list[str]`` (tuple form) per ``_serialize_branches``.
    branches: dict[str, str] | list[str] | None = None
    policy: str | None = None
    merge: str | None = None
    trigger: CompositionObject | None = None
    output_mode: str | None = None
    expected_output_count: int | None = None
    timeout_seconds: float | None = None
    description: str | None = None
    # Collector-only scope binding. The authored ``scope_name`` is ADMITTED
    # here, not stripped: the share surface is canonical (node ids are public
    # in ``nodes[].id`` and the edge list) and the public YAML consumer
    # ``generate_public_pipeline_dict`` requires it to lower the ``scopes:``
    # block (yaml_generator.py ``_require_node_key(c, "scope_name", ...)``).
    # Proposal projections have their own privacy boundary; this strict
    # mirror is not a proposal projection.
    scope_name: str | None = None
    scope_opener: str | None = None
    scope_policy: str | None = None


class EdgeSpecResponse(_StrictResponse):
    """Strict wire mirror of ``EdgeSpec`` (web/composer/state.py)."""

    id: str
    from_node: str
    to_node: str
    edge_type: Literal["on_success", "on_error", "route_true", "route_false", "fork"]
    label: str | None


class OutputSpecResponse(_StrictResponse):
    """Strict wire mirror of ``OutputSpec`` (web/composer/state.py)."""

    name: str
    plugin: str
    options: CompositionObject
    on_write_failure: str
    description: str | None = None


class CompositionStateResponse(_StrictResponse):
    """Strict wire mirror of ``CompositionState`` (web/composer/state.py).

    Shape tracks ``CompositionState.to_dict()`` exactly.

    JsonValue is acceptable as the type of free-form plugin options but
    the structural top-level keys are closed.
    """

    version: int
    metadata: PipelineMetadataResponse
    sources: dict[str, SourceSpecResponse]
    nodes: list[NodeSpecResponse]
    edges: list[EdgeSpecResponse]
    outputs: list[OutputSpecResponse]


class SharedFileSummary(_StrictResponse):
    """A file that comes with a share link, as the recipient sees it.

    Name and size only: content hashes, blob ids, MIME types and binding
    sites stay in the private snapshot manifest.
    """

    filename: str
    size_bytes: int


class SharedInspectResponse(_StrictResponse):
    """Returned by ``GET /api/sessions/shared/{token}``.

    The read-only inspect view for a recipient. ``yaml`` is the rendered
    pipeline YAML (regenerated from the snapshot, not stored verbatim, so
    a YAML-generator improvement automatically flows through). The
    ``audit_readiness`` field is the same snapshot the owner sees —
    including the ``llm_interpretations`` row inherited from the
    interpretation-events surface. No extra aggregation happens at the
    shared-inspect boundary.

    ``pipeline_metadata`` and ``composition_snapshot`` are typed as
    strict Pydantic mirrors of their respective dataclasses so producer/
    consumer drift fails at construction.

    Attribution comes in two fields. ``created_by_user_id`` is the opaque
    identity id the token signature binds — stable, but meaningless to a
    recipient who has no login. ``created_by_username`` is the sharer's
    human-readable name, frozen into the snapshot at mark-time, and is
    what the reviewer's banner shows. This release requires it in every
    signed share snapshot.

    ``expires_at`` lets the frontend show "this link expires in N days"
    without re-decoding the token.

    ``import_allowed`` says whether the link lets the recipient import the
    pipeline into their own session; ``files`` lists the files that come
    with it (names and sizes only, in manifest order). Legacy snapshots
    report ``import_allowed=False`` and no files.
    """

    session_id: str
    state_id: str
    pipeline_metadata: PipelineMetadataResponse
    composition_snapshot: CompositionStateResponse
    yaml: str
    audit_readiness: AuditReadinessSnapshot
    created_by_user_id: str
    created_by_username: str
    created_at: datetime
    expires_at: datetime
    import_allowed: bool
    files: list[SharedFileSummary]

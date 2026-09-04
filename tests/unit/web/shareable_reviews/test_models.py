"""Tests for shareable_reviews response models.

Phase 6A Task 4 (UX redesign 2026-05). All three response models are
``_StrictResponse``-derived: ``strict=True, extra="forbid"``. The plan
re-uses the Phase 2 ``AuditReadinessSnapshot`` model verbatim inside
``SharedInspectResponse`` so the shared inspect view shows the same
six-row readiness panel the owner sees.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from elspeth.web.audit_readiness.models import AuditReadinessSnapshot, ReadinessRow
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    NodeType,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.composer.yaml_generator import generate_public_composition_dict, generate_public_pipeline_dict
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.shareable_reviews.models import (
    CompositionStateResponse,
    MarkReadyForReviewResponse,
    NodeSpecResponse,
    OutputSpecResponse,
    ShareableLinkResponse,
    SharedInspectResponse,
    SourceSpecResponse,
)


def _make_composition_snapshot() -> dict[str, object]:
    """Build a minimal valid composition_snapshot wire shape.

    Mirrors the dict shape produced by ``CompositionState.to_dict()`` —
    version, metadata, sources, nodes, edges, outputs. With the FIX-A
    tightening of SharedInspectResponse (Plan 19a:891-892) the
    composition_snapshot field is a strict Pydantic model and partial
    dicts are rejected at construction.
    """
    return {
        "version": 1,
        "metadata": {"name": "Demo", "description": ""},
        "sources": {},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }


def _make_audit_readiness_snapshot() -> AuditReadinessSnapshot:
    """Build a minimal valid AuditReadinessSnapshot for response-model tests.

    Phase 2 contract requires all six closed-enum row ids to be present.
    """

    def _row(row_id: str) -> ReadinessRow:
        return ReadinessRow(
            id=row_id,  # type: ignore[arg-type]
            label=row_id,
            status="ok",
            summary="ok",
            detail=None,
            component_ids=(),
        )

    validation_result = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        semantic_contracts=[],
    )
    return AuditReadinessSnapshot(
        session_id="s-1",
        composition_version=1,
        checked_at=datetime.now(UTC),
        rows=tuple(
            _row(row_id)
            for row_id in (
                "validation",
                "plugin_trust",
                "provenance",
                "retention",
                "llm_interpretations",
                "secrets",
            )
        ),
        validation_result=validation_result,
    )


def test_mark_ready_response_strict() -> None:
    resp = MarkReadyForReviewResponse(
        token="abc123",
        share_url="https://example.com/shared/abc123",
        expires_at=datetime.now(UTC),
        payload_digest="sha256:" + ("ab" * 32),
    )
    assert resp.token == "abc123"
    assert resp.payload_digest.startswith("sha256:")


def test_mark_ready_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        MarkReadyForReviewResponse(
            token="abc",
            share_url="https://x/",
            expires_at=datetime.now(UTC),
            payload_digest="sha256:" + ("ab" * 32),
            unexpected="field",  # type: ignore[call-arg]
        )


def test_mark_ready_rejects_str_for_datetime() -> None:
    """strict=True forbids str→datetime coercion."""
    with pytest.raises(ValidationError):
        MarkReadyForReviewResponse(
            token="abc",
            share_url="https://x/",
            expires_at="2026-01-01T00:00:00+00:00",  # type: ignore[arg-type]
            payload_digest="sha256:" + ("ab" * 32),
        )


def test_shareable_link_response_strict() -> None:
    resp = ShareableLinkResponse(
        token="abc",
        share_url="https://x/abc",
        expires_at=datetime.now(UTC),
        state_id=str(uuid4()),
        payload_digest="sha256:" + ("cd" * 32),
    )
    assert resp.payload_digest.startswith("sha256:")


def test_shareable_link_response_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        ShareableLinkResponse(
            token="abc",
            share_url="https://x/abc",
            expires_at=datetime.now(UTC),
            state_id=str(uuid4()),
            payload_digest="sha256:" + ("cd" * 32),
            extra="boom",  # type: ignore[call-arg]
        )


def test_shared_inspect_response_carries_audit_readiness() -> None:
    """SharedInspectResponse must include audit_readiness (consumed by 19b Task 8)."""
    snapshot = _make_audit_readiness_snapshot()
    resp = SharedInspectResponse(
        session_id=str(uuid4()),
        state_id=str(uuid4()),
        pipeline_metadata={"name": "Demo", "description": ""},
        composition_snapshot=_make_composition_snapshot(),
        yaml="version: 1\n",
        audit_readiness=snapshot,
        created_by_user_id="user-1",
        created_by_username="user-one",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )
    assert resp.audit_readiness is snapshot
    assert resp.yaml == "version: 1\n"


def test_shared_inspect_response_accepts_plural_sources_snapshot() -> None:
    """Shareable-review snapshots accept only CompositionState's canonical sources map."""
    snapshot = _make_audit_readiness_snapshot()
    composition = _make_composition_snapshot()
    source = {
        "plugin": "csv",
        "on_success": "normalize",
        "options": {"schema": {"mode": "observed"}},
        "on_validation_failure": "discard",
    }
    composition["sources"] = {"source": source}

    resp = SharedInspectResponse(
        session_id=str(uuid4()),
        state_id=str(uuid4()),
        pipeline_metadata={"name": "Demo", "description": ""},
        composition_snapshot=composition,
        yaml="version: 1\n",
        audit_readiness=snapshot,
        created_by_user_id="user-1",
        created_by_username="user-one",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    assert resp.composition_snapshot.sources["source"].plugin == "csv"


def test_shared_inspect_response_rejects_extra_field() -> None:
    snapshot = _make_audit_readiness_snapshot()
    with pytest.raises(ValidationError):
        SharedInspectResponse(
            session_id=str(uuid4()),
            state_id=str(uuid4()),
            pipeline_metadata={"name": "Demo", "description": ""},
            composition_snapshot=_make_composition_snapshot(),
            yaml="version: 1\n",
            audit_readiness=snapshot,
            created_by_user_id="user-1",
            created_by_username="user-one",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
            something_extra=True,  # type: ignore[call-arg]
        )


def test_shared_inspect_response_requires_audit_readiness() -> None:
    """Omitting audit_readiness must fail (it's not Optional)."""
    with pytest.raises(ValidationError, match="audit_readiness"):
        SharedInspectResponse(  # type: ignore[call-arg]
            session_id=str(uuid4()),
            state_id=str(uuid4()),
            pipeline_metadata={"name": "Demo", "description": ""},
            composition_snapshot=_make_composition_snapshot(),
            yaml="version: 1\n",
            created_by_user_id="user-1",
            created_by_username="user-one",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )


# ── FIX-A: drift-crashes-at-construction tests (Plan 19a:891-892) ─────────


def test_shared_inspect_response_rejects_extra_pipeline_metadata_key() -> None:
    """An unknown key inside pipeline_metadata must crash at construction.

    Plan 19a:64 — "drift crashes at construction" — depends on the
    response model being a strict Pydantic mirror of PipelineMetadata,
    not a free-form dict. An unknown key (producer drift) must crash so
    audit-shape regressions are caught at the wire boundary, not silently
    accepted.
    """
    snapshot = _make_audit_readiness_snapshot()
    with pytest.raises(ValidationError):
        SharedInspectResponse(
            session_id=str(uuid4()),
            state_id=str(uuid4()),
            pipeline_metadata={"name": "Demo", "description": "", "unknown_key": True},
            composition_snapshot=_make_composition_snapshot(),
            yaml="version: 1\n",
            audit_readiness=snapshot,
            created_by_user_id="user-1",
            created_by_username="user-one",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )


def test_shared_inspect_response_rejects_extra_composition_snapshot_key() -> None:
    """An unknown top-level key inside composition_snapshot must crash."""
    snapshot = _make_audit_readiness_snapshot()
    bad_snapshot = _make_composition_snapshot()
    bad_snapshot["unknown_top_level_key"] = "boom"
    with pytest.raises(ValidationError):
        SharedInspectResponse(
            session_id=str(uuid4()),
            state_id=str(uuid4()),
            pipeline_metadata={"name": "Demo", "description": ""},
            composition_snapshot=bad_snapshot,
            yaml="version: 1\n",
            audit_readiness=snapshot,
            created_by_user_id="user-1",
            created_by_username="user-one",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )


def test_shared_inspect_response_rejects_wrong_pipeline_metadata_field_type() -> None:
    """A wrong-type value inside pipeline_metadata must crash.

    strict=True forbids int→str coercion. ``name`` is declared as str
    on the response mirror; passing ``123`` must raise rather than
    silently coercing to ``"123"``.
    """
    snapshot = _make_audit_readiness_snapshot()
    with pytest.raises(ValidationError):
        SharedInspectResponse(
            session_id=str(uuid4()),
            state_id=str(uuid4()),
            pipeline_metadata={"name": 123, "description": ""},
            composition_snapshot=_make_composition_snapshot(),
            yaml="version: 1\n",
            audit_readiness=snapshot,
            created_by_user_id="user-1",
            created_by_username="user-one",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
        )


def _canonical_queue_node_wire() -> dict[str, object]:
    """The wire shape a canonical structural queue serialises to."""
    return {
        "id": "inbound",
        "node_type": "queue",
        "plugin": None,
        "input": "inbound",
        "on_success": None,
        "on_error": None,
        "options": {"description": "Orders and refunds interleave here"},
    }


def test_node_spec_response_accepts_queue() -> None:
    """The shareable-review node-type vocabulary is a true discriminated set
    that must accept the ``queue`` structural node (elspeth-a5b86149d4)."""
    node = NodeSpecResponse.model_validate(_canonical_queue_node_wire())
    assert node.node_type == "queue"
    assert node.input == "inbound"
    assert node.plugin is None
    assert dict(node.options) == {"description": "Orders and refunds interleave here"}


def test_shared_inspect_response_accepts_queue_node_in_snapshot() -> None:
    """A composition_snapshot carrying a queue node round-trips through the
    strict SharedInspectResponse mirror without a storage migration."""
    snapshot = _make_audit_readiness_snapshot()
    composition = _make_composition_snapshot()
    composition["nodes"] = [_canonical_queue_node_wire()]
    response = SharedInspectResponse(
        session_id=str(uuid4()),
        state_id=str(uuid4()),
        pipeline_metadata={"name": "Demo", "description": ""},
        composition_snapshot=composition,
        yaml="version: 1\n",
        audit_readiness=snapshot,
        created_by_user_id="user-1",
        created_by_username="user-one",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )
    assert response.composition_snapshot.nodes[0].node_type == "queue"


def test_node_spec_response_accepts_row_union_timeout() -> None:
    node = NodeSpecResponse.model_validate(
        {
            "id": "variant_union",
            "node_type": "row_union",
            "plugin": None,
            "input": "control_done",
            "on_success": "unioned_rows",
            "on_error": None,
            "options": {},
            "branches": {
                "control": "control_done",
                "treatment": "treatment_done",
            },
            "timeout_seconds": 30.0,
        }
    )

    assert node.node_type == "row_union"
    assert node.timeout_seconds == 30.0


# ── Producer-mirror pins (elspeth-989d369d82) ──────────────────────────────
#
# ``CompositionState.to_dict()`` emits every dataclass field of each spec
# (optional ones only when non-None) and ``_StrictResponse`` forbids extras,
# so any field the producer grows that the mirror lacks raises inside
# ``resolve_token`` (shareable_reviews/service.py ``CompositionStateResponse
# .model_validate``) and reaches the recipient as a bare 500 — while the
# owner, whose mint path never runs the mirror, gets a working-looking link.
# ``queue`` (elspeth-a5b86149d4), then ``description`` and ``collector``
# (elspeth-989d369d82) each got through a hand-listed mirror exactly this way.
# These pins are REFLECTIVE over the producer so the next field cannot.


@pytest.mark.parametrize(
    ("producer", "mirror"),
    (
        (SourceSpec, SourceSpecResponse),
        (NodeSpec, NodeSpecResponse),
        (OutputSpec, OutputSpecResponse),
    ),
)
def test_response_model_field_set_mirrors_its_producer_dataclass(
    producer: type[SourceSpec | NodeSpec | OutputSpec],
    mirror: type[SourceSpecResponse | NodeSpecResponse | OutputSpecResponse],
) -> None:
    """Equality in BOTH directions: a mirror field the producer never emits is
    drift too, just the silent kind."""
    assert {field.name for field in dataclasses.fields(producer)} == set(mirror.model_fields)


def test_node_type_literal_mirrors_the_composer_node_type_vocabulary() -> None:
    """``models.py`` carried six of seven kinds for 11 days after ``collector``
    landed; the vocabulary is the composer's, not this file's."""
    mirror_literal = NodeSpecResponse.model_fields["node_type"].annotation
    assert set(get_args(mirror_literal)) == set(get_args(NodeType))


def _collector_composition_state() -> CompositionState:
    """A Stage-1-shaped composition carrying every field this ticket found
    undeclared: a collector with its scope binding, and an authored
    description on the source, a node and the sink."""
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="src_out",
            options={"path": "documents.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
            description="Multi-document export",
        ),
        nodes=(
            NodeSpec(
                id="explode",
                node_type="transform",
                plugin="passthrough",
                input="src_out",
                on_success="sections",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                description="One row per section",
            ),
            NodeSpec(
                id="gather",
                node_type="collector",
                plugin="report_assemble",
                input="sections",
                on_success="out",
                on_error=None,
                options={"schema": {"mode": "observed"}, "text_field": "gist"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                scope_name="per_document",
                scope_opener="explode",
                scope_policy="require_all",
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="out",
                plugin="csv",
                options={"path": "summaries.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
                description="One summary row per document",
            ),
        ),
        metadata=PipelineMetadata(name="Collector share", description="Fixture for the share mirror"),
        version=1,
    )


def test_public_projection_of_a_collector_composition_round_trips_the_strict_mirror() -> None:
    """The exact seam that 500'd: ``generate_public_composition_dict`` passes
    ``node_type`` and the scope binding through verbatim (it rewrites
    ``options`` only), and the mirror must admit what it passes."""
    projected = generate_public_composition_dict(_collector_composition_state())
    gather_wire = next(node for node in projected["nodes"] if node["id"] == "gather")
    assert gather_wire["node_type"] == "collector"
    assert {"scope_name", "scope_opener", "scope_policy"} <= set(gather_wire)

    response = CompositionStateResponse.model_validate(projected)

    gather = next(node for node in response.nodes if node.id == "gather")
    assert (gather.node_type, gather.scope_name, gather.scope_opener, gather.scope_policy) == (
        "collector",
        "per_document",
        "explode",
        "require_all",
    )


def test_public_projection_keeps_authored_descriptions_and_the_mirror_admits_them() -> None:
    """The collector-free arm: any pipeline whose author (or the planner,
    which the freeform tools instruct to do so on every step) gave a source,
    node or sink a description hit the same 500 from 2026-08-15."""
    response = CompositionStateResponse.model_validate(generate_public_composition_dict(_collector_composition_state()))

    assert response.sources["source"].description == "Multi-document export"
    assert next(node for node in response.nodes if node.id == "explode").description == "One row per section"
    assert response.outputs[0].description == "One summary row per document"


def test_scope_name_is_admitted_because_the_public_yaml_consumer_requires_it() -> None:
    """Why the mirror ADMITS ``scope_name`` rather than the projection
    stripping it: ``generate_public_pipeline_dict`` lowers the ``scopes:``
    block from the SAME public dict and refuses to lower without the name
    (yaml_generator ``_require_node_key(c, "scope_name", ...)``). Stripping
    it would trade the share 500 for a public-YAML download failure on every
    collector pipeline. The guided ``_CollectorBehavior`` privacy ruling
    governs the stable-id'd proposal projection, which this surface is not —
    node ids are already public here."""
    public_yaml = generate_public_pipeline_dict(_collector_composition_state())

    assert public_yaml["scopes"] == [
        {"name": "per_document", "opener": "explode", "closer": "gather", "policy": "require_all"},
    ]

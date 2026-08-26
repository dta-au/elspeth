"""Security and integrity contracts for guided proposal projection."""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from itertools import permutations, product
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints
from uuid import UUID

import pytest
import structlog
from sqlalchemy.pool import StaticPool

import elspeth.web.composer.guided.planning as guided_planning
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.capability_skill import load_pipeline_capability_core
from elspeth.web.composer.guided.planning import (
    build_guided_proposal_projection,
    guided_candidate_state,
    guided_private_reviewed_facts,
    guided_redacted_current_state_context,
    guided_redacted_planner_context,
    verified_remaining_deferred_intents,
    verify_guided_proposal_projection,
)
from elspeth.web.composer.guided.protocol import (
    GuidedStep,
    ProposePipelinePayload,
    TurnType,
    proposal_structural_label,
    validate_payload,
)
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.stage_subjects import (
    EdgeRouteConstraint,
    OptionValueConstraint,
    PluginSubject,
    StableSubject,
    StatedGateRoutingConstraint,
)
from elspeth.web.composer.guided.state_machine import DeferredStageIntent, GuidedSession
from elspeth.web.composer.pipeline_planner import (
    PlannerBudgetPolicy,
    PlannerCustodyConfig,
    PlannerDeclined,
    PlannerModelConfig,
    PlannerOriginatingMessage,
    PlannerRequestLifecycle,
    plan_pipeline,
)
from elspeth.web.composer.pipeline_proposal import PipelineProposal, PlannerSurface, PresentBase
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

SOURCE_ID = "00000000-0000-4000-8000-000000000101"
OUTPUT_ID = "00000000-0000-4000-8000-000000000102"
PROPOSAL_ID = UUID("00000000-0000-4000-8000-000000000103")
CHECKPOINT_ID = UUID("00000000-0000-4000-8000-000000000104")
MIXED_INTENT_ID = "00000000-0000-4000-8000-000000000107"
MIXED_MESSAGE_ID = "00000000-0000-4000-8000-000000000108"
GATE_ID = "00000000-0000-4000-8000-000000000109"
PASSTHROUGH_SUBJECT_ID = "00000000-0000-4000-8000-000000000110"
SECOND_GATE_ID = "00000000-0000-4000-8000-000000000111"
CANARIES = (
    "RAW-INLINE-CONTENT-CANARY",
    "CREDENTIAL-CANARY",
    "RESOLVED-SECRET-CANARY",
    "RAW-VALIDATION-CANARY",
    "RAW-PROVIDER-ERROR-CANARY",
)
DEFERRED_VALUE_CANARY = "PRIVATE-OPTION-VALUE-CANARY"
DEFERRED_PATH_CANARY = "private_credential_path_canary"


def _guided() -> GuidedSession:
    return replace(
        GuidedSession.initial(),
        reviewed_sources={
            SOURCE_ID: SourceResolved(
                name="primary",
                plugin="csv",
                options={
                    "inline_blob": {"content": CANARIES[0]},
                    "credentials": {"secret_ref": CANARIES[1], "resolved": CANARIES[2]},
                    "schema": {"mode": "observed"},
                },
                observed_columns=("name", "score"),
                sample_rows=({"name": CANARIES[3], "score": 42},),
                on_validation_failure="discard",
            )
        },
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="cleaned",
                plugin="json",
                options={"path": CANARIES[4], "schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
        source_order=(SOURCE_ID,),
        output_order=(OUTPUT_ID,),
        step=GuidedStep.STEP_3_TRANSFORMS,
    )


def _proposal(guided: GuidedSession, *, supersedes_draft_hash: str | None = None) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"credentials": {"secret_ref": CANARIES[1]}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "clean",
                    "node_type": "transform",
                    "plugin": "normalize",
                    "input": "rows",
                    "on_success": "cleaned",
                    "on_error": "discard",
                    "options": {"rules": [{"column": "name", "operation": "strip"}]},
                }
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": {"path": CANARIES[4]},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=supersedes_draft_hash,
    )


def _field_mapper_proposal(guided: GuidedSession) -> PipelineProposal:
    """A proposal whose transform carries both allowlisted knobs and a canary."""

    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"credentials": {"secret_ref": CANARIES[1]}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "clean",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "rows",
                    "on_success": "cleaned",
                    "on_error": "discard",
                    "options": {
                        "mapping": {"given_name": "first_name", "meta.source": "origin"},
                        "select_only": True,
                        "description": CANARIES[4],
                    },
                }
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": {"path": CANARIES[4]},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_projected_node_options_survive_persistence_and_reverify_byte_for_byte() -> None:
    """R2-F3: the projected option summary is part of the verified authority.

    ``verify_guided_proposal_projection`` byte-compares a PERSISTED payload
    against a live rebuild, so a summary whose rendering is not deterministic
    across the persist/thaw round trip would raise ``AuditIntegrityError`` on
    session reload rather than fail cosmetically. Every other projection test
    uses a plugin outside the allowlist, where the summary is always empty.
    """
    guided = _guided()
    proposal = _field_mapper_proposal(guided)
    catalog_ids = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"field_mapper"}),
        "sink": frozenset({"json"}),
    }

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog_ids,
    )

    assert payload["nodes"][0]["node_options_summary"] == [
        {"key": "mapping", "value": "given_name → first_name, meta.source → origin"},
        {"key": "select_only", "value": "only the mapped fields are kept"},
    ]
    # The non-allowlisted neighbour option must not ride along.
    assert all(canary not in repr(payload) for canary in CANARIES)

    # Round-trip exactly as the payload store does before the reload verifier
    # re-derives the projection from private authority.
    persisted = json.loads(json.dumps(payload))
    assert validate_payload(TurnType.PROPOSE_PIPELINE, persisted) is None
    verify_guided_proposal_projection(
        payload=persisted,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog_ids,
    )

    tampered = json.loads(json.dumps(payload))
    tampered["nodes"][0]["node_options_summary"][0]["value"] = "given_name → surname"
    with pytest.raises(AuditIntegrityError, match="projection"):
        verify_guided_proposal_projection(
            payload=tampered,
            proposal_id=PROPOSAL_ID,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids=catalog_ids,
        )


def _mixed_gate_guided() -> GuidedSession:
    return replace(
        _guided(),
        deferred_intents=(
            DeferredStageIntent.create(
                intent_id=MIXED_INTENT_ID,
                receiving_stage="output",
                target_stage="wire_review",
                catalog_kind=None,
                catalog_name=None,
                redacted_summary="Retain direct and fork gate routes.",
                originating_message_id=MIXED_MESSAGE_ID,
                message_content_hash=stable_hash("mixed gate instruction"),
                constraints=(
                    EdgeRouteConstraint(
                        kind="edge_route",
                        from_subject=StableSubject(kind="stable", component_kind="node", stable_id=GATE_ID),
                        edge_type="route_false",
                        to_subject=StableSubject(kind="stable", component_kind="output", stable_id=OUTPUT_ID),
                        present=True,
                    ),
                    EdgeRouteConstraint(
                        kind="edge_route",
                        from_subject=StableSubject(kind="stable", component_kind="node", stable_id=GATE_ID),
                        edge_type="fork",
                        to_subject=PluginSubject(
                            kind="plugin",
                            subject_id=PASSTHROUGH_SUBJECT_ID,
                            plugin_kind="transform",
                            plugin_name="passthrough",
                        ),
                        present=True,
                    ),
                ),
            ),
        ),
    )


def _mixed_gate_proposal(guided: GuidedSession, routes: dict[str, str]) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "gate-input",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": GATE_ID,
                    "node_type": "gate",
                    "plugin": None,
                    "input": "gate-input",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "row['accepted']",
                    "routes": routes,
                    "fork_to": ["accepted"],
                },
                {
                    "id": "copy",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "accepted",
                    "on_success": "cleaned",
                    "on_error": "discard",
                    "options": {},
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": {"schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(MIXED_INTENT_ID,),
        supersedes_draft_hash=None,
    )


def _two_gate_proposal(
    guided: GuidedSession,
    *,
    first_routes: dict[str, str],
    second_routes: dict[str, str],
) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "first-gate-input",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": GATE_ID,
                    "node_type": "gate",
                    "plugin": None,
                    "input": "first-gate-input",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "row['first']",
                    "routes": first_routes,
                    "fork_to": [],
                },
                {
                    "id": SECOND_GATE_ID,
                    "node_type": "gate",
                    "plugin": None,
                    "input": "second-gate-input",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "row['second']",
                    "routes": second_routes,
                    "fork_to": [],
                },
                {
                    "id": "copy",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "accepted",
                    "on_success": "cleaned",
                    "on_error": "discard",
                    "options": {},
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": {"schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def _build_with_fixed_projection_ids(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proposal: PipelineProposal,
    guided: GuidedSession,
    catalog: dict[str, frozenset[str]],
    allocated_id_count: int,
) -> ProposePipelinePayload:
    allocated_ids = iter(UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(200, 200 + allocated_id_count))
    monkeypatch.setattr(guided_planning, "uuid4", lambda: next(allocated_ids))
    return build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )


def test_planner_context_is_redacted_but_private_anchor_keeps_exact_reviewed_facts() -> None:
    guided = _guided()

    private = guided_private_reviewed_facts(guided)
    public = guided_redacted_planner_context(guided)

    private_text = repr(private)
    public_text = repr(public)
    assert all(canary in private_text for canary in CANARIES[:3])
    assert all(canary not in public_text for canary in CANARIES)
    assert public == {
        "schema": "guided.reviewed-planner-context.v1",
        "sources": [
            {
                "stable_id": SOURCE_ID,
                "name": "primary",
                "plugin": "csv",
                "observed_columns": ["name", "score"],
                # Schema facts are stated, not omitted: an observed schema
                # declares no fields, which is a different fact from "no schema
                # facts were projected". Same for custody — this fixture's
                # inline source is genuinely not blob-bound.
                "schema_mode": "observed",
                "declared_fields": [],
                "option_keys": ["credentials", "inline_blob", "schema"],
                "server_storage_bound": False,
                "on_validation_failure": "discard",
            }
        ],
        "outputs": [
            {
                "stable_id": OUTPUT_ID,
                "name": "cleaned",
                "plugin": "json",
                "required_fields": ["name"],
                "schema_mode": "observed",
                "option_keys": ["path", "schema"],
                "on_write_failure": "discard",
            }
        ],
        # Static anti-lure guidance, never per-request data: the staged
        # surface shows reviewed sink names the freeform surface doesn't,
        # and planners repeatedly wired fork-branch transforms straight to
        # them (guided session 04200b45).
        "output_usage": (
            "Reviewed sink names are commit targets for the pipeline's FINAL producer only — "
            "never for branch transforms feeding a coalesce."
        ),
        # Static redaction-explanation, never per-request data: both projections
        # above carry option_keys WITHOUT values, and a planner told not to
        # invent options reads that gap as missing data worth discovery turns
        # (elspeth-63cf3803e6). Pinned here because the line is the redaction's
        # provider-visible contract — deleting it silently restores the defect,
        # and the rewording constraints live at the source, on this key's
        # comment in guided_redacted_planner_context (planning.py).
        "reviewed_configuration_usage": (
            "Reviewed source and output plugin configuration is operator-approved and is restored "
            "server-side after your call. `option_keys` names which options exist; their values are "
            "withheld by design and are NOT missing data — no state or catalog lookup can return "
            "them, and you never need them to author a candidate."
        ),
        "deferred_intents": [],
    }


def test_option_value_constraint_exposes_only_closed_structural_semantics_to_provider() -> None:
    guided = replace(
        _guided(),
        deferred_intents=(
            DeferredStageIntent.create(
                intent_id="00000000-0000-4000-8000-000000000105",
                receiving_stage="output",
                target_stage="topology",
                catalog_kind="source",
                catalog_name="csv",
                redacted_summary="Apply a private option constraint.",
                originating_message_id="00000000-0000-4000-8000-000000000106",
                message_content_hash=stable_hash("private option instruction"),
                constraints=(
                    OptionValueConstraint(
                        kind="option_value",
                        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
                        option_path=(DEFERRED_PATH_CANARY, "value"),
                        operator="equals",
                        value=DEFERRED_VALUE_CANARY,
                    ),
                ),
            ),
        ),
    )
    state = CompositionState(
        sources={},
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
        guided_session=guided,
    )

    private_constraint = guided.deferred_intents[0].constraints[0].to_dict()
    provider_reviewed = guided_redacted_planner_context(guided)
    provider_current = guided_redacted_current_state_context(state)

    assert private_constraint["option_path"] == [DEFERRED_PATH_CANARY, "value"]
    assert private_constraint["value"] == DEFERRED_VALUE_CANARY
    for provider_context in (provider_reviewed, provider_current):
        rendered = repr(provider_context)
        assert DEFERRED_PATH_CANARY not in rendered
        assert DEFERRED_VALUE_CANARY not in rendered
    assert provider_reviewed["deferred_intents"][0]["constraints"] == [
        {
            "kind": "option_value",
            "subject": {"kind": "stable", "component_kind": "source", "stable_id": SOURCE_ID},
            "operator": "equals",
            "value_type": "string",
            "value_present": True,
        }
    ]


def test_stated_gate_routing_projects_the_operator_literal_and_exact_branch_targets() -> None:
    guided = replace(
        _guided(),
        deferred_intents=(
            DeferredStageIntent.create(
                intent_id="00000000-0000-4000-8000-000000000112",
                receiving_stage="source",
                target_stage="topology",
                catalog_kind=None,
                catalog_name=None,
                redacted_summary="Apply the stated gate routing.",
                originating_message_id="00000000-0000-4000-8000-000000000113",
                message_content_hash=stable_hash("operator gate instruction"),
                constraints=(
                    StatedGateRoutingConstraint(
                        kind="stated_gate_routing",
                        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
                        column="amount",
                        operator="greater_than",
                        value=500,
                        true_target="high_value",
                        false_target="standard",
                    ),
                ),
            ),
        ),
    )

    assert guided_redacted_planner_context(guided)["deferred_intents"][0]["constraints"] == [
        {
            "kind": "stated_gate_routing",
            "subject": {"kind": "stable", "component_kind": "source", "stable_id": SOURCE_ID},
            "column": "amount",
            "operator": "greater_than",
            "value": 500,
            "true_target": "high_value",
            "false_target": "standard",
        }
    ]


def test_projection_is_closed_redacted_and_reverified_against_private_authority() -> None:
    guided = _guided()
    proposal = _proposal(guided)

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids={
            "source": frozenset({"csv"}),
            "transform": frozenset({"normalize"}),
            "sink": frozenset({"json"}),
        },
    )

    rendered = repr(payload)
    assert all(canary not in rendered for canary in CANARIES)
    assert payload["proposal_id"] == str(PROPOSAL_ID)
    assert payload["draft_hash"] == proposal.draft_hash
    assert payload["graph"]["sources"][0]["stable_id"] == SOURCE_ID
    assert payload["outputs"][0]["stable_id"] == OUTPUT_ID
    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids={
            "source": frozenset({"csv"}),
            "transform": frozenset({"normalize"}),
            "sink": frozenset({"json"}),
        },
    )

    payload["nodes"][0]["plugin"]["id"] = "different"
    with pytest.raises(AuditIntegrityError, match="projection"):
        verify_guided_proposal_projection(
            payload=payload,
            proposal_id=PROPOSAL_ID,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids={
                "source": frozenset({"csv"}),
                "transform": frozenset({"normalize"}),
                "sink": frozenset({"json"}),
            },
        )


def test_projection_carries_the_revision_discriminator() -> None:
    """``supersedes_draft_hash`` reaches the closed wire payload verbatim.

    The tutorial frontend discriminates the pre-Send auto-proposal
    (``supersedes_draft_hash`` null — planned from the degenerate transition
    fallback intent, tutorial run 18) from a prose-revision proposal (carries
    the superseded draft hash) to decide whether "Review wiring" may be
    offered. Thread the field through the projection AND its verifier so a
    tampered discriminator cannot survive re-verification.
    """
    guided = _guided()
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"normalize"}),
        "sink": frozenset({"json"}),
    }
    first = _proposal(guided)
    first_payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=first,
        guided=guided,
        catalog_plugin_ids=catalog,
    )
    assert first_payload["supersedes_draft_hash"] is None

    superseding = _proposal(guided, supersedes_draft_hash=first.draft_hash)
    superseding_payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=superseding,
        guided=guided,
        catalog_plugin_ids=catalog,
    )
    assert superseding_payload["supersedes_draft_hash"] == first.draft_hash
    verify_guided_proposal_projection(
        payload=superseding_payload,
        proposal_id=PROPOSAL_ID,
        proposal=superseding,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    tampered = dict(superseding_payload)
    tampered["supersedes_draft_hash"] = None
    with pytest.raises(AuditIntegrityError, match="projection"):
        verify_guided_proposal_projection(
            payload=tampered,
            proposal_id=PROPOSAL_ID,
            proposal=superseding,
            guided=guided,
            catalog_plugin_ids=catalog,
        )


def test_mixed_gate_projection_is_canonical_and_exact_for_every_route_insertion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guided = _mixed_gate_guided()
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"passthrough"}),
        "sink": frozenset({"json"}),
    }
    route_entries = (
        ("alpha", "accepted"),
        ("false", "cleaned"),
        ("beta", "fork"),
        ("true", "fork"),
    )
    payloads = []
    draft_hashes = set()
    for route_permutation in permutations(route_entries):
        proposal = _mixed_gate_proposal(guided, dict(route_permutation))
        payload = _build_with_fixed_projection_ids(
            monkeypatch,
            proposal=proposal,
            guided=guided,
            catalog=catalog,
            allocated_id_count=10,
        )
        verify_guided_proposal_projection(
            payload=payload,
            proposal_id=PROPOSAL_ID,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids=catalog,
        )
        assert verified_remaining_deferred_intents(guided=guided, proposal=proposal) == ()
        draft_hashes.add(proposal.draft_hash)
        payloads.append(payload)

    assert len(draft_hashes) == 1
    assert all(payload == payloads[0] for payload in payloads)
    payload = payloads[0]
    route_aliases = [proposal_structural_label("route", index) for index in range(4)]
    gate = next(node for node in payload["nodes"] if node["stable_id"] and node["node_type"] == "gate")
    assert gate["behavior"] == {
        "kind": "gate",
        # F11: the authored predicate reaches the projection verbatim, and each
        # ordinal alias is bound to its author-visible route key in the same
        # canonical order (direct routes sorted first, then fork routes).
        "condition": "row['accepted']",
        "route_aliases": route_aliases,
        "routes": [
            {"alias": route_aliases[0], "key": "alpha"},
            {"alias": route_aliases[1], "key": "false"},
            {"alias": route_aliases[2], "key": "beta"},
            {"alias": route_aliases[3], "key": "true"},
        ],
        "fork_branches": [
            {
                "routes": route_aliases[2:],
                "branch": proposal_structural_label("branch", 0),
            }
        ],
    }
    gate_flows = [edge["flow"] for edge in payload["graph"]["edges"] if edge["from_endpoint"].get("stable_id") == gate["stable_id"]]
    assert gate_flows == [
        {"kind": "gate_route", "route": route_aliases[0], "branch": None},
        {"kind": "gate_route", "route": route_aliases[1], "branch": None},
        {
            "kind": "gate_fork",
            "routes": route_aliases[2:],
            "branch": proposal_structural_label("branch", 0),
        },
    ]


def test_repeated_route_labels_are_gate_local_and_canonical_for_every_insertion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guided = _guided()
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"passthrough"}),
        "sink": frozenset({"json"}),
    }
    first_entries = (("true", "second-gate-input"), ("false", "cleaned"))
    second_entries = (("true", "accepted"), ("false", "cleaned"))
    payloads = []
    draft_hashes = set()
    route_orders = product(permutations(first_entries), permutations(second_entries))
    for first_routes, second_routes in route_orders:
        proposal = _two_gate_proposal(
            guided,
            first_routes=dict(first_routes),
            second_routes=dict(second_routes),
        )
        payload = _build_with_fixed_projection_ids(
            monkeypatch,
            proposal=proposal,
            guided=guided,
            catalog=catalog,
            allocated_id_count=12,
        )
        verify_guided_proposal_projection(
            payload=payload,
            proposal_id=PROPOSAL_ID,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids=catalog,
        )
        draft_hashes.add(proposal.draft_hash)
        payloads.append(payload)

    assert len(draft_hashes) == 1
    assert all(payload == payloads[0] for payload in payloads)
    gates = [node for node in payloads[0]["nodes"] if node["node_type"] == "gate"]
    route_aliases = [proposal_structural_label("route", index) for index in range(4)]
    assert [gate["behavior"]["route_aliases"] for gate in gates] == [route_aliases[:2], route_aliases[2:]]
    assert len({alias for gate in gates for alias in gate["behavior"]["route_aliases"]}) == 4
    # Each gate carries ITS OWN authored predicate, and repeated route labels
    # ("true"/"false" on both gates) stay gate-local: the shared keys bind to
    # each gate's distinct global ordinal aliases.
    assert [gate["behavior"]["condition"] for gate in gates] == ["row['first']", "row['second']"]
    assert [gate["behavior"]["routes"] for gate in gates] == [
        [{"alias": route_aliases[0], "key": "false"}, {"alias": route_aliases[1], "key": "true"}],
        [{"alias": route_aliases[2], "key": "false"}, {"alias": route_aliases[3], "key": "true"}],
    ]
    assert [
        edge["flow"]
        for gate in gates
        for edge in payloads[0]["graph"]["edges"]
        if edge["from_endpoint"].get("stable_id") == gate["stable_id"]
    ] == [
        {"kind": "gate_route", "route": route_aliases[0], "branch": None},
        {"kind": "gate_route", "route": route_aliases[1], "branch": None},
        {"kind": "gate_route", "route": route_aliases[2], "branch": None},
        {"kind": "gate_route", "route": route_aliases[3], "branch": None},
    ]


def _ab_coalesce_guided() -> GuidedSession:
    return replace(
        GuidedSession.initial(),
        reviewed_sources={
            SOURCE_ID: SourceResolved(
                name="source",
                plugin="csv",
                options={"path": "blob:00000000-0000-0000-0000-000000000001", "schema": {"mode": "observed"}},
                observed_columns=("color_name", "hex"),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="colour_ab_out",
                plugin="json",
                options={"path": "out.json", "schema": {"mode": "observed"}},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
        source_order=(SOURCE_ID,),
        output_order=(OUTPUT_ID,),
        step=GuidedStep.STEP_3_TRANSFORMS,
    )


def _ab_coalesce_proposal(
    guided: GuidedSession,
    *,
    policy: str = "require_all",
    timeout_seconds: float | None = None,
) -> PipelineProposal:
    """A runnable fork -> llm x2 -> coalesce -> field_mapper A/B (session 30acb16e shape)."""
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "source": {
                    "plugin": "csv",
                    "on_success": "csv_rows",
                    "options": {"path": "blob:00000000-0000-0000-0000-000000000001", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "fork_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "csv_rows",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "len(row['color_name']) > 0",
                    "routes": {"true": "fork", "false": "fork"},
                    "fork_to": ["a_rows", "b_rows"],
                },
                {
                    "id": "llm_variant_a",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "a_rows",
                    "on_success": "a_out",
                    "on_error": "discard",
                    "options": {"provider": "openrouter"},
                },
                {
                    "id": "llm_variant_b",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "b_rows",
                    "on_success": "b_out",
                    "on_error": "discard",
                    "options": {"provider": "openrouter"},
                },
                {
                    "id": "reconcile",
                    "node_type": "coalesce",
                    "plugin": None,
                    "input": "a_out",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "branches": {"a_rows": "a_out", "b_rows": "b_out"},
                    "policy": policy,
                    "merge": "union",
                    "timeout_seconds": timeout_seconds,
                },
                {
                    "id": "cleanup",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "reconcile",
                    "on_success": "colour_ab_out",
                    "on_error": "discard",
                    "options": {},
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "colour_ab_out",
                    "plugin": "json",
                    "options": {"path": "out.json"},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_fork_coalesce_ab_projection_routes_every_branch_output_into_the_coalesce() -> None:
    """A fork/coalesce A/B plan must project without an orphaned branch output.

    Regression for guided A/B session 503cac64: ``canonical_connection_consumers``
    keys consumers off ``node.input`` only, so the coalesce's second branch output
    (``b_out``, referenced only via ``branches``) had "no canonical consumer" and
    ``_build_projection`` raised AuditIntegrityError before the proposal could show.
    """
    guided = _ab_coalesce_guided()
    proposal = _ab_coalesce_proposal(guided)
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"llm", "field_mapper"}),
        "sink": frozenset({"json"}),
    }

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )
    node_id_by_role = {node["behavior"]["kind"]: node["stable_id"] for node in payload["nodes"]}
    coalesce_id = node_id_by_role["coalesce"]
    # Both LLM branch producers route their success output into the coalesce.
    coalesce_incoming = [edge for edge in payload["graph"]["edges"] if edge["to_endpoint"].get("stable_id") == coalesce_id]
    assert len(coalesce_incoming) == 2
    assert all(edge["flow"]["kind"] == "node_success" for edge in coalesce_incoming)
    # The coalesce republishes its merged rows to the downstream field_mapper.
    coalesce_outgoing = [edge for edge in payload["graph"]["edges"] if edge["from_endpoint"].get("stable_id") == coalesce_id]
    assert [edge["flow"]["kind"] for edge in coalesce_outgoing] == ["coalesce_success"]


@pytest.mark.parametrize(
    ("policy", "timeout_seconds"),
    [
        ("best_effort", 12.5),
        ("quorum", 30.0),
        ("require_all", None),
    ],
)
def test_coalesce_deadline_is_preserved_and_audit_rejects_lossy_projection(
    policy: str,
    timeout_seconds: float | None,
) -> None:
    guided = _ab_coalesce_guided()
    proposal = _ab_coalesce_proposal(
        guided,
        policy=policy,
        timeout_seconds=timeout_seconds,
    )
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"llm", "field_mapper"}),
        "sink": frozenset({"json"}),
    }

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    coalesce = next(node for node in payload["nodes"] if node["node_type"] == "coalesce")
    assert coalesce["behavior"]["timeout_seconds"] == timeout_seconds
    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    lossy = deep_thaw(payload)
    projected_coalesce = next(node for node in lossy["nodes"] if node["node_type"] == "coalesce")
    del projected_coalesce["behavior"]["timeout_seconds"]
    with pytest.raises(AuditIntegrityError, match="projection"):
        verify_guided_proposal_projection(
            payload=lossy,
            proposal_id=PROPOSAL_ID,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids=catalog,
        )


def _ab_row_union_proposal(guided: GuidedSession) -> PipelineProposal:
    """A fork -> two tagged variants -> row_union -> experiment comparison."""
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "source": {
                    "plugin": "csv",
                    "on_success": "csv_rows",
                    "options": {"path": "blob:00000000-0000-0000-0000-000000000001", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "fork_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "csv_rows",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "True",
                    "routes": {"true": "fork", "false": "fork"},
                    "fork_to": ["control_branch", "treatment_branch"],
                },
                {
                    "id": "tag_control",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "control_branch",
                    "on_success": "control_scored",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "operations": []},
                },
                {
                    "id": "tag_treatment",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "treatment_branch",
                    "on_success": "treatment_scored",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "operations": []},
                },
                {
                    "id": "variant_union",
                    "node_type": "row_union",
                    "plugin": None,
                    "input": "control_scored",
                    "on_success": "experiment_rows",
                    "on_error": None,
                    "options": {},
                    "branches": {
                        "control_branch": "control_scored",
                        "treatment_branch": "treatment_scored",
                    },
                    "policy": None,
                    "merge": None,
                    "timeout_seconds": 12.5,
                },
                {
                    "id": "compare",
                    "node_type": "aggregation",
                    "plugin": "batch_experiment_compare",
                    "input": "experiment_rows",
                    "on_success": "colour_ab_out",
                    "on_error": "discard",
                    "options": {
                        "schema": {"mode": "observed"},
                        "variant_field": "prompt_variant",
                        "score_field": "score",
                    },
                    "trigger": {},
                    "output_mode": "transform",
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "colour_ab_out",
                    "plugin": "json",
                    "options": {"path": "out.json", "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_fork_row_union_ab_projection_preserves_every_branch_and_n_to_n_success() -> None:
    guided = _ab_coalesce_guided()
    proposal = _ab_row_union_proposal(guided)
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"value_transform", "batch_experiment_compare"}),
        "sink": frozenset({"json"}),
    }

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )
    row_union = next(node for node in payload["nodes"] if node["node_type"] == "row_union")
    assert row_union["behavior"] == {
        "kind": "row_union",
        "branch_aliases": ["branch-1", "branch-2"],
        "policy": "require_all",
        "timeout_seconds": 12.5,
    }
    incoming = [edge for edge in payload["graph"]["edges"] if edge["to_endpoint"].get("stable_id") == row_union["stable_id"]]
    assert [edge["flow"]["branch"] for edge in incoming] == ["branch-1", "branch-2"]
    outgoing = [edge for edge in payload["graph"]["edges"] if edge["from_endpoint"].get("stable_id") == row_union["stable_id"]]
    assert [edge["flow"]["kind"] for edge in outgoing] == ["row_union_success"]
    assert outgoing[0]["to_endpoint"]["kind"] == "node"


def test_fork_row_union_projection_preserves_declared_branch_order_when_producers_are_reversed() -> None:
    guided = _ab_coalesce_guided()
    original = _ab_row_union_proposal(guided)
    pipeline = deep_thaw(original.pipeline)
    pipeline["nodes"][1:3] = reversed(pipeline["nodes"][1:3])
    proposal = PipelineProposal.create(
        pipeline=pipeline,
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids={
            "source": frozenset({"csv"}),
            "transform": frozenset({"value_transform", "batch_experiment_compare"}),
            "sink": frozenset({"json"}),
        },
    )

    row_union = next(node for node in payload["nodes"] if node["node_type"] == "row_union")
    assert row_union["behavior"]["branch_aliases"] == ["branch-1", "branch-2"]
    incoming = [edge for edge in payload["graph"]["edges"] if edge["to_endpoint"].get("stable_id") == row_union["stable_id"]]
    assert [edge["flow"]["branch"] for edge in incoming] == ["branch-1", "branch-2"]


def _nested_fork_outer_row_union_proposal(
    guided: GuidedSession,
) -> PipelineProposal:
    """Outer arm may contain its own fork/barrier before the outer row_union."""

    return PipelineProposal.create(
        pipeline={
            "sources": {
                "source": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {
                        "path": "blob:00000000-0000-0000-0000-000000000001",
                        "schema": {"mode": "observed"},
                    },
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "outer_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "rows",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "True",
                    "routes": {"true": "fork", "false": "fork"},
                    "fork_to": ["outer_a", "outer_b"],
                },
                {
                    "id": "nested_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "outer_a",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "True",
                    "routes": {"true": "fork", "false": "fork"},
                    "fork_to": ["inner_a", "inner_b"],
                },
                {
                    "id": "inner_a_step",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "inner_a",
                    "on_success": "inner_a_done",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "operations": []},
                },
                {
                    "id": "inner_b_step",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "inner_b",
                    "on_success": "inner_b_done",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "operations": []},
                },
                {
                    "id": "inner_join",
                    "node_type": "coalesce",
                    "plugin": None,
                    "input": "inner_a_done",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "branches": {
                        "inner_a": "inner_a_done",
                        "inner_b": "inner_b_done",
                    },
                    "policy": "require_all",
                    "merge": "union",
                    "timeout_seconds": None,
                },
                {
                    "id": "outer_a_step",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "inner_join",
                    "on_success": "outer_a_done",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "operations": []},
                },
                {
                    "id": "outer_b_step",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "outer_b",
                    "on_success": "outer_b_done",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "operations": []},
                },
                {
                    "id": "outer_union",
                    "node_type": "row_union",
                    "plugin": None,
                    "input": "outer_a_done",
                    "on_success": "union_rows",
                    "on_error": None,
                    "options": {},
                    "branches": {
                        "outer_a": "outer_a_done",
                        "outer_b": "outer_b_done",
                    },
                    "policy": None,
                    "merge": None,
                    "timeout_seconds": None,
                },
                {
                    "id": "after_union",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "union_rows",
                    "on_success": "colour_ab_out",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}, "operations": []},
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "colour_ab_out",
                    "plugin": "json",
                    "options": {
                        "path": "out.json",
                        "schema": {"mode": "observed"},
                    },
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(
            state_id=CHECKPOINT_ID,
            composition_content_hash="a" * 64,
        ),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_projection_rejects_nested_descendant_fork_inside_outer_row_union_arm() -> None:
    guided = _ab_coalesce_guided()
    proposal = _nested_fork_outer_row_union_proposal(guided)

    validation = guided_candidate_state(proposal).validate()

    assert validation.is_valid is False
    assert [(error.component, error.error_code) for error in validation.errors] == [
        ("node:outer_union", "row_union_nested_fork_invalid"),
    ]


def test_nested_fork_projection_rejects_outer_sibling_branch_contamination() -> None:
    guided = _ab_coalesce_guided()
    proposal = _nested_fork_outer_row_union_proposal(guided)
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"value_transform"}),
        "sink": frozenset({"json"}),
    }
    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )
    outer_union = next(node for node in payload["nodes"] if node["node_type"] == "row_union")
    incoming = [edge for edge in payload["graph"]["edges"] if edge["to_endpoint"].get("stable_id") == outer_union["stable_id"]]
    assert [edge["flow"]["branch"] for edge in incoming] == [
        "branch-1",
        "branch-2",
    ]
    incoming[0]["from_endpoint"], incoming[1]["from_endpoint"] = (
        incoming[1]["from_endpoint"],
        incoming[0]["from_endpoint"],
    )

    error = validate_payload(TurnType.PROPOSE_PIPELINE, payload)

    assert error is not None
    assert "downstream" in error or "not connected" in error


def _multi_stage_row_union_proposal(guided: GuidedSession) -> PipelineProposal:
    """The same A/B fork -> row_union, with TWO transforms in each arm.

    Only the gate's fork edge and the final edge into the row_union carry a
    branch alias; the hop between the two transforms in an arm is untagged.
    """
    pipeline = deep_thaw(_ab_row_union_proposal(guided).pipeline)
    nodes = pipeline["nodes"]
    for arm in ("control", "treatment"):
        stage_one = next(node for node in nodes if node["id"] == f"tag_{arm}")
        stage_one["on_success"] = f"{arm}_mid"
        nodes.insert(
            nodes.index(stage_one) + 1,
            {
                "id": f"score_{arm}",
                "node_type": "transform",
                "plugin": "value_transform",
                "input": f"{arm}_mid",
                "on_success": f"{arm}_scored",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}, "operations": []},
            },
        )
    return PipelineProposal.create(
        pipeline=pipeline,
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_fork_row_union_projection_accepts_multi_transform_branch_arms() -> None:
    guided = _ab_coalesce_guided()
    proposal = _multi_stage_row_union_proposal(guided)
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"value_transform", "batch_experiment_compare"}),
        "sink": frozenset({"json"}),
    }
    assert not guided_candidate_state(proposal).validate().errors

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )
    row_union = next(node for node in payload["nodes"] if node["node_type"] == "row_union")
    assert row_union["behavior"]["branch_aliases"] == ["branch-1", "branch-2"]
    incoming = [edge for edge in payload["graph"]["edges"] if edge["to_endpoint"].get("stable_id") == row_union["stable_id"]]
    assert [edge["flow"]["branch"] for edge in incoming] == ["branch-1", "branch-2"]
    # The arms' second stages are the producers, and their inbound hop is untagged.
    second_stage_ids = {edge["from_endpoint"]["stable_id"] for edge in incoming}
    interior = [
        edge
        for edge in payload["graph"]["edges"]
        if edge["to_endpoint"].get("stable_id") in second_stage_ids and edge["flow"]["kind"] == "node_success"
    ]
    assert len(interior) == 2
    assert all(edge["flow"]["branch"] is None for edge in interior)


def test_fork_row_union_projection_binds_release_order_by_alias_not_fork_position() -> None:
    """A gate may fork straight into a row_union that releases in another order.

    ``fork_to`` order and ``branches`` order are both authored, and with a direct
    fork one edge list carries both. Neither authored order may be corrupted.
    """
    guided = _ab_coalesce_guided()
    pipeline = deep_thaw(_ab_row_union_proposal(guided).pipeline)
    pipeline["nodes"] = [node for node in pipeline["nodes"] if node["id"] not in ("tag_control", "tag_treatment")]
    union = next(node for node in pipeline["nodes"] if node["id"] == "variant_union")
    union["branches"] = {"treatment_branch": "treatment_branch", "control_branch": "control_branch"}
    union["input"] = "treatment_branch"
    proposal = PipelineProposal.create(
        pipeline=pipeline,
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"value_transform", "batch_experiment_compare"}),
        "sink": frozenset({"json"}),
    }
    assert not guided_candidate_state(proposal).validate().errors

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )
    gate = next(node for node in payload["nodes"] if node["node_type"] == "gate")
    row_union = next(node for node in payload["nodes"] if node["node_type"] == "row_union")
    # The gate keeps its authored fork_to order...
    assert [item["branch"] for item in gate["behavior"]["fork_branches"]] == ["branch-1", "branch-2"]
    # ...while the row_union keeps its authored, divergent release order.
    assert row_union["behavior"]["branch_aliases"] == ["branch-2", "branch-1"]
    incoming = [edge for edge in payload["graph"]["edges"] if edge["to_endpoint"].get("stable_id") == row_union["stable_id"]]
    assert [edge["flow"]["branch"] for edge in incoming] == ["branch-2", "branch-1"]


def _ab_coalesce_proposal_ordered(
    guided: GuidedSession,
    *,
    branch_order: tuple[int, int],
    node_order: tuple[int, int],
) -> PipelineProposal:
    """The fork/coalesce A/B with the branch dict and LLM node order permuted."""
    branches = [("a_rows", "a_out"), ("b_rows", "b_out")]
    llm_nodes = [
        {
            "id": "llm_variant_a",
            "node_type": "transform",
            "plugin": "llm",
            "input": "a_rows",
            "on_success": "a_out",
            "on_error": "discard",
            "options": {"provider": "openrouter"},
        },
        {
            "id": "llm_variant_b",
            "node_type": "transform",
            "plugin": "llm",
            "input": "b_rows",
            "on_success": "b_out",
            "on_error": "discard",
            "options": {"provider": "openrouter"},
        },
    ]
    ordered_branches = {branches[i][0]: branches[i][1] for i in branch_order}
    ordered_llm = [llm_nodes[i] for i in node_order]
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "source": {
                    "plugin": "csv",
                    "on_success": "csv_rows",
                    "options": {"path": "blob:00000000-0000-0000-0000-000000000001", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "fork_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "csv_rows",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "len(row['color_name']) > 0",
                    "routes": {"true": "fork", "false": "fork"},
                    "fork_to": ["a_rows", "b_rows"],
                },
                *ordered_llm,
                {
                    "id": "reconcile",
                    "node_type": "coalesce",
                    "plugin": None,
                    "input": "a_out",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "branches": ordered_branches,
                    "policy": "require_all",
                    "merge": "union",
                },
                {
                    "id": "cleanup",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "reconcile",
                    "on_success": "colour_ab_out",
                    "on_error": "discard",
                    "options": {},
                },
            ],
            "edges": [],
            "outputs": [{"name": "colour_ab_out", "plugin": "json", "options": {"path": "out.json"}, "on_write_failure": "discard"}],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_fork_coalesce_projection_is_invariant_to_branch_and_node_ordering() -> None:
    """The coalesce projection must validate+verify for every authored order.

    Regression for live guided session 63f0b04a (AuditIntegrityError at
    validate_payload:1011, "coalesce branch aliases do not match its incoming
    flows"): the planner authors the coalesce ``branches`` dict and its
    branch-producer nodes in a nondeterministic order, and the validator requires
    ``behavior.branch_aliases`` to equal the incoming-flow branch order. Deriving
    the behavior aliases from the coalesce's own incoming edges makes them agree
    by construction, so every permutation must project AND re-verify.
    """
    guided = _ab_coalesce_guided()
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"llm", "field_mapper"}),
        "sink": frozenset({"json"}),
    }
    for branch_order in permutations((0, 1)):
        for node_order in permutations((0, 1)):
            proposal = _ab_coalesce_proposal_ordered(guided, branch_order=branch_order, node_order=node_order)
            payload = build_guided_proposal_projection(
                proposal_id=PROPOSAL_ID,
                proposal=proposal,
                guided=guided,
                catalog_plugin_ids=catalog,
            )
            verify_guided_proposal_projection(
                payload=payload,
                proposal_id=PROPOSAL_ID,
                proposal=proposal,
                guided=guided,
                catalog_plugin_ids=catalog,
            )


def test_projection_rejects_plugins_outside_the_same_catalog_snapshot() -> None:
    guided = _guided()
    with pytest.raises(AuditIntegrityError, match="catalog"):
        build_guided_proposal_projection(
            proposal_id=PROPOSAL_ID,
            proposal=_proposal(guided),
            guided=guided,
            catalog_plugin_ids={
                "source": frozenset({"csv"}),
                "transform": frozenset(),
                "sink": frozenset({"json"}),
            },
        )


def _linear_proposal_missing_transform_on_error(guided: GuidedSession) -> PipelineProposal:
    """A sealed plan whose transform omits on_error — schema-legal and validated.

    The set_pipeline tool schema types node ``on_error`` as ["string","null"]
    and requires only id/node_type/input, and ``build_set_pipeline_candidate``
    DERIVES ``on_error or "discard"`` for transform/aggregation nodes — so the
    plan passes candidate validation. The proposal then seals the raw planner
    dict (not the candidate state), so the derived key is absent here.
    """
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"credentials": {"secret_ref": CANARIES[1]}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "clean",
                    "node_type": "transform",
                    "plugin": "normalize",
                    "input": "rows",
                    "on_success": "cleaned",
                    "options": {"rules": [{"column": "name", "operation": "strip"}]},
                }
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": {"path": CANARIES[4]},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_transform_with_on_error_omitted_projects_the_derived_discard_error_flow() -> None:
    """A validation-accepted plan must not die at projection over an omitted on_error.

    Regression for the live guided A/B failure (session 3324b106, slog
    ``composer.guided_projection_invalid``): the projection adapter defaulted
    an omitted transform ``on_error`` to None instead of the candidate
    builder's "discard", dropping the node_error edge and tripping the wire
    contract's "exact success and error flows" check as an integrity 500.
    """
    guided = _guided()
    proposal = _linear_proposal_missing_transform_on_error(guided)
    catalog = {
        "source": frozenset({"csv"}),
        "transform": frozenset({"normalize"}),
        "sink": frozenset({"json"}),
    }

    payload = build_guided_proposal_projection(
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )

    # The transform projects exactly one success flow (to the output) and the
    # derived error flow to the discard endpoint — the wire contract's shape.
    node_id = payload["nodes"][0]["stable_id"]
    transform_flows = sorted(
        (edge["flow"]["kind"], edge["to_endpoint"]["kind"])
        for edge in payload["graph"]["edges"]
        if edge["from_endpoint"].get("stable_id") == node_id
    )
    assert transform_flows == [("node_error", "discard"), ("node_success", "output")]
    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog,
    )


@pytest.mark.asyncio
async def test_guided_planner_request_carries_evidence_and_manifest_without_private_values(tmp_path: Path) -> None:
    """The guided planner provider request's Task-10 additions leak nothing.

    Pins the request-level context the planner actually sends: the
    session-tracked schema evidence (rehydrated through the live policy view)
    and the information manifest with its aid-supplied flips and static
    batching usage line. The canary sweep runs over the WHOLE serialized
    payload BEFORE the equality pins — it is the no-egress proof for every
    addition, evidence content included (no option values, no policy-hidden
    identities).
    """
    # A deferred intent carrying the private option path/value canaries rides
    # the session, so the DEFERRED_* sweeps below can actually fail: the
    # provider projection must reduce the constraint to closed structural
    # facts (value_present, value_type) with neither canary present.
    guided = replace(
        _guided(),
        deferred_intents=(
            DeferredStageIntent.create(
                intent_id="00000000-0000-4000-8000-000000000120",
                receiving_stage="output",
                target_stage="topology",
                catalog_kind="source",
                catalog_name="csv",
                redacted_summary="Apply a private option constraint.",
                originating_message_id="00000000-0000-4000-8000-000000000121",
                message_content_hash=stable_hash("private option instruction"),
                constraints=(
                    OptionValueConstraint(
                        kind="option_value",
                        subject=StableSubject(kind="stable", component_kind="source", stable_id=SOURCE_ID),
                        option_path=(DEFERRED_PATH_CANARY, "value"),
                        operator="equals",
                        value=DEFERRED_VALUE_CANARY,
                    ),
                ),
            ),
        ),
    )
    state = CompositionState(
        sources={},
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
        guided_session=guided,
    )
    full_catalog = create_catalog_service()
    plugin_snapshot = PluginAvailabilitySnapshot.for_trained_operator(full_catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(full_catalog, plugin_snapshot)
    # Reviewed guided facts activate blob-custody verification, which
    # requires a live session database and an owning session row even when
    # no source is blob-bound.
    session_engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(session_engine)
    sessions = SessionServiceImpl(
        session_engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.proposal-audit-projection"),
    )
    session = await sessions.create_session("planner-user", "guided planner request pin", "local")
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="DECLINE: nothing further to add.", tool_calls=None))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            model="provider/planner-v1",
            id="request-1",
        )

    async def before_start() -> None:
        return None

    @asynccontextmanager
    async def request_scope() -> Any:
        yield

    async def on_settled(outcome: str) -> None:
        return None

    with pytest.raises(PlannerDeclined):
        await plan_pipeline(
            intent="Wire the reviewed input to the reviewed output.",
            current_state=state,
            provider_current_state=guided_redacted_current_state_context(state),
            reviewed_facts=guided_private_reviewed_facts(guided),
            reviewed_planner_context=guided_redacted_planner_context(guided),
            unproducible_output_fields=(),
            schemas_loaded=frozenset({("source", "csv"), ("sink", "json"), ("transform", "field_mapper")}),
            mark_schema_loaded=None,
            eligible_deferred_intent_ids=(),
            claim_evaluator=None,
            supersedes_draft_hash=None,
            surface=PlannerSurface.GUIDED_STAGED,
            profile="ordinary",
            policy_catalog=policy_catalog,
            plugin_snapshot=plugin_snapshot,
            originating_message=PlannerOriginatingMessage(
                session_id=str(session.id),
                message_id=str(CHECKPOINT_ID),
                content="Wire the reviewed input to the reviewed output.",
                user_id="planner-user",
            ),
            base=PresentBase(state_id=CHECKPOINT_ID, composition_content_hash="a" * 64),
            model_config=PlannerModelConfig(
                completion=completion,
                model_identifier="anthropic/claude-planner",
                provider="test-provider",
                temperature=0.0,
                seed=7,
                timeout_seconds=5.0,
                max_composition_turns=4,
                max_discovery_turns=3,
                max_tool_calls_per_turn=3,
                max_api_attempts=1,
                api_retry_base_seconds=0.0,
                discovery_reasoning_effort="none",
                candidate_reasoning_effort="none",
            ),
            rendered_skill=f"{load_pipeline_capability_core()}\n\nYou are the bounded ELSPETH pipeline planner.",
            repair_budget=1,
            budget_policy=PlannerBudgetPolicy(
                max_total_provider_calls=4,
                max_request_bytes=1_000_000,
                max_completion_tokens=800,
                max_cumulative_provider_cost=Decimal("1.00"),
            ),
            custody_config=PlannerCustodyConfig(
                data_dir=str(tmp_path),
                session_engine=session_engine,
                max_storage_per_session=1_000_000,
                secret_service=None,
                runtime_preflight=None,
            ),
            lifecycle=PlannerRequestLifecycle(
                before_start=before_start,
                request_scope=request_scope,
                on_settled=on_settled,
                progress=None,
            ),
            recorder=BufferingRecorder(),
            candidate_finalizer=lambda candidate: candidate,
        )

    assert len(requests) == 1
    payload_text = requests[0]["messages"][1]["content"]
    # Canary sweep FIRST: the equality pins below are meaningful only after
    # the whole payload — evidence included — is proven value-free.
    for canary in CANARIES:
        assert canary not in payload_text
    assert DEFERRED_VALUE_CANARY not in payload_text
    assert DEFERRED_PATH_CANARY not in payload_text

    payload = json.loads(payload_text)
    evidence = payload["schema_contract_evidence"]
    # Referenced-first ordering with an empty topology falls back to
    # (kind, name); every session-loaded identity is rehydrated whole.
    assert [entry["plugin_id"] for entry in evidence["schemas"]] == ["sink/json", "source/csv", "transform/field_mapper"]
    assert evidence["omitted"] == []
    assert payload["information_manifest"] == {
        "supplied": {
            "pipeline_state": "current_projection",
            "plugin_selection": "policy_snapshot",
            "model_catalog": "authoring_aids",
            "expression_grammar": "authoring_aids",
        },
        "discoverable_classes": [
            "plugin.schema",
            "plugin.assistance",
            "blob.metadata",
            "validation.code",
            "secret.reference",
        ],
        "unresolved": [],
        "discovery_usage": "Remaining discovery calls may be issued together in a single turn.",
    }
    # The redacted reviewed context rides unchanged beside the additions.
    assert payload["reviewed_facts"] == guided_redacted_planner_context(guided)


def test_planner_requires_private_provider_safe_and_model_claim_authority() -> None:
    model_signature = inspect.signature(plan_pipeline)
    for name in (
        "reviewed_facts",
        "reviewed_planner_context",
        # F2: the session schema tracker is threaded explicitly — a caller
        # that stops passing it fails loudly instead of silently reverting
        # the planner surface to "no schemas loaded".
        "schemas_loaded",
        "mark_schema_loaded",
        "eligible_deferred_intent_ids",
        "claim_evaluator",
        "supersedes_draft_hash",
    ):
        assert model_signature.parameters[name].default is inspect.Parameter.empty

    verifier_signature = inspect.signature(verified_remaining_deferred_intents)
    assert tuple(verifier_signature.parameters) == ("guided", "proposal")
    assert get_type_hints(verified_remaining_deferred_intents)["return"] == tuple[DeferredStageIntent, ...]
